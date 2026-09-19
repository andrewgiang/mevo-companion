using System.Collections.Concurrent;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Text.Json.Nodes;

namespace MevoCompanion.Desktop;

/// <summary>One private child process; newline-delimited JSON over redirected pipes.</summary>
public sealed class EngineClient : IAsyncDisposable
{
    private Process? _process;
    private readonly ConcurrentDictionary<string, TaskCompletionSource<JsonNode?>> _pending = new();
    private readonly SemaphoreSlim _writeLock = new(1, 1);
    private readonly CancellationTokenSource _stop = new();
    private long _sequence;
    private bool _shuttingDown;
    public event Action<JsonObject>? Message;
    public event Action<string>? Failed;
    public bool Connected => _process is { HasExited: false };
    public string Description { get; private set; } = "Starting connection engine…";
    public string LastError { get; private set; } = "";

    public void Start()
    {
        if (Connected) return;
        string? supplied = App.Option("--engine");
        string? root = FindDevelopmentRoot();
        string engine = supplied ?? (File.Exists(Path.Combine(AppContext.BaseDirectory, "engine", "MevoCompanionEngine.exe"))
            ? Path.Combine(AppContext.BaseDirectory, "engine", "MevoCompanionEngine.exe")
            : root is not null ? Path.Combine(root, "MevoCompanionEngine.py") : "");
        if (string.IsNullOrWhiteSpace(engine) || !File.Exists(engine))
            throw new FileNotFoundException("The connection engine is missing. Reinstall Mevo Companion or start with --engine followed by its path.", engine);
        engine = Path.GetFullPath(engine);
        bool python = Path.GetExtension(engine).Equals(".py", StringComparison.OrdinalIgnoreCase);
        string executable = python ? Path.Combine(Path.GetDirectoryName(engine)!, ".venv", "Scripts", "python.exe") : engine;
        if (!File.Exists(executable)) throw new FileNotFoundException("The bundled Python runtime could not be found.", executable);
        var start = new ProcessStartInfo(executable)
        {
            UseShellExecute = false, CreateNoWindow = true,
            WindowStyle = ProcessWindowStyle.Hidden,
            RedirectStandardInput = true, RedirectStandardOutput = true, RedirectStandardError = true,
            StandardInputEncoding = new UTF8Encoding(false),
            StandardOutputEncoding = new UTF8Encoding(false),
            StandardErrorEncoding = new UTF8Encoding(false),
            WorkingDirectory = Path.GetDirectoryName(engine)!,
        };
        if (python) start.ArgumentList.Add(engine);
        if (App.Option("--data-dir") is { } data) { start.ArgumentList.Add("--data-dir"); start.ArgumentList.Add(data); }
        if (App.Has("--demo")) start.ArgumentList.Add("--demo");
        start.Environment["PYTHONUNBUFFERED"] = "1";
        _process = new Process { StartInfo = start, EnableRaisingEvents = true };
        _process.Exited += (_, _) =>
        {
            foreach (var entry in _pending.Values) entry.TrySetException(new IOException("The connection engine stopped."));
            if (!_shuttingDown) Failed?.Invoke("The connection engine stopped. Restart Mevo Companion. " + LastError);
        };
        if (!_process.Start()) throw new IOException("The connection engine could not start.");
        Description = "Local connection engine · " + Path.GetFileName(engine);
        _ = ReadOutput(_process.StandardOutput);
        _ = ReadErrors(_process.StandardError);
    }

    private static string? FindDevelopmentRoot()
    {
        foreach (string start in new[] { AppContext.BaseDirectory, Environment.CurrentDirectory })
            for (var directory = new DirectoryInfo(start); directory is not null; directory = directory.Parent)
                if (File.Exists(Path.Combine(directory.FullName, "MevoCompanionEngine.py"))) return directory.FullName;
        return null;
    }

    private async Task ReadOutput(StreamReader reader)
    {
        try
        {
            while (!_stop.IsCancellationRequested && await reader.ReadLineAsync(_stop.Token) is { } line)
            {
                JsonObject? message;
                try { message = JsonNode.Parse(line) as JsonObject; }
                catch (System.Text.Json.JsonException) { continue; }
                if (message is null) continue;
                if (message["type"]?.GetValue<string>() == "response" && message["id"]?.GetValue<string>() is { } id)
                {
                    if (_pending.TryRemove(id, out var pending))
                    {
                        if (message["ok"]?.GetValue<bool>() == true) pending.TrySetResult(message["result"]?.DeepClone());
                        else pending.TrySetException(new InvalidOperationException(message["error"]?.ToString() ?? "The connection engine could not complete that action."));
                    }
                }
                else Message?.Invoke(message);
            }
        }
        catch (OperationCanceledException) { }
        catch (Exception error) { if (!_shuttingDown) Failed?.Invoke("Connection engine communication failed: " + error.Message); }
    }

    private async Task ReadErrors(StreamReader reader)
    {
        try
        {
            while (!_stop.IsCancellationRequested && await reader.ReadLineAsync(_stop.Token) is { } line)
                if (!string.IsNullOrWhiteSpace(line)) LastError = line;
        }
        catch (OperationCanceledException) { }
        catch (IOException) { }
    }

    public async Task<JsonNode?> SendAsync(string command, JsonObject? args = null, int timeoutSeconds = 20)
    {
        if (!Connected) throw new IOException("The connection engine is unavailable. Restart Mevo Companion.");
        string id = Interlocked.Increment(ref _sequence).ToString();
        var completion = new TaskCompletionSource<JsonNode?>(TaskCreationOptions.RunContinuationsAsynchronously);
        _pending[id] = completion;
        try
        {
            var request = new JsonObject { ["id"] = id, ["command"] = command, ["args"] = args ?? new JsonObject() };
            await _writeLock.WaitAsync(_stop.Token);
            try { await _process!.StandardInput.WriteLineAsync(request.ToJsonString()); await _process.StandardInput.FlushAsync(_stop.Token); }
            finally { _writeLock.Release(); }
            return await completion.Task.WaitAsync(TimeSpan.FromSeconds(timeoutSeconds), _stop.Token);
        }
        finally { _pending.TryRemove(id, out _); }
    }

    public async ValueTask DisposeAsync()
    {
        if (_shuttingDown) return;
        _shuttingDown = true;
        if (Connected)
        {
            try { await SendAsync("shutdown", timeoutSeconds: 5); } catch (Exception) { }
            try { await _process!.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(4)); }
            catch (TimeoutException) { try { _process!.Kill(entireProcessTree: false); } catch (InvalidOperationException) { } }
        }
        _stop.Cancel();
        foreach (var entry in _pending.Values) entry.TrySetCanceled();
        _process?.Dispose();
    }
}
