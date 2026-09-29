"""Manage Springbok's unmodified cam-putting executable and its localhost API.

No ball detection or speed algorithm lives here. Calibration and the live view
remain the exact native putting experience shipped with Springbok V1.04.51.
"""
from __future__ import annotations

from collections import deque
from configparser import ConfigParser, Error as ConfigError
import ast
import ctypes
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time

import cv2
import numpy as np
from cv2_enumerate_cameras import enumerate_cameras
import psutil
from PySide6.QtCore import QObject, QTimer, Signal

from .camera_controls import directshow_auto_exposure, sync_stock_auto_exposure
from .config import resource_path
from .external_process import OwnedProcessJob, child_environment as stock_child_environment, launch_external
from .models import Shot


LOG = logging.getLogger(__name__)
NATIVE_EXCEPTION = re.compile(r"^(?:[\w.]+(?:error|exception)|error|exception):", re.IGNORECASE)
BALL_COLORS = ("yellow", "yellow2", "white", "white2", "white3", "orange", "orange2",
               "orange3", "orange4", "red", "red2", "green", "green2")
SETUP_INSTRUCTIONS = (
    "Position the camera beside and above the putting area. Align the white line "
    "with a straight putt, then place the ball in the yellow rectangle. The red "
    "circle must fit the ball. Press A for the original camera/zone settings; "
    "press D for color tuning. Putt across and out of the red gate. Press Q to close."
)
def launch_stock_tracker(command: list[str], work_dir: Path, *, stdin=subprocess.DEVNULL) -> subprocess.Popen:
    # The stock tracker uses its OpenCV window for keyboard input. It must never
    # inherit the desktop's private JSON command pipe from the engine.
    return launch_external(command, work_dir, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, stdin=stdin)


def parse_putt(payload: dict, *, captured_at: float | None = None) -> Shot:
    """Read the exact stock tracker schema; preserve its mph, spin and HLA."""
    if not isinstance(payload, dict) or not isinstance(payload.get("ballData"), dict):
        raise ValueError("Expected Springbok ballData")
    data = payload["ballData"]
    values = []
    for key in ("BallSpeed", "TotalSpin", "LaunchDirection"):
        value = data.get(key)
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise ValueError(f"Invalid {key}")
        try:
            value = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"Invalid {key}") from exc
        if not math.isfinite(value):
            raise ValueError(f"Invalid {key}")
        values.append(value)
    shot = Shot(source="webcam", speed_mph=values[0], spin_rpm=values[1], hla=values[2],
                captured_at=time.monotonic() if captured_at is None else captured_at,
                raw={"tracker": "springbok-stock", "ballData": dict(data)})
    shot.validate()
    return shot


class PuttingServer:
    """Bounded loopback-only compatibility endpoint for the unmodified tracker."""
    def __init__(self, callback, port: int = 8888):
        self.callback = callback
        self.port = port
        self._server = None
        self._thread = None

    def start(self) -> None:
        if self._server:
            return
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                self.connection.settimeout(2)

            def do_POST(self):
                received_at = time.monotonic()
                if self.path not in {"/putting", "/"}:
                    self.reply(404, "Unknown endpoint")
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 8192:
                        self.reply(413, "Invalid payload size")
                        return
                    payload = json.loads(self.rfile.read(length))
                    shot = parse_putt(payload, captured_at=received_at)
                    owner.callback(shot)
                    self.reply(200, "Success")
                except (ValueError, TypeError, UnicodeDecodeError):
                    self.reply(400, "Invalid putt data")
                except (TimeoutError, OSError):
                    self.close_connection = True

            def reply(self, code: int, result: str):
                body = json.dumps({"result": result}).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        class Server(ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = False

        self._server = Server(("127.0.0.1", self.port), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        kwargs={"poll_interval": 0.1}, daemon=True,
                                        name="SpringbokPuttReceiver")
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            if self._thread:
                self._thread.join(timeout=2)
            self._server = None
            self._thread = None


def _device_key(value: str) -> str:
    return value.casefold().replace("\\\\?\\", "").replace("\\??\\", "")


def camera_devices(backend: int = cv2.CAP_DSHOW) -> list[dict]:
    """Enumerate the same backend used by stock OpenCV, without opening cameras."""
    result = []
    for device in enumerate_cameras(backend):
        identity = device.path or f"name:{device.name}"
        result.append({"id": identity, "name": device.name, "index": device.index,
                       "backend": backend, "stable": bool(device.path)})
    return result


def native_camera_backend(config_path: str | Path) -> int:
    """Match stock OpenCV enumeration even before an adapter has been started."""
    parser = ConfigParser()
    try:
        parser.read(config_path, encoding="utf-8")
        return cv2.CAP_DSHOW if parser.getint("putting", "mjpeg", fallback=1) else cv2.CAP_MSMF
    except (ConfigError, OSError, ValueError, UnicodeError):
        return cv2.CAP_DSHOW


def resolve_camera(config: dict, devices: list[dict]) -> dict:
    identity = config.get("camera_id", "")
    if identity:
        matches = [device for device in devices if _device_key(device["id"]) == _device_key(identity)]
    else:
        index = int(config.get("camera_index", 0))
        matches = [device for device in devices if device["index"] == index]
    if len(matches) != 1:
        raise ValueError("Saved putting camera is unavailable or ambiguous. Reconnect it or select the camera again.")
    return matches[0]


def validate_native_config(path: Path) -> tuple[bool, str]:
    """Check file integrity only; an actual practice putt validates the setup."""
    parser = ConfigParser()
    try:
        if not parser.read(path, encoding="utf-8") or not parser.has_section("putting"):
            return False, "Original putting settings have not been created"
        values = {key: parser.getint("putting", key) for key in ("startx1", "startx2", "y1", "y2", "radius", "mjpeg")}
        if not (0 <= values["startx1"] < values["startx2"] <= 16384
                and 0 <= values["y1"] < values["y2"] <= 16384
                and 0 <= values["radius"] <= 1000 and values["mjpeg"] in {0, 1}):
            return False, "Putting settings contain an invalid detection area"
        for key in ("flip", "mjpeg", "ps4", "replaycam", "replaycamps4"):
            if parser.has_option("putting", key) and parser.getint("putting", key) not in {0, 1}:
                return False, f"Original putting setting {key} must be 0 or 1"
        for key, low, high in (("flipview", -1, 1), ("darkness", 0, 255), ("fps", 0, 1000),
                               ("height", 0, 16384), ("width", 0, 16384), ("replaycamindex", 0, 100)):
            if parser.has_option("putting", key) and not low <= parser.getint("putting", key) <= high:
                return False, f"Original putting setting {key} is outside its supported range"
        for key in ("saturation", "exposure", "autowb", "whiteBalanceBlue", "whiteBalanceRed", "brightness",
                    "contrast", "hue", "gain", "monochrome", "sharpness", "autoexposure", "gamma", "zoom", "focus", "autofocus"):
            if parser.has_option("putting", key) and not math.isfinite(parser.getfloat("putting", key)):
                return False, f"Original camera setting {key} must be a finite number"
        if parser.has_option("putting", "customhsv"):
            colors = ast.literal_eval(parser.get("putting", "customhsv"))
            if not isinstance(colors, dict) or (colors and not all(
                    type(colors.get(key)) is int and 0 <= colors[key] <= high
                    for key, high in (("hmin", 179), ("hmax", 179), ("smin", 255), ("smax", 255), ("vmin", 255), ("vmax", 255)))):
                return False, "Original custom ball color must contain six valid HSV values"
    except (ConfigError, OSError, ValueError, SyntaxError, UnicodeError):
        return False, "Original putting settings could not be read"
    return True, "Original putting settings loaded; confirm with a practice putt"


def native_view_health(rgb: np.ndarray) -> tuple[bool, str]:
    """Recognize the stock error banner; never infer health from a process alone.

    This is read-only status inspection of the original app, not ball tracking.
    Its source draws this exact banner on error.png when capture returns no frame.
    """
    if rgb.ndim != 3 or rgb.shape[0] < 100 or rgb.shape[1] < 100:
        return False, "Putting view is not available yet"
    normalized = cv2.resize(rgb, (640, round(rgb.shape[0] * 640 / rgb.shape[1])))
    template = np.full((30, 190, 3), 255, dtype=np.uint8)
    cv2.putText(template, "Error: No Frame", (20, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 0, 0))
    region = normalized[:40, :220]
    score = cv2.matchTemplate(region, template, cv2.TM_CCOEFF_NORMED).max()
    if score > 0.90:
        return False, "Springbok cannot read the webcam. Close other camera apps, reconnect it, or change MJPEG in Advanced Settings."
    # The startup splash has no scene/overlays below its text. Do not call it ready.
    if float(normalized[120:-10].std()) < 1.0:
        return False, "Waiting for the original putting camera view"
    return True, "Original putting camera view is available"


class PuttingAdapter(QObject):
    shot = Signal(object)
    preview_shot = Signal(object)
    status = Signal(str, str)
    devices_changed = Signal()
    _native_error = Signal(int, str)

    def __init__(self, config: dict, work_dir: str | Path, parent=None, *, callback_port: int = 8888):
        super().__init__(parent)
        self.config = dict(config)
        self.work_dir = Path(work_dir)
        self.vendor_path = resource_path("vendor/springbok-putting")
        self._process: subprocess.Popen | None = None
        self._process_job: OwnedProcessJob | None = None
        self._server = PuttingServer(self._receive_shot, port=callback_port)
        self._active = False
        self._calibration = False
        self._wanted = False
        self._devices_cache = []
        self._recent = deque(maxlen=8)
        self._lock = threading.Lock()
        self._last_status = None
        self._next_retry = 0.0
        self._show_requested = False
        self._settings_requested = False
        self._window_state = None
        self._fault = ""
        self._view_available = False
        self._view_message = "Waiting for the original putting camera view"
        self._output = deque(maxlen=80)
        self._native_error.connect(self._on_native_error)
        self._timer = QTimer(self)
        self._timer.setInterval(1500)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    @property
    def running(self) -> bool:
        process = self._process
        return process is not None and process.poll() is None

    @property
    def owned_pids(self) -> set[int]:
        if not self.running:
            return set()
        try:
            return {self._process.pid, *(child.pid for child in psutil.Process(self._process.pid).children(recursive=True))}
        except psutil.Error:
            return {self._process.pid}

    @property
    def healthy(self) -> bool:
        return (self.running and self._view_available and not self._fault
                and bool(self.config.get("configured", self.config.get("validated", False))))

    @property
    def last_output(self) -> str:
        return "\n".join(self._output)

    @property
    def config_path(self) -> Path:
        return self.work_dir / "config.ini"

    def _report(self, level: str, message: str) -> None:
        value = (level, message)
        if value != self._last_status:
            self._last_status = value
            self.status.emit(level, message)

    def _backend(self) -> int:
        return native_camera_backend(self.config_path)

    def devices(self) -> list[dict]:
        return camera_devices(self._backend())

    def update_config(self, config: dict) -> None:
        was_running = self.running
        calibration = self._calibration
        if was_running:
            self.stop()
        self.config = dict(config)
        if was_running:
            self.start(calibration=calibration)

    def import_native_config(self, source: str | Path) -> None:
        """Import a user-selected working Springbok config without changing values."""
        source = Path(source)
        valid, message = validate_native_config(source)
        if not valid:
            raise ValueError(message)
        was_running, calibration = self.running, self._calibration
        if was_running:
            self.stop()
        self.work_dir.mkdir(parents=True, exist_ok=True)
        if source.resolve() != self.config_path.resolve():
            if self.config_path.exists():
                shutil.copy2(self.config_path, self.work_dir / "config.before-import.ini")
            temporary = self.work_dir / "config.importing.ini"
            shutil.copy2(source, temporary)
            os.replace(temporary, self.config_path)
        if was_running:
            self.start(calibration=calibration)

    def build_command(self, device: dict, *, color_calibration: bool = False) -> list[str]:
        color = self.config.get("ball_color", "yellow")
        if color not in BALL_COLORS:
            raise ValueError("Select one of the original putting ball color presets")
        width = int(self.config.get("width", 640))
        if not 320 <= width <= 1920:
            raise ValueError("Putting view width must be between 320 and 1920 pixels")
        return [str(self.vendor_path / "ball_tracking.exe"), "-c",
                "calibrate" if color_calibration else color, "-w", str(device["index"]), "-r", str(width)]

    def start(self, calibration: bool = False) -> bool:
        self._wanted = True
        self._calibration = bool(calibration)
        if calibration:
            self.set_active(False)
            self._show_requested = True
        if self.running:
            self._apply_window_state()
            return True
        self._stop_owned_process()  # Release any descendants of a crashed bootloader.
        self._next_retry = time.monotonic() + 8
        try:
            if not (self.vendor_path / "ball_tracking.exe").is_file():
                raise ValueError("The bundled Springbok putting tracker is missing; repair the app installation")
            self.work_dir.mkdir(parents=True, exist_ok=True)
            if not self.config_path.exists():
                shutil.copy2(self.vendor_path / "config-defaults.ini", self.config_path)
            # The stock binary reads its splash image and replay paths from cwd.
            for filename in ("error.png", "Replay1.mp4"):
                destination = self.work_dir / filename
                source = self.vendor_path / filename
                if not destination.exists() and source.exists():
                    shutil.copy2(source, destination)
            for directory in ("replay1", "replay2"):
                (self.work_dir / directory).mkdir(exist_ok=True)
            valid, message = validate_native_config(self.config_path)
            if not valid:
                raise ValueError(message)
            device = resolve_camera(self.config, self.devices())
            self.config["camera_id"] = device["id"]
            command = self.build_command(device)
            self._keep_exposure_mode(device)
            # Bind before launching: port conflicts must not start a second tracker.
            self._server.start()
            with self._lock:
                self._recent.clear()
            self._fault = ""
            self._view_available = False
            self._output.clear()
            self._process = launch_stock_tracker(command, self.work_dir)
            self._process_job = OwnedProcessJob.try_attach(self._process)
            threading.Thread(target=self._read_output, args=(self._process,), daemon=True,
                             name="SpringbokTrackerOutput").start()
            self._window_state = None
            self._report("working", f"Opening {device['name']} in the original Springbok putting view…")
            return True
        except OSError as exc:
            self._server.stop()
            if getattr(exc, "winerror", 0) == 10048 or getattr(exc, "errno", 0) in {48, 98, 10048}:
                self._report("error", "Putting port 8888 is in use. Close the original connector or other putting apps.")
            else:
                self._report("error", f"Could not open Springbok putting: {exc}")
        except (ValueError, ConfigError) as exc:
            self._report("warning", str(exc))
        return False

    def _keep_exposure_mode(self, device: dict) -> None:
        # Stock DirectShow startup replays a saved autoexposure of -1, which turns
        # the camera's auto exposure off every launch. Carry the camera's own mode.
        if device.get("backend") != cv2.CAP_DSHOW or not device.get("stable"):
            return
        key = _device_key(device["id"])
        auto = directshow_auto_exposure(lambda path: _device_key(path) == key)
        if auto is None:
            return
        try:
            if sync_stock_auto_exposure(self.config_path, auto):
                LOG.info("Putting camera exposure stays %s at launch", "automatic" if auto else "manual")
        except OSError as exc:
            LOG.warning("Could not keep the putting camera's exposure mode: %s", exc)

    def _read_output(self, process: subprocess.Popen) -> None:
        if process.stdout is None:
            return
        trace, summary = [], []
        trace_truncated = False
        last_logged = None
        last_error = None

        def report(message):
            nonlocal last_error
            if message != last_error:
                last_error = message
                self._native_error.emit(process.pid, message)

        def flush_trace():
            nonlocal trace, summary, trace_truncated, last_logged
            if not trace:
                return
            detail = "\n".join(trace)
            if trace_truncated:
                detail += "\n[Additional traceback lines omitted]"
            message = " ".join(summary)[:2000] if summary else "Original putting tracker reported an incomplete traceback. See the diagnostic log."
            if summary and summary[0] not in detail:
                detail += "\n" + message
            if detail != last_logged:
                LOG.error("Springbok putting traceback (process %s):\n%s", process.pid, detail)
                last_logged = detail
            report(message)
            trace, summary, trace_truncated = [], [], False

        try:
            for data in iter(process.stdout.readline, b""):
                line = data.decode("utf-8", errors="replace").rstrip("\r\n")
                stripped = line.strip()
                if process is not self._process:
                    continue
                self._output.append(line)
                if "Traceback (most recent call last)" in line:
                    flush_trace()
                    trace = [line[:2000]]
                    report("Original putting tracker reported an error; reading its details")
                    continue
                terminal = bool(NATIVE_EXCEPTION.match(stripped))
                # OpenCV includes useful argument/driver details on following
                # indented or '>' lines. Keep those with the final exception.
                continuation = not stripped or line[:1].isspace() or line.startswith(">")
                if trace and summary and not continuation:
                    flush_trace()
                if trace:
                    if len(trace) < 40:
                        trace.append(line[:2000])
                    else:
                        trace_truncated = True
                    if terminal:
                        summary = [stripped[:2000]]
                        report(summary[0])
                    elif summary and stripped and len(summary) < 8:
                        summary.append(stripped[:500])
                    continue
                if "No Camera could be opened" in line or terminal or "Error:" in line or "Exception:" in line:
                    if stripped != last_error:
                        LOG.error("Springbok putting: %s", stripped[:2000])
                    report(stripped[:2000])
        finally:
            flush_trace()
            process.stdout.close()

    def _on_native_error(self, process_id: int, message: str) -> None:
        if self._process is None or self._process.pid != process_id:
            return
        self._fault = message
        self._report("error", f"Springbok putting: {message}")

    def stop(self) -> None:
        self._wanted = False
        self.set_active(False)
        self._stop_owned_process()
        self._server.stop()
        self._report("warning", "Putting tracker stopped")

    def _stop_owned_process(self) -> None:
        process, self._process = self._process, None
        job, self._process_job = self._process_job, None
        if job is not None:
            # Also covers the one-file child's descendants if its bootloader
            # has already exited. Never attaches GSPro, FS Golf, or their PIDs.
            job.close()
        if process is not None and process.poll() is None:
            # The one-file PyInstaller executable has an owned child process.
            try:
                children = psutil.Process(process.pid).children(recursive=True)
                for child in children:
                    child.terminate()
                process.terminate()
                process.wait(timeout=2)
                _, alive = psutil.wait_procs(children, timeout=1)
                for child in alive:
                    child.kill()
            except (psutil.Error, subprocess.TimeoutExpired, OSError):
                try:
                    process.kill()
                except OSError:
                    pass
        self._window_state = None
        self._view_available = False

    def set_active(self, active: bool) -> None:
        with self._lock:
            self._active = bool(active) and not self._calibration
        if active:
            self._show_requested = False
        self._apply_window_state()

    def _receive_shot(self, shot: Shot) -> None:
        if not self.running or self._fault:
            return
        fingerprint = (shot.speed_mph, shot.hla, shot.spin_rpm)
        with self._lock:
            if any(fp == fingerprint and shot.captured_at - when < 0.35 for when, fp in self._recent):
                return  # Retry of a callback; the stock tracker has no event identifier.
            self._recent.append((shot.captured_at, fingerprint))
            active, calibration = self._active, self._calibration
        if calibration:
            self.preview_shot.emit(shot)
        elif active:
            self.shot.emit(shot)

    def show_preview(self) -> None:
        self._show_requested = True
        if not self.running:
            self.start(calibration=True)
        self._apply_window_state(focus=True)

    def open_settings(self) -> None:
        self._settings_requested = True
        self.show_preview()
        self._send_settings_key()

    def _send_settings_key(self) -> None:
        if not self._settings_requested:
            return
        for hwnd in self._putting_windows():
            if sys.platform == "win32":
                user32 = ctypes.windll.user32
                user32.PostMessageW.argtypes = [wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM]
                user32.PostMessageW(hwnd, 0x0102, ord("a"), 0)  # WM_CHAR (cv2.waitKey)
                self._settings_requested = False

    def _putting_windows(self) -> list[int]:
        if sys.platform != "win32" or not self.running:
            return []
        pids = self.owned_pids
        user32 = ctypes.windll.user32
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        handles = []
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        @callback_type
        def callback(hwnd, _):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in pids:
                title = ctypes.create_unicode_buffer(512)
                user32.GetWindowTextW(hwnd, title, 512)
                if title.value.startswith("Putting View"):
                    handles.append(hwnd)
            return True

        user32.EnumWindows(callback, 0)
        return handles

    def _apply_window_state(self, focus: bool = False) -> None:
        visible = self._calibration or self._show_requested or (self._active and self.config.get("show_while_putting", True))
        state = (visible, self._active)
        if sys.platform != "win32" or (state == self._window_state and not focus):
            return
        handles = self._putting_windows()
        if not handles:
            return
        user32 = ctypes.windll.user32
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        for hwnd in handles:
            user32.ShowWindow(hwnd, 4)  # Show without focus; keep native rendering alive.
            user32.SetWindowPos(hwnd, -1 if self._active and visible else -2, 0, 0, 0, 0, 0x0013)
            if not visible:
                user32.SetWindowPos(hwnd, 1, 0, 0, 0, 0, 0x0013)  # HWND_BOTTOM, same Springbok send-to-back behavior.
            if focus:
                user32.SetForegroundWindow(hwnd)
        self._window_state = state

    def _check_native_view(self) -> None:
        from .window_capture import CaptureError, WindowCapture, WindowInfo
        windows = self._putting_windows()
        if not windows:
            self._view_available = False
            self._view_message = "Waiting for the original putting window"
            return
        try:
            frame = WindowCapture(WindowInfo(windows[0], self._process.pid, "Putting View")).capture()
            self._view_available, self._view_message = native_view_health(frame)
        except (CaptureError, OSError, ValueError):
            self._view_available = False
            self._view_message = "Restore the original putting view to check the camera"

    def _tick(self) -> None:
        try:
            devices = self.devices()
            if devices != self._devices_cache:
                self._devices_cache = devices
                self.devices_changed.emit()
            if not self._wanted:
                return
            try:
                resolve_camera(self.config, devices)
            except ValueError:
                if self.running:
                    self._stop_owned_process()
                self._report("warning", "Putting camera disconnected. Waiting for the saved camera to return…")
                return
            if not self.running:
                self._report("warning", "Putting tracker closed; reopening the saved camera…")
                if time.monotonic() >= self._next_retry:
                    self.start(calibration=self._calibration)
                return
            self._apply_window_state()
            self._send_settings_key()
            self._check_native_view()
            if self._fault:
                self._report("error", f"Springbok putting: {self._fault}")
            elif not self._view_available:
                self._report("warning", self._view_message)
            elif self._calibration:
                self._report("working", "Springbok putting setup is open. Make a practice putt across the red gate.")
            elif self.healthy:
                self._report("ready", "Original Springbok putting is running" + (" · putter selected" if self._active else " · waiting for putter"))
            else:
                self._report("warning", "Original putting view is running. Verify a practice putt in setup.")
        except (OSError, ValueError, RuntimeError) as exc:
            self._report("warning", f"Could not check putting camera: {exc}")
