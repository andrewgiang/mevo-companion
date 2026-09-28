import json
from collections import deque
import socket
import threading
import time

import pytest

from companion.gspro import GSProClient, JsonStream, SourceRouter
from companion.models import Shot


def eventually(condition, timeout=3):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return
        time.sleep(.01)
    assert condition()


def read_shot(conn, received):
    stream = JsonStream()
    while True:
        chunk = conn.recv(65536)
        if not chunk:
            raise ConnectionError("Client disconnected before sending a shot")
        for message in stream.feed(chunk):
            received.append(message)
            if message.get("ShotDataOptions", {}).get("ContainsBallData"):
                return message


def test_stream_handles_fragmented_concatenated_unicode_messages():
    stream = JsonStream()
    payload = '{"Message":"prêt","Code":200}{"Code":201,"Player":{"Club":"PT"}}'.encode()
    messages = []
    for byte in payload:
        messages.extend(stream.feed(bytes([byte])))
    assert [m["Code"] for m in messages] == [200, 201]


def test_router_requires_fresh_matching_source_and_deduplicates():
    router = SourceRouter()
    assert not router.accept(Shot("webcam", 4, 0))[0]  # An unknown club routes to Mevo+.
    old = Shot("mevo", 110, 2, captured_at=time.monotonic() - 1)
    router.player("7I")
    assert not router.accept(old)[0]
    assert not router.accept(Shot("webcam", 4, 0))[0]
    shot = Shot("mevo", 110, 2)
    assert router.accept(shot)[0]
    assert not router.accept(shot)[0]
    router.player("PT")
    assert router.accept(Shot("webcam", 4, -1))[0]


def test_unknown_club_routes_to_mevo_until_gspro_reports_otherwise():
    router = SourceRouter()
    router.epoch -= 1  # Connected a second ago.
    swing = Shot("mevo", 110, 2, captured_at=time.monotonic() - .5)
    assert router.source == "mevo"
    router.player("I7")  # GSPro confirming a full-swing club keeps the source.
    assert router.accept(swing)[0]
    putt = Shot("webcam", 4, 0, captured_at=time.monotonic() - .5)
    router.player("PT")
    assert router.accept(putt) == (False, "An old reading was discarded")


def test_router_fences_measurement_start_but_ages_completed_reading():
    router = SourceRouter()
    now = time.monotonic()
    router.player("I7")
    router.epoch = now - 5
    chip = Shot("mevo", 15, 1, captured_at=now - 1,
                raw={"capture_started_at": now - 4})
    assert router.accept(chip, now=now)[0], "Measuring may take longer than the three-second freshness window"
    router.epoch = now - 2
    interrupted = Shot("mevo", 15, 1, captured_at=now - 1,
                       raw={"capture_started_at": now - 4})
    assert not router.accept(interrupted, now=now)[0], "A club change during measurement must discard the reading"


@pytest.mark.parametrize("started", [True, "123", float("nan"), float("inf"), 10 ** 500])
def test_router_rejects_invalid_measurement_start(started):
    router = SourceRouter()
    router.player("I7")
    assert not router.accept(Shot("mevo", 15, 1, raw={"capture_started_at": started}))[0]


def test_router_rejects_measurement_start_after_completion():
    router = SourceRouter()
    router.player("I7")
    shot = Shot("mevo", 15, 1)
    shot.raw["capture_started_at"] = shot.captured_at + 1
    assert not router.accept(shot)[0]


def test_client_routes_current_club_and_waits_for_ack():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    received, deliveries = [], []
    done = threading.Event()

    def simulator():
        conn, _ = server.accept()
        with conn:
            conn.sendall(b'{"Code":201,"Player":{"Club":"PT","Handed":"RH"}}')
            read_shot(conn, received)
            conn.sendall(b'{"Code":200,"Message":"OK"}')
            done.wait(2)
        server.close()

    worker = threading.Thread(target=simulator, daemon=True)
    worker.start()
    client = GSProClient(port=server.getsockname()[1], on_delivery=lambda s, state, msg: deliveries.append(state))
    try:
        client.start()
        eventually(lambda: client.router.source == "webcam")
        assert not client.submit(Shot("mevo", 120, 0))
        shot = Shot("webcam", 4.2, -1.3)
        assert client.submit(shot)
        eventually(lambda: "accepted" in deliveries)
        shots = [message for message in received if "BallData" in message]
        assert shots[0]["BallData"]["HLA"] == -1.3
        assert len(shots) == 1
        assert received[0]["ShotDataOptions"]["LaunchMonitorIsReady"] is False
        assert not client.submit(shot)
    finally:
        done.set()
        client.stop()
        worker.join(2)


def test_ack_timeout_never_replays_shot_after_reconnect():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    received, deliveries = [], []
    done = threading.Event()

    def simulator():
        first, _ = server.accept()
        with first:
            first.sendall(b'{"Code":201,"Player":{"Club":"PT"}}')
            read_shot(first, received)
            time.sleep(.25)
        second, _ = server.accept()
        with second:
            second.settimeout(.25)
            second.sendall(b'{"Code":201,"Player":{"Club":"PT"}}')
            stream = JsonStream()
            try:
                while chunk := second.recv(65536):
                    received.extend(stream.feed(chunk))
            except socket.timeout:
                pass
        server.close()
        done.set()

    worker = threading.Thread(target=simulator, daemon=True)
    worker.start()
    client = GSProClient(port=server.getsockname()[1], ack_timeout=.15, retry_seconds=.03,
                         on_delivery=lambda s, state, msg: deliveries.append(state))
    try:
        client.start()
        eventually(lambda: client.router.source == "webcam")
        assert client.submit(Shot("webcam", 3, 0))
        assert done.wait(3)
        assert "unconfirmed" in deliveries
        assert len([message for message in received if "BallData" in message]) == 1
    finally:
        client.stop()
        worker.join(2)


def test_missing_open_connect_explains_endpoint_and_keeps_retrying(monkeypatch):
    statuses, attempts = [], []
    client = GSProClient(host="127.0.0.1", port=921, retry_seconds=0,
                         on_status=lambda state, message: statuses.append((state, message)))

    def refused(endpoint, timeout):
        attempts.append(endpoint)
        if len(attempts) == 3:
            client._stop.set()
        raise ConnectionRefusedError(10061, "No connection could be made")

    monkeypatch.setattr(socket, "create_connection", refused)
    client._run()
    assert attempts == [("127.0.0.1", 921)] * 3
    assert [state for state, _ in statuses] == ["connecting", "reconnecting", "reconnecting", "stopped"]
    assert "not accepting connections at 127.0.0.1:921" in statuses[1][1]
    assert "retrying automatically" in statuses[1][1]
    assert "interrupted" not in statuses[1][1]


def test_unreachable_open_connect_explains_address_check(monkeypatch):
    statuses = []
    client = GSProClient(host="192.0.2.1", retry_seconds=0)

    def timeout(endpoint, timeout):
        raise TimeoutError("Timed out")

    def status(state, message):
        statuses.append((state, message))
        if state == "reconnecting":
            client._stop.set()

    client.on_status = status
    monkeypatch.setattr(socket, "create_connection", timeout)
    client._run()
    assert "Cannot reach GSPro Open Connect at 192.0.2.1:921" in statuses[1][1]
    assert "Check the address in Settings" in statuses[1][1]


def test_monitor_ready_is_change_only_status_without_shot_data_or_number():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    received = []

    def simulator():
        conn, _ = server.accept()
        with conn:
            conn.sendall(b'{"Code":201,"Player":{"Club":"I7","Handed":"RH"}}')
            stream = JsonStream()
            while data := conn.recv(65536):
                received.extend(stream.feed(data))
        server.close()

    worker = threading.Thread(target=simulator, daemon=True)
    worker.start()
    client = GSProClient(port=server.getsockname()[1])
    try:
        client.start()
        eventually(lambda: len(received) == 1)
        client.set_monitor_ready(True)
        eventually(lambda: len(received) == 2)
        client.set_monitor_ready(True)
        time.sleep(.15)
        assert len(received) == 2
        client.set_delivery_enabled(False)
        eventually(lambda: len(received) == 3)
        assert [item["ShotDataOptions"]["LaunchMonitorIsReady"] for item in received] == [False, True, False]
        assert all(item["ShotNumber"] == 0 and "BallData" not in item and "ClubData" not in item for item in received)
        assert all(item["ShotDataOptions"]["IsHeartBeat"] is False for item in received)
        assert all(item["ShotDataOptions"]["ContainsBallData"] is False for item in received)
        assert all(item["ShotDataOptions"]["ContainsClubData"] is False for item in received)
    finally:
        client.stop()
        worker.join(2)


def test_readiness_change_waits_for_pending_shot_ack():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    received, deliveries = [], []
    shot_received, release_ack = threading.Event(), threading.Event()

    def simulator():
        conn, _ = server.accept()
        with conn:
            conn.settimeout(.05)
            conn.sendall(b'{"Code":201,"Player":{"Club":"I7","Handed":"RH"}}')
            stream = JsonStream()
            ack_sent = False
            while True:
                if shot_received.is_set() and release_ack.is_set() and not ack_sent:
                    conn.sendall(b'{"Code":200,"Message":"Ball Data received"}')
                    ack_sent = True
                try:
                    data = conn.recv(65536)
                except socket.timeout:
                    continue
                if not data:
                    break
                for item in stream.feed(data):
                    received.append(item)
                    if "BallData" in item:
                        shot_received.set()
        server.close()

    worker = threading.Thread(target=simulator, daemon=True)
    worker.start()
    client = GSProClient(port=server.getsockname()[1],
                         on_delivery=lambda shot, state, message: deliveries.append(state))
    try:
        client.start()
        eventually(lambda: client.router.club == "I7")
        client.set_monitor_ready(True)
        eventually(lambda: any(item["ShotDataOptions"]["LaunchMonitorIsReady"] for item in received))
        assert client.submit(Shot("mevo", 110, 1))
        assert shot_received.wait(2)
        client.set_monitor_ready(False)
        time.sleep(.2)
        assert len(received) == 3  # initial Not Ready, Ready, then the shot
        assert "accepted" not in deliveries
        release_ack.set()
        eventually(lambda: len(received) == 4)
        assert received[-1]["ShotDataOptions"]["LaunchMonitorIsReady"] is False
        assert received[-1]["ShotNumber"] == 1
        assert deliveries == ["submitted", "accepted"]
    finally:
        release_ack.set()
        client.stop()
        worker.join(2)


def test_completed_shot_does_not_claim_ready_while_monitor_is_rearming():
    client = GSProClient()
    client.connected = True
    client.router.player("I7")
    client.set_monitor_ready(False)

    class Peer:
        def __init__(self):
            self.reads, self.sent, self.inbound = 0, [], deque()

        def recv(self, size):
            if self.inbound:
                return self.inbound.popleft()
            self.reads += 1
            if self.reads == 2:
                assert client.submit(Shot("mevo", 15, 1))
            if self.reads >= 5:
                client._stop.set()
            raise socket.timeout()

        def sendall(self, payload):
            item = json.loads(payload)
            self.sent.append(item)
            if item["ShotDataOptions"]["ContainsBallData"]:
                self.inbound.append(b'{"Code":200,"Message":"Ball Data received"}')

    peer = Peer()
    client._session(peer)
    assert [item["ShotDataOptions"]["ContainsBallData"] for item in peer.sent] == [False, True]
    assert all(item["ShotDataOptions"]["LaunchMonitorIsReady"] is False for item in peer.sent)
    assert client._not_ready_sent.is_set()


def test_connected_without_player_event_is_ready_for_mevo():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    received, statuses = [], []

    def simulator():
        conn, _ = server.accept()
        with conn:
            conn.settimeout(.05)
            stream = JsonStream()
            while True:
                try:
                    data = conn.recv(65536)
                except socket.timeout:
                    continue
                if not data:
                    break
                received.extend(stream.feed(data))
        server.close()

    worker = threading.Thread(target=simulator, daemon=True)
    worker.start()
    client = GSProClient(port=server.getsockname()[1],
                         on_status=lambda state, message: statuses.append((state, message)))
    try:
        client.set_monitor_ready(True)
        client.start()
        eventually(lambda: any(item["ShotDataOptions"]["LaunchMonitorIsReady"] for item in received))
        assert received[0]["ShotDataOptions"]["LaunchMonitorIsReady"] is False  # Every connection starts Not Ready.
        assert client.router.club is None
        assert ("connected", "Connected to GSPro Open Connect") in statuses
        assert not client.submit(Shot("webcam", 4, 0))
        assert client.submit(Shot("mevo", 20, 1))
        eventually(lambda: any("BallData" in item for item in received))
    finally:
        client.stop()
        worker.join(2)
