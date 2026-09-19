"""Supervised direct Mevo+ acquisition through a private FlightHook process.

FlightHook owns only the launch monitor; the companion owns GSPro. No history
endpoint is used for forwarding. Only a complete shot whose trigger was observed
on this connection can reach ``on_shot``.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import ipaddress
import json
import logging
import math
import os
from pathlib import Path
import re
import socket
import subprocess
import threading
import time
from typing import Callable
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener
import uuid

from .models import Shot
from .external_process import launch_external

LOG = logging.getLogger(__name__)
_SPEED = re.compile(r"^([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)(mph|mps|kph|fps)$")
_MPH = {"mph": 1.0, "mps": 2.2369362920544, "kph": 0.62137119223733, "fps": 0.68181818181818}


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"Missing or invalid {label}")
    return float(value)


def speed_mph(value: object) -> float:
    """FRP speeds always carry an explicit unit; never guess bare numbers."""
    match = _SPEED.fullmatch(value) if isinstance(value, str) else None
    if not match:
        raise ValueError("Missing or invalid speed unit")
    result = float(match[1]) * _MPH[match[2]]
    if not math.isfinite(result):
        raise ValueError("Invalid speed")
    return result


def shot_from_frp(ball: dict, club: dict | None, event_id: str, captured_at: float) -> Shot:
    """Convert complete measured ball data without filling missing measurements."""
    if not isinstance(ball, dict):
        raise ValueError("No complete ball reading. Estimated-only chips are not forwarded.")
    back = _number(ball.get("backspin_rpm"), "backspin")
    side = _number(ball.get("sidespin_rpm"), "sidespin")
    club_speed = None
    if club and club.get("club_speed") is not None:
        club_speed = speed_mph(club["club_speed"])
        if not 0 < club_speed <= 200:
            raise ValueError("Club speed is outside the supported range")
    shot = Shot(
        source="mevo", speed_mph=speed_mph(ball.get("launch_speed")),
        hla=_number(ball.get("launch_azimuth"), "horizontal launch angle"),
        vla=_number(ball.get("launch_elevation"), "vertical launch angle"),
        spin_rpm=math.hypot(back, side), spin_axis=math.degrees(math.atan2(side, back)),
        club_speed_mph=club_speed, event_id=event_id, captured_at=captured_at,
        # Upstream gspro/mapper.rs converts FRP target-relative lateral signs to
        # GSPro player-relative signs. The GSPro owner applies this exactly once.
        raw={"adapter": "flighthook", "ball": dict(ball), "club": dict(club or {}),
             "gspro_mirror_for_left_handed": True},
    )
    shot.validate()
    return shot


def build_config(config: dict, port: int, session_name: str) -> str:
    """Create the private TOML: no GSPro bridge and no injected shot listener."""
    address = str(config.get("mevo_address", "192.168.2.1:5100"))
    try:
        host, port_text = address.rsplit(":", 1)
        ipaddress.IPv4Address(host)
        if not 1 <= int(port_text) <= 65535:
            raise ValueError()
    except (ValueError, TypeError) as exc:
        raise ValueError("Mevo address must be an IPv4 address and port") from exc
    ball_type = config.get("ball_type", 0)
    if type(ball_type) is not int or ball_type not in (0, 1):
        raise ValueError("Ball type must be RCT (0) or standard (1)")
    camera_mode = config.get("camera_mode", "standard")
    if camera_mode not in {"standard", "fusion", "raw_fusion"}:
        raise ValueError("Unsupported Mevo camera mode")
    estimated = config.get("use_estimated", False)
    if type(estimated) is not bool:
        raise ValueError("Estimated-shot preference must be true or false")
    fields = []
    for key, target, default, lo, hi, suffix in (
        ("mevo_range_ft", "range", 8.0, 4.0, 20.0, "ft"),
        ("tee_height_in", "tee_height", 1.5, 0.0, 6.0, "in"),
        ("surface_height_in", "surface_height", 0.0, 0.0, 10.0, "in"),
        ("track_pct", "track_pct", 80.0, 0.0, 100.0, ""),
    ):
        value = _number(config.get(key, default), key)
        if not lo <= value <= hi:
            raise ValueError(f"{key} must be between {lo:g} and {hi:g}")
        fields.append(f"{target} = " + (json.dumps(f"{value:g}{suffix}") if suffix else f"{value:g}"))
    return "\n".join([
        'default_units = "imperial"', 'chipping_clubs = []', 'putting_clubs = ["PT"]', '',
        '[webserver.0]', f'name = {json.dumps(session_name)}', f'bind = "127.0.0.1:{port}"', '',
        '[mevo.0]', 'name = "Mevo+"', f'address = {json.dumps(address)}',
        f'ball_type = {ball_type}', f'camera_mode = {json.dumps(camera_mode)}',
        f'use_estimated = {str(estimated).lower()}', *fields, '',
    ])


@dataclass
class _Pending:
    captured_at: float
    generation: int
    device: str
    ball: dict | None = None
    club: dict | None = None


class FlightHookAdapter:
    """Threaded acquisition adapter. Callbacks run on the worker thread.

    Config keys: vendor_path, work_dir, mevo_address, mevo_range_ft,
    tee_height_in, surface_height_in, ball_type, track_pct, camera_mode,
    use_estimated, max_shot_age_s. See docs/flighthook-integration.md.
    """

    def __init__(self, config: dict, on_shot: Callable[[Shot], None],
                 on_status: Callable[[str, str], None]):
        self.config = dict(config)
        self.on_shot, self.on_status = on_shot, on_status
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._process: subprocess.Popen | None = None
        self._ws = None
        self._log = None
        self._http = build_opener(ProxyHandler({}))
        self._base_url = ""
        self._mode = "full"
        self._sent_mode: str | None = None
        self._generation = 0
        self._pending: dict[str, _Pending] = {}
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._connected = False
        self._ready = False
        self._device: str | None = None
        self._last_telemetry = 0.0
        self._waiting_mode = False
        self._mode_acknowledged = False
        self._mode_went_unready = False
        self._status = ("stopped", "Mevo+ is stopped")
        self._last_published: tuple[str, str] | None = None
        self._age_limit = _number(self.config.get("max_shot_age_s", 10.0), "shot age limit")
        if not 1 <= self._age_limit <= 30:
            raise ValueError("Shot age limit must be between 1 and 30 seconds")

    @property
    def ready(self) -> bool:
        with self._lock:
            return self._ready and self._connected and not self._waiting_mode

    @property
    def status(self) -> tuple[str, str]:
        with self._lock:
            return self._status

    def _publish(self, state: str, message: str) -> None:
        with self._lock:
            current = (state, message)
            self._status = current
            if current == self._last_published:
                return
            self._last_published = current
        try:
            self.on_status(state, message)
        except Exception:
            LOG.exception("Mevo+ status callback failed")

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="mevo-flighthook", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                LOG.debug("FlightHook socket already closed", exc_info=True)
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=4)
        self._reset_connection()

    def set_mode(self, mode: str) -> None:
        if mode not in {"full", "chipping"}:
            raise ValueError("Mevo+ mode must be full or chipping; webcam owns putting")
        with self._lock:
            if mode == self._mode:
                return
            self._mode = mode
            self._generation += 1
            self._pending.clear()
            self._ready = False
            self._waiting_mode = True
            self._mode_acknowledged = False
            self._mode_went_unready = False
        self._publish("connecting", "Applying chip mode" if mode == "chipping" else "Applying full-shot mode")

    def _reset_connection(self) -> None:
        with self._lock:
            self._generation += 1
            self._pending.clear()
            self._connected = self._ready = False
            self._device = None
            self._last_telemetry = 0.0
            self._sent_mode = None
            self._waiting_mode = self._mode != "full"
            self._mode_acknowledged = False
            self._mode_went_unready = False

    def _request(self, route: str, payload: dict | None = None):
        data = json.dumps(payload).encode() if payload is not None else None
        request = Request(self._base_url + route, data=data,
                          headers={"Content-Type": "application/json"})
        with self._http.open(request, timeout=1.5) as response:
            data = response.read(1024 * 1024)
        return json.loads(data) if data else None

    def _launch(self) -> None:
        vendor = Path(str(self.config.get("vendor_path", ""))).expanduser()
        if not vendor.is_file():
            raise FileNotFoundError("The bundled Mevo+ bridge is missing. Reinstall the companion.")
        work_dir = Path(self.config.get("work_dir") or
                        Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "MevoCompanion" / "bridge")
        work_dir.mkdir(parents=True, exist_ok=True)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        name = "Mevo Companion " + uuid.uuid4().hex
        content = build_config(self.config, port, name)
        config_path = work_dir / "flighthook-companion.toml"
        config_path.write_text(content, encoding="utf-8")
        self._base_url = f"http://127.0.0.1:{port}"
        self._log = (work_dir / "flighthook.log").open("ab")
        self._process = launch_external(
            [str(vendor.resolve()), "--headless", "--config", str(config_path.resolve())],
            work_dir,
            stdin=subprocess.DEVNULL, stdout=self._log, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        deadline = time.monotonic() + 12
        while not self._stop.is_set() and time.monotonic() < deadline:
            if self._process.poll() is not None:
                raise RuntimeError("The Mevo+ bridge stopped during startup. Open diagnostics for details.")
            try:
                settings = self._request("/api/settings")
                if settings.get("webserver", {}).get("0", {}).get("name") != name:
                    raise RuntimeError("The private bridge port is busy. Retrying with a new port.")
                return
            except (URLError, TimeoutError, OSError):
                self._stop.wait(0.15)
        if not self._stop.is_set():
            raise RuntimeError("The Mevo+ bridge did not start. Open diagnostics for details.")

    def _cleanup_process(self) -> None:
        process, self._process = self._process, None
        if process and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
            except OSError:
                LOG.exception("Could not stop owned FlightHook process")
        if self._log:
            self._log.close()
            self._log = None

    def _run(self) -> None:
        try:
            import websocket
        except ImportError:
            self._publish("action_needed", "The Mevo+ connection library is missing. Reinstall the companion.")
            return
        retries = 0
        while not self._stop.is_set():
            self._reset_connection()
            self._publish("connecting", "Connecting to Mevo+. Turn it on and connect to its Wi-Fi.")
            try:
                self._launch()
                if self._stop.is_set():
                    break
                self._ws = websocket.create_connection(
                    self._base_url.replace("http://", "ws://") + "/frp", timeout=0.4,
                    http_proxy_host=None, http_no_proxy=["127.0.0.1", "localhost"],
                )
                self._ws.send(json.dumps({"kind": "start", "version": ["0.1.0"], "name": "Mevo Companion"}))
                self._ws.settimeout(2)
                init = json.loads(self._ws.recv())
                if init.get("kind") != "init" or init.get("version") != "0.1.0":
                    raise RuntimeError("This Mevo+ bridge version is incompatible with the companion.")
                self._ws.settimeout(0.4)
                retries = 0
                while not self._stop.is_set():
                    if self._process.poll() is not None:
                        raise RuntimeError("The Mevo+ bridge stopped. Restarting it.")
                    with self._lock:
                        mode = self._mode
                    if self._sent_mode != mode:
                        self._request("/api/mode", {"mode": mode})
                        self._sent_mode = mode
                    try:
                        raw = self._ws.recv()
                    except websocket.WebSocketTimeoutException:
                        self._check_freshness()
                        continue
                    if not raw:
                        raise RuntimeError("The Mevo+ connection closed. Reconnecting.")
                    if len(raw) > 1024 * 1024:
                        raise RuntimeError("The Mevo+ bridge sent an oversized message.")
                    self.handle_message(json.loads(raw))
                    self._check_freshness()
            except (FileNotFoundError, ValueError) as exc:
                self._publish("action_needed", str(exc))
                break
            except Exception as exc:
                if not self._stop.is_set():
                    LOG.warning("FlightHook reconnect: %s", exc)
                    self._reset_connection()
                    self._publish("reconnecting", str(exc))
            finally:
                ws, self._ws = self._ws, None
                if ws:
                    try:
                        ws.close()
                    except Exception:
                        pass
                self._cleanup_process()
            retries += 1
            self._stop.wait(min(retries, 5))
        self._reset_connection()
        if self._stop.is_set():
            self._publish("stopped", "Mevo+ is stopped")

    def _check_freshness(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            stale = [key for key, shot in self._pending.items() if now - shot.captured_at > self._age_limit]
            for key in stale:
                self._pending.pop(key)
            if self._connected and self._last_telemetry and now - self._last_telemetry > 12:
                self._connected = self._ready = False
                self._pending.clear()
                self._publish("reconnecting", "No fresh Mevo+ status. Check its power and Wi-Fi connection.")
            elif stale:
                self._publish("rejected", "Mevo+ did not finish this reading in time. No shot was sent.")

    def handle_message(self, message: dict, now: float | None = None) -> None:
        """Consume one FRP event; public for recorded-event hardware diagnostics."""
        if not isinstance(message, dict):
            return
        event = message.get("event")
        if not isinstance(event, dict):
            return
        now = time.monotonic() if now is None else now
        with self._lock:
            kind = event.get("kind")
            if kind == "set_detection_mode" and event.get("mode") == self._mode:
                # This event fences queued startup telemetry from the requested
                # configuration. Only an unready -> ready transition after this
                # acknowledgement can arm a changed mode.
                self._mode_acknowledged = True
                self._mode_went_unready = False
                return
            if message.get("actor") != "mevo.0":
                return
            if kind == "actor_status":
                if event.get("status") != "connected":
                    self._generation += 1
                    self._pending.clear()
                    self._connected = self._ready = False
                    self._publish("reconnecting", "Waiting for Mevo+. Check its power and Wi-Fi connection.")
                else:
                    self._connected = True
                    if not self._ready:
                        self._publish("connecting", "Mevo+ connected. Waiting for it to finish preparing.")
                return
            if kind == "device_telemetry":
                device = message.get("device")
                if not isinstance(device, str) or not device:
                    return
                if self._device and self._device != device:
                    self._generation += 1
                    self._pending.clear()
                self._device = device
                self._last_telemetry = now
                telemetry = event.get("telemetry") or {}
                if not isinstance(telemetry, dict):
                    return
                ready = telemetry.get("ready") == "true"
                if self._waiting_mode:
                    if not ready and self._mode_acknowledged:
                        self._mode_went_unready = True
                    if ready and self._mode_went_unready:
                        self._waiting_mode = False
                self._ready = ready and self._connected and not self._waiting_mode
                if self._ready:
                    self._publish("ready", "Mevo+ is ready for chips" if self._mode == "chipping" else "Mevo+ is ready for full shots")
                elif not self._pending:
                    self._publish("connecting", "Mevo+ is preparing. Wait for ready before hitting.")
                return
            if kind == "alert":
                if event.get("severity") in {"error", "critical"}:
                    self._ready = False
                    self._pending.clear()
                    self._publish("action_needed", str(event.get("message", "Mevo+ needs attention")))
                return
            if kind not in {"shot_trigger", "ball_flight", "club_path", "shot_finished"}:
                return
            key = event.get("key")
            if not isinstance(key, dict):
                return
            event_id = key.get("shot_id")
            try:
                parsed_id = uuid.UUID(event_id)
                if parsed_id.version != 4 or type(key.get("shot_number")) is not int or key["shot_number"] <= 0:
                    return
                event_id = str(parsed_id)
            except (ValueError, AttributeError, TypeError):
                return
            if event_id in self._seen:
                return
            device = message.get("device")
            if not self._connected or not self._device or device != self._device or self._waiting_mode:
                return
            if kind == "shot_trigger":
                if event_id in self._pending or not self._ready:
                    return
                if len(self._pending) >= 16:
                    self._pending.clear()
                    self._publish("rejected", "Too many unfinished Mevo+ readings. No shot was sent.")
                    return
                self._pending[event_id] = _Pending(now, self._generation, device)
                self._publish("detecting", "Mevo+ is measuring the shot")
                return
            pending = self._pending.get(event_id)
            if not pending:
                return  # never resurrect a shot from history or after reconnect
            if pending.generation != self._generation or now - pending.captured_at > self._age_limit:
                self._pending.pop(event_id, None)
                self._publish("rejected", "The Mevo+ reading expired. No shot was sent.")
                return
            if kind == "ball_flight":
                pending.ball = event.get("ball")
            elif kind == "club_path":
                pending.club = event.get("club")
            elif kind == "shot_finished":
                self._pending.pop(event_id)
                self._seen[event_id] = None
                if len(self._seen) > 2048:
                    self._seen.popitem(last=False)
                try:
                    shot = shot_from_frp(pending.ball, pending.club, event_id, pending.captured_at)
                except (ValueError, TypeError, AttributeError) as exc:
                    self._publish("rejected", f"Incomplete Mevo+ reading: {exc}. No shot was sent.")
                    return
                try:
                    self.on_shot(shot)
                except Exception:
                    LOG.exception("Mevo+ shot callback failed; shot will not be replayed")
                    self._publish("action_needed", "The shot could not be handed to GSPro. Check the round before hitting again.")
