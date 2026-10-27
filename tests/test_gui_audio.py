import io
import json
from pathlib import Path
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
import wave
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))
from sonar_gui_audio import (audio_request, inspect_wav, matching_configuration,
                             save_configuration, ConfigurationWorker)
from sonar_configure import ControlClient, packet, FIELDS, HEADER, crc32
from capture_store import CaptureStore
from receive_ethernet import decode_header
from test_ethernet import record, Fragmented


def wav_data(samples=1680, rate=48000, channels=1, width=2):
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setparams((channels, width, rate, 0, "NONE", "not compressed"))
        w.writeframes(bytes(samples * channels * width))
    return out.getvalue()


def reply(**extra):
    r = dict.fromkeys(FIELDS, 0)
    r.update(capabilities=3, active_valid=1, config_id=7, mode=0,
             start_hz=2500, stop_hz=5600, duration_us=35000, amplitude_pct=40,
             tx_samples=1680, sample_hz=48000, max_samples=2352, capture_us=50000,
             start_margin_us=1000)
    r.update(extra)
    return r


class Sock:
    def __init__(self):
        self.closed = False
    def close(self):
        self.closed = True
    def shutdown(self, _):
        self.closed = True


class AudioTests(unittest.TestCase):
    def test_generate_ranges_and_duration(self):
        c = audio_request("GENERATE", "5600", "2500", "35", "40", "")
        self.assertEqual(c["duration_us"], 35000)
        self.assertEqual(audio_request("GENERATE", "1", "47999", "49", "100", "")["duration_us"], 49000)
        for field, bad in ((1, "0"), (2, "48000"), (3, "50"), (3, "0.001"), (3, "NaN"), (4, "101")):
            args = ["GENERATE", "2500", "5600", "35", "40", ""]
            args[field] = bad
            with self.subTest(field=field, bad=bad), self.assertRaises(ValueError):
                audio_request(*args)

    def test_wav_inspection_and_limits(self):
        r = inspect_wav(wav_data(channels=2, width=3))
        self.assertEqual((r["samples"], r["duration_us"], r["channels"], r["bits"]), (1680, 35000, 2, 24))
        for data in (wav_data(rate=7999), wav_data(samples=2400), wav_data()[:-1], b"x" * 65537):
            with self.assertRaises(ValueError):
                inspect_wav(data)
        self.assertEqual(inspect_wav(wav_data(samples=2352))["duration_us"], 49000)

    def test_extensible_stereo_and_nan(self):
        fmt = struct.pack("<HHIIHHHHI", 65534, 2, 48000, 192000, 4, 16, 22, 16, 3)
        fmt += bytes.fromhex("0100000000001000800000aa00389b71")
        body = b"WAVEfmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", 4) + bytes(4)
        self.assertEqual(inspect_wav(b"RIFF" + struct.pack("<I", len(body)) + body)["samples"], 1)
        data = bytearray(wav_data(samples=2, width=4))
        struct.pack_into("<H", data, 20, 3)
        struct.pack_into("<I", data, 44, 0x7fc00000)
        with self.assertRaisesRegex(ValueError, "NaN"):
            inspect_wav(bytes(data))

    def test_verify_checks_all_settings_and_pending_flags(self):
        r = reply()
        self.assertTrue(matching_configuration(r, dict(r)))
        for field in ("config_id", "start_hz", "amplitude_pct", "duration_us", "pending_flags"):
            changed = dict(r)
            changed[field] += 1
            self.assertFalse(matching_configuration(r, changed))
        w = reply(mode=1, waveform_id=5, available_waveform_id=5, available_pcm_crc32=123)
        self.assertTrue(matching_configuration(w, dict(w)))
        self.assertFalse(matching_configuration(w, dict(w, available_pcm_crc32=124)))

    def test_bound_socket_protocol_and_fragmentation(self):
        r = reply()
        data = struct.pack("<22I", *(r[k] for k in FIELDS))
        class Connected(Fragmented):
            def close(self):
                pass
        sock = Connected(packet(8 | 0x80000000, 1, data))
        with patch("socket.create_connection", side_effect=AssertionError("must use bound socket")):
            with ControlClient("board", connected_socket=sock) as client:
                self.assertEqual(client.status(), r)
        self.assertEqual(HEADER.unpack(sock.sent[:28])[3], 8)

    def test_worker_preserves_original_wav_and_saves_before_applied_event(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "input.wav"
            original = wav_data(rate=96000)  # Native rate: uploaded unchanged.
            path.write_bytes(original)
            calls, events = [], []
            result = reply(mode=1, waveform_id=2, available_waveform_id=2)
            sock = Sock()
            class Client:
                def __init__(self, board, connected_socket):
                    self_socket = connected_socket
                    self.assert_socket = self_socket
                def __enter__(self):
                    return self
                def __exit__(self, *_):
                    pass
                def upload_wav(self, data, amplitude, progress):
                    calls.append((data, amplitude))
                    progress(len(data), len(data))
                    return result
            def connector(args, log):
                self.assertEqual((args.port, args.board, args.interface), (5003, "board", "Ethernet"))
                return sock, "board", "local"
            def emit(kind, event):
                events.append(event)
                if event[1] == "applied":
                    manifest = Path(event[2][1])
                    self.assertTrue(manifest.is_file())
                    saved = json.loads(manifest.read_text())
                    self.assertEqual((manifest.parent / saved["source_copy"]).read_bytes(), original)
            args = SimpleNamespace(board="board", port=5001, interface="Ethernet", local_ip=None)
            worker = ConfigurationWorker(9, "apply", args, temp, emit,
                {"mode": "WAV", "path": str(path), "amplitude_pct": 40}, connector=connector, client_factory=Client)
            worker.run()
            self.assertEqual(calls, [(original, 40)])
            self.assertIn("applied", [e[1] for e in events])
            self.assertTrue(sock.closed)

    def test_cancel_after_connect_never_applies_or_reenables_start(self):
        events = []
        sock = Sock()
        def connector(args, log):
            worker.stop()
            return sock, "board", "local"
        worker = ConfigurationWorker(1, "apply", SimpleNamespace(port=5001), None,
             lambda *event: events.append(event), {"mode": "GENERATE"}, connector=connector,
             client_factory=lambda *a, **k: self.fail("Cancelled job created a client"))
        worker.run()
        self.assertTrue(sock.closed)
        self.assertFalse(events)

    def test_manifest_failure_does_not_report_success(self):
        events = []
        class Client:
            def __init__(self, *a, **k):
                pass
            def __enter__(self):
                return self
            def __exit__(self, *_):
                pass
            def generate(self, *args):
                return reply()
        worker = ConfigurationWorker(1, "apply", SimpleNamespace(port=5001), "unused",
            lambda _, e: events.append(e), audio_request("GENERATE", "2500", "5600", "35", "40", ""),
            connector=lambda args, log: (Sock(), "board", "local"), client_factory=Client)
        with patch("sonar_gui_audio.save_configuration", side_effect=OSError("disk full")):
            worker.run()
        self.assertNotIn("applied", [e[1] for e in events])
        self.assertIn("disk full", next(e[2] for e in events if e[1] == "error"))

    def test_recording_uses_own_config_not_latest_and_unmatched_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            first = reply(config_id=7)
            save_configuration(temp, "board", first)
            save_configuration(temp, "board", reply(config_id=8, start_hz=3000))
            raw = bytearray(record()[:80])
            struct.pack_into("<I", raw, 20, (7 << 16) | 15)
            struct.pack_into("<I", raw, 64, 35000)
            struct.pack_into("<I", raw, 76, crc32(raw[:76]))
            meta = decode_header(raw)
            store = CaptureStore(temp)
            path = store.save(meta, bytes(4))
            saved = json.loads(path.with_suffix(".json").read_text())
            self.assertEqual(saved["transmit_configuration"]["configuration"]["start_hz"], 2500)
            self.assertEqual(saved["configuration_status"], "matched")
            unknown = dict(meta, sequence=2, cycle=2, config_id=9)
            path = store.save(unknown, bytes(4))
            self.assertEqual(json.loads(path.with_suffix(".json").read_text())["configuration_status"], "unavailable")
            with self.assertRaises(ValueError):
                save_configuration(temp, "board", first)


if __name__ == "__main__":
    unittest.main()
