using System.Net;
using AndroidTools.MyPowerTools;
using MyPowerTools.Platform.Abstractions;
using MyPowerTools.RemoteNotifications.Configuration;
using RemoteNotifications.Surface.Services;

namespace RemoteNotifications.Android.Tests;

/// <summary>
/// Banner text and inbox merge behaviour must be the product's, not an Android dialect. The desktop
/// toast publisher and the desktop background receiver are compiled into this suite from their
/// shipped sources and compared directly against the Android implementations.
/// </summary>
[Collection("desktop-data-root")]
public sealed class BannerAndInboxParityTests
{
    private static RemoteNotificationRecord Record(
        string message,
        string channel = "default",
        string icon = "info",
        string sessionId = "",
        string sourceClient = "",
        string contentKind = "",
        string? timestamp = null) =>
        new(
            "n1",
            channel,
            message,
            icon,
            timestamp ?? DateTimeOffset.UtcNow.ToString("O", System.Globalization.CultureInfo.InvariantCulture),
            timestamp ?? DateTimeOffset.UtcNow.ToString("O", System.Globalization.CultureInfo.InvariantCulture),
            sessionId,
            "",
            sourceClient,
            "",
            "",
            contentKind);

    [Theory]
    [InlineData("[Codex] build finished")]
    [InlineData("no label here at all")]
    [InlineData("[Claude] > earlier question\n\n[Claude] the answer\n\n---\n\nFor reference:\n\n> earlier question")]
    [InlineData("[Claude] 中文通知：构建完成，包含较长的正文内容用于确认截断行为是否一致。")]
    public void Banner_title_and_body_match_the_desktop_toast(string message)
    {
        var record = Record(message, icon: "claude", sourceClient: "claude");
        var stableId = RemoteNotificationsLegacyStore.StableId(record);

        var banner = RemoteNotificationAndroidBanner.Build(record, stableId);
        var desktop = RemoteNotificationWindowsToastPublisher.BuildEnvelope(record, stableId, persistent: false);

        Assert.False(banner.Suppressed);
        Assert.Equal(desktop.Title, banner.Title);
        Assert.Equal(desktop.Body, banner.Body);
        Assert.Equal(desktop.LaunchUri, banner.LaunchUri);
        Assert.Equal(desktop.MessageId, banner.MessageId);
    }

    [Fact]
    public void Quoted_request_is_never_part_of_the_banner_body()
    {
        var record = Record(
            "> should not be shown\n\n[Codex] shipped\n\n---\n\nFor reference:\n\n> should not be shown",
            icon: "codex",
            sourceClient: "codex");
        var banner = RemoteNotificationAndroidBanner.Build(record, RemoteNotificationsLegacyStore.StableId(record));

        Assert.Equal("Codex", banner.Title);
        Assert.Equal("shipped", banner.Body);
        Assert.DoesNotContain("should not be shown", banner.Body, StringComparison.Ordinal);
    }

    [Fact]
    public void Long_bodies_are_truncated_at_the_desktop_limit()
    {
        var message = $"[Codex] {new string('x', 1200)}";
        var record = Record(message, icon: "codex", sourceClient: "codex");
        var banner = RemoteNotificationAndroidBanner.Build(record, RemoteNotificationsLegacyStore.StableId(record));
        var desktop = RemoteNotificationWindowsToastPublisher.BuildEnvelope(
            record,
            RemoteNotificationsLegacyStore.StableId(record),
            persistent: false);

        Assert.Equal(900, banner.Body.Length);
        Assert.EndsWith("...", banner.Body, StringComparison.Ordinal);
        Assert.Equal(desktop.Body, banner.Body);
    }

    [Fact]
    public async Task Claude_task_and_system_health_records_stay_silent_like_the_desktop()
    {
        var claudeTask = Record("[Claude Task] duplicate stop event", icon: "claude", sourceClient: "claude");
        var health = Record("[CHRS 健康] all good", icon: "info", sourceClient: "chris-health", contentKind: "system_health");
        var desktopPublisher = new RemoteNotificationWindowsToastPublisher();

        var claudeBanner = RemoteNotificationAndroidBanner.Build(claudeTask, RemoteNotificationsLegacyStore.StableId(claudeTask));
        var healthBanner = RemoteNotificationAndroidBanner.Build(health, RemoteNotificationsLegacyStore.StableId(health));
        var desktopClaude = await desktopPublisher.PublishAsync(claudeTask, "id", persistent: false);
        var desktopHealth = await desktopPublisher.PublishAsync(health, "id", persistent: false);

        Assert.True(claudeBanner.Suppressed);
        Assert.True(healthBanner.Suppressed);
        Assert.Equal(desktopClaude.State, claudeBanner.State);
        Assert.Equal(desktopHealth.State, healthBanner.State);
        Assert.False(desktopClaude.Shown);
        Assert.False(desktopHealth.Shown);
    }

    [Fact]
    public async Task Suppressed_banners_are_not_published_to_the_tray()
    {
        var notifications = new FakeNotificationService();
        var health = Record("[CHRS 健康] all good", sourceClient: "chris-health", contentKind: "system_health");
        var banner = RemoteNotificationAndroidBanner.Build(health, RemoteNotificationsLegacyStore.StableId(health));

        var published = await banner.PublishAsync(notifications, CancellationToken.None);

        Assert.False(published);
        Assert.Empty(notifications.Published);
    }

    [Fact]
    public async Task Android_merge_semantics_match_the_desktop_background_receiver()
    {
        using var key = TestSigningKey.Create();
        var androidRoot = TestDirectory.Create();
        var desktopRoot = TestDirectory.Create();
        try
        {
            var first = TestJson.Notification("dup-1", "[Codex] first", icon: "codex");
            var duplicate = TestJson.Notification("dup-1", "[Codex] first", icon: "codex");
            var second = TestJson.Notification(
                "dup-2",
                "[Claude] second\n\n---\n\nFor reference:\n\n> question",
                icon: "claude",
                sessionId: "s-1",
                sourceClient: "claude");
            var responses = new Queue<string>(
            [
                TestJson.Envelope(first, second),
                TestJson.Envelope(duplicate, second)
            ]);

            var androidHandler = new RecordingHttpHandler(_ => TestJson.Ok(responses.Peek()));
            var androidPoller = new RemoteNotificationSignedPoller(
                Settings(key),
                await AndroidSigningKeyAsync(key),
                new HttpClient(androidHandler));
            var androidStore = new RemoteNotificationsLegacyStore(
                new RemoteNotificationSettingsStore(System.IO.Path.Combine(androidRoot.Path, "settings.json")),
                androidRoot.Path);
            var androidSynchronizer = new RemoteNotificationInboxSynchronizer(androidStore, androidPoller);

            // The desktop receiver resolves its store through MPT_TOOL_DATA_ROOT.
            var previousRoot = Environment.GetEnvironmentVariable("MPT_TOOL_DATA_ROOT");
            Environment.SetEnvironmentVariable("MPT_TOOL_DATA_ROOT", desktopRoot.Path);
            try
            {
                var desktopHandler = new RecordingHttpHandler(_ => TestJson.Ok(responses.Dequeue()));
                // The shipped receiver builds its poller without an HttpClient, so the poller's
                // shared client is swapped for a recording one. The field is renamed only with the
                // shipped source, and UseSharedDesktopClient fails loudly if that happens.
                using var desktopClient = UseSharedDesktopClient(new HttpClient(desktopHandler));
                var desktopReceiver = new RemoteNotificationBackgroundReceiver(Settings(key));

                var firstAndroid = await androidSynchronizer.PollAsync(CancellationToken.None);
                var firstDesktop = await desktopReceiver.PollAsync(CancellationToken.None);
                Assert.Equal(
                    firstDesktop.Accepted.Select(RemoteNotificationsLegacyStore.StableId),
                    firstAndroid.Accepted.Select(RemoteNotificationsLegacyStore.StableId));
                Assert.Equal(firstDesktop.Waterline, firstAndroid.Waterline);

                var secondAndroid = await androidSynchronizer.PollAsync(CancellationToken.None);
                var secondDesktop = await desktopReceiver.PollAsync(CancellationToken.None);
                Assert.Empty(secondAndroid.Accepted);
                Assert.Empty(secondDesktop.Accepted);
                Assert.Equal(secondDesktop.Waterline, secondAndroid.Waterline);
            }
            finally
            {
                Environment.SetEnvironmentVariable("MPT_TOOL_DATA_ROOT", previousRoot);
            }

            // Reference-block semantics survive the persisted round trip on both sides.
            var androidSnapshot = androidStore.Load();
            Assert.Equal(2, androidSnapshot.MessagesOldestFirst.Count);
            var persistedQuote = androidSnapshot.MessagesOldestFirst.Last();
            // Reference-block semantics are the shipped store's: the trailing reference appendix is
            // split off for display, while the persisted record keeps the full original message.
            Assert.Equal("[Claude] second", RemoteNotificationsLegacyStore.StripLeadingQuotedRequest(persistedQuote.Message));
            Assert.Contains("For reference", persistedQuote.Message, StringComparison.Ordinal);
            Assert.Equal(
                "second",
                RemoteNotificationAndroidBanner.Build(
                    persistedQuote,
                    RemoteNotificationsLegacyStore.StableId(persistedQuote)).Body);
            Assert.Equal(2, androidSnapshot.KnownLabels.Count);
            Assert.Contains("Codex", androidSnapshot.KnownLabels);
            Assert.Contains("Claude", androidSnapshot.KnownLabels);

            var desktopHistory = File.ReadAllText(System.IO.Path.Combine(desktopRoot.Path, "history.json"));
            Assert.Contains("dup-1", desktopHistory, StringComparison.Ordinal);
            Assert.Contains("dup-2", desktopHistory, StringComparison.Ordinal);
        }
        finally
        {
            androidRoot.Dispose();
            desktopRoot.Dispose();
        }
    }

    [Fact]
    public void Future_timestamps_are_dropped_like_the_desktop_receiver()
    {
        var future = Record("future", timestamp: DateTimeOffset.UtcNow.AddHours(1).ToString("O"));
        var sane = Record("now");
        Assert.False(RemoteNotificationInboxSynchronizer.IsSane(future));
        Assert.True(RemoteNotificationInboxSynchronizer.IsSane(sane));
    }

    [Fact]
    public void Waterline_is_the_newest_server_timestamp_and_ignores_future_skew()
    {
        var older = Record("older", timestamp: "2026-01-01T00:00:00.0000000Z");
        var newer = Record("newer", timestamp: "2026-02-01T00:00:00.0000000Z");
        var future = Record("future", timestamp: DateTimeOffset.UtcNow.AddHours(3).ToString("O"));

        var waterline = RemoteNotificationInboxSynchronizer.ResolveWaterline([older, newer, future]);

        Assert.Equal(
            DateTimeOffset.Parse("2026-02-01T00:00:00Z", System.Globalization.CultureInfo.InvariantCulture)
                .ToUniversalTime()
                .ToString("O", System.Globalization.CultureInfo.InvariantCulture),
            waterline);
    }

    [Fact]
    public async Task Rejected_responses_do_not_touch_the_persisted_inbox()
    {
        using var key = TestSigningKey.Create();
        using var root = TestDirectory.Create();
        var handler = new RecordingHttpHandler(_ => TestJson.Status(HttpStatusCode.InternalServerError, "boom"));
        var poller = new RemoteNotificationSignedPoller(
            Settings(key),
            await AndroidSigningKeyAsync(key),
            new HttpClient(handler));
        var store = new RemoteNotificationsLegacyStore(
            new RemoteNotificationSettingsStore(System.IO.Path.Combine(root.Path, "settings.json")),
            root.Path);
        var synchronizer = new RemoteNotificationInboxSynchronizer(store, poller);

        var outcome = await synchronizer.PollAsync(CancellationToken.None);

        Assert.Equal("error", outcome.Pull.State);
        Assert.Empty(outcome.Accepted);
        Assert.Empty(store.Load().MessagesOldestFirst);
    }

    /// <summary>
    /// Replaces the shipped poller's shared client for the duration of a test. The desktop receiver
    /// has no injection point, and a parity test must never touch the real server.
    /// </summary>
    private static IDisposable UseSharedDesktopClient(HttpClient client)
    {
        var field = typeof(RemoteNotificationHttpPoller).GetField(
            "_sharedHttpClient",
            System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Static)
            ?? throw new InvalidOperationException(
                "RemoteNotificationHttpPoller._sharedHttpClient was renamed; update this parity test.");
        var previous = (HttpClient)field.GetValue(null)!;
        field.SetValue(null, client);
        return new SharedClientRestore(field, previous);
    }

    private sealed class SharedClientRestore(System.Reflection.FieldInfo field, HttpClient previous) : IDisposable
    {
        public void Dispose() => field.SetValue(null, previous);
    }

    private static RemoteNotificationSettings Settings(TestSigningKey key) =>
        new("https", "message.example.test", 8888, "default", 5, key.Path, false);

    private static async Task<RemoteNotificationSigningKey> AndroidSigningKeyAsync(TestSigningKey key)
    {
        var signing = new RemoteNotificationSigningKey(
            new InMemorySecretStore(),
            RemoteNotificationsAndroidOptions.ModuleId);
        await signing.SaveAsync(key.Pem, CancellationToken.None);
        return signing;
    }
}
