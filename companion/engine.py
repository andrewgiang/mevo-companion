"""Private JSON-lines IPC host for the native desktop application.

No listening control port: only the parent that owns stdin/stdout can issue
commands. Worker diagnostics go to a rotating local file, never the protocol.
"""
from __future__ import annotations

import argparse
import base64
from copy import deepcopy
import io
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import shutil
import sys
import threading
import time

from PIL import Image, ImageDraw
from PySide6.QtCore import QCoreApplication, QObject, QTimer, Signal, Slot

from companion import __version__
from companion.config import ConfigStore, DEFAULTS
from companion.controller import SessionController

log = logging.getLogger(__name__)


def validate_settings(current: dict, patch: dict) -> dict:
    """Accept UI preferences, never forged test results or hardware readiness."""
    if not isinstance(patch, dict):
        raise ValueError("Settings must be an object")
    updated = deepcopy(current)
    strings = {"gspro_path", "fs_golf_path", "camera_id", "camera_name", "gspro_host"}
    booleans = {"auto_connect", "start_with_windows", "auto_chipping"}
    for key in strings:
        if key in patch:
            if not isinstance(patch[key], str) or len(patch[key]) > 4096:
                raise ValueError(f"Invalid {key}")
            updated[key] = patch[key].strip()
    for key in booleans:
        if key in patch:
            if not isinstance(patch[key], bool):
                raise ValueError(f"Invalid {key}")
            updated[key] = patch[key]
    if "adapter" in patch:
        if patch["adapter"] not in {"ocr", "direct"}:
            raise ValueError("Choose FS Golf or the experimental direct connection")
        updated["adapter"] = patch["adapter"]
    if "gspro_port" in patch:
        value = patch["gspro_port"]
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
            raise ValueError("GSPro port must be between 1 and 65535")
        updated["gspro_port"] = value
    if "putting" in patch:
        from companion.putting_adapter import BALL_COLORS
        putting = patch["putting"]
        if not isinstance(putting, dict):
            raise ValueError("Invalid putting settings")
        for key, low, high in (("camera_index", 0, 100), ("width", 320, 1920)):
            if key in putting:
                value = putting[key]
                if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                    raise ValueError(f"Invalid {key}")
                updated["putting"][key] = value
        if "camera_id" in putting:
            if not isinstance(putting["camera_id"], str) or len(putting["camera_id"]) > 4096:
                raise ValueError("Invalid camera identity")
            updated["putting"]["camera_id"] = putting["camera_id"]
        if "ball_color" in putting:
            if putting["ball_color"] not in BALL_COLORS:
                raise ValueError("Choose an original Springbok ball-color preset")
            updated["putting"]["ball_color"] = putting["ball_color"]
        if "show_while_putting" in putting:
            if not isinstance(putting["show_while_putting"], bool):
                raise ValueError("Invalid putting preview preference")
            updated["putting"]["show_while_putting"] = putting["show_while_putting"]
    if "ocr" in patch:
        ocr = patch["ocr"]
        if not isinstance(ocr, dict):
            raise ValueError("Invalid screen-reading settings")
        if "speed_unit" in ocr:
            if ocr["speed_unit"] not in {"auto", "mph", "km/h", "m/s"}:
                raise ValueError("Choose the speed unit displayed in FS Golf")
            updated["ocr"]["speed_unit"] = ocr["speed_unit"]
        if "mode" in ocr:
            if ocr["mode"] not in {"automatic", "legacy"}:
                raise ValueError("Invalid reading mode")
            updated["ocr"]["mode"] = ocr["mode"]
    if "mevo" in patch:
        mevo = patch["mevo"]
        if not isinstance(mevo, dict):
            raise ValueError("Invalid direct connection settings")
        for key, low, high in (("mevo_range_ft", 3, 20), ("tee_height_in", 0, 5), ("surface_height_in", -12, 24)):
            if key in mevo:
                value = mevo[key]
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
                    raise ValueError(f"Invalid {key}")
                updated["mevo"][key] = value
        if "mevo_address" in mevo:
            if not isinstance(mevo["mevo_address"], str) or len(mevo["mevo_address"]) > 255:
                raise ValueError("Invalid Mevo address")
            updated["mevo"]["mevo_address"] = mevo["mevo_address"]
        if "ball_type" in mevo:
            if type(mevo["ball_type"]) is not int or mevo["ball_type"] not in {0, 1}:
                raise ValueError("Invalid ball type")
            updated["mevo"]["ball_type"] = mevo["ball_type"]
    source_changed = any(updated[k] != current[k] for k in ("adapter", "fs_golf_path", "ocr", "mevo"))
    camera_changed = any(updated["putting"].get(k) != current["putting"].get(k)
                         for k in ("camera_id", "camera_index", "ball_color", "width"))
    if source_changed or camera_changed:
        updated["validation"] = deepcopy(DEFAULTS["validation"])
        updated["setup_complete"] = False
    if camera_changed:
        updated["putting"]["configured"] = False
    return updated


class Engine(QObject):
    command_received = Signal(object)
    parent_closed = Signal()

    def __init__(self, store: ConfigStore, demo=False, output=None):
        super().__init__()
        self.store, self.demo = store, demo
        self.output = output or sys.stdout
        self._output_lock = threading.Lock()
        self._preview_enabled = False
        self._last_preview = 0.0
        self._shutting_down = False
        self.controller = SessionController(store, demo=demo)
        self.command_received.connect(self.dispatch)
        self.parent_closed.connect(self.shutdown)
        self.controller.changed.connect(lambda data: self.send_event("state", data=data))
        self.controller.event.connect(lambda message: self.send_event("event", message=message))
        self.controller.preview.connect(self._on_preview)
        self.controller.delivery.connect(lambda *_: self.emit_config())
        self.controller.discovered.connect(lambda *_: QTimer.singleShot(0, self.emit_config))

    def send_event(self, kind: str, **payload):
        message = json.dumps({"type": kind, **payload}, ensure_ascii=False, allow_nan=False, default=str)
        try:
            with self._output_lock:
                self.output.write(message + "\n")
                self.output.flush()
        except (OSError, ValueError):
            if not self._shutting_down:
                self.parent_closed.emit()

    def emit_config(self):
        self.send_event("config", data={**self.store.data, "data_dir": str(self.store.directory), "demo": self.demo})

    def bootstrap(self):
        self.send_event("hello", version=__version__, protocol=1, demo=self.demo)
        self.emit_config()
        self.controller.publish()
        if self.store.warning:
            self.send_event("event", message=self.store.warning)
        self.controller.discover()

    def start_reader(self):
        def reader():
            try:
                for line in sys.stdin:
                    if len(line) > 1_000_000:
                        continue
                    try:
                        message = json.loads(line)
                    except ValueError:
                        self.send_event("response", id=None, ok=False, error="Invalid command JSON")
                        continue
                    self.command_received.emit(message)
            finally:
                self.parent_closed.emit()
        threading.Thread(target=reader, name="Desktop commands", daemon=True).start()

    @Slot(object)
    def dispatch(self, message):
        identifier = message.get("id") if isinstance(message, dict) else None
        log.info("Desktop command: %s", message.get("command") if isinstance(message, dict) else "invalid")
        try:
            if not isinstance(message, dict) or not isinstance(message.get("args", {}), dict):
                raise ValueError("Invalid desktop command")
            result = self.execute(str(message.get("command", "")), message.get("args", {}))
            self.send_event("response", id=identifier, ok=True, result=result)
        except Exception as exc:
            log.exception("Command failed: %s", message.get("command") if isinstance(message, dict) else "invalid")
            self.send_event("response", id=identifier, ok=False, error=str(exc))

    def execute(self, command: str, args: dict):
        c = self.controller
        if command == "status":
            self.emit_config()
            c.publish()
            return c.snapshot()
        if command == "discover":
            c.discover()
            return {"started": True}
        if command == "save_settings":
            before = deepcopy(self.store.data)
            self.store.data = validate_settings(before, args.get("settings", {}))
            self.store.save()
            acquisition_changed = any(before[k] != self.store.data[k] for k in ("adapter", "fs_golf_path", "ocr", "mevo")) or any(
                before["putting"].get(k) != self.store.data["putting"].get(k)
                for k in ("camera_id", "camera_index", "ball_color", "width"))
            if acquisition_changed and c.live_enabled:
                c.pause()
                c.setup_mode = True
            if any(before[k] != self.store.data[k] for k in ("adapter", "gspro_host", "gspro_port", "fs_golf_path", "putting", "ocr", "mevo")):
                c.reconfigure()
            if before.get("auto_chipping", True) != self.store.data.get("auto_chipping", True):
                c.apply_auto_chipping()
            self.emit_config()
            return self.store.data
        if command == "play":
            c.play()
        elif command == "connect":
            c.connect_session(live=bool(args.get("live", False)), setup=bool(args.get("setup", True)))
        elif command == "pause":
            c.pause()
        elif command == "resume":
            if not self.store.data["setup_complete"] and not c.setup_mode:
                raise ValueError("Complete setup or explicitly start practice validation before resuming")
            c.resume()
        elif command == "stop":
            c.stop()
        elif command == "open_fs_golf":
            c.open_fs_golf()
        elif command == "setup_putting":
            c.setup_putting()
        elif command == "putting_settings":
            c.setup_putting()
            self.store.data["putting"]["configured"] = False
            self.store.reset_validation()
            if c.putting:
                c.putting.config["configured"] = False
                c.putting.open_settings()
            self.emit_config()
        elif command == "show_putting":
            if not c.putting:
                c.setup_putting()
            if c.putting:
                c.putting.show_preview()
        elif command == "camera_devices":
            if self.demo:
                devices = [{"name": "USB putting camera (demo)", "id": "demo-camera", "index": 0, "stable": True}]
            else:
                log.info("Loading camera enumeration")
                from companion.putting_adapter import camera_devices, native_camera_backend
                log.info("Enumerating connected cameras")
                devices = c.putting.devices() if c.putting else camera_devices(
                    native_camera_backend(self.store.directory / "putting" / "config.ini"))
                log.info("Found %s cameras", len(devices))
            self.send_event("devices", data=devices)
            return devices
        elif command == "set_chipping":
            c.set_chipping(bool(args.get("enabled", False)))
        elif command == "preview":
            self._preview_enabled = bool(args.get("enabled", False))
        elif command == "finish_setup":
            validation = self.store.data["validation"]
            if not args.get("confirmed"):
                raise ValueError("Confirm the practice shots appeared correctly in GSPro")
            if not self.demo and (not all(validation.get(k) for k in ("swing", "chip", "putt"))
                                  or not self.store.data["putting"].get("configured")):
                raise ValueError("Verify a Springbok practice putt, then a full shot, chip and putt in GSPro before finishing")
            if self.demo:
                raise ValueError("Demo mode cannot validate physical equipment or complete setup")
            validation["confirmed"] = True
            self.store.data["setup_complete"] = True
            self.store.save()
            c.stop()
            self.emit_config()
        elif command == "export_diagnostics":
            destination = Path(str(args.get("path", "")))
            if destination.suffix.lower() != ".zip" or not destination.parent.is_dir():
                raise ValueError("Choose a .zip file in an existing folder")
            return {"path": c.diagnostics(str(destination))}
        elif command == "import_profile":
            return self.import_profile(Path(str(args.get("path", ""))))
        elif command == "shutdown":
            QTimer.singleShot(0, self.shutdown)
            return {"stopping": True}
        else:
            raise ValueError("Unknown desktop command")
        return c.snapshot()

    def import_profile(self, path: Path):
        if not path.is_file():
            raise ValueError("Choose an existing Springbok config.ini or device JSON file")
        if self.demo:
            raise ValueError("Demo mode does not import physical setup profiles")
        self.controller.stop()
        if path.name.lower() == "config.ini":
            from companion.putting_adapter import validate_native_config
            valid, message = validate_native_config(path)
            if not valid:
                raise ValueError(message)
            target = self.store.directory / "putting" / "config.ini"
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                shutil.copy2(target, target.with_suffix(".previous.ini"))
            if path.resolve() != target.resolve():
                shutil.copy2(path, target)
            self.store.data["putting"]["configured"] = False
        elif path.suffix.lower() == ".json":
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            if "webcam" in data:
                original = data["webcam"]
                self.store.data = validate_settings(self.store.data, {"putting": {
                    "camera_id": "",
                    "camera_index": int(original.get("camera", 0)),
                    "ball_color": str(original.get("ball_color", "yellow")),
                    "width": int(original.get("width", 640))}})
                self.store.data["putting"]["configured"] = False
            elif "rois" in data and "window_rect" in data:
                rect = data["window_rect"]
                size = [rect["right"] - rect["left"], rect["bottom"] - rect["top"]]
                mapped = {}
                aliases = {"speed": "speed_mph", "total_spin": "spin_rpm", "spin_axis": "spin_axis",
                           "hla": "hla", "vla": "vla", "club_speed": "club_speed_mph"}
                for old, new in aliases.items():
                    roi = data["rois"].get(old)
                    if isinstance(roi, dict):
                        pos, extent = roi.get("pos"), roi.get("size")
                        if pos and extent:
                            mapped[new] = list(pos) + list(extent)
                    elif isinstance(roi, list) and len(roi) == 4:
                        mapped[new] = roi
                if min(size) <= 0 or not {"speed_mph", "spin_rpm", "spin_axis", "hla", "vla"}.issubset(mapped):
                    raise ValueError("The device profile is missing required regions or its original window size")
                self.store.data["ocr"].update(mode="legacy", speed_unit="mph",
                                              legacy_profile={"source_size": size, "rois": mapped})
                self.store.data["fs_golf_path"] = data.get("window_path", self.store.data["fs_golf_path"])
            else:
                raise ValueError("This file is not a recognized Springbok profile")
        else:
            raise ValueError("Choose config.ini, putting_settings.json or a device JSON profile")
        self.store.reset_validation()
        self.emit_config()
        self.controller.record("Springbok settings imported into a separate profile. Original settings were not changed.")
        return self.store.data

    @Slot(object, object)
    def _on_preview(self, pixels, recognition):
        if not self._preview_enabled or time.monotonic() - self._last_preview < .7:
            return
        self._last_preview = time.monotonic()
        if pixels is None:
            self.send_event("preview", data={"values": recognition.values,
                                            "errors": recognition.errors,
                                            "valid": recognition.valid,
                                            "mode": "accessibility"})
            return
        image = Image.fromarray(pixels)
        drawing = ImageDraw.Draw(image)
        for box in recognition.anchors.values():
            drawing.rectangle(box, outline=(33, 174, 104), width=3)
        image.thumbnail((1000, 650))
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=78)
        self.send_event("preview", data={"image_base64": base64.b64encode(buffer.getvalue()).decode("ascii"),
                                   "values": recognition.values, "errors": recognition.errors,
                                   "valid": recognition.valid})

    @Slot()
    def shutdown(self):
        if self._shutting_down:
            return
        self._shutting_down = True
        self.controller.timer.stop()
        self.controller.stop()
        QCoreApplication.instance().quit()


def main():
    parser = argparse.ArgumentParser(description="Mevo Companion integration engine")
    parser.add_argument("--data-dir")
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--self-check", action="store_true")
    options = parser.parse_args()
    if not options.demo:
        # Native NumPy initialization can stall after the blocking stdin reader
        # starts on Windows. Preload workers so the first camera command does
        # not depend on receiving another protocol line or EOF.
        from companion import putting_adapter, accessibility_adapter  # noqa: F401
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
        sys.stdin.reconfigure(encoding="utf-8")
    store = ConfigStore(options.data_dir)
    handler = RotatingFileHandler(store.directory / "companion.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, handlers=[handler],
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    application = QCoreApplication(sys.argv)
    engine = Engine(store, options.demo)
    if options.self_check:
        engine.bootstrap()
        QTimer.singleShot(500, engine.shutdown)
    else:
        engine.start_reader()
        QTimer.singleShot(0, engine.bootstrap)
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
