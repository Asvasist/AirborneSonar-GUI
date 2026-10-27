"""Receiver-mode selection and macOS/Linux/Windows portability."""
from pathlib import Path
import struct
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, PropertyMock, patch
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))
import sonar_connection as net
import sonar_platform
from receive_ethernet import HEADER as WIRE_HEADER, decode_header
from sonar_configure import (BoardError, ControlClient, FIELDS, HEADER, board_mode, crc32,
                             mode_switchable)
from sonar_gui_audio import ConfigurationWorker, matching_configuration, mode_error_text
from sonar_gui_control import ExperimentControl

MAC_IFCONFIG = """lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
\tinet 127.0.0.1 netmask 0xff000000
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tether 11:22:33:44:55:66
\tinet6 fe80::1%en0 prefixlen 64 scopeid 0x6
\tinet 192.168.1.20 netmask 0xffffff00 broadcast 192.168.1.255
en5: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet 169.254.59.10 netmask 0xffff0000 broadcast 169.254.255.255
\tstatus: active
"""
LINUX_IP = ("1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever\n"
            "2: eth0    inet 169.254.7.8/16 brd 169.254.255.255 scope link eth0\\       valid_lft forever\n")


def reply_bytes(opcode, request_id, **fields):
    values = dict.fromkeys(FIELDS, 0)
    values.update(fields)
    data = struct.pack("<30I", *(values[key] for key in FIELDS))
    head = HEADER.pack(b"ASC2", 2, HEADER.size, opcode | 0x80000000, request_id, len(data), crc32(data), 0)
    return head[:24] + struct.pack("<I", crc32(head[:24])) + data


class FakeSocket:
    def __init__(self, reply):
        self.reply, self.sent = reply, bytearray()

    def settimeout(self, _):
        pass

    def sendall(self, data):
        self.sent += data

    def recv(self, count):
        part, self.reply = self.reply[:count], self.reply[count:]
        return part

    def close(self):
        pass


class PortabilityTests(unittest.TestCase):
    def test_macos_ifconfig_selects_direct_cable_interface(self):
        rows = net.parse_ifconfig(MAC_IFCONFIG)
        self.assertEqual([(r["Name"], r["IPAddress"], r["PrefixLength"]) for r in rows],
                         [("lo0", "127.0.0.1", 8), ("en0", "192.168.1.20", 24), ("en5", "169.254.59.10", 16)])
        self.assertEqual(net.choose_interface(rows).address, "169.254.59.10")

    def test_linux_ip_output_is_parsed(self):
        rows = net.parse_ip_addr(LINUX_IP)
        self.assertEqual([(r["Name"], r["IPAddress"], r["PrefixLength"]) for r in rows],
                         [("lo", "127.0.0.1", 8), ("eth0", "169.254.7.8", 16)])

    def test_posix_adapter_name_filters_and_auto_detects(self):
        rows = net.parse_ifconfig(MAC_IFCONFIG)
        with patch.object(net.os, "name", "posix"), patch.object(net, "_posix_rows", return_value=rows):
            self.assertEqual(net.local_interface("auto").address, "169.254.59.10")
            self.assertEqual(net.local_interface("en0").address, "192.168.1.20")
            with self.assertRaisesRegex(OSError, "No Ethernet interface"):
                net.local_interface("en9")

    def test_two_link_local_interfaces_pick_the_one_that_hears_the_board(self):
        rows = [{"Name": "en0", "IPAddress": "169.254.1.1", "PrefixLength": 16},
                {"Name": "en5", "IPAddress": "169.254.9.9", "PrefixLength": 16}]
        with patch.object(net.os, "name", "posix"), patch.object(net, "_posix_rows", return_value=rows), \
                patch.object(net, "discover", side_effect=lambda item, timeout: "169.254.59.46" if item.address.endswith(".9.9") else None):
            self.assertEqual(net.local_interface("auto").address, "169.254.9.9")

    def test_manual_laptop_ip_without_prefix_means_direct_cable_subnet(self):
        item = net.local_interface("auto", "169.254.10.20")
        self.assertEqual((item.address, item.prefix), ("169.254.10.20", 16))
        self.assertEqual(net.local_interface("auto", "10.0.0.5/24").prefix, 24)

    def test_macos_permission_help_names_local_network_setting(self):
        with patch.object(net.sys, "platform", "darwin"):
            self.assertIn("Local Network", net.permission_help(PermissionError("x")))

    def test_platform_defaults(self):
        with patch.object(sonar_platform.os, "name", "posix"):
            self.assertEqual(sonar_platform.default_adapter(), "auto")
        with patch.object(sonar_platform.sys, "platform", "darwin"):
            self.assertEqual(sonar_platform.ui_fonts(), ("Helvetica Neue", "Menlo"))


class ReceiverModeTests(unittest.TestCase):
    def test_mode_flags(self):
        self.assertEqual(board_mode({"pending_flags": 0}), "STANDARD")
        self.assertEqual(board_mode({"pending_flags": 8 | 16}), "ULTRASONIC")
        self.assertTrue(mode_switchable({"pending_flags": 16}))
        self.assertFalse(mode_switchable({"pending_flags": 8}))

    def test_set_mode_sends_opcode_12_and_checks_the_reply(self):
        sock = FakeSocket(reply_bytes(12, 1, pending_flags=8 | 16))
        reply = ControlClient("x", connected_socket=sock).set_mode("ULTRASONIC")
        self.assertEqual(board_mode(reply), "ULTRASONIC")
        _, _, _, opcode, _, count, _, _ = HEADER.unpack(bytes(sock.sent[:HEADER.size]))
        self.assertEqual((opcode, count, bytes(sock.sent[HEADER.size:])), (12, 4, struct.pack("<I", 1)))
        stale = FakeSocket(reply_bytes(12, 1, pending_flags=16))
        with self.assertRaisesRegex(ValueError, "did not confirm"):
            ControlClient("x", connected_socket=stale).set_mode("ULTRASONIC")

    def test_board_refusals_are_explained(self):
        busy = BoardError({"error": 2})
        self.assertIn("Stop the scan", mode_error_text(busy))
        self.assertIn("bitstream", mode_error_text(BoardError({"error": 6})))
        self.assertIn("no receiver-mode support", mode_error_text(BoardError({"error": 1})))

    def test_mode_bits_do_not_break_idle_configuration_match(self):
        keys = ("config_id", "mode", "start_hz", "stop_hz", "duration_us", "amplitude_pct",
                "waveform_id", "tx_samples", "sample_hz", "capture_us", "boot_id")
        actual = {key: 1 for key in keys}
        actual.update(active_valid=1, mode=0, pending_flags=8 | 16)  # GENERATE, ultrasonic + switchable
        self.assertTrue(matching_configuration(dict(actual, pending_flags=0), actual))
        self.assertFalse(matching_configuration(dict(actual), dict(actual, pending_flags=1 | 16)))

    def test_console_status_reports_and_clears_mode(self):
        control = ExperimentControl(lambda _: None, lambda _: None)
        control.line("SONAR IDLE cycle=0 steps=30 divisor=8 receiver=1 motor_pos=0 DDR=0/32 config=1 "
                     "mode=GENERATE tx_us=1 amplitude=1 waveform=0 audio_ready=1 tx_sample_hz=96000 "
                     "rx_sample_hz=96000 rx_mode=ULTRASONIC pdm_hz=3072000")
        self.assertEqual(control.rx_mode, "ULTRASONIC")
        control.line("INIT: cycle=PASS")
        self.assertIsNone(control.rx_mode)

    def test_recording_header_names_the_mode(self):
        def header(pdm_hz):
            fields = [b"ASN3", 2, WIRE_HEADER.size, 0, 0, 0, 0, 16, pdm_hz, 0, 0, 0, 0,
                      8, 0, 50000, 0, *([0] * 8), 0]
            raw = WIRE_HEADER.pack(*fields)
            return raw[:-4] + struct.pack("<I", zlib.crc32(raw[:-4]))
        self.assertEqual(decode_header(header(2_400_000))["rx_mode"], "STANDARD")
        self.assertEqual(decode_header(header(3_072_000))["rx_mode"], "ULTRASONIC")
        self.assertEqual(decode_header(header(1_000_000))["rx_mode"], "UNKNOWN")

    def test_worker_switches_mode_and_reports_refusals(self):
        events = []
        client = MagicMock()
        client.__enter__.return_value = client
        client.set_mode.return_value = {"pending_flags": 8 | 16}
        run = ConfigurationWorker(3, "mode", SimpleNamespace(port=5001), None, lambda *e: events.append(e),
                                  {"rx_mode": "ULTRASONIC"}, connector=lambda *a, **k: (MagicMock(), "169.254.59.46", "x"),
                                  client_factory=lambda *a, **k: client)
        run.run()
        client.set_mode.assert_called_once_with("ULTRASONIC")
        self.assertIn(("audio", (3, "mode", {"pending_flags": 24})), events)
        events.clear()
        client.set_mode.side_effect = BoardError({"error": 2})
        run.run()
        self.assertTrue(any(e[1][1] == "mode_error" and "Stop the scan" in e[1][2] for e in events))


class GuiModeTests(unittest.TestCase):
    def test_selector_follows_board_and_start_requires_matching_mode(self):
        try:
            import tkinter as tk
            root = tk.Tk()
        except Exception as error:  # no display in some CI sessions
            self.skipTest(f"no display: {error}")
        import sonar_gui
        root.withdraw()
        app = sonar_gui.SonarApp(root)
        try:
            app.control.line("SONAR IDLE cycle=0 steps=30 divisor=8 receiver=1 motor_pos=0 DDR=0/32 config=1 "
                             "mode=GENERATE tx_us=1 amplitude=1 waveform=0 audio_ready=1 tx_sample_hz=96000 "
                             "rx_sample_hz=96000 rx_mode=ULTRASONIC pdm_hz=3072000")
            app.render()
            self.assertEqual(app.rx_mode.get(), "ULTRASONIC")
            app.rx_mode.set("STANDARD")  # user changes the selection without switching
            with patch.object(type(app.control), "can_start", new_callable=PropertyMock, return_value=True), \
                    patch.object(sonar_gui.messagebox, "showerror") as shown:
                app.start()
            self.assertIn("Switch mode", shown.call_args[0][1])
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
