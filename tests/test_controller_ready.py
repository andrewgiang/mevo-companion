from types import SimpleNamespace
import time

import pytest
from PySide6.QtCore import QCoreApplication

from companion.config import ConfigStore
from companion.controller import SessionController
from companion.models import Shot


@pytest.fixture
def controller(tmp_path):
    app = QCoreApplication.instance() or QCoreApplication([])
    value = SessionController(ConfigStore(tmp_path))
    value.timer.stop()
    ready, submitted = [], []
    value.gspro = SimpleNamespace(set_monitor_ready=ready.append, set_delivery_enabled=lambda _: None,
                                  submit=submitted.append)
    value.health["gspro"]["state"] = "connected"
    value.health["mevo"]["state"] = "ready"
    value.club, value.live_enabled, value.session_requested = "I7", True, True
    yield value, ready, submitted
    value.timer.stop()


def test_ready_status_follows_live_delivery_and_selected_source(controller):
    value, ready, _ = controller
    value.publish()
    assert ready[-1] is True
    value.pause()
    assert ready[-1] is False
    value.resume()
    assert ready[-1] is True
    value.club = "PT"
    value.publish()
    assert ready[-1] is False
    value.health["putting"]["state"] = "ready"
    value.publish()
    assert ready[-1] is True
    value.health["gspro"]["state"] = "reconnecting"
    value.publish()
    assert ready[-1] is False


def test_unknown_club_does_not_claim_putter_is_selected(controller):
    value, _, _ = controller
    value.club = ""
    value._relay_health(value._generation, "mevo", "standby", "Putter selected")
    assert value.health["mevo"]["state"] == "checking"
    assert "current club" in value.health["mevo"]["message"]


def test_measurement_started_before_resume_is_not_forwarded(controller):
    value, _, submitted = controller
    started = time.monotonic()
    value.pause()
    value.resume()
    shot = Shot("mevo", 85, 1, captured_at=value._capture_epoch + .1,
                raw={"capture_started_at": started})
    value._on_shot(shot)
    assert submitted == []


@pytest.mark.parametrize("started", [True, "old", float("nan"), float("inf")])
def test_invalid_measurement_start_cannot_cross_delivery_fence(controller, started):
    value, _, submitted = controller
    value._on_shot(Shot("mevo", 85, 1, raw={"capture_started_at": started}))
    assert submitted == []


@pytest.mark.parametrize("mode,manual,expected", [("chipping", False, "chip"), ("full_swing", True, "swing")])
def test_play_mode_classifies_practice_shot_from_fs_golf(controller, mode, manual, expected):
    value, _, submitted = controller
    value.setup_mode, value.chipping = True, manual
    shot = Shot("mevo", 29.5, 1, raw={"capture_context": "play_mode", "shot_mode": mode})
    value._on_shot(shot)
    assert submitted == [shot]
    value._on_delivery(shot, "accepted", "Accepted by GSPro")
    assert value.store.data["validation"][expected] is True
