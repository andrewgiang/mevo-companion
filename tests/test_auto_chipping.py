import io
import json
import socket
import threading
import time
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication

from companion.accessibility_adapter import AccessibilityAdapter
from companion.config import ConfigStore
from companion.controller import SessionController, automatic_shot_mode
from companion.gspro import GSProClient, JsonStream, distance_to_target_yards
from companion.models import Shot
from companion.engine import validate_settings


@pytest.mark.parametrize("metres,mode", [(4.6, "chipping"), (18.2, "chipping"),
                                       (18.3, "chipping"), (18.4, "full_swing"), (100, "full_swing")])
def test_installed_protocol_metres_and_rounded_twenty_yard_boundary(metres, mode):
    yards = distance_to_target_yards({"DistanceToTarget": metres})
    assert yards == pytest.approx(metres / .9144)
    assert automatic_shot_mode(yards, "I7") == mode
    assert automatic_shot_mode(yards, "PT") is None
    assert automatic_shot_mode(yards, "I7", enabled=False) is None


@pytest.mark.parametrize("value", [None, 0, -1, True, "18.3", float("nan"), float("inf"), 10 ** 500])
def test_missing_or_invalid_distance_never_means_zero_yard_chip(value):
    assert distance_to_target_yards({"DistanceToTarget": value}) is None
    assert automatic_shot_mode(None, "I7") is None
    assert distance_to_target_yards({}) is None


def test_distance_only_player_updates_and_round_end_reach_the_controller():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    contexts, finished = [], threading.Event()

    def server():
        conn, _ = listener.accept()
        with conn:
            events = [
                {"Code": 201, "Player": {"Club": "I7", "DistanceToTarget": 100}},
                {"Code": 201, "Player": {"Club": "I7", "DistanceToTarget": 18.3}},
                {"Code": 201, "Player": {"Club": "P", "DistanceToTarget": 3}},
                {"Code": 203},
            ]
            conn.sendall("".join(json.dumps(event) for event in events).encode())
            finished.wait(3)
        listener.close()

    worker = threading.Thread(target=server, daemon=True)
    worker.start()
    client = GSProClient(port=listener.getsockname()[1], on_context=lambda *args: contexts.append(args))
    try:
        client.start()
        end = time.monotonic() + 2
        while len(contexts) < 4 and time.monotonic() < end:
            time.sleep(.01)
        assert [item[0] for item in contexts] == ["I7", "I7", "PT", ""]
        assert contexts[1][2] == pytest.approx(18.3 / .9144)
        assert contexts[-1][2] is None
        assert client.router.club is None
    finally:
        finished.set()
        client.stop()
        worker.join(2)


@pytest.fixture
def session(tmp_path):
    app = QCoreApplication.instance() or QCoreApplication([])
    controller = SessionController(ConfigStore(tmp_path))
    controller.timer.stop()
    requests = []
    controller.source = SimpleNamespace(set_target_mode=requests.append, set_active=lambda _: None)
    controller.live_enabled = True
    controller.session_requested = True
    controller.health["gspro"]["state"] = "connected"
    controller.health["mevo"]["state"] = "ready"
    yield controller, requests
    controller._target_timer.stop()


def test_pair_of_club_distance_events_is_coalesced_before_mode_change(session):
    controller, requests = session
    controller._on_player_context("I7", "RH", 100)
    controller._on_player_context("I7", "RH", 10)
    assert all(item is None for item in requests)
    controller.apply_auto_chipping()
    assert requests[-1] == "chipping"
    assert not controller.snapshot()["ready"]
    controller._relay_mode(controller._generation, "chipping")
    assert controller.snapshot()["ready"]
    controller._on_player_context("I7", "RH", 80)
    controller.apply_auto_chipping()
    assert requests[-1] == "full_swing"


def test_putter_pause_manual_missing_distance_and_disconnect_cancel_requests(session):
    controller, requests = session
    for club, distance in [("PT", 3), ("I7", None), ("", 10)]:
        controller._on_player_context(club, "RH", distance)
        controller.apply_auto_chipping()
        assert requests[-1] is None
    controller._on_player_context("I7", "RH", 5)
    controller.store.data["auto_chipping"] = False
    controller.apply_auto_chipping()
    assert requests[-1] is None
    controller.store.data["auto_chipping"] = True
    controller.apply_auto_chipping()
    assert requests[-1] == "chipping"
    controller.pause()
    assert requests[-1] is None
    controller._on_gspro_status("reconnecting", "Reconnecting")
    assert controller.distance_to_target_yards is None
    controller._relay_player_context(controller._generation - 1, "I7", "RH", 5)
    assert controller.distance_to_target_yards is None


def test_sending_a_shot_invalidates_distance_until_next_lie(session):
    controller, requests = session
    controller._on_player_context("I7", "RH", 10)
    controller.apply_auto_chipping()
    controller._on_delivery(Shot("mevo", 35, 0), "submitted", "Sent")
    assert controller.distance_to_target_yards is None
    assert requests[-1] is None


def test_identical_context_does_not_cancel_pending_mode_request(session):
    controller, requests = session
    controller._on_player_context("I7", "RH", 10)
    controller.apply_auto_chipping()
    before = list(requests)
    controller._on_player_context("I7", "RH", 10)
    assert requests == before
    assert not controller._target_timer.isActive()
    assert controller._auto_mode == "chipping"


def test_new_wrong_mode_shot_is_held_but_existing_tracking_can_complete(session):
    controller, requests = session
    submitted = []
    controller.gspro = SimpleNamespace(submit=submitted.append, set_monitor_ready=lambda _: None)
    controller._on_player_context("I7", "RH", 10)
    controller.apply_auto_chipping()
    now = time.monotonic()
    raw = {"capture_context": "play_mode", "shot_mode": "full_swing"}
    controller._on_shot(Shot("mevo", 90, 0, captured_at=now + .1, raw=raw))
    assert not submitted
    older = controller._auto_context_at - .01
    controller._capture_epoch = older - .01
    existing = Shot("mevo", 90, 0, captured_at=now, raw={**raw, "capture_started_at": older})
    controller._on_shot(existing)
    assert submitted == [existing]


def test_distance_update_with_same_required_mode_keeps_capture_fence(session):
    controller, _ = session
    submitted = []
    controller.gspro = SimpleNamespace(submit=submitted.append, set_monitor_ready=lambda _: None)
    controller._on_player_context("I7", "RH", 10)
    controller.apply_auto_chipping()
    fence = controller._auto_context_at
    controller._on_player_context("I7", "RH", 12)
    controller.apply_auto_chipping()
    assert controller._auto_context_at == fence
    controller._on_shot(Shot("mevo", 90, 0, captured_at=fence + .1,
                            raw={"capture_context": "play_mode", "shot_mode": "full_swing"}))
    assert submitted == []


def test_chipping_preference_does_not_invalidate_calibration(tmp_path):
    current = ConfigStore(tmp_path).data
    current["validation"]["swing"] = True
    updated = validate_settings(current, {"auto_chipping": False})
    assert updated["auto_chipping"] is False
    assert updated["validation"]["swing"] is True
    with pytest.raises(ValueError):
        validate_settings(current, {"auto_chipping": "false"})


def test_native_mode_command_waits_for_idle_and_then_observed_confirmation():
    states, modes = [], []
    adapter = AccessibilityAdapter({}, lambda _: None, lambda *args: states.append(args), on_mode=modes.append)
    pipe = io.BytesIO()
    adapter._process = SimpleNamespace(stdin=pipe, poll=lambda: None)
    snapshot = {"live_context": True, "capture_context": "play_mode", "shot_mode": "full_swing"}
    adapter.set_target_mode("chipping")
    adapter.gate.ready = False  # Tracking / rearming must not be interrupted.
    assert not adapter._apply_target_mode(snapshot)
    assert pipe.getvalue() == b""
    adapter.gate.ready = True
    assert adapter._apply_target_mode(snapshot)
    command = json.loads(pipe.getvalue())
    assert command["type"] == "set_shot_mode" and command["mode"] == "chipping"
    assert not adapter.gate.ready
    assert adapter._apply_target_mode(snapshot)  # No old-mode acquisition during switch.
    snapshot["shot_mode"] = "chipping"
    assert not adapter._apply_target_mode(snapshot)
    assert modes == ["full_swing", "chipping"]
    adapter.gate.ready = True
    adapter._apply_target_mode(snapshot)
    assert len(pipe.getvalue().splitlines()) == 1


def test_completed_shot_is_not_interrupted_by_new_target():
    adapter = AccessibilityAdapter({}, lambda _: None, lambda *_: None)
    adapter.set_target_mode("chipping")
    adapter.gate.ready = True
    assert not adapter._apply_target_mode({"shot_mode": "full_swing"}, Shot("mevo", 90, 0))
