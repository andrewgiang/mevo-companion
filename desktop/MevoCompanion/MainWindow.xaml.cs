using System.ComponentModel;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Interop;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Shapes;
using System.Windows.Threading;
using Microsoft.Win32;
using Drawing = System.Drawing;
using Forms = System.Windows.Forms;

namespace MevoCompanion.Desktop;

public partial class MainWindow : Window
{
    private enum Tone { Good, Warn, Bad, Idle }

    private readonly EngineClient _engine = new();
    private readonly Forms.NotifyIcon _tray;
    private readonly Forms.ToolStripItem _trayPause;
    private readonly DispatcherTimer _noticeTimer = new() { Interval = TimeSpan.FromSeconds(4) };
    private JsonObject _config = new();
    private JsonObject _state = new();
    private readonly List<string> _events = [];
    private bool _closing, _loadedConfig, _applying, _paused, _dirty, _readingVerified, _trayHintShown;
    private string _page = "play";
    private int _step;
    private bool _previewEnabled;
    private bool _receivedState;
    private readonly List<string> _verifiedCommands = [];
    private readonly TaskCompletionSource<bool> _initialState = new(TaskCreationOptions.RunContinuationsAsynchronously);
    private readonly List<CameraChoice> _cameras = [];
    private static readonly string[] Colors = ["yellow", "white", "white2", "white3", "yellow2", "orange", "orange2", "orange3", "orange4", "green", "green2", "red", "red2"];
    private static readonly int[] Widths = [640, 800, 1280, 1920];
    private static readonly CultureInfo Invariant = CultureInfo.InvariantCulture;

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
        _noticeTimer.Tick += (_, _) => { _noticeTimer.Stop(); AlertBar.Visibility = Visibility.Collapsed; };
        var icon = CreateAppIcon();
        Icon = Imaging.CreateBitmapSourceFromHIcon(icon.Handle, Int32Rect.Empty, BitmapSizeOptions.FromEmptyOptions());
        _tray = new Forms.NotifyIcon { Text = "Mevo Companion", Icon = icon, Visible = !App.Has("--smoke-test") };
        var menu = new Forms.ContextMenuStrip();
        menu.Items.Add("Open Mevo Companion", null, (_, _) => Dispatcher.Invoke(ShowFromTray)).Font = new Drawing.Font(menu.Font, Drawing.FontStyle.Bold);
        menu.Items.Add("Start GSPro", null, (_, _) => Dispatcher.InvokeAsync(async () => await Command("play")));
        _trayPause = menu.Items.Add("Pause shots", null, (_, _) => Dispatcher.InvokeAsync(async () => await Command(_paused ? "resume" : "pause")));
        _trayPause.Enabled = false;
        menu.Items.Add(new Forms.ToolStripSeparator());
        menu.Items.Add("Quit", null, (_, _) => Dispatcher.InvokeAsync(async () => await ExitAsync()));
        _tray.ContextMenuStrip = menu;
        _tray.DoubleClick += (_, _) => Dispatcher.Invoke(ShowFromTray);
        ShowStep(0);
        ShowPage("play");
    }

    private static string Text(JsonNode? node, string fallback = "") => node?.ToString() ?? fallback;
    private static bool Flag(JsonNode? node) => bool.TryParse(node?.ToString(), out bool result) && result;
    private static int Number(JsonNode? node, int fallback = 0) => int.TryParse(node?.ToString(), out int result) ? result : fallback;
    private static double? Decimal(JsonNode? node) => double.TryParse(node?.ToString(), NumberStyles.Float, Invariant, out double value) && double.IsFinite(value) ? value : null;
    private static string Format(JsonNode? node, string format) => Decimal(node) is { } value ? value.ToString(format, Invariant) : "—";
    private static Brush Brush(string color) => new SolidColorBrush((Color)ColorConverter.ConvertFromString(color));
    private Brush Resource(string key) => (Brush)FindResource(key);
    private Brush Strong(Tone tone) => Resource(tone.ToString());
    private Brush Soft(Tone tone) => Resource(tone + "Soft");
    private bool Demo => App.Has("--demo");
    private bool SetupComplete => Flag(_config["setup_complete"]);

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
            if (App.Has("--background") && SetupComplete) Hide();
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

    // ---------------------------------------------------------------- config

    private void ApplyConfig(JsonObject config)
    {
        _config = (JsonObject)config.DeepClone();
        _applying = true;
        string gspro = Text(config["gspro_path"]), fsGolf = Text(config["fs_golf_path"]);
        SetFound(FoundGSProIcon, FoundGSPro, gspro);
        SetFound(FoundFSGolfIcon, FoundFSGolf, fsGolf);
        // Engine config events arrive after every delivery; never clobber edits the user hasn't saved.
        if (!_dirty) ApplySettingsFields(config);
        ChippingCheck.Visibility = Text(config["adapter"]) == "direct" || Text(config["ocr"]?["mode"]) == "legacy"
            ? Visibility.Visible : Visibility.Collapsed;
        ChipHint.Text = "Use GSPro's ball-placement icon (top left) to place the ball off the green within 20 yards of the pin."
            + (config["auto_chipping"] is null || Flag(config["auto_chipping"]) ? "" : " Select Chipping in FS Golf first.");
        SetupColor.SelectedItem = Text(config["putting"]?["ball_color"], "yellow");
        int savedWidth = Number(config["putting"]?["width"], 640);
        SetupWidth.ItemsSource = WidthOptions(savedWidth);
        SetupWidth.SelectedValue = savedWidth;
        SetupCamera.SelectedValue = Number(config["putting"]?["camera_index"]);
        var validation = config["validation"];
        SetCheck(SwingIcon, SwingStatus, Flag(validation?["swing"]));
        SetCheck(ChipIcon, ChipStatus, Flag(validation?["chip"]));
        SetCheck(PuttIcon, PuttStatus, Flag(validation?["putt"]));
        bool putted = PuttingConfigured(config);
        PuttingPreviewIcon.Text = putted ? "" : "";
        PuttingPreviewIcon.Foreground = Strong(putted ? Tone.Good : Tone.Idle);
        PuttingPreviewStatus.Text = putted ? "Practice putt read successfully. Putting is ready." : "Waiting for a practice putt in the camera view.";
        if (!_loadedConfig)
        {
            FinishStartup.IsChecked = SetupComplete ? Flag(config["start_with_windows"]) : true;
            _loadedConfig = true;
            ShowPage(SetupComplete ? "play" : "setup");
            if (!SetupComplete) ShowStep(FirstIncompleteStep());
        }
        _applying = false;
        UpdateSetupProgress();
        ApplyBanner();
    }

    private void ApplySettingsFields(JsonObject config)
    {
        bool applying = _applying;
        _applying = true;
        GSProPath.Text = Text(config["gspro_path"]);
        FSGolfPath.Text = Text(config["fs_golf_path"]);
        AutoConnect.IsChecked = Flag(config["auto_connect"]);
        AutoChipping.IsChecked = config["auto_chipping"] is null || Flag(config["auto_chipping"]);
        WindowsStartup.IsChecked = Flag(config["start_with_windows"]);
        GSProHost.Text = Text(config["gspro_host"], "127.0.0.1");
        GSProPort.Text = Text(config["gspro_port"], "921");
        MevoAddress.Text = Text(config["mevo"]?["mevo_address"], "192.168.2.1:5100");
        SelectTag(AdapterChoice, Text(config["adapter"], "ocr"));
        SelectTag(SpeedUnits, Text(config["ocr"]?["speed_unit"], "auto"));
        SettingsColor.SelectedItem = Text(config["putting"]?["ball_color"], "yellow");
        int savedWidth = Number(config["putting"]?["width"], 640);
        SettingsWidth.ItemsSource = WidthOptions(savedWidth);
        SettingsWidth.SelectedValue = savedWidth;
        SettingsCamera.SelectedValue = Number(config["putting"]?["camera_index"]);
        _applying = applying;
    }

    private static bool PuttingConfigured(JsonObject config) => Flag(config["putting"]?["configured"]) || Flag(config["putting_preview_passed"]);

    private void SetFound(TextBlock icon, TextBlock label, string path)
    {
        bool found = !string.IsNullOrEmpty(path);
        icon.Text = found ? "" : "";
        icon.Foreground = Strong(found ? Tone.Good : Tone.Warn);
        label.Text = found ? path : "Not found. Select Browse to choose the application.";
        label.ToolTip = found ? path : null;
    }

    private void SetCheck(TextBlock icon, TextBlock status, bool done)
    {
        icon.Text = done ? "" : "";
        icon.Foreground = Strong(done ? Tone.Good : Tone.Idle);
        status.Text = done ? "Accepted by GSPro" : "Waiting";
        status.Foreground = Strong(done ? Tone.Good : Tone.Idle);
    }

    private static void SelectTag(ComboBox box, string tag)
    {
        box.SelectedItem = box.Items.OfType<ComboBoxItem>().FirstOrDefault(item => TextNode(item.Tag) == tag) ?? box.Items[0];
    }
    private static string TextNode(object? value) => value?.ToString() ?? "";
    private static string SelectedTag(ComboBox box) => (box.SelectedItem as ComboBoxItem)?.Tag?.ToString() ?? "";

    // ----------------------------------------------------------------- state

    private void ApplyState(JsonObject state)
    {
        _state = (JsonObject)state.DeepClone();
        _receivedState = true;
        _initialState.TrySetResult(true);
        ApplyHealth(state["health"]?["mevo"], MevoChip, MevoState, MevoMessage, SideMevoDot, MevoAction, s => s is not ("ready" or "connected" or "tracking" or "checking" or "working" or "standby"));
        ApplyHealth(state["health"]?["gspro"], GSProChip, GSProState, GSProMessage, SideGSProDot, GSProAction, s => s is not ("ready" or "connected" or "checking" or "working"));
        ApplyHealth(state["health"]?["putting"], PuttingChip, PuttingState, PuttingMessage, SidePuttingDot, PuttingAction, s => s is "action_needed" or "needs_attention");
        bool requested = Flag(state["requested"]), live = Flag(state["live_enabled"]);
        _paused = requested && !live;
        _applying = true;
        PracticeConsent.IsChecked = requested && live && Flag(state["setup_mode"]);
        _applying = false;
        ApplyClub(state);
        ApplyShots(state["shots"] as JsonArray);
        if (state["events"] is JsonArray events)
        {
            var values = events.Select(entry => entry is JsonArray pair ? string.Join("  ", pair.Select(value => Text(value))) : Text(entry)).Reverse().Take(100).ToList();
            if (values.Count > 0) EventList.ItemsSource = values;
        }
        if (Text(state["health"]?["mevo"]?["state"]) == "ready") _readingVerified = true;
        UpdateSetupProgress();
        ApplyBanner();
    }

    private void ApplyBanner()
    {
        bool ready = Flag(_state["ready"]), requested = Flag(_state["requested"]);
        string club = Text(_state["club"]);
        var issue = new[] { ("mevo", "Mevo+"), ("gspro", "GSPro"), ("putting", "Webcam putting") }
            .Select(c => (Name: c.Item2, Health: _state["health"]?[c.Item1]))
            .FirstOrDefault(c => Text(c.Health?["state"]) is "action_needed" or "needs_attention");
        Tone tone; string title, body, pill;
        bool showPlay = false, showPause = requested;
        string playText = "Start GSPro";
        if (!SetupComplete && !Demo && !requested)
        {
            (tone, title, pill) = (Tone.Warn, "Finish setup to start playing", "Setup needed");
            body = "A one-time check of GSPro, FS Golf and your putting camera. It takes about ten minutes on the practice range.";
            (showPlay, playText) = (true, "Continue setup");
        }
        else if (!requested)
        {
            (tone, title, pill) = (Tone.Idle, "Waiting for GSPro", "Waiting for GSPro");
            body = Flag(_config["auto_connect"]) ? "Open GSPro and your Mevo+ and webcam connect automatically." : "Select Start GSPro to connect your Mevo+ and webcam.";
            showPlay = true;
        }
        else if (_paused)
        {
            (tone, title, pill) = (Tone.Warn, "Shots paused", "Paused");
            body = "Shots from your Mevo+ and webcam aren't sent to GSPro. Resume when you're ready.";
        }
        else if (ready)
        {
            (tone, title, pill) = (Tone.Good, "Ready to play", "Ready to play");
            body = club == "PT" ? "Webcam putting is active. Putt when the ball is in position." : "Hit when you're ready. Select the putter in GSPro to switch to your webcam.";
        }
        else if (issue.Health is not null)
        {
            (tone, title, pill) = (Tone.Warn, issue.Name + " needs attention", "Action needed");
            body = Text(issue.Health["message"], "Check the status cards below.");
        }
        else
        {
            (tone, title, pill) = (Tone.Idle, "Connecting…", "Connecting");
            body = "Checking GSPro, FS Golf and your webcam. This takes a few seconds.";
        }
        if (Demo) pill = "Preview mode";
        BannerTitle.Text = title;
        BannerBody.Text = body;
        BannerStripe.Background = Strong(tone);
        StatusPill.Text = pill;
        StatusPillDot.Fill = Strong(tone);
        StatusPillBorder.Background = Soft(tone);
        PlayButton.Visibility = showPlay ? Visibility.Visible : Visibility.Collapsed;
        PlayButtonText.Text = playText;
        PauseButton.Visibility = showPause ? Visibility.Visible : Visibility.Collapsed;
        PauseText.Text = _paused ? "Resume shots" : "Pause shots";
        PauseIcon.Text = _paused ? "" : "";
        PauseButton.Style = (Style)FindResource(_paused ? "Primary" : typeof(Button));
        SidebarState.Text = pill.ToUpperInvariant();
        TroubleSummary.Text = issue.Health is not null ? issue.Name + ": " + Text(issue.Health["message"])
            : !requested ? "Not connected yet. Open GSPro, or select Start GSPro on the Play page."
            : "Everything looks fine right now. If shots aren't arriving, try Reconnect equipment.";
        _trayPause.Enabled = requested;
        _trayPause.Text = _paused ? "Resume shots" : "Pause shots";
        if (!App.Has("--smoke-test"))
        {
            string tip = "Mevo Companion — " + pill.ToLowerInvariant();
            _tray.Text = tip.Length > 63 ? tip[..63] : tip;
        }
    }

    private void ApplyClub(JsonObject state)
    {
        string club = Text(state["club"]);
        ClubText.Text = string.IsNullOrEmpty(club) ? "—" : club;
        // Until GSPro reports a club, the companion assumes a full-swing club.
        ModeText.Text = string.IsNullOrEmpty(club) && !Flag(state["requested"]) ? "Not connected"
            : club == "PT" ? "Webcam putting"
            : Text(state["shot_mode"]) switch { "chipping" => "Mevo+ · Chipping", "full_swing" => "Mevo+ · Full Swing", _ => "Mevo+" };
        double? distance = Decimal(state["distance_to_target_yards"]);
        DistanceText.Visibility = distance is >= 0 && !string.IsNullOrEmpty(club) ? Visibility.Visible : Visibility.Collapsed;
        DistanceText.Text = distance is { } d ? d.ToString("0.#", Invariant) + " yd to the pin" : "";
    }

    // Kept for the tray and smoke checks: a one-line description of the active shot source.
    private static string ActiveSourceText(JsonObject state)
    {
        string club = Text(state["club"]);
        if (string.IsNullOrEmpty(club) && !Flag(state["requested"])) return "Ready when you are";
        string suffix = string.IsNullOrEmpty(club) ? "" : " · " + club;
        string source = club == "PT" ? "Webcam putting active" : Text(state["shot_mode"]) switch
        {
            "chipping" => "Mevo+ · Chipping" + suffix,
            "full_swing" => "Mevo+ · Full Swing" + suffix,
            _ => "Mevo+ active" + suffix,
        };
        if (Decimal(state["distance_to_target_yards"]) is { } distance && distance >= 0)
            source += "\n" + distance.ToString("0.#", Invariant) + " yd to target";
        return source;
    }

    private void ApplyHealth(JsonNode? health, Border chip, TextBlock title, TextBlock message, Ellipse dot, Button action, Func<string, bool> showAction)
    {
        string status = Text(health?["state"], "waiting");
        Tone tone = status switch
        {
            "ready" or "connected" or "tracking" => Tone.Good,
            "action_needed" or "needs_attention" => Tone.Warn,
            "error" or "failed" => Tone.Bad,
            _ => Tone.Idle,
        };
        title.Text = status switch { "ready" => "Ready", "connected" => "Connected", "standby" => "Standing by", "checking" => "Checking", "working" => "Working", "action_needed" or "needs_attention" => "Action needed", "tracking" => "Tracking", "idle" => "Not connected", _ => "Waiting" };
        title.Foreground = tone == Tone.Idle ? Resource("Muted") : Strong(tone);
        chip.Background = Soft(tone);
        dot.Fill = tone == Tone.Idle ? Brush("#5C7A6C") : Strong(tone);
        message.Text = Text(health?["message"], "Waiting for a connection.");
        action.Visibility = !Demo && showAction(status) ? Visibility.Visible : Visibility.Collapsed;
    }

    private void ApplyShots(JsonArray? shots)
    {
        if (shots is null) return;
        var rows = shots.OfType<JsonObject>().Take(10).Select(ToRow).ToList();
        EmptyShots.Visibility = rows.Count == 0 ? Visibility.Visible : Visibility.Collapsed;
        LastShotPanel.Visibility = LastShotChip.Visibility = rows.Count == 0 ? Visibility.Collapsed : Visibility.Visible;
        HistorySection.Visibility = rows.Count > 1 ? Visibility.Visible : Visibility.Collapsed;
        ShotList.ItemsSource = rows;
        if (shots.FirstOrDefault() is not JsonObject last) { LastShotMeta.Text = ""; return; }
        bool putt = Text(last["source"]) == "webcam";
        var row = rows[0];
        LastShotMeta.Text = row.Source + " · " + row.Time;
        LastShotStatus.Text = row.Status;
        LastShotStatus.Foreground = row.Strong;
        LastShotChip.Background = row.Soft;
        LastShotMessage.Text = row.Message;
        LastSpeed.Text = Format(last["speed"], "0.0");
        LastHla.Text = Format(last["hla"], "0.0");
        LastVla.Text = putt ? "—" : Format(last["vla"], "0.0");
        LastSpin.Text = putt ? "—" : Format(last["spin_rpm"], "#,0");
        LastAxis.Text = putt ? "—" : Format(last["spin_axis"], "0.0");
        LastClub.Text = putt ? "—" : Format(last["club_speed"], "0.0");
    }

    private ShotRow ToRow(JsonObject shot)
    {
        string state = Text(shot["state"]);
        (string label, Tone tone) = state switch
        {
            "accepted" => ("Accepted", Tone.Good),
            "rejected" => ("Rejected", Tone.Bad),
            "unconfirmed" => ("Unconfirmed", Tone.Warn),
            "not_sent" => ("Not sent", Tone.Warn),
            "practice" => ("Practice", Tone.Idle),
            "submitted" => ("Sending", Tone.Idle),
            _ => (string.IsNullOrEmpty(state) ? "—" : char.ToUpperInvariant(state[0]) + state[1..].Replace('_', ' '), Tone.Idle),
        };
        string speed = Decimal(shot["speed"]) is { } s ? s.ToString("0.0", Invariant) + " mph" : "—";
        string direction = Decimal(shot["hla"]) is { } h ? h.ToString("0.0", Invariant) + "°" : "—";
        return new ShotRow(Text(shot["time"]), Text(shot["source"]) == "webcam" ? "Webcam" : "Mevo+", speed, direction,
            label, Text(shot["message"]), Strong(tone), Soft(tone));
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
            catch (Exception error) when (error is FormatException or NotSupportedException or IOException) { SetReading(Tone.Bad, "The preview could not be displayed. " + error.Message); }
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
        if (valid)
        {
            _readingVerified = true;
            SetReading(Tone.Good, "Shot reading works. All five required measurements were read. Continue when you're ready.");
        }
        else if (preview["image_base64"] is null && Text(_state["health"]?["mevo"]?["state"]) == "ready")
            SetReading(Tone.Idle, "FS Golf is ready. Hit a shot to see its measurements here.");
        else if (preview["errors"] is JsonArray { Count: > 0 } errors)
            SetReading(Tone.Warn, string.Join(" ", errors.Select(value => Text(value))));
        else
            SetReading(Tone.Idle, "Waiting for complete shot data from FS Golf's Play Mode.");
        ReadingValues.Text = valid && preview["image_base64"] is not null && preview["values"] is JsonObject values
            ? $"Ball {Text(values["speed_mph"])} mph   ·   Direction {Text(values["hla"])}°   ·   Launch {Text(values["vla"])}°   ·   Spin {Text(values["spin_rpm"])} rpm   ·   Axis {Text(values["spin_axis"])}°" : "";
        UpdateSetupProgress();
    }

    private void SetReading(Tone tone, string message)
    {
        ReadingIcon.Text = tone switch { Tone.Good => "", Tone.Warn => "", Tone.Bad => "", _ => "" };
        ReadingIcon.Foreground = Strong(tone);
        ReadingMessage.Text = message;
    }

    private void ApplyDevices(JsonArray devices)
    {
        int selected = Number(_config["putting"]?["camera_index"]);
        bool applying = _applying;
        _applying = true;
        _cameras.Clear();
        foreach (var device in devices.OfType<JsonObject>())
            _cameras.Add(new CameraChoice(Text(device["name"], "Camera"), Number(device["index"]), Text(device["id"])));
        var settingsSelection = SettingsCamera.SelectedValue;
        SetupCamera.ItemsSource = SettingsCamera.ItemsSource = null;
        SetupCamera.ItemsSource = SettingsCamera.ItemsSource = _cameras.ToList();
        SetupCamera.SelectedValue = selected;
        SettingsCamera.SelectedValue = _dirty ? settingsSelection ?? selected : selected;
        if (SetupCamera.SelectedIndex < 0 && _cameras.Count > 0) SetupCamera.SelectedIndex = 0;
        if (SettingsCamera.SelectedIndex < 0 && _cameras.Count > 0) SettingsCamera.SelectedIndex = 0;
        _applying = applying;
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

    // ---------------------------------------------------------------- alerts

    private void ShowError(string message)
    {
        ShowAlert(message, Tone.Bad);
        AddEvent(message);
    }

    // A brief confirmation that clears itself; errors stay until dismissed.
    private void ShowNotice(string message) => ShowAlert(message, Tone.Good);

    private void ShowAlert(string message, Tone tone)
    {
        _noticeTimer.Stop();
        AlertText.Text = message;
        AlertText.Foreground = Resource("Ink");
        AlertBar.Background = Soft(tone);
        AlertIcon.Text = tone switch { Tone.Good => "", Tone.Bad => "", Tone.Warn => "", _ => "" };
        AlertIcon.Foreground = Strong(tone);
        AlertBar.Visibility = Visibility.Visible;
        if (tone == Tone.Good) _noticeTimer.Start();
    }

    // ------------------------------------------------------------ navigation

    private void ShowPage(string page)
    {
        if (page is not ("play" or "setup" or "connections" or "help")) page = "play";
        _page = page;
        PlayPage.Visibility = page == "play" ? Visibility.Visible : Visibility.Collapsed;
        SetupPage.Visibility = page == "setup" ? Visibility.Visible : Visibility.Collapsed;
        WizardNavigation.Visibility = page == "setup" ? Visibility.Visible : Visibility.Collapsed;
        ConnectionsPage.Visibility = page == "connections" ? Visibility.Visible : Visibility.Collapsed;
        SaveBar.Visibility = page == "connections" ? Visibility.Visible : Visibility.Collapsed;
        HelpPage.Visibility = page == "help" ? Visibility.Visible : Visibility.Collapsed;
        PageTitle.Text = page switch { "setup" => "Setup", "connections" => "Settings", "help" => "Help", _ => "Play" };
        PageSubtitle.Text = page switch
        {
            "setup" => SetupComplete ? "Setup is complete. Revisit any step to change your equipment." : "A one-time check so every shot reaches GSPro correctly.",
            "connections" => "Where your apps live and how the companion connects.",
            "help" => "Fix connection problems and see what the companion is doing.",
            _ => "Live status of your simulator.",
        };
        foreach (var button in new[] { NavPlay, NavSetup, NavConnections, NavHelp })
        {
            bool active = button.Tag.ToString() == page;
            button.Background = active ? Brush("#24483A") : Brushes.Transparent;
            button.Foreground = active ? Brushes.White : Brush("#C4D6CB");
            button.FontWeight = active ? FontWeights.SemiBold : FontWeights.Normal;
        }
        PageScroller.ScrollToTop();
        UpdatePreviewSubscription();
    }

    private void ShowStep(int step)
    {
        _step = Math.Clamp(step, 0, 3);
        var panels = new[] { EquipmentStep, ReadingStep, PuttingStep, PracticeStep };
        for (int i = 0; i < 4; i++) panels[i].Visibility = i == _step ? Visibility.Visible : Visibility.Collapsed;
        PreviousStep.Visibility = _step > 0 ? Visibility.Visible : Visibility.Hidden;
        NextStep.Visibility = _step == 3 ? Visibility.Collapsed : Visibility.Visible;
        StepCount.Text = $"Step {_step + 1} of 4";
        UpdateSetupProgress();
        PageScroller.ScrollToTop();
        UpdatePreviewSubscription();
        if (_step == 2 && _cameras.Count == 0 && _engine.Connected) _ = RefreshCameras();
    }

    private bool[] StepsDone()
    {
        var validation = _config["validation"];
        bool complete = SetupComplete;
        return
        [
            complete || (!string.IsNullOrEmpty(Text(_config["gspro_path"])) && !string.IsNullOrEmpty(Text(_config["fs_golf_path"]))),
            complete || _readingVerified || Flag(validation?["swing"]),
            complete || PuttingConfigured(_config),
            complete || (Flag(validation?["swing"]) && Flag(validation?["chip"]) && Flag(validation?["putt"])),
        ];
    }

    private int FirstIncompleteStep() => Array.IndexOf(StepsDone(), false) is var i and >= 0 ? i : 3;

    private void UpdateSetupProgress()
    {
        bool[] done = StepsDone();
        var circles = new[] { StepCircle1, StepCircle2, StepCircle3, StepCircle4 };
        var numbers = new[] { StepNum1, StepNum2, StepNum3, StepNum4 };
        var titles = new[] { Step1, Step2, Step3, Step4 };
        var states = new[] { StepState1, StepState2, StepState3, StepState4 };
        for (int i = 0; i < 4; i++)
        {
            bool current = i == _step;
            Brush accent = Resource("Accent"), good = Strong(Tone.Good);
            circles[i].BorderBrush = done[i] ? good : current ? accent : Resource("Field");
            circles[i].Background = done[i] ? good : current ? accent : Brushes.White;
            numbers[i].Text = done[i] ? "✓" : (i + 1).ToString(Invariant);
            numbers[i].Foreground = done[i] || current ? Brushes.White : Resource("Subtle");
            titles[i].Foreground = current ? Resource("Ink") : Resource("Muted");
            states[i].Text = done[i] ? "Done" : current ? "In progress" : "To do";
            states[i].Foreground = done[i] ? good : Resource("Subtle");
        }
        int count = done.Count(value => value);
        SetupBadge.Visibility = SetupComplete ? Visibility.Collapsed : Visibility.Visible;
        SetupBadgeText.Text = $"{count}/4";
        bool stepDone = done[_step];
        NextStep.Content = stepDone ? "Continue" : "Skip for now";
        NextStep.Style = (Style)FindResource(stepDone ? "Primary" : typeof(Button));

        var validation = _config["validation"];
        var missing = new List<string>();
        if (!Flag(validation?["swing"])) missing.Add("full shot");
        if (!Flag(validation?["chip"])) missing.Add("chip");
        if (!Flag(validation?["putt"])) missing.Add("webcam putt");
        if (!PuttingConfigured(_config)) missing.Add("practice putt in step 3");
        FinishButton.IsEnabled = ConfirmPractice.IsChecked == true;
        PracticeResult.Text = SetupComplete ? "Setup is complete."
            : missing.Count > 0 ? "Still needed: " + string.Join(", ", missing) + "."
            : ConfirmPractice.IsChecked == true ? "" : "Confirm the shots flew correctly to finish.";
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

    // -------------------------------------------------------------- settings

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

    private void Setting_Changed(object sender, RoutedEventArgs e)
    {
        if (_applying || !_loadedConfig) return;
        SetDirty(true);
    }

    private void SetDirty(bool dirty)
    {
        _dirty = dirty;
        SaveButton.IsEnabled = DiscardButton.IsEnabled = dirty;
        SaveState.Text = dirty ? "You have unsaved changes." : "All changes saved.";
        SaveState.Foreground = dirty ? Strong(Tone.Warn) : Resource("Subtle");
        UnsavedDot.Visibility = dirty ? Visibility.Visible : Visibility.Collapsed;
    }

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
            bool wasComplete = SetupComplete;
            // Registry mutations occur only in this explicit user save handler.
            if (startupChanged && !Demo && !App.Has("--smoke-test")) SetStartup(startup);
            SetDirty(false);
            await _engine.SendAsync("save_settings", new JsonObject { ["settings"] = settings });
            ShowNotice(wasComplete && !SetupComplete
                ? "Settings saved. Your shot source or camera changed, so run Setup again before playing."
                : "Settings saved.");
        }
        catch (Exception error) { SetDirty(true); ShowError(error.Message); }
    }

    private void Discard_Click(object sender, RoutedEventArgs e)
    {
        SetDirty(false);
        ApplySettingsFields(_config);
    }

    private static void SetStartup(bool enabled)
    {
        string exe = Environment.ProcessPath ?? throw new InvalidOperationException("The app location could not be found for Windows startup.");
        if (!System.IO.Path.GetFileName(exe).Equals("MevoCompanion.exe", StringComparison.OrdinalIgnoreCase))
            throw new InvalidOperationException("Open MevoCompanion.exe directly before enabling Windows startup.");
        using var key = Registry.CurrentUser.CreateSubKey(@"Software\Microsoft\Windows\CurrentVersion\Run", writable: true);
        if (enabled) key.SetValue("MevoCompanion", $"\"{exe}\" --background");
        else key.DeleteValue("MevoCompanion", throwOnMissingValue: false);
    }

    // ---------------------------------------------------------------- events

    private void Nav_Click(object sender, RoutedEventArgs e) => ShowPage(((Button)sender).Tag.ToString()!);
    private void Step_Click(object sender, RoutedEventArgs e) => ShowStep(int.Parse(((Button)sender).Tag.ToString()!, Invariant));
    private void Dismiss_Click(object sender, RoutedEventArgs e) { _noticeTimer.Stop(); AlertBar.Visibility = Visibility.Collapsed; }
    private void Next_Click(object sender, RoutedEventArgs e) => ShowStep(_step + 1);
    private void Back_Click(object sender, RoutedEventArgs e) => ShowStep(_step - 1);
    private async void Play_Click(object sender, RoutedEventArgs e)
    {
        if (!SetupComplete && !Demo && sender == PlayButton)
        {
            ShowPage("setup");
            ShowStep(FirstIncompleteStep());
            return;
        }
        await Command("play");
    }
    private async void Pause_Click(object sender, RoutedEventArgs e) => await Command(_paused ? "resume" : "pause");
    private async void OpenFS_Click(object sender, RoutedEventArgs e) => await Command("open_fs_golf");
    private async void Discover_Click(object sender, RoutedEventArgs e) { await Command("discover"); await RefreshCameras(); }
    private async void Cameras_Click(object sender, RoutedEventArgs e) => await RefreshCameras();
    private async void ShowPutting_Click(object sender, RoutedEventArgs e) => await Command("show_putting");
    private async void PuttingSettings_Click(object sender, RoutedEventArgs e) => await Command("putting_settings");
    private async void CheckReading_Click(object sender, RoutedEventArgs e)
    {
        SetReading(Tone.Idle, "Connecting to FS Golf…");
        await Command("connect", new JsonObject { ["live"] = false, ["setup"] = true });
        await Command("preview", new JsonObject { ["enabled"] = true });
        _previewEnabled = true;
    }
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
    private void ConfirmPractice_Changed(object sender, RoutedEventArgs e) => UpdateSetupProgress();
    private async void Finish_Click(object sender, RoutedEventArgs e)
    {
        try
        {
            if (ConfirmPractice.IsChecked != true) throw new InvalidOperationException("Check the three practice shots in GSPro, then confirm their flight and direction above.");
            await _engine.SendAsync("finish_setup", new JsonObject { ["confirmed"] = true });
            bool startup = FinishStartup.IsChecked == true;
            // Finish is the user's explicit approval of the visible preference.
            // Hardware validation completes before any registry change is made.
            if (!Demo && !App.Has("--smoke-test") && (startup || Flag(_config["start_with_windows"]))) SetStartup(startup);
            await _engine.SendAsync("save_settings", new JsonObject { ["settings"] = new JsonObject { ["start_with_windows"] = startup, ["auto_connect"] = true } });
            ShowPage("play");
            ShowNotice("Setup complete. Open GSPro and your equipment connects automatically.");
        }
        catch (Exception error) { PracticeResult.Text = error.Message; ShowError(error.Message); }
    }
    private async void Reconnect_Click(object sender, RoutedEventArgs e)
    {
        bool live = Flag(_state["live_enabled"]), setup = Flag(_state["setup_mode"]);
        await Command("stop");
        await Command("connect", new JsonObject { ["live"] = live, ["setup"] = setup });
        ShowNotice("Reconnecting your equipment…");
    }
    private string? PickApplication(string title)
    {
        var picker = new OpenFileDialog { Filter = "Windows applications (*.exe)|*.exe", Title = title };
        return picker.ShowDialog(this) == true ? picker.FileName : null;
    }
    private void BrowseApp_Click(object sender, RoutedEventArgs e)
    {
        bool gspro = ((Button)sender).Tag.ToString() == "gspro";
        if (PickApplication(gspro ? "Choose GSPro" : "Choose FS Golf PC") is not { } path) return;
        if (gspro) GSProPath.Text = path; else FSGolfPath.Text = path;
    }
    // Setup has no separate save step, so a chosen path is saved straight away.
    private async void BrowseSetup_Click(object sender, RoutedEventArgs e)
    {
        bool gspro = ((Button)sender).Tag.ToString() == "gspro";
        if (PickApplication(gspro ? "Choose GSPro" : "Choose FS Golf PC") is not { } path) return;
        var result = await Command("save_settings", new JsonObject { ["settings"] = new JsonObject { [gspro ? "gspro_path" : "fs_golf_path"] = path } });
        if (result is not null) ShowNotice((gspro ? "GSPro" : "FS Golf") + " location saved.");
    }
    private async void Import_Click(object sender, RoutedEventArgs e)
    {
        var picker = new OpenFileDialog { Filter = "Connector profiles (*.ini;*.json)|*.ini;*.json|All files (*.*)|*.*", Title = "Import a Springbok profile" };
        if (picker.ShowDialog(this) != true) return;
        SetDirty(false);
        if (await Command("import_profile", new JsonObject { ["path"] = picker.FileName }) is not null)
            ShowNotice("Profile imported. Run Setup again to check your equipment with it.");
    }
    private async void Diagnostics_Click(object sender, RoutedEventArgs e)
    {
        var picker = new SaveFileDialog { Filter = "Diagnostic archive (*.zip)|*.zip", FileName = $"Mevo-Companion-diagnostics-{DateTime.Now:yyyyMMdd-HHmm}.zip" };
        if (picker.ShowDialog(this) == true)
        {
            var result = await Command("export_diagnostics", new JsonObject { ["path"] = picker.FileName });
            if (result is not null) ShowNotice("Diagnostics saved to " + picker.FileName);
        }
    }
    private void OpenData_Click(object sender, RoutedEventArgs e)
    {
        string folder = Text(_config["data_dir"]);
        if (!Directory.Exists(folder)) { ShowError("The data folder isn't available yet."); return; }
        Process.Start(new ProcessStartInfo("explorer.exe", $"\"{folder}\"") { UseShellExecute = true });
    }
    private void CopyLog_Click(object sender, RoutedEventArgs e)
    {
        var lines = EventList.ItemsSource?.Cast<object>().Select(item => item.ToString()) ?? [];
        try { Clipboard.SetText(string.Join(Environment.NewLine, lines)); ShowNotice("Activity copied to the clipboard."); }
        catch (Exception error) when (error is System.Runtime.InteropServices.COMException or System.Runtime.InteropServices.ExternalException) { ShowError("The clipboard is busy. Try again."); }
    }

    // ------------------------------------------------------------ tray/close

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
        if (Flag(_state["requested"]) || SetupComplete)
        {
            Hide();
            if (!_trayHintShown && _tray.Visible)
            {
                _trayHintShown = true;
                _tray.ShowBalloonTip(4000, "Mevo Companion is still running",
                    "It stays in the tray and connects when GSPro opens. Right-click the tray icon to quit.", Forms.ToolTipIcon.Info);
            }
        }
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

    // The app ships without an .ico; draw the sidebar's "m" mark so the tray and taskbar are recognizable.
    private static Drawing.Icon CreateAppIcon()
    {
        using var bitmap = new Drawing.Bitmap(32, 32);
        using (var g = Drawing.Graphics.FromImage(bitmap))
        {
            g.SmoothingMode = Drawing.Drawing2D.SmoothingMode.AntiAlias;
            g.TextRenderingHint = Drawing.Text.TextRenderingHint.AntiAliasGridFit;
            using var path = new Drawing.Drawing2D.GraphicsPath();
            path.AddArc(0, 0, 12, 12, 180, 90); path.AddArc(19, 0, 12, 12, 270, 90);
            path.AddArc(19, 19, 12, 12, 0, 90); path.AddArc(0, 19, 12, 12, 90, 90);
            path.CloseFigure();
            using var fill = new Drawing.SolidBrush(Drawing.Color.FromArgb(0x14, 0x6B, 0x4F));
            g.FillPath(fill, path);
            using var font = new Drawing.Font("Segoe UI", 17, Drawing.FontStyle.Bold, Drawing.GraphicsUnit.Pixel);
            using var ink = new Drawing.SolidBrush(Drawing.Color.FromArgb(0xD4, 0xEB, 0xCF));
            var format = new Drawing.StringFormat { Alignment = Drawing.StringAlignment.Center, LineAlignment = Drawing.StringAlignment.Center };
            g.DrawString("m", font, ink, new Drawing.RectangleF(0, -2, 32, 32), format);
        }
        return Drawing.Icon.FromHandle(bitmap.GetHicon());
    }

    // ----------------------------------------------------------- smoke tests

    private async Task CaptureAsync(string path)
    {
        await Dispatcher.InvokeAsync(UpdateLayout, DispatcherPriority.Render);
        var content = (FrameworkElement)Content;
        double scale = double.TryParse(App.Option("--render-scale"), NumberStyles.Float, Invariant, out double parsed) ? Math.Clamp(parsed, .75, 2) : 1;
        var image = new RenderTargetBitmap((int)Math.Ceiling(content.ActualWidth * scale), (int)Math.Ceiling(content.ActualHeight * scale), 96 * scale, 96 * scale, PixelFormats.Pbgra32);
        image.Render(content);
        var encoder = new PngBitmapEncoder(); encoder.Frames.Add(BitmapFrame.Create(image));
        Directory.CreateDirectory(System.IO.Path.GetDirectoryName(System.IO.Path.GetFullPath(path))!);
        using var file = File.Create(path); encoder.Save(file);
    }

    private async Task RunSmokeTest(string path)
    {
        try
        {
            await _initialState.Task.WaitAsync(TimeSpan.FromSeconds(10));
            if (Demo)
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
                VerifyUnsavedSettingsKept();
                _verifiedCommands.Add("unsaved-settings-kept");
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
            ApplyClub(state);
            if (ModeText.Text != "Mevo+ · Chipping" || DistanceText.Text != "18.5 yd to the pin") throw new InvalidOperationException("The Play page did not show Chipping and the distance.");
            state["club"] = "PT";
            if (!ActiveSourceText(state).StartsWith("Webcam putting active", StringComparison.Ordinal)) throw new InvalidOperationException("The putter must keep its webcam source label.");
            state["club"] = "7I";
            state["shot_mode"] = "full_swing";
            state["distance_to_target_yards"] = null;
            if (ActiveSourceText(state) != "Mevo+ · Full Swing · 7I") throw new InvalidOperationException("Unavailable distance must not be displayed as zero.");
            ApplyClub(state);
            if (DistanceText.Visibility == Visibility.Visible) throw new InvalidOperationException("Unavailable distance must be hidden on the Play page.");
        }
        finally { ApplyConfig(saved); ApplyClub(_state); }
    }

    private void VerifyUnsavedSettingsKept()
    {
        string original = GSProHost.Text;
        try
        {
            GSProHost.Text = "192.0.2.10";
            if (!_dirty || !SaveButton.IsEnabled) throw new InvalidOperationException("Editing a setting did not mark Settings as unsaved.");
            ApplyConfig(_config);
            if (GSProHost.Text != "192.0.2.10") throw new InvalidOperationException("An engine update overwrote an unsaved setting.");
            Discard_Click(this, new RoutedEventArgs());
            if (_dirty || GSProHost.Text != original) throw new InvalidOperationException("Discard did not restore the saved settings.");
        }
        finally { SetDirty(false); ApplySettingsFields(_config); }
    }

    private async Task WriteSmoke(string path, bool success, string error, IReadOnlyCollection<string> pages)
    {
        Directory.CreateDirectory(System.IO.Path.GetDirectoryName(System.IO.Path.GetFullPath(path))!);
        var result = new { success, error, engineConnected = _engine.Connected, stateReceived = _receivedState, configReceived = _loadedConfig, pages, verifiedCommands = _verifiedCommands, width = ActualWidth, height = ActualHeight, demo = Demo, hardwareActions = false };
        await File.WriteAllTextAsync(path, JsonSerializer.Serialize(result, new JsonSerializerOptions { WriteIndented = true }));
    }

    private sealed record CameraChoice(string Name, int Index, string Id);
    private sealed record WidthChoice(int Value, string Label);
    private static List<WidthChoice> WidthOptions(int saved) => Widths.Append(saved).Distinct().OrderBy(value => value)
        .Select(value => new WidthChoice(value, value switch { 640 => "Standard (640 px)", 800 => "Medium (800 px)", 1280 => "High (1280 px)", 1920 => "Full HD (1920 px)", _ => $"Saved profile ({value} px)" })).ToList();
    private sealed record ShotRow(string Time, string Source, string Speed, string Direction, string Status, string Message, Brush Strong, Brush Soft);
}
