using System.IO;
using System.Text.Json;

namespace MevoCompanion.Setup;

internal sealed record InstallManifest(string Application, string Version, DateTime InstalledUtc, string[] Files)
{
    public const string ApplicationId = "org.mevocompanion.windows";
    public const string FileName = ".mevo-companion-install.json";

    public static InstallManifest Read(string root)
    {
        SafePaths.AssertNoReparseAncestors(root);
        var path = Path.Combine(root, FileName);
        SafePaths.AssertNoReparseAncestors(path);
        if (!File.Exists(path) || new FileInfo(path).Length > 8 * 1024 * 1024)
            throw new IOException("This folder is not an installation managed by Mevo Companion Setup.");
        var manifest = JsonSerializer.Deserialize<InstallManifest>(File.ReadAllText(path));
        if (manifest?.Application != ApplicationId || manifest.Files is null || manifest.Files.Length > 50000)
            throw new IOException("The installation ownership record is invalid.");
        foreach (var file in manifest.Files) SafePaths.AssertWithin(root, Path.Combine(root, SafePaths.RelativePath(file)));
        return manifest;
    }

    public static void Create(string root, string version)
    {
        var files = SafePaths.EnumerateFiles(root)
            .Select(file => Path.GetRelativePath(root, file))
            .Where(file => file != FileName).Order(StringComparer.OrdinalIgnoreCase).ToArray();
        var manifest = new InstallManifest(ApplicationId, version, DateTime.UtcNow, files);
        File.WriteAllText(Path.Combine(root, FileName), JsonSerializer.Serialize(manifest, new JsonSerializerOptions { WriteIndented = true }));
    }

    public static bool RemoveOwnedFiles(string root)
    {
        var manifest = Read(root);
        // Preflight every ancestor before deleting any file. Never follow a
        // junction inserted after installation, even for an owned filename.
        foreach (var file in manifest.Files)
        {
            var target = Path.Combine(root, SafePaths.RelativePath(file));
            SafePaths.AssertWithin(root, target);
            SafePaths.AssertNoReparseAncestors(target);
        }
        foreach (var file in manifest.Files)
        {
            var target = Path.Combine(root, SafePaths.RelativePath(file));
            if (File.Exists(target)) File.Delete(target);
        }
        File.Delete(Path.Combine(root, FileName));
        SafePaths.DeleteEmptyDirectories(root);
        return !Directory.Exists(root);
    }

    public static bool HasUnownedFiles(string root)
    {
        var manifest = Read(root);
        var owned = new HashSet<string>(manifest.Files.Select(SafePaths.RelativePath), StringComparer.OrdinalIgnoreCase) { FileName };
        return SafePaths.EnumerateFiles(root).Any(file => !owned.Contains(Path.GetRelativePath(root, file)));
    }
}
