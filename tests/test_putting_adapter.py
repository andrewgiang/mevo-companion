import http.client
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import cv2
import numpy as np
from PySide6.QtCore import QCoreApplication

from companion.putting_adapter import (PuttingAdapter, PuttingServer, parse_putt,
                                       resolve_camera, validate_native_config, native_view_health,
                                       native_camera_backend, stock_child_environment,
                                       launch_stock_tracker)


PAYLOAD = {"ballData": {"BallSpeed": "2.42", "TotalSpin": 0, "LaunchDirection": "-1.37"}}
DEVICES = [{"id": "usb:one", "name": "First camera", "index": 2},
           {"id": "usb:two", "name": "Second camera", "index": 0}]


class SchemaTests(unittest.TestCase):
    def test_stock_process_cannot_consume_desktop_command_pipe(self):
        with patch("companion.putting_adapter.launch_external") as launch:
            launch_stock_tracker(["ball_tracking.exe", "-c", "yellow"], Path("."))
        self.assertEqual(launch.call_args.kwargs["stdin"], subprocess.DEVNULL)

    def test_stock_values_are_preserved(self):
        shot = parse_putt(PAYLOAD, captured_at=12.0)
        self.assertEqual(shot.source, "webcam")
        self.assertEqual(shot.speed_mph, 2.42)
        self.assertEqual(shot.hla, -1.37)
        self.assertEqual(shot.spin_rpm, 0)
        self.assertEqual(shot.captured_at, 12.0)

    def test_malformed_and_nonfinite_values_are_rejected(self):
        for value in (None, [], {}, {"ballData": []}, {"ballData": {"BallSpeed": 2}},
                      {"ballData": {**PAYLOAD["ballData"], "BallSpeed": "NaN"}},
                      {"ballData": {**PAYLOAD["ballData"], "BallSpeed": True}},
                      {"ballData": {**PAYLOAD["ballData"], "BallSpeed": -2}},
                      {"ballData": {**PAYLOAD["ballData"], "LaunchDirection": 400}}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_putt(value)

    def test_camera_identity_remaps_after_index_change(self):
        camera = resolve_camera({"camera_id": "USB:ONE", "camera_index": 0}, DEVICES)
        self.assertEqual(camera["index"], 2)

    def test_missing_saved_camera_never_falls_back_to_another(self):
        with self.assertRaises(ValueError):
            resolve_camera({"camera_id": "usb:missing", "camera_index": 0}, DEVICES)

    def test_duplicate_identity_is_ambiguous(self):
        with self.assertRaises(ValueError):
            resolve_camera({"camera_id": "usb:one"}, DEVICES + [DEVICES[0]])

    def test_native_config_validation_does_not_rewrite_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.ini"
            content = "[putting]\nstartx1=10\nstartx2=180\ny1=180\ny2=450\nradius=0\nmjpeg=1\ncustomhsv={'hmin': 8,'hmax': 50,'smin': 20,'smax': 250,'vmin': 30,'vmax': 255}\n"
            path.write_text(content)
            self.assertTrue(validate_native_config(path)[0])
            self.assertEqual(path.read_text(), content)
            path.write_text(content.replace("startx2=180", "startx2=5"))
            self.assertFalse(validate_native_config(path)[0])

    def test_native_config_rejects_values_that_crash_stock_startup(self):
        base = "[putting]\nstartx1=10\nstartx2=180\ny1=180\ny2=450\nradius=0\nmjpeg=1\n"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.ini"
            for extra in ("fps=oops", "width=-2", "customhsv={'hmin': 8}", "customhsv=[]", "exposure=nan"):
                with self.subTest(extra=extra):
                    path.write_text(base + extra)
                    self.assertFalse(validate_native_config(path)[0])

    def test_imported_non_mjpeg_mode_uses_matching_enumeration_backend(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.ini"
            self.assertEqual(native_camera_backend(path), cv2.CAP_DSHOW)
            path.write_text("[putting]\nmjpeg=0\n")
            self.assertEqual(native_camera_backend(path), cv2.CAP_MSMF)

    def test_stock_child_does_not_inherit_parent_frozen_library_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = str(Path(tmp) / "engine")
            unrelated = str(Path(tmp) / "engine-other")
            path = os.pathsep.join([parent, str(Path(parent) / "opencv"), unrelated])
            with patch("companion.putting_adapter.sys._MEIPASS", parent, create=True), patch.dict(os.environ, {"PATH": path, "_MEIPASS2": parent}):
                environment = stock_child_environment()
                self.assertEqual(environment["PATH"], unrelated)
                self.assertEqual(environment["PYINSTALLER_RESET_ENVIRONMENT"], "1")
                self.assertNotIn("_MEIPASS2", environment)

    def test_original_no_frame_banner_blocks_readiness(self):
        frame = np.full((480, 640, 3), 255, dtype=np.uint8)
        cv2.putText(frame, "Error: No Frame", (20, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 0, 0))
        self.assertFalse(native_view_health(frame)[0])
        self.assertIn("cannot read", native_view_health(frame)[1])
        self.assertFalse(native_view_health(cv2.resize(frame, (960, 720)))[0])

    def test_startup_splash_does_not_count_as_camera_ready(self):
        frame = np.full((480, 640, 3), 255, dtype=np.uint8)
        cv2.putText(frame, "Starting Video", (20, 100), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 0, 0))
        self.assertFalse(native_view_health(frame)[0])

    def test_rendered_scene_can_clear_startup_warning(self):
        frame = np.full((480, 640, 3), (25, 65, 25), dtype=np.uint8)
        cv2.rectangle(frame, (10, 180), (180, 450), (255, 255, 0), 2)
        cv2.circle(frame, (75, 240), 15, (255, 255, 255), -1)
        self.assertTrue(native_view_health(frame)[0])


class ReceiverTests(unittest.TestCase):
    def setUp(self):
        self.received = []
        self.server = PuttingServer(self.received.append, port=0)
        self.server.start()

    def tearDown(self):
        self.server.stop()

    def request(self, payload, route="/putting"):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=3)
        try:
            connection.request("POST", route, body=json.dumps(payload), headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_actual_stock_callback_and_response(self):
        status, response = self.request(PAYLOAD)
        self.assertEqual(status, 200)
        self.assertEqual(response, {"result": "Success"})
        self.assertEqual(len(self.received), 1)
        self.assertEqual(self.received[0].hla, -1.37)

    def test_invalid_request_is_not_forwarded(self):
        self.assertEqual(self.request({"no": "ball"})[0], 400)
        self.assertEqual(self.received, [])

    def test_unknown_endpoint_is_rejected(self):
        self.assertEqual(self.request(PAYLOAD, "/arbitrary")[0], 404)
        self.assertEqual(self.received, [])

    def test_port_conflict_is_explicit_and_preserves_other_server(self):
        other = PuttingServer(lambda shot: None, self.server.port)
        with self.assertRaises(OSError):
            other.start()
        self.assertEqual(self.request(PAYLOAD)[0], 200)

    def test_stop_releases_port(self):
        port = self.server.port
        self.server.stop()
        other = PuttingServer(lambda shot: None, port)
        try:
            other.start()
        finally:
            other.stop()


class RunningProcess:
    def poll(self):
        return None


class AdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.adapter = PuttingAdapter({"camera_id": "usb:one", "ball_color": "orange2", "width": 640}, self.tmp.name)
        self.adapter._process = RunningProcess()
        self.adapter._timer.stop()
        self.live, self.preview = [], []
        self.adapter.shot.connect(self.live.append)
        self.adapter.preview_shot.connect(self.preview.append)

    def tearDown(self):
        self.adapter._process = None
        self.adapter.stop()
        self.tmp.cleanup()

    def test_setup_putt_is_preview_only_even_if_arm_requested(self):
        self.adapter._calibration = True
        with patch.object(self.adapter, "_apply_window_state"):
            self.adapter.set_active(True)
        self.adapter._receive_shot(parse_putt(PAYLOAD))
        self.assertEqual(len(self.preview), 1)
        self.assertEqual(self.live, [])

    def test_native_opencv_traceback_keeps_context_and_reports_actual_exception(self):
        output = ("FPS 60\nTraceback (most recent call last):\n"
                  "  File \"ball_tracking.py\", line 1286, in <module>\n"
                  "    vs.set(cv2.CAP_PROP_SETTINGS, 37)\n"
                  "cv2.error: OpenCV: camera settings failed\n"
                  "> Driver did not expose the settings property\n"
                  "FPS 60\n")
        process = SimpleNamespace(pid=1234, stdout=io.BytesIO(output.encode()))
        self.adapter._process = process
        with self.assertLogs("companion.putting_adapter", level="ERROR") as captured:
            self.adapter._read_output(process)
        self.assertEqual(len(captured.output), 1)
        self.assertIn('File "ball_tracking.py", line 1286', captured.output[0])
        self.assertIn("vs.set(cv2.CAP_PROP_SETTINGS, 37)", captured.output[0])
        self.assertIn("Driver did not expose", captured.output[0])
        self.assertNotIn("FPS 60", captured.output[0])
        self.assertEqual(self.adapter._fault, "cv2.error: OpenCV: camera settings failed > Driver did not expose the settings property")
        self.assertIn("    vs.set", self.adapter.last_output)
        self.assertTrue(process.stdout.closed)

    def test_native_traceback_flushes_at_exit_and_is_bounded(self):
        output = "Traceback (most recent call last):\n" + ("  repeated frame\n" * 100) + "PermissionError: config.ini is not writable\n"
        process = SimpleNamespace(pid=1234, stdout=io.BytesIO(output.encode()))
        self.adapter._process = process
        with self.assertLogs("companion.putting_adapter", level="ERROR") as captured:
            self.adapter._read_output(process)
        self.assertEqual(len(captured.output), 1)
        self.assertLess(captured.output[0].count("repeated frame"), 40)
        self.assertIn("Additional traceback lines omitted", captured.output[0])
        self.assertIn("PermissionError: config.ini is not writable", captured.output[0])
        self.assertEqual(self.adapter._fault, "PermissionError: config.ini is not writable")

    def test_ordinary_native_frames_do_not_spam_logs_or_set_a_fault(self):
        process = SimpleNamespace(pid=1234, stdout=io.BytesIO(b"FPS 60\nBall radius: 12\n" * 100))
        self.adapter._process = process
        with self.assertNoLogs("companion.putting_adapter", level="ERROR"):
            self.adapter._read_output(process)
        self.assertEqual(self.adapter._fault, "")

    def test_inactive_putter_drops_shots(self):
        self.adapter._receive_shot(parse_putt(PAYLOAD))
        self.assertEqual(self.live, [])
        self.assertEqual(self.preview, [])

    def test_active_putter_forwards_and_duplicate_callback_is_ignored(self):
        with patch.object(self.adapter, "_apply_window_state"):
            self.adapter.set_active(True)
        now = time.monotonic()
        self.adapter._receive_shot(parse_putt(PAYLOAD, captured_at=now))
        self.adapter._receive_shot(parse_putt(PAYLOAD, captured_at=now + 0.05))
        self.assertEqual(len(self.live), 1)
        self.adapter._receive_shot(parse_putt(PAYLOAD, captured_at=now + 1.0))
        self.assertEqual(len(self.live), 2)

    def test_owned_process_must_be_running(self):
        self.adapter._calibration = True
        self.adapter._process = None
        self.adapter._receive_shot(parse_putt(PAYLOAD))
        self.assertEqual(self.preview, [])

    def test_command_uses_stock_flags_and_remapped_camera(self):
        command = self.adapter.build_command(resolve_camera(self.adapter.config, DEVICES))
        self.assertEqual(command[1:], ["-c", "orange2", "-w", "2", "-r", "640"])
        self.assertEqual(Path(command[0]).name, "ball_tracking.exe")

    def test_invalid_color_cannot_inject_command_flags(self):
        self.adapter.config["ball_color"] = "yellow -v somefile"
        with self.assertRaises(ValueError):
            self.adapter.build_command(DEVICES[0])

    def launch_with_camera_mode(self, config: str, device: dict, auto):
        self.adapter._process = None
        self.adapter._server = PuttingServer(lambda shot: None, port=0)
        self.adapter.config_path.write_text(config)
        launched = []

        def launch(command, work_dir):
            launched.append(self.adapter.config_path.read_text())
            return SimpleNamespace(pid=1234, stdout=None, poll=lambda: None)

        with patch("companion.putting_adapter.camera_devices", return_value=[device]), \
                patch("companion.putting_adapter.directshow_auto_exposure", return_value=auto) as reader, \
                patch("companion.putting_adapter.launch_stock_tracker", side_effect=launch), \
                patch("companion.putting_adapter.OwnedProcessJob.try_attach", return_value=None):
            self.assertTrue(self.adapter.start())
        self.adapter._process = None  # Never let stop() look up the fake PID.
        self.adapter.stop()
        return launched[0], reader

    def test_directshow_launch_keeps_camera_auto_exposure(self):
        stock = "[putting]\nstartx1=10\nstartx2=180\ny1=180\ny2=450\nradius=8\nmjpeg=1\nexposure = 0.0\nautoexposure = -1.0\nautofocus = 1.0\n"
        device = {"id": r"\\?\USB#ONE", "name": "HD USB Camera", "index": 2, "backend": cv2.CAP_DSHOW, "stable": True}
        self.adapter.config["camera_id"] = device["id"]
        launched, reader = self.launch_with_camera_mode(stock, device, True)
        self.assertEqual(launched, stock.replace("autoexposure = -1.0", "autoexposure = 1.0"))
        self.assertTrue(reader.call_args.args[0](r"\\?\usb#one"))

    def test_unknown_or_non_directshow_exposure_mode_leaves_stock_settings(self):
        stock = "[putting]\nstartx1=10\nstartx2=180\ny1=180\ny2=450\nradius=8\nmjpeg=0\nautoexposure = -1.0\n"
        for device, auto in (({"id": "usb:one", "name": "Cam", "index": 2, "backend": cv2.CAP_MSMF, "stable": True}, True),
                             ({"id": "usb:one", "name": "Cam", "index": 2, "backend": cv2.CAP_DSHOW, "stable": True}, None)):
            with self.subTest(backend=device["backend"]):
                self.assertEqual(self.launch_with_camera_mode(stock, device, auto)[0], stock)

    def test_import_keeps_existing_tuning_and_backs_up_previous_file(self):
        self.adapter._process = None
        content = "[putting]\nstartx1=10\nstartx2=180\ny1=180\ny2=450\nradius=13\nmjpeg=1\ncustomhsv={'hmin': 8,'hmax': 50,'smin': 20,'smax': 250,'vmin': 30,'vmax': 255}\n"
        original = self.adapter.config_path
        original.write_text(content.replace("radius=13", "radius=0"))
        source = Path(self.tmp.name) / "working-springbok.ini"
        source.write_text(content)
        self.adapter.import_native_config(source)
        self.assertEqual(original.read_text(), content)
        self.assertIn("radius=0", (Path(self.tmp.name) / "config.before-import.ini").read_text())


if __name__ == "__main__":
    unittest.main()
