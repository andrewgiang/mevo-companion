using System.Diagnostics;
using System.Globalization;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Windows;
using System.Windows.Automation;
using Condition = System.Windows.Automation.Condition;

namespace MevoCompanion.FsGolfReader;

internal static partial class Program
{
    private static readonly JsonSerializerOptions JsonOptions = new() { PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower };
    private static readonly string[] ProcessNames = ["FlightScopeVideoTeachingApp", "FSGolfPC", "FSGolf"];
    private static volatile bool _stopping;
    private static bool _inspect;
    private static readonly object OutputLock = new();
    private static readonly object InboxLock = new();
    private static readonly Queue<ModeCommand> ModeInbox = new();
    private static readonly Dictionary<string, ModeCommandResult> CommandResults = new();
    private static readonly Queue<string> CompletedCommands = new();

    [MTAThread]
    private static int Main(string[] args)
    {
        Console.OutputEncoding = new UTF8Encoding(false);
        if (args.Contains("--self-test")) return SelfTest();
        bool watch = args.Contains("--watch");
        _inspect = args.Contains("--inspect");
        if (watch)
            _ = Task.Run(() =>
            {
                // The Python owner keeps this private pipe open. EOF means the
                // owner has gone away; exit even if a UIA provider is hung.
                try
                {
                    while (Console.In.ReadLine() is { } line)
                    {
                        var command = ParseModeCommand(line, out var rejected);
                        if (command is null) { WriteMessage(rejected!); continue; }
                        bool queued;
                        lock (InboxLock)
                        {
                            queued = ModeInbox.Count < 8;
                            if (queued) ModeInbox.Enqueue(command);
                        }
                        if (!queued) WriteMessage(ModeReply(command, false, "FS Golf's command queue is busy. Retry after a fresh snapshot."));
                    }
                }
                catch (System.IO.IOException) { }
                Environment.Exit(0);
            });
        int index = Array.IndexOf(args, "--interval-ms");
        int interval = index >= 0 && index + 1 < args.Length && int.TryParse(args[index + 1], out int value) ? Math.Clamp(value, 100, 5000) : 350;
        Console.CancelKeyPress += (_, e) => { e.Cancel = true; _stopping = true; };
        bool ensureLive = args.Contains("--ensure-live");
        string? startupOutcome = null;
        do
        {
            var timer = Stopwatch.StartNew();
            if (watch)
            {
                ModeCommand? command;
                lock (InboxLock) command = ModeInbox.TryDequeue(out var pending) ? pending : null;
                if (command is not null)
                {
                    try { WriteMessage(ProcessModeCommand(command)); }
                    catch (System.IO.IOException) { return 0; }
                }
            }
            Snapshot result;
            try { result = ensureLive ? EnsureLive() : ReadSnapshot(); ensureLive = false; }
            catch (ElementNotAvailableException) { result = Failure("FS Golf changed its screen. Waiting for the current view."); }
            catch (UnauthorizedAccessException) { result = Failure("FS Golf accessibility is unavailable in this Windows session."); }
            catch (Exception error) { result = Failure("FS Golf could not be read: " + error.Message); }
            // The owner keeps only the latest snapshots. Repeat the terminal
            // outcome so a skipped first frame cannot trigger preparation twice.
            startupOutcome = result.StartupStatus ?? startupOutcome;
            result.StartupStatus = startupOutcome;
            if (string.IsNullOrEmpty(result.Timestamp))
            {
                result.Timestamp = DateTimeOffset.UtcNow.ToString("O", CultureInfo.InvariantCulture);
                result.DurationMs = timer.ElapsedMilliseconds;
            }
            try { WriteMessage(result); }
            catch (System.IO.IOException) { return 0; }
            if (watch && !_stopping) Thread.Sleep(Math.Max(10, interval - (int)timer.ElapsedMilliseconds));
        } while (watch && !_stopping);
        return 0;
    }

    private static Snapshot Failure(string message) => new() { Ok = false, Error = message };

    private static void WriteMessage<T>(T message)
    {
        string json = JsonSerializer.Serialize(message, JsonOptions);
        lock (OutputLock) { Console.WriteLine(json); Console.Out.Flush(); }
    }

    private static ModeCommand? ParseModeCommand(string line, out ModeCommandResult? rejected)
    {
        string? requestId = null, mode = null;
        rejected = null;
        try
        {
            if (line.Length > 4096) throw new FormatException("Reader commands must be at most 4096 characters.");
            using var document = JsonDocument.Parse(line);
            var root = document.RootElement;
            if (root.ValueKind != JsonValueKind.Object) throw new FormatException("Reader commands must be JSON objects.");
            if (root.TryGetProperty("request_id", out var id) && id.ValueKind == JsonValueKind.String) requestId = id.GetString();
            if (root.TryGetProperty("mode", out var value) && value.ValueKind == JsonValueKind.String) mode = value.GetString();
            var names = root.EnumerateObject().Select(property => property.Name).ToList();
            if (names.Count != 3 || names.Distinct(StringComparer.Ordinal).Count() != 3
                || names.Any(name => name is not ("type" or "request_id" or "mode"))) throw new FormatException("Use only type, request_id and mode in a reader command.");
            if (!root.TryGetProperty("type", out var type) || type.ValueKind != JsonValueKind.String || type.GetString() != "set_shot_mode")
                throw new FormatException("The supported reader command is set_shot_mode.");
            if (string.IsNullOrWhiteSpace(requestId) || requestId.Length > 128 || requestId.Any(char.IsControl))
                throw new FormatException("A nonempty request_id of at most 128 characters is required.");
            if (mode is not ("full_swing" or "chipping")) throw new FormatException("Shot mode must be full_swing or chipping.");
            return new ModeCommand(requestId, mode, Stopwatch.GetTimestamp());
        }
        catch (Exception error) when (error is JsonException or FormatException)
        {
            rejected = new ModeCommandResult(requestId?.Length <= 128 ? requestId : null, false, mode is "full_swing" or "chipping" ? mode : null,
                error is JsonException ? "Reader command JSON is invalid." : error.Message);
            return null;
        }
    }

    private static ModeCommandResult ModeReply(ModeCommand command, bool success, string message) => new(command.RequestId, success, command.Mode, message);

    private static ModeCommandResult ProcessModeCommand(ModeCommand command)
    {
        if (CommandResults.TryGetValue(command.RequestId, out var prior))
            return prior.Mode == command.Mode ? prior : ModeReply(command, false, "This request_id was already used for a different mode.");
        ModeCommandResult result;
        try { result = SelectShotMode(command); }
        catch (ElementNotAvailableException) { result = ModeReply(command, false, "FS Golf changed its controls. Retry after a fresh Ready snapshot."); }
        catch (Exception error) when (IsTransientUia(error)) { result = ModeReply(command, false, "FS Golf is rebuilding its controls. Retry after a fresh Ready snapshot."); }
        catch (Exception error) { result = ModeReply(command, false, "FS Golf could not change shot mode: " + error.Message); }
        CommandResults[command.RequestId] = result;
        CompletedCommands.Enqueue(command.RequestId);
        if (CompletedCommands.Count > 32) CommandResults.Remove(CompletedCommands.Dequeue());
        return result;
    }

    private static string? ModeSelectionBlock(Snapshot snapshot, bool modal)
    {
        if (modal) return "Complete the FS Golf dialog before changing shot mode.";
        if (!snapshot.Ok || snapshot.Window is null || !snapshot.LiveContext || snapshot.CaptureContext != "play_mode"
            || !snapshot.ContextStable || !snapshot.CounterStable || snapshot.ShotMode is not ("full_swing" or "chipping")
            || !IsLiveContext(snapshot.Evidence, snapshot.ShotMode)) return "Shot mode can change only in a verified live FS Golf Play Mode session.";
        if (snapshot.SelectedShot is null || snapshot.SelectedShot < 0 || snapshot.SelectedShot != snapshot.TotalShots)
            return "Select the latest FS Golf shot before changing mode.";
        if (snapshot.RadarStatus.Split('·')[0].Trim() != "Ready")
            return "Mevo+ is busy or asleep. Shot mode will wait for Ready.";
        return null;
    }

    private static ModeCommandResult SelectShotMode(ModeCommand command)
    {
        if (Stopwatch.GetElapsedTime(command.ReceivedAt).TotalSeconds > 5)
            return ModeReply(command, false, "This mode request expired. Retry with the current desired mode.");
        var current = ReadSnapshot();
        string? blocked = ModeSelectionBlock(current, false);
        if (blocked is not null) return ModeReply(command, false, blocked);
        var root = AutomationElement.FromHandle(new IntPtr(current.Window!.Hwnd));
        if (root.Current.ProcessId != current.Window.Pid) return ModeReply(command, false, "FS Golf's window changed.");
        var nodes = ReadChildren(root);
        var selectors = ReadModeSelectors(nodes);
        if (selectors.Count != 1) return ModeReply(command, false, "FS Golf's Full Swing/Chipping selector is unavailable or ambiguous.");
        var selector = selectors[0];
        string label = command.Mode == "chipping" ? "Chipping" : "Full Swing";
        var choices = selector.Choices.Where(node => node.ControlType == ControlType.ListItem && node.Enabled && !node.Offscreen && StartupLabelIs(node.Name, label)).ToList();
        if (choices.Count != 1 || !choices[0].Element.TryGetCurrentPattern(SelectionItemPattern.Pattern, out object pattern))
            return ModeReply(command, false, "The native shot-mode selector cannot be selected in this view.");
        var target = choices[0].Element;

        // Recheck the window, context, selection, modal state, and finally the
        // radar immediately before the one allowed native selection action.
        var counter = FindCounter(nodes);
        var final = new Snapshot
        {
            Ok = true, Window = current.Window, CaptureContext = "play_mode", ShotMode = selector.Selection.Mode,
            Evidence = ReadContextEvidence(nodes, selector.Selection), CounterStable = true,
            SelectedShot = counter.Selected, TotalShots = counter.Total,
        };
        final.LiveContext = IsLiveContext(final.Evidence, final.ShotMode);
        bool modal = HasModal(root, current.Window.Pid);
        final.RadarStatus = ReadRadarStatus(nodes, requireRadarControl: true);
        ApplySleepOverlay(final, nodes);
        blocked = ModeSelectionBlock(final, modal);
        if (blocked is not null) return ModeReply(command, false, blocked);
        if (counter.Total != current.TotalShots || counter.Selected != current.SelectedShot
            || root.Current.ProcessId != current.Window.Pid || !target.Current.IsEnabled || target.Current.IsOffscreen
            || !StartupLabelIs(target.Current.Name, label) || Stopwatch.GetElapsedTime(command.ReceivedAt).TotalSeconds > 5)
            return ModeReply(command, false, "FS Golf changed while preparing shot mode. Retry after the next Ready snapshot.");
        if (final.ShotMode == command.Mode) return ModeReply(command, true, "FS Golf already has the requested shot mode selected.");
        ((SelectionItemPattern)pattern).Select();
        return ModeReply(command, true, "Selected " + label + "; waiting for the next snapshot to confirm the mode.");
    }

    private static Snapshot EnsureLive()
    {
        var plan = new StartupPlan();
        var timer = Stopwatch.StartNew();
        while (true)
        {
            var current = ReadSnapshot();
            // The owner retries preparation when a delayed application window
            // appears. Do not consume that attempt while FS Golf is absent.
            if (current.Window is null) return current;
            var root = AutomationElement.FromHandle(new IntPtr(current.Window.Hwnd));
            var nodes = ReadChildren(root);
            var view = StartupViewOf(current, nodes, HasModal(root, current.Window.Pid));
            var action = plan.Next(view, timer.ElapsedMilliseconds);
            if (action == StartupAction.Complete)
            {
                current.StartupStatus = plan.Acted ? "prepared" : "already_live";
                return current;
            }
            if (action == StartupAction.Fail) return StartupFailure(plan.Message);

            // Preparation can legitimately exceed the owner's normal read
            // watchdog. Progress is a separate message, never shot data.
            WriteMessage(new { type = "startup_progress", message = plan.Message, window = current.Window });
            if (action == StartupAction.OpenPlayMode)
            {
                var buttons = nodes.Where(node => node.ControlType == ControlType.Button && StartupLabelIs(node.Name, "Play Mode") && node.Enabled && !node.Offscreen).ToList();
                if (buttons.Count != 1 || !InvokeExact(buttons[0].Element)) return StartupFailure("Open Play Mode in FS Golf to continue.");
            }
            else if (action == StartupAction.Wake)
            {
                var button = UniqueTextButton(nodes, "Wake");
                if (button is null || !InvokeExact(button)) return StartupFailure("Wake the radar in FS Golf to continue.");
            }
            Thread.Sleep(200);
        }
    }

    private enum StartupView { Unknown, Home, Setup, LiveWaiting, Sleeping, Ready, Modal, Review }
    private enum StartupAction { Wait, OpenPlayMode, Wake, Complete, Fail }

    private static StartupView StartupViewOf(Snapshot snapshot, List<Node> nodes, bool modal)
    {
        if (modal) return StartupView.Modal;
        // Home also exposes Quit application. Its exact supported markers must
        // be recognized before using that button as a review refusal marker.
        if (IsHome(nodes)) return StartupView.Home;
        if (IsSessionSetup(nodes)) return StartupView.Setup;
        if (snapshot.Evidence.QuitApplicationPresent) return StartupView.Review;
        if (snapshot.LiveContext)
        {
            if (HasSleepOverlay(nodes)) return StartupView.Sleeping;
            return snapshot.RadarStatus.Split('·').Any(part => part.Trim() == "Ready") ? StartupView.Ready : StartupView.LiveWaiting;
        }
        return StartupView.Unknown;
    }

    private static bool HasSleepOverlay(List<Node> nodes) => nodes.Any(node => node.ControlType == ControlType.Text
        && !node.Offscreen && StartupLabelIs(node.Name, "Radar is in Sleep Mode"));
    private static void ApplySleepOverlay(Snapshot snapshot, List<Node> nodes)
    {
        // FS Golf's header can briefly say Ready under its sleep overlay.
        // Preserve the observed device state for ordinary reads too.
        if (snapshot.LiveContext && HasSleepOverlay(nodes)) snapshot.RadarStatus = "Sleeping";
    }

    // Pure sequencing policy: wait for supported evidence, with one invocation
    // of each action at most. A transient loading window is observed passively;
    // a modal or review is refused immediately, and unknown views expire.
    private sealed class StartupPlan
    {
        private long _deadline = 15000;
        private bool _opened, _woke, _finished;
        public bool Acted => _opened || _woke;
        public string Message { get; private set; } = "Waiting for FS Golf to finish opening.";
        public StartupAction Next(StartupView view, long elapsedMs)
        {
            if (_finished) return StartupAction.Fail;
            if (view == StartupView.Modal) return Fail("Complete the FS Golf dialog before preparing its live session.");
            if (view == StartupView.Review) return Fail("Leave saved-shot review and open Play Mode from FS Golf's Home screen.");
            if (view == StartupView.Setup) return Fail("Open Play Mode from FS Golf's Home screen to switch between Full Swing and Chipping.");
            if (view == StartupView.Ready) { _finished = true; return StartupAction.Complete; }
            if (elapsedMs >= _deadline)
                return Fail(_woke ? "FS Golf has not reported the radar Ready after waking. Check the radar in FS Golf."
                    : _opened || view == StartupView.LiveWaiting ? "FS Golf's Play Mode is not Ready yet. Check the radar in FS Golf."
                    : "Open FS Golf's Home screen to prepare Play Mode.");
            if (view == StartupView.Sleeping && !_woke)
            {
                _woke = true;
                _deadline = elapsedMs + 12000;
                Message = "Waking the radar in FS Golf; waiting for Ready.";
                return StartupAction.Wake;
            }
            if (view == StartupView.Home && !_opened && !_woke)
            {
                _opened = true;
                _deadline = elapsedMs + 15000;
                Message = "Opening FS Golf's Play Mode; waiting for Ready.";
                return StartupAction.OpenPlayMode;
            }
            if (!Acted && view == StartupView.LiveWaiting) Message = "Waiting for FS Golf to report the radar Ready.";
            return StartupAction.Wait;
        }
        private StartupAction Fail(string message) { _finished = true; Message = message; return StartupAction.Fail; }
    }

    private static AutomationElement? UniqueTextButton(List<Node> nodes, string text)
    {
        var buttons = new List<AutomationElement>();
        foreach (var node in nodes.Where(node => node.ControlType == ControlType.Text && StartupLabelIs(node.Name, text) && !node.Offscreen))
        {
            var button = TreeWalker.ControlViewWalker.GetParent(node.Element);
            for (int depth = 0; depth < 3 && button is not null && button.Current.ControlType != ControlType.Button; depth++)
                button = TreeWalker.ControlViewWalker.GetParent(button);
            if (button is not null && button.Current.ControlType == ControlType.Button && button.Current.IsEnabled && !button.Current.IsOffscreen
                && !buttons.Any(existing => Automation.Compare(existing, button))) buttons.Add(button);
        }
        return buttons.Count == 1 ? buttons[0] : null;
    }

    private static bool StartupLabelIs(string actual, string expected) => WhitespacePattern().Replace(actual, " ").Trim() == expected;

    private static bool IsSessionSetup(List<Node> nodes) => nodes.Any(node => StartupLabelIs(node.Name, "Session Setup") && node.ControlType == ControlType.Text)
        && nodes.Any(node => node.Id == "RadarSetupBtn") && nodes.Any(node => node.Id == "ContentFrame");

    private static bool IsHome(List<Node> nodes) => nodes.Any(node => node.Id == "ContentFrame")
        && nodes.Any(node => node.ControlType == ControlType.Button && !node.Enabled && (StartupLabelIs(node.Name, "Home") || StartupLabelIs(node.Help, "Home")))
        && nodes.Any(node => node.ControlType == ControlType.Button && StartupLabelIs(node.Name, "Play Mode") && node.Enabled && !node.Offscreen)
        && nodes.Any(node => node.ControlType == ControlType.Button && StartupLabelIs(node.Name, "Review Session"));

    private static bool InvokeExact(AutomationElement button)
    {
        if (!button.Current.IsEnabled || button.Current.IsOffscreen || !button.TryGetCurrentPattern(InvokePattern.Pattern, out object pattern)) return false;
        ((InvokePattern)pattern).Invoke();
        return true;
    }

    private static bool HasModal(AutomationElement root, int pid)
    {
        if (!root.Current.IsEnabled) return true;
        var windows = AutomationElement.RootElement.FindAll(TreeScope.Children, new PropertyCondition(AutomationElement.ProcessIdProperty, pid));
        foreach (AutomationElement window in windows)
            if (window.TryGetCurrentPattern(WindowPattern.Pattern, out object pattern) && ((WindowPattern)pattern).Current.IsModal) return true;
        return false;
    }

    private static Snapshot StartupFailure(string message)
    {
        var result = ReadSnapshot();
        result.StartupStatus = "action_needed";
        result.Error = message;
        return result;
    }

    private static Snapshot ReadSnapshot() => ReadWithFreshRetries(ReadSnapshotCore, Thread.Sleep);

    private static bool IsTransientUia(Exception error) => error is ElementNotAvailableException
        || error.HResult == unchecked((int)0x80040201); // UIA_E_ELEMENTNOTAVAILABLE, including COM providers.

    private static Snapshot ReadWithFreshRetries(Func<Snapshot> acquire, Action<int> wait)
    {
        for (int attempt = 0; ; attempt++)
        {
            var timer = Stopwatch.StartNew();
            try
            {
                // acquire resolves the process/window and every node again.
                // Nothing from a partially rebuilt tree crosses this boundary.
                var result = acquire();
                result.Timestamp = DateTimeOffset.UtcNow.ToString("O", CultureInfo.InvariantCulture);
                result.DurationMs = timer.ElapsedMilliseconds;
                return result;
            }
            catch (Exception error) when (attempt < 2 && IsTransientUia(error))
            {
                wait(100);
            }
        }
    }

    private static bool IsMetricPanel(Node node) => node.Name.Contains("ParamModel", StringComparison.Ordinal)
        || (node.ControlType == ControlType.DataItem && RecognizeMetric([node.Name]) is not null);

    private static Snapshot ReadSnapshotCore()
    {
        Process? process = null;
        foreach (string name in ProcessNames)
        {
            process = Process.GetProcessesByName(name).FirstOrDefault(item => item.MainWindowHandle != IntPtr.Zero);
            if (process is not null) break;
        }
        if (process is null) return Failure("Waiting for FS Golf to open.");
        using (process)
        {
            var root = AutomationElement.FromHandle(process.MainWindowHandle);
            if (root is null) return Failure("FS Golf's window is unavailable.");
            var result = new Snapshot
            {
                Window = new WindowIdentity(process.Id, process.MainWindowHandle.ToInt64(), root.Current.Name),
            };
            var request = NewCache();
            List<Node> nodes;
            using (request.Activate())
                nodes = root.FindAll(TreeScope.Descendants, Condition.TrueCondition).Cast<AutomationElement>().Select(ReadCached).ToList();
            if (_inspect)
                result.Debug = nodes.Where(node => node.ControlType == ControlType.Button || node.ControlType == ControlType.List || node.ControlType == ControlType.ListItem || ParseTotal(node.Name) is not null || ParseInteger(node.Name) is not null)
                    .Select(node => new { node.Name, node.Id, node.Help, node.Enabled, node.Offscreen, node.Selected, rect = node.Rect.ToString(CultureInfo.InvariantCulture) }).ToArray();

            var playMode = ReadPlayModeSelection(nodes);
            result.Evidence = ReadContextEvidence(nodes, playMode);
            result.ShotMode = playMode.SelectorPresent ? playMode.Mode : "full_swing";
            result.CaptureContext = playMode.SelectorPresent ? "play_mode" : "full_swing_session";
            result.LiveContext = IsLiveContext(result.Evidence, result.ShotMode);

            result.RadarStatus = ReadRadarStatus(nodes);
            ApplySleepOverlay(result, nodes);

            var counter = FindCounter(nodes);
            result.SelectedShot = counter.Selected;
            result.TotalShots = counter.Total;
            foreach (var panel in nodes.Where(IsMetricPanel))
            {
                var children = ReadChildren(panel.Element);
                var displayValues = children.Where(node => node.Id.Equals("ValueTextBlock", StringComparison.OrdinalIgnoreCase)).ToList();
                if (displayValues.Count != 1) continue;
                string rawValue = displayValues[0].Name.Trim();
                var texts = children.Where(node => node.ControlType == ControlType.Text && node.Id != "ValueTextBlock").Select(node => node.Name.Trim()).Where(text => !string.IsNullOrEmpty(text)).ToList();
                var recognized = RecognizeMetric(texts, panel.Name);
                if (recognized is null) continue;
                var reading = new Reading(rawValue, recognized.Value.Unit);
                if (result.Readings.TryGetValue(recognized.Value.Key, out var prior) && prior != reading)
                {
                    result.Error = "Multiple different readings are exposed for one shot metric.";
                    return result;
                }
                result.Readings[recognized.Value.Key] = reading;
            }

            // A changing counter means the scan straddled an update. Do not
            // describe the mixed snapshot as coherent even if its values parse.
            if (counter.SelectedElement is not null && counter.TotalElement is not null)
            {
                int? finalSelected = ParseInteger(counter.SelectedElement.Current.Name);
                int? finalTotal = ParseTotal(counter.TotalElement.Current.Name);
                result.CounterStable = counter.Selected == finalSelected && counter.Total == finalTotal;
                if (!result.CounterStable)
                {
                    result.Error = "transient update";
                    return result;
                }
            }
            else result.CounterStable = false;
            // Mode changes can leave the previous shot visible, or expose a
            // different mode's ordinal. Never join metrics from both contexts.
            var finalMode = ReadPlayModeSelection(ReadLists(root));
            result.ContextStable = playMode.SelectorPresent == finalMode.SelectorPresent && playMode.Mode == finalMode.Mode;
            if (!result.ContextStable)
            {
                result.Error = "FS Golf is changing shot mode; existing readings are held.";
                return result;
            }
            result.Ok = true;
            if (!result.LiveContext) result.Error = playMode.SelectorPresent && playMode.Mode is null
                ? "Choose Full Swing or Chipping in FS Golf's Play Mode."
                : "Open FS Golf's Play Mode. Review and history views are held.";
            else if (result.TotalShots is null) result.Error = "FS Golf's live shot counter is not exposed in this view.";
            return result;
        }
    }

    private static CacheRequest NewCache()
    {
        var cache = new CacheRequest { TreeScope = TreeScope.Element };
        foreach (var property in new[] { AutomationElement.NameProperty, AutomationElement.AutomationIdProperty,
                     AutomationElement.ClassNameProperty, AutomationElement.ControlTypeProperty,
                     AutomationElement.IsEnabledProperty, AutomationElement.IsOffscreenProperty,
                     AutomationElement.BoundingRectangleProperty, AutomationElement.HelpTextProperty }) cache.Add(property);
        return cache;
    }

    private static Node ReadCached(AutomationElement element)
    {
        var info = element.Cached;
        return new Node(element, info.Name ?? "", info.AutomationId ?? "", info.ClassName ?? "", info.HelpText ?? "", info.ControlType, info.IsEnabled, info.IsOffscreen, info.BoundingRectangle);
    }

    private static bool IsLiveContext(ContextEvidence evidence, string? shotMode) => evidence.AutoPlaybackPresent
        && !evidence.QuitApplicationPresent
        && (evidence.PlayModeSelectorPresent
            ? evidence.ClubChangeEnabled && shotMode is "full_swing" or "chipping"
            : evidence.PlayerChangeEnabled);

    private static ContextEvidence ReadContextEvidence(List<Node> nodes, ModeSelection playMode) => new(
        nodes.Any(node => node.Id.Equals("AutoPlaybackButton", StringComparison.OrdinalIgnoreCase) && !node.Offscreen),
        nodes.Any(node => Normalize(node.Name + node.Help).Contains("clicktochangeplayer", StringComparison.Ordinal) && node.Enabled && !node.Offscreen),
        nodes.Any(node => Normalize(node.Name + node.Help).Contains("quitapplication", StringComparison.Ordinal) && !node.Offscreen),
        nodes.Any(node => node.ControlType == ControlType.Button && Normalize(node.Name + node.Help).Contains("clicktochangeclub", StringComparison.Ordinal) && node.Enabled && !node.Offscreen),
        playMode.SelectorPresent);

    private static string ReadRadarStatus(List<Node> nodes, bool requireRadarControl = false)
    {
        var radarNode = nodes.FirstOrDefault(node => node.Id.Contains("OperatorRadarControl", StringComparison.OrdinalIgnoreCase)
            || node.ClassName.Contains("OperatorRadarControl", StringComparison.OrdinalIgnoreCase)
            || node.Name.Contains("OperatorRadarControl", StringComparison.OrdinalIgnoreCase));
        if (radarNode is not null)
            return string.Join(" · ", ReadChildren(radarNode.Element).Where(node => node.ControlType == ControlType.Text)
                .Select(node => node.Name).Where(text => !string.IsNullOrWhiteSpace(text)).Distinct());
        if (requireRadarControl) return "";
        // A global Ready label cannot establish live context on its own.
        return string.Join(" · ", nodes.Where(node => node.ControlType == ControlType.Text && !node.Offscreen)
            .Select(node => node.Name.Trim()).Where(name => name is "Ready" or "Sleeping" or "Disconnected" or "Connecting" or "Busy" or "Limited Flight").Distinct());
    }

    private static List<ModeSelector> ReadModeSelectors(List<Node> nodes) => nodes
        .Where(node => node.ControlType == ControlType.List && node.Enabled && !node.Offscreen)
        .Select(node => ReadModeChoices(node.Element))
        .Select(choices => new ModeSelector(choices, ModeSelectionOf(choices)))
        .Where(selector => selector.Selection.SelectorPresent).ToList();

    private static ModeSelection ReadPlayModeSelection(List<Node> nodes)
    {
        var matches = ReadModeSelectors(nodes);
        return matches.Count == 1 ? matches[0].Selection : new ModeSelection(matches.Count > 1, null);
    }

    private static List<Node> ReadModeChoices(AutomationElement list)
    {
        // Query SelectionItem only on the two known mode options. Requesting
        // it for every cached descendant also touches virtualized shot-history
        // items whose provider can reject that pattern while new shots arrive.
        return ReadChildren(list).Select(node => node.ControlType == ControlType.ListItem
            && (StartupLabelIs(node.Name, "Full Swing") || StartupLabelIs(node.Name, "Chipping"))
            ? node with { Selected = node.Element.GetCurrentPropertyValue(SelectionItemPattern.IsSelectedProperty, true) is bool selected ? selected : null }
            : node).ToList();
    }

    // Both observed choices must share one native List. A stray label or a
    // previous screen's hidden controls cannot establish live Play Mode.
    private static ModeSelection ModeSelectionOf(List<Node> nodes)
    {
        var items = nodes.Where(node => node.ControlType == ControlType.ListItem && node.Enabled && !node.Offscreen).ToList();
        var swing = items.Where(node => StartupLabelIs(node.Name, "Full Swing")).ToList();
        var chip = items.Where(node => StartupLabelIs(node.Name, "Chipping")).ToList();
        if (swing.Count != 1 || chip.Count != 1) return new ModeSelection(false, null);
        string? mode = (swing[0].Selected, chip[0].Selected) switch
        {
            (true, false) => "full_swing",
            (false, true) => "chipping",
            _ => null,
        };
        return new ModeSelection(true, mode);
    }

    private static List<Node> ReadChildren(AutomationElement element)
    {
        using (NewCache().Activate())
            return element.FindAll(TreeScope.Descendants, Condition.TrueCondition).Cast<AutomationElement>().Select(ReadCached).ToList();
    }

    private static List<Node> ReadLists(AutomationElement element)
    {
        // Rechecking the mode must not retrieve every metric and control a
        // second time: UIA cross-process calls otherwise exceed the shot's
        // freshness budget. Only the native mode List is needed here.
        using (NewCache().Activate())
            return element.FindAll(TreeScope.Descendants, new PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.List))
                .Cast<AutomationElement>().Select(ReadCached).ToList();
    }

    internal static (string Key, string Unit)? RecognizeMetric(IEnumerable<string> labels, string panelName = "")
    {
        string joined = string.Join(" ", labels);
        string normalized = Normalize(joined);
        string unit = UnitPattern().Match(joined).Value.ToLowerInvariant();
        if (unit == "kph" || unit == "kmh") unit = "km/h";
        if (normalized.Contains("ballspeed")) return ("speed_mph", unit);
        if (normalized.Contains("clubspeed") || normalized.Contains("clubheadspeed")) return ("club_speed_mph", unit);
        if (normalized.Contains("spinaxis")) return ("spin_axis", "deg");
        if (normalized.Contains("launchv") || normalized.Contains("verticallaunch") || normalized.Contains("launchangle")) return ("vla", "deg");
        if (normalized.Contains("launchh") || normalized.Contains("horizontallaunch") || normalized.Contains("launchdirection")) return ("hla", "deg");
        // Back spin / side spin are different measurements and cannot substitute.
        if (normalized.Contains("backspin") || normalized.Contains("sidespin")) return null;
        if (normalized is "spin" or "spinrpm" || normalized.Contains("spinrate") || normalized.Contains("totalspin")) return ("spin_rpm", unit.Length == 0 ? "rpm" : unit);
        return null;
    }

    private static Counter FindCounter(List<Node> nodes)
    {
        var text = nodes.Where(node => node.ControlType == ControlType.Text && !node.Offscreen).ToList();
        foreach (var total in text)
        {
            int? count = ParseTotal(total.Name);
            if (count is null) continue;
            var selected = text.Where(node => ParseInteger(node.Name) is { } n && n <= count
                && !node.Rect.IsEmpty && !total.Rect.IsEmpty
                && ((Math.Abs(node.Rect.Top - total.Rect.Top) < Math.Max(node.Rect.Height, total.Rect.Height)
                     && node.Rect.Right <= total.Rect.Left + 5 && total.Rect.Left - node.Rect.Right < 150)
                    || (node.Rect.Bottom <= total.Rect.Top + 5 && total.Rect.Top - node.Rect.Bottom < 60
                        && Math.Abs((node.Rect.Left + node.Rect.Right) / 2 - (total.Rect.Left + total.Rect.Right) / 2) < 30)))
                .OrderBy(node => Math.Abs(total.Rect.Left - node.Rect.Left) + Math.Abs(total.Rect.Top - node.Rect.Top)).FirstOrDefault();
            if (selected is not null) return new Counter(ParseInteger(selected.Name), count, selected.Element, total.Element);
        }
        return new Counter(null, null, null, null);
    }

    internal static int? ParseInteger(string text) => IntegerPattern().IsMatch(text.Trim()) && int.TryParse(text.Trim(), out int value) ? value : null;
    internal static int? ParseTotal(string text)
    {
        var match = TotalPattern().Match(text.Trim());
        return match.Success && int.TryParse(match.Groups[1].Value, out int count) ? count : null;
    }
    private static string Normalize(string text) => NonLettersPattern().Replace(text.ToLowerInvariant(), "");
    [GeneratedRegex(@"[^a-z0-9]")] private static partial Regex NonLettersPattern();
    [GeneratedRegex(@"\s+")] private static partial Regex WhitespacePattern();
    [GeneratedRegex(@"(?i)mph|km/h|kph|kmh|m/s|rpm")] private static partial Regex UnitPattern();
    [GeneratedRegex(@"^\d+$")] private static partial Regex IntegerPattern();
    [GeneratedRegex(@"(?i)^of\s*(\d+)$")] private static partial Regex TotalPattern();

    private static int SelfTest()
    {
        var fixtures = new (string[] Labels, string Key, string Unit)[]
        {
            (["Ball Speed", "mph"], "speed_mph", "mph"),
            (["Club Speed", "km/h"], "club_speed_mph", "km/h"),
            (["Spin", "rpm"], "spin_rpm", "rpm"),
            (["Spin Axis", "°"], "spin_axis", "deg"),
            (["Launch V", "°"], "vla", "deg"),
            (["Launch H", "°"], "hla", "deg"),
        };
        foreach (var fixture in fixtures)
            if (RecognizeMetric(fixture.Labels) != (fixture.Key, fixture.Unit)) throw new InvalidOperationException("Metric label regression: " + string.Join(" ", fixture.Labels));
        if (RecognizeMetric(["Back Spin", "rpm"]) is not null || RecognizeMetric(["Side Spin", "rpm"]) is not null)
            throw new InvalidOperationException("Component spin must not become total spin.");
        if (ParseTotal("of3") != 3 || ParseTotal("of 0") != 0 || ParseTotal("3 of 5") is not null || ParseInteger("3.2") is not null)
            throw new InvalidOperationException("Counter parsing regression.");
        static Node Fixture(string name = "", string id = "", string help = "", bool enabled = true, ControlType? type = null, bool? selected = null, bool offscreen = false) => new(null!, name, id, "", help, type ?? ControlType.Button, enabled, offscreen, Rect.Empty, selected);
        var home = new List<Node> { Fixture(id: "ContentFrame", type: ControlType.Pane), Fixture(help: "Home", enabled: false), Fixture("Play Mode"), Fixture("Review Session") };
        if (!IsHome(home) || IsSessionSetup(home)) throw new InvalidOperationException("Home recognition regression.");
        if (IsHome(home.Where(node => node.Name != "Review Session").ToList())) throw new InvalidOperationException("Home requires its review marker.");
        if (IsHome(home.Where(node => node.Help != "Home").Append(Fixture(help: "Home")).ToList())) throw new InvalidOperationException("Enabled Home indicates a different screen.");
        var setup = new List<Node> { Fixture("Session Setup", type: ControlType.Text), Fixture(id: "RadarSetupBtn"), Fixture(id: "ContentFrame", type: ControlType.Pane) };
        if (!IsSessionSetup(setup) || IsHome(setup)) throw new InvalidOperationException("Session setup recognition regression.");
        if (IsSessionSetup(setup.Where(node => node.Id != "RadarSetupBtn").ToList())) throw new InvalidOperationException("Session setup requires its radar marker.");
        if (IsHome([]) || IsSessionSetup([])) throw new InvalidOperationException("Unknown views must not be navigated.");
        static void Expect(StartupPlan plan, StartupView view, long time, StartupAction action)
        {
            if (plan.Next(view, time) != action) throw new InvalidOperationException($"Startup regression: {view} at {time} expected {action}.");
        }
        var delayed = new StartupPlan();
        Expect(delayed, StartupView.Unknown, 0, StartupAction.Wait);
        Expect(delayed, StartupView.Unknown, 14000, StartupAction.Wait);
        Expect(delayed, StartupView.Home, 14500, StartupAction.OpenPlayMode);
        Expect(delayed, StartupView.Home, 19000, StartupAction.Wait);
        Expect(delayed, StartupView.Unknown, 20000, StartupAction.Wait);
        Expect(delayed, StartupView.Unknown, 23000, StartupAction.Wait);
        Expect(delayed, StartupView.LiveWaiting, 27000, StartupAction.Wait);
        Expect(delayed, StartupView.Ready, 27500, StartupAction.Complete);
        if (!delayed.Acted) throw new InvalidOperationException("Prepared session must record its actions.");
        var unknown = new StartupPlan();
        Expect(unknown, StartupView.Unknown, 15000, StartupAction.Fail);
        Expect(unknown, StartupView.Home, 15100, StartupAction.Fail);
        var review = new StartupPlan();
        Expect(review, StartupView.Review, 0, StartupAction.Fail);
        Expect(review, StartupView.Home, 200, StartupAction.Fail);
        var modal = new StartupPlan();
        Expect(modal, StartupView.Home, 0, StartupAction.OpenPlayMode);
        Expect(modal, StartupView.Modal, 200, StartupAction.Fail);
        var wake = new StartupPlan();
        Expect(wake, StartupView.Sleeping, 0, StartupAction.Wake);
        Expect(wake, StartupView.Sleeping, 5000, StartupAction.Wait);
        Expect(wake, StartupView.LiveWaiting, 11000, StartupAction.Wait);
        Expect(wake, StartupView.LiveWaiting, 12000, StartupAction.Fail);
        Expect(wake, StartupView.Sleeping, 12500, StartupAction.Fail);
        var awoken = new StartupPlan();
        Expect(awoken, StartupView.Sleeping, 0, StartupAction.Wake);
        Expect(awoken, StartupView.Ready, 1000, StartupAction.Complete);
        var ready = new StartupPlan();
        Expect(ready, StartupView.Ready, 0, StartupAction.Complete);
        if (ready.Acted) throw new InvalidOperationException("Ready live sessions must be left alone.");
        var sleepNodes = new List<Node> { Fixture("Radar is in Sleep Mode", type: ControlType.Text) };
        var live = new Snapshot { LiveContext = true, RadarStatus = "Ready · Limited Flight" };
        if (StartupViewOf(live, sleepNodes, false) != StartupView.Sleeping) throw new InvalidOperationException("Sleep overlay must outrank a stale Ready header.");
        ApplySleepOverlay(live, sleepNodes);
        if (live.RadarStatus != "Sleeping") throw new InvalidOperationException("Ordinary snapshots must not report a stale Ready header under the sleep overlay.");
        if (StartupViewOf(live, sleepNodes, true) != StartupView.Modal) throw new InvalidOperationException("An unrelated modal must block wake.");
        live.Evidence = new(false, false, true);
        if (StartupViewOf(live, sleepNodes, false) != StartupView.Review) throw new InvalidOperationException("Saved review must block all startup actions.");
        live.LiveContext = false;
        live.Evidence = new(false, false, false);
        if (StartupViewOf(live, sleepNodes, false) != StartupView.Unknown) throw new InvalidOperationException("Sleep text requires proven live-session context.");
        live.Evidence = new(false, false, true);
        if (StartupViewOf(live, home, false) != StartupView.Home) throw new InvalidOperationException("Home's Quit application button must not be mistaken for review.");
        if (StartupViewOf(live, setup, false) != StartupView.Setup) throw new InvalidOperationException("Known Session Setup must remain available.");
        var wrappedHome = new List<Node> { Fixture(id: "ContentFrame", type: ControlType.Pane), Fixture(help: "Home", enabled: false), Fixture("Play\r\nMode"), Fixture("Review\r\nSession") };
        if (!IsHome(wrappedHome) || StartupViewOf(live, wrappedHome, false) != StartupView.Home) throw new InvalidOperationException("Observed CRLF-wrapped Home labels must retain their known meaning.");
        if (!StartupLabelIs("Play\r\nMode", "Play Mode")) throw new InvalidOperationException("The Play Mode invocation must use the same normalized label as Home recognition.");
        if (!StartupLabelIs("  Quick\r\n Start\t", "Quick Start") || !StartupLabelIs("Radar is in\r\nSleep Mode", "Radar is in Sleep Mode")) throw new InvalidOperationException("Known startup labels may contain layout whitespace.");
        if (StartupLabelIs("Full Swing Sessions", "Full Swing Session") || StartupLabelIs("FullSwingSession", "Full Swing Session")) throw new InvalidOperationException("Whitespace normalization must preserve exact words.");
        var setupRefusal = new StartupPlan();
        Expect(setupRefusal, StartupView.Setup, 0, StartupAction.Fail);
        var playDirect = new StartupPlan();
        Expect(playDirect, StartupView.Home, 0, StartupAction.OpenPlayMode);
        Expect(playDirect, StartupView.Ready, 1000, StartupAction.Complete);
        var playChoices = new List<Node> { Fixture("Full Swing", type: ControlType.ListItem, selected: true), Fixture("Chipping", type: ControlType.ListItem, selected: false) };
        if (ModeSelectionOf(playChoices) != new ModeSelection(true, "full_swing")) throw new InvalidOperationException("Selected Full Swing must establish its mode identity.");
        var chipChoices = new List<Node> { Fixture("Full Swing", type: ControlType.ListItem, selected: false), Fixture("Chipping", type: ControlType.ListItem, selected: true) };
        if (ModeSelectionOf(chipChoices) != new ModeSelection(true, "chipping")) throw new InvalidOperationException("Selected Chipping must establish its mode identity.");
        var playEvidence = new ContextEvidence(true, false, false, true, true);
        if (!IsLiveContext(playEvidence, "full_swing") || !IsLiveContext(playEvidence, "chipping")) throw new InvalidOperationException("Play Mode must not require a Change Player button.");
        if (IsLiveContext(playEvidence with { QuitApplicationPresent = true }, "full_swing")) throw new InvalidOperationException("Review must remain excluded even when play controls exist.");
        if (IsLiveContext(playEvidence with { ClubChangeEnabled = false }, "full_swing") || IsLiveContext(playEvidence with { AutoPlaybackPresent = false }, "chipping")) throw new InvalidOperationException("Play Mode needs its full observed live evidence.");
        if (IsLiveContext(playEvidence, null) || IsLiveContext(playEvidence, "unknown")) throw new InvalidOperationException("An unknown Play Mode selection must hold readings.");
        if (!IsLiveContext(new(true, true, false), "full_swing") || IsLiveContext(new(true, false, false), "full_swing")) throw new InvalidOperationException("Existing full-swing session evidence must remain strict.");
        if (ModeSelectionOf([playChoices[0]]).SelectorPresent || ModeSelectionOf([playChoices[0], Fixture("Chipping", type: ControlType.Text)]).SelectorPresent) throw new InvalidOperationException("Labels alone cannot establish the Play Mode selector.");
        if (ModeSelectionOf([playChoices[0], chipChoices[1]]).Mode is not null || ModeSelectionOf([chipChoices[0], playChoices[1]]).Mode is not null) throw new InvalidOperationException("Zero or multiple selected modes must hold readings.");
        if (ModeSelectionOf([playChoices[0], Fixture("Chipping", type: ControlType.ListItem, selected: false, offscreen: true)]).SelectorPresent) throw new InvalidOperationException("Hidden mode choices are not live evidence.");
        if (ModeSelectionOf([playChoices[0], Fixture("Chipping", type: ControlType.ListItem, selected: false, enabled: false)]).SelectorPresent) throw new InvalidOperationException("Disabled mode choices are not live evidence.");
        if (ModeSelectionOf([playChoices[0], Fixture("Chipping", type: ControlType.ListItem)]).Mode is not null) throw new InvalidOperationException("Unavailable native selection state must hold readings.");
        if (!IsTransientUia(new ElementNotAvailableException())
            || !IsTransientUia(new System.Runtime.InteropServices.COMException("virtualized", unchecked((int)0x80040201)))
            || IsTransientUia(new InvalidOperationException("Element does not exist or it is virtualized"))
            || IsTransientUia(new UnauthorizedAccessException())) throw new InvalidOperationException("Retry classification must use the UIA exception/code, never message guessing.");
        int reads = 0;
        var waits = new List<int>();
        var retried = ReadWithFreshRetries(() =>
        {
            reads++;
            if (reads < 3) throw new System.Runtime.InteropServices.COMException("virtualized", unchecked((int)0x80040201));
            return new Snapshot { Ok = true, RadarStatus = "Tracking" };
        }, waits.Add);
        if (reads != 3 || !waits.SequenceEqual([100, 100]) || !retried.Ok || retried.RadarStatus != "Tracking"
            || string.IsNullOrEmpty(retried.Timestamp)) throw new InvalidOperationException("Transient UIA rebuilds must retry fresh reads with bounded short delays.");
        reads = 0;
        waits.Clear();
        try
        {
            ReadWithFreshRetries(() => { reads++; throw new ElementNotAvailableException(); }, waits.Add);
            throw new InvalidOperationException("Persistent provider failures must not become successful snapshots.");
        }
        catch (ElementNotAvailableException) { }
        if (reads != 3 || waits.Count != 2) throw new InvalidOperationException("Persistent UIA failures must escape after two retries.");
        reads = 0;
        waits.Clear();
        try
        {
            ReadWithFreshRetries(() => { reads++; throw new UnauthorizedAccessException(); }, waits.Add);
            throw new InvalidOperationException("Permanent errors must not be suppressed.");
        }
        catch (UnauthorizedAccessException) { }
        if (reads != 1 || waits.Count != 0) throw new InvalidOperationException("Permanent errors must not be retried.");
        if (!IsMetricPanel(Fixture("FlightScope.Parameters.ParamModel", type: ControlType.DataItem))
            || !IsMetricPanel(Fixture("Ball Speed", type: ControlType.DataItem))
            || IsMetricPanel(Fixture("FlightScope.ShotHistoryModel", type: ControlType.DataItem))
            || IsMetricPanel(Fixture("", type: ControlType.DataItem))) throw new InvalidOperationException("Only recognized metric panels may be traversed; unrelated virtualized history items are excluded.");
        int commandTests = 0;
        foreach (string mode in new[] { "full_swing", "chipping" })
        {
            var parsed = ParseModeCommand(JsonSerializer.Serialize(new { type = "set_shot_mode", mode, request_id = "mode-1" }), out var rejected);
            if (parsed is null || rejected is not null || parsed.Mode != mode || parsed.RequestId != "mode-1") throw new InvalidOperationException("Supported shot-mode commands must parse exactly.");
            commandTests++;
        }
        foreach (string invalid in new[] {
            "not json", "[]", "{}", "null",
            "{\"type\":\"set_shot_mode\",\"mode\":\"chipping\"}",
            "{\"type\":\"set_shot_mode\",\"mode\":\"Chipping\",\"request_id\":\"x\"}",
            "{\"type\":\"set_shot_mode\",\"mode\":\"putting\",\"request_id\":\"x\"}",
            "{\"type\":\"set_shot_mode\",\"mode\":\"chipping\",\"request_id\":4}",
            "{\"type\":\"set_shot_mode\",\"mode\":\"chipping\",\"request_id\":\" \"}",
            "{\"type\":\"set_shot_mode\",\"mode\":\"chipping\",\"request_id\":\"x\\ny\"}",
            "{\"type\":\"click\",\"mode\":\"chipping\",\"request_id\":\"x\"}",
            "{\"type\":\"set_shot_mode\",\"mode\":\"chipping\",\"request_id\":\"x\",\"force\":true}",
            "{\"type\":\"set_shot_mode\",\"mode\":\"chipping\",\"request_id\":\"x\",\"request_id\":\"y\"}",
            new string('x', 4097), JsonSerializer.Serialize(new { type = "set_shot_mode", mode = "chipping", request_id = new string('x', 129) }) })
        {
            if (ParseModeCommand(invalid, out var rejected) is not null || rejected is null || rejected.Success)
                throw new InvalidOperationException("Invalid mode commands must be rejected before native actions.");
            commandTests++;
        }
        static Snapshot ReadyPlay() => new()
        {
            Ok = true, Window = new(10, 20, "FS Golf"), LiveContext = true, CaptureContext = "play_mode",
            ContextStable = true, CounterStable = true, ShotMode = "full_swing", SelectedShot = 10, TotalShots = 10,
            RadarStatus = "Ready · Limited Flight", Evidence = new(true, false, false, true, true),
        };
        if (ModeSelectionBlock(ReadyPlay(), false) is not null || ModeSelectionBlock(ReadyPlay(), true) is null)
            throw new InvalidOperationException("Mode selection needs a proven Ready Play Mode without modal dialogs.");
        commandTests += 2;
        foreach (string status in new[] { "Tracking…", "Tracking", "Arming…", "Connected · Limited Flight", "Sleeping", "Disconnected", "", "Busy" })
        {
            var state = ReadyPlay(); state.RadarStatus = status;
            if (ModeSelectionBlock(state, false) is null) throw new InvalidOperationException("Busy/unknown radar states must never change modes.");
            commandTests++;
        }
        foreach (Action<Snapshot> alter in new Action<Snapshot>[] {
            value => value.Ok = false, value => value.Window = null, value => value.LiveContext = false,
            value => value.CaptureContext = "full_swing_session", value => value.ContextStable = false,
            value => value.CounterStable = false, value => value.ShotMode = null, value => value.SelectedShot = 9,
            value => value.SelectedShot = null, value => value.TotalShots = null,
            value => value.Evidence = value.Evidence with { QuitApplicationPresent = true },
            value => value.Evidence = value.Evidence with { ClubChangeEnabled = false },
            value => value.Evidence = value.Evidence with { PlayModeSelectorPresent = false } })
        {
            var state = ReadyPlay(); alter(state);
            if (ModeSelectionBlock(state, false) is null) throw new InvalidOperationException("Unverified, historical, or unstable views cannot change modes.");
            commandTests++;
        }
        var expiry = SelectShotMode(new("expired-test", "chipping", Stopwatch.GetTimestamp() - Stopwatch.Frequency * 6));
        if (expiry.Success || !expiry.Message.Contains("expired")) throw new InvalidOperationException("Queued stale requests must expire before native reads or actions.");
        commandTests++;
        var completed = new ModeCommandResult("duplicate-test", true, "chipping", "already completed");
        CommandResults[completed.RequestId!] = completed;
        if (ProcessModeCommand(new("duplicate-test", "chipping", Stopwatch.GetTimestamp())) != completed
            || ProcessModeCommand(new("duplicate-test", "full_swing", Stopwatch.GetTimestamp())).Success)
            throw new InvalidOperationException("Duplicate command ids must not invoke native selection again.");
        CommandResults.Clear();
        commandTests += 2;
        using var resultJson = JsonDocument.Parse(JsonSerializer.Serialize(completed, JsonOptions));
        if (resultJson.RootElement.GetProperty("type").GetString() != "command_result"
            || resultJson.RootElement.GetProperty("request_id").GetString() != "duplicate-test")
            throw new InvalidOperationException("The command result JSON contract must retain snake_case fields.");
        commandTests++;
        Console.WriteLine(JsonSerializer.Serialize(new { success = true, tests = 78 + commandTests }, JsonOptions));
        return 0;
    }

    private sealed record Node(AutomationElement Element, string Name, string Id, string ClassName, string Help, ControlType ControlType, bool Enabled, bool Offscreen, Rect Rect, bool? Selected = null);
    private sealed record ModeSelection(bool SelectorPresent, string? Mode);
    private sealed record ModeSelector(List<Node> Choices, ModeSelection Selection);
    private sealed record ModeCommand(string RequestId, string Mode, long ReceivedAt);
    private sealed record ModeCommandResult(string? RequestId, bool Success, string? Mode, string Message)
    {
        public string Type => "command_result";
    }
    private sealed record Counter(int? Selected, int? Total, AutomationElement? SelectedElement, AutomationElement? TotalElement);
    private sealed record WindowIdentity(int Pid, long Hwnd, string Title);
    private sealed record Reading(string Text, string Unit);
    private sealed record ContextEvidence(bool AutoPlaybackPresent, bool PlayerChangeEnabled, bool QuitApplicationPresent, bool ClubChangeEnabled = false, bool PlayModeSelectorPresent = false);
    private sealed class Snapshot
    {
        public string Type { get; set; } = "snapshot";
        public bool Ok { get; set; }
        public string Timestamp { get; set; } = "";
        public long DurationMs { get; set; }
        public WindowIdentity? Window { get; set; }
        public bool LiveContext { get; set; }
        public string CaptureContext { get; set; } = "";
        public string? ShotMode { get; set; }
        public bool ContextStable { get; set; } = true;
        public string RadarStatus { get; set; } = "";
        public int? SelectedShot { get; set; }
        public int? TotalShots { get; set; }
        public bool CounterStable { get; set; }
        public Dictionary<string, Reading> Readings { get; set; } = new();
        public ContextEvidence Evidence { get; set; } = new(false, false, false);
        public string? Error { get; set; }
        [System.Text.Json.Serialization.JsonIgnore(Condition = System.Text.Json.Serialization.JsonIgnoreCondition.WhenWritingNull)]
        public string? StartupStatus { get; set; }
        [System.Text.Json.Serialization.JsonIgnore(Condition = System.Text.Json.Serialization.JsonIgnoreCondition.WhenWritingNull)]
        public object? Debug { get; set; }
    }
}
