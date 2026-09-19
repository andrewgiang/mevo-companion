"""Putting integration regressions across the controller/adapter boundary."""
import json
from types import SimpleNamespace
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication

from companion.config import ConfigStore
from companion.controller import SessionController


def make_controller(tmp_path):
    application = QCoreApplication.instance() or QCoreApplication([])
    controller = SessionController(ConfigStore(tmp_path))
    controller.timer.stop()
    return application, controller


def test_reconnecting_does_not_treat_owned_tracker_as_another_connector(tmp_path):
    application, controller = make_controller(tmp_path)
    controller.putting = SimpleNamespace(
        owned_pids={100, 101}, config={"camera_id": "usb:putting"},
        start=lambda calibration=False: True, set_active=lambda active: None,
    )
    processes = [{"pid": 100, "name": "ball_tracking.exe"},
                 {"pid": 101, "name": "ball_tracking.exe"},
                 {"pid": 102, "name": "GSPro.exe"}]
    with patch("companion.controller.platform.processes", return_value=processes), \
         patch.object(controller, "_create_gspro"), patch.object(controller, "start_source"):
        controller.connect_session(live=True)
    assert controller.session_requested
    assert controller.live_enabled


def test_separate_tracker_remains_a_real_conflict(tmp_path):
    application, controller = make_controller(tmp_path)
    controller.putting = SimpleNamespace(owned_pids={100}, config={})
    processes = [{"pid": 100, "name": "ball_tracking.exe"},
                 {"pid": 200, "name": "ball_tracking.exe"}]
    with patch("companion.controller.platform.processes", return_value=processes), \
         patch.object(controller, "_create_gspro") as start_gspro:
        controller.connect_session(live=True)
    start_gspro.assert_not_called()
    assert not controller.live_enabled
    assert controller.health["gspro"]["state"] == "action_needed"


def test_successful_legacy_camera_resolution_is_saved_for_next_launch(tmp_path):
    application, controller = make_controller(tmp_path)
    controller.putting = SimpleNamespace(
        config={"camera_id": "usb:stable-device"},
        start=lambda calibration=False: True, set_active=lambda active: None,
    )
    controller.start_putting(calibration=True)
    stored = json.loads(controller.store.path.read_text())
    assert stored["putting"]["camera_id"] == "usb:stable-device"


def test_failed_camera_start_does_not_persist_a_guessed_identity(tmp_path):
    application, controller = make_controller(tmp_path)
    controller.putting = SimpleNamespace(
        config={"camera_id": "usb:unavailable"},
        start=lambda calibration=False: False, set_active=lambda active: None,
    )
    controller.start_putting(calibration=True)
    assert not controller.store.data["putting"].get("camera_id")
