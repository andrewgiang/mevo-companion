using System.IO;
using System.Runtime.InteropServices;
using Microsoft.Win32;

namespace MevoCompanion.Setup;

internal static class WindowsShell
{
    private const string UninstallKey = @"Software\Microsoft\Windows\CurrentVersion\Uninstall\MevoCompanion";
    private const string RunKey = @"Software\Microsoft\Windows\CurrentVersion\Run";
    private static string StartMenuLink => Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Programs), "Mevo Companion.lnk");
    private static string DesktopLink => Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory), "Mevo Companion.lnk");

    public static void Register(string root, string version, bool startMenu, bool desktop)
    {
        using var key = Registry.CurrentUser.CreateSubKey(UninstallKey);
        key.SetValue("DisplayName", "Mevo Companion");
        key.SetValue("DisplayVersion", version);
        key.SetValue("Publisher", "Mevo Companion community project");
        key.SetValue("InstallLocation", root);
        key.SetValue("DisplayIcon", Path.Combine(root, "MevoCompanion.exe"));
        key.SetValue("UninstallString", '"' + Path.Combine(root, "MevoCompanionSetup.exe") + "\" --uninstall");
        key.SetValue("NoModify", 1, RegistryValueKind.DWord);
        key.SetValue("NoRepair", 1, RegistryValueKind.DWord);
        if (startMenu) CreateShortcut(StartMenuLink, root);
        if (desktop) CreateShortcut(DesktopLink, root);
    }

    private static void CreateShortcut(string path, string root)
    {
        SafePaths.AssertNoReparseAncestors(Path.GetDirectoryName(path)!);
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        if (File.Exists(path) && !ShortcutBelongsTo(path, root))
            throw new IOException("An unrelated shortcut already uses the Mevo Companion name: " + path);
        object? shell = null, shortcut = null;
        try
        {
            shell = Activator.CreateInstance(Type.GetTypeFromProgID("WScript.Shell")!);
            dynamic target = shell!;
            shortcut = target.CreateShortcut(path);
            dynamic link = shortcut;
            link.TargetPath = Path.Combine(root, "MevoCompanion.exe");
            link.WorkingDirectory = root;
            link.Description = "Mevo Companion — GSPro and Springbok webcam putting";
            link.Save();
        }
        finally
        {
            if (shortcut is not null) Marshal.FinalReleaseComObject(shortcut);
            if (shell is not null) Marshal.FinalReleaseComObject(shell);
        }
    }

    private static bool ShortcutBelongsTo(string path, string root)
    {
        SafePaths.AssertNoReparseAncestors(path);
        object? shell = null, shortcut = null;
        try
        {
            shell = Activator.CreateInstance(Type.GetTypeFromProgID("WScript.Shell")!);
            dynamic target = shell!;
            shortcut = target.CreateShortcut(path);
            dynamic link = shortcut;
            return string.Equals(SafePaths.Canonical((string)link.TargetPath),
                SafePaths.Canonical(Path.Combine(root, "MevoCompanion.exe")), StringComparison.OrdinalIgnoreCase);
        }
        catch (Exception) { return false; }
        finally
        {
            if (shortcut is not null) Marshal.FinalReleaseComObject(shortcut);
            if (shell is not null) Marshal.FinalReleaseComObject(shell);
        }
    }

    public static void Unregister(string root)
    {
        foreach (var link in new[] { StartMenuLink, DesktopLink })
            if (File.Exists(link) && ShortcutBelongsTo(link, root)) File.Delete(link);
        using (var run = Registry.CurrentUser.OpenSubKey(RunKey, writable: true))
        {
            var command = run?.GetValue("MevoCompanion") as string;
            if (CommandBelongsTo(command, root)) run!.DeleteValue("MevoCompanion", false);
        }
        using var uninstall = Registry.CurrentUser.OpenSubKey(UninstallKey);
        var installed = uninstall?.GetValue("InstallLocation") as string;
        if (installed is not null && string.Equals(SafePaths.Canonical(installed), SafePaths.Canonical(root), StringComparison.OrdinalIgnoreCase))
            Registry.CurrentUser.DeleteSubKeyTree(UninstallKey, false);
    }

    private static bool CommandBelongsTo(string? command, string root)
    {
        if (string.IsNullOrWhiteSpace(command)) return false;
        var trimmed = command.Trim();
        var executable = trimmed.StartsWith('"') ? trimmed.Split('"').ElementAtOrDefault(1) : trimmed.Split(' ')[0];
        return !string.IsNullOrWhiteSpace(executable) && SafePaths.IsWithin(root, executable);
    }
}
