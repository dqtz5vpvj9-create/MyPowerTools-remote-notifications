using System.Runtime.CompilerServices;
using System.Text.Json;
using System.Text.Json.Nodes;
using MyPowerTools.Abstractions;
using MyPowerTools.Protocol;
using MyPowerTools.RemoteNotifications.Configuration;
using RemoteNotifications.Surface.Services;

namespace AndroidTools.MyPowerTools;

/// <summary>
/// Runtime adapter that observes the independently supervised notification service.
/// The service owns the only remote polling loop; this module exposes status, commands,
/// settings, and persisted-inbox events without issuing competing network pulls.
/// </summary>
public sealed partial class RemoteNotificationsServiceObserverModule : IMptModule
{
    private ModuleContext? _context;
    private RemoteNotificationSettingsStore? _settingsStore;
    private RemoteNotificationsLegacyStore? _store;
    private long _settingsRevision = 1;
    private bool _disposed;

    public string Id => "android-tools.notifications";
    public string PackageId => "android-tools-suite";
    public Version Version => new(0, 2, 0);

    private ModuleContext Context =>
        _context ?? throw new InvalidOperationException("Remote Notifications was not initialized.");

    private RemoteNotificationSettingsStore SettingsStore =>
        _settingsStore ?? throw new InvalidOperationException("Remote Notifications was not initialized.");

    private RemoteNotificationsLegacyStore Store =>
        _store ?? throw new InvalidOperationException("Remote Notifications was not initialized.");

    public ValueTask<InitializeResult> InitializeAsync(
        ModuleContext context,
        CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        _context = context;
        _disposed = false;
        Directory.CreateDirectory(context.DataDirectory);
        Directory.CreateDirectory(context.CacheDirectory);
        Directory.CreateDirectory(context.LogDirectory);
        _settingsStore = new RemoteNotificationSettingsStore(
            Path.Combine(context.DataDirectory, "settings.json"));
        _store = new RemoteNotificationsLegacyStore(_settingsStore, context.DataDirectory);
        return ValueTask.FromResult(new InitializeResult(
            true,
            context.ProtocolVersion,
            ["status", "commands", "settings", "logs", "dashboardCard", "detailPage"]));
    }

    public async ValueTask<ModuleStatusSnapshot> GetStatusAsync(
        CancellationToken cancellationToken)
    {
        ThrowIfDisposed();
        var settings = SettingsStore.Load();
        var service = await RemoteNotificationServiceControl.TrySendAsync(
            "state",
            null,
            cancellationToken).ConfigureAwait(false);
        var snapshot = LoadSnapshot();
        var keyAvailable = File.Exists(settings.ExpandedPrivateKeyPath);
        var connectionState = ReadString(service.Data, "connectionState", "unavailable");
        var notificationState = ReadString(service.Data, "notificationState", "unknown");
        var authorization = ReadString(service.Data, "notificationAuthorization", "unknown");
        var serviceHealthy = service.Connected &&
            connectionState is "ok" or "idle" or "running";
        var notificationHealthy =
            notificationState is not ("error" or "permission-denied" or "delivery-failed");

        var checks = new[]
        {
            new HealthCheckSnapshot(
                "notification.service",
                "Background notification service",
                service.Connected,
                service.Connected
                    ? $"Service Unit 'remote-notifications.service' answered on {service.Endpoint}."
                    : service.Error),
            new HealthCheckSnapshot(
                "notification.poll",
                "Signed remote pull",
                serviceHealthy,
                service.Connected
                    ? $"Connection state is '{connectionState}'; last poll {ReadString(service.Data, "lastPoll", "never")} ."
                    : "The signed-pull state is unavailable until the service is ready."),
            new HealthCheckSnapshot(
                "notification.secret",
                "SSH request signing",
                keyAvailable,
                keyAvailable
                    ? $"The configured Ed25519 key exists at {settings.PrivateKeyPath}."
                    : $"The configured signing key was not found at {settings.PrivateKeyPath}."),
            new HealthCheckSnapshot(
                "notification.delivery",
                "Desktop notification delivery",
                notificationHealthy,
                $"State '{notificationState}', authorization '{authorization}'. " +
                ReadString(service.Data, "notificationError", "")),
            new HealthCheckSnapshot(
                "notification.history",
                "Persisted notification inbox",
                snapshot.Error.Length == 0,
                snapshot.Error.Length == 0
                    ? $"{snapshot.Value.MessagesOldestFirst.Count} message(s) are persisted."
                    : snapshot.Error)
        };

        var healthy = serviceHealthy && keyAvailable &&
            notificationHealthy && snapshot.Error.Length == 0;
        return new ModuleStatusSnapshot(
            Id,
            healthy ? "running" : "degraded",
            healthy
                ? $"Remote Notifications is synchronized through the supervised service; " +
                  $"{snapshot.Value.MessagesOldestFirst.Count} message(s) are stored locally."
                : FirstFailure(checks),
            DateTimeOffset.UtcNow,
            checks,
            0);
    }

    public ValueTask<IReadOnlyList<MptCommandDescriptor>> ListCommandsAsync(
        CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        IReadOnlyList<MptCommandDescriptor> commands =
        [
            Command(
                "android-tools.notifications.server.check",
                "Check notification service",
                "Read service, signing, delivery, and inbox state"),
            Command(
                "android-tools.notifications.sync-now",
                "Synchronize notifications now",
                "Perform one signed pull through the supervised service",
                20000),
            Command(
                "android-tools.notifications.inbox.summary",
                "Summarize notification inbox",
                "Show persisted message and topic counts"),
            Command(
                "android-tools.notifications.test-event",
                "Create test notification",
                "Persist and deliver a test message through the supervised service")
        ];
        return ValueTask.FromResult(commands);
    }

    public async ValueTask<CommandExecutionResult> ExecuteCommandAsync(
        CommandRequest request,
        CancellationToken cancellationToken)
    {
        ThrowIfDisposed();
        return request.CommandId switch
        {
            "android-tools.notifications.server.check" =>
                await ExecuteServiceCommandAsync(request, "state", null, cancellationToken)
                    .ConfigureAwait(false),
            "android-tools.notifications.sync-now" =>
                await ExecuteServiceCommandAsync(request, "poll", null, cancellationToken)
                    .ConfigureAwait(false),
            "android-tools.notifications.test-event" =>
                await ExecuteServiceCommandAsync(
                    request,
                    "inject",
                    new JsonObject
                    {
                        ["title"] = ReadString(
                            request.Args, "title", "Remote Notifications test"),
                        ["message"] = ReadString(
                            request.Args,
                            "message",
                            "A production-path test notification was delivered.")
                    },
                    cancellationToken).ConfigureAwait(false),
            "android-tools.notifications.inbox.summary" => InboxSummary(request),
            _ => Failed(
                request,
                MptErrorCodes.NotFound,
                $"Command '{request.CommandId}' is not implemented by Remote Notifications.")
        };
    }

    public async IAsyncEnumerable<CommandExecutionEvent> ExecuteCommandStreamAsync(
        CommandRequest request,
        [EnumeratorCancellation] CancellationToken cancellationToken)
    {
        var result = await ExecuteCommandAsync(request, cancellationToken).ConfigureAwait(false);
        yield return new CommandExecutionEvent(
            result.InvocationId,
            result.CommandId,
            result.State,
            result.Success ? result.Output : result.Error?.Message ?? "Command failed.",
            1,
            true,
            result);
    }

    public async IAsyncEnumerable<MptModuleEvent> SubscribeEventsAsync(
        EventCursor cursor,
        [EnumeratorCancellation] CancellationToken cancellationToken)
    {
        ThrowIfDisposed();
        var sequence = Math.Max(1UL, cursor.LastEventSeq);
        var observedIds = LoadSnapshot().Value.MessagesOldestFirst
            .Select(RemoteNotificationsLegacyStore.StableId)
            .ToHashSet(StringComparer.Ordinal);
        var serviceFingerprint = "";

        if (cursor.LastEventSeq < 1)
        {
            var status = await GetStatusAsync(cancellationToken).ConfigureAwait(false);
            yield return new MptModuleEvent(
                Id,
                1,
                status.State == "running" ? "module.running" : "module.degraded",
                DateTimeOffset.UtcNow,
                new JsonObject
                {
                    ["title"] = "Remote Notifications",
                    ["message"] = status.Summary,
                    ["state"] = status.State
                });
        }

        while (!cancellationToken.IsCancellationRequested)
        {
            await Task.Delay(TimeSpan.FromSeconds(2), cancellationToken).ConfigureAwait(false);
            var current = LoadSnapshot();
            if (current.Error.Length == 0)
            {
                foreach (var notification in current.Value.MessagesOldestFirst)
                {
                    var id = RemoteNotificationsLegacyStore.StableId(notification);
                    if (!observedIds.Add(id) ||
                        RemoteNotificationsLegacyStore.IsSystemHealthRecord(notification))
                    {
                        continue;
                    }

                    sequence++;
                    yield return new MptModuleEvent(
                        Id,
                        sequence,
                        "message.received",
                        DateTimeOffset.UtcNow,
                        new JsonObject
                        {
                            ["title"] = RemoteNotificationsLegacyStore.ExtractLabel(notification.Message),
                            ["message"] = notification.Message,
                            ["messageId"] = id,
                            ["channel"] = notification.Channel,
                            ["sessionId"] = notification.SessionId,
                            ["sourceClient"] = notification.SourceClient
                        });
                }
            }

            var service = await RemoteNotificationServiceControl.TrySendAsync(
                "state", null, cancellationToken).ConfigureAwait(false);
            var state = ReadString(service.Data, "connectionState", "unavailable");
            var error = service.Connected
                ? ReadString(service.Data, "lastError", "")
                : service.Error;
            var fingerprint = $"{service.Connected}|{state}|{error}";
            if (fingerprint == serviceFingerprint)
            {
                continue;
            }

            serviceFingerprint = fingerprint;
            if (service.Connected &&
                state is "ok" or "idle" or "running" or "starting")
            {
                continue;
            }

            sequence++;
            yield return new MptModuleEvent(
                Id,
                sequence,
                "server.disconnected",
                DateTimeOffset.UtcNow,
                new JsonObject
                {
                    ["title"] = "Remote notification synchronization",
                    ["message"] = error.Length == 0
                        ? $"The service reported state '{state}'."
                        : error,
                    ["state"] = state
                });
        }
    }

    public ValueTask DisposeAsync(CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        _disposed = true;
        return ValueTask.CompletedTask;
    }

    private async ValueTask<CommandExecutionResult> ExecuteServiceCommandAsync(
        CommandRequest request,
        string command,
        JsonObject? arguments,
        CancellationToken cancellationToken)
    {
        var response = await RemoteNotificationServiceControl.TrySendAsync(
            command, arguments, cancellationToken).ConfigureAwait(false);
        if (!response.Connected || response.Data is null)
        {
            return Failed(
                request,
                MptErrorCodes.RuntimeUnavailable,
                response.Error.Length > 0
                    ? response.Error
                    : "The Remote Notifications Service did not return a response.",
                true);
        }

        return Succeeded(request, response.Data.ToJsonString(
            new JsonSerializerOptions { WriteIndented = true }));
    }

    private CommandExecutionResult InboxSummary(CommandRequest request)
    {
        var snapshot = LoadSnapshot();
        if (snapshot.Error.Length > 0)
        {
            return Failed(
                request,
                MptErrorCodes.RuntimeUnavailable,
                snapshot.Error,
                true);
        }

        var latest = snapshot.Value.MessagesOldestFirst.LastOrDefault();
        var payload = new JsonObject
        {
            ["messageCount"] = snapshot.Value.MessagesOldestFirst.Count,
            ["topicCount"] = snapshot.Value.KnownLabels.Count,
            ["selectedTopic"] = snapshot.Value.FilterLabel ?? "",
            ["latestMessageId"] = latest is null
                ? ""
                : RemoteNotificationsLegacyStore.StableId(latest),
            ["dataDirectory"] = Context.DataDirectory
        };
        return Succeeded(request, payload.ToJsonString(
            new JsonSerializerOptions { WriteIndented = true }));
    }
}
