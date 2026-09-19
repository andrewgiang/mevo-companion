"""Observe real Play Mode shots with the production gate, without GSPro delivery."""
from dataclasses import asdict
import json
from pathlib import Path
import sys
import time

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from companion.accessibility_adapter import AccessibilityAdapter

states, shots = [], []

def status(state, message):
    item = {"state": state, "message": message}
    states.append(item)
    print(json.dumps(item), flush=True)

def shot(value):
    item = asdict(value)
    shots.append(item)
    print(json.dumps({"measured": item}), flush=True)

adapter = AccessibilityAdapter({"helper_path": str(root / "artifacts/play-mode-reader/FsGolfReader.exe")}, shot, status)
adapter.start()
try:
    time.sleep(45)
finally:
    adapter.stop()
    report = {"shot_delivery": False, "shots": shots, "states": states}
    path = root / "artifacts/play-mode-live-gate.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"measured_shots": len(shots), "report": str(path)}), flush=True)
