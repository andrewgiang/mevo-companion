using System.IO.Compression;
using System.Text.Json;
using MevoCompanion.Setup;

if (args.Length != 1) throw new ArgumentException("Pass a test-output directory inside the current workspace.");
var output = Path.GetFullPath(args[0]);
SafePaths.AssertWithin(Environment.CurrentDirectory, output);
SafePaths.AssertNoReparseAncestors(output);
Directory.CreateDirectory(output);
var run = Path.Combine(output, "run-" + Guid.NewGuid().ToString("N"));
Directory.CreateDirectory(run);
var passed = 0;

MemoryStream Archive(string? extra = null, int attributes = 0, bool includeRequired = true)
{
    var stream = new MemoryStream();
    using (var zip = new ZipArchive(stream, ZipArchiveMode.Create, true))
    {
        if (includeRequired)
        {
            foreach (var name in new[] { "MevoCompanion/MevoCompanion.exe", "MevoCompanion/engine/MevoCompanionEngine.exe" })
            {
                using var writer = new StreamWriter(zip.CreateEntry(name).Open());
                writer.Write("UNIT TEST FIXTURE — not an executable");
            }
        }
        if (extra is not null)
        {
            var entry = zip.CreateEntry(extra);
            entry.ExternalAttributes = attributes;
            using var writer = new StreamWriter(entry.Open());
            writer.Write("fixture");
        }
    }
    stream.Position = 0;
    return stream;
}

void Check(bool condition, string name)
{
    if (!condition) throw new Exception("FAILED: " + name);
    passed++;
    Console.WriteLine("PASS " + name);
}

void Reject(Action action, string name)
{
    try { action(); }
    catch (IOException) { Check(true, name); return; }
    throw new Exception("FAILED: should reject " + name);
}

var valid = Path.Combine(run, "valid");
using (var zip = Archive("MevoCompanion/engine/library/data.txt")) ArchivePayload.Extract(zip, valid);
Check(File.Exists(Path.Combine(valid, "MevoCompanion.exe")) &&
      File.ReadAllText(Path.Combine(valid, "engine/library/data.txt")) == "fixture", "valid package extracts under one root");

var attacks = new[]
{
    "MevoCompanion/../escape.txt", "MevoCompanion/engine/../../escape.txt",
    "MevoCompanion/C:/absolute.txt", "MevoCompanion//absolute.txt",
    "MevoCompanion/engine/file.txt:stream", "MevoCompanion/CON.txt",
    "MevoCompanion/engine/LPT1", "MevoCompanion/trailing. ",
    "MevoCompanion/./hidden.txt", "MevoCompanion/MevoCompanion.EXE",
    "OtherApp/file.txt", "/MevoCompanion/absolute.txt", "MevoCompanion/engine/file?.txt",
    "MevoCompanion/COM¹.txt", "MevoCompanion/CONOUT$",
};
foreach (var attack in attacks)
{
    using var zip = Archive(attack);
    var destination = Path.Combine(run, "attack-" + passed);
    Reject(() => ArchivePayload.Extract(zip, destination), "reject " + attack);
    Check(!Directory.Exists(destination), "metadata rejection writes no files");
}
using (var zip = Archive("MevoCompanion/symlink", unchecked((int)0xA1FF0000)))
    Reject(() => ArchivePayload.Extract(zip, Path.Combine(run, "symlink")), "reject Unix symlink ZIP entry");
using (var zip = Archive("MevoCompanion/reparse", (int)FileAttributes.ReparsePoint))
    Reject(() => ArchivePayload.Extract(zip, Path.Combine(run, "reparse")), "reject Windows reparse ZIP entry");
using (var zip = Archive("MevoCompanion/readme.txt", includeRequired: false))
    Reject(() => ArchivePayload.Extract(zip, Path.Combine(run, "incomplete")), "reject incomplete app payload");

Reject(() => SafePaths.AssertWithin(valid, valid + "-sibling/file.txt"), "root prefix is not a sibling boundary");
Reject(() => InstallManifest.Read(valid), "unmarked directory cannot be removed");
InstallManifest.Create(valid, "test");
Check(!InstallManifest.HasUnownedFiles(valid), "manifest records exactly installed files");
var userFile = Path.Combine(valid, "personal-notes.txt");
File.WriteAllText(userFile, "KEEP");
Check(InstallManifest.HasUnownedFiles(valid), "detect added files before replacing a backup");
Check(!InstallManifest.RemoveOwnedFiles(valid) && File.ReadAllText(userFile) == "KEEP", "uninstall preserves untracked files");
Check(!File.Exists(Path.Combine(valid, "MevoCompanion.exe")), "uninstall removes installed files");

var owned = Path.Combine(run, "owned");
Directory.CreateDirectory(owned);
File.WriteAllText(Path.Combine(owned, "app.exe"), "fixture");
InstallManifest.Create(owned, "test");
Check(InstallManifest.RemoveOwnedFiles(owned) && !Directory.Exists(owned), "uninstall removes empty owned installation tree");

var forged = Path.Combine(run, "forged");
Directory.CreateDirectory(forged);
var sentinel = Path.Combine(run, "must-remain.txt");
File.WriteAllText(sentinel, "KEEP");
File.WriteAllText(Path.Combine(forged, InstallManifest.FileName), JsonSerializer.Serialize(
    new InstallManifest(InstallManifest.ApplicationId, "test", DateTime.UtcNow, ["../must-remain.txt"])));
Reject(() => InstallManifest.RemoveOwnedFiles(forged), "poisoned manifest cannot delete outside installation");
Check(File.ReadAllText(sentinel) == "KEEP", "outside sentinel remains intact");

Console.WriteLine($"{passed} checks passed. Test fixtures: {run}");
