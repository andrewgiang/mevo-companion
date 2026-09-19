"""Protocol invariants for the private Mevo+ acquisition process."""
import math
import tomllib
import uuid

import pytest

from companion.flighthook_adapter import FlightHookAdapter, build_config, shot_from_frp, speed_mph


BALL = {"launch_speed": "67.2mps", "launch_azimuth": -1.3,
        "launch_elevation": 14.2, "backspin_rpm": 12345, "sidespin_rpm": -450}
DEVICE = "FS-M2-12345"


def adapter():
    shots, statuses = [], []
    instance = FlightHookAdapter({}, shots.append, lambda *status: statuses.append(status))
    return instance, shots, statuses


def message(kind, **fields):
    return {"actor": "mevo.0", "device": DEVICE, "event": {"kind": kind, **fields}}


def ready(instance, now=100.0):
    instance.handle_message(message("actor_status", status="connected"), now=now)
    instance.handle_message(message("device_telemetry", telemetry={"ready": "true"}), now=now)


def sequence(instance, *, key=None, ball=None, now=100.0):
    key = key or {"shot_id": str(uuid.uuid4()), "shot_number": 1}
    instance.handle_message(message("shot_trigger", key=key), now=now)
    instance.handle_message(message("device_telemetry", telemetry={"ready": "false"}), now=now)
    instance.handle_message(message("ball_flight", key=key, ball=BALL if ball is None else ball), now=now + .5)
    instance.handle_message(message("shot_finished", key=key), now=now + 1)
    return key


@pytest.mark.parametrize("reading, expected", [("10mph", 10), ("10mps", 22.369362920544),
                                                ("10kph", 6.2137119223733), ("10fps", 6.8181818181818)])
def test_explicit_speed_units(reading, expected):
    assert speed_mph(reading) == pytest.approx(expected)


@pytest.mark.parametrize("reading", [10, "10", "10 knots", "nanmph", "infinitymps", "1e999mph", True])
def test_missing_and_nonfinite_speed_rejected(reading):
    with pytest.raises(ValueError):
        speed_mph(reading)


def test_preserves_signed_angles_five_digit_spin_and_optional_club():
    shot = shot_from_frp(BALL, None, str(uuid.uuid4()), 123.0)
    assert shot.hla == -1.3
    assert shot.spin_rpm == pytest.approx(math.hypot(12345, -450))
    assert shot.spin_axis == pytest.approx(math.degrees(math.atan2(-450, 12345)))
    assert shot.captured_at == 123.0
    assert shot.club_speed_mph is None
    assert shot.raw["gspro_mirror_for_left_handed"] is True


@pytest.mark.parametrize("field", list(BALL))
def test_missing_required_measurement_never_zero_filled(field):
    ball = {k: v for k, v in BALL.items() if k != field}
    with pytest.raises(ValueError):
        shot_from_frp(ball, None, str(uuid.uuid4()), 100)


@pytest.mark.parametrize("field,value", [("launch_azimuth", float("nan")),
                                         ("backspin_rpm", True), ("sidespin_rpm", "-400"),
                                         ("launch_elevation", 200)])
def test_invalid_measurements_rejected(field, value):
    with pytest.raises(ValueError):
        shot_from_frp({**BALL, field: value}, None, str(uuid.uuid4()), 100)


def test_connected_is_not_ready_until_device_reports_it():
    instance, shots, _ = adapter()
    instance.handle_message(message("actor_status", status="connected"), now=100)
    assert not instance.ready
    sequence(instance)
    assert shots == []
    ready(instance)
    assert instance.ready


def test_complete_shot_forwarded_once_identical_physical_shots_are_distinct():
    instance, shots, _ = adapter()
    ready(instance)
    key = sequence(instance)
    ready(instance, 101)
    sequence(instance, key=key, now=101)
    ready(instance, 102)
    sequence(instance, now=102)
    assert len(shots) == 2
    assert shots[0].event_id != shots[1].event_id
    assert shots[0].speed_mph == shots[1].speed_mph


def test_orphan_finish_and_ball_events_do_not_resurrect_history():
    instance, shots, _ = adapter()
    ready(instance)
    key = {"shot_id": str(uuid.uuid4()), "shot_number": 1}
    instance.handle_message(message("ball_flight", key=key, ball=BALL), now=100)
    instance.handle_message(message("shot_finished", key=key), now=101)
    assert shots == []


@pytest.mark.parametrize("key", [{"shot_id": "old", "shot_number": 1},
                                 {"shot_id": str(uuid.uuid4()), "shot_number": True},
                                 {"shot_id": str(uuid.uuid4()), "shot_number": 0}])
def test_invalid_identity_is_not_forwarded(key):
    instance, shots, _ = adapter()
    ready(instance)
    sequence(instance, key=key)
    assert shots == []


def test_partial_estimated_chip_is_visible_rejection():
    instance, shots, statuses = adapter()
    ready(instance)
    sequence(instance, ball={k: v for k, v in BALL.items() if k != "sidespin_rpm"})
    assert shots == []
    assert statuses[-1][0] == "rejected"
    assert "sidespin" in statuses[-1][1]


def test_estimated_disabled_no_ball_finishes_with_clear_rejection():
    instance, shots, statuses = adapter()
    ready(instance)
    key = {"shot_id": str(uuid.uuid4()), "shot_number": 1}
    instance.handle_message(message("shot_trigger", key=key), now=100)
    instance.handle_message(message("shot_finished", key=key), now=101)
    assert shots == []
    assert "Estimated-only chips" in statuses[-1][1]


def test_reconnect_drops_inflight_shot_even_after_ready_again():
    instance, shots, _ = adapter()
    ready(instance)
    key = {"shot_id": str(uuid.uuid4()), "shot_number": 1}
    instance.handle_message(message("shot_trigger", key=key), now=100)
    instance.handle_message(message("actor_status", status="reconnecting"), now=101)
    ready(instance, 102)
    instance.handle_message(message("ball_flight", key=key, ball=BALL), now=102)
    instance.handle_message(message("shot_finished", key=key), now=103)
    assert shots == []


def test_mode_switch_fences_old_telemetry_and_shot():
    instance, shots, _ = adapter()
    ready(instance)
    key = {"shot_id": str(uuid.uuid4()), "shot_number": 1}
    instance.handle_message(message("shot_trigger", key=key), now=100)
    instance.set_mode("chipping")
    ready(instance, 101)
    assert not instance.ready
    instance.handle_message(message("device_telemetry", telemetry={"ready": "false"}), now=102)
    ready(instance, 103)
    assert not instance.ready  # stale startup false/true before the mode command
    instance.handle_message({"actor": "web", "event": {"kind": "set_detection_mode", "mode": "chipping"}}, now=104)
    ready(instance, 104)
    assert not instance.ready
    instance.handle_message(message("device_telemetry", telemetry={"ready": "false"}), now=105)
    ready(instance, 106)
    assert instance.ready
    instance.handle_message(message("ball_flight", key=key, ball=BALL), now=106)
    instance.handle_message(message("shot_finished", key=key), now=107)
    assert shots == []


def test_stale_readings_and_stale_device_status_unarm():
    instance, shots, statuses = adapter()
    ready(instance)
    key = {"shot_id": str(uuid.uuid4()), "shot_number": 1}
    instance.handle_message(message("shot_trigger", key=key), now=100)
    instance.handle_message(message("ball_flight", key=key, ball=BALL), now=111)
    instance.handle_message(message("shot_finished", key=key), now=112)
    assert shots == []
    assert statuses[-1][0] == "rejected"
    instance._check_freshness(now=113)
    assert not instance.ready
    assert statuses[-1][0] == "reconnecting"


def test_wrong_actor_and_changed_device_cannot_finish_mevo_shot():
    instance, shots, _ = adapter()
    ready(instance)
    key = {"shot_id": str(uuid.uuid4()), "shot_number": 1}
    instance.handle_message(message("shot_trigger", key=key), now=100)
    instance.handle_message({"actor": "mock_monitor.0", "device": DEVICE,
                             "event": {"kind": "ball_flight", "key": key, "ball": BALL}}, now=100)
    instance.handle_message({**message("device_telemetry", telemetry={"ready": "true"}),
                             "device": "FS-M2-OTHER"}, now=101)
    instance.handle_message(message("shot_finished", key=key), now=102)
    assert shots == []


def test_no_putting_mode_on_direct_device():
    instance, _, _ = adapter()
    with pytest.raises(ValueError):
        instance.set_mode("putting")


def test_private_config_has_no_simulator_socket_and_default_estimates_disabled():
    config = tomllib.loads(build_config({}, 54321, "test session"))
    assert config["webserver"]["0"]["bind"] == "127.0.0.1:54321"
    assert "gspro" not in config
    assert "openconnect_server" not in config
    assert config["mevo"]["0"]["use_estimated"] is False
    assert config["mevo"]["0"]["camera_mode"] == "standard"
    assert config["chipping_clubs"] == []


@pytest.mark.parametrize("config", [{"mevo_address": 'bad"\n[gspro.0]'},
                                    {"mevo_address": "192.168.2.1:70000"},
                                    {"ball_type": True}, {"use_estimated": "false"},
                                    {"mevo_range_ft": 0}, {"track_pct": float("nan")},
                                    {"camera_mode": "unknown"}])
def test_config_rejects_invalid_values(config):
    with pytest.raises(ValueError):
        build_config(config, 54321, "test session")


def test_cleanup_only_terminates_the_child_process_it_owns():
    instance, _, _ = adapter()

    class OwnedProcess:
        stopped = False
        waited = False

        def poll(self):
            return None

        def terminate(self):
            self.stopped = True

        def wait(self, timeout):
            self.waited = True

    process = OwnedProcess()
    instance._process = process
    instance._cleanup_process()
    assert process.stopped and process.waited
    assert instance._process is None
