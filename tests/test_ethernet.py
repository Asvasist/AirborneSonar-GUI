import contextlib
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))
from receive_ethernet import HEADER, decode_header, receive_record, run_receiver
from capture_store import CaptureStore
from plot_ethernet import channel_bits


def record(payload=b"\x01\x80\x02\x40", seq=1, stamp=100):
    raw = HEADER.pack(b"ASN2", 1, 80, len(payload), seq, seq, 15, 16, 2400000,
                      stamp, stamp + 100, -8, -4, 2, 2000000, 3125, zlib.crc32(payload), 0)
    return raw[:-4] + struct.pack("<I", zlib.crc32(raw[:-4])) + payload


class Fragmented:
    def __init__(self, data):
        self.data = data
        self.sent = b""
        self.timeouts = []

    def recv(self, n):
        part, self.data = self.data[:min(n, 3)], self.data[min(n, 3):]
        return part

    def sendall(self, data):
        self.sent += data

    def settimeout(self, value):
        self.timeouts.append(value)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class EthernetTests(unittest.TestCase):
    def test_fragmented_and_duplicate(self):
        with tempfile.TemporaryDirectory() as folder:
            store = CaptureStore(folder)
            for _ in range(2):
                sock = Fragmented(record(stamp=1_500_000))
                path, meta = receive_record(sock, store)
                self.assertEqual(path.parent.name, "second_0000000001")
                self.assertEqual(path.read_bytes(), b"\x01\x80\x02\x40")
                saved = json.loads(path.with_suffix(".json").read_text())
                self.assertTrue(all(saved[key] == value for key, value in meta.items()))
                self.assertEqual(sock.sent, struct.pack("<4sII", b"ACK2", 1, zlib.crc32(path.read_bytes())))
            self.assertEqual(len(list(Path(folder).rglob("*.bin"))), 1)
            self.assertFalse(list(Path(folder).rglob("*.part")))

    def test_bad_checksum_and_truncation_no_ack(self):
        with tempfile.TemporaryDirectory() as folder:
            store = CaptureStore(folder)
            for data in (record()[:-1], record()[:-1] + b"\xff", bytes(80)):
                sock = Fragmented(data)
                with self.assertRaises((EOFError, ValueError)):
                    receive_record(sock, store)
                self.assertFalse(sock.sent)
            self.assertFalse(list(Path(folder).rglob("*.bin")))

    def test_disk_error_no_ack(self):
        sock = Fragmented(record())
        with tempfile.TemporaryDirectory() as folder:
            store = CaptureStore(folder)
            with patch("capture_store.os.fsync", side_effect=OSError("disk failure")):
                with self.assertRaises(OSError):
                    receive_record(sock, store)
            self.assertFalse(list(Path(folder).rglob("*.bin")))
        self.assertFalse(sock.sent)

    def test_idle_marker_acknowledged_without_creating_second_folder(self):
        with tempfile.TemporaryDirectory() as folder:
            sock = Fragmented(record(b"", 0, stamp=3_000_000))
            path, _ = receive_record(sock, CaptureStore(folder))
            self.assertIsNone(path)
            self.assertEqual(len(sock.sent), 12)
            self.assertFalse(list(Path(folder).iterdir()))

    def test_capture_second_is_independent_of_delayed_delivery(self):
        with tempfile.TemporaryDirectory() as folder:
            store = CaptureStore(folder)
            receive_record(Fragmented(record(b"", 0, stamp=12_000_000)), store)
            path, _ = receive_record(Fragmented(record(stamp=2_999_999)), store)
            self.assertEqual(path.parent.name, "second_0000000002")
            saved = json.loads(path.with_suffix(".json").read_text())
            self.assertEqual(saved["board_batch_release_us"], 12_000_000)
            next_path, _ = receive_record(Fragmented(record(seq=2, stamp=3_000_000)), store)
            self.assertEqual(next_path.parent.name, "second_0000000003")

    def test_reconnect_after_timeout_and_mid_frame_disconnect(self):
        args = SimpleNamespace(board=None, port=5001, retry_delay=1.0, receive_timeout=75)
        partial = Fragmented(b"READY2\r\n" + record()[:-1])
        complete = Fragmented(b"READY2\r\n" + record(b"", 0, 1_000_000) + record())
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as folder:
            endpoints = [TimeoutError(), (partial, "169.254.59.46", "169.254.59.45"),
                         (complete, "169.254.59.46", "169.254.12.34"), KeyboardInterrupt()]
            with patch("receive_ethernet.connect_board", side_effect=endpoints) as connect, \
                    patch("receive_ethernet.time.sleep") as sleep, contextlib.redirect_stdout(output):
                with self.assertRaises(KeyboardInterrupt):
                    run_receiver(args, Path(folder))
            self.assertEqual(connect.call_count, 4)
            self.assertEqual(sleep.call_count, 3)
            self.assertEqual(len(list(Path(folder).rglob("*.bin"))), 1)
            self.assertEqual(partial.sent, b"SONAR2\r\n")
            self.assertIn(b"ACK2", complete.sent)
            self.assertEqual(complete.timeouts, [10, 75])
            metadata = json.loads((Path(folder) / "connection_000002" / "connection.json").read_text())
            self.assertEqual(metadata["local_ip"], "169.254.12.34")
        self.assertIn("Retrying automatically", output.getvalue())

    def test_full_current_dma_frame_in_delayed_batch(self):
        payload = bytes(range(250)) * 60  # 3750 words, exactly 15000 bytes.
        with tempfile.TemporaryDirectory() as folder:
            store = CaptureStore(folder)
            receive_record(Fragmented(record(b"", 0, stamp=30_000_000)), store)
            sock = Fragmented(record(payload, stamp=1_000_000))
            path, _ = receive_record(sock, store)
            self.assertEqual(path.read_bytes(), payload)
            self.assertEqual(len(sock.sent), 12)
            self.assertEqual(json.loads(path.with_suffix(".json").read_text())["board_batch_release_us"], 30_000_000)

    def test_conflicting_duplicate_is_not_acknowledged(self):
        with tempfile.TemporaryDirectory() as folder:
            store = CaptureStore(folder)
            receive_record(Fragmented(record()), store)
            conflict = Fragmented(record(b"\x00\x00\x00\x00"))
            with self.assertRaises(ValueError):
                receive_record(conflict, store)
            self.assertFalse(conflict.sent)

    def test_packing(self):
        data = b"\x01\x80\x02\x40"
        self.assertEqual(channel_bits(data, 0), [1, 0])
        self.assertEqual(channel_bits(data, 1), [0, 1])
        self.assertEqual(channel_bits(data, 15), [1, 0])
        self.assertEqual(channel_bits(data, 14), [0, 1])

    def test_c_wire_fixture(self):
        fixture = Path("wire_fixture.bin")
        if not fixture.exists():
            fixture = Path(__file__).parent / "fixtures" / "wire_v1.bin"
        meta = decode_header(fixture.read_bytes())
        self.assertEqual(meta["trigger_request_us"], 1234567890123)
        self.assertEqual(meta["commanded_position"], -16)
        self.assertEqual(meta["requested_steps"], -8)
        self.assertEqual(meta["rx_nominal_us"], 3125)


if __name__ == "__main__":
    unittest.main()
