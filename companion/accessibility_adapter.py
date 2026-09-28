"""Read native FS Golf UI Automation values without screenshots or OCR regions.

The isolated Windows helper owns UI Automation calls. Python validates units,
live context, stable metrics, and shot ordinal freshness before producing a Shot.
Rejected native snapshots never trigger an automatic fallback to screen OCR.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
import logging
import math
from pathlib import Path
import queue
import subprocess
import threading
import time

from .config import resource_path
from .external_process import launch_external
from .models import Shot
from .ocr_adapter import Recognition, REQUIRED, parse_number


LOG = logging.getLogger(__name__)
SPEED_MULTIPLIER = {"mph": 1.0, "km/h": 0.621371192237334, "m/s": 2.2369362920544}


def recognize_snapshot(snapshot: dict, speed_unit: str = "auto") -> Recognition:
    """Use the same strict numeric rules as OCR, with native labels and units."""
    result = Recognition(mode="accessibility")
    readings = snapshot.get("readings", {})
    if not isinstance(readings, dict):
        result.errors.append("FS Golf did not expose its native shot fields")
        return result
    total = snapshot.get("total_shots")
    if type(total) is int:
        result.shot_id = str(total)
    for key in (*REQUIRED, "club_speed_mph"):
        reading = readings.get(key)
        if not isinstance(reading, dict) or not isinstance(reading.get("text"), str):
            if key in REQUIRED:
                result.errors.append(f"FS Golf is not showing {key.replace('_', ' ')}")
            continue
        raw = reading["text"]
        result.raw[key] = raw
        try:
            number = parse_number(raw, key)
            if key in {"speed_mph", "club_speed_mph"}:
                observed = str(reading.get("unit", "")).strip().casefold().replace(" ", "")
                observed = {"kmh": "km/h", "kph": "km/h"}.get(observed, observed)
                unit = observed or speed_unit
                if observed and speed_unit not in {"auto", observed}:
                    raise ValueError("FS Golf's speed unit changed. Update the speed unit in setup.")
                if unit not in SPEED_MULTIPLIER:
                    raise ValueError("FS Golf's native ball-speed unit is unavailable. Choose its speed unit in setup.")
                number *= SPEED_MULTIPLIER[unit]
                if number > 250:
                    raise ValueError("FS Golf speed is outside the supported range")
            result.values[key] = number
        except ValueError as exc:
            if key in REQUIRED:
                result.errors.append(str(exc))
    return result


def native_fields_visible(snapshot: dict, speed_unit: str = "auto") -> bool:
    """An empty live session is ready only in the supported metric layout."""
    readings = snapshot.get("readings")
    if not isinstance(readings, dict) or not all(
            isinstance(readings.get(key), dict) and isinstance(readings[key].get("text"), str)
            and readings[key]["text"].strip() for key in REQUIRED):
        return False
    observed = str(readings["speed_mph"].get("unit", "")).strip().casefold().replace(" ", "")
    observed = {"kmh": "km/h", "kph": "km/h"}.get(observed, observed)
    return (observed or speed_unit) in SPEED_MULTIPLIER and (not observed or speed_unit in {"auto", observed})


class AccessibilityShotGate:
    """Require a new latest live shot, not a changed value in historical review."""
    def __init__(self, stable_frames=3, stable_seconds=.5, speed_unit="auto"):
        self.stable_frames = max(2, int(stable_frames))
        self.stable_seconds = max(.25, float(stable_seconds))
        self.speed_unit = speed_unit
        self.reading = Recognition(mode="accessibility")
        self.last_reason = "Waiting for FS Golf's live practice screen"
        self.ready = False
        self.reset()

    def reset(self) -> None:
        self.baseline: int | None = None
        self._identity = None
        self._epoch = None
        self._candidate = None
        self._candidate_count = 0
        self._candidate_since = 0.0
        self._pending_since: float | None = None
        self._pending_captured_at: float | None = None
        self._last_observed_at: float | None = None
        self._play_armed = False
        self._play_cycle_started: float | None = None
        self._play_cycle_observed: float | None = None
        self._play_cycle_kind: str | None = None
        self.ready = False
        self.state = "checking"

    def _hold(self, message: str, *, reset=False, state="checking") -> None:
        if reset:
            self.reset()
        self.ready = False
        self.state = state
        self.last_reason = message

    @staticmethod
    def _capture_time(snapshot: dict, now: float, wall_time: float) -> float:
        stamp = snapshot.get("timestamp")
        if not isinstance(stamp, str):
            raise ValueError("Native FS Golf snapshot has no capture timestamp")
        try:
            parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("Timestamp is missing its time zone")
            age = wall_time - parsed.timestamp()
            duration = snapshot.get("duration_ms", 0)
            if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration):
                raise ValueError("Invalid scan duration")
            if not 0 <= duration <= 2000:
                raise ValueError("FS Golf's native screen took too long to read")
            age += duration / 1000  # The helper stamps the end of the UIA scan.
            if not -.5 <= age <= 3.0:
                raise ValueError("An old native FS Golf snapshot was discarded")
            return now - max(0, age)
        except (TypeError, OverflowError, OSError) as exc:
            raise ValueError("Invalid native FS Golf capture timestamp") from exc

    def observe(self, snapshot: dict, *, now: float | None = None,
                wall_time: float | None = None, epoch=0) -> Shot | None:
        now = time.monotonic() if now is None else now
        wall_time = time.time() if wall_time is None else wall_time
        if not isinstance(snapshot, dict) or snapshot.get("type") != "snapshot":
            self._hold("Waiting for a native FS Golf snapshot", reset=True)
            return None
        self.reading = recognize_snapshot(snapshot, self.speed_unit)
        if epoch != self._epoch:
            self.reset()
            self._epoch = epoch
        try:
            captured_at = self._capture_time(snapshot, now, wall_time)
        except ValueError as exc:
            self._hold(str(exc), reset=True, state="needs_attention")
            return None
        if self._last_observed_at is not None and captured_at <= self._last_observed_at:
            self._hold("Repeated or out-of-order native FS Golf snapshot discarded")
            return None
        self._last_observed_at = captured_at
        if snapshot.get("live_context") is not True:
            message = snapshot.get("error") or "Open a live FS Golf practice session. Review shots are never forwarded."
            self._hold(str(message), reset=True, state="needs_attention")
            return None
        if snapshot.get("context_stable") is False:
            self._hold("FS Golf is changing shot mode; existing readings are held", reset=True)
            return None
        if snapshot.get("capture_context") == "play_mode" and snapshot.get("shot_mode") not in {"full_swing", "chipping"}:
            self._hold("Choose Full Swing or Chipping in FS Golf's Play Mode", reset=True, state="needs_attention")
            return None
        window = snapshot.get("window")
        if not isinstance(window, dict) or type(window.get("pid")) is not int or not window.get("hwnd"):
            self._hold("FS Golf's native window identity is unavailable", reset=True, state="needs_attention")
            return None
        identity = (window["pid"], str(window["hwnd"]), snapshot.get("session_id"),
                    snapshot.get("capture_context"), snapshot.get("shot_mode"))
        if identity != self._identity:
            self.reset()
            self._epoch = epoch
            self._identity = identity
            self._last_observed_at = captured_at
        # The native helper joins status and flight mode: "Ready · Limited Flight".
        radar = str(snapshot.get("radar_status", "")).split("·", 1)[0].strip().casefold().rstrip(".\u2026")
        if radar in {"sleeping", "asleep", "disconnected", "not connected", "connection lost", "error"}:
            self._hold("Wake or reconnect Mevo+ in FS Golf; existing readings are held", reset=True, state="needs_attention")
            return None
        if snapshot.get("ok") is not True or snapshot.get("counter_stable") is False:
            if snapshot.get("counter_stable") is False:
                self._candidate = None
                self._candidate_count = 0
                self._hold("FS Golf is updating its new shot; waiting for consistent fields")
            else:
                self._hold(str(snapshot.get("error") or "FS Golf's native shot fields are unavailable"), reset=True, state="needs_attention")
            return None
        selected, total = snapshot.get("selected_shot"), snapshot.get("total_shots")
        if type(selected) is not int or type(total) is not int or not 0 <= selected <= total <= 1_000_000:
            self._hold("FS Golf's native shot counter is unavailable", reset=True, state="needs_attention")
            return None
        if selected != total:
            self._hold("Select the latest shot in FS Golf. Historical shots are never forwarded.", reset=True, state="needs_attention")
            return None
        if self.baseline is None:
            if radar != "ready":
                self._hold("Waiting for Mevo+ to be Ready in FS Golf")
                return None
            self.baseline = total
            self.ready = (total == 0 and native_fields_visible(snapshot, self.speed_unit)) or self.reading.valid
            self.state = "ready" if self.ready else "needs_attention"
            self.last_reason = ("Mevo+ is Ready in live FS Golf. Waiting for the next shot." if self.ready
                                else self.reading.errors[0])
            if snapshot.get("capture_context") == "play_mode":
                self._play_armed = self.ready
            return None  # Includes total=0, so the first new shot can be accepted.
        if total < self.baseline or total > self.baseline + 1:
            self.baseline = total
            self._candidate = None
            self._pending_since = None
            self._pending_captured_at = None
            self._clear_play_cycle()
            self._hold("FS Golf's shot counter changed sessions or skipped shots; existing readings were held")
            return None
        if snapshot.get("capture_context") == "play_mode":
            return self._observe_play(snapshot, radar, total, now, captured_at, window)
        if total == self.baseline:
            self._candidate = None
            self._pending_since = None
            self._pending_captured_at = None
            self.ready = radar == "ready" and ((total == 0 and native_fields_visible(snapshot, self.speed_unit)) or self.reading.valid)
            self.state = "ready" if self.ready else "checking" if radar != "ready" else "needs_attention"
            self.last_reason = ("Mevo+ is Ready in live FS Golf. Waiting for the next shot." if self.ready
                                else "Waiting for Mevo+ to be Ready in FS Golf" if radar != "ready"
                                else "Open FS Golf's native shot metrics with Ball Speed, Spin, Spin Axis, Launch V and Launch H")
            return None
        if self._pending_since is None:
            self._pending_since = now
            self._pending_captured_at = captured_at
        if now - self._pending_since > 3.0:
            self.baseline = total
            self._candidate = None
            self._pending_since = None
            self._hold("New shot fields took too long to settle. That reading was discarded.")
            return None
        if radar != "ready":
            self._candidate = None
            self._hold("FS Golf is processing the new shot; waiting for Mevo+ Ready")
            return None
        if not self.reading.valid:
            self._candidate = None
            self._hold(self.reading.errors[0] if self.reading.errors else "Waiting for complete native shot fields")
            return None
        fingerprint = (total, tuple(sorted(self.reading.values.items())))
        if fingerprint != self._candidate:
            self._candidate, self._candidate_count, self._candidate_since = fingerprint, 1, now
        else:
            self._candidate_count += 1
        self._hold("Checking the new native FS Golf shot")
        if self._candidate_count < self.stable_frames or now - self._candidate_since < self.stable_seconds:
            return None
        self.baseline = total
        self._candidate = None
        self.ready = True
        self.state = "ready"
        self.last_reason = "New live FS Golf shot measured without screen regions"
        shot = self.reading.to_shot(captured_at=self._pending_captured_at)
        self._pending_since = None
        self._pending_captured_at = None
        return replace(shot, raw={"accessibility": self.reading.raw.copy(), "shot_id": str(total),
                                  "mode": "accessibility", "window": dict(window),
                                  "capture_context": snapshot.get("capture_context"),
                                  "shot_mode": snapshot.get("shot_mode"),
                                  "radar_status": snapshot.get("radar_status")})

    def _clear_play_cycle(self) -> None:
        self._play_armed = False
        self._play_cycle_started = None
        self._play_cycle_observed = None
        self._play_cycle_kind = None
        self._candidate = None
        self._candidate_count = 0
        self._pending_since = None
        self._pending_captured_at = None

    @staticmethod
    def _fields_cleared(snapshot: dict) -> bool:
        readings = snapshot.get("readings", {})
        return isinstance(readings, dict) and all(
            isinstance(readings.get(key), dict) and isinstance(readings[key].get("text"), str)
            and readings[key]["text"].strip() == "-"
            for key in REQUIRED)

    def _observe_play(self, snapshot, radar, total, now, captured_at, window) -> Shot | None:
        """Play Mode retains only ten shots, so its ordinal eventually stops.

        At that cap only a Ready -> Tracking / cleared-fields acquisition cycle
        proves a new measurement. Arming, mode changes, and changed values alone
        never do. Below the cap a new latest ordinal is independent evidence.
        """
        if self._play_cycle_started is None:
            kind = ("counter" if total == self.baseline + 1 else
                    "tracking" if self._play_armed and radar == "tracking" and self._fields_cleared(snapshot) else None)
            if kind is not None:
                self._play_cycle_started = captured_at
                self._play_cycle_observed = now
                self._play_cycle_kind = kind
                self._play_armed = False
                self._candidate = None
                self._candidate_count = 0
                self._pending_since = None
                self._pending_captured_at = None
            else:
                self.ready = radar == "ready" and ((total == 0 and native_fields_visible(snapshot, self.speed_unit)) or self.reading.valid)
                if self.ready:
                    self._play_armed = True
                self.state = "ready" if self.ready else "checking"
                self.last_reason = ("Mevo+ is Ready in FS Golf Play Mode. Waiting for the next shot." if self.ready
                                    else "Waiting for a new measured shot in FS Golf Play Mode")
                return None

        # Acquisition and settling have separate bounded waits. The first
        # complete measurement is timestamped once, never refreshed by retries.
        if now - self._play_cycle_observed > 8.0:
            self.baseline = total
            self._clear_play_cycle()
            self._hold("FS Golf's tracking cycle took too long. That reading was discarded.")
            return None
        if radar == "tracking":
            if self._pending_since is not None:
                self.baseline = total
                self._clear_play_cycle()
                self._hold("Another tracking cycle started before the previous reading settled; both were held")
            else:
                self._hold("Mevo+ is tracking the new shot")
            return None
        if self._pending_since is not None and now - self._pending_since > 3.0:
            self.baseline = total
            self._clear_play_cycle()
            self._hold("New Play Mode shot fields took too long to settle. That reading was discarded.")
            return None
        if radar not in {"connected", "arming", "ready"} or not self.reading.valid:
            self._candidate = None
            self._candidate_count = 0
            self._hold("Waiting for complete measured fields after FS Golf tracking")
            return None
        if self._pending_since is None:
            self._pending_since = now
            self._pending_captured_at = captured_at
        fingerprint = (total, tuple(sorted(self.reading.values.items())))
        if fingerprint != self._candidate:
            self._candidate = fingerprint
            self._candidate_count = 1
            self._candidate_since = now
        else:
            self._candidate_count += 1
        self._hold("Checking the completed FS Golf Play Mode shot")
        # Cleared native fields and an actual tracking cycle provide a stronger
        # freshness marker than values alone. Two matching coherent scans over
        # >=0.5 s are sufficient even when UIA calls take nearly a second each.
        if self._candidate_count < 2 or now - self._candidate_since < self.stable_seconds:
            return None
        shot = self.reading.to_shot(captured_at=self._pending_captured_at)
        raw = {"accessibility": self.reading.raw.copy(), "shot_id": str(total), "mode": "accessibility",
               "window": dict(window), "capture_context": "play_mode", "shot_mode": snapshot.get("shot_mode"),
               "radar_status": snapshot.get("radar_status"), "freshness": self._play_cycle_kind,
               "capture_started_at": self._play_cycle_started}
        self.baseline = total
        self._clear_play_cycle()
        self.ready = radar == "ready"
        self._play_armed = self.ready
        self.state = "ready" if self.ready else "checking"
        self.last_reason = ("New FS Golf Play Mode shot measured; Mevo+ is Ready" if self.ready
                            else "New FS Golf Play Mode shot measured; waiting for Mevo+ to rearm")
        return replace(shot, raw=raw)


class AccessibilityAdapter:
    """Worker API compatible with OCRAdapter; the native helper is owned/reaped."""
    def __init__(self, config: dict, on_shot, on_status, on_preview=None, on_mode=None):
        self.config = dict(config)
        self.on_shot, self.on_status, self.on_preview = on_shot, on_status, on_preview
        self.on_mode = on_mode or (lambda *_: None)
        self.helper_path = Path(self.config.get("helper_path") or resource_path("helpers/FsGolfReader/FsGolfReader.exe"))
        self._stop = threading.Event()
        self._thread = None
        self._process = None
        self._active = True
        self._epoch = 0
        self._helper_epoch = 0
        self._lock = threading.Lock()
        self._process_lock = threading.Lock()
        self._status = None
        self._startup_attempted = False
        self._target_mode = None
        self._observed_mode = ""
        self._mode_pending = None
        self._mode_attempts = 0
        self._mode_retry_at = 0.0
        self._mode_serial = 0
        self.gate = AccessibilityShotGate(self.config.get("stable_frames", 3),
                                          self.config.get("stable_seconds", .5), self.config.get("speed_unit", "auto"))

    def _emit_status(self, state, message):
        value = state, message
        if value != self._status:
            self._status = value
            self.on_status(*value)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._status = None
        self._startup_attempted = False
        self.gate.reset()
        self._thread = threading.Thread(target=self._run, name="FS Golf native values", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        with self._lock:
            self._epoch += 1
        self._stop_helper()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=3)

    def set_active(self, active: bool):
        with self._lock:
            if self._active != bool(active):
                self._active = bool(active)
                self._epoch += 1

    def set_target_mode(self, mode):
        """Request a mode on the owning worker; never invoke UIA on the UI thread."""
        if mode not in {None, "full_swing", "chipping"}:
            raise ValueError("Unknown FS Golf shot mode")
        with self._lock:
            if mode != self._target_mode:
                self._target_mode = mode
                self._mode_pending = None
                self._mode_attempts = 0
                self._mode_retry_at = 0.0

    def _apply_target_mode(self, snapshot, shot=None):
        """Return True while acquisition must be held for an actual mode change."""
        observed = (snapshot.get("shot_mode") if snapshot.get("capture_context") == "play_mode"
                    and snapshot.get("live_context") is True and snapshot.get("context_stable") is not False else "") or ""
        if observed != self._observed_mode:
            self._observed_mode = observed
            self.on_mode(observed)
        with self._lock:
            target = self._target_mode if self._active else None
            if not target or observed == target:
                self._mode_pending = None
                return False
            # Complete an already tracked shot before changing the next mode.
            if shot is not None or not self.gate.ready:
                if self._mode_pending is not None:
                    self.gate.reset()
                    self._emit_status("checking", "Waiting for FS Golf to confirm the new shot mode")
                    return True
                return False
            now = time.monotonic()
            if now < self._mode_retry_at:
                self._emit_status("checking", "Switching FS Golf to " + ("Chipping" if target == "chipping" else "Full Swing"))
                return True
            if self._mode_attempts >= 3:
                self._emit_status("needs_attention", "Choose " + ("Chipping" if target == "chipping" else "Full Swing")
                                  + " in FS Golf, or turn off automatic chipping in Settings")
                return True
            self._mode_serial += 1
            request_id = f"{self._helper_epoch}:{self._mode_serial}"
            command = {"type": "set_shot_mode", "mode": target, "request_id": request_id}
            with self._process_lock:
                process = self._process
                if process is None or process.poll() is not None or process.stdin is None:
                    return True
                process.stdin.write((json.dumps(command) + "\n").encode("utf-8"))
                process.stdin.flush()
            self._mode_pending = request_id
            self._mode_attempts += 1
            self._mode_retry_at = now + 3
            # A fresh observed mode establishes a new baseline. Existing values
            # on the old view must never become a shot during the transition.
            self.gate.reset()
            self._emit_status("checking", "Switching FS Golf to " + ("Chipping" if target == "chipping" else "Full Swing"))
            return True

    def _stop_helper(self):
        with self._process_lock:
            process, self._process = self._process, None
        if process is not None and process.poll() is None:
            try:
                if process.stdin:
                    process.stdin.close()  # The native helper exits on parent EOF.
                process.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                    process.wait(timeout=1)
                except OSError:
                    pass
            except Exception:
                try:
                    process.kill()
                    process.wait(timeout=1)
                except Exception:
                    pass

    @staticmethod
    def _read_lines(process, snapshots: queue.Queue):
        try:
            for line in iter(process.stdout.readline, b""):
                if len(line) > 1_000_000:
                    continue
                try:
                    value = json.loads(line.decode("utf-8-sig"))
                except (UnicodeError, ValueError):
                    LOG.warning("Native FS Golf helper diagnostic: %s", line[:500].decode("utf-8", errors="replace").strip())
                    continue
                while True:
                    try:
                        snapshots.put_nowait(value)
                        break
                    except queue.Full:
                        try:
                            snapshots.get_nowait()  # Keep current snapshots, never build a backlog.
                        except queue.Empty:
                            pass
        finally:
            process.stdout.close()

    def _run(self):
        try:
            while not self._stop.is_set():
                if not self.helper_path.is_file():
                    self._emit_status("needs_attention", "Native FS Golf reader is missing. Repair the app installation.")
                    self._stop.wait(2)
                    continue
                try:
                    milliseconds = max(150, min(1500, int(float(self.config.get("poll_seconds", .35)) * 1000)))
                    command = [str(self.helper_path), "--watch", "--interval-ms", str(milliseconds)]
                    if self.config.get("auto_start_session") is True and not self._startup_attempted:
                        command.append("--ensure-live")
                    process = launch_external(command, self.helper_path.parent, stdin=subprocess.PIPE,
                                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                    with self._process_lock:
                        self._process = process
                    self._helper_epoch += 1
                    self._mode_attempts = 0
                    self._mode_retry_at = 0.0
                    self._mode_pending = None
                    snapshots = queue.Queue(maxsize=2)
                    threading.Thread(target=self._read_lines, args=(process, snapshots), daemon=True,
                                     name="FS Golf native pipe").start()
                    self.gate.reset()
                    self._emit_status("checking", "Reading native FS Golf values; no screen boxes are needed")
                    last_message = time.monotonic()
                    while not self._stop.is_set():
                        try:
                            snapshot = snapshots.get(timeout=.2)
                        except queue.Empty:
                            if self._stop.is_set():
                                break
                            if process.poll() is not None:
                                raise RuntimeError("Native FS Golf reader stopped. Reconnecting…")
                            if time.monotonic() - last_message > 10:
                                raise RuntimeError("FS Golf is not answering native reads. Reconnecting…")
                            continue
                        last_message = time.monotonic()
                        if isinstance(snapshot, dict) and snapshot.get("type") == "command_result":
                            if snapshot.get("request_id") == self._mode_pending and not snapshot.get("success"):
                                LOG.info("FS Golf mode change: %s", snapshot.get("message", "not applied"))
                            continue  # Results contain no measurement or readiness evidence.
                        if isinstance(snapshot, dict) and snapshot.get("type") == "startup_progress":
                            # Native preparation waits can exceed a normal UIA
                            # read. Its progress keeps the watchdog alive, but
                            # cannot satisfy readiness or consume the one-shot
                            # automatic preparation attempt.
                            self.gate.reset()
                            self._emit_status("checking", str(snapshot.get("message") or "Preparing FS Golf's live session"))
                            continue
                        if isinstance(snapshot, dict) and self.config.get("auto_start_session") is True:
                            if snapshot.get("startup_status") in {"already_live", "prepared", "action_needed"}:
                                self._startup_attempted = True
                            elif not self._startup_attempted and snapshot.get("window"):
                                # FS Golf can open after the first --ensure-live
                                # read. Relaunch once now that its window exists.
                                break
                        with self._lock:
                            epoch, active = self._epoch, self._active
                        shot = self.gate.observe(snapshot, epoch=(self._helper_epoch, epoch))
                        if self._apply_target_mode(snapshot, shot):
                            continue
                        if self.on_preview:
                            self.on_preview(None, self.gate.reading)
                        self._emit_status(self.gate.state if active else "standby",
                                          self.gate.last_reason if active else "Mevo+ is standing by while the putter is selected")
                        with self._lock:
                            still_active = active and self._active and epoch == self._epoch
                        if shot and still_active and not self._stop.is_set():
                            self.on_shot(shot)
                except Exception as exc:
                    if not self._stop.is_set():
                        LOG.exception("Native FS Golf acquisition failed")
                        self.gate.reset()
                        self._emit_status("needs_attention", str(exc))
                finally:
                    self._stop_helper()
                self._stop.wait(1)
        finally:
            self._stop_helper()
