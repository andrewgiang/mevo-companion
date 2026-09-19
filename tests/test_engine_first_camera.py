"""Exercise the real startup/import order with the command pipe held open."""
import json
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

import pytest


@pytest.mark.skipif(sys.platform != "win32", reason="Actual Windows camera-name enumeration")
def test_first_camera_command_responds_without_another_input_line_or_eof(tmp_path):
    process = subprocess.Popen(
        [sys.executable, "MevoCompanionEngine.py", "--data-dir", str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    messages, errors = queue.Queue(), []

    def read_output():
        for line in process.stdout:
            messages.put(json.loads(line))

    def read_errors():
        errors.extend(process.stderr.readlines())

    readers = [threading.Thread(target=read_output, daemon=True),
               threading.Thread(target=read_errors, daemon=True)]
    for reader in readers:
        reader.start()
    try:
        # Enumeration lists device names only: no VideoCapture or putting tracker
        # starts, and no launch-monitor or GSPro session is requested.
        process.stdin.write(json.dumps({"id": "first-camera", "command": "camera_devices"}) + "\n")
        process.stdin.flush()
        deadline = time.monotonic() + 8
        response = None
        while time.monotonic() < deadline:
            try:
                value = messages.get(timeout=max(.01, deadline - time.monotonic()))
            except queue.Empty:
                break
            if value.get("type") == "response" and value.get("id") == "first-camera":
                response = value
                break
        assert response is not None, "First camera command blocked with stdin open: " + "".join(errors)
        assert response["ok"], response
        assert isinstance(response["result"], list)  # Zero cameras is also valid.
        assert process.poll() is None  # No EOF or second command unlocked import.
        process.stdin.write(json.dumps({"id": "quit", "command": "shutdown"}) + "\n")
        process.stdin.flush()
        assert process.wait(timeout=5) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        for reader in readers:
            reader.join(timeout=1)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()
