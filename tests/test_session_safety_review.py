"""Regression cases found by independent session ownership review."""
from collections import deque
import json
import socket
import threading
import time
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication

from companion.config import ConfigStore
from companion.controller import SessionController
from companion.gspro import GSProClient
from companion.models import Shot


@pytest.fixture(scope="module")
def qt_application():
    application = QCoreApplication.instance() or QCoreApplication([])
    yield application


@pytest.fixture
def session(tmp_path, qt_application):
    controller = SessionController(ConfigStore(tmp_path))
    controller.timer.stop()
    client = GSProClient()
    client.connected = True
    client.router.player("7I", "RH")
    controller.gspro = client
    controller.club = "7I"
    controller.session_requested = True
    controller.live_enabled = True
    yield controller, client
    client._stop.set()
    controller.timer.stop()


class BufferedGSPro:
    """Deterministic transport: these inbound bytes exist before session runs."""
    def __init__(self, client, messages=()):
        self.client = client
        self.inbound = deque(json.dumps(item).encode() for item in messages)
        self.sent = []
        self.timeouts = 0

    def sendall(self, payload):
        self.sent.append(json.loads(payload))

    def recv(self, size):
        if self.inbound:
            return self.inbound.popleft()
        self.timeouts += 1
        if self.timeouts > 1:
            self.client._stop.set()
        raise socket.timeout()


def test_pause_prevents_an_accepted_but_not_yet_written_shot(session):
    controller, client = session
    controller._on_shot(Shot("mevo", 120, 0))
    assert not client._outbox.empty()
    controller.pause()
    transport = BufferedGSPro(client)
    client._session(transport)
    assert not any(message["ShotDataOptions"]["ContainsBallData"] for message in transport.sent), "A queued shot escaped while the UI said delivery was paused"


def test_resume_does_not_forward_a_qt_delayed_practice_reading(session):
    controller, client = session
    controller.pause()
    captured_while_paused = Shot("mevo", 100, 1)
    # Queued Qt signals may arrive after a user clicks Resume. Capture time, not
    # the current live_enabled boolean alone, must determine the delivery epoch.
    time.sleep(.001)
    controller.resume()
    controller._on_shot(captured_while_paused)
    assert client._outbox.empty(), "A practice reading entered the resumed live session"


def test_pending_club_change_is_read_before_sending_old_source(session):
    controller, client = session
    controller._on_shot(Shot("mevo", 120, 0))
    transport = BufferedGSPro(client, [{"Code": 201, "Player": {"Club": "PT", "Handed": "RH"}}])
    client._session(transport)
    assert not any(message["ShotDataOptions"]["ContainsBallData"] for message in transport.sent), "A Mevo shot was sent before processing an already-pending putter selection"


def test_chip_validation_uses_mode_at_submission_not_mode_at_ack(session):
    controller, client = session
    controller.setup_mode = True
    controller.chipping = False
    full_swing = Shot("mevo", 120, 0)
    controller._on_shot(full_swing)
    controller._on_delivery(full_swing, "submitted", "Sent to GSPro")
    controller.chipping = True
    controller._on_delivery(full_swing, "accepted", "GSPro accepted the shot")
    assert controller.store.data["validation"]["swing"] is True
    assert controller.store.data["validation"]["chip"] is False


def test_old_queued_worker_signals_cannot_enter_replacement_session(session, qt_application):
    controller, old_client = session
    previous_generation = controller._generation
    old_shot = Shot("mevo", 120, 0)

    def old_worker():
        controller._source_status.emit(previous_generation, "mevo", "ready", "old device ready")
        controller._source_shot.emit(previous_generation, old_shot)
        controller._gspro_status.emit(previous_generation, "connected", "old GSPro connected")
        controller._player.emit(previous_generation, "PT", "RH")

    worker = threading.Thread(target=old_worker)
    worker.start()
    worker.join()
    # All four callbacks are queued on Qt and have not run yet.
    controller.stop()
    new_client = GSProClient()
    new_client.connected = True
    new_client.router.player("7I")
    controller.gspro = new_client
    controller.session_requested = controller.live_enabled = True
    controller.club = "7I"
    qt_application.processEvents()
    assert controller.club == "7I"
    assert controller.health["mevo"]["state"] == "idle"
    assert controller.health["gspro"]["state"] == "idle"
    assert new_client._outbox.empty()
    new_client._stop.set()


def test_native_calibration_putt_stays_practice_after_live_was_enabled(session):
    controller, client = session
    controller.putting = SimpleNamespace(config={})
    controller.club = "PT"
    client.router.player("PT")
    shot = Shot("webcam", 4, .5, raw={"tracker": "springbok-stock"})
    observed = []
    controller.shot_observed.connect(observed.append)
    controller._relay_practice_shot(controller._generation, shot)
    assert client._outbox.empty()
    assert controller.recent_shots[0]["state"] == "practice"
    assert controller.store.data["putting"]["configured"] is True
    assert controller.putting.config["configured"] is True
    assert observed == [shot]


@pytest.mark.parametrize("state", ["connected", "working", "tracking", "checking"])
def test_connection_or_activity_is_not_shot_readiness(session, state):
    controller, _ = session
    controller.health["gspro"] = {"state": "connected", "message": "connected"}
    controller.health["mevo"] = {"state": state, "message": state}
    assert controller.snapshot()["ready"] is False
    controller.health["mevo"]["state"] = "ready"
    assert controller.snapshot()["ready"] is True


def test_resuming_native_calibration_switches_tracker_to_live_first(session):
    controller, client = session
    calls = []
    controller.putting = SimpleNamespace(
        start=lambda calibration=False: calls.append(("start", calibration)),
        set_active=lambda active: calls.append(("active", active)),
    )
    controller.pause()
    controller.resume()
    assert calls[0] == ("start", False)
    assert controller.live_enabled is True
    assert client._delivery_enabled is True


def test_pause_does_not_claim_to_cancel_a_shot_already_written(session):
    controller, client = session
    shot = Shot("mevo", 120, 0)
    client._in_flight = shot
    controller.pause()
    assert client._in_flight is shot
    assert client._delivery_enabled is False


def test_explicit_source_change_drops_queued_shot_but_keeps_player_state(session):
    controller, client = session
    controller._on_shot(Shot("mevo", 120, 0))
    controller.set_chipping(True)
    assert client._outbox.empty()
    assert client.router.club == "7I"
    assert controller.live_enabled is True
