"""Hidden Tk smoke tests. No network connections are opened."""
from pathlib import Path
import sys
import tkinter as tk
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))
from sonar_gui import SonarApp
from test_gui_control import status, APPLIED


class FakeWorker:
    def __init__(self):
        self.sent = []
        self.stopped = False
        self.tests = []
    def send(self, command):
        self.sent.append(command)
    def is_alive(self):
        return not self.stopped
    def stop(self):
        self.stopped = True
    def begin_test(self, first, positions, folder, config_id):
        self.tests.append((first, positions, folder, config_id))
        return Path(f"D:/test_{len(self.tests)}")
    def request_test_stop(self):
        pass
    def finish_test(self, cycle):
        pass
    def reset_board(self):
        pass
    def cleanup(self):
        pass


class GuiViewTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f"Tk display unavailable: {error}")
        self.root.withdraw()
        self.app = SonarApp(self.root)
        self.app.console_worker = FakeWorker()
        self.app.ethernet_worker = FakeWorker()

    def tearDown(self):
        if hasattr(self, "app"):
            self.root.after_cancel(self.app.timer)
        if hasattr(self, "root"):
            self.root.destroy()

    def test_view_command_path_and_receiver_remains_after_stop(self):
        app = self.app
        app.handle("console", True)
        app.handle("ethernet", True)
        app.control.audio_configuration(APPLIED)
        app.handle("line", status())
        app.render()
        self.assertEqual(str(app.start_button["state"]), "normal")
        app.endpoint = dict(folder="cached_configuration")
        app.steps.set("45")
        with patch.object(app, "audio_job") as job:
            app.start()
            self.assertEqual(job.call_args.args[0], "verify")
        app.handle_audio(app.audio_token, "verify", APPLIED)
        app.handle("line", "Enter signed positions per 360 degrees (1..1000), then Enter:")
        app.handle("line", "Positions-per-revolution stored.")
        app.handle("line", status(steps=45))
        app.handle("line", status("ACQUIRE", steps=45, used=1))
        self.assertEqual(app.console_worker.sent, [b"s", b"n", b"45\r", b"r"])
        self.assertEqual(app.ethernet_worker.tests, [(4,45,"cached_configuration",1)])
        app.stop()
        self.assertEqual(app.console_worker.sent[-1], b"x")
        self.assertFalse(app.ethernet_worker.stopped)
        app.handle("line", status("STOPPED", steps=45, used=1))
        app.request_disconnect()
        self.assertTrue(app.exit_pending)
        self.assertFalse(app.ethernet_worker.stopped)
        app.handle("line", status("STOPPED", steps=45, used=0))
        self.root.after_cancel(app.timer)
        app.poll()
        self.assertIsNone(app.console_worker)
        self.assertIsNone(app.ethernet_worker)

    def test_status_and_saved_record_render(self):
        app = self.app
        app.handle("record", (r"D:\capture_1.bin", {"payload_bytes": 15000, "cycle": 1, "rx_nominal_us": 3125}))
        app.render()
        self.assertIn("15,000 bytes", app.capture_text.get())
        self.assertIn("3.125 ms", app.detail_text.get())
        self.assertEqual(str(app.start_button["state"]), "disabled")

    def test_stop_drain_start_allocates_another_test(self):
        app=self.app
        app.handle("console",True)
        app.handle("ethernet",True)
        app.control.audio_configuration(APPLIED)
        app.handle("line",status())
        app.endpoint=dict(folder="cached_configuration")
        app.steps.set("3")
        def begin(cycle):
            with patch.object(app,"audio_job"):app.start()
            app.handle_audio(app.audio_token,"verify",APPLIED)
            app.handle("line","Enter signed positions per 360 degrees (1..1000), then Enter:")
            app.handle("line","Positions-per-revolution stored.")
            app.handle("line",status(steps=3).replace("cycle=3",f"cycle={cycle}"))
        begin(3)
        first=app.session_folder
        app.handle("line",status("ACQUIRE",steps=3,used=1).replace("cycle=3","cycle=4"))
        app.stop()
        app.handle("line",status("STOPPED",steps=3,used=1).replace("cycle=3","cycle=4"))
        app.render()
        self.assertEqual(str(app.start_button['state']),'disabled')
        self.assertIn('previous test',app.guidance.get())
        app.handle("line",status("STOPPED",steps=3,used=0).replace("cycle=3","cycle=4"))
        begin(4)
        self.assertNotEqual(first,app.session_folder)
        self.assertEqual([t[0] for t in app.ethernet_worker.tests],[4,5])

    def test_default_layout_fits_window(self):
        self.root.update_idletasks()
        self.assertLessEqual(self.root.winfo_reqwidth(), 1100)
        self.assertLessEqual(self.root.winfo_reqheight(), 900)

    def test_edit_clears_apply_and_late_cancelled_success_is_ignored(self):
        app = self.app
        app.handle("console", True)
        app.handle("ethernet", True)
        app.control.audio_configuration(APPLIED)
        app.handle("line", status())
        self.assertTrue(app.control.can_start)
        app.tx_start_hz.set("3000")
        self.assertFalse(app.control.can_start)
        old_token = app.audio_token
        app.cancel_audio()
        app.handle_audio(old_token, "applied", (APPLIED, "unused.json"))
        self.assertIsNone(app.control.applied_audio)

    def test_stop_during_verify_never_sends_late_start(self):
        app = self.app
        app.handle("console", True)
        app.handle("ethernet", True)
        app.control.audio_configuration(APPLIED)
        app.handle("line", status())
        with patch.object(app, "audio_job"):
            app.start()
        app.audio_worker = FakeWorker()
        old_token = app.audio_token
        app.stop()
        app.handle_audio(old_token, "verify", APPLIED)
        self.assertEqual(app.console_worker.sent, [b"s", b"x"])
        self.assertNotIn(b"n", app.console_worker.sent)

    def test_wav_selection_and_controls_lock_during_scan(self):
        app = self.app
        app.tx_mode.set("WAV")
        app.handle("console", True)
        app.handle("ethernet", True)
        app.handle("line", status("ACQUIRE"))
        app.render()
        self.assertEqual(str(app.wav_browse["state"]), "disabled")
        self.assertEqual(str(app.apply_button["state"]), "disabled")
        self.assertEqual(str(app.stop_button["state"]), "normal")


if __name__ == "__main__":
    unittest.main()
