using System.Diagnostics;
using System.IO;
using System.Windows;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Threading;

namespace MevoCompanion.Setup;

public partial class App : Application
{
    private Mutex? _mutex;

    protected override void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);
        try
        {
            var root = InstallService.DefaultTarget;
            var self = Environment.ProcessPath ?? throw new IOException("Could not locate Setup.");
            // Windows cannot delete a running executable. Relocate only our
            // installed Setup, before presenting any install/uninstall action.
            if (SafePaths.IsWithin(root, self))
            {
                SafePaths.AssertNoReparseAncestors(Path.GetTempPath());
                var temporary = Path.Combine(Path.GetTempPath(), "MevoCompanionSetup-" + Guid.NewGuid().ToString("N") + ".exe");
                SafePaths.AssertWithin(Path.GetTempPath(), temporary);
                File.Copy(self, temporary, false);
                var info = new ProcessStartInfo(temporary) { UseShellExecute = false, WorkingDirectory = Path.GetTempPath() };
                foreach (var argument in e.Args) info.ArgumentList.Add(argument);
                _ = Process.Start(info) ?? throw new IOException("Could not restart Setup outside the installation folder.");
                Shutdown();
                return;
            }
            _mutex = new Mutex(true, @"Local\MevoCompanionSetup-" + Environment.UserName, out var created);
            if (!created)
            {
                MessageBox.Show("Mevo Companion Setup is already open.", "Mevo Companion", MessageBoxButton.OK, MessageBoxImage.Information);
                Shutdown();
                return;
            }
            var window = new SetupWindow(e.Args.Contains("--uninstall", StringComparer.OrdinalIgnoreCase));
            MainWindow = window;
            var previewIndex = Array.IndexOf(e.Args, "--render-preview");
            if (previewIndex >= 0)
            {
                if (previewIndex + 1 >= e.Args.Length) throw new ArgumentException("Pass an output PNG path after --render-preview.");
                var previewPath = Path.GetFullPath(e.Args[previewIndex + 1]);
                if (File.Exists(previewPath)) throw new IOException("The preview file already exists.");
                window.WindowStartupLocation = WindowStartupLocation.Manual;
                window.Left = -10000;
                window.Top = -10000;
                window.ShowInTaskbar = false;
                window.Show();
                Dispatcher.BeginInvoke(DispatcherPriority.ApplicationIdle, () =>
                {
                    try
                    {
                        var bitmap = new RenderTargetBitmap((int)window.ActualWidth, (int)window.ActualHeight, 96, 96, PixelFormats.Pbgra32);
                        bitmap.Render(window);
                        var encoder = new PngBitmapEncoder();
                        encoder.Frames.Add(BitmapFrame.Create(bitmap));
                        using var output = new FileStream(previewPath, FileMode.CreateNew);
                        encoder.Save(output);
                        Shutdown();
                    }
                    catch (Exception error)
                    {
                        File.WriteAllText(previewPath + ".error.txt", error.ToString());
                        Shutdown(1);
                    }
                });
                return;
            }
            window.Show();
        }
        catch (Exception error)
        {
            MessageBox.Show(error.Message, "Mevo Companion Setup", MessageBoxButton.OK, MessageBoxImage.Error);
            Shutdown(1);
        }
    }

    protected override void OnExit(ExitEventArgs e)
    {
        _mutex?.Dispose();
        base.OnExit(e);
    }
}
