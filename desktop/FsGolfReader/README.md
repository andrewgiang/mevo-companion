# FS Golf native reader

This Windows UI Automation helper reads FS Golf PC 2's actual accessible values.
It does not require screen rectangles or OCR. `--once` / `--watch` observations
are read-only. Explicit startup and shot-mode commands act only through the
supported native controls and never request focus for FS Golf or use
coordinates.

```text
FsGolfReader.exe --once
FsGolfReader.exe --watch --interval-ms 350
FsGolfReader.exe --self-test
```

The owner must keep redirected stdin open for watch mode. EOF exits the process,
including when a provider call stalls, so a failed parent does not orphan readers.
Stdout contains one UTF-8 JSON object per line. Each snapshot includes:

- `type="snapshot"`, `ok`, acquisition `timestamp` in UTC, `duration_ms`.
- `window={pid,hwnd,title}` for identity changes and baseline resets.
- `live_context`, `radar_status`, `selected_shot`, `total_shots`, `counter_stable`.
- `capture_context` (`play_mode` or `full_swing_session`), `shot_mode`
  (`full_swing` or `chipping`), and `context_stable`. An unavailable Play Mode
  selection is represented by a null shot mode and never establishes readiness.
- `readings` under canonical keys `speed_mph`, `club_speed_mph`, `hla`, `vla`,
  `spin_rpm`, `spin_axis`, each `{text,unit}`. Parsing/conversion stays in Python.
- `evidence={auto_playback_present,player_change_enabled,quit_application_present,
  club_change_enabled,play_mode_selector_present}`.
- `error` when a view is unavailable/unsupported. No measurements are guessed.

Values come from each ParamModel/DataItem's `ValueTextBlock`. Recognized labels
include Ball Speed, Club Speed, Spin, Spin Axis, Launch V and Launch H. Device
status comes from `OperatorRadarControl`. Live context requires the visible
`AutoPlaybackButton` and no visible Quit Application control. Play Mode also
requires an enabled "Click to change club" button and the observed native List
containing enabled, visible Full Swing and Chipping options, with exactly one
selected through UI Automation. Existing Full Swing Sessions remain supported
through their enabled "Click to change player" control. A "Ready" label by itself
cannot make a review/history page live. Play Mode does not expose a player-change
button and does not need one.

Only recognized metric panels are traversed; unrelated virtualized shot-history
items are excluded. Selection state is requested only from the two known mode
options, rather than every descendant in the window. During a control rebuild,
the managed element-unavailable exception or native `UIA_E_ELEMENTNOTAVAILABLE`
HRESULT triggers at most two fresh-root retries, 100 ms apart. Partial snapshots
are discarded. A successful retry uses its own acquisition timestamp and duration;
persistent or other failures still reset acquisition through the parent adapter.

The current shot and `of N` text can be side by side or stacked. Counters are
checked again after the metric scan. Changes mark the snapshot `ok=false`,
`counter_stable=false`, `error="transient update"`; consumers should wait for a
coherent next observation while retaining their baseline. The mode selector is
checked again after the scan. A mid-scan mode change marks `context_stable=false`
and discards the baseline. The parent includes the capture context and selected
mode in its identity: switching Full Swing/Chipping holds any displayed old shot,
then accepts only the next new shot in the selected mode. Other session/source
changes and stale snapshot handling also belong to the parent adapter.

Play Mode's live buffer stops at ten shots. At an unchanged ordinal, freshness
therefore requires an observed Ready baseline followed by Tracking with every
required metric explicitly cleared to `-`. After that cycle, two matching complete
native readings over at least half a second prove the measurement; Connected or
Arming can expose those values before the radar is Ready for the next shot.
Readiness and measurement delivery are separate. Arming alone, mode changes, and
changed values never create a new shot. Before the cap, a new latest ordinal is
also valid freshness evidence when Tracking fell between polls.

The acquisition cycle expires after eight seconds; incomplete or changing
completed fields expire after three seconds. The first complete observation is
the shot's capture timestamp and is never refreshed by settling retries. A
separate `capture_started_at` preserves the Tracking/source-change fence. The
sanitized actual chip trace in `tests/fixtures/fs_play_mode_chip_cycle.json`
verifies that the measured chip passes both this gate and the GSPro router.

## Explicit shot-mode commands

In watch mode, the owner can write one JSON command per stdin line:

```json
{"type":"set_shot_mode","mode":"chipping","request_id":"mode-1"}
```

The other accepted mode is `full_swing`. The owner decides the desired mode
from the current GSPro state; this helper does not infer a distance threshold.
Commands are parsed on the stdin reader and applied on the main acquisition
thread between scans. Only a verified live Play Mode session displaying its
latest shot is eligible. The helper rechecks the exact Full Swing/Chipping list,
window, current selection, modal state, sleep overlay, and radar Ready status
immediately before one `SelectionItemPattern.Select` attempt. Tracking, Arming,
Connected, Sleeping, review, unknown views, and dialogs do not permit a switch.
An already selected mode succeeds without another selection action.

FS Golf raises its own window when its mode changes. The helper records the
foreground window (normally GSPro) just before selecting and, for three seconds
afterwards, returns focus to it if FS Golf takes the foreground. It stops
watching as soon as the golfer switches to any other window.

Each response has `type="command_result"`, `request_id`, `success`, `mode`, and
`message`. Success means the selection was issued or already matched; the next
ordinary snapshot remains authoritative confirmation. A failed/busy request may
be retried by the owner after another Ready snapshot, with a **new request id**.
The helper remembers its last 32 command results to prevent duplicate selection
attempts, queues at most eight commands, and expires requests older than five
seconds. It performs no automatic action retries. EOF still exits immediately.

## Explicit automatic session preparation

`--ensure-live` is an optional, one-time action before a snapshot/watch. It may
invoke only the observed FS Golf PC 2 controls:

1. Home: requires ContentFrame, disabled Home button, Play Mode and Review
   Session buttons. Invoke only Play Mode, including its observed `Play\r\nMode`
   wrapped label.
2. A proven live screen showing the exact "Radar is in Sleep Mode" overlay:
   invoke its Wake button once, then wait for Ready. The sleep overlay outranks
   a temporarily stale Ready header.

It uses existing setup choices, refuses owned modal dialogs, never leaves an
unrecognized/review screen and does nothing when live context already exists.
Generic Session Setup is held rather than opening a Full Swing Session; it has
not been identified as part of the Play Mode startup path.
No generic Next/Continue or popup dismissal is used. The output's optional
`startup_status` is `already_live`, `prepared`, or `action_needed`.

The parent should enable this only for the user's opted-in session startup,
invoke it once per startup and display an actionable failure without repeatedly
creating sessions. The helper emits separate `startup_progress` messages during
bounded waits, so a slow startup does not trip the owner's normal read watchdog.

## Build and current evidence

Publish self-contained for redistribution:

```powershell
./.tooling/dotnet/dotnet.exe publish desktop/FsGolfReader/FsGolfReader.csproj -c Release -r win-x64 --self-contained true -p:PublishSingleFile=true -p:IncludeNativeLibrariesForSelfExtract=true -o dist/MevoCompanion/reader --configfile desktop/NuGet.Config
```

The packaged helper was verified against the installed live session: Ready /
Limited Flight, current 0 of 0, all six fields available as not-yet-measured
placeholders. Three consecutive scans took 269, 206 and 161 milliseconds; stdin
closure exited with code 0. `--self-test` covers metric/unit labels, component-spin
exclusion, counter parsing and conservative startup-view classification. Physical
shot measurement and review-to-live transitions are validated by the app-level
hardware tests; placeholder discovery does not prove numeric shot accuracy.

`--inspect` optionally adds control metadata for targeted local debugging. Do not
enable it in normal logging or package captures from a user's device.
