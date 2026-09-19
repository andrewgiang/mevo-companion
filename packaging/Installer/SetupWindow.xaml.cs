using System.ComponentModel;
using System.Windows;

namespace MevoCompanion.Setup;

public partial class SetupWindow : Window
{
    private readonly bool _uninstall;
    private bool _busy;
    private bool _completed;
    private readonly InstallService _service = new();

    public SetupWindow(bool uninstall)
    {
        InitializeComponent();
        _uninstall = uninstall;
        Location.Text = InstallService.DefaultTarget;
        Closing += OnClosing;
        if (uninstall)
        {
            Heading.Text = "Remove Mevo Companion?";
            Introduction.Text = "The app, its shortcuts and its automatic startup entry will be removed from this Windows account.";
            InstallOptions.Visibility = Visibility.Collapsed;
            Detail.Text = "Your equipment setup, putting calibration and logs stay in your account. GSPro and FS Golf are kept.";
            ActionButton.Content = "Uninstall";
        }
        else
        {
            RollbackButton.Visibility = InstallService.HasPreviousVersion ? Visibility.Visible : Visibility.Collapsed;
            if (!InstallService.HasPayload)
            {
                ActionButton.IsEnabled = false;
                Status.Text = "Development smoke build — no installation payload is attached.";
            }
        }
    }

    private void OnClosing(object? sender, CancelEventArgs e)
    {
        if (_busy) e.Cancel = true;
    }

    private void Cancel_Click(object sender, RoutedEventArgs e) => Close();

    private async void Action_Click(object sender, RoutedEventArgs e)
    {
        if (_completed)
        {
            if (!_uninstall)
            {
                try { InstallService.LaunchApplication(); }
                catch (Exception error) { Status.Text = error.Message; return; }
            }
            Close();
            return;
        }
        var startMenu = StartMenu.IsChecked == true;
        var desktop = Desktop.IsChecked == true;
        await RunAction(() => _uninstall ? _service.Uninstall() : _service.Install(startMenu, desktop,
            value => Dispatcher.BeginInvoke(() => Progress.Value = value * 100)));
    }

    private async void Rollback_Click(object sender, RoutedEventArgs e)
    {
        if (MessageBox.Show(this, "Restore the previous installed version? Your equipment settings will be kept.",
                "Restore previous version", MessageBoxButton.YesNo, MessageBoxImage.Question) != MessageBoxResult.Yes) return;
        await RunAction(_service.Rollback);
    }

    private async Task RunAction(Func<string> action)
    {
        _busy = true;
        ActionButton.IsEnabled = CancelButton.IsEnabled = RollbackButton.IsEnabled = false;
        InstallOptions.IsEnabled = false;
        Progress.Visibility = Visibility.Visible;
        Progress.IsIndeterminate = _uninstall;
        Status.Text = _uninstall ? "Removing the app. Your saved setup stays here." : "Preparing your companion…";
        try
        {
            Status.Text = await Task.Run(action);
            _completed = true;
            Progress.IsIndeterminate = false;
            Progress.Value = 100;
            Heading.Text = _uninstall ? "Your saved setup is kept." : "You’re ready to set up your round.";
            ActionButton.Content = _uninstall ? "Done" : "Open Mevo Companion";
            CancelButton.Content = "Close";
            RollbackButton.Visibility = Visibility.Collapsed;
        }
        catch (Exception error)
        {
            Status.Text = error.Message;
            Progress.IsIndeterminate = false;
            Progress.Value = 0;
            InstallOptions.IsEnabled = true;
        }
        finally
        {
            _busy = false;
            ActionButton.IsEnabled = CancelButton.IsEnabled = true;
            RollbackButton.IsEnabled = true;
        }
    }
}
