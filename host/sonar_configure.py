#!/usr/bin/env python3
"""Configure Vitis on TCP 5003. Upload original WAV bytes; Vitis converts to PCM16.

This module can also be imported by a GUI worker thread. It never starts motion.
Only one connection is supported at a time; close it after each transaction.
"""
from __future__ import annotations

import argparse
import datetime as dt
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import socket
import struct
import sys
import zlib
import uuid
from sonar_wav_format import inspect_wav, prepare_wav
from sonar_sample_rates import TX_SAMPLE_HZ

HEADER = struct.Struct("<4sHHIIIII")
MAX_PAYLOAD = 1024
MAX_FILE_BYTES = 65536
FIELDS = (
    "error", "capabilities", "active_valid", "config_id", "mode",
    "start_hz", "stop_hz", "duration_us", "amplitude_pct", "waveform_id",
    "tx_samples", "available_waveform_id", "available_waveform_samples",
    "upload_received", "sample_hz", "max_samples", "capture_us",
    "start_margin_us", "available_pcm_crc32", "fpga_status", "pending_flags",
    "rx_filter_cutoff_hz",
    "boot_word_0", "boot_word_1", "boot_word_2", "boot_word_3",
    "test_word_0", "test_word_1", "test_word_2", "test_word_3",
)
ERRORS = (
    "OK", "BAD_REQUEST", "BUSY", "RANGE_OR_WAV_FORMAT", "CHECKSUM_OR_INCOMPLETE",
    "NO_WAVEFORM", "HARDWARE_NOT_READY", "TIMEOUT", "CONFIG_IDS_EXHAUSTED",
)

MODE_NAMES = ("STANDARD", "ULTRASONIC")


def board_mode(reply):
    """Receiver mode from the control reply: pending_flags bit 3 = ultrasonic."""
    return MODE_NAMES[1 if reply.get("pending_flags", 0) & 8 else 0]


def mode_switchable(reply):
    """pending_flags bit 4: the loaded bitstream can switch modes at run time."""
    return bool(reply.get("pending_flags", 0) & 16)


def playback_description(reply):
    caps = reply.get("capabilities", 0)
    if not caps & 2:
        return "WAV unavailable: install/configure the Vitis playback adapter and matching platform."
    n = min(reply.get("max_samples", 0), 4704)
    gain = "adjustable amplitude" if caps & 4 else "100% amplitude only"
    completion = "FPGA completion status" if caps & 8 else "estimated completion timing"
    return f"WAV ready: up to {n/96:g} ms at 96 kHz ({n:,} samples), {gain}, {completion}."


def crc32(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


def packet(opcode: int, request_id: int, payload: bytes = b"") -> bytes:
    if len(payload) > MAX_PAYLOAD:
        raise ValueError("Control payload exceeds 1024 bytes")
    head = HEADER.pack(b"ASC2", 2, HEADER.size, opcode, request_id,
                       len(payload), crc32(payload), 0)
    return head[:24] + struct.pack("<I", crc32(head[:24])) + payload


class BoardError(RuntimeError):
    def __init__(self, reply: dict):
        self.reply = reply
        code = reply["error"]
        name = ERRORS[code] if code < len(ERRORS) else str(code)
        super().__init__(f"Board rejected request: {name}")


class ControlClient:
    def __init__(self, board: str, port: int = 5003, *, connected_socket=None):
        self.socket = connected_socket if connected_socket is not None else socket.create_connection((board, port), timeout=7)
        self.socket.settimeout(7)
        self.request_id = 0

    def close(self):
        self.socket.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _read_exact(self, count: int) -> bytes:
        data = bytearray()
        while len(data) < count:
            part = self.socket.recv(count - len(data))
            if not part:
                raise ConnectionError("Board closed the control connection")
            data.extend(part)
        return bytes(data)

    def request(self, opcode: int, payload: bytes = b"") -> dict:
        self.request_id += 1
        if self.request_id > 0xFFFFFFFF:
            raise ValueError("Reconnect before request IDs wrap")
        self.socket.sendall(packet(opcode, self.request_id, payload))
        head = self._read_exact(HEADER.size)
        magic, version, size, op, rid, count, payload_crc, header_crc = HEADER.unpack(head)
        if (magic != b"ASC2" or version != 2 or size != HEADER.size
                or op != opcode | 0x80000000 or rid != self.request_id
                or count != len(FIELDS) * 4 or crc32(head[:24]) != header_crc):
            raise ValueError("Invalid control response header")
        data = self._read_exact(count)
        if crc32(data) != payload_crc:
            raise ValueError("Invalid control response checksum")
        reply = dict(zip(FIELDS, struct.unpack("<30I", data)))
        reply["boot_id"] = struct.pack("<4I", *(reply[f"boot_word_{i}"] for i in range(4))).hex()
        reply["test_id"] = struct.pack("<4I", *(reply[f"test_word_{i}"] for i in range(4))).hex()
        if reply["error"]:
            raise BoardError(reply)
        return reply

    def status(self) -> dict:
        return self.request(8)

    def ensure_boot_identity(self) -> dict:
        reply = self.request(1)
        if reply["boot_id"] == "0" * 32:
            reply = self.request(10, uuid.uuid4().bytes)
        return reply

    def prepare_test(self, boot_id: str, config_id: int) -> dict:
        test = uuid.uuid4().bytes
        reply = self.request(11, bytes.fromhex(boot_id) + test + struct.pack("<I", config_id))
        if reply["boot_id"] != boot_id or reply["test_id"] != test.hex():
            raise ValueError("Board did not confirm the requested recording identity.")
        return reply

    def set_mode(self, name: str) -> dict:
        """Switch the PL microphone clock (CTRL_SET_MODE). Board must be idle with DDR empty."""
        reply = self.request(12, struct.pack("<I", MODE_NAMES.index(name)))
        if board_mode(reply) != name:
            raise ValueError("Board did not confirm the requested receiver mode.")
        return reply

    def generate(self, start_hz: int, stop_hz: int, duration_us: int,
                 amplitude_pct: int) -> dict:
        caps = self.ensure_boot_identity()
        if not caps["capabilities"] & 1:
            raise RuntimeError("FPGA GENERATE interface is unavailable")
        if caps["sample_hz"] != TX_SAMPLE_HZ:
            raise ValueError("This GUI requires fixed-96-kHz Vitis firmware. Update Vitis before applying settings.")
        if (not 0 <= amplitude_pct <= 100 or not 1 <= start_hz < TX_SAMPLE_HZ // 2
                or not 1 <= stop_hz < TX_SAMPLE_HZ // 2 or not 1 <= duration_us <= 49000
                or (duration_us * TX_SAMPLE_HZ + 500000) // 1000000 < 2):
            raise ValueError("Invalid 96 kHz chirp settings: check frequency, duration and amplitude.")
        self.request(2, struct.pack("<4I", start_hz, stop_hz, duration_us, amplitude_pct))
        reply = self.request(7)
        expected_samples = (duration_us * TX_SAMPLE_HZ + 500000) // 1000000
        if (not reply["active_valid"] or reply["mode"] != 0
                or reply["start_hz"] != start_hz or reply["stop_hz"] != stop_hz
                or reply["amplitude_pct"] != amplitude_pct
                or reply["tx_samples"] != expected_samples or reply["sample_hz"] != TX_SAMPLE_HZ):
            raise ValueError("Applied configuration did not match the request")
        return reply

    def upload_wav(self, data: bytes, amplitude_pct: int = 100, *, progress=None) -> dict:
        if not isinstance(amplitude_pct, int) or not 0 <= amplitude_pct <= 100:
            raise ValueError("Amplitude must be a whole number from 0 to 100 percent")
        # Inspect before UPLOAD_BEGIN: the board has one buffer and a new upload
        # invalidates its previous WAV. Unsupported files must not overwrite it.
        info = inspect_wav(data, sample_hz=TX_SAMPLE_HZ)
        caps = self.ensure_boot_identity()
        if not caps["capabilities"] & 2:
            raise RuntimeError(playback_description(caps))
        if not caps["capabilities"] & 4 and amplitude_pct != 100:
            raise ValueError("This WAV player has no amplitude control. Use 100%, or enable the stride-4 software gain / FPGA linear gain in Vitis.")
        if caps["sample_hz"] != TX_SAMPLE_HZ or not 1 <= caps["max_samples"] <= 4704:
            raise ValueError("Board must run fixed-96-kHz firmware and report a valid BRAM capacity")
        inspect_wav(data, max_samples=caps["max_samples"], sample_hz=TX_SAMPLE_HZ)
        self.request(3, struct.pack("<2I", len(data), crc32(data)))
        for offset in range(0, len(data), MAX_PAYLOAD - 4):
            block = data[offset:offset + MAX_PAYLOAD - 4]
            self.request(4, struct.pack("<I", offset) + block)
            if progress is not None:
                progress(offset + len(block), len(data))
        uploaded = self.request(5)
        wave_id = uploaded["available_waveform_id"]
        if wave_id == 0 or uploaded["available_waveform_samples"] != info["samples"]:
            raise ValueError("Upload completed without a waveform ID")
        self.request(6, struct.pack("<2I", wave_id, amplitude_pct))
        reply = self.request(7)
        if (not reply["active_valid"] or reply["mode"] != 1
                or reply["waveform_id"] != wave_id or reply["amplitude_pct"] != amplitude_pct
                or reply["tx_samples"] != uploaded["available_waveform_samples"]
                or reply["sample_hz"] != TX_SAMPLE_HZ):
            raise ValueError("Applied waveform did not match the upload")
        return reply


def bounded_u32(value: str) -> int:
    number = int(value)
    if not 0 <= number <= 0xFFFFFFFF:
        raise argparse.ArgumentTypeError("Expected an unsigned 32-bit integer")
    return number


def amplitude(value: str) -> int:
    number = bounded_u32(value)
    if number > 100:
        raise argparse.ArgumentTypeError("Amplitude must be 0..100 percent")
    return number


def duration_us(value: str) -> int:
    try:
        us = Decimal(value) * 1000
        if not us.is_finite() or us != us.to_integral_value() or not 1 <= us <= 49000:
            raise ValueError()
        return int(us)
    except (InvalidOperation, ValueError):
        raise argparse.ArgumentTypeError("Use milliseconds up to 49, with at most 3 decimal places") from None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--board", default="169.254.59.46")
    parser.add_argument("--port", type=int, default=5003)
    parser.add_argument("--manifest-dir", type=Path, default=Path("sonar_configs"),
                        help="Save applied configuration here; keep it with your recordings")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    mode = sub.add_parser("mode", help="switch receiver mode (idle board, empty DDR)")
    mode.add_argument("name", choices=[name.lower() for name in MODE_NAMES])
    gen = sub.add_parser("generate")
    gen.add_argument("--start-hz", type=bounded_u32, required=True)
    gen.add_argument("--stop-hz", type=bounded_u32, required=True)
    gen.add_argument("--duration-ms", type=duration_us, required=True, dest="duration_us")
    gen.add_argument("--amplitude", type=amplitude, required=True)
    wav = sub.add_parser("wav")
    wav.add_argument("file", type=Path)
    wav.add_argument("--amplitude", type=amplitude, default=100)
    args = parser.parse_args(argv)
    source = None
    try:
        data = None
        if args.command == "wav":
            with args.file.open("rb") as stream:
                data = stream.read(MAX_FILE_BYTES + 1)
            if len(data) > MAX_FILE_BYTES:
                raise ValueError("WAV file exceeds 65536 bytes")
            source = {"name": args.file.name, "file_bytes": len(data),
                      "file_crc32": crc32(data), "sha256": hashlib.sha256(data).hexdigest()}
        if args.command == "wav":
            original = data
            data, transmission = prepare_wav(data, TX_SAMPLE_HZ)
            source["transmission"] = transmission
        if args.command not in ("status", "mode"):
            args.manifest_dir.mkdir(parents=True, exist_ok=True)
        with ControlClient(args.board, args.port) as client:
            if args.command == "generate":
                reply = client.generate(args.start_hz, args.stop_hz, args.duration_us, args.amplitude)
            elif args.command == "wav":
                reply = client.upload_wav(data, args.amplitude)
            elif args.command == "mode":
                reply = client.set_mode(args.name.upper())
            else:
                reply = client.status()
        print(json.dumps(reply, indent=2))
        if args.command not in ("status", "mode"):
            now = dt.datetime.now(dt.timezone.utc)
            manifest = {"protocol": "ASC2-v2", "board": args.board,
                        "configured_at_utc": now.isoformat(), "configuration": reply,
                        "source_wav": source, "rx_sample_hz": 96000,
                        "id_scope": "config_id is unique only until the board resets"}
            path = args.manifest_dir / f"{now:%Y%m%dT%H%M%S.%fZ}_config_{reply['config_id']:05d}.json"
            try:
                if args.command == "wav":
                    source_name = path.stem + "_source.wav"
                    playback_name = path.stem + "_playback.wav"
                    (path.parent / source_name).write_bytes(original)
                    (path.parent / playback_name).write_bytes(data)
                    manifest.update(source_copy=source_name, playback_copy=playback_name)
                with path.open("x", encoding="utf-8") as stream:
                    json.dump(manifest, stream, indent=2)
                    stream.write("\n")
            except OSError as error:
                print(f"Configuration WAS applied, but manifest could not be saved: {error}", file=sys.stderr)
                return 2
            print(f"Applied config {reply['config_id']}; manifest: {path}")
            print("Use the GUI/Ethernet console to start; x stops.")
        return 0
    except (OSError, ValueError, RuntimeError, struct.error) as error:
        print(str(error), file=sys.stderr)
        print("If the connection failed during APPLY, query status before retrying; the result may be unknown.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
