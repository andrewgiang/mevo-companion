"""Automatic, label-anchored FS Golf acquisition with conservative shot freshness.

All coordinates are client-image coordinates. Nothing here alters FS Golf's UI.
Recognition is separate from capture and routing so real screenshots can be
regression fixtures. See docs/ocr-profile.md for the supported layout contract.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import logging
import math
from pathlib import Path
import re
import sys
import threading
import time
from typing import Callable

import numpy as np
from PIL import Image

from .models import Shot
from .window_capture import CaptureError, WindowCapture, discover_windows

LOG = logging.getLogger(__name__)
REQUIRED = ("speed_mph", "hla", "vla", "spin_rpm", "spin_axis")
LABELS = {
    "speed_mph": ("ball speed",),
    "hla": ("launch direction", "horizontal launch", "horizontal launch angle", "lateral launch", "launch h", "hla"),
    "vla": ("launch angle", "vertical launch", "vertical launch angle", "launch v", "vla"),
    "spin_rpm": ("spin rate", "total spin", "spin"),
    "spin_axis": ("spin axis",),
    "club_speed_mph": ("club speed", "clubhead speed", "club head speed"),
    "shot_id": ("shot number", "shot count", "shot"),
}


@dataclass(frozen=True)
class OCRToken:
    text: str
    confidence: float
    box: tuple[int, int, int, int]

    @property
    def cx(self):
        return (self.box[0] + self.box[2]) / 2

    @property
    def cy(self):
        return (self.box[1] + self.box[3]) / 2

    @property
    def height(self):
        return max(1, self.box[3] - self.box[1])


@dataclass
class Recognition:
    values: dict[str, float] = field(default_factory=dict)
    anchors: dict[str, tuple[int, int, int, int]] = field(default_factory=dict)
    raw: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    shot_id: str | None = None
    dimensions: tuple[int, int] = (0, 0)
    mode: str = "automatic"

    @property
    def valid(self) -> bool:
        return not self.errors and all(key in self.values for key in REQUIRED)

    @property
    def signature(self) -> tuple:
        return tuple(self.values.get(key) for key in REQUIRED)

    def to_shot(self, captured_at: float | None = None) -> Shot:
        if not self.valid:
            raise ValueError("Incomplete or uncertain FS Golf readings")
        shot = Shot(source="mevo", **self.values,
                    captured_at=time.monotonic() if captured_at is None else captured_at,
                    raw={"ocr": self.raw.copy(), "shot_id": self.shot_id, "mode": self.mode})
        shot.validate()
        return shot


def _normal(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.casefold())


def parse_number(text: str, metric: str) -> float:
    """Parse a complete token; never truncate, guess a decimal or correct a digit."""
    value = text.strip().upper().replace("\u2212", "-").replace("\u2013", "-")
    value = value.replace("\u00b0", "").replace(" ", "")
    # Decimal commas are ambiguous. Commas only mean thousands grouping here.
    if "," in value:
        if not re.fullmatch(r"[LR]?[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?[LR]?", value):
            raise ValueError(f"Ambiguous decimal or grouping: {text}")
        value = value.replace(",", "")
    match = re.fullmatch(r"([LR]?)([+-]?(?:\d+(?:\.\d+)?|\.\d+))([LR]?)", value)
    if not match or (match[1] and match[3]):
        raise ValueError(f"Uncertain numeric reading: {text}")
    direction = match[1] or match[3]
    number = float(match[2])
    if direction:
        if metric not in {"hla", "spin_axis"} or match[2][0] in "+-":
            raise ValueError(f"Unexpected direction: {text}")
        number *= -1 if direction == "L" else 1
    bounds = {"speed_mph": (0, 350), "club_speed_mph": (0, 350),
              "hla": (-90, 90), "vla": (-30, 90), "spin_rpm": (0, 20000),
              "spin_axis": (-180, 180), "shot_id": (0, 1_000_000)}
    low, high = bounds[metric]
    if not math.isfinite(number) or not low <= number <= high:
        raise ValueError(f"{metric} is outside the supported range")
    if metric in {"speed_mph", "club_speed_mph"} and number == 0:
        raise ValueError("No completed shot speed is visible")
    if metric in {"spin_rpm", "shot_id"} and number != int(number):
        raise ValueError(f"{metric} must contain complete whole digits")
    return number


def _union(tokens: list[OCRToken]) -> tuple[int, int, int, int]:
    return (min(t.box[0] for t in tokens), min(t.box[1] for t in tokens),
            max(t.box[2] for t in tokens), max(t.box[3] for t in tokens))


class LabelRecognizer:
    """Find label/value pairs afresh on every image; no saved absolute rectangles."""
    def __init__(self, config: dict | None = None):
        self.config = config or {}
        self.minimum_confidence = float(self.config.get("minimum_confidence", 65))
        self._api = None

    def close(self):
        if self._api is not None:
            self._api.End()
            self._api = None

    def _engine(self):
        if self._api is None:
            import tesserocr
            roots = [Path(self.config["tessdata_path"])] if self.config.get("tessdata_path") else [
                Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent)),
                Path(__file__).resolve().parent / "resources" / "tessdata",
            ]
            root = next((p for p in roots if (p / "eng.traineddata").is_file()), None)
            if root is None:
                raise RuntimeError("English OCR data is missing. Repair the Mevo Companion installation.")
            self._api = tesserocr.PyTessBaseAPI(path=str(root), lang="eng", psm=tesserocr.PSM.SPARSE_TEXT)
            self._api.SetVariable("preserve_interword_spaces", "1")
        return self._api

    def tokens(self, image: np.ndarray | Image.Image) -> list[OCRToken]:
        import tesserocr
        frame = image if isinstance(image, Image.Image) else Image.fromarray(image)
        frame = frame.convert("RGB")
        api = self._engine()
        api.SetPageSegMode(tesserocr.PSM.SPARSE_TEXT)
        api.SetImage(frame)
        api.Recognize()
        iterator = api.GetIterator()
        result = []
        if iterator is not None:
            level = tesserocr.RIL.WORD
            while True:
                try:
                    text = iterator.GetUTF8Text(level)
                except RuntimeError:  # Tesseract returns an empty iterator for blank frames.
                    break
                box = iterator.BoundingBox(level)
                if text and box:
                    result.append(OCRToken(text.strip(), iterator.Confidence(level), box))
                if not iterator.Next(level):
                    break
        return result

    def recognize(self, image: np.ndarray | Image.Image) -> Recognition:
        frame = image if isinstance(image, Image.Image) else Image.fromarray(image)
        if self.config.get("mode") == "legacy":
            return self._legacy(frame)
        return self.recognize_tokens(self.tokens(frame), frame.size)

    def _anchors(self, tokens: list[OCRToken]):
        aliases = [(_normal(alias), key) for key, names in LABELS.items() for alias in names]
        found = []
        # Form spatially adjacent phrases, independent of Tesseract block ordering.
        for index, first in enumerate(tokens):
            if first.confidence < self.minimum_confidence:
                continue
            pending = [[(index, first)]]
            while pending:
                group = pending.pop()
                normalized = _normal(" ".join(t.text for _, t in group))
                for alias, key in aliases:
                    if normalized == alias:
                        found.append((len(alias), key, [i for i, _ in group], _union([t for _, t in group])))
                if len(group) >= 4 or not any(alias.startswith(normalized) and alias != normalized for alias, _ in aliases):
                    continue
                last = group[-1][1]
                for i, token in enumerate(tokens):
                    if i in [n for n, _ in group] or token.confidence < self.minimum_confidence:
                        continue
                    line_height = max(last.height, token.height)
                    adjacent = (token.box[0] >= last.box[2] - 2
                                and abs(token.cy - last.cy) <= line_height * .5
                                and token.box[0] - last.box[2] < line_height * 2)
                    # Cards may wrap long labels over two lines.
                    wrapped = (0 <= token.box[1] - last.box[3] <= line_height * .9
                               and abs(token.box[0] - first.box[0]) < line_height * 2)
                    extension = normalized + _normal(token.text)
                    if (adjacent or wrapped) and any(alias.startswith(extension) for alias, _ in aliases):
                        pending.append(group + [(i, token)])
        # Longest labels win: "spin" must not steal "spin axis".
        used, anchors, duplicates = set(), {}, set()
        for _, key, indices, box in sorted(found, key=lambda x: x[0], reverse=True):
            if used.intersection(indices):
                continue
            used.update(indices)
            if key in anchors:
                duplicates.add(key)
            else:
                anchors[key] = OCRToken(key, 100, box)
        return anchors, used, duplicates

    def _numbers(self, tokens: list[OCRToken], used: set[int]):
        result = []
        consumed = set(used)
        for i, token in enumerate(tokens):
            if i in consumed or not re.search(r"\d", token.text):
                continue
            group = [token]
            # Join a detached +/- or L/R only when tightly adjacent on this line.
            for j, part in enumerate(tokens):
                if j in consumed or j == i or part.text.strip().upper() not in {"+", "-", "\u2212", "L", "R", "\u00b0"}:
                    continue
                same_line = abs(part.cy - token.cy) <= max(part.height, token.height) * .6
                gap = min(abs(part.box[0] - token.box[2]), abs(token.box[0] - part.box[2]))
                if same_line and gap < token.height * .6:
                    group.append(part)
                    consumed.add(j)
            group.sort(key=lambda t: t.box[0])
            candidate = OCRToken("".join(t.text for t in group), min(t.confidence for t in group), _union(group))
            consumed.add(i)
            # Values containing letter lookalikes (O/I/S) never become numbers.
            if re.fullmatch(r"[LR+\-\u2212\u2013\d.,\u00b0]+", candidate.text.upper()):
                result.append(candidate)
        return result

    @staticmethod
    def _distance(anchor: OCRToken, value: OCRToken) -> float | None:
        line = max(anchor.height, value.height)
        horizontal = abs(anchor.cx - value.cx)
        vertical = abs(anchor.cy - value.cy)
        # Card layouts, with label directly above or below the larger value.
        if horizontal < max((anchor.box[2] - anchor.box[0]) * .8, line * 2) and vertical <= line * 4:
            return vertical / line + horizontal / line * .9
        # Table layouts: label on the left, value in the adjacent cell.
        if vertical < line * .55 and 0 <= value.box[0] - anchor.box[2] <= line * 5:
            return 1 + (value.box[0] - anchor.box[2]) / line
        return None

    @staticmethod
    def _speed_unit(tokens, anchor, value):
        candidates = []
        for token in tokens:
            unit = token.text.strip().lower().replace(" ", "").replace(".", "")
            unit = {"mph": "mph", "km/h": "km/h", "kmh": "km/h", "kph": "km/h", "m/s": "m/s"}.get(unit)
            if unit and abs(token.cx - value.cx) < max(anchor.box[2] - anchor.box[0], value.height * 4) and abs(token.cy - value.cy) < value.height * 3:
                candidates.append((abs(token.cx - value.cx) + abs(token.cy - value.cy), unit))
        return min(candidates)[1] if candidates else None

    def recognize_tokens(self, tokens: list[OCRToken], dimensions=(0, 0)) -> Recognition:
        result = Recognition(dimensions=dimensions)
        anchors, used, duplicates = self._anchors(tokens)
        result.anchors = {key: token.box for key, token in anchors.items()}
        if any(key in duplicates for key in REQUIRED):
            result.errors.append("Multiple shot panels are visible. Open a single shot's data view.")
            return result
        numbers = self._numbers(tokens, used)
        selected = {}
        for key, anchor in anchors.items():
            scored = []
            for index, value in enumerate(numbers):
                distance = self._distance(anchor, value)
                if distance is not None:
                    scored.append((distance, index, value))
            scored.sort(key=lambda item: item[0])
            if not scored:
                continue
            if len(scored) > 1 and scored[1][0] - scored[0][0] < .3:
                if key in REQUIRED:
                    result.errors.append(f"The value beside {key.replace('_', ' ')} is ambiguous.")
                continue
            _, index, value = scored[0]
            if index in selected:
                result.errors.append("Two labels point to the same value. Open the supported shot data layout.")
                continue
            selected[index] = key
            result.raw[key] = value.text
            try:
                if value.confidence < self.minimum_confidence:
                    raise ValueError(f"Low reading confidence for {key.replace('_', ' ')}")
                number = parse_number(value.text, key)
                if key == "shot_id":
                    result.shot_id = str(int(number))
                    continue
                if key in {"speed_mph", "club_speed_mph"}:
                    configured_unit = self.config.get("speed_unit", "auto")
                    observed_unit = self._speed_unit(tokens, anchor, value)
                    unit = observed_unit or configured_unit
                    if observed_unit and configured_unit not in {"auto", observed_unit}:
                        raise ValueError("FS Golf's displayed speed unit changed. Update the speed unit in setup.")
                    if unit not in {"mph", "km/h", "m/s"}:
                        raise ValueError("Show MPH beside Ball Speed in FS Golf, or choose its speed unit in setup.")
                    number *= {"mph": 1, "km/h": 0.621371192237334, "m/s": 2.2369362920544}[unit]
                    if number > 250:
                        raise ValueError("Speed is outside the supported range")
                result.values[key] = number
            except ValueError as error:
                if key in REQUIRED:
                    result.errors.append(str(error))
        missing = [key for key in REQUIRED if key not in result.values]
        if missing and not result.errors:
            names = {"speed_mph": "Ball Speed", "hla": "Launch Direction", "vla": "Launch Angle", "spin_rpm": "Spin Rate", "spin_axis": "Spin Axis"}
            result.errors.append("Open FS Golf's shot data view showing " + ", ".join(names[key] for key in missing) + ".")
        return result

    def _legacy(self, frame: Image.Image) -> Recognition:
        """Explicit compatibility import only. ROIs scale from their original image."""
        import tesserocr
        result = Recognition(dimensions=frame.size, mode="legacy")
        profile = self.config.get("legacy_profile") or {}
        source = profile.get("source_size")
        rois = profile.get("rois") or {}
        if not source or len(source) != 2 or min(source) <= 0:
            result.errors.append("The imported OCR profile needs its original capture dimensions.")
            return result
        api = self._engine()
        for key in REQUIRED + ("club_speed_mph",):
            roi = rois.get(key)
            if not roi or len(roi) != 4:
                if key in REQUIRED:
                    result.errors.append(f"Imported profile is missing {key}.")
                continue
            x, y, width, height = roi
            scaled = (round(x * frame.width / source[0]), round(y * frame.height / source[1]),
                      round((x + width) * frame.width / source[0]), round((y + height) * frame.height / source[1]))
            if not 0 <= scaled[0] < scaled[2] <= frame.width or not 0 <= scaled[1] < scaled[3] <= frame.height:
                result.errors.append("Imported profile does not fit this FS Golf capture.")
                continue
            api.SetPageSegMode(tesserocr.PSM.SINGLE_LINE)
            api.SetImage(frame.crop(scaled))
            text = api.GetUTF8Text().strip()
            result.raw[key] = text
            result.anchors[key] = scaled
            try:
                if api.MeanTextConf() < self.minimum_confidence:
                    raise ValueError(f"Low reading confidence for {key}")
                number = parse_number(text, key)
                if key in {"speed_mph", "club_speed_mph"}:
                    unit = self.config.get("speed_unit", "auto")
                    if unit not in {"mph", "km/h", "m/s"}:
                        raise ValueError("Choose the speed unit for the imported OCR profile.")
                    number *= {"mph": 1, "km/h": 0.621371192237334, "m/s": 2.2369362920544}[unit]
                    if number > 250:
                        raise ValueError("Speed is outside the supported range")
                result.values[key] = number
            except ValueError as error:
                if key in REQUIRED:
                    result.errors.append(str(error))
        return result


def recognize_frame(image, config: dict | None = None) -> Recognition:
    recognizer = LabelRecognizer(config)
    try:
        return recognizer.recognize(image)
    finally:
        recognizer.close()


class FreshShotGate:
    """Discard startup data and require stable new observations.

    A visible increasing shot counter permits identical shots. Without one, only
    changed readings count as evidence; identical physical shots are unobservable
    and history navigation cannot be distinguished from a new shot. No timeout
    alone rearms an old reading. Capture gaps and source switches reset the gate.
    """
    def __init__(self, stable_frames=3, stable_seconds=.6):
        self.stable_frames = max(2, int(stable_frames))
        self.stable_seconds = max(.1, float(stable_seconds))
        self.reset()

    def reset(self):
        self._candidate = None
        self._candidate_since = 0.0
        self._count = 0
        self._baseline = None
        self._highest_counter = None
        self._dimensions = None
        self.last_reason = "Checking the current shot before arming"

    def observe(self, reading: Recognition, now: float | None = None) -> Shot | None:
        now = time.monotonic() if now is None else now
        if self._dimensions is not None and reading.dimensions != self._dimensions:
            self.reset()
            self.last_reason = "FS Golf resized; checking its current shot again"
        self._dimensions = reading.dimensions
        if not reading.valid:
            self._candidate = None
            self._count = 0
            self.last_reason = "Waiting for complete shot data"
            return None
        fingerprint = (reading.shot_id, reading.signature)
        if fingerprint != self._candidate:
            self._candidate = fingerprint
            self._candidate_since = now
            self._count = 1
            self.last_reason = "Waiting for the shot readings to settle"
            return None
        self._count += 1
        if self._count < self.stable_frames or now - self._candidate_since < self.stable_seconds:
            return None
        if self._baseline is None:
            self._baseline = fingerprint
            self._highest_counter = int(reading.shot_id) if reading.shot_id is not None else None
            self.last_reason = "Current shot held as baseline; waiting for a new shot"
            return None
        if fingerprint == self._baseline:
            return None
        old_id, old_values = self._baseline
        new_id, new_values = fingerprint
        if old_id is not None and new_id is not None:
            if int(new_id) <= self._highest_counter:
                # Lower counters are history navigation or a fresh session. Baseline
                # the new screen without forwarding an older shot.
                self._baseline = fingerprint
                self.last_reason = "Shot counter did not advance; current shot held"
                return None
            self._highest_counter = int(new_id)
        elif new_id != old_id:
            self._baseline = fingerprint
            if new_id is not None:
                self._highest_counter = max(self._highest_counter or 0, int(new_id))
            self.last_reason = "Shot counter visibility changed; current shot held"
            return None
        elif new_values == old_values:
            return None
        self._baseline = fingerprint
        self.last_reason = "Fresh stable shot detected"
        return reading.to_shot(now)


class OCRAdapter:
    """Worker-thread adapter. Callbacks run on this worker, never the UI thread.

    on_preview, if supplied, receives (RGB ndarray, Recognition). A callback must
    marshal its UI work to Qt. `ready` means a complete stable shot was observed;
    it does not assert physical Mevo connectivity. The message states the scope.
    """
    def __init__(self, config: dict, on_shot: Callable[[Shot], None],
                 on_status: Callable[[str, str], None], on_preview=None):
        self.config = dict(config)
        self.on_shot, self.on_status, self.on_preview = on_shot, on_status, on_preview
        self._stop = threading.Event()
        self._thread = None
        self._active = True
        self._lock = threading.Lock()
        self._epoch = 0
        self._status = None
        self.gate = FreshShotGate(self.config.get("stable_frames", 3), self.config.get("stable_seconds", .6))

    def _emit_status(self, state, message):
        value = state, message
        if self._status != value:
            self._status = value
            self.on_status(*value)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._status = None
        self.gate.reset()
        self._thread = threading.Thread(target=self._run, name="FS Golf OCR", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        with self._lock:
            self._epoch += 1
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)

    def set_active(self, active: bool):
        with self._lock:
            if self._active != bool(active):
                self._active = bool(active)
                self._epoch += 1

    def probe(self) -> dict:
        windows = discover_windows(self.config.get("executable"), self.config.get("window_title"))
        return {"windows": [{"title": w.title, "pid": w.pid, "hwnd": w.hwnd} for w in windows],
                "mode": self.config.get("mode", "automatic")}

    def _run(self):
        recognizer = LabelRecognizer(self.config)
        capture = None
        epoch = -1
        invalid_since = None
        try:
            while not self._stop.is_set():
                with self._lock:
                    current_epoch, active = self._epoch, self._active
                if epoch != current_epoch:
                    self.gate.reset()
                    epoch = current_epoch
                    invalid_since = None
                try:
                    if capture is None:
                        windows = discover_windows(self.config.get("executable"), self.config.get("window_title"))
                        if not windows:
                            self._emit_status("waiting", "Waiting for FS Golf. Open a practice session with the shot data view.")
                            self._stop.wait(1)
                            continue
                        capture = WindowCapture(windows[0])
                        self.gate.reset()
                    frame = capture.capture()
                    reading = recognizer.recognize(frame)
                    if self.on_preview:
                        self.on_preview(frame, reading)
                    with self._lock:
                        still_current = epoch == self._epoch
                    if not still_current or self._stop.is_set():
                        continue
                    shot = self.gate.observe(reading)
                    if not reading.valid:
                        if invalid_since is None:
                            invalid_since = time.monotonic()
                        elif time.monotonic() - invalid_since > 2:
                            # Long interruptions (popup, wrong page) require a new
                            # baseline when the readable screen returns.
                            self.gate.reset()
                        self._emit_status("needs_attention", reading.errors[0] if reading.errors else "Waiting for readable shot data.")
                    elif self.gate._baseline is not None:
                        invalid_since = None
                        message = "FS Golf shot fields verified. Waiting for a new shot."
                        if not reading.shot_id:
                            message = "FS Golf shot fields verified. Keep the live shot view open."
                        self._emit_status("ready" if active else "standby", message if active else "Mevo+ is standing by while the putter is selected.")
                    else:
                        self._emit_status("checking", "Checking FS Golf's current shot. Existing readings will not be replayed.")
                    if shot and active:
                        # Epoch is checked again because preview/status handlers may
                        # change the active source while this frame is processed.
                        with self._lock:
                            valid_epoch = epoch == self._epoch and self._active
                        if valid_epoch and not self._stop.is_set():
                            self.on_shot(shot)
                except CaptureError as error:
                    capture = None
                    self.gate.reset()
                    invalid_since = None
                    self._emit_status("needs_attention", str(error))
                except Exception as error:
                    LOG.exception("FS Golf acquisition failed")
                    self.gate.reset()
                    self._emit_status("needs_attention", str(error))
                    self._stop.wait(1)
                self._stop.wait(max(.15, float(self.config.get("poll_seconds", .35))))
        finally:
            recognizer.close()
