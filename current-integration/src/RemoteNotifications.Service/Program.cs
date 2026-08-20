using System.Buffers.Binary;
using System.Globalization;
using System.IO.Pipes;
using System.Net.Sockets;
using System.Security;
using System.Text.Json;
using System.Text.Json.Nodes;
using MyPowerTools.Ipc;
using MyPowerTools.Abstractions;
using MyPowerTools.Platform.Abstractions;
using MyPowerTools.Platform.Mac;
using MyPowerTools.RemoteNotifications.Configuration;
using RemoteNotifications.Surface.Services;
using RemoteNotifications.Service;

// Remote Notifications Service Unit — supervised by MyPowerTools.ServiceManager.
//
// Role: own the long-running signed-pull polling loop, dedup, persisted history,
// topic/label indexing and platform-native banner dispatch with a life independent of the
// Shell and the Runner. Before this process existed the poll loop lived inside the
// Surface/module adapter and stopped whenever the Shell window closed or the Runner
// recycled; notifications delivered while no UI was open were lost. This worker keeps
// polling, persists every accepted message to the same legacy store the Surface reads,
// and raises a Windows toast, so a message delivered while the Shell is minimized to the
// tray or fully closed is still recorded and surfaced on next open.
//
// Transport: Windows uses a named pipe and macOS uses a Unix domain socket. Both speak
// the same length-prefixed binary-JSON framing so the ServiceManager readiness probe and
// command proxying reuse one wire shape. Supported commands: `ping` (readiness),
// `state`/`get_state` (status snapshot),
// `inject` (drop a unique test message into the history — used by the process tests and
// the Shell "trigger test message" acceptance).

var activationUri = ProductActivationLauncher.GetLaunchUri(args);
if (!string.IsNullOrWhiteSpace(activationUri))
{
    return ProductActivationLauncher.TryLaunch(activationUri) ? 0 : 2;
}

if (OperatingSystem.IsWindows())
{
    try
    {
        // Installation and upgrade can replace the worker before the next banner arrives.
        // Register the protocol at service startup so every existing toast stays actionable.
        WorkerToastPlatform.EnsureRegistered();
    }
    catch (Exception exception)
    {
        try { Console.Error.WriteLine($"Remote Notifications activation registration failed: {exception.Message}"); }
        catch { }
    }
}

var pipeName = GetOption(args, "--pipe") ?? "remote-notifications.core";
var socketPath = GetOption(args, "--socket");
if (!OperatingSystem.IsWindows() && string.IsNullOrWhiteSpace(socketPath))
{
    socketPath = Path.Combine(Path.GetTempPath(), "mypowertools", "remote-notifications.core.sock");
}
var heartbeatFile = GetOption(args, "--heartbeat-file");
INotificationService? desktopNotifications = OperatingSystem.IsMacOS()
    ? new MacUserNotificationService()
    : null;

using var cts = new CancellationTokenSource();
Console.CancelKeyPress += (_, e) =>
{
    e.Cancel = true;
    cts.Cancel();
};
AppDomain.CurrentDomain.ProcessExit += (_, _) => cts.Cancel();

var pid = Environment.ProcessId;
Console.WriteLine($"RemoteNotifications.Service starting pid={pid} endpoint={socketPath ?? pipeName}");

var state = new WorkerState();
var pollGate = new object();
var startupBackfillPending = true;
var pipeCts = CancellationTokenSource.CreateLinkedTokenSource(cts.Token);
_ = Task.Run(() => socketPath is null
    ? ServeControlPipe(pipeName, state, pollGate, desktopNotifications, pipeCts.Token)
    : ServeControlSocket(socketPath, state, pollGate, desktopNotifications, pipeCts.Token));

// Polling loop: drives the real signed-pull, dedup, persistence and banner path.
// Settings are reloaded every cycle so the operator can change endpoint, channel,
// interval and banner toggle without restarting the worker (mirrors the legacy
// sidecar behaviour that read the product settings file each iteration).
try
{
    while (!cts.Token.IsCancellationRequested)
    {
        try
        {
            lock (pollGate)
            {
                RunOnePollCycle(state, desktopNotifications, startupBackfillPending);
                startupBackfillPending = false;
            }
        }
        catch (OperationCanceledException) when (cts.Token.IsCancellationRequested)
        {
            break;
        }
        catch (Exception ex)
        {
            // A single failed cycle must not kill the worker; record and back off.
            state.RecordFailure(ex.Message);
            try { Console.Error.WriteLine($"RemoteNotifications.Service poll error: {ex.Message}"); } catch { }
        }

        var heartbeat = $"heartbeat pid={pid} ts={DateTimeOffset.UtcNow:O}";
        Console.WriteLine(heartbeat);
        if (!string.IsNullOrEmpty(heartbeatFile))
        {
            try { await File.AppendAllTextAsync(heartbeatFile, heartbeat + Environment.NewLine, cts.Token); }
            catch { /* heartbeat file is best-effort */ }
        }

        try
        {
            var delaySeconds = Math.Clamp(state.CurrentPollIntervalSeconds, 5, 3600);
            await Task.Delay(TimeSpan.FromSeconds(delaySeconds), cts.Token);
        }
        catch (TaskCanceledException)
        {
            break;
        }
    }
}
catch (OperationCanceledException)
{
    // expected on stop
}

Console.WriteLine($"RemoteNotifications.Service stopping pid={pid}");
return 0;

// ---------------------------------------------------------------------------
// Single poll cycle. Mirrors RemoteNotificationBackgroundReceiver.PollAsync plus the
// Surface banner dispatch, consolidated into one owned path.
// ---------------------------------------------------------------------------
static void RunOnePollCycle(
    WorkerState state,
    INotificationService? desktopNotifications,
    bool startupBackfill = false)
{
    var settingsStore = new RemoteNotificationSettingsStore();
    var settings = settingsStore.Load();
    state.ApplySettings(settings);

    var store = new RemoteNotificationsLegacyStore(settingsStore);
    var poller = new RemoteNotificationHttpPoller(settings);

    var snapshot = store.Load();
    // A cleanup migration can leave a short local tail while the server still
    // has valid human records older than that tail. Pull the complete retained
    // server window once per worker start so those records can be rebuilt.
    var waterline = startupBackfill ? "" : ResolveWaterline(snapshot.MessagesOldestFirst);
    var seen = new RemoteNotificationSeenIdRing(snapshot.SeenMessageIds);
    foreach (var message in snapshot.MessagesOldestFirst)
    {
        seen.TryAccept(RemoteNotificationsLegacyStore.StableId(message));
        seen.TryAccept(RemoteNotificationsLegacyStore.FallbackId(message));
    }

    var pull = poller.PullAsync(
        waterline,
        CancellationToken.None,
        startupBackfill ? RemoteNotificationsLegacyStore.MaximumMessages : null)
        .GetAwaiter()
        .GetResult();
    var sane = pull.Notifications
        .Where(IsSane)
        .OrderBy(NotificationTime)
        .ToArray();

    if (sane.Length > 0)
    {
        waterline = NotificationTime(sane[^1]).ToUniversalTime().ToString("O", CultureInfo.InvariantCulture);
    }

    if (!pull.IsSuccess || sane.Length == 0)
    {
        state.RecordPoll(pull.State, pull.Error, pull.Notifications.Count, accepted: 0, shown: 0);
        return;
    }

    var accepted = new List<RemoteNotificationRecord>();
    foreach (var notification in sane)
    {
        if (seen.TryAccept(
                RemoteNotificationsLegacyStore.StableId(notification),
                RemoteNotificationsLegacyStore.FallbackId(notification)))
        {
            accepted.Add(notification);
        }
    }

    if (accepted.Count == 0)
    {
        state.RecordPoll(pull.State, pull.Error, pull.Notifications.Count, accepted: 0, shown: 0);
        return;
    }

    var merged = snapshot.MessagesOldestFirst
        .Concat(accepted)
        .GroupBy(RemoteNotificationsLegacyStore.StableId, StringComparer.Ordinal)
        .Select(group => group.Last())
        .OrderBy(NotificationTime)
        .TakeLast(RemoteNotificationsLegacyStore.MaximumMessages)
        .ToArray();
    store.SaveMessages(merged);
    store.SaveSeenMessageIds(seen.OldestFirst);

    var labels = snapshot.KnownLabels.ToList();
    foreach (var notification in accepted)
    {
        var label = RemoteNotificationsLegacyStore.ExtractLabel(notification.Message);
        labels.Remove(label);
        labels.Insert(0, label);
    }
    store.SaveKnownLabels(labels);

    // Backfill is historical reconstruction. Persist it without replaying
    // hundreds of old Windows banners after every worker restart.
    var shown = startupBackfill
        ? 0
        : DispatchBanners(accepted, settings.KeepWindowsBanners, desktopNotifications);
    state.RecordPoll(pull.State, pull.Error, pull.Notifications.Count, accepted.Count, shown);
}

// Banner dispatch. On Windows, send a real toast via the same COM ABI the Surface uses.
// The worker runs with no Avalonia lifetime, so it constructs the Windows platform
// directly rather than through the Surface factory that checks for a desktop lifetime.
static int DispatchBanners(
    IReadOnlyList<RemoteNotificationRecord> accepted,
    bool keepWindowsBanners,
    INotificationService? desktopNotifications)
{
    if (!OperatingSystem.IsWindows() && desktopNotifications is null)
    {
        return 0;
    }

    var shown = 0;
    foreach (var notification in accepted)
    {
        var envelope = BuildToastEnvelope(notification, persistent: keepWindowsBanners);
        try
        {
            if (OperatingSystem.IsWindows())
            {
                var result = WorkerToastPlatform.Show(envelope);
                if (result.Shown)
                {
                    shown++;
                }
            }
            else if (desktopNotifications is not null)
            {
                var productActivationUri = ToolActivationProtocol.CreateProductActivationUri(
                    new ToolActivationRequest("remote-notifications", "inbox", envelope.LaunchUri)
                    {
                        SuppressShellWindow = true
                    });
                desktopNotifications.PublishAsync(
                    new DesktopNotificationRequest(
                        envelope.MessageId,
                        envelope.Title,
                        envelope.Body,
                        productActivationUri.AbsoluteUri),
                    CancellationToken.None).GetAwaiter().GetResult();
                shown++;
            }
        }
        catch (Exception ex)
        {
            try { Console.Error.WriteLine($"RemoteNotifications.Service toast error: {ex.Message}"); } catch { }
        }
    }

    return shown;
}

// Build a toast envelope identical in shape to RemoteNotificationWindowsToastPublisher.
static WorkerToastEnvelope BuildToastEnvelope(RemoteNotificationRecord notification, bool persistent)
{
    var sourceMessage = RemoteNotificationsLegacyStore.StripLeadingQuotedRequest(notification.Message ?? "");
    var label = RemoteNotificationsLegacyStore.ExtractLabel(sourceMessage);
    var hasLabel = !string.Equals(label, "(unlabeled)", StringComparison.Ordinal);
    var prefix = hasLabel ? $"[{label}]" : "";
    var title = hasLabel
        ? label
        : string.IsNullOrWhiteSpace(notification.Channel) ? "Notification" : notification.Channel;
    var body = hasLabel && sourceMessage.StartsWith(prefix, StringComparison.Ordinal)
        ? sourceMessage[prefix.Length..].TrimStart()
        : sourceMessage;
    title = NormalizeToastText(title, 140);
    body = NormalizeToastText(body, 900);
    var stableId = RemoteNotificationsLegacyStore.StableId(notification);
    return new WorkerToastEnvelope(
        stableId,
        title.Length > 0 ? title : "MyPowerTools",
        body,
        persistent ? "reminder" : "",
        stableId.Length <= 16 ? stableId : stableId[..16],
        "page1",
        $"mypowertools://remote-notification?id={Uri.EscapeDataString(stableId)}");
}

static string NormalizeToastText(string value, int maximumLength)
{
    var cleaned = new string((value ?? "")
        .Select(character => character is '\t' or '\n' or '\r' || character >= ' '
            ? character
            : ' ')
        .ToArray());
    cleaned = string.Join(' ', cleaned.Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries));
    return cleaned.Length <= maximumLength
        ? cleaned
        : $"{cleaned[..(maximumLength - 3)]}...";
}

// ---------------------------------------------------------------------------
// Control pipe. Speaks the Chromium native-messaging wire format (4-byte LE length
// header + UTF-8 JSON body), matching the ScreenEase Service Unit so a single
// ServiceManager readiness probe and command shape works across units.
// ---------------------------------------------------------------------------
static async Task ServeControlPipe(
    string name,
    WorkerState state,
    object pollGate,
    INotificationService? desktopNotifications,
    CancellationToken cancellationToken)
{
    while (!cancellationToken.IsCancellationRequested)
    {
        NamedPipeServerStream? server = null;
        try
        {
            server = MptNamedPipePolicy.CreateServer(name);
            await server.WaitForConnectionAsync(cancellationToken);

            await ServeControlClient(server, state, pollGate, desktopNotifications, cancellationToken);
        }
        catch (OperationCanceledException)
        {
            break;
        }
        catch (Exception ex)
        {
            try { Console.Error.WriteLine($"RemoteNotifications.Service pipe error: {ex.Message}"); } catch { }
        }
        finally
        {
            server?.Dispose();
        }
    }
}

static async Task ServeControlSocket(
    string path,
    WorkerState state,
    object pollGate,
    INotificationService? desktopNotifications,
    CancellationToken cancellationToken)
{
    path = Path.GetFullPath(path);
    Directory.CreateDirectory(Path.GetDirectoryName(path)!);
    if (File.Exists(path))
    {
        File.Delete(path);
    }

    using var listener = new Socket(AddressFamily.Unix, SocketType.Stream, ProtocolType.Unspecified);
    listener.Bind(new UnixDomainSocketEndPoint(path));
    listener.Listen(8);
    try
    {
        while (!cancellationToken.IsCancellationRequested)
        {
            try
            {
                using var client = await listener.AcceptAsync(cancellationToken);
                await using var stream = new NetworkStream(client, ownsSocket: false);
                await ServeControlClient(stream, state, pollGate, desktopNotifications, cancellationToken);
            }
            catch (OperationCanceledException)
            {
                break;
            }
            catch (Exception ex)
            {
                try { Console.Error.WriteLine($"RemoteNotifications.Service socket error: {ex.Message}"); } catch { }
            }
        }
    }
    finally
    {
        if (File.Exists(path))
        {
            File.Delete(path);
        }
    }
}

static async Task ServeControlClient(
    Stream stream,
    WorkerState state,
    object pollGate,
    INotificationService? desktopNotifications,
    CancellationToken cancellationToken)
{
    while (!cancellationToken.IsCancellationRequested)
    {
        var request = await ReadFramedMessageAsync(stream, cancellationToken);
        if (request is null)
        {
            return;
        }

        var command = ExtractCommand(request);
        object? data = command switch
        {
            "ping" => new { pong = true },
            "state" or "get_state" => state.ToStateObject(),
            "poll" => HandlePoll(state, pollGate, desktopNotifications),
            "inject" => HandleInject(request, state, pollGate),
            _ => null
        };
        var ok = data is not null;
        await WriteFramedMessageAsync(stream, new
        {
            ok,
            command,
            data,
            error = ok ? null : $"Unknown command '{command}'."
        }, cancellationToken);
    }
}

// `inject` writes a unique test message straight into the persisted history so the
// process tests and the Shell "trigger test message" acceptance can prove the worker
// owns persistence and the Surface reads it back without any UI involvement.
static object HandlePoll(
    WorkerState state,
    object pollGate,
    INotificationService? desktopNotifications)
{
    lock (pollGate)
    {
        RunOnePollCycle(state, desktopNotifications);
        return state.ToStateObject();
    }
}

static object HandleInject(JsonDocument request, WorkerState state, object pollGate)
{
    lock (pollGate)
    {
        return HandleInjectCore(request, state);
    }
}

static object HandleInjectCore(JsonDocument request, WorkerState state)
{
    var idSuffix = Guid.NewGuid().ToString("N")[..8];
    var now = DateTimeOffset.UtcNow;
    var record = new RemoteNotificationRecord(
        $"test-inject-{idSuffix}",
        "default",
        $"[test] Service Unit injected message {idSuffix} at {now:O}",
        "info",
        now.ToString("O", CultureInfo.InvariantCulture),
        now.ToString("O", CultureInfo.InvariantCulture));

    var settingsStore = new RemoteNotificationSettingsStore();
    var store = new RemoteNotificationsLegacyStore(settingsStore);
    var snapshot = store.Load();
    var seen = new RemoteNotificationSeenIdRing(snapshot.SeenMessageIds);
    foreach (var message in snapshot.MessagesOldestFirst)
    {
        seen.TryAccept(RemoteNotificationsLegacyStore.StableId(message));
        seen.TryAccept(RemoteNotificationsLegacyStore.FallbackId(message));
    }

    if (!seen.TryAccept(RemoteNotificationsLegacyStore.StableId(record), RemoteNotificationsLegacyStore.FallbackId(record)))
    {
        return new { injected = false, reason = "duplicate" };
    }

    var merged = snapshot.MessagesOldestFirst
        .Append(record)
        .OrderBy(NotificationTime)
        .TakeLast(RemoteNotificationsLegacyStore.MaximumMessages)
        .ToArray();
    store.SaveMessages(merged);
    store.SaveSeenMessageIds(seen.OldestFirst);

    var labels = snapshot.KnownLabels.ToList();
    var label = RemoteNotificationsLegacyStore.ExtractLabel(record.Message);
    labels.Remove(label);
    labels.Insert(0, label);
    store.SaveKnownLabels(labels);

    state.RecordInject(RemoteNotificationsLegacyStore.StableId(record));
    return new { injected = true, messageId = RemoteNotificationsLegacyStore.StableId(record), historyCount = merged.Length };
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
static string ResolveWaterline(IReadOnlyList<RemoteNotificationRecord> messages)
{
    var newest = messages
        .Where(message => !string.IsNullOrWhiteSpace(message.ServerTimestamp))
        .Select(message => TryParse(message.ServerTimestamp, out var parsed) ? parsed : DateTimeOffset.MinValue)
        .Where(timestamp => timestamp > DateTimeOffset.MinValue && timestamp <= DateTimeOffset.UtcNow.AddMinutes(2))
        .DefaultIfEmpty(DateTimeOffset.MinValue)
        .Max();
    return newest == DateTimeOffset.MinValue
        ? ""
        : newest.ToUniversalTime().ToString("O", CultureInfo.InvariantCulture);
}

static bool IsSane(RemoteNotificationRecord notification)
{
    var timestamp = string.IsNullOrWhiteSpace(notification.ServerTimestamp)
        ? notification.Timestamp
        : notification.ServerTimestamp;
    return TryParse(timestamp, out var parsed) && parsed <= DateTimeOffset.UtcNow.AddMinutes(2);
}

static DateTimeOffset NotificationTime(RemoteNotificationRecord notification)
{
    var timestamp = string.IsNullOrWhiteSpace(notification.ServerTimestamp)
        ? notification.Timestamp
        : notification.ServerTimestamp;
    return TryParse(timestamp, out var parsed) ? parsed : DateTimeOffset.MinValue;
}

static bool TryParse(string value, out DateTimeOffset parsed)
{
    return DateTimeOffset.TryParse(
        value,
        CultureInfo.InvariantCulture,
        DateTimeStyles.AllowWhiteSpaces | DateTimeStyles.AssumeUniversal,
        out parsed);
}

static async Task<JsonDocument?> ReadFramedMessageAsync(Stream stream, CancellationToken cancellationToken)
{
    var header = new byte[4];
    var read = 0;
    while (read < 4)
    {
        var n = await stream.ReadAsync(header.AsMemory(read, 4 - read), cancellationToken);
        if (n == 0)
        {
            return read == 0 ? null : throw new EndOfStreamException();
        }
        read += n;
    }

    var length = BinaryPrimitives.ReadInt32LittleEndian(header);
    if (length <= 0 || length > 1024 * 1024)
    {
        throw new InvalidDataException($"Invalid message length {length}");
    }

    var payload = new byte[length];
    read = 0;
    while (read < length)
    {
        var n = await stream.ReadAsync(payload.AsMemory(read, length - read), cancellationToken);
        if (n == 0)
        {
            throw new EndOfStreamException();
        }
        read += n;
    }

    return JsonDocument.Parse(payload);
}

static async Task WriteFramedMessageAsync(Stream stream, object message, CancellationToken cancellationToken)
{
    var json = JsonSerializer.SerializeToUtf8Bytes(message, new JsonSerializerOptions
    {
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
        DefaultIgnoreCondition = System.Text.Json.Serialization.JsonIgnoreCondition.WhenWritingNull
    });

    var header = new byte[4];
    BinaryPrimitives.WriteInt32LittleEndian(header, json.Length);
    await stream.WriteAsync(header, cancellationToken);
    await stream.WriteAsync(json, cancellationToken);
    await stream.FlushAsync(cancellationToken);
}

static string ExtractCommand(JsonDocument doc)
{
    foreach (var key in new[] { "command", "type", "action" })
    {
        if (doc.RootElement.TryGetProperty(key, out var el) && el.ValueKind == JsonValueKind.String)
        {
            return el.GetString()?.Trim().ToLowerInvariant() ?? "state";
        }
    }
    return "state";
}

static string? GetOption(string[] args, string name)
{
    for (var i = 0; i < args.Length - 1; i++)
    {
        if (string.Equals(args[i], name, StringComparison.OrdinalIgnoreCase))
        {
            return args[i + 1];
        }
    }
    return null;
}

// ---------------------------------------------------------------------------
// Mutable worker status observed by the control pipe and the readiness probe.
// ---------------------------------------------------------------------------
sealed class WorkerState
{
    private readonly object _gate = new();
    private string _connectionState = "starting";
    private string _lastPoll = "never";
    private string _lastError = "none";
    private string _latest = "never";
    private int _totalAccepted;
    private int _totalShown;
    private int _lastFetched;
    private int _lastShown;
    private int _injectCount;
    private int _pollIntervalSeconds = RemoteNotificationSettings.DefaultPollIntervalSeconds;
    private DateTimeOffset _startedAt = DateTimeOffset.UtcNow;

    public int CurrentPollIntervalSeconds
    {
        get { lock (_gate) { return _pollIntervalSeconds; } }
    }

    public void ApplySettings(RemoteNotificationSettings settings)
    {
        lock (_gate)
        {
            _pollIntervalSeconds = Math.Clamp(settings.PollIntervalSeconds, 5, 3600);
        }
    }

    public void RecordPoll(string state, string error, int fetched, int accepted, int shown)
    {
        lock (_gate)
        {
            _connectionState = state;
            _lastError = error;
            _totalAccepted += accepted;
            _totalShown += shown;
            _lastFetched = fetched;
            _lastShown = shown;
            _lastPoll = DateTimeOffset.Now.ToString("yyyy/MM/dd HH:mm:ss", CultureInfo.InvariantCulture);
            if (accepted > 0)
            {
                _latest = _lastPoll;
            }
        }
    }

    public void RecordInject(string messageId)
    {
        lock (_gate)
        {
            _injectCount++;
            _totalAccepted++;
            _latest = DateTimeOffset.Now.ToString("yyyy/MM/dd HH:mm:ss", CultureInfo.InvariantCulture);
        }
    }

    public void RecordFailure(string message)
    {
        lock (_gate)
        {
            _connectionState = "error";
            _lastError = message;
            _lastPoll = DateTimeOffset.Now.ToString("yyyy/MM/dd HH:mm:ss", CultureInfo.InvariantCulture);
        }
    }

    public object ToStateObject()
    {
        lock (_gate)
        {
            return new
            {
                pid = Environment.ProcessId,
                state = "active",
                connectionState = _connectionState,
                lastPoll = _lastPoll,
                lastError = _lastError,
                latest = _latest,
                totalAccepted = _totalAccepted,
                totalShown = _totalShown,
                fetched = _lastFetched,
                shown = _lastShown,
                injectCount = _injectCount,
                pollIntervalSeconds = _pollIntervalSeconds,
                startedAt = _startedAt.ToString("O", CultureInfo.InvariantCulture)
            };
        }
    }
}
