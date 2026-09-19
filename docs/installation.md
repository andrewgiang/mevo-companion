# Installing Mevo Companion

The release artifact is `MevoCompanionSetup.exe`. Open it, keep the Start menu
option selected, optionally add a desktop shortcut, then choose **Install
companion**. Setup installs for the signed-in Windows account at
`%LOCALAPPDATA%\Programs\MevoCompanion`; it does not require administrator access.
Open Mevo Companion after installation to finish the guided equipment check.

The installer includes the desktop application and its connection engine. GSPro,
FS Golf, and camera drivers remain separately installed. Setup does not change
their application settings. The companion's settings, putting calibration and
logs live separately under `%LOCALAPPDATA%\MevoCompanion` and are kept during
install, update, rollback and uninstall.

This build is **unsigned** because no code-signing certificate is available.
Windows may show an unknown-publisher or SmartScreen prompt. A SHA-256 sidecar is
produced for each installer, but a hash is not a publisher signature. Hardware
acceptance remains separate from successful installation.

## Updating and rollback

Close Mevo Companion and its putting window before running a newer Setup. Setup
refuses to replace a running installation. It first validates and extracts the
whole new package to a private staging folder on the same drive, then moves the
current app to `MevoCompanion.previous` and places the new version at the normal
path. A failure during replacement restores the old app path.

One previous version is retained. On the next update, Setup replaces that older
backup only when it contains installer-owned files. If files were manually added
there, Setup asks you to move those files first. Use **Restore previous version**
in Setup to swap the app with its retained previous version. Settings remain in
place; rollback does not downgrade or erase equipment configuration.

## Uninstalling

Use **Windows Settings → Apps → Installed apps → Mevo Companion → Uninstall**, or
run `MevoCompanionSetup.exe --uninstall`. A confirmation window explains what will
be kept; choosing **Uninstall** removes the app's recorded installation files,
its own Start menu/desktop shortcuts, its own Windows startup entry and retained
app backup. Files you manually added to the app folder are preserved. All saved
equipment settings and calibration are preserved.

The installed uninstaller temporarily copies itself to an app-named file in the
Windows temporary folder before displaying its confirmation. This lets Windows
release the installed executable so it can be removed. The temporary helper may
remain until normal Windows temporary-file cleanup.

## Building the installer

First build the real desktop/engine package with `tools/build_companion.py`.
The ZIP must contain a single `MevoCompanion/` root, including
`MevoCompanion.exe` and `engine/MevoCompanionEngine.exe`.

From the repository, using the bundled .NET 10 SDK:

```powershell
& .\packaging\build-installer.ps1
```

Output: `dist\installer\MevoCompanionSetup.exe` and its `.sha256` file. The build
embeds the complete ZIP and publishes a self-contained Windows x64 executable;
the user does not need .NET or Python installed. `-PayloadPath` and `-Dotnet` can
override build inputs. CLI home and NuGet cache are scoped to repository tooling
directories by the script.

Without a ready release payload, compile only a clearly named disabled smoke
build:

```powershell
& .\packaging\build-installer.ps1 -Smoke
```

It produces `artifacts\installer-smoke\MevoCompanionSetup.Smoke.exe`, with the
Install button disabled and a development-build message. A normal build fails
when the real ZIP is missing; a test fixture is never substituted as a release.

## Verification

The standalone filesystem test harness uses benign and malicious ZIP fixtures
inside a supplied workspace subdirectory. It has no installation, registry or
shortcut code:

```powershell
$env:DOTNET_CLI_HOME = "$PWD\.tooling\installer-dotnet-home"
$env:NUGET_PACKAGES = "$PWD\.tooling\installer-nuget"
& .\.tooling\dotnet\dotnet.exe run --project .\packaging\Installer.Tests -- .\artifacts\installer-tests
```

It checks traversal, absolute paths, alternate data streams, reserved names,
case-insensitive duplicate names, symbolic-link ZIP entries, invalid payloads,
ownership manifests and preservation of unrelated files. Extraction and file
operations reject junctions/reparse points in the target path. Installed-app
replacement and Windows shell registration still require a separate clean-PC
acceptance test before declaring the release installer validated end to end.
