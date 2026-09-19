from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import queue
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from companion.accessibility_adapter import AccessibilityAdapter, AccessibilityShotGate, recognize_snapshot


READINGS = {
    "speed_mph": {"text": "117.7", "unit": "mph"},
    "club_speed_mph": {"text": "85.1", "unit": "mph"},
    "spin_rpm": {"text": "4720", "unit": "rpm"},
    "spin_axis": {"text": "11.9 L", "unit": "°"},
    "vla": {"text": "13.5", "unit": "°"},
    "hla": {"text": "0.1 R", "unit": "°"},
}
EMPTY_READINGS = {key: {"text": "-", "unit": value["unit"]} for key, value in READINGS.items()}


class Sequence:
    def __init__(self):
        self.gate = AccessibilityShotGate()
        self.now = 100.0
        self.wall = 1_800_000_000.0
        self.epoch = 0
        self.shots = []

    def observe(self, total=3, *, selected=None, step=.35, readings=None, **updates):
        self.now += step
        self.wall += step
        snapshot = {
            "type": "snapshot", "ok": True,
            "timestamp": datetime.fromtimestamp(self.wall, timezone.utc).isoformat(),
            "duration_ms": 0, "window": {"pid": 100, "hwnd": 200, "title": "FS Golf"},
            "live_context": True, "radar_status": "Ready", "counter_stable": True,
            "selected_shot": total if selected is None else selected, "total_shots": total,
            "readings": deepcopy(READINGS if readings is None else readings),
        }
        snapshot.update(updates)
        shot = self.gate.observe(snapshot, now=self.now, wall_time=self.wall, epoch=self.epoch)
        if shot:
            self.shots.append(shot)
        return shot

    def settle(self, total=3, **kwargs):
        for _ in range(4):
            self.observe(total, **kwargs)


def test_actual_native_values_and_direction_are_preserved():
    reading = recognize_snapshot({"readings": READINGS, "total_shots": 3})
    assert reading.valid
    assert reading.values == {"speed_mph": 117.7, "club_speed_mph": 85.1, "spin_rpm": 4720,
                              "spin_axis": -11.9, "vla": 13.5, "hla": .1}


@pytest.mark.parametrize("unit,value,mph", [("km/h", "160.9344", 100), ("m/s", "44.704", 100)])
def test_native_speed_units_are_converted(unit, value, mph):
    readings = deepcopy(READINGS)
    readings["speed_mph"] = {"text": value, "unit": unit}
    result = recognize_snapshot({"readings": readings})
    assert result.valid
    assert result.values["speed_mph"] == pytest.approx(mph)


@pytest.mark.parametrize("change", [{"text": "117,7", "unit": "mph"},
                                    {"text": "nan", "unit": "mph"},
                                    {"text": "117.7", "unit": ""}])
def test_ambiguous_native_value_or_unknown_unit_blocks_shot(change):
    readings = deepcopy(READINGS)
    readings["speed_mph"] = change
    assert not recognize_snapshot({"readings": readings}).valid


def test_explicit_speed_unit_conflict_is_rejected():
    assert not recognize_snapshot({"readings": READINGS}, "km/h").valid


def test_existing_latest_live_shot_is_only_baseline():
    seq = Sequence()
    seq.settle(3)
    seq.settle(3)
    assert seq.shots == []
    assert seq.gate.ready


def test_zero_shot_live_ready_baseline_allows_first_real_shot():
    seq = Sequence()
    seq.observe(0, readings=EMPTY_READINGS)
    assert seq.gate.ready
    seq.settle(1)
    assert len(seq.shots) == 1
    assert seq.shots[0].raw["mode"] == "accessibility"
    assert seq.shots[0].raw["shot_id"] == "1"


def test_observed_ready_with_flight_mode_establishes_empty_live_baseline():
    seq = Sequence()
    seq.observe(0, readings=EMPTY_READINGS, radar_status="Ready · Limited Flight")
    assert seq.gate.ready
    assert seq.gate.baseline == 0
    seq.settle(1, radar_status="Ready · Limited Flight")
    assert len(seq.shots) == 1


def test_empty_count_without_required_metric_layout_is_not_ready():
    seq = Sequence()
    seq.observe(0, readings={})
    assert not seq.gate.ready
    assert seq.gate.state == "needs_attention"


def test_zero_shot_placeholder_layout_requires_a_known_speed_unit():
    seq = Sequence()
    readings = deepcopy(EMPTY_READINGS)
    readings["speed_mph"]["unit"] = ""
    seq.observe(0, readings=readings)
    assert not seq.gate.ready


def test_identical_values_for_two_real_counter_increments_are_two_shots():
    seq = Sequence()
    seq.observe(3)
    seq.settle(4)
    seq.settle(5)
    assert len(seq.shots) == 2
    assert seq.shots[0].event_id != seq.shots[1].event_id


def test_new_count_requires_values_to_stabilize():
    seq = Sequence()
    seq.observe(3)
    seq.observe(4)
    changed = deepcopy(READINGS)
    changed["speed_mph"]["text"] = "122.8"
    seq.observe(4, readings=changed)
    assert seq.shots == []
    seq.observe(4, readings=changed)
    seq.observe(4, readings=changed)
    assert len(seq.shots) == 1
    assert seq.shots[0].speed_mph == 122.8


def test_review_counter_changes_are_never_forwarded():
    seq = Sequence()
    seq.settle(3, live_context=False, radar_status="Sleeping")
    seq.settle(4, live_context=False, radar_status="Sleeping")
    seq.settle(5, live_context=False, radar_status="Sleeping")
    assert seq.shots == []
    assert not seq.gate.ready
    seq.settle(5)
    assert seq.shots == []
    seq.settle(6)
    assert len(seq.shots) == 1


def test_historical_selection_inside_live_session_requires_new_baseline():
    seq = Sequence()
    seq.observe(3)
    seq.observe(4, selected=2)
    seq.settle(4)
    assert seq.shots == []
    seq.settle(5)
    assert len(seq.shots) == 1


def test_transient_mid_scan_counter_update_does_not_swallow_a_real_shot():
    seq = Sequence()
    seq.observe(3)
    seq.observe(4, ok=False, counter_stable=False, error="transient update")
    seq.settle(4)
    assert len(seq.shots) == 1


def test_transient_processing_waits_for_ready_without_resetting_live_baseline():
    seq = Sequence()
    seq.observe(3)
    seq.observe(4, radar_status="Processing")
    seq.settle(4)
    assert len(seq.shots) == 1


@pytest.mark.parametrize("status", ["Sleeping", "Disconnected", "Error"])
def test_unavailable_radar_discards_old_pending_shot(status):
    seq = Sequence()
    seq.observe(3)
    seq.observe(4, radar_status=status)
    seq.settle(4)
    assert seq.shots == []


def test_count_jump_or_session_reset_is_baseline_not_replay():
    seq = Sequence()
    seq.observe(3)
    seq.settle(6)
    seq.settle(2)
    assert seq.shots == []
    seq.settle(3)
    assert len(seq.shots) == 1


def test_window_or_helper_epoch_change_cannot_finish_previous_shot():
    for key in ("window", "epoch"):
        seq = Sequence()
        seq.observe(3)
        seq.observe(4)
        if key == "epoch":
            seq.epoch += 1
            seq.settle(4)
        else:
            seq.settle(4, window={"pid": 101, "hwnd": 201, "title": "FS Golf"})
        assert seq.shots == []


@pytest.mark.parametrize("first_mode,next_mode", [("full_swing", "chipping"), ("chipping", "full_swing")])
def test_play_mode_switch_baselines_displayed_shot_and_accepts_only_next_shot(first_mode, next_mode):
    seq = Sequence()
    seq.observe(3, capture_context="play_mode", shot_mode=first_mode)
    # The old shot remains visible while the selector switches modes. A mode
    # can also restore its own latest ordinal, including exactly old count + 1.
    seq.settle(4, capture_context="play_mode", shot_mode=next_mode)
    assert seq.shots == []
    assert seq.gate.ready
    seq.settle(5, capture_context="play_mode", shot_mode=next_mode)
    assert len(seq.shots) == 1
    assert seq.shots[0].raw["capture_context"] == "play_mode"
    assert seq.shots[0].raw["shot_mode"] == next_mode


def test_play_mode_switch_discards_a_pending_measurement_from_previous_mode():
    seq = Sequence()
    seq.observe(3, capture_context="play_mode", shot_mode="full_swing")
    seq.observe(4, capture_context="play_mode", shot_mode="full_swing")
    seq.settle(4, capture_context="play_mode", shot_mode="chipping")
    seq.settle(4, capture_context="play_mode", shot_mode="full_swing")
    assert seq.shots == []
    seq.settle(5, capture_context="play_mode", shot_mode="full_swing")
    assert len(seq.shots) == 1


def test_mid_scan_mode_switch_requires_new_baseline_after_consistent_context():
    seq = Sequence()
    seq.observe(3, capture_context="play_mode", shot_mode="full_swing")
    seq.observe(4, capture_context="play_mode", shot_mode="full_swing", context_stable=False, ok=False)
    seq.settle(4, capture_context="play_mode", shot_mode="chipping")
    assert seq.shots == []
    seq.settle(5, capture_context="play_mode", shot_mode="chipping")
    assert len(seq.shots) == 1


@pytest.mark.parametrize("mode", [None, "unknown", ""])
def test_play_mode_without_known_selection_never_establishes_readiness(mode):
    seq = Sequence()
    seq.settle(3, capture_context="play_mode", shot_mode=mode)
    seq.settle(4, capture_context="play_mode", shot_mode=mode)
    assert not seq.gate.ready and seq.shots == []
    seq.settle(4, capture_context="play_mode", shot_mode="full_swing")
    assert seq.shots == []
    seq.settle(5, capture_context="play_mode", shot_mode="full_swing")
    assert len(seq.shots) == 1


def test_leaving_full_swing_session_for_play_mode_discards_existing_latest_shot():
    seq = Sequence()
    seq.observe(3, capture_context="full_swing_session", shot_mode="full_swing")
    seq.settle(4, capture_context="play_mode", shot_mode="full_swing")
    assert seq.shots == []
    seq.settle(5, capture_context="play_mode", shot_mode="full_swing")
    assert len(seq.shots) == 1


def play_observe(seq, total=10, **updates):
    updates.setdefault("capture_context", "play_mode")
    updates.setdefault("shot_mode", "chipping")
    return seq.observe(total, **updates)


def play_cycle(seq, *, readings=None):
    play_observe(seq, radar_status="Tracking\u2026", readings=EMPTY_READINGS)
    play_observe(seq, radar_status="Connected · Limited Flight", readings=readings)
    return play_observe(seq, radar_status="Connected · Limited Flight", readings=readings, step=.6)


def test_actual_capped_counter_chip_trace_emits_once_before_rearm_and_passes_router():
    from companion.gspro import SourceRouter
    fixture = json.loads((Path(__file__).parent / "fixtures/fs_play_mode_chip_cycle.json").read_text())
    gate = AccessibilityShotGate()
    with patch("companion.gspro.time.monotonic", return_value=999.0):
        router = SourceRouter()
        router.player("I7")
    emitted = []
    for recorded in fixture["frames"]:
        elapsed = recorded["elapsed_seconds"]
        wall, now = 1_800_000_000 + elapsed, 1000 + elapsed
        snapshot = {"type": "snapshot", "ok": True, "live_context": True, "context_stable": True,
                    "capture_context": "play_mode", "counter_stable": True,
                    "window": {"pid": 100, "hwnd": 200},
                    "timestamp": datetime.fromtimestamp(wall, timezone.utc).isoformat(), **recorded}
        shot = gate.observe(snapshot, now=now, wall_time=wall)
        if shot:
            emitted.append(shot)
            assert shot.speed_mph == fixture["expected_speed_mph"]
            assert shot.raw["shot_id"] == "10"
            assert shot.raw["shot_mode"] == "chipping"
            assert shot.raw["freshness"] == "tracking"
            assert recorded["radar_status"] == "Connected · Limited Flight"
            assert not gate.ready, "Measured shot delivery is independent of next-shot readiness"
            assert router.accept(shot, now=now) == (True, "")
            assert 2 < now - shot.captured_at < 3, "Timestamp is the first completed reading, not the stable retry"
    assert len(emitted) == 1


def test_tracking_cycle_at_counter_cap_accepts_identical_consecutive_measurements():
    seq = Sequence()
    play_observe(seq)
    assert play_cycle(seq)
    play_observe(seq, radar_status="Arming\u2026")
    play_observe(seq)
    assert play_cycle(seq)
    assert len(seq.shots) == 2
    assert seq.shots[0].event_id != seq.shots[1].event_id
    assert seq.shots[0].speed_mph == seq.shots[1].speed_mph
    for _ in range(5):
        play_observe(seq)
    assert len(seq.shots) == 2


@pytest.mark.parametrize("status", ["Arming\u2026", "Connected · Limited Flight", "Ready"])
def test_metric_changes_without_tracking_or_new_counter_never_become_a_shot(status):
    seq = Sequence()
    play_observe(seq)
    changed = deepcopy(READINGS)
    changed["speed_mph"]["text"] = "29.5"
    for _ in range(5):
        play_observe(seq, radar_status=status, readings=changed)
    assert not seq.shots


def test_tracking_requires_all_native_fields_cleared_and_prior_ready_baseline():
    for prior_ready, blank_fields in [(False, EMPTY_READINGS), (True, READINGS), (True, {})]:
        seq = Sequence()
        if prior_ready:
            play_observe(seq)
        play_observe(seq, radar_status="Tracking\u2026", readings=blank_fields)
        for _ in range(4):
            play_observe(seq, radar_status="Connected · Limited Flight")
        assert not seq.shots


@pytest.mark.parametrize("change", [
    {"shot_mode": "full_swing"}, {"live_context": False}, {"selected": 9},
    {"window": {"pid": 101, "hwnd": 201}}, {"context_stable": False},
    {"radar_status": "Disconnected"},
])
def test_context_changes_cancel_pending_play_mode_cycle(change):
    seq = Sequence()
    play_observe(seq)
    play_observe(seq, radar_status="Tracking\u2026", readings=EMPTY_READINGS)
    play_observe(seq, **change)
    for _ in range(5):
        play_observe(seq)
    assert not seq.shots


def test_pause_epoch_change_cannot_finish_a_previous_tracking_cycle():
    seq = Sequence()
    play_observe(seq)
    play_observe(seq, radar_status="Tracking\u2026", readings=EMPTY_READINGS)
    seq.epoch += 1
    for _ in range(4):
        play_observe(seq)
    assert not seq.shots


def test_late_tracking_result_is_discarded_without_replaying_after_rearm():
    seq = Sequence()
    play_observe(seq)
    play_observe(seq, radar_status="Tracking\u2026", readings=EMPTY_READINGS)
    play_observe(seq, radar_status="Connected", step=8.1)
    for _ in range(5):
        play_observe(seq)
    assert not seq.shots


def test_unstable_completed_cycle_never_refreshes_its_capture_timestamp():
    seq = Sequence()
    play_observe(seq)
    play_observe(seq, radar_status="Tracking\u2026", readings=EMPTY_READINGS)
    play_observe(seq, radar_status="Connected")
    changed = deepcopy(READINGS)
    for index in range(6):
        changed["speed_mph"]["text"] = str(100 + index)
        play_observe(seq, radar_status="Connected", readings=changed, step=.6)
    for _ in range(5):
        play_observe(seq)
    assert not seq.shots


def test_new_tracking_before_previous_cycle_settles_is_ambiguous_and_held():
    seq = Sequence()
    play_observe(seq)
    play_observe(seq, radar_status="Tracking\u2026", readings=EMPTY_READINGS)
    play_observe(seq, radar_status="Connected")
    play_observe(seq, radar_status="Tracking\u2026", readings=EMPTY_READINGS)
    for _ in range(5):
        play_observe(seq)
    assert not seq.shots


def test_new_counter_can_prove_freshness_before_cap_when_tracking_was_between_polls():
    seq = Sequence()
    play_observe(seq, 8)
    play_observe(seq, 9, radar_status="Connected")
    shot = play_observe(seq, 9, radar_status="Connected", step=.6)
    assert shot and shot.raw["freshness"] == "counter"
    assert not seq.gate.ready


@pytest.mark.parametrize("extra", [{"timestamp": "invalid"}, {"timestamp": "2020-01-01T00:00:00Z"},
                                   {"duration_ms": 3000}, {"duration_ms": float("nan")},
                                   {"total_shots": True}, {"selected_shot": None},
                                   {"live_context": "true"}])
def test_untrustworthy_freshness_metadata_cannot_emit(extra):
    seq = Sequence()
    seq.observe(3)
    seq.settle(4, **extra)
    assert seq.shots == []


def test_incomplete_new_shot_cannot_be_replayed_after_late_repair():
    seq = Sequence()
    seq.observe(3)
    for _ in range(12):
        seq.observe(4, readings={})
    seq.settle(4)
    assert seq.shots == []


def test_reader_queue_keeps_only_current_snapshots():
    import io
    values = [{"index": index} for index in range(10)]
    process = SimpleNamespace(stdout=io.BytesIO(b"\n".join(json.dumps(item).encode() for item in values)))
    snapshots = queue.Queue(maxsize=2)
    AccessibilityAdapter._read_lines(process, snapshots)
    assert snapshots.get_nowait() == values[-2]
    assert snapshots.get_nowait() == values[-1]


def test_missing_helper_reports_repair_without_starting_ocr(tmp_path):
    states = []
    adapter = AccessibilityAdapter({"helper_path": str(tmp_path / "missing.exe")}, lambda _: None,
                                   lambda state, message: states.append((state, message)))
    adapter.start()
    deadline = time.monotonic() + 2
    while not states and time.monotonic() < deadline:
        time.sleep(.01)
    adapter.stop()
    assert states and "missing" in states[0][1]
    assert adapter._thread is not None and not adapter._thread.is_alive()


def run_fake_preparation(tmp_path, monkeypatch, schedule, *, stop_after_snapshot=True):
    """Drive the real worker/watchdog with a fake native pipe and virtual time."""
    import io
    from companion import accessibility_adapter as native

    helper = tmp_path / "reader.exe"
    helper.touch()
    states, shots, previews, launches = [], [], [], []
    clock = [0.0]
    adapter = AccessibilityAdapter({"helper_path": str(helper), "auto_start_session": True}, shots.append,
                                   lambda state, message: states.append((state, message)))
    def preview(frame, reading):
        previews.append(reading)
        if stop_after_snapshot:
            adapter._stop.set()
    adapter.on_preview = preview
    def status(state, message):
        states.append((state, message))
        if state == "needs_attention" and "not answering" in message:
            adapter._stop.set()
    adapter.on_status = status

    class PipeProcess:
        stdin = io.BytesIO()
        stopped = False
        def poll(self): return 0 if self.stopped else None
        def wait(self, timeout): self.stopped = True
        def kill(self): self.stopped = True

    class PipeQueue:
        def get(self, timeout):
            if not schedule:
                adapter._stop.set()
                raise queue.Empty()
            delay, message = schedule.pop(0)
            clock[0] += delay
            if message is None:
                raise queue.Empty()
            return message

    process = PipeProcess()
    def launch(command, *args, **kwargs):
        launches.append(command)
        return process
    monkeypatch.setattr(native, "launch_external", launch)
    monkeypatch.setattr(native.queue, "Queue", lambda **_: PipeQueue())
    monkeypatch.setattr(native.threading, "Thread", lambda **_: SimpleNamespace(start=lambda: None))
    monkeypatch.setattr(native.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(native.time, "time", lambda: 1_800_000_000.0)
    adapter._run()
    assert process.stopped
    return adapter, launches, states, shots, previews


def test_startup_progress_allows_long_preparation_without_relaunch_or_shots(tmp_path, monkeypatch):
    window = {"pid": 100, "hwnd": 200, "title": "FS Golf"}
    progress = {"type": "startup_progress", "message": "Preparing the live session", "window": window}
    ready = {
        "type": "snapshot", "ok": True, "startup_status": "prepared", "window": window,
        "timestamp": datetime.fromtimestamp(1_800_000_000, timezone.utc).isoformat(), "duration_ms": 0,
        "live_context": True, "radar_status": "Ready", "counter_stable": True,
        "selected_shot": 0, "total_shots": 0, "readings": EMPTY_READINGS,
    }
    adapter, launches, states, shots, previews = run_fake_preparation(tmp_path, monkeypatch, [
        (9, None), (0, progress), (9, None), (0, progress), (9, ready),
    ])
    assert len(launches) == 1 and "--ensure-live" in launches[0]
    assert adapter._startup_attempted is True
    assert not shots
    assert len(previews) == 1, "Preparation progress must never enter the shot gate"
    assert not any(state == "needs_attention" for state, _ in states)
    assert states[-1][0] == "ready"


def test_progress_does_not_disable_watchdog_for_a_hung_preparation(tmp_path, monkeypatch):
    adapter, launches, states, shots, previews = run_fake_preparation(tmp_path, monkeypatch, [
        (1, {"type": "startup_progress", "message": "Waiting for FS Golf"}), (11, None),
    ])
    assert len(launches) == 1
    assert adapter._startup_attempted is False
    assert not shots and not previews
    assert states[-1][0] == "needs_attention"
    assert "not answering" in states[-1][1]


def test_terminal_preparation_refusal_is_not_automatically_retried(tmp_path, monkeypatch):
    terminal = {
        "type": "snapshot", "startup_status": "action_needed", "window": {"pid": 100, "hwnd": 200},
        "error": "Leave saved-shot review", "live_context": False,
        "timestamp": datetime.fromtimestamp(1_800_000_000, timezone.utc).isoformat(), "duration_ms": 0,
    }
    adapter, launches, _, shots, previews = run_fake_preparation(tmp_path, monkeypatch,
        [(0, terminal), (1, terminal)], stop_after_snapshot=False)
    assert len(launches) == 1
    assert adapter._startup_attempted is True
    assert not shots and len(previews) == 2
