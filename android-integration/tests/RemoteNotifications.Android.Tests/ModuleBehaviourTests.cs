using System.Globalization;
using MyPowerTools.Abstractions;
using MyPowerTools.Platform.Abstractions;
using RemoteNotifications.Surface.Services;

namespace RemoteNotifications.Android.Tests;

/// <summary>
/// Module-level behaviour: MPT command/settings/event contract, opt-in background polling with a
/// platform lease, lifecycle cancellation and notification delivery through the already-implemented
/// <c>notification.desktop</c> capability.
/// </summary>
public sealed class ModuleBehaviourTests
{
    private static RemoteNotificationPullResult Result(params RemoteNotificationRecord[] records) =>
        records.Length == 0
            ? new RemoteNotificationPullResult("idle", [], "")
            : new RemoteNotificationPullResult("ok", records, "");

    private static RemoteNotificationRecord Record(
        string id,
        string message,
        string icon = "codex",
        string sessionId = "",
        string sourceClient = "codex") =>
        new(
            id,
            "default",
            message,
            icon,
            DateTimeOffset.UtcNow.ToString("O", CultureInfo.InvariantCulture),
            DateTimeOffset.UtcNow.ToString("O", CultureInfo.InvariantCulture),
            sessionId,
            "",
            sourceClient,
            "",
            "",
            "");

    [Fact]
    public async Task Module_start_never_acquires_background_work()
    {
        await using var harness = new ModuleHarness();
        await harness.InitializeAsync();

        Assert.Equal(0, harness.Background.AcquireCount);
        var status = await harness.Module.GetStatusAsync(CancellationToken.None);
        Assert.Contains(
            status.Checks,
            check => check.Id == "notification.background" && check.Message.Contains("后台接收未开启", StringComparison.Ordinal));
        Assert.Equal("degraded", status.State);
        var state = await harness.ExecuteAsync(RemoteNotificationsAndroidOptions.CommandStatus);
        Assert.Contains("\"backgroundActive\": false", state.Output, StringComparison.Ordinal);
    }

    [Fact]
    public async Task Polling_start_acquires_the_lease_polls_and_stop_releases_it()
    {
        await using var harness = new ModuleHarness();
        var poller = await harness.UseScriptedPollerAsync(_ => Result(Record("n1", "[Codex] hello")));
        await harness.InitializeAsync();
        harness.Module.BackgroundPollerForTests.DelayAsync = (_, _) => Task.CompletedTask;

        var start = await harness.ExecuteAsync(RemoteNotificationsAndroidOptions.CommandPollingStart);
        Assert.True(start.Success, start.Error?.Message);
        Assert.Equal(1, harness.Background.AcquireCount);
        Assert.Contains(RemoteNotificationsAndroidOptions.BackgroundActivityTitle, harness.Background.Titles);
        Assert.True(await harness.WaitUntilAsync(() => poller.Calls >= 2, TimeSpan.FromSeconds(5)));

        var stop = await harness.ExecuteAsync(RemoteNotificationsAndroidOptions.CommandPollingStop);
        Assert.True(stop.Success, stop.Error?.Message);
        var callsAtStop = poller.Calls;
        await Task.Delay(200);
        Assert.Equal(callsAtStop, poller.Calls);
        Assert.Equal(0, harness.Background.ActiveCount);
    }

    [Fact]
    public async Task Polling_start_without_a_signing_key_fails_and_keeps_background_stopped()
    {
        await using var harness = new ModuleHarness();
        await harness.InitializeAsync();

        var result = await harness.ExecuteAsync(RemoteNotificationsAndroidOptions.CommandPollingStart);

        Assert.False(result.Success);
        Assert.Equal(RemoteNotificationErrorCodes.PermissionRequired, result.Error?.Code);
        Assert.Equal(0, harness.Background.AcquireCount);
    }

    [Fact]
    public async Task Notification_permission_denial_is_reported_as_a_permission_error()
    {
        var background = new FakeBackgroundActivityService
        {
            Failure = new UnauthorizedAccessException("请允许通知，以便在后台接收文件时显示状态和停止按钮。")
        };
        await using var harness = new ModuleHarness(background: background);
        using var key = TestSigningKey.Create();
        await harness.ImportKeyAsync(key);
        await harness.InitializeAsync();

        var result = await harness.ExecuteAsync(RemoteNotificationsAndroidOptions.CommandPollingStart);

        Assert.False(result.Success);
        Assert.Equal(RemoteNotificationErrorCodes.PermissionRequired, result.Error?.Code);
        Assert.Contains("请允许通知", result.Error!.Message, StringComparison.Ordinal);
    }

    [Fact]
    public async Task Host_without_background_capability_reports_a_friendly_failure()
    {
        await using var harness = new ModuleHarness(withoutBackgroundCapability: true);
        using var key = TestSigningKey.Create();
        await harness.ImportKeyAsync(key);
        await harness.InitializeAsync();

        var result = await harness.ExecuteAsync(RemoteNotificationsAndroidOptions.CommandPollingStart);

        Assert.False(result.Success);
        Assert.Equal(RemoteNotificationErrorCodes.RuntimeUnavailable, result.Error?.Code);
        var status = await harness.Module.GetStatusAsync(CancellationToken.None);
        Assert.Contains(status.Checks, check => check.Id == "notification.background" && !check.Ok);
    }

    [Fact]
    public async Task Disabling_the_module_cancels_background_polling_and_releases_the_lease()
    {
        await using var harness = new ModuleHarness();
        var poller = await harness.UseScriptedPollerAsync(_ => Result());
        await harness.InitializeAsync();
        harness.Module.BackgroundPollerForTests.DelayAsync = (_, _) => Task.CompletedTask;
        await harness.ExecuteAsync(RemoteNotificationsAndroidOptions.CommandPollingStart);

        await harness.Module.DisableAsync(harness.Context, CancellationToken.None);

        Assert.Equal(0, harness.Background.ActiveCount);
        var calls = poller.Calls;
        await Task.Delay(150);
        Assert.Equal(calls, poller.Calls);
    }

    [Fact]
    public async Task Sync_delivers_each_new_notification_once_and_persists_it()
    {
        await using var harness = new ModuleHarness();
        var record = Record("n1", "[Codex] build finished", icon: "codex");
        var poller = await harness.UseScriptedPollerAsync(_ => Result(record));
        await harness.InitializeAsync();

        var first = await harness.ExecuteAsync(RemoteNotificationsAndroidOptions.CommandSyncNow);
        var second = await harness.ExecuteAsync(RemoteNotificationsAndroidOptions.CommandSyncNow);

        Assert.True(first.Success, first.Error?.Message);
        Assert.Contains("\"accepted\": 1", first.Output, StringComparison.Ordinal);
        Assert.Contains("\"accepted\": 0", second.Output, StringComparison.Ordinal);
        Assert.Single(harness.Notifications.Published);
        var published = harness.Notifications.Published[0];
        Assert.Equal("Codex", published.Title);
        Assert.Equal("build finished", published.Body);
        Assert.StartsWith("mypowertools://remote-notification?id=", published.ActivationUri, StringComparison.Ordinal);
        Assert.True(File.Exists(harness.HistoryPath));
        Assert.Contains("n1", await File.ReadAllTextAsync(harness.HistoryPath), StringComparison.Ordinal);
        Assert.Equal(2, poller.Calls);
    }

    [Fact]
    public async Task Sync_without_a_signing_key_reports_auth_and_does_not_throw()
    {
        await using var harness = new ModuleHarness();
        await harness.InitializeAsync();

        var result = await harness.ExecuteAsync(RemoteNotificationsAndroidOptions.CommandSyncNow);

        Assert.True(result.Success, result.Error?.Message);
        Assert.Contains("\"connectionState\": \"auth\"", result.Output, StringComparison.Ordinal);
        Assert.Empty(harness.Notifications.Published);
    }

    [Fact]
    public async Task Delivery_failure_keeps_the_message_in_history()
    {
        var notifications = new FakeNotificationService { Failure = new UnauthorizedAccessException("notification permission revoked") };
        await using var harness = new ModuleHarness(notifications: notifications);
        var poller = await harness.UseScriptedPollerAsync(_ => Result(Record("n1", "[Codex] still stored")));
        await harness.InitializeAsync();

        var result = await harness.ExecuteAsync(RemoteNotificationsAndroidOptions.CommandSyncNow);

        Assert.True(result.Success, result.Error?.Message);
        Assert.Contains("n1", await File.ReadAllTextAsync(harness.HistoryPath), StringComparison.Ordinal);
        Assert.Contains("\"deliveryState\": \"error\"", result.Output, StringComparison.Ordinal);
    }

    [Fact]
    public async Task Module_events_report_message_receipt_with_the_desktop_payload_shape()
    {
        await using var harness = new ModuleHarness();
        await harness.UseScriptedPollerAsync(_ => Result(Record("n9", "[Claude] done", icon: "claude", sessionId: "s-3", sourceClient: "claude")));
        await harness.InitializeAsync();

        using var cancellation = new CancellationTokenSource(TimeSpan.FromSeconds(10));
        var events = new List<MptModuleEvent>();
        var pump = Task.Run(async () =>
        {
            await foreach (var item in harness.Module.SubscribeEventsAsync(new EventCursor(0), cancellation.Token))
            {
                events.Add(item);
                if (events.Any(e => e.Type == "message.received"))
                {
                    return;
                }
            }
        });

        await Task.Delay(100);
        await harness.ExecuteAsync(RemoteNotificationsAndroidOptions.CommandSyncNow);
        await pump;

        Assert.Contains(events, item => item.Type == "module.running");
        var received = Assert.Single(events, item => item.Type == "message.received");
        Assert.True(received.Seq > 0);
        Assert.Equal("Claude", received.Payload["title"]!.GetValue<string>());
        Assert.Equal("n9", received.Payload["messageId"]!.GetValue<string>());
        Assert.Equal("default", received.Payload["channel"]!.GetValue<string>());
        Assert.Equal("s-3", received.Payload["sessionId"]!.GetValue<string>());
        Assert.Equal("claude", received.Payload["sourceClient"]!.GetValue<string>());
        Assert.False(received.Payload["suppressed"]!.GetValue<bool>());
    }

    [Fact]
    public async Task Settings_schema_never_exposes_the_signing_key()
    {
        await using var harness = new ModuleHarness();
        await harness.InitializeAsync();

        var schema = await harness.Module.GetSettingsSchemaAsync(CancellationToken.None);
        Assert.DoesNotContain("signingKey\"", schema.SchemaJson, StringComparison.Ordinal);
        Assert.DoesNotContain("privateKey", schema.SchemaJson, StringComparison.OrdinalIgnoreCase);
        Assert.Contains("signingKeyConfigured", schema.SchemaJson, StringComparison.Ordinal);

        var snapshot = await harness.Module.GetSettingsAsync(CancellationToken.None);
        Assert.False(snapshot.Values["signingKeyConfigured"]!.GetValue<bool>());
        Assert.False(snapshot.Values.ContainsKey("signingKey"));
    }

    [Fact]
    public async Task Settings_patch_cannot_smuggle_a_private_key_into_the_host_store()
    {
        await using var harness = new ModuleHarness();
        await harness.InitializeAsync();

        var validation = await harness.Module.ValidateSettingsAsync(
            new SettingsPatch(
                RemoteNotificationsAndroidOptions.ModuleId,
                1,
                new JsonObject { ["signingKey"] = "-----BEGIN OPENSSH PRIVATE KEY-----" }),
            CancellationToken.None);

        Assert.False(validation.Ok);
        Assert.Equal(RemoteNotificationErrorCodes.ValidationFailed, validation.Error?.Code);

        await Assert.ThrowsAsync<ArgumentException>(() => harness.Module.ApplySettingsAsync(
            new SettingsSnapshotDocument(
                RemoteNotificationsAndroidOptions.ModuleId,
                1,
                new JsonObject { ["signingKey"] = "-----BEGIN OPENSSH PRIVATE KEY-----" },
                DateTimeOffset.UtcNow),
            CancellationToken.None).AsTask());
    }

    [Fact]
    public async Task Settings_apply_persists_values_and_is_visible_in_status()
    {
        await using var harness = new ModuleHarness();
        await harness.InitializeAsync();

        var applied = await harness.Module.ApplySettingsAsync(
            new SettingsSnapshotDocument(
                RemoteNotificationsAndroidOptions.ModuleId,
                1,
                new JsonObject
                {
                    ["protocol"] = "https",
                    ["host"] = "notify.example.test",
                    ["port"] = 9443,
                    ["channel"] = "phone",
                    ["pollIntervalSeconds"] = 30
                },
                DateTimeOffset.UtcNow),
            CancellationToken.None);

        Assert.Equal("phone", applied.Values["channel"]!.GetValue<string>());
        var persisted = await File.ReadAllTextAsync(harness.SettingsPath);
        Assert.Contains("notify.example.test", persisted, StringComparison.Ordinal);
        Assert.Contains("\"channel\": \"phone\"", persisted, StringComparison.Ordinal);

        var status = await harness.ExecuteAsync(RemoteNotificationsAndroidOptions.CommandStatus);
        Assert.Contains("https://notify.example.test:9443", status.Output, StringComparison.Ordinal);
    }

    [Fact]
    public async Task Settings_apply_rejects_invalid_values()
    {
        await using var harness = new ModuleHarness();
        await harness.InitializeAsync();

        var validation = await harness.Module.ValidateSettingsAsync(
            new SettingsPatch(
                RemoteNotificationsAndroidOptions.ModuleId,
                1,
                new JsonObject { ["port"] = 0, ["pollIntervalSeconds"] = 1 }),
            CancellationToken.None);

        Assert.False(validation.Ok);
        Assert.NotEmpty(validation.Messages);
    }

    [Fact]
    public async Task Configure_command_validates_and_persists()
    {
        await using var harness = new ModuleHarness();
        await harness.InitializeAsync();

        var bad = await harness.ExecuteAsync(
            RemoteNotificationsAndroidOptions.CommandConfigure,
            new JsonObject { ["host"] = "", ["port"] = 8888 });
        Assert.False(bad.Success);
        Assert.Equal(RemoteNotificationErrorCodes.ValidationFailed, bad.Error?.Code);

        var good = await harness.ExecuteAsync(
            RemoteNotificationsAndroidOptions.CommandConfigure,
            new JsonObject { ["protocol"] = "http", ["host"] = "10.0.0.5", ["port"] = 8080, ["channel"] = "lab", ["pollIntervalSeconds"] = 15 });
        Assert.True(good.Success, good.Error?.Message);
        Assert.Contains("http://10.0.0.5:8080", good.Output, StringComparison.Ordinal);
    }

    [Fact]
    public async Task Inbox_summary_clear_and_banner_preview_commands_work()
    {
        await using var harness = new ModuleHarness();
        await harness.UseScriptedPollerAsync(_ => Result(Record("n1", "[Codex] stored"), Record("n2", "[Claude] other", icon: "claude", sourceClient: "claude")));
        await harness.InitializeAsync();
        await harness.ExecuteAsync(RemoteNotificationsAndroidOptions.CommandSyncNow);

        var summary = await harness.ExecuteAsync(RemoteNotificationsAndroidOptions.CommandInboxSummary);
        Assert.True(summary.Success, summary.Error?.Message);
        Assert.Contains("\"messageCount\": 2", summary.Output, StringComparison.Ordinal);
        Assert.Contains("\"labelCount\": 2", summary.Output, StringComparison.Ordinal);

        var preview = await harness.ExecuteAsync(
            RemoteNotificationsAndroidOptions.CommandBannerPreview,
            new JsonObject { ["message"] = "[Codex] preview only" });
        Assert.True(preview.Success, preview.Error?.Message);
        Assert.Contains("\"title\": \"Codex\"", preview.Output, StringComparison.Ordinal);
        Assert.Contains("\"published\": false", preview.Output, StringComparison.Ordinal);
        Assert.Equal(2, harness.Notifications.Published.Count);

        var cleared = await harness.ExecuteAsync(RemoteNotificationsAndroidOptions.CommandInboxClear);
        Assert.True(cleared.Success, cleared.Error?.Message);
        Assert.Contains("\"messageCount\": 0", cleared.Output, StringComparison.Ordinal);
    }

    [Fact]
    public async Task Status_reports_the_shared_private_directory()
    {
        await using var harness = new ModuleHarness();
        await harness.InitializeAsync();

        var result = await harness.ExecuteAsync(RemoteNotificationsAndroidOptions.CommandStatus);

        Assert.True(result.Success, result.Error?.Message);
        Assert.Contains(harness.ToolDataDirectory.Replace("\\", "\\\\", StringComparison.Ordinal), result.Output, StringComparison.Ordinal);
        Assert.Contains("\"notificationAvailable\": true", result.Output, StringComparison.Ordinal);
        Assert.Contains("\"backgroundAvailable\": true", result.Output, StringComparison.Ordinal);
        Assert.DoesNotContain("PRIVATE KEY", result.Output, StringComparison.Ordinal);
    }

    [Fact]
    public async Task Command_ids_are_stable_and_every_listed_command_executes_or_reports_not_found()
    {
        await using var harness = new ModuleHarness();
        await harness.InitializeAsync();

        var descriptors = await harness.Module.ListCommandsAsync(CancellationToken.None);
        Assert.Equal(
            RemoteNotificationsAndroidOptions.CommandIds.OrderBy(id => id, StringComparer.Ordinal),
            descriptors.Select(descriptor => descriptor.Id).OrderBy(id => id, StringComparer.Ordinal));
        Assert.All(descriptors, descriptor => Assert.Equal(RemoteNotificationsAndroidOptions.ModuleId, descriptor.ModuleId));

        var unknown = await harness.ExecuteAsync("remote-notifications-android.does-not-exist");
        Assert.False(unknown.Success);
        Assert.Equal(RemoteNotificationErrorCodes.NotFound, unknown.Error?.Code);
    }

    [Fact]
    public void Module_data_directory_maps_to_the_tool_directory_the_surface_uses()
    {
        var state = System.IO.Path.Combine(System.IO.Path.GetTempPath(), "mpt-state");
        var moduleData = System.IO.Path.Combine(state, "modules", RemoteNotificationsAndroidOptions.ModuleId, "data");

        var resolved = RemoteNotificationAndroidPaths.ResolveDataRoot(moduleData);

        Assert.Equal(
            System.IO.Path.Combine(state, "tools", RemoteNotificationsAndroidOptions.ToolId),
            resolved);
        Assert.Equal(
            System.IO.Path.Combine(resolved, "history.json"),
            RemoteNotificationAndroidPaths.HistoryPath(resolved));
        Assert.Equal(
            System.IO.Path.Combine(resolved, "settings.json"),
            RemoteNotificationAndroidPaths.SettingsPath(resolved));
    }

    [Fact]
    public void Unexpected_module_layout_falls_back_to_the_module_private_directory()
    {
        var custom = System.IO.Path.Combine(System.IO.Path.GetTempPath(), "mpt-custom-module-dir");
        Assert.Equal(custom, RemoteNotificationAndroidPaths.ResolveDataRoot(custom));
    }

    [Fact]
    public void Error_codes_still_match_the_protocol_contract()
    {
        Assert.Equal(MyPowerTools.Protocol.MptErrorCodes.NotFound, RemoteNotificationErrorCodes.NotFound);
        Assert.Equal(MyPowerTools.Protocol.MptErrorCodes.RuntimeUnavailable, RemoteNotificationErrorCodes.RuntimeUnavailable);
        Assert.Equal(MyPowerTools.Protocol.MptErrorCodes.ValidationFailed, RemoteNotificationErrorCodes.ValidationFailed);
        Assert.Equal(MyPowerTools.Protocol.MptErrorCodes.PermissionRequired, RemoteNotificationErrorCodes.PermissionRequired);
        Assert.Equal(MyPowerTools.Protocol.MptErrorCodes.CapabilityMissing, RemoteNotificationErrorCodes.CapabilityMissing);
    }
}
