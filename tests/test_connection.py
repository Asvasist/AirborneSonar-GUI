import contextlib
import io
from pathlib import Path
import socket
import struct
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))
import sonar_connection as net


class ConnectionTests(unittest.TestCase):
    def test_link_local_preferred_and_changed_ip_selected(self):
        rows = [{"IPAddress": "192.168.1.3", "PrefixLength": 24, "InterfaceIndex": 8},
                {"IPAddress": "169.254.200.10", "PrefixLength": 16, "InterfaceIndex": 8}]
        selected = net.choose_interface(rows)
        self.assertEqual(selected.address, "169.254.200.10")
        self.assertIn(net.ipaddress.IPv4Address("169.254.59.46"), selected.network)
        rows[1]["IPAddress"] = "169.254.1.2"
        self.assertEqual(net.choose_interface(rows).address, "169.254.1.2")

    def test_missing_adapter_is_diagnosed(self):
        with self.assertRaisesRegex(OSError, "no ready IPv4"):
            net.choose_interface([])

    def test_windows_socket_pinned_to_selected_interface(self):
        sock = MagicMock()
        with patch.object(net.os, "name", "nt"):
            net.bind_interface(sock, net.Interface("169.254.20.30", 16, 8))
        sock.bind.assert_called_once_with(("169.254.20.30", 0))
        sock.setsockopt.assert_called_once_with(socket.IPPROTO_IP, getattr(socket, "IP_UNICAST_IF", 31), struct.pack("!I", 8))

    def test_discovery_checks_nonce_source_port_and_subnet(self):
        sock = MagicMock()
        sock.__enter__.return_value = sock
        sock.recvfrom.side_effect = [
            (b"SONARIP2wrong000", ("169.254.1.2", 5002)),
            (b"SONARIP212345678", ("169.254.1.2", 1234)),
            (b"SONARIP212345678", ("192.168.1.2", 5002)),
            (b"SONARIP212345678", ("169.254.1.2", 5002)), socket.timeout()]
        with patch.object(net.socket, "socket", return_value=sock), \
                patch.object(net.secrets, "token_bytes", return_value=b"12345678"):
            self.assertEqual(net.discover(net.Interface("169.254.20.30", 16)), "169.254.1.2")
        sock.sendto.assert_called_once_with(b"SONARWHO12345678", ("169.254.255.255", 5002))

    def test_multiple_boards_require_selection(self):
        sock = MagicMock()
        sock.__enter__.return_value = sock
        sock.recvfrom.side_effect = [(b"SONARIP212345678", ("169.254.1.2", 5002)),
                                    (b"SONARIP212345678", ("169.254.1.3", 5002)), socket.timeout()]
        with patch.object(net.socket, "socket", return_value=sock), \
                patch.object(net.secrets, "token_bytes", return_value=b"12345678"):
            with self.assertRaisesRegex(ValueError, "Multiple sonar"):
                net.discover(net.Interface("169.254.20.30", 16))

    def test_discovery_absent_on_older_firmware_allows_tcp_fallback(self):
        sock = MagicMock()
        sock.__enter__.return_value = sock
        sock.recvfrom.side_effect = ConnectionResetError("ICMP port unreachable")
        with patch.object(net.socket, "socket", return_value=sock):
            self.assertIsNone(net.discover(net.Interface("169.254.20.30", 16)))

    def test_reconnect_reads_new_local_address(self):
        args = SimpleNamespace(interface="Ethernet", local_ip=None, board="169.254.59.46", port=5001)
        sock = MagicMock()
        with patch.object(net, "local_interface", side_effect=[net.Interface("169.254.1.2", 16),
                                                               net.Interface("169.254.3.4", 16)]), \
                patch.object(net.socket, "socket", return_value=sock), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(net.connect_board(args)[2], "169.254.1.2")
            self.assertEqual(net.connect_board(args)[2], "169.254.3.4")

    def test_wrong_subnet_does_not_attempt_tcp(self):
        args = SimpleNamespace(interface="Ethernet", local_ip=None, board="169.254.59.46", port=5001)
        with patch.object(net, "local_interface", return_value=net.Interface("192.168.1.2", 24)), \
                patch.object(net.socket, "socket") as make:
            with self.assertRaisesRegex(OSError, "unrelated subnets"):
                net.connect_board(args)
        make.assert_not_called()

    def test_tcp_failure_closes_socket_and_diagnoses_permissions(self):
        args = SimpleNamespace(interface="Ethernet", local_ip=None, board="169.254.59.46", port=5001)
        sock = MagicMock()
        error = PermissionError("blocked")
        sock.connect.side_effect = error
        with patch.object(net, "local_interface", return_value=net.Interface("169.254.1.2", 16)), \
                patch.object(net.socket, "socket", return_value=sock), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(PermissionError):
                net.connect_board(args)
        sock.close.assert_called_once()
        self.assertIn("VPN", net.permission_help(error))
        self.assertIsNone(net.permission_help(TimeoutError()))


if __name__ == "__main__":
    unittest.main()
