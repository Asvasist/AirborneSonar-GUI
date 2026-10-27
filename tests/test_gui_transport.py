"""Exercise the desktop transports with fake ports and sockets only."""
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))
from sonar_gui_transport import EthernetWorker
from test_ethernet import Fragmented, record


class Socket(Fragmented):
    def __init__(self, data):
        super().__init__(data)
        self.closed = False

    def shutdown(self, how):
        self.closed = True

    def close(self):
        self.closed = True


class FastEvent(threading.Event):
    def wait(self, timeout=None):
        return super().wait(0.001)


class GuiTransportTests(unittest.TestCase):
    def args(self, output):
        return SimpleNamespace(output=output, board=None, port=5001, interface="Ethernet", local_ip=None)

    def test_receiver_reconnects_and_persists_before_event(self):
        with tempfile.TemporaryDirectory() as folder:
            sock = Socket(b"READY2\r\n" + record())
            attempts, events = [], []
            def connector(args, log):
                attempts.append(1)
                if len(attempts) == 1:
                    raise TimeoutError("connection timed out")
                return sock, "test-board", "test-laptop"
            def emit(kind, value):
                events.append((kind, value))
                if kind == "record":
                    self.assertEqual(Path(value[0]).read_bytes(), b"\x01\x80\x02\x40")
                    self.assertTrue(Path(value[0]).with_suffix(".json").is_file())
                    self.assertIn(b"ACK2", sock.sent)
                    worker.stop()
            worker = EthernetWorker(self.args(folder), emit, connector)
            self.addCleanup(worker.cleanup)
            worker.cancel = FastEvent()
            worker.run()
            self.assertEqual(len(attempts), 2)
            self.assertIn(("ethernet", True), events)
            self.assertEqual(len(list(Path(folder).rglob("*.bin"))), 1)
            self.assertTrue(sock.closed)

    def test_bad_payload_not_saved_or_acknowledged(self):
        with tempfile.TemporaryDirectory() as folder:
            sock = Socket(b"READY2\r\n" + record()[:-1] + b"\xff")
            def emit(kind, value):
                if kind == "log" and "checksum" in value:
                    worker.stop()
            worker = EthernetWorker(self.args(folder), emit, lambda args, log: (sock, "board", "local"))
            self.addCleanup(worker.cleanup)
            worker.run()
            self.assertNotIn(b"ACK2", sock.sent)
            self.assertFalse(list(Path(folder).rglob("*.bin")))

    def test_cancel_while_connecting_closes_late_socket_without_handshake(self):
        with tempfile.TemporaryDirectory() as folder:
            sock = Socket(b"READY2\r\n")
            entered, release = threading.Event(), threading.Event()
            def connect(args, log):
                entered.set()
                release.wait(2)
                return sock, "board", "local"
            events = []
            worker = EthernetWorker(self.args(folder), lambda *e: events.append(e), connect)
            self.addCleanup(worker.cleanup)
            worker.start()
            self.assertTrue(entered.wait(2))
            worker.stop()
            release.set()
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertTrue(sock.closed)
            self.assertEqual(sock.sent, b"")
            self.assertNotIn(("ethernet", True), events)

    def test_stop_releases_blocking_receive(self):
        with tempfile.TemporaryDirectory() as folder:
            blocked, release = threading.Event(), threading.Event()
            class Blocking(Socket):
                def recv(self, n):
                    if self.data:
                        return super().recv(n)
                    blocked.set()
                    release.wait(2)
                    return b""
                def shutdown(self, how):
                    release.set()
                    super().shutdown(how)
            sock = Blocking(b"READY2\r\n")
            worker = EthernetWorker(self.args(folder), lambda *e: None, lambda args, log: (sock, "board", "local"))
            self.addCleanup(worker.cleanup)
            worker.start()
            self.assertTrue(blocked.wait(2))
            worker.stop()
            worker.join(2)
            self.assertFalse(worker.is_alive())

    def test_idle_connected_receiver_does_not_create_output_folders(self):
        with tempfile.TemporaryDirectory() as folder:
            output=Path(folder)/"recordings"
            sock=Socket(b"READY2\r\n"+record(b"",0,30_000_000))
            events=[]
            def emit(kind,value):
                events.append((kind,value))
                if kind=="batch":worker.stop()
            worker=EthernetWorker(self.args(output),emit,lambda args,log:(sock,"board","local"))
            self.addCleanup(worker.cleanup)
            worker.run()
            self.assertIn(b"ACK2",sock.sent)
            self.assertFalse(output.exists())
            self.assertFalse(any(k=="folder" for k,_ in events))


if __name__ == "__main__":
    unittest.main()
