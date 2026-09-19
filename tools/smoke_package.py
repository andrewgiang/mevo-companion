"""Exercise the packaged desktop/worker without forwarding a golf shot.

Hardware mode enumerates the selected camera, opens the original putting preview,
and reads FS Golf. It uses an isolated profile with live delivery disabled.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import queue
import subprocess
import threading
import time


ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--package', type=Path, default=ROOT / 'dist/MevoCompanion')
    parser.add_argument('--hardware', action='store_true')
    options = parser.parse_args()
    mode = 'hardware' if options.hardware else 'demo'
    directory = ROOT / 'artifacts' / ('packaged-' + mode)
    directory.mkdir(parents=True, exist_ok=True)
    command = [str(options.package / 'engine/MevoCompanionEngine.exe'), '--data-dir', str(directory / 'profile')]
    if not options.hardware:
        command.append('--demo')
    error_path = directory / 'stderr.log'
    error_stream = error_path.open('w', encoding='utf-8')
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=error_stream, encoding='utf-8',
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    messages = queue.Queue()
    def read():
        for line in process.stdout:
            try:
                messages.put(json.loads(line))
            except ValueError:
                pass
    threading.Thread(target=read, daemon=True).start()
    states, responses, configs = [], {}, []
    def send(command, **args):
        process.stdin.write(json.dumps({'id': command, 'command': command, 'args': args}) + '\n')
        process.stdin.flush()
    def collect(seconds, until=None):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                message = messages.get(timeout=.1)
            except queue.Empty:
                if process.poll() is not None:
                    break
                continue
            if message.get('type') == 'state':
                states.append(message['data'])
            elif message.get('type') == 'config':
                configs.append(message['data'])
            elif message.get('type') == 'response':
                responses[message['id']] = message
            if until and until():
                return
    report = {'hardware_mode': options.hardware, 'live_shot_delivery': False}
    try:
        send('status')
        collect(10, lambda: 'status' in responses)
        if not responses.get('status', {}).get('ok'):
            raise RuntimeError('Packaged worker did not answer status')
        send('save_settings', settings={'auto_connect': False, 'start_with_windows': False})
        collect(5, lambda: 'save_settings' in responses)
        send('camera_devices')
        collect(10, lambda: 'camera_devices' in responses)
        devices = responses.get('camera_devices', {})
        report['camera_enumeration_ok'] = devices.get('ok', False)
        report['camera_enumeration_error'] = devices.get('error', 'No response within 10 seconds') if not devices.get('ok') else None
        report['camera_count'] = len(devices.get('result') or [])
        send('connect', live=False, setup=True)
        collect(18 if options.hardware else 2,
                lambda: bool(states and states[-1]['health']['mevo']['state'] == 'ready'
                             and 'setup is open' in states[-1]['health']['putting']['message']))
        if any(state.get('live_enabled') for state in states):
            raise RuntimeError('Smoke session unexpectedly enabled shot delivery')
        report['health'] = states[-1]['health'] if states else {}
        report['shot_count'] = sum(len(state.get('shots', [])) for state in states)
        report['setup_complete'] = configs[-1].get('setup_complete') if configs else None
        report['worker_ok'] = True
        send('stop')
        collect(6, lambda: 'stop' in responses)
    finally:
        if process.poll() is None:
            send('shutdown')
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        report['exit_code'] = process.returncode
        error_stream.close()
        report['stderr'] = error_path.read_text(encoding='utf-8', errors='replace')[-2000:]
        (directory / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
