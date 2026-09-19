"""Automatic Chipping is on for new/older profiles; manual choice survives reload."""
import json

from companion.config import ConfigStore


def test_new_profile_enables_automatic_chipping(tmp_path):
    assert ConfigStore(tmp_path).data["auto_chipping"] is True


def test_existing_profile_gets_automatic_chipping_without_losing_setup(tmp_path):
    (tmp_path / "settings.json").write_text(json.dumps({
        "schema_version": 1, "setup_complete": True, "auto_connect": False,
        "putting": {"camera_id": "known-camera", "configured": True},
    }), encoding="utf-8")
    data = ConfigStore(tmp_path).data
    assert data["auto_chipping"] is True
    assert data["setup_complete"] is True
    assert data["auto_connect"] is False
    assert data["putting"]["camera_id"] == "known-camera"
    assert data["putting"]["configured"] is True


def test_manual_mode_preference_survives_unrelated_save_and_reload(tmp_path):
    store = ConfigStore(tmp_path)
    store.set("auto_chipping", False)
    reloaded = ConfigStore(tmp_path)
    reloaded.set("auto_connect", False)
    assert ConfigStore(tmp_path).data["auto_chipping"] is False
