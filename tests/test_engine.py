from copy import deepcopy
import io
import json
from pathlib import Path
import subprocess
import sys

import pytest
from PySide6.QtCore import QCoreApplication

from companion.config import ConfigStore, DEFAULTS
from companion.engine import Engine, validate_settings


def test_settings_cannot_forge_completed_hardware_validation():
    current = deepcopy(DEFAULTS)
    updated = validate_settings(current, {"setup_complete": True, "validation": {"confirmed": True},
                                          "putting": {"configured": True}})
    assert not updated["setup_complete"]
    assert not updated["validation"]["confirmed"]
    assert not updated["putting"]["configured"]


def test_camera_change_invalidates_prior_calibration_but_preview_preference_does_not():
    current = deepcopy(DEFAULTS)
    current["setup_complete"] = True
    current["putting"]["configured"] = True
    changed = validate_settings(current, {"putting": {"camera_id": "new-device"}})
    assert not changed["putting"]["configured"]
    assert not changed["setup_complete"]
    style = validate_settings(current, {"putting": {"show_while_putting": False}})
    assert style["putting"]["configured"]


@pytest.mark.parametrize("patch", [{"gspro_port": True}, {"gspro_port": -1}, {"auto_connect": "yes"},
                                    {"putting": {"camera_index": -1}}, {"putting": {"width": 2}},
                                    {"putting": {"ball_color": "anything"}}, {"mevo": {"mevo_range_ft": float("nan")}}])
def test_invalid_settings_rejected(patch):
    with pytest.raises(ValueError):
        validate_settings(deepcopy(DEFAULTS), patch)


def test_atomic_config_keeps_previous_profile(tmp_path):
    store = ConfigStore(tmp_path)
    store.save()
    store.set("gspro_path", "C:/GSPro/GSPro.exe")
    assert json.loads((tmp_path / "settings.previous.json").read_text())["gspro_path"] == ""
    assert ConfigStore(tmp_path).data["gspro_path"] == "C:/GSPro/GSPro.exe"


def test_engine_demo_cannot_complete_hardware_validation(tmp_path):
    app = QCoreApplication.instance() or QCoreApplication([])
    output = io.StringIO()
    engine = Engine(ConfigStore(tmp_path), demo=True, output=output)
    engine.controller.timer.stop()
    engine.dispatch({"id": "test", "command": "finish_setup", "args": {"confirmed": True}})
    response = json.loads(output.getvalue().splitlines()[-1])
    assert response["ok"] is False
    assert "Demo mode" in response["error"]
    assert not engine.store.data["setup_complete"]


def test_private_ipc_dispatches_queued_commands_and_shuts_down(tmp_path):
    commands = [{"id": "status", "command": "status"},
                {"id": "bad", "command": "unknown"},
                {"id": "quit", "command": "shutdown"}]
    result = subprocess.run(
        [sys.executable, "MevoCompanionEngine.py", "--demo", "--data-dir", str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1],
        input="".join(json.dumps(command) + "\n" for command in commands),
        encoding="utf-8", capture_output=True, timeout=15, check=True)
    messages = [json.loads(line) for line in result.stdout.splitlines()]
    responses = {message["id"]: message for message in messages if message["type"] == "response"}
    assert responses["status"]["ok"]
    assert responses["status"]["result"]["demo"]
    assert not responses["bad"]["ok"]
    assert responses["quit"]["ok"]


def test_import_legacy_camera_clears_current_identity(tmp_path):
    app = QCoreApplication.instance() or QCoreApplication([])
    store = ConfigStore(tmp_path / "profile")
    store.data["putting"].update(camera_id="current-usb-device", configured=True)
    source = tmp_path / "putting_settings.json"
    source.write_text(json.dumps({"webcam": {"camera": 2, "width": 640, "ball_color": "yellow"}}))
    engine = Engine(store, output=io.StringIO())
    engine.controller.timer.stop()
    engine.execute("import_profile", {"path": str(source)})
    assert not store.data["putting"]["camera_id"]
    assert store.data["putting"]["camera_index"] == 2
    assert not store.data["putting"]["configured"]
