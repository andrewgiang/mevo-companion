# FS Golf automatic screen reader

This adapter reads the FS Golf client window without asking the user to place
rectangles. It is the primary compatibility path for the Mevo+ / FS Golf setup.
The current installation identified during development is FS Golf PC 2.0,
`FlightScopeVideoTeachingApp.exe`, file version 25.4.28.0. The user's device is a
first-generation Mevo+, reported firmware 0.43. These facts identify the target;
they are not a claim that physical shot capture has passed acceptance testing.

## Supported view

Use one live shot data panel with these English labels visible:

| Required reading | Recognized labels |
| --- | --- |
| Ball speed | Ball Speed |
| Horizontal launch | Launch Direction, Horizontal Launch, Horizontal Launch Angle, Lateral Launch, HLA |
| Vertical launch | Launch Angle, Vertical Launch, Vertical Launch Angle, VLA |
| Total spin | Spin Rate, Total Spin, Spin |
| Spin axis | Spin Axis |

Club Speed (including Clubhead Speed / Club Head Speed) is optional. Labels may
wrap onto two lines. Values may be above or below their label in cards, or beside
their label in a table. Labels and values are located on every image. Resizing
does not reuse old pixel coordinates. Multiple visible shot panels are rejected
rather than selecting an arbitrary shot.

The automatic profile reads MPH, km/h or m/s next to speed and converts explicitly
to MPH for GSPro. If FS Golf hides the units, setup must specify its unit. A
displayed unit conflicting with a saved choice pauses acquisition. Values must
be whole OCR tokens with sufficient confidence. L/R angle suffixes, signed
angles, decimals and five-digit spin are preserved. Ambiguous comma decimals,
letter lookalikes, incomplete metrics and implausible readings are rejected;
numbers are never silently repaired.

The shipped `eng.traineddata` is required for labels; upstream `mevo.traineddata`
alone is insufficient. When packaging, include English data beside the app's
bundled modules, or under `companion/resources/tessdata`. An explicit
`tessdata_path` can point to a directory containing `eng.traineddata`.

## Capture and readiness

Window discovery checks FlightScope's process identity, optionally narrowed to a
configured executable path/title. A browser page whose title mentions FS Golf
cannot be selected. Captures use `PrintWindow` in client coordinates with
64-bit-safe API signatures and release all GDI handles. The adapter never
restores, moves or focuses a window and never substitutes desktop pixels when a
covered window cannot be captured. Minimized, blank and missing windows produce
specific recovery messages.

`ready` means complete shot fields have been recognized over stable frames. Its
message says **FS Golf shot fields verified**. It does not establish a physical
Mevo connection, prove that a hidden window is still rendering live measurements,
or replace a practice-shot validation. App-level readiness should preserve this
distinction. Background rendering with GSPro foregrounded, minimize/restore,
display scaling and real hardware remain acceptance tests.

## Freshness and limitations

The first stable reading at startup is held as a baseline and never sent. A
minimum of three consistent readings over 0.6 seconds is required before a new
shot is emitted. Source changes, capture interruptions and window resizing
discard previous observations and require a new baseline. A prolonged unreadable
screen also discards the old baseline. A callback is gated by a source epoch so
a frame captured before switching to the putter cannot be sent afterward.

If a visible `Shot`, `Shot Number` or `Shot Count` counter is recognized, it must
advance above the highest observed number. This permits two physical shots with
identical displayed measurements. Browsing backwards and forwards through
previous counter values does not replay them. A session counter reset needs
adapter restart/revalidation; it is never interpreted as a new shot by itself.

When FS Golf exposes no counter, changed stable readings are the available
freshness evidence. **Identical consecutive shots cannot be distinguished, and
opening a historical shot can resemble a fresh shot.** Keep the live view open.
No timeout alone rearms an old reading. This limitation must not be described as
guaranteed physical-shot deduplication. UI automation or a digital event stream
would be required to close the gap completely.

Stable readings reduce partial-update risk but cannot prove atomic completion if
FS Golf updates individual values slowly. The real hardware fixture set must
include partial frames, startup data, shot navigation and identical shots before
shipping a claim of zero duplicates/stale submissions.

## Python integration

```python
adapter = OCRAdapter(config, on_shot, on_status, on_preview=None)
adapter.start()
adapter.set_active(False)  # webcam putting selected
adapter.set_active(True)   # discard old readings before rearming
adapter.stop()
```

Callbacks run on the capture worker. `on_status(state, message)` uses `waiting`,
`checking`, `needs_attention`, `ready`, and `standby`. `on_shot(Shot)` receives
monotonic capture time. `on_preview(rgb_array, recognition)` receives RGB pixels
and the parsed `Recognition`; marshal UI work onto Qt's main thread. `probe()`
returns matching windows without launching or modifying applications.

For offline fixtures, `LabelRecognizer(config).recognize(image)` returns values,
anchors, original OCR strings, errors, dimensions and optional shot counter.
Close the recognizer when done. `recognize_frame(image, config)` is a convenience
wrapper that manages its lifetime. `FreshShotGate` can be exercised with
deterministic timestamps separately from OCR and Windows APIs.

Configuration defaults: `mode="automatic"`, `speed_unit="auto"`,
`minimum_confidence=65`, `poll_seconds=0.35`, `stable_frames=3`,
`stable_seconds=0.6`. Optional keys: `executable`, `window_title`,
`tessdata_path`.

Advanced compatibility import requires explicit `mode="legacy"`, an explicit
speed unit, and `legacy_profile={"source_size": [width, height], "rois": ...}`.
Each ROI uses `[x, y, width, height]` under the metric keys in the shared Shot
model. The adapter scales these from the original client capture dimensions;
an import without original dimensions is rejected. This is not the normal
setup path and does not auto-import or alter the old connector's settings.

## Current automated evidence

Run `.venv/Scripts/python.exe -m pytest tests/test_ocr_adapter.py -q`.
The tests exercise real Tesseract on a generated FS-style image at 75%, 100% and
150%, explicit unit conversion, label wrapping, signed/suffixed angles,
five-digit spin, duplicate-panel rejection, low confidence, legacy coordinate
scaling, baseline suppression, stable transitions, identical numbered shots,
counter history and reset/resize behavior. These generated fixtures test the
implementation and do not substitute for screenshots from the installed app or
physical Mevo+ validation.

A read-only capture from the installed FS Golf window was also verified on the
user's Windows desktop at 1706 x 1066. The active Settings > Radar page was
correctly rejected as missing shot fields. This confirms discovery/capture and
wrong-screen handling; it does not yet validate recognition of the app's actual
live shot layout. Desktop capture must run in the signed-in Windows session;
sandboxed command sessions do not expose that desktop.
