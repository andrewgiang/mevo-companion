"""Record native Play Mode observations; this tool has no GSPro connection."""
import json
from pathlib import Path
import queue
import subprocess
import threading
import time

root = Path(__file__).resolve().parents[1]
process = subprocess.Popen([str(root / 'artifacts/play-mode-reader/FsGolfReader.exe'), '--watch'],
                           stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           encoding='utf-8', creationflags=subprocess.CREATE_NO_WINDOW)
messages = queue.Queue()
def read():
    for line in process.stdout:
        try: messages.put(json.loads(line))
        except ValueError: pass
threading.Thread(target=read, daemon=True).start()
frames, changes, previous = [], [], None
deadline = time.monotonic() + 40
try:
    while time.monotonic() < deadline:
        try: frame = messages.get(timeout=.2)
        except queue.Empty: continue
        frames.append(frame)
        key = (frame.get('shot_mode'), frame.get('radar_status'), frame.get('total_shots'))
        if key != previous:
            change = {k: frame.get(k) for k in ('timestamp', 'shot_mode', 'radar_status', 'selected_shot', 'total_shots', 'context_stable', 'readings')}
            changes.append(change)
            print(json.dumps(change), flush=True)
            previous = key
finally:
    process.stdin.close()
    try: process.wait(timeout=3)
    except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=3)
    destination = root / 'artifacts/fs-play-physical-trace.json'
    destination.write_text(json.dumps({'shot_delivery': False, 'changes': changes, 'frames': frames}, indent=2), encoding='utf-8')
    print(json.dumps({'frames': len(frames), 'exit_code': process.returncode, 'report': str(destination)}), flush=True)
