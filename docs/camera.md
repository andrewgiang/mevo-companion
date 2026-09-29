# Original Springbok webcam putting

Mevo Companion runs the **unmodified** `ball_tracking.exe` bundled with Springbok
MLM2PRO-GSPro-Connector V1.04.51. Ball detection, speed, direction, colors, camera
controls, and calibration are the original experience. No MevoPutt component or
replacement tracking algorithm is used.

The binary is cam-putting-py 1.6, commit
`805ab753aaa1e89d2938679a0ee44866ada0fa45`. Both of its project code modules were
extracted and compared with compiled source: recursive Python code-object equality
passed for `ball_tracking.py` and `ColorModuleExtended.py`. Corresponding source,
build specification, dependency list, GPLv3 license, source verifier, and provenance
are included under `vendor/springbok-putting`.

## Setup

1. Select the camera by name. The app records its device path and resolves the
   current OpenCV index using the same Windows backend as the stock tracker.
2. Open the original putting view. Position the camera beside and above the
   putting area, align its white line with the target, and put the ball inside the
   yellow start rectangle. The red detected circle must match the ball's diameter.
3. Use **Original camera settings**, or press **A** in the native view. The original
   settings control the start area, ball radius, flip, MJPEG, FPS, darkness, and
   camera properties. Press **D** for the original color-mask/HSV tuning.
4. Make a practice putt across and out of the red detection gate. Setup callbacks
   are displayed as preview evidence; they cannot be submitted to GSPro.

Stock changes remain in the per-user putting `config.ini`. A working Springbok
configuration can be imported unchanged; the previous file is backed up. A readable
configuration alone is not evidence of a calibrated ball/camera. A successful
practice putt and user observation remain necessary. The optional stock
`-c calibrate` mode is a color comparison recording, not a metric calibration.

## Session integration and recovery

The adapter starts the original executable with `-c <color> -w <index> -r <width>`
and supplies its splash asset and replay directories in a dedicated writable folder.
It listens only on `127.0.0.1:8888`, the stock hard-coded address. Close another
connector using that port before starting a session.

The exact callback is `POST /putting` with JSON
`{"ballData":{"BallSpeed":"2.42","TotalSpin":0,"LaunchDirection":"-1.37"}}`.
Speed is mph and launch direction is degrees. Values are preserved, validated,
and forwarded only when the coordinator arms webcam putting. Source switches and
setup must remain gated in the coordinator as well. An identical callback within
350 ms is treated as a retry because the stock protocol has no shot identifier.

The adapter owns only the process it starts. It reconnects the saved device after
hotplug and does not silently substitute another camera if indices change. Stock
MJPEG mode uses DirectShow; non-MJPEG mode uses Media Foundation enumeration.
Virtual cameras without a stable path are identified by name, and duplicate names
require user selection. Changing physical USB ports can change the device path.

The native putting view stays running behind GSPro and comes forward without
stealing focus when the putter is selected. A read-only capture of this **owned**
window checks for the stock "Error: No Frame" banner and startup splash; process
existence alone never marks it healthy. Actual FPS and detection remain displayed
by the original tracker. There is no external heartbeat or API for changing the
stock tracker's exposure/calibration settings.

## Camera exposure mode

In MJPEG (DirectShow) mode, the stock tracker cannot keep a camera on auto
exposure by itself. Its OpenCV 4.7 DirectShow backend can set
`CAP_PROP_AUTO_EXPOSURE` but always reads it as `-1`. The tracker saves that value
as `autoexposure = -1.0` and replays it at the next launch. DirectShow treats any
value other than `1` as "switch to manual exposure", so the camera would leave
auto exposure every time putting opened.

Before each launch, the adapter reads the saved camera's current exposure mode
through DirectShow's `IAMCameraControl`. This read does not open a video stream.
When the camera is on auto exposure, the adapter sets `autoexposure = 1.0` so the
tracker restores auto. When it is on manual exposure, the adapter keeps the stock
manual value. No other setting is changed. If the mode can't be read, or the
tracker uses Media Foundation (MJPEG off), the file is left untouched. Media
Foundation already reads and restores the flag correctly.

## Verified on this development machine

- Automated tests cover the HTTP contract, malformed input,
  port conflicts/release, identity remapping, setup/live gating, duplicate callbacks,
  unchanged unit values, and native command flags. Tests also cover view
  health and importing unchanged native configuration.
- The bundled executable's `-h` was run successfully.
- The adapter opened the detected **HD USB Camera** with default stock MJPEG=1,
  displayed a live native putting view, and shut down its owned processes cleanly.
  Windows camera access was blocked inside the development sandbox; the same test
  passed with approved normal desktop access.
- The packaged engine also opened the original camera view in 5.09 seconds in an
  isolated setup profile, with live delivery disabled and zero shots. Its first
  setup command responded in 0.61 seconds; shutdown left no owned child processes.
- No physical putt was made during these checks. Ball accuracy, lighting, camera
  alignment, and real GSPro routing still require the user's hardware practice test.

Upstream: https://github.com/springbok/MLM2PRO-GSPro-Connector and
https://github.com/alleexx/cam-putting-py/tree/1.6
