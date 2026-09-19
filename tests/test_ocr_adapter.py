from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
import pytest

from companion.ocr_adapter import (
    FreshShotGate, LabelRecognizer, OCRToken, Recognition, parse_number,
)


def make_reading(speed=140.5, counter=None, dimensions=(1200, 650)):
    return Recognition(values={"speed_mph": speed, "hla": -2.4, "vla": 14.8,
                               "spin_rpm": 12345, "spin_axis": -10.2},
                       shot_id=str(counter) if counter is not None else None,
                       dimensions=dimensions)


def settle(gate, reading, start):
    outputs = [gate.observe(reading, start + step * .35) for step in range(3)]
    return [shot for shot in outputs if shot is not None]


def synthetic_frame(scale=1.0, counter=14, signed=True):
    """Rendered FS-style cards exercise the real Tesseract pipeline, not mock OCR."""
    frame = Image.new("RGB", (1200, 680), "#15232e")
    draw = ImageDraw.Draw(frame)
    font_path = Path("C:/Windows/Fonts/arial.ttf")
    if not font_path.exists():
        font_path = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
    label = ImageFont.truetype(str(font_path), 25)
    value = ImageFont.truetype(str(font_path), 47)
    small = ImageFont.truetype(str(font_path), 22)
    draw.text((40, 23), "FS Golf PC 2", font=label, fill="white")
    draw.text((900, 23), f"Shot {counter}", font=label, fill="white")
    cards = [("Ball Speed", "140.5", "mph"), ("Launch Direction", "-2.4" if signed else "2.4L", ""),
             ("Launch Angle", "14.8", ""), ("Spin Rate", "12345", "rpm"),
             ("Spin Axis", "-10.2" if signed else "10.2L", ""), ("Club Speed", "95.1", "mph")]
    for i, (title, number, unit) in enumerate(cards):
        x, y = 30 + (i % 3) * 390, 100 + (i // 3) * 265
        draw.rounded_rectangle((x, y, x + 360, y + 235), radius=10, fill="#243844")
        draw.text((x + 30, y + 22), title, font=label, fill="white")
        draw.text((x + 30, y + 88), number, font=value, fill="white")
        if unit:
            draw.text((x + 30, y + 158), unit, font=small, fill="white")
    if scale != 1:
        frame = frame.resize((round(frame.width * scale), round(frame.height * scale)), Image.Resampling.LANCZOS)
    return frame


@pytest.mark.parametrize("text,metric,expected", [
    ("12345", "spin_rpm", 12345), ("12,345", "spin_rpm", 12345),
    ("-12.4", "spin_axis", -12.4), ("\u221212.4", "spin_axis", -12.4),
    ("12.4L", "spin_axis", -12.4), ("R2.4", "hla", 2.4),
    ("+2.4", "hla", 2.4), (".4", "hla", .4),
])
def test_complete_numbers_preserve_sign_decimal_and_five_digit_spin(text, metric, expected):
    assert parse_number(text, metric) == expected


@pytest.mark.parametrize("text,metric", [
    ("12.4LL", "spin_axis"), ("-12.4L", "spin_axis"), ("1O.2", "spin_axis"),
    ("14,5", "speed_mph"), ("12345.6", "spin_rpm"), ("99999", "spin_rpm"),
    ("+", "hla"), ("140 mph", "speed_mph"), ("L140", "speed_mph"),
])
def test_uncertain_numbers_are_rejected_without_correction(text, metric):
    with pytest.raises(ValueError):
        parse_number(text, metric)


@pytest.mark.parametrize("scale", [1, .75, 1.5])
def test_actual_english_ocr_finds_labels_and_values_after_resize(scale):
    engine = LabelRecognizer()
    try:
        reading = engine.recognize(synthetic_frame(scale))
        assert reading.valid, (reading.errors, reading.raw)
        assert reading.values == {"speed_mph": 140.5, "hla": -2.4, "vla": 14.8,
                                  "spin_rpm": 12345, "spin_axis": -10.2, "club_speed_mph": 95.1}
        assert reading.shot_id == "14"
        assert reading.dimensions == (round(1200 * scale), round(680 * scale))
    finally:
        engine.close()


def test_actual_ocr_direction_suffixes():
    engine = LabelRecognizer()
    try:
        reading = engine.recognize(synthetic_frame(signed=False))
        assert reading.valid, (reading.errors, reading.raw)
        assert reading.values["hla"] == -2.4
        assert reading.values["spin_axis"] == -10.2
    finally:
        engine.close()


def test_unrecognized_screen_never_becomes_readable():
    engine = LabelRecognizer()
    try:
        assert not engine.recognize(Image.new("RGB", (800, 600), "white")).valid
    finally:
        engine.close()


def test_startup_readings_are_not_replayed_and_identical_counter_shots_are_forwarded():
    gate = FreshShotGate()
    assert settle(gate, make_reading(counter=7), 0) == []
    assert settle(gate, make_reading(counter=7), 1) == []
    shots = settle(gate, make_reading(counter=8), 2)
    assert len(shots) == 1
    assert shots[0].speed_mph == 140.5
    assert settle(gate, make_reading(counter=8), 3) == []


def test_no_counter_requires_changed_stable_readings_without_timeout_replays():
    gate = FreshShotGate()
    assert settle(gate, make_reading(), 0) == []
    assert settle(gate, make_reading(), 600) == []
    assert len(settle(gate, make_reading(speed=141), 601)) == 1
    assert settle(gate, make_reading(speed=141), 1200) == []


def test_transient_partial_updates_do_not_emit():
    gate = FreshShotGate()
    settle(gate, make_reading(counter=7), 0)
    assert gate.observe(make_reading(speed=90, counter=8), 1) is None
    assert gate.observe(make_reading(speed=100, counter=8), 1.35) is None
    assert gate.observe(make_reading(speed=142, counter=8), 1.7) is None
    assert gate.observe(make_reading(speed=142, counter=8), 2.05) is None
    assert gate.observe(make_reading(speed=142, counter=8), 2.4) is not None


def test_resize_and_reset_hold_new_baseline_without_stale_replay():
    gate = FreshShotGate()
    settle(gate, make_reading(counter=7), 0)
    assert settle(gate, make_reading(speed=141, counter=8, dimensions=(1800, 975)), 1) == []
    assert len(settle(gate, make_reading(speed=142, counter=9, dimensions=(1800, 975)), 2)) == 1
    gate.reset()
    assert settle(gate, make_reading(speed=142, counter=9), 3) == []


def test_lower_counter_is_held_without_replaying_history():
    gate = FreshShotGate()
    settle(gate, make_reading(counter=7), 0)
    assert settle(gate, make_reading(speed=135, counter=6), 1) == []
    assert settle(gate, make_reading(counter=7), 2) == []
    assert len(settle(gate, make_reading(counter=8), 3)) == 1


def test_missing_units_do_not_assume_mph():
    engine = LabelRecognizer()
    try:
        frame = synthetic_frame()
        tokens = [t for t in engine.tokens(frame) if t.text.lower() != "mph"]
        reading = engine.recognize_tokens(tokens, frame.size)
        assert not reading.valid
        assert "speed unit" in reading.errors[0]
    finally:
        engine.close()


def test_low_confidence_value_is_not_accepted():
    engine = LabelRecognizer()
    try:
        frame = synthetic_frame()
        tokens = [OCRToken(t.text, 10 if t.text == "12345" else t.confidence, t.box) for t in engine.tokens(frame)]
        reading = engine.recognize_tokens(tokens, frame.size)
        assert not reading.valid
        assert "confidence" in reading.errors[0]
    finally:
        engine.close()


def test_wrapped_labels_are_located_without_rectangles():
    tokens = [OCRToken("Horizontal", 98, (10, 10, 140, 35)),
              OCRToken("Launch", 98, (10, 40, 90, 65)),
              OCRToken("-2.4", 98, (10, 85, 90, 125))]
    engine = LabelRecognizer()
    reading = engine.recognize_tokens(tokens, (300, 200))
    assert reading.values["hla"] == -2.4


def test_speed_units_are_converted_explicitly_and_conflicts_rejected():
    engine = LabelRecognizer()
    try:
        frame = synthetic_frame()
        tokens = [OCRToken("km/h" if t.text == "mph" else t.text, t.confidence, t.box) for t in engine.tokens(frame)]
        reading = engine.recognize_tokens(tokens, frame.size)
        assert reading.valid, reading.errors
        assert reading.values["speed_mph"] == pytest.approx(140.5 / 1.609344)
        configured = LabelRecognizer({"speed_unit": "mph"})
        reading = configured.recognize_tokens(tokens, frame.size)
        assert not reading.valid
        assert "unit changed" in reading.errors[0]
    finally:
        engine.close()


def test_duplicate_shot_panels_rejected_instead_of_picking_arbitrary_values():
    engine = LabelRecognizer()
    try:
        frame = synthetic_frame()
        tokens = engine.tokens(frame)
        tokens += [OCRToken(t.text, t.confidence, (t.box[0] + 1300, t.box[1], t.box[2] + 1300, t.box[3])) for t in list(tokens)]
        reading = engine.recognize_tokens(tokens, (2500, 680))
        assert not reading.valid
        assert "Multiple shot panels" in reading.errors[0]
    finally:
        engine.close()


def test_legacy_profile_is_explicit_and_scales_relative_to_original_capture():
    metrics = ["speed_mph", "hla", "vla", "spin_rpm", "spin_axis", "club_speed_mph"]
    rois = {key: [60 + (i % 3) * 390, 182 + (i // 3) * 265, 290, 74] for i, key in enumerate(metrics)}
    engine = LabelRecognizer({"mode": "legacy", "speed_unit": "mph",
                              "legacy_profile": {"source_size": [1200, 680], "rois": rois}})
    try:
        reading = engine.recognize(synthetic_frame(1.5))
        assert reading.valid, (reading.errors, reading.raw)
        assert reading.mode == "legacy"
        assert reading.values["spin_rpm"] == 12345
        assert reading.values["spin_axis"] == -10.2
    finally:
        engine.close()
