"""TCP 5004 console. History is display-only; live lines drive the GUI model."""
from collections import deque
import socket
import struct
import threading
import time

HELLO = b"SONARCON1\r\n"
WELCOME = b"CONSOLE1\r\n"


class ConsoleDecoder:
    def __init__(self):
        self.pending = bytearray()
        self.line = bytearray()
        self.live = False
        self.discard_line = False

    def feed(self, data):
        self.pending.extend(data)
        events = []
        while len(self.pending) >= 4:
            magic, kind, length = struct.unpack_from("<BBH", self.pending)
            if magic != ord("C") or length > 512 or kind not in b"HLRG":
                raise ValueError("Invalid Ethernet console frame.")
            if len(self.pending) < 4 + length:
                break
            payload = bytes(self.pending[4:4 + length])
            del self.pending[:4 + length]
            if kind == ord("R"):
                if length or self.live or self.line:
                    raise ValueError("Invalid console live boundary.")
                self.live = True
                events.append(("console", True))
                continue
            if kind == ord("G"):
                if length != 8:
                    raise ValueError("Invalid console gap report.")
                lost = struct.unpack("<Q", payload)[0]
                if self.live:
                    raise ValueError(f"Console lost {lost} bytes; reconnecting for fresh state.")
                self.line.clear()
                self.discard_line = True
                events.append(("log", f"Console history: {lost} older bytes were overwritten in the board's buffer."))
                continue
            if (kind == ord("L")) != self.live:
                raise ValueError("Console history/live ordering error.")
            for value in payload:
                if self.discard_line:
                    if value == 10:
                        self.discard_line = False
                    continue
                if value == 10:
                    text = self.line.rstrip(b"\r").decode("utf-8", errors="replace")
                    self.line.clear()
                    events.append(("line" if self.live else "history", text))
                else:
                    self.line.append(value)
                    if len(self.line) > 8192:
                        raise ValueError("Console line exceeded 8192 bytes.")
        return events


class ConsoleWorker(threading.Thread):
    def __init__(self, board, local_ip, emit, port=5004, socket_factory=socket.socket):
        super().__init__(name="sonar-console", daemon=True)
        self.board, self.local_ip, self.emit = board, local_ip, emit
        self.port, self.socket_factory = port, socket_factory
        self.cancel = threading.Event()
        self.lock = threading.Lock()
        self.commands = deque()
        self.sock = None
        self.connected = False

    def send(self, data):
        if not isinstance(data, bytes) or not 0 < len(data) <= 64:
            raise ValueError("Console commands must contain 1-64 bytes.")
        with self.lock:
            if self.cancel.is_set() or not self.connected:
                raise OSError("Ethernet console is disconnected.")
            if data.lower() == b"x":
                self.commands.clear()
            if len(self.commands) >= 32:
                raise OSError("Console command queue is full.")
            self.commands.append(data)

    def stop(self):
        self.cancel.set()
        with self.lock:
            self.connected = False
            self.commands.clear()
            sock = self.sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def run(self):
        while not self.cancel.is_set():
            sock = None
            try:
                sock = self.socket_factory(socket.AF_INET, socket.SOCK_STREAM)
                with self.lock:
                    self.sock = sock
                sock.settimeout(2.0)
                if self.local_ip:
                    sock.bind((self.local_ip, 0))
                sock.connect((self.board, self.port))
                if self.cancel.is_set():
                    return
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.sendall(HELLO)
                reply = bytearray()
                while len(reply) < len(WELCOME):
                    data = sock.recv(len(WELCOME) - len(reply))
                    if not data:
                        raise OSError("Console closed during handshake.")
                    reply.extend(data)
                if bytes(reply) != WELCOME:
                    raise ValueError("Unexpected console handshake; install the matching firmware.")
                sock.settimeout(0.05)
                decoder = ConsoleDecoder()
                last_heartbeat = 0.0
                while not self.cancel.is_set():
                    # Stop discards unsent starts. Already sent bytes cannot be recalled.
                    with self.lock:
                        command = self.commands.popleft() if self.commands else None
                        if command is not None:
                            sock.sendall(command)
                    if time.monotonic() - last_heartbeat >= 1.0:
                        sock.sendall(b"\0")
                        last_heartbeat = time.monotonic()
                    try:
                        data = sock.recv(8192)
                    except socket.timeout:
                        continue
                    if not data:
                        raise OSError("Board closed the console connection.")
                    for kind, value in decoder.feed(data):
                        if self.cancel.is_set():
                            return
                        if kind == "console":
                            with self.lock:
                                self.connected = True
                        self.emit(kind, value)
            except (OSError, ValueError) as error:
                if not self.cancel.is_set():
                    self.emit("log", f"Ethernet console interrupted: {error} Retrying in 2 seconds.")
            finally:
                with self.lock:
                    self.connected = False
                    self.commands.clear()  # Never replay commands after reconnection.
                    self.sock = None
                if sock is not None:
                    sock.close()
                self.emit("console", False)
            self.cancel.wait(2.0)
