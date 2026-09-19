# Native Windows frontend

`MevoCompanion` is a .NET 10 WPF desktop application. Its separate integration
engine owns FS Golf acquisition, the GSPro socket and Springbok's original
putting process. The UI and engine communicate over private redirected
stdin/stdout with newline-delimited JSON; no control web server is opened.

Build with the workspace SDK:

```powershell
$env:DOTNET_CLI_HOME = "$PWD/.tooling/dotnet-home"
$env:NUGET_PACKAGES = "$PWD/.tooling/nuget"
./.tooling/dotnet/dotnet.exe build desktop/MevoCompanion/MevoCompanion.csproj -c Release --configfile desktop/NuGet.Config
```

Development launch needs `DOTNET_ROOT` set to `.tooling/dotnet`. The application
finds `MevoCompanionEngine.py` by walking up from its executable or working
directory, and uses the root `.venv/Scripts/python.exe`. Production uses
`engine/MevoCompanionEngine.exe` beside the frontend; publish the frontend
self-contained for machines without .NET.

Arguments:

- `--engine PATH`: explicit backend executable or root Python entrypoint.
- `--data-dir PATH`: independent engine settings directory.
- `--demo`: no physical hardware connections; cannot finish hardware validation.
- `--background`: hide in the tray after configured startup; first setup remains visible.
- `--screenshot PATH`: render the client UI to a PNG.
- `--smoke-test PATH`: verify backend IPC and navigation, save a JSON report and exit.
- `--page play|setup|connections|help`, `--step 1..4`: select a screenshot view.
- `--width N`, `--height N`, `--render-scale N`: viewport/render checks.

Run `desktop/scripts/smoke.ps1 -Build` to check every page, the four setup steps,
minimum supported window size and 125%/150% rendering. All smoke runs use demo
mode and an isolated workspace profile. They exercise actual engine responses
for status, connect, pause, resume and camera enumeration, and confirm demo mode
cannot mark physical setup complete. Screenshots/reports are under ignored
`desktop/artifacts`. This proves UI/IPC behavior, not physical shot acquisition.

The initial wizard does not forward shots until the user checks the explicit
GSPro practice-session checkbox. Completion is decided by the engine's accepted
full-shot/chip/putt evidence plus a successful Springbok preview putt and user
confirmation. Preview and settings controls retain the stock Springbok engine.

Single-instance activation uses a current-user named pipe. Closing a configured
app hides it in the tray; explicit Exit asks the engine to shut down. The engine
is responsible for releasing its camera/tracker without closing user-opened
GSPro or FS Golf. A hung engine is terminated alone, never by indiscriminately
killing its process tree.

HKCU Windows startup is changed only when the user explicitly saves a changed
startup checkbox or finishes validated setup with its visible startup preference.
The final setup step proposes starting quietly with Windows; the preference has
no effect until the user presses Finish. Loading settings, demos and smoke tests do not register it.
The saved command is the native app executable with `--background`.
