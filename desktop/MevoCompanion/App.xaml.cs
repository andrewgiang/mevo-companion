using System.IO;
using System.IO.Pipes;
using System.Security.Cryptography;
using System.Text;
using System.Windows;

namespace MevoCompanion.Desktop;

public partial class App : System.Windows.Application
{
    private Mutex? _instance;
    private CancellationTokenSource _pipeStop = new();
    private bool _ownsMutex;
    public static string[] Arguments { get; private set; } = [];
    public static string? Option(string name)
    {
        int i = Array.IndexOf(Arguments, name);
        return i >= 0 && i + 1 < Arguments.Length ? Arguments[i + 1] : null;
    }
    public static bool Has(string name) => Arguments.Contains(name);

    protected override void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);
        Arguments = e.Args;
        // Smoke tests use a private instance and cannot activate the user's app.
        string suffix = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(Environment.UserName + (Option("--data-dir") ?? ""))))[..16];
        string identity = "MevoCompanion-" + suffix + (Has("--smoke-test") ? "-smoke" : "");
        _instance = new Mutex(true, @"Local\" + identity, out _ownsMutex);
        if (!_ownsMutex)
        {
            if (Has("--background")) { Shutdown(); return; }
            try
            {
                using var client = new NamedPipeClientStream(".", identity, PipeDirection.Out);
                client.Connect(750);
                using var writer = new StreamWriter(client);
                writer.WriteLine("activate");
            }
            catch (IOException) { }
            catch (TimeoutException) { }
            Shutdown();
            return;
        }
        var window = new MainWindow();
        if (Has("--background")) { window.Opacity = 0; window.ShowInTaskbar = false; }
        MainWindow = window;
        window.Show();
        _ = ListenForActivation(identity, window);
    }

    private async Task ListenForActivation(string name, MainWindow window)
    {
        while (!_pipeStop.IsCancellationRequested)
        {
            try
            {
                using var server = new NamedPipeServerStream(name, PipeDirection.In, 1, PipeTransmissionMode.Byte, PipeOptions.Asynchronous | PipeOptions.CurrentUserOnly);
                await server.WaitForConnectionAsync(_pipeStop.Token);
                using var reader = new StreamReader(server);
                if (await reader.ReadLineAsync(_pipeStop.Token) == "activate")
                    await Dispatcher.InvokeAsync(window.ShowFromTray);
            }
            catch (OperationCanceledException) { break; }
            catch (IOException) { }
        }
    }

    protected override void OnExit(ExitEventArgs e)
    {
        _pipeStop.Cancel();
        if (_ownsMutex) _instance?.ReleaseMutex();
        _instance?.Dispose();
        base.OnExit(e);
    }
}
