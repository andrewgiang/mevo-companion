using System.IO;

namespace MevoCompanion.Setup;

internal static class SafePaths
{
    public static string Canonical(string path) => Path.TrimEndingDirectorySeparator(Path.GetFullPath(path));

    public static bool IsWithin(string root, string path) => Canonical(path).StartsWith(
        Canonical(root) + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase);

    public static void AssertWithin(string root, string path)
    {
        if (!IsWithin(root, path)) throw new IOException("The package contains a path outside its installation folder.");
    }

    public static void AssertNoReparseAncestors(string path)
    {
        var current = Canonical(path);
        while (!string.IsNullOrEmpty(current))
        {
            try
            {
                if ((File.GetAttributes(current) & FileAttributes.ReparsePoint) != 0)
                    throw new IOException("Setup cannot use a folder containing a symbolic link or junction: " + current);
            }
            catch (FileNotFoundException) { }
            catch (DirectoryNotFoundException) { }
            current = Path.GetDirectoryName(current)!;
        }
    }

    public static string RelativePath(string value)
    {
        if (string.IsNullOrWhiteSpace(value) || value.StartsWith('/') || value.StartsWith('\\') || value.Contains(':'))
            throw new IOException("The package contains an absolute or empty path.");
        var parts = value.Replace('\\', '/').Split('/');
        foreach (var part in parts)
        {
            if (string.IsNullOrEmpty(part) || part is "." or ".." || part.EndsWith('.') || part.EndsWith(' ')
                || part.IndexOfAny(Path.GetInvalidFileNameChars()) >= 0)
                throw new IOException("The package contains an unsafe filename.");
            var name = part.Split('.')[0].ToUpperInvariant();
            if (name is "CON" or "PRN" or "AUX" or "NUL" or "CONIN$" or "CONOUT$" or "CLOCK$" ||
                (name.Length == 4 && (name.StartsWith("COM") || name.StartsWith("LPT"))
                 && (name[3] is >= '1' and <= '9' || "¹²³".Contains(name[3]))))
                throw new IOException("The package contains a reserved Windows filename.");
        }
        return string.Join(Path.DirectorySeparatorChar, parts);
    }

    public static IReadOnlyList<string> EnumerateFiles(string root)
    {
        AssertNoReparseAncestors(root);
        var files = new List<string>();
        var pending = new Stack<string>();
        pending.Push(root);
        while (pending.TryPop(out var directory))
        {
            foreach (var entry in Directory.EnumerateFileSystemEntries(directory))
            {
                AssertWithin(root, entry);
                var attributes = File.GetAttributes(entry);
                if ((attributes & FileAttributes.ReparsePoint) != 0)
                    throw new IOException("Setup found a symbolic link or junction inside the installation.");
                if ((attributes & FileAttributes.Directory) != 0) pending.Push(entry);
                else files.Add(entry);
            }
        }
        return files;
    }

    public static void DeleteEmptyDirectories(string root)
    {
        AssertNoReparseAncestors(root);
        var directories = new List<string> { Canonical(root) };
        for (var index = 0; index < directories.Count; index++)
        {
            foreach (var child in Directory.EnumerateDirectories(directories[index]))
            {
                AssertWithin(root, child);
                AssertNoReparseAncestors(child);
                directories.Add(child);
            }
        }
        foreach (var directory in directories.OrderByDescending(value => value.Length))
            if (!Directory.EnumerateFileSystemEntries(directory).Any()) Directory.Delete(directory, false);
    }
}
