using System.Diagnostics;
using System.IO;
using System.Reflection;

namespace MevoCompanion.Setup;

internal sealed class InstallService
{
    public static string ProgramsRoot => Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Programs");
    public static string DefaultTarget => Path.Combine(ProgramsRoot, "MevoCompanion");
    public static string BackupTarget => Path.Combine(ProgramsRoot, "MevoCompanion.previous");
    public const string Version = "0.1.0";
    public static bool HasPayload => Assembly.GetExecutingAssembly().GetManifestResourceNames().Contains("MevoCompanion.Payload.zip");

    private static void VerifyTarget(string path, bool staging = false)
    {
        var full = SafePaths.Canonical(path);
        SafePaths.AssertWithin(ProgramsRoot, full);
        var name = Path.GetFileName(full);
        if (!string.Equals(full, SafePaths.Canonical(DefaultTarget), StringComparison.OrdinalIgnoreCase)
            && !string.Equals(full, SafePaths.Canonical(BackupTarget), StringComparison.OrdinalIgnoreCase)
            && !(staging && name.StartsWith(".MevoCompanion-stage-", StringComparison.Ordinal)
                 && Guid.TryParseExact(name[".MevoCompanion-stage-".Length..], "N", out _)
                 && Path.GetDirectoryName(full) == SafePaths.Canonical(ProgramsRoot)))
            throw new IOException("Setup refused an unexpected installation folder.");
        SafePaths.AssertNoReparseAncestors(full);
    }

    public static bool HasPreviousVersion
    {
        get
        {
            try { VerifyTarget(BackupTarget); InstallManifest.Read(BackupTarget); return true; }
            catch { return false; }
        }
    }

    private static void EnsureNotRunning()
    {
        foreach (var process in Process.GetProcesses())
        {
            using (process)
            {
                if (process.Id == Environment.ProcessId) continue;
                try
                {
                    var name = process.ProcessName;
                    if (!name.Equals("MevoCompanion", StringComparison.OrdinalIgnoreCase)
                        && !name.Equals("MevoCompanionEngine", StringComparison.OrdinalIgnoreCase)
                        && !name.Equals("ball_tracking", StringComparison.OrdinalIgnoreCase)) continue;
                    var executable = process.MainModule?.FileName;
                    if (executable is null || SafePaths.IsWithin(DefaultTarget, executable))
                        throw new IOException("Close Mevo Companion and its putting window before continuing. Your round and settings will be kept.");
                }
                catch (System.ComponentModel.Win32Exception)
                {
                    throw new IOException("Setup could not check a running golf connector. Close Mevo Companion and its putting window, then try again.");
                }
                catch (InvalidOperationException) { /* A process exited during enumeration. */ }
            }
        }
    }

    public string Install(bool startMenu, bool desktop, Action<double> progress)
    {
        if (!HasPayload) throw new IOException("This development build has no installation package.");
        VerifyTarget(DefaultTarget);
        VerifyTarget(BackupTarget);
        EnsureNotRunning();
        if (Directory.Exists(DefaultTarget)) InstallManifest.Read(DefaultTarget);
        if (Directory.Exists(BackupTarget)) InstallManifest.Read(BackupTarget);
        Directory.CreateDirectory(ProgramsRoot);
        var staging = Path.Combine(ProgramsRoot, ".MevoCompanion-stage-" + Guid.NewGuid().ToString("N"));
        VerifyTarget(staging, staging: true);
        var movedPrevious = false;
        var committed = false;
        try
        {
            using (var payload = Assembly.GetExecutingAssembly().GetManifestResourceStream("MevoCompanion.Payload.zip")!)
                ArchivePayload.Extract(payload, staging, value => progress(value * 0.8));
            File.Copy(Environment.ProcessPath!, Path.Combine(staging, "MevoCompanionSetup.exe"), true);
            InstallManifest.Create(staging, Version);
            progress(0.85);
            EnsureNotRunning();
            if (Directory.Exists(BackupTarget))
            {
                if (InstallManifest.HasUnownedFiles(BackupTarget))
                    throw new IOException("The previous-version folder contains files added outside Setup. Move those files elsewhere before updating: " + BackupTarget);
                InstallManifest.RemoveOwnedFiles(BackupTarget);
            }
            if (Directory.Exists(DefaultTarget))
            {
                // Re-check immediately before moving; never move an unverified
                // computed path or a tree containing a junction.
                VerifyTarget(DefaultTarget);
                SafePaths.EnumerateFiles(DefaultTarget);
                Directory.Move(DefaultTarget, BackupTarget);
                movedPrevious = true;
            }
            VerifyTarget(staging, staging: true);
            VerifyTarget(DefaultTarget);
            Directory.Move(staging, DefaultTarget);
            committed = true;
            progress(0.95);
        }
        catch
        {
            if (movedPrevious && !Directory.Exists(DefaultTarget))
            {
                VerifyTarget(BackupTarget);
                VerifyTarget(DefaultTarget);
                Directory.Move(BackupTarget, DefaultTarget);
            }
            throw;
        }
        finally
        {
            if (Directory.Exists(staging)) CleanupStaging(staging);
        }
        if (!committed) throw new IOException("Installation did not complete.");
        // Shell integration failure cannot corrupt the now-complete application.
        var message = "Installed. Your saved equipment and calibration settings are kept.";
        try { WindowsShell.Register(DefaultTarget, Version, startMenu, desktop); }
        catch (Exception error) { message += " The application is installed, but Windows shortcuts could not be updated: " + error.Message; }
        progress(1);
        return message;
    }

    private static void CleanupStaging(string staging)
    {
        VerifyTarget(staging, staging: true);
        // The random empty staging folder was created by this run. Enumerate
        // safely and delete individual files; never recurse through reparse points.
        foreach (var file in SafePaths.EnumerateFiles(staging)) File.Delete(file);
        SafePaths.DeleteEmptyDirectories(staging);
    }

    public string Uninstall()
    {
        VerifyTarget(DefaultTarget);
        VerifyTarget(BackupTarget);
        EnsureNotRunning();
        InstallManifest.Read(DefaultTarget);
        SafePaths.EnumerateFiles(DefaultTarget);
        if (Directory.Exists(BackupTarget))
        {
            InstallManifest.Read(BackupTarget);
            SafePaths.EnumerateFiles(BackupTarget);
        }
        WindowsShell.Unregister(DefaultTarget);
        var completelyRemoved = InstallManifest.RemoveOwnedFiles(DefaultTarget);
        if (Directory.Exists(BackupTarget))
        {
            InstallManifest.Read(BackupTarget);
            completelyRemoved &= InstallManifest.RemoveOwnedFiles(BackupTarget);
        }
        return completelyRemoved
            ? "Mevo Companion was removed. Your equipment, calibration and diagnostic settings are kept for a future install."
            : "Mevo Companion was removed. Files you added to its folder and all saved equipment settings were kept.";
    }

    public string Rollback()
    {
        VerifyTarget(DefaultTarget);
        VerifyTarget(BackupTarget);
        EnsureNotRunning();
        InstallManifest.Read(DefaultTarget);
        var previous = InstallManifest.Read(BackupTarget);
        SafePaths.EnumerateFiles(DefaultTarget);
        SafePaths.EnumerateFiles(BackupTarget);
        var swap = Path.Combine(ProgramsRoot, ".MevoCompanion-stage-" + Guid.NewGuid().ToString("N"));
        VerifyTarget(swap, staging: true);
        Directory.Move(DefaultTarget, swap);
        try { Directory.Move(BackupTarget, DefaultTarget); }
        catch { Directory.Move(swap, DefaultTarget); throw; }
        Directory.Move(swap, BackupTarget); // Keep the replaced version for recovery.
        WindowsShell.Register(DefaultTarget, previous.Version, startMenu: true, desktop: false);
        return "The previous app version is restored. Your saved settings are unchanged.";
    }

    public static void LaunchApplication()
    {
        VerifyTarget(DefaultTarget);
        InstallManifest.Read(DefaultTarget);
        Process.Start(new ProcessStartInfo(Path.Combine(DefaultTarget, "MevoCompanion.exe"))
        {
            UseShellExecute = false, WorkingDirectory = DefaultTarget,
        });
    }
}
