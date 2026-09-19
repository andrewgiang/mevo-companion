"""Versioned, atomic per-user settings with recoverable backups."""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import sys


def resource_path(name: str) -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent)) / name


def default_data_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local/share")) / "MevoCompanion"


DEFAULTS = {
    "schema_version": 1,
    "setup_complete": False,
    "auto_connect": True,
    "auto_chipping": True,
    "start_with_windows": False,
    "adapter": "ocr",
    "gspro_path": "",
    "fs_golf_path": "",
    "gspro_host": "127.0.0.1",
    "gspro_port": 921,
    "camera_id": "",
    "camera_name": "",
    "calibration": None,
    "putting": {"camera_index": 0, "ball_color": "yellow", "width": 640,
                "configured": False, "show_while_putting": True},
    "validation": {"swing": False, "chip": False, "putt": False, "confirmed": False},
    "ocr": {"mode": "automatic", "speed_unit": "auto", "stable_frames": 3},
    "mevo": {"mevo_address": "192.168.2.1:5100", "mevo_range_ft": 8.0,
             "tee_height_in": 1.5, "surface_height_in": 0.0, "ball_type": 0,
             "camera_mode": "standard", "use_estimated": False},
    "ui": {"last_page": "play"},
}


class ConfigStore:
    def __init__(self, directory: str | Path | None = None):
        self.directory = Path(directory) if directory else default_data_dir()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "settings.json"
        self.data = deepcopy(DEFAULTS)
        self.warning = ""
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            stored = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(stored, dict) or stored.get("schema_version", 1) != 1:
                raise ValueError("Unsupported settings version")
            for key, value in stored.items():
                if key in self.data:
                    if isinstance(value, dict) and isinstance(self.data[key], dict):
                        self.data[key].update(value)
                    else:
                        self.data[key] = value
        except (ValueError, OSError) as exc:
            self.warning = f"Settings could not be read. Your original file was preserved: {exc}"
            damaged = self.directory / "settings.unreadable.json"
            if not damaged.exists():
                shutil.copy2(self.path, damaged)

    def save(self) -> None:
        payload = json.dumps(self.data, indent=2, ensure_ascii=False, allow_nan=False)
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if self.path.exists():
            shutil.copy2(self.path, self.path.with_suffix(".previous.json"))
        os.replace(temporary, self.path)

    def set(self, key: str, value) -> None:
        self.data[key] = value
        self.save()

    def reset_validation(self) -> None:
        self.data["validation"] = deepcopy(DEFAULTS["validation"])
        self.data["setup_complete"] = False
        self.save()
