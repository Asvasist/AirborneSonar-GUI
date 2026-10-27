#!/usr/bin/env python3
"""Receive sonar WAV or raw PDM frames over TCP; acknowledge only persisted records."""
import argparse
import os
from datetime import datetime
from pathlib import Path
import socket
import struct
import time
import zlib
from capture_store import CaptureStore, write_json, utc_now
from sonar_connection import connect_board, permission_help

HEADER = struct.Struct("<4sHH6IQQqi13I")
MAX_PAYLOAD = 16 * 1024 * 1024
RX_MODES = {2_400_000: "STANDARD", 3_072_000: "ULTRASONIC"}


def read_exact(sock, length):
    parts = bytearray()
    while len(parts) < length:
        block = sock.recv(length - len(parts))
        if not block:
            raise EOFError("Board closed the connection")
        parts.extend(block)
    return bytes(parts)


def decode_header(raw):
    if len(raw) != HEADER.size:
        raise ValueError("Truncated header")
    fields = HEADER.unpack(raw)
    keys = ("magic", "version", "header_bytes", "payload_bytes", "sequence", "cycle",
            "flags", "channels", "pdm_hz", "trigger_request_us", "dma_observed_us",
            "commanded_position", "requested_steps", "step_divisor", "tx_nominal_us",
            "rx_nominal_us", "payload_crc32",
            *(f"boot_word_{i}" for i in range(4)),
            *(f"test_word_{i}" for i in range(4)), "header_crc32")
    meta = dict(zip(keys, fields))
    if (meta.pop("magic") != b"ASN3" or meta["version"] != 2 or
            meta["header_bytes"] != HEADER.size or
            zlib.crc32(raw[:-4]) != meta["header_crc32"]):
        raise ValueError("Invalid header or checksum")
    if (meta["payload_bytes"] > MAX_PAYLOAD or meta["payload_bytes"] % 4 or
            meta["channels"] != 16 or meta["pdm_hz"] == 0 or
            meta["cycle"] != meta["sequence"]):
        raise ValueError("Unsupported frame dimensions")
    if bool(meta["payload_bytes"]) != bool(meta["sequence"]):
        raise ValueError("Invalid heartbeat/frame sequence")
    meta["boot_id"] = struct.pack("<4I", *(meta.pop(f"boot_word_{i}") for i in range(4))).hex()
    meta["test_id"] = struct.pack("<4I", *(meta.pop(f"test_word_{i}") for i in range(4))).hex()
    if meta["payload_bytes"] and (meta["boot_id"] == "0" * 32 or meta["test_id"] == "0" * 32):
        raise ValueError("Recording lacks a valid boot/test identity.")
    meta["config_id"] = (meta["flags"] >> 16) & 0xffff
    meta["tx_mode"] = ("WAV" if meta["flags"] & 0x200 else "GENERATE") if meta["config_id"] else "UNKNOWN"
    meta["tx_duration_us"] = meta["tx_nominal_us"]
    meta["rx_mode"] = RX_MODES.get(meta["pdm_hz"], "UNKNOWN")
    return meta


def receive_record(sock, store):
    meta = decode_header(read_exact(sock, HEADER.size))
    payload = read_exact(sock, meta["payload_bytes"])
    received_crc = zlib.crc32(payload) & 0xFFFFFFFF
    if received_crc != meta["payload_crc32"]:
        raise ValueError(
            f"Payload checksum mismatch; record not acknowledged: "
            f"sequence={meta['sequence']} bytes={len(payload)} "
            f"expected_crc=0x{meta['payload_crc32']:08X} "
            f"received_crc=0x{received_crc:08X} "
            f"boot_id={meta['boot_id']} test_id={meta['test_id']}")
    path = store.save(meta, payload)
    sock.sendall(struct.pack("<4sII", b"ACK3", meta["sequence"], meta["payload_crc32"]))
    return path, meta


def run_receiver(args, folder):
    """Keep one Python process alive across network/board disconnects."""
    connection = 0
    while True:
        phase = "TCP connection"
        try:
            sock, board, local_ip = connect_board(args)
            with sock:
                phase = "protocol handshake"
                sock.settimeout(10)
                sock.sendall(b"SONAR3\r\n")
                if read_exact(sock, 8) != b"READY3\r\n":
                    raise ValueError("Unexpected board protocol")
                # A healthy board deliberately sends no records for 30 seconds.
                sock.settimeout(args.receive_timeout)
                connection += 1
                store = CaptureStore(folder / f"connection_{connection:06d}")
                write_json(store.folder / "connection.json", {
                    "connected_utc": utc_now(), "board": board, "port": args.port, "local_ip": local_ip,
                })
                print("Connected. Data arrives in 30-second batches; keep this window open. "
                      "Use the GUI or TCP 5004 console to start a scan; this receiver never starts capture.")
                phase = "record reception"
                last, saved = 0, 0
                while True:
                    path, meta = receive_record(sock, store)
                    if path:
                        seq = meta["sequence"]
                        if last and seq not in (last, last + 1):
                            print(f"Sequence discontinuity: previous={last}, received={seq}")
                        last = seq
                        saved += 1
                        print(f"Saved {path}: {meta['payload_bytes']} bytes, cycle {seq}, flags=0x{meta['flags']:x}")
                    else:
                        second = meta["trigger_request_us"] // 1_000_000
                        print(f"Batch at board second {second}; {saved} frame deliveries saved so far. "
                              "Queued recordings follow this marker. No audio file is created for a status marker.")
        except (OSError, EOFError, ValueError) as error:
            print(f"Interrupted during {phase}: {error}")
            hint = permission_help(error)
            if hint:
                print(hint)
            print(f"Retrying automatically in {args.retry_delay:g} second(s); keep this window open. "
                  "Unacknowledged captures remain in board RAM unless the board is reset or powered off.")
            time.sleep(args.retry_delay)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--board", help="Explicit board IPv4 address; default: discover on Ethernet")
    parser.add_argument("--interface", default="Ethernet" if os.name == "nt" else "auto",
                        help="wired adapter name (Windows) or interface such as en5 (macOS); auto = detect")
    parser.add_argument("--local-ip", help="Optional wired IPv4 address/prefix, e.g. 169.254.59.45/16")
    parser.add_argument("--port", type=int, default=5001)
    parser.add_argument("--output", type=Path, default=Path("captures"))
    parser.add_argument("--retry-delay", type=float, default=5.0)
    parser.add_argument("--receive-timeout", type=float, default=75.0,
                        help="Socket idle timeout in seconds; must exceed the 30-second batch interval")
    args = parser.parse_args()
    if not 0 < args.retry_delay <= 60:
        parser.error("--retry-delay must be greater than 0 and at most 60 seconds")
    if not 35 <= args.receive_timeout <= 3600:
        parser.error("--receive-timeout must be between 35 and 3600 seconds")
    folder = args.output / datetime.now().strftime("session_%Y%m%d_%H%M%S_%f")
    folder.mkdir(parents=True, exist_ok=False)
    write_json(folder / "session.json", {
        "started_utc": utc_now(), "board": args.board, "port": args.port,
        "folder_time_basis": "board software capture-request timestamp, in seconds",
    })
    print(f"Saving recordings in {folder.resolve()}")
    try:
        run_receiver(args, folder)
    except KeyboardInterrupt:
        print("Receiver stopped. Unacknowledged frames remain on the board until reset.")


if __name__ == "__main__":
    main()
