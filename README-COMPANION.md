# Mevo Companion

A native Windows companion for Mevo+ shots, original Springbok webcam putting, and GSPro.

The interface is C#/.NET WPF. The private integration worker and original putting executable are bundled: users do not install Python or .NET. The original Springbok ball-tracking algorithm and its settings are unchanged.

## Start here

1. Run `MevoCompanion.exe`, or install the per-user `MevoCompanionSetup.exe` package.
2. Follow **Setup** to locate GSPro and FS Golf, select the putting camera by name, and check FS Golf's shot data in **Play Mode**.
3. Open the original putting view. Align the camera and ball using Springbok's existing controls. Import your previous Springbok `config.ini` if you already have working tuning.
4. On the GSPro practice range, verify a swing, a chip, and a putt. The app records GSPro acknowledgements and asks you to confirm the results look correct.
5. Enable **Connect when GSPro opens**. Optionally enable **Start with Windows** to keep the companion in the tray. Thereafter, opening GSPro starts the connections. **Play** can also launch your saved GSPro executable.

For the chip check, use GSPro's top-left **ball-placement icon**, then click an off-green position within 20 yards of the pin. The tested Driving Range/Dynamic Holes slider stops at 20 meters (about 22 yards), so use ball placement to reach the automatic Chipping threshold. We verified this in Playground at 18.7 yards (a 19-yard flag). See the [official ball-placement guide](https://gspro.gitbook.io/gspro-knowledge-base/practice-main-menu/practice-range#ball-placement).

GSPro and FS Golf must be installed and licensed separately. GSPro must use its Open Connect interface. Your Mevo+ must be powered on and reachable, and the selected camera must be available. Initial physical camera alignment and practice-shot validation are required once; software cannot infer the location of your mat or camera mount.

## During play

- Mevo+ supplies full shots. Selecting the putter in GSPro switches to the original Springbok webcam tracker.
- In FS Golf's **Play Mode**, the companion automatically selects **Chipping** within 20 yards of the pin and **Full Swing** for longer shots. The putter always uses the webcam. Turn off **Automatically switch to Chipping within 20 yards** in Connections to choose the mode yourself. Imported legacy OCR profiles retain the manual **Testing a chip** option.
- **Pause** stops shot forwarding. Disconnects never replay queued shots.
- Closing the window keeps the tray connection alive. Choose **Quit** from the tray to release the camera and stop the companion. GSPro and FS Golf remain open.
- **Help** contains connection events and local diagnostic export. Nothing is uploaded automatically.

## FS Golf acquisition

The supported target is English FS Golf PC 2.0 on the same Windows PC. The native reader uses Windows accessibility controls to read the metric labels and values directly, without OCR boxes. Startup selects **Play Mode** from the recognized Home screen and preserves an existing live session. The reader verifies the selected Full Swing/Chipping mode, live controls and shot freshness. Play Mode's history stops at ten shots; the reader then recognizes a new tracking cycle and settled measurements instead of relying on an increasing counter. Keep the live shot data view available; saved-session reviews are excluded.

Open Connect receives readiness updates as the selected source becomes ready or unavailable. Start the companion before entering a GSPro practice session or round so it receives the initial club update. After reconnecting mid-session, Open Connect may require selecting your current club once in GSPro before shots can resume; the companion explains this when needed. Its displayed club can be stale, so that label is never used to guess the active shot source.

Existing Springbok device-region profiles can be imported as an explicit OCR compatibility option. That legacy mode has weaker freshness guarantees and requires keeping the live view open. A different FS Golf version, locale, or unsupported view needs validation before live use. Experimental direct FlightHook support is separate and is not included in this build.

Automatic chipping uses the installed GSPro/Open Connect version's distance-to-pin field, including on the practice range. That field is in meters, rounded to 0.1 m; the inclusive 20-yard boundary uses the corresponding 18.3 m reading. Missing or invalid distance leaves FS Golf's mode unchanged. Club/distance updates are briefly coalesced, and switching waits until the native reader is idle. The app verifies the selected mode before reporting Ready. This distance field is not specified in the public Open Connect V1 contract, so other GSPro versions need verification.

## Files and privacy

Settings, original putting configuration copies, and rotating logs live under `%LOCALAPPDATA%\MevoCompanion`. Imports leave the original Springbok profile untouched. The installer preserves this directory during upgrades and uninstall. The integration worker uses a private parent/child pipe; only the tracker callback listens on localhost port 8888, and GSPro uses its configured local endpoint (normally port 921).

`--demo` previews the interface without hardware, camera capture, or GSPro shot delivery. Demo activity cannot satisfy real setup validation.

## Build from source

Use Windows x64, Python 3.12, and .NET SDK 10. Install `requirements-companion.txt` into a virtual environment. Before building a fresh clone, run `python tools/fetch_springbok_putting.py`. This downloads the unchanged putting executable from the pinned [Springbok V1.04.51 release](https://github.com/springbok/MLM2PRO-GSPro-Connector/releases/download/V1.04.51/MLM2Pro-GSPro-Connector_V1_04_51.zip), verifies both the release ZIP and executable against `vendor/springbok-putting/provenance.json`, and writes only the executable. An existing executable is verified without downloading. If you downloaded that exact ZIP separately, pass `--archive <path-to-release.zip>` instead. The large executable is excluded from Git; its pinned source and small stock assets are included.

Run `python tools/build_companion.py --dotnet <path-to-dotnet.exe>`, then `packaging/build-installer.ps1` for the installer. `MevoCompanionEngine.spec` builds the private worker. See `docs/camera.md`, `docs/ocr-profile.md`, and `docs/installation.md` for component details.

This is an unsigned development build. The installed app has delivered a real Mevo+ full shot and chip accepted by GSPro. Webcam putting, perceived measurement accuracy, and unattended startup still need physical validation. Each installation retains the swing, chip, and putt setup checks.

Mevo Companion is GPL-3.0. Corresponding application source, exact Springbok putting provenance and source, and third-party notices accompany the package. This independent community application is not affiliated with FlightScope or GSPro.
