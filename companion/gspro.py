"""Single-owner GSPro transport. No stale queue or automatic shot replay."""
from __future__ import annotations

from collections import deque
from dataclasses import replace
import codecs
import errno
import json
import logging
import math
import queue
import socket
import threading
import time

from companion.models import Shot

log = logging.getLogger(__name__)


def distance_to_target_yards(player: dict) -> float | None:
    """This installed GSPro/Open Connect version reports world distance in metres.

    It is independent of the outgoing shot payload's Units field. Zero is also
    Open Connect's uninitialised default, so it cannot select Chipping.
    """
    value = player.get("DistanceToTarget")
    if type(value) not in {int, float}:
        return None
    try:
        if not math.isfinite(value) or not 0 < value <= 10000:
            return None
    except OverflowError:
        return None
    return value / 0.9144


def shot_capture_start(shot: Shot) -> float:
    """Keep measurement-start fences distinct from completed-reading freshness."""
    started = shot.raw.get("capture_started_at", shot.captured_at)
    try:
        finite = math.isfinite(started)
    except (OverflowError, TypeError):
        finite = False
    if (isinstance(started, bool) or not isinstance(started, (int, float))
            or not finite or started > shot.captured_at):
        raise ValueError("Shot capture timing is invalid")
    return started


class JsonStream:
    """Decode fragmented or concatenated JSON objects without assuming recv boundaries."""
    def __init__(self):
        self.text = ""
        self.decoder = json.JSONDecoder()
        self.utf8 = codecs.getincrementaldecoder("utf-8")()

    def feed(self, chunk: bytes) -> list[dict]:
        self.text += self.utf8.decode(chunk)
        if len(self.text) > 1_000_000:
            raise ValueError("GSPro response exceeded the message limit")
        messages = []
        while self.text.strip(" \r\n\t\x00"):
            self.text = self.text.lstrip(" \r\n\t\x00")
            try:
                value, end = self.decoder.raw_decode(self.text)
            except json.JSONDecodeError:
                break
            self.text = self.text[end:]
            if not isinstance(value, dict):
                raise ValueError("GSPro response must be an object")
            messages.append(value)
        return messages


class SourceRouter:
    """Reject delayed/duplicate/wrong-source shots before writing to the simulator."""
    def __init__(self, max_age: float = 3.0):
        self.club: str | None = None
        self.handedness = "RH"
        self.epoch = time.monotonic()
        self.max_age = max_age
        self.seen = deque(maxlen=1000)
        self._lock = threading.Lock()

    def reset(self):
        with self._lock:
            self.club = None
            self.epoch = time.monotonic()

    def invalidate(self):
        """Start a new capture epoch while retaining known GSPro player state."""
        with self._lock:
            # Python 3.12 on Windows can use GetTickCount64 (15.6 ms). A
            # practice reading and Resume can otherwise share one timestamp.
            self.epoch = time.monotonic() + time.get_clock_info("monotonic").resolution

    def player(self, club: str, handedness: str = "RH") -> bool:
        if not club or len(club) > 20:
            return False
        with self._lock:
            changed = self.club != club.upper() or self.handedness != handedness
            if changed:
                self.epoch = time.monotonic()
            self.club = club.upper()
            self.handedness = handedness
            return changed

    @property
    def source(self) -> str | None:
        return None if self.club is None else ("webcam" if self.club == "PT" else "mevo")

    def accept(self, shot: Shot, now: float | None = None) -> tuple[bool, str]:
        now = time.monotonic() if now is None else now
        try:
            shot.validate()
            capture_start = shot_capture_start(shot)
        except ValueError as exc:
            return False, str(exc)
        with self._lock:
            if shot.event_id in self.seen:
                return False, "Duplicate reading ignored"
            self.seen.append(shot.event_id)
            if self.club is None:
                return False, "Select your current club in GSPro before taking a shot"
            expected = "webcam" if self.club == "PT" else "mevo"
            if shot.source != expected:
                return False, "Inactive shot source ignored"
            if capture_start < self.epoch or not 0 <= now - shot.captured_at <= self.max_age:
                return False, "An old reading was discarded"
            return True, ""


class GSProClient:
    def __init__(self, host="127.0.0.1", port=921, on_status=None, on_player=None,
                 on_delivery=None, ack_timeout=4.0, retry_seconds=1.0, on_context=None):
        self.host, self.port = host, int(port)
        self.on_status = on_status or (lambda *_: None)
        self.on_player = on_player or (lambda *_: None)
        self.on_context = on_context or (lambda *_: None)
        self.on_delivery = on_delivery or (lambda *_: None)
        self.ack_timeout, self.retry_seconds = ack_timeout, retry_seconds
        self.router = SourceRouter()
        self.connected = False
        self._stop = threading.Event()
        self._thread = None
        self._outbox = queue.Queue(maxsize=1)
        self._socket = None
        self._number = 0
        self._in_flight = None
        # Pausing and writing share this lock: after pause returns no queued
        # reading can cross the socket. Already-written shots remain in flight.
        self._lock = threading.RLock()
        self._delivery_enabled = True
        self._monitor_ready = False
        self._not_ready_sent = threading.Event()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="GSPro connection", daemon=True)
        self._thread.start()

    def stop(self):
        self.set_delivery_enabled(False)
        self.set_monitor_ready(False)
        if (self.connected and self._thread and self._thread.is_alive()
                and threading.current_thread() != self._thread and self._in_flight is None):
            # Let the socket owner publish Not Ready before disconnecting.
            # An in-flight shot keeps exclusive ownership of its ACK instead.
            self._not_ready_sent.wait(0.3)
        self._stop.set()
        sock = self._socket
        if sock:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if self._thread and threading.current_thread() != self._thread:
            self._thread.join(timeout=2)
        self.router.reset()

    def set_monitor_ready(self, ready: bool):
        """Publish actual active-source readiness, without manufacturing a shot."""
        with self._lock:
            self._monitor_ready = bool(ready)

    def _send_monitor_state(self, sock, ready):
        # IsHeartBeat=true is explicitly ignored for readiness by Open Connect.
        # Status contains neither data object and never increments ShotNumber.
        # Its Code 200 ACK is reserved for payloads containing ball/club data.
        payload = {"DeviceID": "MevoCompanion", "Units": "Yards",
                   "ShotNumber": self._number, "APIversion": "1",
                   "ShotDataOptions": {"ContainsBallData": False, "ContainsClubData": False,
                                       "LaunchMonitorIsReady": bool(ready),
                                       "LaunchMonitorBallDetected": False, "IsHeartBeat": False}}
        sock.sendall(json.dumps(payload, allow_nan=False).encode("utf-8"))
        if ready:
            self._not_ready_sent.clear()
        else:
            self._not_ready_sent.set()

    def set_delivery_enabled(self, enabled: bool):
        """Fence practice/live transitions and discard all unsent observations.

        Keep an in-flight shot until its ACK or disconnect; a pause cannot undo
        bytes already written, and their eventual delivery must remain visible.
        """
        discarded = []
        with self._lock:
            self._delivery_enabled = bool(enabled)
            self.router.invalidate()
            while True:
                try:
                    discarded.append(self._outbox.get_nowait())
                except queue.Empty:
                    break
        for shot in discarded:
            self.on_delivery(shot, "not_sent", "Delivery mode changed; queued reading discarded")

    def submit(self, shot: Shot) -> bool:
        with self._lock:
            if not self.connected:
                self.on_delivery(shot, "not_sent", "GSPro is disconnected; this shot was not queued")
                return False
            if not self._delivery_enabled or self._stop.is_set():
                self.on_delivery(shot, "not_sent", "Shot delivery is paused; this reading was not queued")
                return False
            allowed, reason = self.router.accept(shot)
            if not allowed:
                self.on_delivery(shot, "not_sent", reason)
                return False
            if self._in_flight is not None or not self._outbox.empty():
                self.on_delivery(shot, "not_sent", "Wait for the previous shot's confirmation")
                return False
            try:
                self._outbox.put_nowait(shot)
            except queue.Full:
                self.on_delivery(shot, "not_sent", "Another shot is already being delivered")
                return False
        return True

    def _run(self):
        self.on_status("connecting", f"Waiting for GSPro Open Connect at {self.host}:{self.port}")
        while not self._stop.is_set():
            try:
                sock = socket.create_connection((self.host, self.port), timeout=1)
                sock.settimeout(0.1)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                self._socket = sock
                self._not_ready_sent.clear()
                self.router.reset()
                self.connected = True
                self.on_status("connected", "Connected to Open Connect. Select your current club once in GSPro to enable shots.")
                self._session(sock)
            except (OSError, ValueError) as exc:
                if not self._stop.is_set():
                    if self.connected:
                        message = "GSPro connection interrupted; reconnecting automatically"
                    elif isinstance(exc, ConnectionRefusedError) or getattr(exc, "winerror", None) == 10061 or getattr(exc, "errno", None) == errno.ECONNREFUSED:
                        message = (f"GSPro Open Connect is not accepting connections at {self.host}:{self.port}. "
                                   "Open its connection window in GSPro; retrying automatically.")
                    else:
                        message = (f"Cannot reach GSPro Open Connect at {self.host}:{self.port}. "
                                   "Check the address in Connections and that Open Connect is running; retrying automatically.")
                    self.on_status("reconnecting", message)
                    log.debug("GSPro transport: %s", exc)
            finally:
                with self._lock:
                    self.connected = False
                    self.router.reset()
                if self._socket:
                    self._socket.close()
                self._socket = None
                with self._lock:
                    pending = self._in_flight
                    self._in_flight = None
                if pending:
                    self.on_delivery(pending, "unconfirmed", "Delivery was not confirmed. Check GSPro before retaking the shot; it will not be replayed.")
                with self._lock:
                    while not self._outbox.empty():
                        try:
                            shot = self._outbox.get_nowait()
                            self.on_delivery(shot, "not_sent", "Connection changed; queued reading discarded")
                        except queue.Empty:
                            break
            self._stop.wait(self.retry_seconds)
        self.on_status("stopped", "GSPro connection stopped")

    def _session(self, sock):
        stream = JsonStream()
        deadline = 0.0
        last_ready = None
        # The one socket owner serializes readiness and shot messages. No status
        # is sent while a shot awaits acknowledgement, and no shot is replayed.
        while not self._stop.is_set():
            # Consume all currently buffered player changes before choosing a
            # source. recv boundaries are unrelated to JSON or club transitions.
            drained = False
            try:
                for _ in range(32):
                    chunk = sock.recv(65536)
                    if not chunk:
                        raise ConnectionError("GSPro closed the connection")
                    for message in stream.feed(chunk):
                        code = message.get("Code")
                        if code == 201:
                            player = message.get("Player") or {}
                            if not isinstance(player, dict):
                                raise ValueError("Invalid GSPro player response")
                            club = str(player.get("Club") or "").strip().upper()
                            if club == "P":
                                club = "PT"
                            handed = str(player.get("Handed") or self.router.handedness).upper()
                            if club:
                                with self._lock:
                                    self.router.player(club, handed)
                                self.on_player(club, handed)
                                # Distance-only updates still arrive as Code 201,
                                # including when the selected club is unchanged.
                                self.on_context(club, handed, distance_to_target_yards(player))
                                self.on_status("connected", "Connected to GSPro Open Connect")
                        elif code == 203:
                            self.router.reset()
                            self.on_context("", self.router.handedness, None)
                        elif code == 200 or (type(code) is int and code >= 500):
                            with self._lock:
                                pending = self._in_flight
                                self._in_flight = None
                            if pending:
                                accepted = code == 200
                                self.on_delivery(pending, "accepted" if accepted else "rejected",
                                                 "GSPro accepted the shot" if accepted else str(message.get("Message", "GSPro rejected the shot")))
            except socket.timeout:
                drained = True
            if self._in_flight is not None and time.monotonic() >= deadline:
                # Reset the socket so a late ACK can never confirm the next shot.
                raise ConnectionError("GSPro acknowledgement timed out")
            if not drained or stream.text.strip(" \r\n\t\x00") or self._stop.is_set():
                continue
            with self._lock:
                if self._in_flight is not None:
                    continue
                ready = bool(self._monitor_ready and self._delivery_enabled and self.router.club)
                # Every connection starts explicitly Not Ready, including a
                # reconnect that occurs while the controller is still updating.
                if last_ready is None or ready != last_ready:
                    last_ready = False if last_ready is None else ready
                    self._send_monitor_state(sock, last_ready)
                    # Separate writes with a receive pass: current Open Connect
                    # implementations expect one JSON document per read.
                    continue
            with self._lock:
                if not self._delivery_enabled:
                    continue
                try:
                    shot = self._outbox.get_nowait()
                except queue.Empty:
                    shot = None
                if shot:
                    try:
                        capture_start = shot_capture_start(shot)
                    except ValueError as exc:
                        self.on_delivery(shot, "not_sent", str(exc))
                        continue
                    if shot.source != self.router.source or capture_start < self.router.epoch:
                        self.on_delivery(shot, "not_sent", "Club changed before delivery; reading discarded")
                    elif time.monotonic() - shot.captured_at > self.router.max_age:
                        self.on_delivery(shot, "not_sent", "Reading expired before delivery")
                    else:
                        self._number += 1
                        outgoing = shot
                        if self.router.handedness == "LH" and shot.raw.get("gspro_mirror_for_left_handed"):
                            outgoing = replace(shot, hla=-shot.hla, spin_axis=-shot.spin_axis)
                        message = outgoing.gspro_payload(self._number)
                        outgoing_ready = bool(self._monitor_ready and self._delivery_enabled and self.router.club)
                        # A completed measurement may arrive while Mevo+ is
                        # still rearming. Its ball data must not override the
                        # controller's current Not Ready signal.
                        message["ShotDataOptions"]["LaunchMonitorIsReady"] = outgoing_ready
                        payload = json.dumps(message, allow_nan=False).encode("utf-8")
                        self._in_flight = shot
                        sock.sendall(payload)
                        last_ready = outgoing_ready
                        if outgoing_ready:
                            self._not_ready_sent.clear()
                        else:
                            self._not_ready_sent.set()
                        deadline = time.monotonic() + self.ack_timeout
                        self.on_delivery(shot, "submitted", "Sent to GSPro; waiting for acknowledgement")
