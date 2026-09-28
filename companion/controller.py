"""Session orchestration shared by setup, tray startup, and the playing screen."""
from __future__ import annotations

from collections import deque
from datetime import datetime
import json
import logging
import math
from pathlib import Path
import sys
import threading
import time
import zipfile

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from companion.config import ConfigStore, resource_path
from companion.gspro import GSProClient, shot_capture_start
from companion import platform_windows as platform

log = logging.getLogger(__name__)


def automatic_shot_mode(distance_yards, club, enabled=True):
    if not enabled or not club or club == "PT" or type(distance_yards) not in {int, float}:
        return None
    if not math.isfinite(distance_yards) or distance_yards <= 0:
        return None
    # GSPro quantises its metre distance to one decimal place. Exactly 20 yd
    # arrives as 18.3 m; use that same precision at the inclusive boundary.
    return "chipping" if distance_yards <= 18.3 / .9144 else "full_swing"


class SessionController(QObject):
    changed = Signal(dict)
    preview = Signal(object, object)
    delivery = Signal(object, str, str)
    shot_observed = Signal(object)
    event = Signal(str)
    discovered = Signal(object)
    # Every worker callback carries the owning session generation. QObject
    # disconnect alone cannot remove signals already queued on the GUI thread.
    _source_status = Signal(int, str, str, str)
    _source_shot = Signal(int, object)
    _source_preview_shot = Signal(int, object)
    _source_preview = Signal(int, object, object)
    _gspro_status = Signal(int, str, str)
    _player = Signal(int, str, str)
    _player_context = Signal(int, str, str, object)
    _source_mode = Signal(int, str)
    _delivery = Signal(int, object, str, str)

    def __init__(self, store: ConfigStore, demo=False):
        super().__init__()
        self.store, self.demo = store, demo
        self.health = {
            "gspro": {"state": "idle", "message": "Open GSPro to connect"},
            "mevo": {"state": "idle", "message": "FS Golf connects automatically"},
            "putting": {"state": "idle", "message": "Springbok webcam putting"},
        }
        self.events = deque(maxlen=200)
        self.recent_shots = deque(maxlen=30)
        self.club, self.handedness = "", "RH"
        self.session_requested = False
        self.setup_mode = False
        self.live_enabled = False
        self.chipping = False
        self.shot_mode = ""
        self.distance_to_target_yards = None
        self._auto_mode = None
        self._required_mode = None
        self._auto_context_at = time.monotonic()
        self.source = None
        self.putting = None
        self.gspro = None
        self._owned_golf = None
        self._last_golf_launch = 0.0
        self._last_process_check = 0.0
        self._session_saw_gspro = False
        self._stopping = False
        self._generation = 0
        self._capture_epoch = time.monotonic()
        self._submission_context = {}
        self._source_status.connect(self._relay_health)
        self._source_shot.connect(self._relay_shot)
        self._source_preview_shot.connect(self._relay_practice_shot)
        self._source_preview.connect(self._relay_preview)
        self._gspro_status.connect(self._relay_gspro_status)
        self._player.connect(self._relay_player)
        self._player_context.connect(self._relay_player_context)
        self._source_mode.connect(self._relay_mode)
        self._delivery.connect(self._relay_delivery)
        self.timer = QTimer(self)
        self.timer.setInterval(1500)
        self.timer.timeout.connect(self._tick)
        self.timer.start()
        self._target_timer = QTimer(self)
        self._target_timer.setSingleShot(True)
        self._target_timer.setInterval(650)
        self._target_timer.timeout.connect(self.apply_auto_chipping)
        self.discovered.connect(self._apply_discovery)

    def discover(self):
        if self.demo:
            self.discovered.emit(platform.InstalledApps("C:/GSPro/GSPro.exe", "C:/FlightScope/FS Golf.exe"))
            return
        def work():
            try:
                self.discovered.emit(platform.discover_apps())
            except Exception:
                log.exception("Application discovery failed")
        threading.Thread(target=work, name="Find installed apps", daemon=True).start()

    @Slot(object)
    def _apply_discovery(self, apps):
        if not self.demo:
            if not self.store.data["gspro_path"] and apps.gspro:
                self.store.data["gspro_path"] = apps.gspro
            if not self.store.data["fs_golf_path"] and apps.fs_golf:
                self.store.data["fs_golf_path"] = apps.fs_golf
            self.store.save()
        self.publish()

    def record(self, message):
        if self.events and self.events[-1][1] == message:
            return
        self.events.append((datetime.now().strftime("%H:%M:%S"), message))
        log.info(message)
        self.event.emit(message)

    def snapshot(self):
        # Until GSPro reports a club (it only does so on a change), assume a
        # full-swing club so connecting is enough to play.
        source = "putting" if self.club == "PT" else "mevo"
        active = self.health.get(source, {})
        ready = bool(self.live_enabled and self.health["gspro"]["state"] == "connected"
                     and source and active.get("state") == "ready"
                     and (not self._auto_mode or self.shot_mode == self._auto_mode)
                     and not (self._target_timer.isActive() and self.store.data.get("auto_chipping", True)
                              and self.source and hasattr(self.source, "set_target_mode")
                              and self.distance_to_target_yards is not None and self.club != "PT"))
        return {"health": {k: dict(v) for k, v in self.health.items()}, "club": self.club,
                "handedness": self.handedness, "ready": ready, "source": source,
                "live_enabled": self.live_enabled, "setup_mode": self.setup_mode,
                "requested": self.session_requested, "chipping": self.chipping,
                "shot_mode": self.shot_mode, "distance_to_target_yards": self.distance_to_target_yards,
                "desired_shot_mode": self._auto_mode,
                "demo": self.demo, "events": list(self.events), "shots": list(self.recent_shots)}

    def publish(self):
        state = self.snapshot()
        if self.gspro is not None and hasattr(self.gspro, "set_monitor_ready"):
            self.gspro.set_monitor_ready(state["ready"])
        self.changed.emit(state)

    @Slot(int, str, str, str)
    def _relay_health(self, generation, component, state, message):
        if generation == self._generation and not self._stopping:
            self._apply_health(component, state, message)

    @Slot(int, object)
    def _relay_shot(self, generation, shot):
        if generation == self._generation and not self._stopping:
            self._on_shot(shot)

    @Slot(int, object)
    def _relay_practice_shot(self, generation, shot):
        if generation == self._generation and not self._stopping:
            # The stock tracker's calibration callback is always local practice,
            # even if the user enables live play before this Qt event arrives.
            self._on_putting_preview(shot)

    @Slot(object)
    def _on_putting_preview(self, shot):
        try:
            shot.validate()
            if shot.source != "webcam" or shot.raw.get("tracker") != "springbok-stock":
                return
        except (ValueError, AttributeError):
            return
        self.store.data["putting"]["configured"] = True
        if self.putting:
            self.putting.config["configured"] = True
        self.store.save()
        self.shot_observed.emit(shot)
        self._on_delivery(shot, "practice", "Practice reading — not sent to GSPro")

    @Slot(int, object, object)
    def _relay_preview(self, generation, frame, details):
        if generation == self._generation and not self._stopping:
            self.preview.emit(frame, details)

    @Slot(int, str, str)
    def _relay_gspro_status(self, generation, state, message):
        if generation == self._generation and not self._stopping:
            self._on_gspro_status(state, message)

    @Slot(int, str, str)
    def _relay_player(self, generation, club, handed):
        if generation == self._generation and not self._stopping:
            self._on_player(club, handed)

    @Slot(int, str, str, object)
    def _relay_player_context(self, generation, club, handed, distance):
        if generation == self._generation and not self._stopping:
            self._on_player_context(club, handed, distance)

    @Slot(int, str)
    def _relay_mode(self, generation, mode):
        if generation == self._generation and not self._stopping:
            self.shot_mode = mode
            self.publish()

    @Slot(int, object, str, str)
    def _relay_delivery(self, generation, shot, state, message):
        if generation == self._generation and not self._stopping:
            self._on_delivery(shot, state, message)
        elif state in {"accepted", "rejected", "unconfirmed"}:
            # Preserve the outcome of bytes already written in the old session,
            # but never use an old ACK to satisfy the new session's setup gate.
            self._submission_context.pop(shot.event_id, None)
            self._on_delivery(shot, state, "Previous session: " + message)

    def _set_live(self, enabled):
        self.live_enabled = bool(enabled)
        self._capture_epoch = time.monotonic() + time.get_clock_info("monotonic").resolution
        if self.gspro:
            self.gspro.set_delivery_enabled(self.live_enabled)
            if not self.live_enabled and hasattr(self.gspro, "set_monitor_ready"):
                self.gspro.set_monitor_ready(False)
        if not self.live_enabled:
            self._target_timer.stop()
            self._auto_mode = None
            self._required_mode = None
            if self.source and hasattr(self.source, "set_target_mode"):
                self.source.set_target_mode(None)

    @Slot(str, str, str)
    def _apply_health(self, component, state, message):
        if self._stopping:
            return
        previous = self.health.get(component)
        self.health[component] = {"state": state, "message": message}
        if previous != self.health[component]:
            self.record(message)
            self.publish()

    def _create_gspro(self):
        if self.gspro is None:
            generation = self._generation
            self.gspro = GSProClient(self.store.data["gspro_host"], self.store.data["gspro_port"],
                                     on_status=lambda state, msg: self._gspro_status.emit(generation, state, msg),
                                     on_context=lambda club, handed, distance: self._player_context.emit(generation, club, handed, distance),
                                     on_delivery=lambda shot, state, msg: self._delivery.emit(generation, shot, state, msg))
            self.gspro.set_delivery_enabled(self.live_enabled)
        self.gspro.start()

    @staticmethod
    def _native_reader_path():
        if getattr(sys, "frozen", False):
            return Path(sys.executable).parent.parent / "reader" / "FsGolfReader.exe"
        return resource_path("dist/MevoCompanion/reader/FsGolfReader.exe")

    def connect_session(self, live=False, setup=False):
        self._stopping = False
        self.session_requested = True
        self.setup_mode = setup
        self._set_live(live)
        if self.demo:
            for key in self.health:
                self.health[key] = {"state": "connected" if key == "gspro" else "ready", "message": "Preview only — no hardware connection"}
            self.club = "7I"
            self.publish()
            return
        owned_pids = self.putting.owned_pids if self.putting is not None else set()
        other_processes = [item for item in platform.processes() if item.get("pid") not in owned_pids]
        conflicts = platform.conflicting_connectors(other_processes)
        if conflicts:
            self._apply_health("gspro", "action_needed", "Close the other connector before connecting: " + ", ".join(conflicts))
            self.session_requested = False
            self._set_live(False)
            return
        self._create_gspro()
        self.start_source()
        self.start_putting(calibration=setup and not live)
        self.update_routing()
        self.publish()

    def start_source(self):
        if self.source is not None or self.demo:
            return
        generation = self._generation
        on_shot = lambda shot: self._source_shot.emit(generation, shot)
        on_status = lambda state, msg: self._source_status.emit(generation, "mevo", state, msg)
        if self.store.data["adapter"] == "direct":
            if platform.fs_golf_running():
                self._apply_health("mevo", "action_needed", "Close FS Golf to use the experimental direct connection, or select FS Golf mode")
                return
            from companion.flighthook_adapter import FlightHookAdapter
            config = dict(self.store.data["mevo"])
            config.update(vendor_path=str(resource_path("vendor/flighthook/flighthook.exe")),
                          work_dir=str(self.store.directory / "direct"))
            self.source = FlightHookAdapter(config, on_shot, on_status)
        else:
            if not platform.fs_golf_running() and self.store.data["fs_golf_path"]:
                self.open_fs_golf()
            config = dict(self.store.data["ocr"])
            config.update(executable=self.store.data["fs_golf_path"] or None,
                          tessdata_path=str(resource_path("")))
            on_preview = lambda frame, details: self._source_preview.emit(generation, frame, details)
            if config.get("mode") == "legacy":
                from companion.ocr_adapter import OCRAdapter
                self.source = OCRAdapter(config, on_shot, on_status, on_preview=on_preview)
            else:
                from companion.accessibility_adapter import AccessibilityAdapter
                helper = self._native_reader_path()
                if not helper.is_file():
                    self._apply_health("mevo", "action_needed", "The FS Golf reader is missing. Rebuild or reinstall Mevo Companion.")
                    return
                config["helper_path"] = str(helper)
                config["auto_start_session"] = self.session_requested or bool(self.store.data["setup_complete"])
                self.source = AccessibilityAdapter(config, on_shot, on_status, on_preview=on_preview,
                                                  on_mode=lambda mode: self._source_mode.emit(generation, mode))
        self.source.start()

    def start_putting(self, calibration=False):
        if self.demo:
            return
        if self.putting is None:
            from companion.putting_adapter import PuttingAdapter
            config = dict(self.store.data["putting"])
            config["vendor_path"] = str(resource_path("vendor/springbok-putting/ball_tracking.exe"))
            self.putting = PuttingAdapter(config, self.store.directory / "putting", self)
            generation = self._generation
            self.putting.status.connect(lambda state, msg: self._source_status.emit(generation, "putting", state, msg))
            self.putting.shot.connect(lambda shot: self._source_shot.emit(generation, shot))
            self.putting.preview_shot.connect(lambda shot: self._source_preview_shot.emit(generation, shot))
        try:
            started = self.putting.start(calibration=calibration)
            # Resolve an imported numeric camera index once, then persist its
            # device identity so USB enumeration changes cannot select another.
            identity = self.putting.config.get("camera_id", "") if started else ""
            if started and identity and identity != self.store.data["putting"].get("camera_id"):
                self.store.data["putting"]["camera_id"] = identity
                self.store.save()
        except Exception as exc:
            self._apply_health("putting", "action_needed", str(exc))
        self.update_routing()

    def setup_putting(self):
        self.setup_mode = True
        self._set_live(False)
        self.start_putting(calibration=True)
        if self.putting:
            self.putting.show_preview()
            self.putting.set_active(True)
        self.publish()

    def update_routing(self):
        if self.source and hasattr(self.source, "set_active"):
            self.source.set_active(self.club != "PT" or (self.setup_mode and not self.live_enabled))
        if self.putting:
            self.putting.set_active(self.club == "PT" or (self.setup_mode and not self.live_enabled))

    def play(self):
        if self.demo:
            self.connect_session(live=True)
            return
        path = self.store.data["gspro_path"]
        if not platform.gspro_running():
            if not path:
                self._apply_health("gspro", "action_needed", "Choose your GSPro application in Settings, or open GSPro yourself")
                return
            try:
                platform.launch_app(path)
            except Exception as exc:
                self._apply_health("gspro", "action_needed", f"GSPro could not start: {exc}")
                return
        self.connect_session(live=bool(self.store.data["setup_complete"]), setup=not self.store.data["setup_complete"])

    def open_fs_golf(self):
        if self.demo:
            return
        if platform.fs_golf_running():
            return
        if time.monotonic() - self._last_golf_launch < 15:
            return
        try:
            self._last_golf_launch = time.monotonic()
            self._owned_golf = platform.launch_app(self.store.data["fs_golf_path"])
            self.record("Opening FS Golf")
        except Exception as exc:
            self._apply_health("mevo", "action_needed", f"Choose the FS Golf application in Settings: {exc}")

    def set_chipping(self, enabled):
        if self.chipping != bool(enabled):
            self._set_live(self.live_enabled)
        self.chipping = bool(enabled)
        if self.source and hasattr(self.source, "set_mode"):
            self.source.set_mode("chipping" if enabled else "full")
        else:
            self.record("Select Chipping in FS Golf" if enabled else "Select Full Swing in FS Golf")
        self.publish()

    @Slot(str, str)
    def _on_player(self, club, handed):
        self._on_player_context(club, handed, None)

    def _on_player_context(self, club, handed, distance):
        if (club, handed, distance) == (self.club, self.handedness, self.distance_to_target_yards):
            return  # Repeated 201 state must not cancel an in-progress switch.
        self.club, self.handedness = club, handed
        self.distance_to_target_yards = distance
        next_mode = automatic_shot_mode(distance, club, self.store.data.get("auto_chipping", True) and self.live_enabled)
        if next_mode != self._required_mode:
            self._auto_context_at = time.monotonic()
            self._required_mode = next_mode
        if next_mode != self._auto_mode:
            self._auto_mode = None
            if self.source and hasattr(self.source, "set_target_mode"):
                self.source.set_target_mode(None)
        self.record(f"GSPro selected {'webcam putting' if club == 'PT' else 'Mevo+'} ({club})" if club
                    else "GSPro round ended; waiting for the next session")
        self.update_routing()
        # The installed Open Connect emits a club event with the old distance,
        # then a distance event on its next 250 ms tick. Coalesce that pair.
        self._target_timer.start()
        self.publish()

    def apply_auto_chipping(self):
        self._target_timer.stop()
        supported = self.source and hasattr(self.source, "set_target_mode")
        desired = automatic_shot_mode(self.distance_to_target_yards, self.club,
                                      self.store.data.get("auto_chipping", True) and self.live_enabled and supported)
        if desired != self._required_mode:
            self._auto_context_at = time.monotonic()
            self._required_mode = desired
        self._auto_mode = desired
        if supported:
            self.source.set_target_mode(desired)
        self.publish()

    @Slot(str, str)
    def _on_gspro_status(self, state, message):
        if state != "connected":
            self.club = ""
            self.distance_to_target_yards = None
            self._target_timer.stop()
            self._auto_mode = None
            self._required_mode = None
            if self.source and hasattr(self.source, "set_target_mode"):
                self.source.set_target_mode(None)
            self.update_routing()
        self._apply_health("gspro", state, message)

    @Slot(object)
    def _on_shot(self, shot):
        self.shot_observed.emit(shot)
        try:
            capture_started = shot_capture_start(shot)
        except ValueError:
            capture_started = float("-inf")
        expected_mode = automatic_shot_mode(self.distance_to_target_yards, self.club,
                                           self.store.data.get("auto_chipping", True))
        if not self.live_enabled:
            self._on_delivery(shot, "practice", "Practice reading — not sent to GSPro")
        elif capture_started < self._capture_epoch:
            self._on_delivery(shot, "not_sent", "Reading belongs to the previous delivery mode; it was discarded")
        elif (expected_mode and shot.source == "mevo" and shot.raw.get("capture_context") == "play_mode"
              and capture_started >= self._auto_context_at and shot.raw.get("shot_mode") != expected_mode):
            self._on_delivery(shot, "not_sent", "FS Golf had not switched to the required shot mode. Wait for Ready before hitting.")
        elif self.gspro:
            if shot.event_id in self._submission_context:
                self.record("Duplicate reading ignored")
                return
            is_chip = (shot.raw.get("shot_mode") == "chipping"
                       if shot.raw.get("capture_context") == "play_mode" else self.chipping)
            self._submission_context[shot.event_id] = {
                "generation": self._generation, "setup": self.setup_mode,
                "validation_key": "putt" if shot.source == "webcam" else "chip" if is_chip else "swing",
            }
            self.gspro.submit(shot)

    @Slot(object, str, str)
    def _on_delivery(self, shot, state, message):
        if state == "submitted":
            # The next lie must be established by a new GSPro player event.
            self.distance_to_target_yards = None
            self._target_timer.stop()
            self._auto_mode = None
            self._required_mode = None
            if self.source and hasattr(self.source, "set_target_mode"):
                self.source.set_target_mode(None)
        if state not in {"submitted"}:
            self.recent_shots.appendleft({"time": datetime.now().strftime("%H:%M:%S"),
                                         "source": shot.source, "speed": shot.speed_mph,
                                         "hla": shot.hla, "vla": shot.vla, "spin_rpm": shot.spin_rpm,
                                         "spin_axis": shot.spin_axis, "club_speed": shot.club_speed_mph,
                                         "state": state, "message": message})
        context = self._submission_context.get(shot.event_id)
        if state == "accepted" and context and context["setup"] and context["generation"] == self._generation:
            key = context["validation_key"]
            self.store.data["validation"][key] = True
            self.store.save()
        if state != "submitted":
            self._submission_context.pop(shot.event_id, None)
        self.record(message)
        self.delivery.emit(shot, state, message)
        self.publish()

    def pause(self):
        self._set_live(False)
        self.record("Shot delivery paused")
        self.publish()

    def resume(self):
        if not self.session_requested:
            self.connect_session(live=True)
        else:
            if self.putting:
                self.start_putting(calibration=False)
            self._set_live(True)
            self.update_routing()
            self.apply_auto_chipping()
            self.publish()

    def stop(self):
        self._stopping = True
        self._generation += 1
        self._capture_epoch = time.monotonic()
        self._submission_context.clear()
        self.session_requested = False
        self.live_enabled = False
        self._target_timer.stop()
        self.distance_to_target_yards = None
        self._auto_mode = None
        self._required_mode = None
        self.shot_mode = ""
        self.setup_mode = False
        if self.gspro:
            self.gspro.stop()
            self.gspro = None
        if self.source:
            self.source.stop()
            self.source = None
        if self.putting:
            self.putting.stop()
            self.putting.deleteLater()
            self.putting = None
        self.club = ""
        self.health = {"gspro": {"state": "idle", "message": "Open GSPro to connect"},
                       "mevo": {"state": "idle", "message": "Connection stopped"},
                       "putting": {"state": "idle", "message": "Camera released"}}
        self._stopping = False
        self.record("Session stopped; your applications remain open")
        self.publish()

    def reconfigure(self):
        requested, live, setup = self.session_requested, self.live_enabled, self.setup_mode
        self.stop()
        if requested:
            self.connect_session(live, setup)

    def _tick(self):
        if self.demo or self._stopping:
            return
        running = platform.gspro_running()
        if running:
            self._session_saw_gspro = True
            if not self.session_requested and self.store.data["auto_connect"] and self.store.data["setup_complete"]:
                self.connect_session(live=True)
        elif self._session_saw_gspro and self.session_requested and not self.setup_mode:
            # Process exit ends acquisition. A transient TCP error alone does not.
            self._session_saw_gspro = False
            self.stop()

    def diagnostics(self, destination: str) -> str:
        settings = dict(self.store.data)
        report = {"app": "Mevo Companion", "settings": settings, "session": self.snapshot(),
                  "hardware_validated": bool(settings["validation"].get("confirmed"))}
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("diagnostics.json", json.dumps(report, indent=2, default=str))
            log_path = self.store.directory / "companion.log"
            if log_path.is_file():
                archive.write(log_path, "companion.log")
        return destination
