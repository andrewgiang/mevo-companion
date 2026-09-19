using System.ComponentModel;
using System.IO;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Threading;
using Microsoft.Win32;
using Forms = System.Windows.Forms;

namespace MevoCompanion.Desktop;

public partial class MainWindow : Window
{
    private readonly EngineClient _engine = new();
    private readonly Forms.NotifyIcon _tray;
    private JsonObject _config = new();
    private JsonObject _state = new();
    private readonly List<string> _events = [];
    private bool _closing, _loadedConfig, _applying, _paused;
    private string _page = "play";
    private int _step;
    private bool _previewEnabled;
    private bool _receivedState;
    private readonly List<string> _verifiedCommands = [];
    private readonly TaskCompletionSource<bool> _initialState = new(TaskCreationOptions.RunContinuationsAsynchronously);
    private readonly List<CameraChoice> _cameras = [];
    private static readonly string[] Colors = ["yellow", "white", "white2", "white3", "yellow2", "orange", "orange2", "orange3", "orange4", "green", "green2", "red", "red2"];
    private static readonly int[] Widths = [640, 800, 1280, 1920];

    public MainWindow()
    {
        InitializeComponent();
        if (double.TryParse(App.Option("--width"), out double width)) Width = Math.Max(MinWidth, width);
        if (double.TryParse(App.Option("--height"), out double height)) Height = Math.Max(MinHeight, height);
        DemoLabel.Visibility = App.Has("--demo") ? Visibility.Visible : Visibility.Collapsed;
        SetupColor.ItemsSource = SettingsColor.ItemsSource = Colors;
        SetupWidth.ItemsSource = SettingsWidth.ItemsSource = WidthOptions(640);
        _engine.Message += message => Dispatcher.BeginInvoke(() => HandleMessage(message));
        _engine.Failed += message => Dispatcher.BeginInvoke(() => ShowError(message));
        _tray = new Forms.NotifyIcon { Text = "Mevo Companion", Icon = System.Drawing.SystemIcons.Application, Visible = !App.Has("--smoke-test") };
        var menu = new Forms.ContextMenuStrip();
        menu.Items.Add("Open Mevo Companion", null, (_, _) => Dispatcher.Invoke(ShowFromTray));
        menu.Items.Add("Start GSPro", null, (_, _) => Dispatcher.InvokeAsync(async () => await Command("play")));
        menu.Items.Add("Pause shots", null, (_, _) => Dispatcher.InvokeAsync(async () => await Command("pause")));
        menu.Items.Add(new Forms.ToolStripSeparator());
        menu.Items.Add("Exit", null, (_, _) => Dispatcher.InvokeAsync(async () => await ExitAsync()));
        _tray.ContextMenuStrip = menu;
        _tray.DoubleClick += (_, _) => Dispatcher.Invoke(ShowFromTray);
        ShowStep(0);
    }

    private static string Text(JsonNode? node, string fallback = "") => node?.ToString() ?? fallback;
    private static bool Flag(JsonNode? node) => bool.TryParse(node?.ToString(), out bool result) && result;
    private static int Number(JsonNode? node, int fallback = 0) => int.TryParse(node?.ToString(), out int result) ? result : fallback;
    private static Brush Brush(string color) => new SolidColorBrush((Color)ColorConverter.ConvertFromString(color));

    private async void Window_Loaded(object sender, RoutedEventArgs e)
    {
        try
        {
            _engine.Start();
            EngineStatus.Text = _engine.Description;
            var status = await _engine.SendAsync("status");
            if (status is JsonObject obj)
            {
                if (obj["config"] is JsonObject settings) ApplyConfig(settings);
                if (obj["state"] is JsonObject state) ApplyState(state);
                else if (obj["health"] is not null) ApplyState(obj);
            }
            await Command("discover");
            if (App.Has("--background") && Flag(_config["setup_complete"])) Hide();
            else { Opacity = 1; ShowInTaskbar = true; }
            if (App.Option("--page") is { } page) ShowPage(page);
            if (App.Option("--smoke-test") is { } smoke)
            {
                await RunSmokeTest(smoke);
                return;
            }
            if (App.Option("--screenshot") is { } shot)
            {
                await Task.Delay(700);
                await CaptureAsync(shot);
            }
        }
        catch (Exception error)
        {
            Opacity = 1; ShowInTaskbar = true;
            ShowError(error.Message);
            EngineStatus.Text = "Connection engine unavailable";
            if (App.Option("--smoke-test") is { } smoke)
            {
                await WriteSmoke(smoke, false, error.Message, []);
                await ExitAsync();
            }
        }
    }

    private void HandleMessage(JsonObject message)
    {
        if (_closing) return;
        switch (Text(message["type"]))
        {
            case "state": if (message["data"] is JsonObject state) ApplyState(state); break;
            case "config": if (message["data"] is JsonObject config) ApplyConfig(config); break;
            case "preview": if (message["data"] is JsonObject preview) ApplyPreview(preview); break;
            case "event": AddEvent(Text(message["message"])); break;
            case "devices": if (message["data"] is JsonArray devices) ApplyDevices(devices); break;
        }
    }

    private void ApplyConfig(JsonObject config)
    {
        _config = (JsonObject)config.DeepClone();
        _applying = true;
        GSProPath.Text = Text(config["gspro_path"]);
        FSGolfPath.Text = Text(config["fs_golf_path"]);
        FoundGSPro.Text = string.IsNullOrEmpty(GSProPath.Text) ? "Not found yet. Choose the application in Connections." : GSProPath.Text;
        FoundFSGolf.Text = string.IsNullOrEmpty(FSGolfPath.Text) ? "Not found yet. Choose the application in Connections." : FSGolfPath.Text;
        AutoConnect.IsChecked = Flag(config["auto_connect"]);
        AutoChipping.IsChecked = config["auto_chipping"] is null || Flag(config["auto_chipping"]);
        WindowsStartup.IsChecked = Flag(config["start_with_windows"]);
        GSProHost.Text = Text(config["gspro_host"], "127.0.0.1");
        GSProPort.Text = Text(config["gspro_port"], "921");
        MevoAddress.Text = Text(config["mevo"]?["mevo_address"], "192.168.2.1:5100");
        SelectTag(AdapterChoice, Text(config["adapter"], "ocr"));
        SelectTag(SpeedUnits, Text(config["ocr"]?["speed_unit"], "auto"));
        ChippingCheck.Visibility = Text(config["adapter"]) == "direct" || Text(config["ocr"]?["mode"]) == "legacy"
            ? Visibility.Visible : Visibility.Collapsed;
        SetupColor.SelectedItem = SettingsColor.SelectedItem = Text(config["putting"]?["ball_color"], "yellow");
        int savedWidth = Number(config["putting"]?["width"], 640);
        SetupWidth.ItemsSource = SettingsWidth.ItemsSource = WidthOptions(savedWidth);
        SetupWidth.SelectedValue = SettingsWidth.SelectedValue = savedWidth;
        SetupCamera.SelectedValue = SettingsCamera.SelectedValue = Number(config["putting"]?["camera_index"]);
        var validation = config["validation"];
        SwingCheck.Text = ValidationText(Flag(validation?["swing"]), "Full shot");
        ChipCheck.Text = ValidationText(Flag(validation?["chip"]), "Chip");
        PuttCheck.Text = ValidationText(Flag(validation?["putt"]), "Webcam putt");
        PuttingPreviewStatus.Text = Flag(config["putting"]?["configured"]) || Flag(config["putting_preview_passed"])
            ? "✓  Springbok preview putt checked." : "A successful preview putt is required before setup is complete.";
        if (!_loadedConfig)
        {
            FinishStartup.IsChecked = Flag(config["setup_complete"]) ? Flag(config["start_with_windows"]) : true;
            _loadedConfig = true;
            ShowPage(Flag(config["setup_complete"]) ? "play" : "setup");
        }
        _applying = false;
    }

    private static string ValidationText(bool success, string name) => (success ? "✓   " : "○   ") + name + (success ? " — accepted by GSPro" : " — waiting for GSPro acceptance");
    private static void SelectTag(ComboBox box, string tag)
    {
        box.SelectedItem = box.Items.OfType<ComboBoxItem>().FirstOrDefault(item => TextNode(item.Tag) == tag) ?? box.Items[0];
    }
    private static string TextNode(object? value) => value?.ToString() ?? "";
    private static string SelectedTag(ComboBox box) => (box.SelectedItem as ComboBoxItem)?.Tag?.ToString() ?? "";

    private void ApplyState(JsonObject state)
    {
        _state = (JsonObject)state.DeepClone();
        _receivedState = true;
        _initialState.TrySetResult(true);
        ApplyHealth(state["health"]?["mevo"], MevoState, MevoMessage);
        ApplyHealth(state["health"]?["gspro"], GSProState, GSProMessage);
        ApplyHealth(state["health"]?["putting"], PuttingState, PuttingMessage);
        bool ready = Flag(state["ready"]), requested = Flag(state["requested"]), live = Flag(state["live_enabled"]);
        string club = Text(state["club"]);
        StatusPill.Text = App.Has("--demo") ? "Preview mode" : ready ? "Ready to play" : requested ? "Checking your setup" : "Waiting for GSPro";
        ActiveClub.Text = ActiveSourceText(state);
        SidebarState.Text = ready ? "CONNECTED AND READY" : requested ? "CHECKING CONNECTIONS" : "YOUR SIMULATOR, TOGETHER";
        _paused = requested && !live;
        PauseButton.Content = _paused ? "Resume shots" : "Pause shots";
        PauseButton.IsEnabled = requested;
        FooterText.Text = ready ? "The companion can stay in your system tray while you play."
            : _paused ? "Shot delivery is paused. Select Resume shots when you’re ready."
            : requested && string.IsNullOrEmpty(club) && Text(state["health"]?["gspro"]?["state"]) == "connected"
                ? "Reselect your current club once in GSPro to enable shots after reconnecting."
            : requested ? "Check the equipment cards above for the next step."
            : "Connection checks stay in the background.";
        if (state["shots"] is JsonArray shots)
        {
            ShotList.ItemsSource = shots.OfType<JsonObject>().Take(5).Select(shot => new ShotRow(
                Text(shot["time"]), Text(shot["source"]) == "webcam" ? "Webcam" : "Mevo+",
                double.TryParse(Text(shot["speed"]), System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture, out var speed) ? $"{speed:0.0} mph" : "—",
                Text(shot["message"], Text(shot["state"])))) .ToList();
            EmptyShots.Visibility = shots.Count == 0 ? Visibility.Visible : Visibility.Collapsed;
        }
        if (state["events"] is JsonArray events)
        {
            var values = events.Select(entry => entry is JsonArray pair ? string.Join("  ", pair.Select(value => Text(value))) : Text(entry)).Reverse().Take(100).ToList();
            if (values.Count > 0) EventList.ItemsSource = values;
        }
        if (!App.Has("--smoke-test")) _tray.Text = ready ? "Mevo Companion — ready to play" : "Mevo Companion — " + (requested ? "checking setup" : "waiting for GSPro");
    }

    private static string ActiveSourceText(JsonObject state)
    {
        string club = Text(state["club"]);
        if (string.IsNullOrEmpty(club)) return Flag(state["requested"]) ? "Waiting for GSPro's club selection" : "Ready when you are";
        string source = club == "PT" ? "Webcam putting active" : Text(state["shot_mode"]) switch
        {
            "chipping" => $"Mevo+ · Chipping · {club}",
            "full_swing" => $"Mevo+ · Full Swing · {club}",
            _ => $"Mevo+ active · {club}",
        };
        if (double.TryParse(Text(state["distance_to_target_yards"]), System.Globalization.NumberStyles.Float,
            System.Globalization.CultureInfo.InvariantCulture, out double distance) && double.IsFinite(distance) && distance >= 0)
            source += "\n" + distance.ToString("0.#", System.Globalization.CultureInfo.InvariantCulture) + " yd to target";
        return source;
    }

    private static void ApplyHealth(JsonNode? health, TextBlock title, TextBlock message)
    {
        string status = Text(health?["state"], "waiting");
        title.Text = status switch { "ready" => "● Ready", "connected" => "● Connected", "standby" => "● Standing by", "checking" => "● Checking", "working" => "● Working", "action_needed" or "needs_attention" => "● Action needed", "tracking" => "● Tracking", _ => "○ Waiting" };
        title.Foreground = Brush(status is "ready" or "connected" or "tracking" ? "#146B4F" : status is "action_needed" or "needs_attention" ? "#9A682A" : "#7D8A80");
        message.Text = Text(health?["message"], "Waiting for a connection.");
    }

    private void ApplyPreview(JsonObject preview)
    {
        if (_page != "setup" || _step != 1) return;
        if (preview["image_base64"] is { } encoded)
        {
            try
            {
                byte[] bytes = Convert.FromBase64String(encoded.ToString());
                using var stream = new MemoryStream(bytes);
                var image = new BitmapImage();
                image.BeginInit(); image.CacheOption = BitmapCacheOption.OnLoad; image.StreamSource = stream; image.EndInit(); image.Freeze();
                ShotPreview.Source = image;
                NativeMetricsGrid.Visibility = Visibility.Collapsed;
                PreviewPlaceholder.Visibility = Visibility.Collapsed;
            }
            catch (Exception error) when (error is FormatException or NotSupportedException or IOException) { ReadingMessage.Text = "The preview could not be displayed. " + error.Message; }
        }
        else
        {
            ShotPreview.Source = null;
            PreviewPlaceholder.Visibility = Visibility.Collapsed;
            NativeMetricsGrid.Visibility = Visibility.Visible;
            var nativeValues = preview["values"];
            LiveBall.Text = Text(nativeValues?["speed_mph"], "—");
            LiveClub.Text = Text(nativeValues?["club_speed_mph"], "—");
            LiveHla.Text = Text(nativeValues?["hla"], "—");
            LiveVla.Text = Text(nativeValues?["vla"], "—");
            LiveSpin.Text = Text(nativeValues?["spin_rpm"], "—");
            LiveAxis.Text = Text(nativeValues?["spin_axis"], "—");
        }
        bool valid = Flag(preview["valid"]);
        ReadingMessage.Text = valid ? "✓  All five required shot readings are available. Existing readings are held until the next shot." : preview["errors"] is JsonArray { Count: > 0 } errors ? string.Join(" ", errors.Select(value => Text(value))) : "Waiting for complete shot data from your live FS Golf session.";
        if (!valid && preview["image_base64"] is null && Text(_state["health"]?["mevo"]?["state"]) == "ready")
            ReadingMessage.Text = "FS Golf is ready. Hit a shot to see its measurements here.";
        ReadingValues.Text = valid && preview["values"] is JsonObject values
            ? $"Ball {Text(values["speed_mph"])} mph   ·   HLA {Text(values["hla"])}°   ·   VLA {Text(values["vla"])}°   ·   Spin {Text(values["spin_rpm"])} rpm   ·   Axis {Text(values["spin_axis"])}°" : "";
    }

    private void ApplyDevices(JsonArray devices)
    {
        int selected = Number(_config["putting"]?["camera_index"]);
        _cameras.Clear();
        foreach (var device in devices.OfType<JsonObject>())
            _cameras.Add(new CameraChoice(Text(device["name"], "Camera"), Number(device["index"]), Text(device["id"])));
        SetupCamera.ItemsSource = SettingsCamera.ItemsSource = null;
        SetupCamera.ItemsSource = SettingsCamera.ItemsSource = _cameras.ToList();
        SetupCamera.SelectedValue = SettingsCamera.SelectedValue = selected;
        if (SetupCamera.SelectedIndex < 0 && _cameras.Count > 0) SetupCamera.SelectedIndex = SettingsCamera.SelectedIndex = 0;
        if (_cameras.Count == 0) ShowError("No webcam was found. Connect your putting camera, then select Refresh.");
    }

    private void AddEvent(string message)
    {
        if (string.IsNullOrWhiteSpace(message)) return;
        _events.Insert(0, $"{DateTime.Now:HH:mm:ss}  {message}");
        if (_events.Count > 100) _events.RemoveAt(_events.Count - 1);
        EventList.ItemsSource = _events.ToList();
    }

    private async Task<JsonNode?> Command(string command, JsonObject? args = null)
    {
        try { return await _engine.SendAsync(command, args); }
        catch (Exception error) { ShowError(error is TimeoutException ? "That action took longer than expected. Check the connection status and try again." : error.Message); return null; }
    }

    private void ShowError(string message)
    {
        AlertText.Text = message;
        AlertBar.Visibility = Visibility.Visible;
        AddEvent(message);
    }

    private void ShowPage(string page)
    {
        if (page is not ("play" or "setup" or "connections" or "help")) page = "play";
        _page = page;
        PlayPage.Visibility = page == "play" ? Visibility.Visible : Visibility.Collapsed;
        SetupPage.Visibility = page == "setup" ? Visibility.Visible : Visibility.Collapsed;
        WizardNavigation.Visibility = page == "setup" ? Visibility.Visible : Visibility.Collapsed;
        ConnectionsPage.Visibility = page == "connections" ? Visibility.Visible : Visibility.Collapsed;
        HelpPage.Visibility = page == "help" ? Visibility.Visible : Visibility.Collapsed;
        PageTitle.Text = page switch { "setup" => "A better start to every round.", "connections" => "Make yourself at home.", "help" => "We’ll get you playing.", _ => "Let’s play." };
        PageEyebrow.Text = page switch { "setup" => "ONE-TIME SETUP", "connections" => "YOUR CONNECTIONS", "help" => "HELP & DIAGNOSTICS", _ => "YOUR SIMULATOR" };
        foreach (var button in new[] { NavPlay, NavSetup, NavConnections, NavHelp })
        {
            bool active = button.Tag.ToString() == page;
            button.Background = active ? Brush("#294B3C") : Brushes.Transparent;
            button.Foreground = active ? Brushes.White : Brush("#CADBD1");
        }
        UpdatePreviewSubscription();
    }

    private void ShowStep(int step)
    {
        _step = Math.Clamp(step, 0, 3);
        var panels = new[] { EquipmentStep, ReadingStep, PuttingStep, PracticeStep };
        var titles = new[] { Step1, Step2, Step3, Step4 };
        for (int i = 0; i < 4; i++)
        {
            panels[i].Visibility = i == _step ? Visibility.Visible : Visibility.Collapsed;
            titles[i].Foreground = i == _step ? Brush("#146B4F") : Brush("#8B998F");
        }
        PreviousStep.IsEnabled = _step > 0;
        NextStep.Visibility = _step == 3 ? Visibility.Collapsed : Visibility.Visible;
        StepCount.Text = $"Step {_step + 1} of 4";
        UpdatePreviewSubscription();
        if (_step == 2 && _cameras.Count == 0 && _engine.Connected) _ = RefreshCameras();
    }

    private void UpdatePreviewSubscription()
    {
        bool enabled = _page == "setup" && _step == 1;
        if (enabled == _previewEnabled || !_engine.Connected) return;
        _previewEnabled = enabled;
        _ = Command("preview", new JsonObject { ["enabled"] = enabled });
    }

    private async Task RefreshCameras()
    {
        var result = await Command("camera_devices");
        if (result is JsonArray list) ApplyDevices(list);
        else if (result?["devices"] is JsonArray nested) ApplyDevices(nested);
    }

    private JsonObject PuttingPatch(bool setup)
    {
        var camera = (setup ? SetupCamera : SettingsCamera).SelectedItem as CameraChoice;
        var putting = new JsonObject { ["ball_color"] = TextNode((setup ? SetupColor : SettingsColor).SelectedItem), ["width"] = (int?)((setup ? SetupWidth : SettingsWidth).SelectedValue) ?? 640 };
        if (camera is not null) { putting["camera_index"] = camera.Index; putting["camera_id"] = camera.Id; }
        var settings = new JsonObject { ["putting"] = putting };
        if (camera is not null) { settings["camera_id"] = camera.Id; settings["camera_name"] = camera.Name; }
        return settings;
    }

    private async Task SavePutting(bool setup) => await _engine.SendAsync("save_settings", new JsonObject { ["settings"] = PuttingPatch(setup) });

    private async void Save_Click(object sender, RoutedEventArgs e)
    {
        try
        {
            if (!int.TryParse(GSProPort.Text, out int port) || port is < 1 or > 65535) throw new InvalidOperationException("Enter a GSPro port between 1 and 65535.");
            bool startup = WindowsStartup.IsChecked == true;
            bool startupChanged = startup != Flag(_config["start_with_windows"]);
            var settings = new JsonObject
            {
                ["gspro_path"] = GSProPath.Text.Trim(), ["fs_golf_path"] = FSGolfPath.Text.Trim(),
                ["gspro_host"] = GSProHost.Text.Trim(), ["gspro_port"] = port,
                ["auto_connect"] = AutoConnect.IsChecked == true, ["start_with_windows"] = startup,
                ["auto_chipping"] = AutoChipping.IsChecked == true,
                ["adapter"] = SelectedTag(AdapterChoice),
                ["ocr"] = new JsonObject { ["speed_unit"] = SelectedTag(SpeedUnits) },
                ["mevo"] = new JsonObject { ["mevo_address"] = MevoAddress.Text.Trim() },
            };
            foreach (var entry in PuttingPatch(false)) settings[entry.Key] = entry.Value?.DeepClone();
            // Registry mutations occur only in this explicit user save handler.
            if (startupChanged && !App.Has("--demo") && !App.Has("--smoke-test")) SetStartup(startup);
            await _engine.SendAsync("save_settings", new JsonObject { ["settings"] = settings });
            FooterText.Text = "Connections saved. Your next round uses this setup.";
            AlertBar.Visibility = Visibility.Collapsed;
        }
        catch (Exception error) { ShowError(error.Message); }
    }

    private static void SetStartup(bool enabled)
    {
        string exe = Environment.ProcessPath ?? throw new InvalidOperationException("The app location could not be found for Windows startup.");
        if (!Path.GetFileName(exe).Equals("MevoCompanion.exe", StringComparison.OrdinalIgnoreCase))
            throw new InvalidOperationException("Open MevoCompanion.exe directly before enabling Windows startup.");
        using var key = Registry.CurrentUser.CreateSubKey(@"Software\Microsoft\Windows\CurrentVersion\Run", writable: true);
        if (enabled) key.SetValue("MevoCompanion", $"\"{exe}\" --background");
        else key.DeleteValue("MevoCompanion", throwOnMissingValue: false);
    }

    private void Nav_Click(object sender, RoutedEventArgs e) => ShowPage(((System.Windows.Controls.Button)sender).Tag.ToString()!);
    private void Connections_Click(object sender, RoutedEventArgs e) => ShowPage("connections");
    private void Dismiss_Click(object sender, RoutedEventArgs e) => AlertBar.Visibility = Visibility.Collapsed;
    private void Next_Click(object sender, RoutedEventArgs e) => ShowStep(_step + 1);
    private void Back_Click(object sender, RoutedEventArgs e) => ShowStep(_step - 1);
    private async void Play_Click(object sender, RoutedEventArgs e) { if (!Flag(_config["setup_complete"]) && !App.Has("--demo")) ShowPage("setup"); await Command("play"); }
    private async void Pause_Click(object sender, RoutedEventArgs e) => await Command(_paused ? "resume" : "pause");
    private async void OpenFS_Click(object sender, RoutedEventArgs e) => await Command("open_fs_golf");
    private async void Discover_Click(object sender, RoutedEventArgs e) { await Command("discover"); await RefreshCameras(); }
    private async void Cameras_Click(object sender, RoutedEventArgs e) => await RefreshCameras();
    private async void ShowPutting_Click(object sender, RoutedEventArgs e) => await Command("show_putting");
    private async void PuttingSettings_Click(object sender, RoutedEventArgs e) => await Command("putting_settings");
    private async void CheckReading_Click(object sender, RoutedEventArgs e) { await Command("connect", new JsonObject { ["live"] = false, ["setup"] = true }); await Command("preview", new JsonObject { ["enabled"] = true }); }
    private async void SetupPutting_Click(object sender, RoutedEventArgs e)
    {
        try { await SavePutting(true); await _engine.SendAsync("setup_putting"); }
        catch (Exception error) { ShowError(error.Message); }
    }
    private async void PracticeConsent_Changed(object sender, RoutedEventArgs e)
    {
        if (_applying || !_engine.Connected) return;
        if (PracticeConsent.IsChecked == true) await Command("connect", new JsonObject { ["live"] = true, ["setup"] = true });
        else await Command("pause");
    }
    private async void Chipping_Changed(object sender, RoutedEventArgs e)
    {
        if (!_applying && _engine.Connected) await Command("set_chipping", new JsonObject { ["enabled"] = ChippingCheck.IsChecked == true });
    }
    private async void Finish_Click(object sender, RoutedEventArgs e)
    {
        try
        {
            if (ConfirmPractice.IsChecked != true) throw new InvalidOperationException("Check the three practice shots in GSPro, then confirm their flight and direction above.");
            await _engine.SendAsync("finish_setup", new JsonObject { ["confirmed"] = true });
            bool startup = FinishStartup.IsChecked == true;
            // Finish is the user's explicit approval of the visible preference.
            // Hardware validation completes before any registry change is made.
            if (!App.Has("--demo") && !App.Has("--smoke-test") && (startup || Flag(_config["start_with_windows"]))) SetStartup(startup);
            await _engine.SendAsync("save_settings", new JsonObject { ["settings"] = new JsonObject { ["start_with_windows"] = startup, ["auto_connect"] = true } });
            PracticeResult.Text = "Setup saved. Open GSPro and your connections will follow.";
            ShowPage("play");
        }
        catch (Exception error) { PracticeResult.Text = error.Message; ShowError(error.Message); }
    }
    private async void Reconnect_Click(object sender, RoutedEventArgs e)
    {
        bool live = Flag(_state["live_enabled"]), setup = Flag(_state["setup_mode"]);
        await Command("stop");
        await Command("connect", new JsonObject { ["live"] = live, ["setup"] = setup });
    }
    private void BrowseApp_Click(object sender, RoutedEventArgs e)
    {
        var picker = new Microsoft.Win32.OpenFileDialog { Filter = "Windows applications (*.exe)|*.exe", Title = "Choose your golf application" };
        if (picker.ShowDialog(this) == true)
        {
            if (((System.Windows.Controls.Button)sender).Tag.ToString() == "gspro") GSProPath.Text = picker.FileName;
            else FSGolfPath.Text = picker.FileName;
        }
    }
    private async void Import_Click(object sender, RoutedEventArgs e)
    {
        var picker = new Microsoft.Win32.OpenFileDialog { Filter = "Connector profiles (*.ini;*.json)|*.ini;*.json|All files (*.*)|*.*", Title = "Import a connector profile" };
        if (picker.ShowDialog(this) == true) await Command("import_profile", new JsonObject { ["path"] = picker.FileName });
    }
    private async void Diagnostics_Click(object sender, RoutedEventArgs e)
    {
        var picker = new Microsoft.Win32.SaveFileDialog { Filter = "Diagnostic archive (*.zip)|*.zip", FileName = "Mevo-Companion-diagnostics.zip" };
        if (picker.ShowDialog(this) == true)
        {
            var result = await Command("export_diagnostics", new JsonObject { ["path"] = picker.FileName });
            if (result is not null) FooterText.Text = "Diagnostics saved to " + picker.FileName;
        }
    }

    public void ShowFromTray()
    {
        Opacity = 1; ShowInTaskbar = true;
        Show();
        if (WindowState == WindowState.Minimized) WindowState = WindowState.Normal;
        Activate();
    }
    private async void Window_Closing(object? sender, CancelEventArgs e)
    {
        if (_closing) return;
        e.Cancel = true;
        if (Flag(_state["requested"]) || Flag(_config["setup_complete"])) Hide();
        else await ExitAsync();
    }
    private async Task ExitAsync()
    {
        if (_closing) return;
        _closing = true;
        _tray.Visible = false;
        _tray.Dispose();
        await _engine.DisposeAsync();
        System.Windows.Application.Current.Shutdown();
    }

    private async Task CaptureAsync(string path)
    {
        await Dispatcher.InvokeAsync(UpdateLayout, DispatcherPriority.Render);
        var content = (FrameworkElement)Content;
        double scale = double.TryParse(App.Option("--render-scale"), System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture, out double parsed) ? Math.Clamp(parsed, .75, 2) : 1;
        var image = new RenderTargetBitmap((int)Math.Ceiling(content.ActualWidth * scale), (int)Math.Ceiling(content.ActualHeight * scale), 96 * scale, 96 * scale, PixelFormats.Pbgra32);
        image.Render(content);
        var encoder = new PngBitmapEncoder(); encoder.Frames.Add(BitmapFrame.Create(image));
        Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(path))!);
        using var file = File.Create(path); encoder.Save(file);
    }

    private async Task RunSmokeTest(string path)
    {
        try
        {
            await _initialState.Task.WaitAsync(TimeSpan.FromSeconds(10));
            if (App.Has("--demo"))
            {
                await _engine.SendAsync("connect", new JsonObject { ["live"] = false, ["setup"] = true });
                _verifiedCommands.Add("connect-preview");
                await _engine.SendAsync("pause"); _verifiedCommands.Add("pause");
                await _engine.SendAsync("resume"); _verifiedCommands.Add("resume");
                await RefreshCameras(); _verifiedCommands.Add("camera_devices");
                try { await _engine.SendAsync("finish_setup", new JsonObject { ["confirmed"] = true }); throw new InvalidOperationException("Demo mode incorrectly completed hardware validation."); }
                catch (InvalidOperationException error) when (error.Message.Contains("Demo mode cannot", StringComparison.OrdinalIgnoreCase)) { _verifiedCommands.Add("demo-validation-rejected"); }
                VerifyAutomaticChippingUi();
                _verifiedCommands.Add("automatic-chipping-ui");
            }
            var visited = new List<string>();
            foreach (string page in new[] { "play", "setup", "connections", "help" })
            {
                ShowPage(page);
                await Dispatcher.InvokeAsync(UpdateLayout, DispatcherPriority.Render);
                visited.Add(page);
            }
            ShowPage(App.Option("--page") ?? "play");
            if (int.TryParse(App.Option("--step"), out int step)) ShowStep(step - 1);
            if (App.Option("--screenshot") is { } screenshot) await CaptureAsync(screenshot);
            await WriteSmoke(path, _engine.Connected && _receivedState, "", visited);
        }
        catch (Exception error) { await WriteSmoke(path, false, error.Message, []); }
        await ExitAsync();
    }

    private void VerifyAutomaticChippingUi()
    {
        var saved = (JsonObject)_config.DeepClone();
        try
        {
            var changed = (JsonObject)saved.DeepClone();
            changed["auto_chipping"] = false;
            ApplyConfig(changed);
            if (AutoChipping.IsChecked != false) throw new InvalidOperationException("The manual FS Golf preference was not shown.");
            changed["auto_chipping"] = true;
            ApplyConfig(changed);
            if (AutoChipping.IsChecked != true) throw new InvalidOperationException("Automatic Chipping was not shown.");
            var state = new JsonObject { ["club"] = "SW", ["shot_mode"] = "chipping", ["distance_to_target_yards"] = 18.5 };
            if (ActiveSourceText(state) != "Mevo+ · Chipping · SW\n18.5 yd to target") throw new InvalidOperationException("Observed Chipping mode and distance were not displayed.");
            state["club"] = "PT";
            if (!ActiveSourceText(state).StartsWith("Webcam putting active", StringComparison.Ordinal)) throw new InvalidOperationException("The putter must keep its webcam source label.");
            state["club"] = "7I";
            state["shot_mode"] = "full_swing";
            state["distance_to_target_yards"] = null;
            if (ActiveSourceText(state) != "Mevo+ · Full Swing · 7I") throw new InvalidOperationException("Unavailable distance must not be displayed as zero.");
        }
        finally { ApplyConfig(saved); }
    }
    private async Task WriteSmoke(string path, bool success, string error, IReadOnlyCollection<string> pages)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(path))!);
        var result = new { success, error, engineConnected = _engine.Connected, stateReceived = _receivedState, configReceived = _loadedConfig, pages, verifiedCommands = _verifiedCommands, width = ActualWidth, height = ActualHeight, demo = App.Has("--demo"), hardwareActions = false };
        await File.WriteAllTextAsync(path, JsonSerializer.Serialize(result, new JsonSerializerOptions { WriteIndented = true }));
    }

    private sealed record CameraChoice(string Name, int Index, string Id);
    private sealed record WidthChoice(int Value, string Label);
    private static List<WidthChoice> WidthOptions(int saved) => Widths.Append(saved).Distinct().OrderBy(value => value)
        .Select(value => new WidthChoice(value, value switch { 640 => "Standard", 800 => "Medium", 1280 => "High", 1920 => "Full HD", _ => $"Saved profile ({value}px)" })).ToList();
    private sealed record ShotRow(string Time, string Source, string Speed, string Status);
}
