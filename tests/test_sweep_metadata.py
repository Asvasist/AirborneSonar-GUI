"""Sweep-angle start sequence and the board's per-recording WAV metadata."""
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))
from capture_store import CaptureStore
from sonar_audio import wav_chunk
from sonar_gui_control import ExperimentControl, parse_sweep, recordings

APPLIED = dict(config_id=1, mode=0, duration_us=35000, amplitude_pct=40, waveform_id=0)


def status(state="IDLE", steps=20, sweep=360, used=0):
    ready = int(state in {"IDLE", "STOPPED"})
    tail = "" if sweep is None else f" sweep_deg={sweep} overruns=0"
    return (f"SONAR {state} cycle=3 steps={steps} divisor=8 receiver=1 motor_pos=60 DDR={used}/32"
            f" config=1 mode=GENERATE tx_us=35000 amplitude=40 waveform=0 audio_ready={ready}"
            f" tx_sample_hz=96000 rx_sample_hz=96000 rx_mode=STANDARD pdm_hz=2400000{tail}")


def wav(frames=4, extra=b""):
    data = bytes(frames * 16 * 2)
    fmt = struct.pack("<HHIIHH", 1, 16, 96000, 96000 * 32, 32, 16)
    body = b"WAVE" + b"fmt " + struct.pack("<I", 16) + fmt + b"data" + struct.pack("<I", len(data)) + data + extra
    return b"RIFF" + struct.pack("<I", len(body)) + body


def chunk(name, body):
    padded = body + bytes(-len(body) % 4)
    return name + struct.pack("<I", len(body)) + padded


class SweepTests(unittest.TestCase):
    def control(self, board_sweep=360):
        self.now = 10.0
        self.sent, self.log = [], []
        c = ExperimentControl(self.sent.append, self.log.append, lambda: self.now)
        c.console_connection(True)
        c.ethernet_connection(True)
        c.audio_configuration(APPLIED)
        c.line(status(sweep=board_sweep))
        return c

    def test_parse_and_recording_count(self):
        self.assertEqual(parse_sweep("90"), 90)
        for bad in ("0", "361", "45.5", ""):
            with self.assertRaises(ValueError):
                parse_sweep(bad)
        self.assertEqual(recordings(8, 360), 8)
        self.assertEqual(recordings(-8, 90), 9)

    def test_partial_sweep_sends_g_then_n_then_r(self):
        c = self.control()
        c.start("8", sweep="90")
        self.assertEqual(self.sent[-1], b"g")
        c.line("Enter sweep angle in whole degrees (1..360), then Enter:")
        self.assertEqual(self.sent[-1], b"90\r")
        c.line("Sweep stored: 90 degrees in 30 step(s), 31 recording(s). Press R to run; X stops.")
        self.assertEqual(self.sent[-1], b"n")
        c.line("Enter signed positions per 360 degrees (1..1000), then Enter:")
        c.line("Positions-per-revolution stored. Press R for one complete 360-degree scan; X stops.")
        c.line(status(steps=8, sweep=360))  # Board did not take the sweep: no start.
        self.assertNotIn(b"r", self.sent)
        self.assertEqual(self.sent[-1], b"x")

    def test_partial_sweep_starts_when_board_confirms(self):
        c = self.control()
        c.start("8", sweep="90")
        c.line("Enter sweep angle in whole degrees (1..360), then Enter:")
        c.line("Sweep stored: 90 degrees in 30 step(s), 31 recording(s). Press R to run; X stops.")
        c.line("Enter signed positions per 360 degrees (1..1000), then Enter:")
        c.line("Positions-per-revolution stored. Press R for one complete 360-degree scan; X stops.")
        c.line(status(steps=8, sweep=90))
        self.assertEqual(self.sent[-1], b"r")

    def test_matching_sweep_skips_g_and_old_firmware_keeps_360(self):
        c = self.control(board_sweep=90)
        c.start("8", sweep="90")
        self.assertEqual(self.sent[-1], b"n")
        old = self.control(board_sweep=None)
        old.start("8")  # 360 works without the sweep option.
        self.assertEqual(self.sent[-1], b"n")
        with self.assertRaises(ValueError):
            self.control(board_sweep=None).start("8", sweep="90")


class BoardMetadataTests(unittest.TestCase):
    def test_wav_chunk_lookup(self):
        payload = wav(extra=chunk(b"smta", b'{"a":1}'))
        self.assertEqual(wav_chunk(payload, b"smta"), b'{"a":1}')
        self.assertIsNone(wav_chunk(wav(), b"smta"))
        self.assertIsNone(wav_chunk(b"not a wav", b"smta"))

    def test_capture_json_carries_board_metadata(self):
        board = {"scan": {"index": 3, "requested_angle_deg": 22.5}, "motor": {"final_position": 5500}}
        payload = wav(extra=chunk(b"smta", json.dumps(board).encode("ascii")))
        meta = dict(sequence=7, trigger_request_us=5_000_000, flags=0x100, channels=16)
        with tempfile.TemporaryDirectory() as folder:
            path = CaptureStore(folder).save(meta, payload)
            saved = json.loads(path.with_suffix(".json").read_text())
            self.assertEqual(saved["board_metadata"], board)
            self.assertEqual(path.read_bytes(), payload)  # WAV kept byte for byte.


if __name__ == "__main__":
    unittest.main()
