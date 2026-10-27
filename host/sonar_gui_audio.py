"""Audio validation, configuration persistence and cancellable TCP 5003 jobs."""
from decimal import Decimal, InvalidOperation
import hashlib
from pathlib import Path
import socket
import struct
import threading
from types import SimpleNamespace

from capture_store import utc_now, write_atomic, write_json
from sonar_configure import BoardError, ControlClient, MAX_FILE_BYTES, crc32
from sonar_wav_format import inspect_wav, prepare_wav
from sonar_sample_rates import TX_SAMPLE_HZ
from sonar_connection import connect_board

CONFIG_FIELDS = ("config_id", "mode", "start_hz", "stop_hz", "duration_us",
                 "amplitude_pct", "waveform_id", "tx_samples", "sample_hz", "capture_us", "boot_id")


def unsigned(text, label, low, high):
    value = str(text).strip()
    if not value.isascii() or not value.isdecimal() or len(value) > 10 or not low <= int(value) <= high:
        raise ValueError(f"{label} must be a whole number from {low} to {high}.")
    return int(value)


def audio_request(mode, start, stop, duration, amplitude, wav_path):
    request = {"mode": mode, "amplitude_pct": unsigned(amplitude, "Amplitude (%)", 0, 100)}
    if mode == "GENERATE":
        request["start_hz"] = unsigned(start, "Start frequency (Hz)", 1, 47999)
        request["stop_hz"] = unsigned(stop, "Stop frequency (Hz)", 1, 47999)
        try:
            us = Decimal(str(duration).strip()) * 1000
            if not us.is_finite() or us != us.to_integral_value() or not 1 <= us <= 49000:
                raise ValueError()
            request["duration_us"] = int(us)
        except (InvalidOperation, ValueError):
            raise ValueError("Duration must be at most 49 ms, with at most 3 decimal places.") from None
        if (request["duration_us"] * TX_SAMPLE_HZ + 500000) // 1000000 < 2:
            raise ValueError("A generated chirp needs at least two samples (use at least 0.016 ms).")
    elif mode == "WAV":
        if not str(wav_path).strip():
            raise ValueError("Select a WAV file first.")
        request["path"] = str(Path(wav_path).expanduser())
    else:
        raise ValueError("Choose Generate chirp or Upload WAV.")
    return request


def mode_error_text(error):
    """Plain-language reason a receiver-mode switch was refused."""
    code = error.reply["error"] if isinstance(error, BoardError) else None
    if code == 2:
        return "The board is busy. Stop the scan and wait until DDR shows 0, then switch mode."
    if code == 6:
        return ("The loaded bitstream cannot switch receiver modes at run time. "
                "Load the dual-mode hardware platform.")
    if code == 1:
        return "This firmware has no receiver-mode support. Load the dual-mode firmware."
    return str(error)


def read_waveform(path):
    with Path(path).open("rb") as stream:
        data = stream.read(MAX_FILE_BYTES + 1)
    return data, {**inspect_wav(data), "name": Path(path).name}


def matching_configuration(expected, actual):
    return (bool(actual.get("active_valid")) and not (actual.get("pending_flags", 7) & 7)
            and all(expected.get(key) == actual.get(key) for key in CONFIG_FIELDS)
            and (actual.get("mode") != 1 or
                 (actual.get("waveform_id") == actual.get("available_waveform_id")
                  and expected.get("available_pcm_crc32") == actual.get("available_pcm_crc32"))))


def save_configuration(folder, board, reply, source=None, data=None, transmitted=None):
    configs = Path(folder) / "configurations"
    configs.mkdir(parents=True, exist_ok=True)
    target = configs / f"config_{reply['config_id']:05d}.json"
    if target.exists():
        raise ValueError("Configuration ID already exists in this connection folder. Reconnect after a board reset.")
    manifest = {"protocol": "ASC2-v2", "board": board, "configured_at_utc": utc_now(),
                "configuration": reply, "source_wav": source, "rx_sample_hz": 96000,
                "id_scope": "This receiver connection; IDs restart on board reset."}
    if data is not None:
        name = f"config_{reply['config_id']:05d}_source.wav"
        write_atomic(configs / name, data)
        manifest["source_copy"] = name
    if transmitted is not None:
        name = f"config_{reply['config_id']:05d}_playback.wav"
        write_atomic(configs / name, transmitted)
        manifest["playback_copy"] = name
    write_json(target, manifest)
    return target


class ConfigurationWorker(threading.Thread):
    """A one-shot transaction. Every GUI event carries a job token."""
    def __init__(self, token, action, args, folder, emit, request=None, expected=None,
                 connector=connect_board, client_factory=ControlClient):
        super().__init__(name="sonar-configuration", daemon=True)
        self.token, self.action, self.args, self.folder = token, action, args, folder
        self.emit, self.request, self.expected = emit, request, expected
        self.connector, self.client_factory = connector, client_factory
        self.cancel = threading.Event()
        self.lock = threading.Lock()
        self.sock = None

    def event(self, kind, value):
        if not self.cancel.is_set():
            self.emit("audio", (self.token, kind, value))

    def stop(self):
        self.cancel.set()
        with self.lock:
            if self.sock is not None:
                try:
                    self.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                self.sock.close()

    def progress(self, sent, total):
        if self.cancel.is_set():
            raise OSError("Upload cancelled.")
        self.event("progress", f"Uploading WAV: {sent * 100 // total}% ({sent:,}/{total:,} bytes)")

    def run(self):
        applying = False
        try:
            data, source, original = None, None, None
            if self.action in ("preview", "apply") and self.request["mode"] == "WAV":
                original, source = read_waveform(self.request["path"])
                data, transmission = prepare_wav(original, TX_SAMPLE_HZ)
                source["transmission"] = transmission
                self.event("preview", source)
            if self.cancel.is_set() or self.action == "preview":
                return
            options = SimpleNamespace(**vars(self.args))
            options.port = 5003
            sock, board, _ = self.connector(options, log=lambda text: self.event("log", text))
            with self.lock:
                if self.cancel.is_set():
                    sock.close()
                    return
                self.sock = sock
            with self.client_factory(board, connected_socket=sock) as client:
                if self.action == "probe":
                    reply = client.ensure_boot_identity()
                elif self.action == "verify":
                    reply = client.status()
                    if not matching_configuration(self.expected, reply):
                        raise ValueError("Board configuration changed or is not ready. Apply the displayed settings again.")
                    reply = client.prepare_test(reply["boot_id"], reply["config_id"])
                elif self.action == "mode":
                    client.ensure_boot_identity()
                    reply = client.set_mode(self.request["rx_mode"])
                elif self.action == "apply":
                    applying = True
                    client.ensure_boot_identity()
                    if self.request["mode"] == "GENERATE":
                        reply = client.generate(self.request["start_hz"], self.request["stop_hz"],
                                                self.request["duration_us"], self.request["amplitude_pct"])
                    else:
                        reply = client.upload_wav(data, self.request["amplitude_pct"], progress=self.progress)
                else:
                    raise ValueError("Unknown configuration operation.")
            if self.cancel.is_set():
                return
            if self.action == "apply":
                target = save_configuration(self.folder, board, reply, source, original, data)
                self.event("applied", (reply, str(target)))
            else:
                self.event(self.action, reply)
        except (OSError, ValueError, RuntimeError, struct.error) as error:
            text = str(error)
            if applying:
                text += " Start is blocked. The board may have accepted settings; check status and apply again when ready."
            if self.action == "mode":
                self.event("mode_error", mode_error_text(error))
            else:
                self.event("error", text)
        finally:
            with self.lock:
                if self.sock is not None:
                    self.sock.close()
                self.sock = None
            self.event("finished", self.action)
