"""Desktop command sequencing without connecting a board."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))
from sonar_gui_control import ExperimentControl, parse_steps


APPLIED = dict(config_id=1, mode=0, duration_us=35000, amplitude_pct=40, waveform_id=0)


def status(state="IDLE", steps=20, receiver=1, used=0, config=1):
    ready = int(state in {"IDLE", "STOPPED"})
    return (f"SONAR {state} cycle=3 steps={steps} divisor=1 receiver={receiver} motor_pos=60 DDR={used}/32"
            f" config={config} mode=GENERATE tx_us=35000 amplitude=40 waveform=0 audio_ready={ready}")


class GuiControlTests(unittest.TestCase):
    def setUp(self):
        self.now = 10.0
        self.sent, self.log = [], []
        self.c = ExperimentControl(self.sent.append, self.log.append, lambda: self.now)
        self.c.console_connection(True)
        self.c.ethernet_connection(True)
        self.c.audio_configuration(APPLIED)
        self.c.line(status())

    def enter_value(self, steps="45", reverse=False):
        self.c.start(steps, reverse)
        self.c.line("Enter signed positions per 360 degrees (1..1000), then Enter:")
        self.c.line("Positions-per-revolution stored. Press R for one complete 360-degree scan; X stops.")

    def test_start_requires_network_and_fresh_idle_and_board_receiver(self):
        self.assertTrue(self.c.can_start)
        self.c.ethernet_connection(False)
        with self.assertRaises(ValueError):
            self.c.start("20")
        self.c.ethernet_connection(True)
        self.c.line(status(receiver=0))
        self.assertFalse(self.c.can_start)
        self.c.line(status())
        self.now += 4
        self.assertFalse(self.c.can_start)
        self.assertEqual(self.sent, [b"s"])

    def test_confirmed_steps_before_start_and_only_one_start(self):
        self.enter_value()
        self.assertEqual(self.sent, [b"s", b"n", b"45\r"])
        self.c.line(status(steps=45))
        self.assertEqual(self.sent[-1], b"r")
        self.c.line(status("ACQUIRE", steps=45, used=1))
        self.c.line(status("MOVING", steps=45, used=1))
        self.now += 10
        self.c.tick()
        self.assertEqual(self.sent.count(b"r"), 1)
        self.assertFalse(self.c.can_start)

    def test_reverse_and_invalid_numbers(self):
        self.enter_value("100", True)
        self.assertEqual(self.sent[-1], b"-100\r")
        self.c.line(status(steps=-100))
        self.assertEqual(self.sent[-1], b"r")
        for value in ("0", "100001", "-20", "1.5", "NaN", "20\rr", "", "999999999"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_steps(value)
        self.assertEqual(parse_steps(" 1000 "), 1000)
        with self.assertRaises(ValueError):
            parse_steps("1001")

    def test_mismatch_or_receiver_lost_never_starts(self):
        self.enter_value()
        self.c.line(status(steps=20))
        self.assertNotIn(b"r", self.sent)
        self.assertEqual(self.sent[-1], b"x")

    def test_stop_cancels_pending_prompt_and_late_confirmation(self):
        self.c.start("45")
        self.c.stop()
        self.c.line("Enter signed positions per 360 degrees (1..1000), then Enter:")
        self.c.line("Positions-per-revolution stored.")
        self.c.line(status(steps=45))
        self.assertNotIn(b"r", self.sent)
        self.assertNotIn(b"45\r", self.sent)

    def test_network_loss_pending_start_stops_but_running_keeps_buffering(self):
        self.c.start("45")
        self.c.ethernet_connection(False)
        self.assertEqual(self.sent[-1], b"x")
        self.c.line(status("STOPPED"))
        self.c.ethernet_connection(True)
        self.c.audio_configuration(APPLIED)
        self.c.line(status("STOPPED"))
        self.enter_value()
        self.c.line(status(steps=45))
        self.c.line(status("WAIT_BUFFER", steps=45, used=32))
        before = list(self.sent)
        self.c.ethernet_connection(False)
        self.c.ethernet_connection(True)
        self.assertEqual(self.sent, before)

    def test_timeout_cancels_then_reports_unconfirmed_stop(self):
        self.c.start("45")
        self.now += 11
        self.c.tick()
        self.assertEqual(self.sent[-1], b"x")
        self.now += 11
        self.c.tick()
        self.assertIn("not been confirmed", self.c.error)
        self.assertFalse(self.c.can_start)

    def test_drain_requires_stopped_empty_fresh_status(self):
        self.c.line(status("MOVING", used=1))
        self.assertFalse(self.c.drained)
        self.c.stop()
        self.c.line(status("STOPPED", used=1))
        self.assertFalse(self.c.drained)
        self.c.line(status("STOPPED", used=0))
        self.assertTrue(self.c.drained)
        self.now += 4
        self.assertFalse(self.c.drained)

    def test_start_waits_for_previous_test_ddr_to_drain(self):
        self.c.line(status("STOPPED", used=2))
        self.assertFalse(self.c.can_start)
        before=list(self.sent)
        with self.assertRaises(ValueError):self.c.start("45")
        self.assertEqual(self.sent,before)
        self.c.line(status("STOPPED", used=0))
        self.assertTrue(self.c.can_start)

    def test_destination_prepared_once_after_confirmation_before_r(self):
        calls=[]
        self.c.before_run=lambda first,positions:calls.append((first,positions,list(self.sent)))
        self.enter_value("45")
        self.assertFalse(calls)
        self.c.line(status(steps=45))
        self.assertEqual(calls,[(4,45,[b"s",b"n",b"45\r"])])
        self.assertEqual(self.sent[-1],b"r")
        self.c.line(status("ACQUIRE",steps=45,used=1))
        self.assertEqual(len(calls),1)

    def test_configuration_copy_failure_prevents_start(self):
        def failed(*_):raise OSError("cached playback missing")
        self.c.before_run=failed
        self.enter_value("45")
        self.c.line(status(steps=45))
        self.assertNotIn(b"r",self.sent)
        self.assertEqual(self.sent[-1],b"x")
        self.assertIn("cached playback missing",self.c.error)

    def test_fault_latched_even_after_idle_report(self):
        self.c.line("HEALTH FAULT TIMEOUT")
        self.c.line(status())
        self.assertFalse(self.c.can_start)
        self.assertIn("HEALTH FAULT", self.c.error)

    def test_disconnect_invalidates_state_and_stop_cannot_be_claimed(self):
        self.c.console_connection(False)
        self.assertFalse(self.c.can_start)
        self.assertFalse(self.c.drained)
        with self.assertRaises(ValueError):
            self.c.stop()

    def test_missing_audio_or_old_firmware_never_starts(self):
        self.c.audio_configuration(None)
        self.assertTrue(self.c.can_configure)
        self.assertFalse(self.c.can_start)
        self.c.audio_configuration(APPLIED)
        self.c.line(status().split(" config=")[0])
        self.assertFalse(self.c.can_start)

    def test_changed_audio_during_handshake_cancels_start(self):
        self.enter_value()
        self.c.line(status(steps=45, config=2))
        self.assertNotIn(b"r", self.sent)
        self.assertEqual(self.sent[-1], b"x")

    def test_reset_discards_applied_configuration(self):
        self.c.line("INIT: cycle=PASS")
        self.c.line(status())
        self.assertIsNone(self.c.applied_audio)
        self.assertFalse(self.c.can_start)


if __name__ == "__main__":
    unittest.main()
