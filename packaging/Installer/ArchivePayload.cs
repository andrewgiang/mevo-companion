using System.IO;
using System.IO.Compression;

namespace MevoCompanion.Setup;

internal static class ArchivePayload
{
    private const long MaximumExpandedSize = 4L * 1024 * 1024 * 1024;

    public static void Extract(Stream payload, string destination, Action<double>? progress = null)
    {
        SafePaths.AssertNoReparseAncestors(destination);
        if (Directory.Exists(destination) && Directory.EnumerateFileSystemEntries(destination).Any())
            throw new IOException("The staging folder must be empty.");
        using var archive = new ZipArchive(payload, ZipArchiveMode.Read, leaveOpen: true);
        if (archive.Entries.Count > 50000) throw new IOException("The package contains too many files.");
        var names = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        var files = new List<(ZipArchiveEntry Entry, string Name)>();
        long total = 0;
        foreach (var entry in archive.Entries)
        {
            var unixType = ((uint)entry.ExternalAttributes >> 16) & 0xF000;
            if (unixType == 0xA000 || ((uint)entry.ExternalAttributes & (uint)FileAttributes.ReparsePoint) != 0)
                throw new IOException("The package contains a symbolic link.");
            var original = entry.FullName.Replace('\\', '/');
            var isDirectory = original.EndsWith('/');
            original = original.TrimEnd('/');
            if (original == "MevoCompanion" && isDirectory) continue;
            if (!original.StartsWith("MevoCompanion/", StringComparison.Ordinal))
                throw new IOException("The package must contain the MevoCompanion folder.");
            var name = SafePaths.RelativePath(original["MevoCompanion/".Length..]);
            var fullPath = Path.GetFullPath(Path.Combine(destination, name));
            SafePaths.AssertWithin(destination, fullPath);
            if (!names.Add(name)) throw new IOException("The package contains duplicate filenames.");
            if (isDirectory) continue;
            total = checked(total + entry.Length);
            if (entry.Length < 0 || total > MaximumExpandedSize)
                throw new IOException("The expanded package exceeds the installation size limit.");
            files.Add((entry, name));
        }
        if (!files.Any(item => item.Name.Equals("MevoCompanion.exe", StringComparison.OrdinalIgnoreCase))
            || !files.Any(item => item.Name.Equals(Path.Combine("engine", "MevoCompanionEngine.exe"), StringComparison.OrdinalIgnoreCase)))
            throw new IOException("The package is missing the desktop application or connection engine.");
        // Validate all archive metadata before the first file is written.
        Directory.CreateDirectory(destination);
        long completed = 0;
        var buffer = new byte[128 * 1024];
        foreach (var (entry, name) in files)
        {
            var target = Path.Combine(destination, name);
            Directory.CreateDirectory(Path.GetDirectoryName(target)!);
            SafePaths.AssertNoReparseAncestors(target);
            using var input = entry.Open();
            using var output = new FileStream(target, FileMode.CreateNew, FileAccess.Write, FileShare.None);
            long written = 0;
            int count;
            while ((count = input.Read(buffer)) > 0)
            {
                written = checked(written + count);
                if (written > entry.Length) throw new IOException("The package expanded beyond its declared size.");
                output.Write(buffer, 0, count);
                completed += count;
                progress?.Invoke(total == 0 ? 1 : (double)completed / total);
            }
            if (written != entry.Length) throw new IOException("The package contains a truncated file.");
        }
    }
}
