using System.Globalization;
using System.Runtime.CompilerServices;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Threading.Channels;
using MyPowerTools.Abstractions;
using MyPowerTools.Platform.Abstractions;
using MyPowerTools.RemoteNotifications.Configuration;
using RemoteNotifications.Surface.Services;

namespace RemoteNotifications.Android;

/// <summary>
/// Remote Notifications adapter for the MyPowerTools Android host.
///
/// It is a normal MPT module - same lifecycle, commands, settings, events and tool surface contract
/// as every other module - but it owns its own signed pull loop instead of talking to the desktop
/// <c>remote-notifications.service</c> Service Unit, because Android has no service units and must
/// never depend on a desktop Python/service sidecar.
///
/// Product behaviour is shared, not re-implemented:
/// <list type="bullet">
///   <item>signed protocol: <see cref="RemoteNotificationSignedPoller"/> (parity-tested against the
///   shipped desktop poller);</item>
///   <item>inbox merge/waterline/seen-ring: <see cref="RemoteNotificationInboxSynchronizer"/>
///   (parity-tested against the shipped desktop background receiver);</item>
///   <item>message format, reference blocks and labels: the shipped
///   <c>RemoteNotificationsLegacyStore</c>/<c>RemoteNotificationSettingsStore</c> sources are
///   compiled in;</item>
///   <item>banner text: <see cref="RemoteNotificationAndroidBanner"/> (parity-tested against the
///   shipped desktop toast publisher);</item>
///   <item>history and settings live in app-private data, the signing key lives only in the
///   platform <c>secret.store</c>;</item>
///   <item>background polling is opt-in, holds a <c>background.activity</c> lease and stops - and
///   waits for the loop to end - on disable/dispose.</item>
/// </list>
/// </summary>
public sealed class RemoteNotificationsAndroidModule : IMptModule, IMptModuleLifecycle
{
    private readonly Channel<MptModuleEvent> _events =
        Channel.CreateUnbounded<MptModuleEvent>(new UnboundedChannelOptions { SingleReader = true });
    private readonly SemaphoreSlim _operations = new(1, 1);

    private ModuleContext? _context;
    private ISecretStore? _secrets;
    private INotificationService? _notifications;
    private IBackgroundActivityService? _background;
    private RemoteNotificationSettingsStore? _settingsStore;
    private RemoteNotificationsLegacyStore? _store;
    private RemoteNotificationSigningKey? _signingKey;
    private IRemoteNotificationPoller? _poller;
    private RemoteNotificationInboxSynchronizer? _synchronizer;
    private RemoteNotificationBackgroundPoller? _backgroundPoller;
    private string _dataRoot = "";
    private string _preferencesPath = "";
    private long _settingsRevision = 1;
    private long _eventSeq;
    private long _lastFetched;
    private long _lastAccepted;
    private long _lastNotified;
    private string _connectionState = "idle";
    private string _lastError = "";
    private string _lastPoll = "";
    private string _latestMessageId = "";
    private string _deliveryState = "unknown";
    private string _transportFingerprint = "";
    private bool _backgroundPollingRequested;
    private volatile bool _stopped;
    private bool _disposed;

    public string Id => RemoteNotificationsAndroidOptions.ModuleId;
    public string PackageId => RemoteNotificationsAndroidOptions.PackageId;
    public Version Version => RemoteNotificationsAndroidOptions.ModuleVersion;

    private ModuleContext Context =>
        _context ?? throw new InvalidOperationException("远程通知模块尚未初始化。");

    private RemoteNotificationSettingsStore SettingsStore =>
        _settingsStore ?? throw new InvalidOperationException("远程通知模块尚未初始化。");

    private RemoteNotificationsLegacyStore Store =>
        _store ?? throw new InvalidOperationException("远程通知模块尚未初始化。");

    private RemoteNotificationSigningKey SigningKey =>
        _signingKey ?? throw new InvalidOperationException("远程通知模块尚未初始化。");

    private RemoteNotificationInboxSynchronizer Synchronizer =>
        _synchronizer ?? throw new InvalidOperationException("远程通知模块尚未初始化。");

    /// <summary>
    /// Test seam: builds the pull client. Production always uses the signed HTTPS poller; the test
    /// suite replaces it with a scripted client so no socket is ever opened.
    /// </summary>
    internal Func<RemoteNotificationSettings, RemoteNotificationSigningKey, IRemoteNotificationPoller>? PollerFactory { get; set; }

    /// <summary>Test seam: drives the background loop's wait between two pulls.</summary>
    internal RemoteNotificationBackgroundPoller BackgroundPollerForTests => BackgroundPoller;

    private RemoteNotificationBackgroundPoller BackgroundPoller =>
        _backgroundPoller ?? throw new InvalidOperationException("远程通知模块尚未初始化。");

    // ---------------------------------------------------------------- lifecycle

    public ValueTask<InitializeResult> InitializeAsync(ModuleContext context, CancellationToken cancellationToken)
    {
        ArgumentNullException.ThrowIfNull(context);
        cancellationToken.ThrowIfCancellationRequested();
        _context = context;
        _disposed = false;
        _stopped = false;

        _dataRoot = RemoteNotificationAndroidPaths.ResolveDataRoot(context.DataDirectory);
        Directory.CreateDirectory(_dataRoot);
        Directory.CreateDirectory(context.CacheDirectory);
        Directory.CreateDirectory(context.LogDirectory);

        _secrets = context.GetCapability<ISecretStore>("secret.store");
        context.TryGetCapability<IBackgroundActivityService>("background.activity", out _background);
        context.TryGetCapability<INotificationService>("notification.desktop", out _notifications);
        _preferencesPath = RemoteNotificationAndroidPaths.AndroidPreferencesPath(_dataRoot);
        _backgroundPollingRequested = RemoteNotificationAndroidPreferences.Load(_preferencesPath).BackgroundPollingRequested;

        _settingsStore = new RemoteNotificationSettingsStore(RemoteNotificationAndroidPaths.SettingsPath(_dataRoot));
        _store = new RemoteNotificationsLegacyStore(_settingsStore, _dataRoot);
        _signingKey = new RemoteNotificationSigningKey(_secrets, Id);
        _poller = CreatePoller(_settingsStore.Load());
        _synchronizer = new RemoteNotificationInboxSynchronizer(_store, _poller);
        _backgroundPoller = new RemoteNotificationBackgroundPoller(
            _background,
            PollInBackgroundAsync,
            CurrentPollInterval,
            OnBackgroundStateChanged,
            OnBackgroundError);

        return ValueTask.FromResult(new InitializeResult(
            true,
            context.ProtocolVersion,
            ["lifecycle", "status", "commands", "settings", "logs", "dashboardCard", "detailPage"]));
    }

    /// <summary>Enabling the module never acquires background work; the user starts that explicitly.</summary>
    public ValueTask EnableAsync(ModuleContext context, CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        _stopped = false;
        return ValueTask.CompletedTask;
    }

    public ValueTask StartAsync(ModuleContext context, CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        _stopped = false;
        PublishEvent("module.running", new JsonObject
        {
            ["title"] = RemoteNotificationsAndroidOptions.DisplayName,
            ["message"] = "远程通知模块已就绪。后台接收需要手动开启。",
            ["state"] = "ready"
        });
        return ValueTask.CompletedTask;
    }

    /// <summary>Cancels the background loop and releases the foreground-activity lease.</summary>
    public async ValueTask StopAsync(ModuleContext context, CancellationToken cancellationToken)
    {
        _stopped = true;
        if (_backgroundPoller is not null)
        {
            await _backgroundPoller.StopAsync().ConfigureAwait(false);
        }
    }

    public async ValueTask DisableAsync(ModuleContext context, CancellationToken cancellationToken)
    {
        await StopAsync(context, cancellationToken).ConfigureAwait(false);
    }

    public async ValueTask DisposeAsync(CancellationToken cancellationToken)
    {
        if (_disposed)
        {
            return;
        }

        _disposed = true;
        if (_backgroundPoller is not null)
        {
            await _backgroundPoller.DisposeAsync().ConfigureAwait(false);
        }

        _events.Writer.TryComplete();
        _operations.Dispose();
    }

    private void ThrowIfDisposed()
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
    }

    // ---------------------------------------------------------------- status

    public async ValueTask<ModuleStatusSnapshot> GetStatusAsync(CancellationToken cancellationToken)
    {
        ThrowIfDisposed();
        var settings = SettingsStore.Load();
        var keyConfigured = await SigningKey.IsConfiguredAsync(cancellationToken).ConfigureAwait(false);
        var snapshot = Store.Load();
        var backgroundActive = _backgroundPoller?.IsActive == true;
        var transportHealthy = _connectionState is "ok" or "idle" or "running" or "starting";
        var deliveryAvailable = _notifications is not null;
        var checks = new[]
        {
            new HealthCheckSnapshot(
                "notification.secret",
                "Ed25519 签名密钥",
                keyConfigured,
                keyConfigured
                    ? "签名密钥已保存在系统凭据库，可用于签名握手。"
                    : "尚未导入签名密钥，请在工具页面导入与桌面端相同的 OpenSSH Ed25519 私钥。"),
            new HealthCheckSnapshot(
                "notification.transport",
                "签名拉取",
                transportHealthy,
                transportHealthy
                    ? $"上次同步状态 '{_connectionState}'，时间 {(_lastPoll.Length == 0 ? "尚未同步" : _lastPoll)}。"
                    : _lastError.Length > 0 ? _lastError : $"同步状态 '{_connectionState}'。"),
            new HealthCheckSnapshot(
                "notification.delivery",
                "系统通知",
                deliveryAvailable,
                deliveryAvailable
                    ? $"通知渠道可用，最近投递状态 '{_deliveryState}'。"
                    : "当前主机未提供 notification.desktop 能力，通知只能保存在历史中。"),
            new HealthCheckSnapshot(
                "notification.background",
                "后台接收",
                _backgroundPoller?.IsAvailable == true,
                _backgroundPoller?.IsAvailable != true
                    ? "当前主机未提供 background.activity 能力，应用退到后台后不会继续接收。"
                    : backgroundActive
                        ? "后台接收进行中，已持有前台服务任务。"
                        : _backgroundPollingRequested
                            ? "后台接收当前已停止（上次曾开启，可一键恢复）。"
                            : "后台接收未开启。"),
            new HealthCheckSnapshot(
                "notification.history",
                "通知历史",
                true,
                $"{snapshot.MessagesOldestFirst.Count} 条通知，{snapshot.KnownLabels.Count} 个标签。")
        };

        var healthy = keyConfigured && deliveryAvailable && transportHealthy;
        var state = healthy ? "running" : "degraded";
        var summary = healthy
            ? $"远程通知已就绪，共 {snapshot.MessagesOldestFirst.Count} 条历史记录" +
              (backgroundActive ? "，后台接收进行中。" : "，后台接收未开启。")
            : FirstFailure(checks);
        return new ModuleStatusSnapshot(
            Id,
            state,
            summary,
            DateTimeOffset.UtcNow,
            checks,
            (ulong)Interlocked.Read(ref _eventSeq));
    }

    private static string FirstFailure(IReadOnlyList<HealthCheckSnapshot> checks) =>
        checks.FirstOrDefault(check => !check.Ok)?.Message ?? "远程通知需要检查。";

    // ---------------------------------------------------------------- commands

    public ValueTask<IReadOnlyList<MptCommandDescriptor>> ListCommandsAsync(CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        IReadOnlyList<MptCommandDescriptor> commands =
        [
            Command(RemoteNotificationsAndroidOptions.CommandStatus, "查看通知状态", "签名、同步、后台与历史的当前状态"),
            Command(RemoteNotificationsAndroidOptions.CommandSyncNow, "立即同步", "执行一次签名拉取并显示新通知", timeoutMs: 30000),
            Command(RemoteNotificationsAndroidOptions.CommandInboxSummary, "通知历史摘要", "统计历史条数、标签与最新消息"),
            Command(RemoteNotificationsAndroidOptions.CommandPollingStart, "开启后台接收", "申请后台任务并在退到后台后继续接收"),
            Command(RemoteNotificationsAndroidOptions.CommandPollingStop, "停止后台接收", "释放后台任务并停止轮询"),
            Command(RemoteNotificationsAndroidOptions.CommandConfigure, "保存服务器设置", "保存服务器地址、频道与同步间隔"),
            Command(RemoteNotificationsAndroidOptions.CommandSigningKeyImport, "导入签名密钥", "把 OpenSSH Ed25519 私钥保存到系统凭据库"),
            Command(RemoteNotificationsAndroidOptions.CommandSigningKeyClear, "清除签名密钥", "从系统凭据库删除签名密钥"),
            Command(RemoteNotificationsAndroidOptions.CommandInboxClear, "清空通知历史", "删除本机保存的全部通知记录"),
            Command(RemoteNotificationsAndroidOptions.CommandBannerPreview, "预览通知横幅", "只生成横幅文案，不发送系统通知")
        ];
        return ValueTask.FromResult(commands);
    }

    private static MptCommandDescriptor Command(string id, string title, string subtitle, int timeoutMs = 15000) =>
        new(
            id,
            RemoteNotificationsAndroidOptions.ModuleId,
            title,
            subtitle,
            "action",
            Category: RemoteNotificationsAndroidOptions.DisplayName,
            TimeoutMs: timeoutMs,
            Execution: new JsonObject { ["type"] = "module.execute" });

    public async ValueTask<CommandExecutionResult> ExecuteCommandAsync(
        CommandRequest request,
        CancellationToken cancellationToken)
    {
        ThrowIfDisposed();
        ArgumentNullException.ThrowIfNull(request);
        try
        {
            return request.CommandId switch
            {
                RemoteNotificationsAndroidOptions.CommandStatus =>
                    Succeeded(request, (await BuildStateJsonAsync(cancellationToken).ConfigureAwait(false))
                        .ToJsonString(Indented)),
                RemoteNotificationsAndroidOptions.CommandSyncNow =>
                    Succeeded(request, (await SyncAsync(publishNotifications: true, cancellationToken).ConfigureAwait(false))
                        .ToJsonString(Indented)),
                RemoteNotificationsAndroidOptions.CommandInboxSummary => InboxSummary(request),
                RemoteNotificationsAndroidOptions.CommandInboxClear => await ClearInboxAsync(request, cancellationToken)
                    .ConfigureAwait(false),
                RemoteNotificationsAndroidOptions.CommandPollingStart => await StartPollingAsync(request, cancellationToken)
                    .ConfigureAwait(false),
                RemoteNotificationsAndroidOptions.CommandPollingStop => await StopPollingAsync(request, cancellationToken)
                    .ConfigureAwait(false),
                RemoteNotificationsAndroidOptions.CommandConfigure => await ConfigureAsync(request, cancellationToken)
                    .ConfigureAwait(false),
                RemoteNotificationsAndroidOptions.CommandSigningKeyImport => await ImportKeyAsync(request, cancellationToken)
                    .ConfigureAwait(false),
                RemoteNotificationsAndroidOptions.CommandSigningKeyClear => await ClearKeyAsync(request, cancellationToken)
                    .ConfigureAwait(false),
                RemoteNotificationsAndroidOptions.CommandBannerPreview => PreviewBanner(request),
                _ => Failed(
                    request,
                    RemoteNotificationErrorCodes.NotFound,
                    $"远程通知模块未实现命令 '{request.CommandId}'。")
            };
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
            throw;
        }
        catch (Exception exception)
        {
            return Failed(request, ClassifyError(exception), exception.Message);
        }
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
            result.Success ? result.Output : result.Error?.Message ?? "命令执行失败。",
            1,
            true,
            result);
    }

    // ---------------------------------------------------------------- events

    public async IAsyncEnumerable<MptModuleEvent> SubscribeEventsAsync(
        EventCursor cursor,
        [EnumeratorCancellation] CancellationToken cancellationToken)
    {
        ThrowIfDisposed();
        await foreach (var item in _events.Reader.ReadAllAsync(cancellationToken).ConfigureAwait(false))
        {
            if (item.Seq > cursor.LastEventSeq)
            {
                yield return item;
            }
        }
    }

    private void PublishEvent(string type, JsonObject payload)
    {
        var sequence = (ulong)Interlocked.Increment(ref _eventSeq);
        _events.Writer.TryWrite(new MptModuleEvent(Id, sequence, type, DateTimeOffset.UtcNow, payload));
    }

    // ---------------------------------------------------------------- settings

    /// <summary>
    /// Only non-secret fields are exposed. The signing key is deliberately absent so a settings
    /// patch can never carry private key material into the host's settings store; it is imported
    /// through <c>signing-key.import</c> and written straight to the platform credential store.
    /// </summary>
    private const string SchemaJson = """
    {
      "type": "object",
      "properties": {
        "protocol": { "type": "string", "title": "协议", "enum": ["https", "http"], "default": "https" },
        "host": { "type": "string", "title": "服务器地址", "default": "message.lixinrui000.cn" },
        "port": { "type": "integer", "title": "端口", "minimum": 1, "maximum": 65535, "default": 8888 },
        "channel": { "type": "string", "title": "频道", "default": "default" },
        "pollIntervalSeconds": { "type": "integer", "title": "同步间隔（秒）", "minimum": 5, "maximum": 3600, "default": 5 },
        "signingKeyConfigured": {
          "type": "boolean",
          "title": "签名密钥已导入",
          "readOnly": true,
          "description": "私钥只保存在系统凭据库；请在“远程通知”工具页面导入或清除。"
        }
      }
    }
    """;

    private static readonly string[] EditableSettingKeys =
        ["protocol", "host", "port", "channel", "pollIntervalSeconds"];

    public ValueTask<SettingsSchemaDocument> GetSettingsSchemaAsync(CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        return ValueTask.FromResult(new SettingsSchemaDocument(Id, SchemaJson));
    }

    public async ValueTask<SettingsSnapshotDocument> GetSettingsAsync(CancellationToken cancellationToken)
    {
        ThrowIfDisposed();
        var settings = SettingsStore.Load();
        var values = new JsonObject
        {
            ["protocol"] = settings.Protocol,
            ["host"] = settings.Host,
            ["port"] = settings.Port,
            ["channel"] = settings.Channel,
            ["pollIntervalSeconds"] = settings.PollIntervalSeconds,
            ["signingKeyConfigured"] = await SigningKey.IsConfiguredAsync(cancellationToken).ConfigureAwait(false)
        };
        return new SettingsSnapshotDocument(
            Id,
            (ulong)Interlocked.Read(ref _settingsRevision),
            values,
            DateTimeOffset.UtcNow);
    }

    public ValueTask<SettingsValidationResult> ValidateSettingsAsync(
        SettingsPatch patch,
        CancellationToken cancellationToken)
    {
        ThrowIfDisposed();
        cancellationToken.ThrowIfCancellationRequested();
        try
        {
            var candidate = MergeSettings(SettingsStore.Load(), patch.Patch);
            var validation = candidate.Validate();
            return ValueTask.FromResult(validation.IsValid
                ? new SettingsValidationResult(true, [])
                : new SettingsValidationResult(
                    false,
                    [validation.Error],
                    new MptRuntimeError(RemoteNotificationErrorCodes.ValidationFailed, validation.Error)));
        }
        catch (Exception exception) when (exception is ArgumentException or InvalidOperationException)
        {
            return ValueTask.FromResult(new SettingsValidationResult(
                false,
                [exception.Message],
                new MptRuntimeError(RemoteNotificationErrorCodes.ValidationFailed, exception.Message)));
        }
    }

    public async ValueTask<SettingsSnapshotDocument> ApplySettingsAsync(
        SettingsSnapshotDocument snapshot,
        CancellationToken cancellationToken)
    {
        ThrowIfDisposed();
        ArgumentNullException.ThrowIfNull(snapshot);
        await _operations.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            var candidate = MergeSettings(SettingsStore.Load(), snapshot.Values);
            var validation = candidate.Validate();
            if (!validation.IsValid || validation.Settings is null)
            {
                throw new ArgumentException(validation.Error);
            }

            SettingsStore.Save(validation.Settings);
            ReplacePoller(validation.Settings);
            Interlocked.Increment(ref _settingsRevision);
        }
        finally
        {
            _operations.Release();
        }

        return await GetSettingsAsync(cancellationToken).ConfigureAwait(false);
    }

    /// <summary>
    /// Projects the whitelisted, non-secret settings onto the shared settings record. Unknown keys
    /// are rejected instead of silently dropped so a secret or a typo cannot ride along.
    /// </summary>
    private static RemoteNotificationSettings MergeSettings(RemoteNotificationSettings current, JsonObject values)
    {
        foreach (var key in values.Select(pair => pair.Key))
        {
            if (!EditableSettingKeys.Contains(key, StringComparer.Ordinal))
            {
                throw new ArgumentException($"不支持通过设置接口修改“{key}”，签名密钥请使用导入命令。");
            }
        }

        var protocol = SettingsJson.ReadString(values, "protocol") ?? current.Protocol;
        var host = SettingsJson.ReadString(values, "host") ?? current.Host;
        var channel = SettingsJson.ReadString(values, "channel") ?? current.Channel;
        var port = values.ContainsKey("port")
            ? SettingsJson.ReadInt(values, "port") ?? int.MinValue
            : current.Port;
        var interval = values.ContainsKey("pollIntervalSeconds")
            ? SettingsJson.ReadInt(values, "pollIntervalSeconds") ?? int.MinValue
            : current.PollIntervalSeconds;
        return (current with
        {
            Protocol = protocol,
            Host = host,
            Channel = channel,
            Port = port,
            PollIntervalSeconds = interval
        }).Normalize();
    }

    private void ReplacePoller(RemoteNotificationSettings settings)
    {
        _poller = CreatePoller(settings);
        _synchronizer = new RemoteNotificationInboxSynchronizer(Store, _poller);
    }

    private IRemoteNotificationPoller CreatePoller(RemoteNotificationSettings settings) =>
        PollerFactory is { } factory
            ? factory(settings, SigningKey)
            : new RemoteNotificationSignedPoller(settings, SigningKey);

    // ---------------------------------------------------------------- command bodies

    private async Task<JsonObject> BuildStateJsonAsync(CancellationToken cancellationToken)
    {
        var settings = SettingsStore.Load();
        var snapshot = Store.Load();
        return new JsonObject
        {
            ["moduleId"] = Id,
            ["endpoint"] = settings.Endpoint,
            ["channel"] = settings.Channel,
            ["pollIntervalSeconds"] = settings.PollIntervalSeconds,
            ["connectionState"] = _connectionState,
            ["lastPoll"] = _lastPoll,
            ["lastError"] = _lastError,
            ["latestMessageId"] = _latestMessageId,
            ["fetched"] = Interlocked.Read(ref _lastFetched),
            ["accepted"] = Interlocked.Read(ref _lastAccepted),
            ["notified"] = Interlocked.Read(ref _lastNotified),
            ["deliveryState"] = _deliveryState,
            ["messageCount"] = snapshot.MessagesOldestFirst.Count,
            ["labelCount"] = snapshot.KnownLabels.Count,
            ["keyConfigured"] = await SigningKey.IsConfiguredAsync(cancellationToken).ConfigureAwait(false),
            ["backgroundAvailable"] = _backgroundPoller?.IsAvailable == true,
            ["backgroundActive"] = _backgroundPoller?.IsActive == true,
            ["backgroundPollingRequested"] = _backgroundPollingRequested,
            ["notificationAvailable"] = _notifications is not null,
            ["dataDirectory"] = _dataRoot
        };
    }

    private async Task<JsonObject> SyncAsync(bool publishNotifications, CancellationToken cancellationToken)
    {
        await _operations.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            var outcome = await Synchronizer.PollAsync(cancellationToken).ConfigureAwait(false);
            var pull = outcome.Pull;
            Interlocked.Exchange(ref _lastFetched, pull.Notifications.Count);
            Interlocked.Exchange(ref _lastAccepted, outcome.Accepted.Count);
            _lastPoll = DateTimeOffset.Now.ToString("yyyy/MM/dd HH:mm:ss", CultureInfo.InvariantCulture);
            _connectionState = pull.State;
            _lastError = pull.Error;

            var notified = 0;
            foreach (var record in outcome.Accepted)
            {
                var messageId = RemoteNotificationsLegacyStore.StableId(record);
                _latestMessageId = messageId;
                var banner = RemoteNotificationAndroidBanner.Build(record, messageId);
                if (banner.Suppressed)
                {
                    _deliveryState = banner.State;
                }
                else if (publishNotifications && _notifications is not null)
                {
                    try
                    {
                        notified += await banner.PublishAsync(_notifications, cancellationToken).ConfigureAwait(false) ? 1 : 0;
                        _deliveryState = "shown";
                    }
                    catch (Exception exception) when (exception is not OperationCanceledException)
                    {
                        // Delivery problems (permission revoked, channel disabled) must not lose the
                        // message: it is already persisted, only the tray banner failed.
                        _deliveryState = "error";
                        _lastError = exception.Message;
                    }
                }

                PublishEvent("message.received", new JsonObject
                {
                    ["title"] = banner.Suppressed
                        ? RemoteNotificationsLegacyStore.ExtractLabel(record.Message)
                        : banner.Title,
                    ["message"] = record.Message,
                    ["messageId"] = messageId,
                    ["channel"] = record.Channel,
                    ["sessionId"] = record.SessionId,
                    ["sourceClient"] = record.SourceClient,
                    ["suppressed"] = banner.Suppressed,
                    ["bannerState"] = banner.State
                });
            }

            Interlocked.Exchange(ref _lastNotified, notified);
            PublishTransportEvent();
            return await BuildStateJsonAsync(cancellationToken).ConfigureAwait(false);
        }
        finally
        {
            _operations.Release();
        }
    }

    /// <summary>Reports transport transitions once per state change instead of once per poll.</summary>
    private void PublishTransportEvent()
    {
        var fingerprint = $"{_connectionState}|{_lastError}";
        if (string.Equals(fingerprint, _transportFingerprint, StringComparison.Ordinal))
        {
            return;
        }

        _transportFingerprint = fingerprint;
        if (_connectionState is "ok" or "idle" or "running" or "starting")
        {
            if (_connectionState != "running")
            {
                PublishEvent("server.connected", new JsonObject
                {
                    ["title"] = "远程通知同步正常",
                    ["message"] = $"同步状态 '{_connectionState}'。",
                    ["state"] = _connectionState
                });
            }

            return;
        }

        PublishEvent("server.disconnected", new JsonObject
        {
            ["title"] = "远程通知同步异常",
            ["message"] = _lastError.Length > 0 ? _lastError : $"同步状态 '{_connectionState}'。",
            ["state"] = _connectionState
        });
    }

    private async Task<CommandExecutionResult> ClearInboxAsync(
        CommandRequest request,
        CancellationToken cancellationToken)
    {
        await _operations.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            Store.ClearMessages();
            Interlocked.Exchange(ref _lastAccepted, 0);
        }
        finally
        {
            _operations.Release();
        }

        PublishEvent("inbox.cleared", new JsonObject
        {
            ["title"] = "通知历史已清空",
            ["message"] = "本机保存的通知记录已删除。"
        });
        return Succeeded(request, (await BuildStateJsonAsync(cancellationToken).ConfigureAwait(false))
            .ToJsonString(Indented));
    }

    private CommandExecutionResult InboxSummary(CommandRequest request)
    {
        var snapshot = Store.Load();
        var latest = snapshot.MessagesOldestFirst.LastOrDefault();
        var payload = new JsonObject
        {
            ["messageCount"] = snapshot.MessagesOldestFirst.Count,
            ["labelCount"] = snapshot.KnownLabels.Count,
            ["filterLabel"] = snapshot.FilterLabel ?? "",
            ["latestMessageId"] = latest is null ? "" : RemoteNotificationsLegacyStore.StableId(latest),
            ["latestLabel"] = latest is null
                ? ""
                : RemoteNotificationsLegacyStore.ExtractLabel(latest.Message),
            ["latestTimestamp"] = latest?.ServerTimestamp is { Length: > 0 } serverTimestamp
                ? serverTimestamp
                : latest?.Timestamp ?? ""
        };
        return Succeeded(request, payload.ToJsonString(Indented));
    }

    private async Task<CommandExecutionResult> StartPollingAsync(
        CommandRequest request,
        CancellationToken cancellationToken)
    {
        await _operations.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            var settings = SettingsStore.Load();
            var validation = settings.Validate();
            if (!validation.IsValid)
            {
                return Failed(request, RemoteNotificationErrorCodes.ValidationFailed, validation.Error);
            }

            if (!await SigningKey.IsConfiguredAsync(cancellationToken).ConfigureAwait(false))
            {
                return Failed(
                    request,
                    RemoteNotificationErrorCodes.PermissionRequired,
                    "请先导入签名密钥，再开启后台接收。");
            }

            var started = await BackgroundPoller.StartAsync(cancellationToken).ConfigureAwait(false);
            _backgroundPollingRequested = true;
            SavePreferences();
            var state = await BuildStateJsonAsync(cancellationToken).ConfigureAwait(false);
            state["started"] = started;
            return Succeeded(request, state.ToJsonString(Indented));
        }
        finally
        {
            _operations.Release();
        }
    }

    private async Task<CommandExecutionResult> StopPollingAsync(
        CommandRequest request,
        CancellationToken cancellationToken)
    {
        await _operations.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            var stopped = await BackgroundPoller.StopAsync().ConfigureAwait(false);
            _backgroundPollingRequested = false;
            SavePreferences();
            var state = await BuildStateJsonAsync(cancellationToken).ConfigureAwait(false);
            state["stopped"] = stopped;
            return Succeeded(request, state.ToJsonString(Indented));
        }
        finally
        {
            _operations.Release();
        }
    }

    private async Task<CommandExecutionResult> ConfigureAsync(
        CommandRequest request,
        CancellationToken cancellationToken)
    {
        await _operations.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            var candidate = MergeSettings(SettingsStore.Load(), request.Args ?? new JsonObject());
            var validation = candidate.Validate();
            if (!validation.IsValid || validation.Settings is null)
            {
                return Failed(request, RemoteNotificationErrorCodes.ValidationFailed, validation.Error);
            }

            SettingsStore.Save(validation.Settings);
            ReplacePoller(validation.Settings);
            Interlocked.Increment(ref _settingsRevision);
            return Succeeded(request, (await BuildStateJsonAsync(cancellationToken).ConfigureAwait(false))
                .ToJsonString(Indented));
        }
        finally
        {
            _operations.Release();
        }
    }

    private async Task<CommandExecutionResult> ImportKeyAsync(
        CommandRequest request,
        CancellationToken cancellationToken)
    {
        var material = SettingsJson.ReadString(request.Args ?? new JsonObject(), RemoteNotificationsAndroidOptions.SigningKeyArgument);
        if (string.IsNullOrWhiteSpace(material))
        {
            return Failed(
                request,
                RemoteNotificationErrorCodes.ValidationFailed,
                "请提供 OpenSSH Ed25519 私钥内容。");
        }

        await _operations.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            await SigningKey.SaveAsync(material, cancellationToken).ConfigureAwait(false);
        }
        catch (InvalidDataException exception)
        {
            return Failed(request, RemoteNotificationErrorCodes.ValidationFailed, exception.Message);
        }
        finally
        {
            _operations.Release();
        }

        PublishEvent("signing-key.updated", new JsonObject
        {
            ["title"] = "签名密钥已更新",
            ["message"] = "新的 Ed25519 私钥已保存到系统凭据库。"
        });

        // The response never echoes key material; only the resulting state is reported.
        var payload = new JsonObject
        {
            ["keyConfigured"] = true,
            ["algorithm"] = "ed25519",
            ["storage"] = "secret.store"
        };
        return Succeeded(request, payload.ToJsonString(Indented));
    }

    private async Task<CommandExecutionResult> ClearKeyAsync(
        CommandRequest request,
        CancellationToken cancellationToken)
    {
        await _operations.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            await SigningKey.ClearAsync(cancellationToken).ConfigureAwait(false);
            await BackgroundPoller.StopAsync().ConfigureAwait(false);
        }
        finally
        {
            _operations.Release();
        }

        PublishEvent("signing-key.cleared", new JsonObject
        {
            ["title"] = "签名密钥已清除",
            ["message"] = "系统凭据库中的签名密钥已删除，后台接收已停止。"
        });
        return Succeeded(request, new JsonObject
        {
            ["keyConfigured"] = false
        }.ToJsonString(Indented));
    }

    private CommandExecutionResult PreviewBanner(CommandRequest request)
    {
        var args = request.Args ?? new JsonObject();
        var message = SettingsJson.ReadString(args, "message");
        var record = message is null
            ? Store.Load().MessagesOldestFirst.LastOrDefault() ?? new RemoteNotificationRecord(
                "preview",
                SettingsStore.Load().Channel,
                "[预览] 这是一条本地预览通知，不会发送到服务器，也不会打扰你。",
                "info",
                DateTimeOffset.Now.ToString("O", CultureInfo.InvariantCulture))
            : new RemoteNotificationRecord(
                SettingsJson.ReadString(args, "id") ?? "preview",
                SettingsJson.ReadString(args, "channel") ?? SettingsStore.Load().Channel,
                message,
                SettingsJson.ReadString(args, "icon") ?? "info",
                SettingsJson.ReadString(args, "timestamp") ?? DateTimeOffset.Now.ToString("O", CultureInfo.InvariantCulture),
                SessionId: SettingsJson.ReadString(args, "sessionId") ?? "",
                SessionName: SettingsJson.ReadString(args, "sessionName") ?? "",
                SourceClient: SettingsJson.ReadString(args, "sourceClient") ?? "");
        var banner = RemoteNotificationAndroidBanner.Build(record, RemoteNotificationsLegacyStore.StableId(record));
        return Succeeded(request, new JsonObject
        {
            ["messageId"] = banner.MessageId,
            ["title"] = banner.Title,
            ["body"] = banner.Body,
            ["launchUri"] = banner.LaunchUri,
            ["state"] = banner.State,
            ["suppressed"] = banner.Suppressed,
            ["published"] = false
        }.ToJsonString(Indented));
    }

    // ---------------------------------------------------------------- background loop

    private TimeSpan CurrentPollInterval()
    {
        var seconds = SettingsStore.Load().PollIntervalSeconds;
        return TimeSpan.FromSeconds(Math.Clamp(seconds, 5, 3600));
    }

    private async Task PollInBackgroundAsync(CancellationToken cancellationToken)
    {
        if (_stopped)
        {
            return;
        }

        await SyncAsync(publishNotifications: true, cancellationToken).ConfigureAwait(false);
    }

    private void OnBackgroundStateChanged(bool active)
    {
        PublishEvent(active ? "polling.started" : "polling.stopped", new JsonObject
        {
            ["title"] = active ? "后台接收已开启" : "后台接收已停止",
            ["message"] = active
                ? "应用退到后台后仍会继续接收远程通知。"
                : "已释放后台任务，不再进行后台轮询。",
            ["active"] = active
        });
    }

    private void OnBackgroundError(string message)
    {
        _connectionState = "error";
        _lastError = message;
        PublishTransportEvent();
    }

    private void SavePreferences() =>
        RemoteNotificationAndroidPreferences.Save(
            _preferencesPath,
            RemoteNotificationAndroidPreferences.WithBackgroundPolling(_backgroundPollingRequested));

    // ---------------------------------------------------------------- surfaces

    public ValueTask<IReadOnlyList<UiSurfaceDescriptor>> ListSurfacesAsync(CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        IReadOnlyList<UiSurfaceDescriptor> surfaces =
        [
            new(
                $"{RemoteNotificationsAndroidOptions.ModuleId}.detail",
                "detail-page",
                RemoteNotificationsAndroidOptions.DisplayName,
                new JsonObject
                {
                    ["surfaceAssembly"] = RemoteNotificationsAndroidOptions.SurfaceAssemblyFileName,
                    ["surfaceType"] = RemoteNotificationsAndroidOptions.SurfaceTypeName
                })
        ];
        return ValueTask.FromResult(surfaces);
    }

    // ---------------------------------------------------------------- helpers

    private static readonly JsonSerializerOptions Indented = new() { WriteIndented = true };

    private static string ClassifyError(Exception exception) => exception switch
    {
        UnauthorizedAccessException => RemoteNotificationErrorCodes.PermissionRequired,
        InvalidDataException or ArgumentException => RemoteNotificationErrorCodes.ValidationFailed,
        NotSupportedException => RemoteNotificationErrorCodes.RuntimeUnavailable,
        _ => RemoteNotificationErrorCodes.RuntimeUnavailable
    };

    private static CommandExecutionResult Succeeded(CommandRequest request, string output) =>
        new(request.InvocationId, request.CommandId, "succeeded", true, output);

    private static CommandExecutionResult Failed(
        CommandRequest request,
        string code,
        string message,
        bool retryable = false) =>
        new(
            request.InvocationId,
            request.CommandId,
            "failed",
            false,
            "",
            new MptRuntimeError(code, message, retryable));
}

/// <summary>
/// Standard MyPowerTools runtime error codes, duplicated as literals so the Android module payload
/// does not drag the gRPC/protobuf contract package onto the phone. A test asserts these values
/// still equal <c>MyPowerTools.Protocol.MptErrorCodes</c>.
/// </summary>
internal static class RemoteNotificationErrorCodes
{
    public const string NotFound = "MPT_NOT_FOUND";
    public const string RuntimeUnavailable = "MPT_RUNTIME_UNAVAILABLE";
    public const string ValidationFailed = "MPT_VALIDATION_FAILED";
    public const string PermissionRequired = "MPT_PERMISSION_REQUIRED";
    public const string CapabilityMissing = "MPT_CAPABILITY_MISSING";
}
