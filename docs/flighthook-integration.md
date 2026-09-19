# Direct Mevo+ adapter

This is an optional experimental acquisition path. The default product uses FS
Golf OCR and the existing Springbok putting integration, per the user's choice.
This module provides no putting tracker and must not enable direct mode implicitly.

The companion starts its own headless FlightHook process and consumes its Flight
Relay Protocol (FRP) 0.1.0 event stream. It does not configure FlightHook's GSPro
actor: the companion's coordinator is the only GSPro connection owner. FS Golf
must release the Mevo+ connection before this direct mode starts.

## Provenance and tested boundary

Source: <https://github.com/divotmaker/flighthook>, revision
`9f3d817d3dabcd18b9e05fb259c22856208d5573`, application version 0.1.11,
MIT OR Apache-2.0. Mevo acquisition uses the Rust `ironsight` 0.2.1 dependency.
Relevant upstream source files:

- `app/src/actors/mevo/mod.rs`: handshake, auto-arm, reconnect, shot lifecycle.
- `app/src/actors/mevo/settings.rs`: physical setup and detection-mode mapping.
- `app/src/actors/web/ws.rs`: version negotiation and cached telemetry replay.
- `app/src/actors/gspro/mapper.rs`: FRP-to-GSPro units and lateral sign conversion.
- `lib/src/config.rs`: complete configuration schema.
- `docs/devices/flightscope.md`: hardware, estimates, Fusion limitations.

The existing local MevoPutt package includes a built Windows binary, upstream
licenses, source revision lock and `flighthook-companion.patch`. The patch enables
headless builds without WASM assets and fixes fragmented GSPro TCP responses. The
companion uses no FlightHook GSPro connection, but preserve the patch and licenses
when redistributing that binary. Build command from that package:
`cargo build -p flighthook-app --release --no-default-features --locked`.

The bundled executable was run on this Windows PC using a mock launch monitor,
a private loopback port and no GSPro bridge. Its REST status/configuration API,
mode mutation, FRP handshake, readiness transitions and complete shot event stream
were verified. This does not validate the user's first-generation Mevo+ with
firmware 0.43, physical chip performance, purchased capabilities, or ball-flight
accuracy. `camera_mode=standard` is the upstream default, not a claim of firmware
0.43 hardware validation. Hardware practice acceptance remains required.

The adapter itself was also run against the bundled executable with a deliberately
unavailable loopback Mevo endpoint. It established FRP, stayed unready, emitted no
shots and stopped its own child and worker cleanly. Forty-three unit tests cover
complete lifecycle, stale/reconnect rejection, identity, source/mode changes,
strict measurement conversion, private configuration and process ownership.

## Adapter configuration

`FlightHookAdapter(config, on_shot, on_status)` exposes `start()`, `stop()`,
`set_mode("full" | "chipping")`, `ready` and `status`. Callbacks run on a worker
thread; a Qt consumer must marshal them through signals. Runtime dependency:
`websocket-client>=1.8,<2`.

| Key | Default | Meaning |
| --- | --- | --- |
| `vendor_path` | required | Bundled `flighthook.exe` absolute path |
| `work_dir` | `%LOCALAPPDATA%/MevoCompanion/bridge` | Private generated TOML and bridge log |
| `mevo_address` | `192.168.2.1:5100` | Mevo Wi-Fi TCP endpoint |
| `mevo_range_ft` | 8 | Radar-to-ball distance in feet |
| `tee_height_in` | 1.5 | Tee height in inches |
| `surface_height_in` | 0 | Surface height in inches (0–10) |
| `ball_type` | 0 | 0 = RCT, 1 = standard |
| `track_pct` | 80 | Upstream tracking percentage (0–100) |
| `camera_mode` | `standard` | `standard`, `fusion`, `raw_fusion` |
| `use_estimated` | false | Request E8 fallback events; incomplete data is still rejected |
| `max_shot_age_s` | 10 | Maximum local trigger-to-completion age (1–30 seconds) |

Statuses are `connecting`, `ready`, `detecting`, `reconnecting`, `rejected`,
`action_needed`, `stopped`. Only `ready` means Mevo reported an armed measurement
state. A `rejected` event is a shot result, not proof of permanent disconnection.

The adapter preserves UUID identity and captures monotonic time at the observed
trigger. Ball speed, launch elevation, launch azimuth, backspin and sidespin are
required. It does not fabricate missing spin or identify duplicates by numerical
equality. Chip estimates lacking sidespin cannot be forwarded even when the user
enables `use_estimated`; the UI reports the rejection. Upstream may deliver only
E8 estimates for some short chips, so this restriction must be measured in the
user's physical practice session rather than hidden.

FRP uses physical target-relative lateral signs. Upstream's GSPro mapper changes
HLA and sidespin polarity for left-handed GSPro players. The adapter preserves
original signs and sets `Shot.raw["gspro_mirror_for_left_handed"] = True`; the
single GSPro owner must negate HLA and spin axis exactly once for a known LH
player. This marker is specific to this source and is not a universal webcam rule.

## Runtime boundaries

- Dynamically chosen loopback API port, validated against a unique process name.
- No webcam input listener, GSPro actor, history replay or default wedge-to-chip rule.
- WebSocket failure restarts only the process the adapter created, clearing pending shots.
- Device reconnect, source mode change or identity change invalidates unfinished shots.
- Mode changes require the requested mode event followed by unready then ready.
- The adapter cannot associate Windows Wi-Fi, prove physical alignment, or verify GSPro
  displayed a shot. Those are coordinator and guided practice responsibilities.
- The configured bridge log is local and includes connection and shot measurements.
