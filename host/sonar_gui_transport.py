"""Background Ethernet capture receiver for the desktop application."""
from pathlib import Path
import socket
import tempfile
import threading

from capture_store import utc_now
from sonar_test_store import TestCaptureStore
from receive_ethernet import read_exact, receive_record
from sonar_connection import connect_board, permission_help


class EthernetWorker(threading.Thread):
    def __init__(self, args, emit, connector=connect_board):
        super().__init__(name="sonar-ethernet", daemon=True)
        self.args, self.emit, self.connector = args, emit, connector
        self.cancel = threading.Event()
        self.lock = threading.Lock()
        self.sock = None
        self.tests = TestCaptureStore(args.output, emit)
        self.configuration_cache = None

    def begin_test(self, first_sequence, positions, configuration_folder, config_id, *, boot_id, test_id):
        return self.tests.begin_test(first_sequence, positions, configuration_folder, config_id,
                                     boot_id=boot_id, test_id=test_id)

    def request_test_stop(self):
        self.tests.request_stop()

    def finish_test(self, last_sequence):
        self.tests.finish_test(last_sequence)

    def reset_board(self):
        self.tests.reset_board()

    def cleanup(self):
        if self.configuration_cache is not None:
            self.configuration_cache.cleanup()
            self.configuration_cache = None

    def stop(self):
        self.cancel.set()
        with self.lock:
            if self.sock is not None:
                try:
                    self.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                self.sock.close()

    def log(self, message):
        self.emit("log", message)

    def run(self):
        try:
            # Apply may save WAV/configuration assets while idle; keep that cache
            # outside the user's recordings until a completed capture arrives.
            self.configuration_cache = tempfile.TemporaryDirectory(prefix="sonar_gui_config_")
            self.receive_loop(Path(self.configuration_cache.name))
        except (OSError, ValueError) as error:
            self.log(f"Receiver cannot create the recording session: {error}")
        finally:
            self.emit("ethernet", False)

    def receive_loop(self, folder):
        connection = 0
        while not self.cancel.is_set():
            sock = None
            try:
                sock, board, local = self.connector(self.args, log=self.log)
                with self.lock:
                    if self.cancel.is_set():
                        sock.close()
                        return
                    self.sock = sock
                sock.settimeout(10)
                sock.sendall(b"SONAR3\r\n")
                if read_exact(sock, 8) != b"READY3\r\n":
                    raise ValueError("Unexpected board handshake.")
                sock.settimeout(75)
                connection += 1
                configuration_folder = folder / f"connection_{connection:06d}"
                configuration_folder.mkdir(parents=True, exist_ok=True)
                self.tests.set_connection(dict(connected_utc=utc_now(), board=board,
                    port=self.args.port, local_ip=local))
                if self.cancel.is_set():
                    return
                self.emit("endpoint", {"board": board, "local_ip": local, "folder": str(configuration_folder)})
                self.emit("ethernet", True)
                self.log(f"Ethernet connected to {board}. Recordings arrive in 30-second batches.")
                bound_store = BootBoundStore(self.tests)
                last = 0
                while not self.cancel.is_set():
                    path, meta = receive_record(sock, bound_store)
                    if path:
                        seq = meta["sequence"]
                        if last and seq not in (last, last + 1):
                            self.log(f"Sequence discontinuity: previous={last}, received={seq}")
                        last = seq
                        self.emit("record", (str(path), meta))
                    else:
                        self.emit("batch", meta["trigger_request_us"] // 1_000_000)
            except (OSError, EOFError, ValueError) as error:
                if not self.cancel.is_set():
                    self.log(f"Ethernet interrupted: {error}. Retrying in 5 seconds; queued frames remain on the board.")
                    hint = permission_help(error)
                    if hint:
                        self.log(hint)
            finally:
                with self.lock:
                    self.sock = None
                    if sock is not None:
                        sock.close()
                self.emit("ethernet", False)
            self.cancel.wait(5)


class BootBoundStore:
    """One socket accepts recordings from only one immutable board boot."""
    def __init__(self, target):
        self.target = target
        self.boot_id = None

    def save(self, meta, payload):
        boot = meta["boot_id"]
        if payload:
            if self.boot_id is None:
                self.boot_id = boot
            elif boot != self.boot_id:
                raise ValueError("Boot identity changed on an existing recording connection.")
        return self.target.save(meta, payload)
