using MyPowerTools.Shell.Avalonia.Services;
using MyPowerTools.Shell.Avalonia.ViewModels;
using MyPowerTools.Shell.Avalonia.Views;
using RemoteNotifications.Surface.Views;
using MyPowerTools.RemoteNotifications.Configuration;

namespace MyPowerTools.Tests;

public sealed class RemoteNotificationsProductTests
{
    private static readonly string Root = FindRepositoryRoot();

    [Fact]
    public void Session_metadata_is_visible_and_searchable()
    {
        var message = new RemoteNotificationMessageViewModel(new RemoteNotificationRecord(
            "session-message",
            "default",
            "Task complete",
            "codex",
            DateTimeOffset.UtcNow.ToString("O"),
            "",
            "00000000-0000-0000-0000-000000000001",
            "Session metadata rollout",
            "codex"));

        Assert.True(message.HasSession);
        Assert.Equal("00000000…", message.SessionShortId);
        Assert.Contains("Session metadata rollout", message.SessionDisplay, StringComparison.Ordinal);
        Assert.True(message.MatchesSearch("00000000-0000"));
        Assert.True(message.MatchesSearch("metadata rollout"));
    }

    [Fact]
    public void Task_completed_merges_into_the_previous_same_session_reply()
    {
        var reply = new RemoteNotificationRecord(
            "reply",
            "default",
            "[Claude Code] The work is complete.",
            "claude",
            "2026-08-18T12:00:00Z",
            "2026-08-18T12:00:00Z",
            "session-61",
            "",
            "claude");
        var completed = new RemoteNotificationRecord(
            "completed",
            "default",
            "[Claude Code] Task completed",
            "claude",
            "2026-08-18T12:38:00Z",
            "2026-08-18T12:38:00Z",
            "session-61",
            "",
            "claude");
        var duplicate = completed with { Id = "completed-duplicate" };

        var merged = RemoteNotificationsLegacyStore.MergeTaskCompletedRecords(
            [reply, completed, duplicate]);

        var result = Assert.Single(merged);
        Assert.Equal("reply", result.Id);
        Assert.Contains("The work is complete.", result.Message, StringComparison.Ordinal);
        Assert.EndsWith("Task completed", result.Message, StringComparison.Ordinal);
        Assert.Equal("2026-08-18T12:38:00Z", result.ServerTimestamp);
        Assert.True(RemoteNotificationsLegacyStore.IsTaskCompletedRecord(completed));
    }

    [Fact]
    public void Explicit_agent_internal_kind_is_filtered_but_human_kind_survives()
    {
        var internalMessage = new RemoteNotificationRecord(
            "internal",
            "default",
            "> 亲自巡查（不派子代理）：检查全部在跑实验 loop 日志；分析子代理是否返回；有新结果立即分析并主动向用户汇报；无新进展则一句话说明。\n>\n[autodroid-52] 巡查小结 + 新动作：三轮画像构建全部失败，下一步补起 B07/B08；监控确认进程在跑。",
            "claude",
            "2026-08-21T00:00:00Z",
            "",
            "session-internal",
            "autodroid-52",
            "claude",
            ContentKind: "agent_internal");
        var humanMessage = internalMessage with
        {
            Id = "human",
            Message = "> 亲自巡查（不派子代理）：检查全部在跑实验 loop 日志。\n\n[autodroid-52] 人工回复保留。",
            ContentKind = "text"
        };

        Assert.True(RemoteNotificationsLegacyStore.IsInternalAgentCommunication(internalMessage));
        Assert.False(RemoteNotificationsLegacyStore.IsInternalAgentCommunication(humanMessage));
    }

    [Fact]
    public void Historical_claude_task_event_identity_is_filtered_without_text_matching()
    {
        var legacy = new RemoteNotificationRecord(
            "legacy",
            "default",
            "[autodroid-52] A07 仍在，观察者就位。",
            "claude",
            "2026-08-21T01:48:09Z",
            SourceClient: "claude",
            SourceEventId: "038a6a87-8162-4f66-96d7-7b4d46ebf12d",
            ContentKind: "text");
        var human = legacy with
        {
            Id = "human",
            SourceEventId = "human-event",
            Message = "> A07 结果怎么样？\n\n[autodroid-52] 已完成。"
        };

        Assert.True(RemoteNotificationsLegacyStore.IsHistoricalClaudeInternalEvent(legacy));
        Assert.False(RemoteNotificationsLegacyStore.IsHistoricalClaudeInternalEvent(human));
        Assert.DoesNotContain(
            RemoteNotificationsLegacyStore.CleanupClaudeStopNoise([legacy, human]),
            message => message.Id == "legacy");
        Assert.Contains(
            RemoteNotificationsLegacyStore.CleanupClaudeStopNoise([legacy, human]),
            message => message.Id == "human");
    }

    [Fact]
    public async Task Task_completed_still_publishes_one_banner_without_creating_a_card()
    {
        var reply = new RemoteNotificationRecord(
            "reply",
            "default",
            "[Claude Code] The work is complete.",
            "claude",
            "2026-08-18T12:00:00Z",
            "2026-08-18T12:00:00Z",
            "session-62",
            "",
            "claude");
        var completed = new RemoteNotificationRecord(
            "completed",
            "default",
            "[Claude Code] Task completed",
            "claude",
            "2026-08-18T12:38:00Z",
            "2026-08-18T12:38:00Z",
            "session-62",
            "",
            "claude");
        var store = new FakeStore(new RemoteNotificationsSnapshot(
            [reply],
            ["Claude Code"],
            null,
            false));
        var toasts = new FakeToastPublisher();
        var viewModel = new RemoteNotificationsViewModel(
            new RemoteNotificationsSnapshot([reply], ["Claude Code"], null, false),
            store,
            new FakePoller(new RemoteNotificationPullResult("ok", [completed], "")),
            toasts);

        await viewModel.PollAsync();

        var card = Assert.Single(viewModel.Messages);
        Assert.Equal("reply", card.Id);
        Assert.Contains("Task completed", card.Message, StringComparison.Ordinal);
        Assert.Equal("1", viewModel.Shown);
        Assert.Equal("completed", Assert.Single(toasts.Published).MessageId);
    }

    [Fact]
    public void Detail_window_title_hides_the_internal_default_channel()
    {
        var defaultMessage = new RemoteNotificationMessageViewModel(
            Message("default-title", "message", DateTimeOffset.UtcNow));
        var customMessage = new RemoteNotificationMessageViewModel(new RemoteNotificationRecord(
            "custom-title",
            "builds",
            "message",
            "codex",
            DateTimeOffset.UtcNow.ToString("O")));

        Assert.Equal("Remote notification", defaultMessage.DetailWindowTitle);
        Assert.Equal("builds notification", customMessage.DetailWindowTitle);
    }

    [Fact]
    public void Feed_preview_removes_the_repeated_topic_prefix()
    {
        var message = new RemoteNotificationMessageViewModel(
            Message("preview", "[alpha] **Build complete**\n\nOpen the artifact.", DateTimeOffset.UtcNow));

        Assert.Equal("alpha", message.Label);
        Assert.Equal("**Build complete**\n\nOpen the artifact.", message.DisplayMessage);
        Assert.StartsWith("[alpha]", message.Message, StringComparison.Ordinal);
    }

    [Fact]
    public void Remote_notification_detail_window_renders_markdown_through_native_web_view()
    {
        var integrationRoot = Path.Combine(
            Root, "tools", "remote-notifications", "current-integration", "src", "RemoteNotifications.Surface");
        var project = File.ReadAllText(Path.Combine(integrationRoot, "RemoteNotifications.Surface.csproj"));
        var feed = File.ReadAllText(Path.Combine(integrationRoot, "Views", "RemoteNotificationsView.axaml"));
        var detail = File.ReadAllText(Path.Combine(integrationRoot, "Views", "RemoteNotificationDetailWindow.axaml"));
        var detailCode = File.ReadAllText(Path.Combine(integrationRoot, "Views", "RemoteNotificationDetailWindow.axaml.cs"));
        var service = File.ReadAllText(Path.Combine(integrationRoot, "Services", "RemoteNotificationDetailWindowService.cs"));
        var viewCode = File.ReadAllText(Path.Combine(integrationRoot, "Views", "RemoteNotificationsView.axaml.cs"));
        var factory = File.ReadAllText(Path.Combine(integrationRoot, "RemoteNotificationsSurfaceFactory.cs"));

        Assert.Contains("Avalonia.Controls.WebView", project, StringComparison.Ordinal);
        Assert.Contains("Markdig", project, StringComparison.Ordinal);
        Assert.Contains("x:Name=\"MarkdownWebView\"", detail, StringComparison.Ordinal);
        Assert.Contains("NativeWebView", detail, StringComparison.Ordinal);
        Assert.Contains("x:Name=\"FallbackViewer\"", detail, StringComparison.Ordinal);
        Assert.Contains("x:Name=\"FallbackStatus\"", detail, StringComparison.Ordinal);
        Assert.DoesNotContain("MarkdownViewerHost", detail, StringComparison.Ordinal);
        Assert.DoesNotContain("MptMarkdownView", detail, StringComparison.Ordinal);
        Assert.Contains("NavigateToString", detailCode, StringComparison.Ordinal);
        Assert.Contains("Markdown.ToHtml", detailCode, StringComparison.Ordinal);
        Assert.Contains("DisableHtml", detailCode, StringComparison.Ordinal);
        Assert.Contains("UseAdvancedExtensions", detailCode, StringComparison.Ordinal);
        Assert.Contains("UseShellExecute", detailCode, StringComparison.Ordinal);
        Assert.DoesNotContain("RemoteNotificationMarkdownViewerController", detailCode, StringComparison.Ordinal);
        Assert.DoesNotContain("IMptWebSurfaceService", service, StringComparison.Ordinal);
        Assert.Contains("new RemoteNotificationDetailWindow(message)", service, StringComparison.Ordinal);
        Assert.DoesNotContain("IMptWebSurfaceService", viewCode, StringComparison.Ordinal);
        Assert.DoesNotContain("context.WebSurfaces", factory, StringComparison.Ordinal);
        Assert.DoesNotContain("RemoteNotificationsView(context.WebSurfaces", factory, StringComparison.Ordinal);

        Assert.Contains("<controls:MptMarkdownView Markdown=\"{Binding DisplayMessage}\"", feed, StringComparison.Ordinal);
        Assert.Contains("ColumnDefinitions=\"*,320,Auto\"", feed, StringComparison.Ordinal);
        Assert.Contains("x:Name=\"LabelScroller\"", feed, StringComparison.Ordinal);
        Assert.Contains("HorizontalScrollBarVisibility=\"Hidden\"", feed, StringComparison.Ordinal);
        Assert.Contains("PointerMoved=\"OnLabelScrollerPointerMoved\"", feed, StringComparison.Ordinal);
        Assert.Contains("<StackPanel Orientation=\"Horizontal\" />", feed, StringComparison.Ordinal);
        Assert.DoesNotContain("<WrapPanel", feed, StringComparison.Ordinal);
    }

    [Theory]
    [InlineData(0, 1000, 1120, 400, 0)]
    [InlineData(40, 1000, 1120, 400, 0)]
    [InlineData(100, 1000, 1120, 400, 220)]
    [InlineData(700, 1000, 1120, 400, 720)]
    public void Newest_first_feed_preserves_the_reading_anchor(
        double previousOffset,
        double previousExtent,
        double currentExtent,
        double viewport,
        double expected)
    {
        Assert.Equal(
            expected,
            RemoteNotificationScrollAnchor.CalculateOffset(
                previousOffset,
                previousExtent,
                currentExtent,
                viewport));
    }

    [Theory]
    [InlineData(0, -120, 1000, 400, 120)]
    [InlineData(200, 80, 1000, 400, 120)]
    [InlineData(550, -200, 1000, 400, 600)]
    [InlineData(40, 200, 1000, 400, 0)]
    public void Label_strip_drag_clamps_the_horizontal_offset(
        double startOffset,
        double pointerDelta,
        double extent,
        double viewport,
        double expected)
    {
        Assert.Equal(
            expected,
            RemoteNotificationLabelDrag.CalculateOffset(startOffset, pointerDelta, extent, viewport));
    }

    [Fact]
    public void Restored_history_uses_original_newest_first_feed_and_session_chip_filter()
    {
        var store = new FakeStore();
        var snapshot = new RemoteNotificationsSnapshot(
            [
                Message("old", "[alpha] older message", DateTimeOffset.UtcNow.AddMinutes(-4)),
                Message("new", "[beta] newest message", DateTimeOffset.UtcNow.AddMinutes(-2))
            ],
            ["beta", "alpha"],
            null,
            false);
        var viewModel = new RemoteNotificationsViewModel(snapshot, store, new FakePoller());

        Assert.Equal("new", viewModel.Messages[0].Id);
        Assert.Equal("old", viewModel.Messages[1].Id);
        Assert.Equal(["All", "beta", "alpha"], viewModel.Chips.Select(chip => chip.Label));
        Assert.Equal("2 messages", viewModel.MessageCountText);

        var alpha = viewModel.Chips.Single(chip => chip.Label == "alpha");
        alpha.SelectCommand.Execute(null);

        Assert.Equal("alpha", store.SavedFilter);
        Assert.Equal("old", Assert.Single(viewModel.VisibleMessages).Id);
        Assert.Equal("1 of 2 message", viewModel.MessageCountText);
    }

    [Fact]
    public async Task Empty_pull_uses_the_original_idle_status_treatment()
    {
        var viewModel = new RemoteNotificationsViewModel(
            new RemoteNotificationsSnapshot([], [], null, false),
            new FakeStore(),
            new FakePoller(new RemoteNotificationPullResult("idle", [], "")));

        await viewModel.PollAsync();

        Assert.Equal("Idle", viewModel.StatusText);
        Assert.Equal("#9E9E9E", viewModel.StatusForeground);
        Assert.Equal("#F0F0F0", viewModel.StatusBackground);
    }

    [Fact]
    public async Task Saving_product_settings_rebuilds_the_poller_and_refreshes_immediately()
    {
        var savedSettings = RemoteNotificationSettings.Default;
        var settingsStore = new FakeSettingsStore(savedSettings);
        var rebuiltPoller = new FakePoller(new RemoteNotificationPullResult("idle", [], ""));
        RemoteNotificationSettings? factorySettings = null;
        var viewModel = new RemoteNotificationsViewModel(
            new RemoteNotificationsSnapshot([], [], null, false),
            new FakeStore(),
            new FakePoller(),
            settingsStore: settingsStore,
            pollerFactory: settings =>
            {
                factorySettings = settings;
                return rebuiltPoller;
            });
        viewModel.ProtocolDraft = "http";
        viewModel.HostDraft = "0.0.0.0";
        viewModel.PortDraft = "19091";
        viewModel.ChannelDraft = "automation";
        viewModel.PollIntervalDraft = "17";
        viewModel.PrivateKeyPathDraft = "key";
        viewModel.KeepWindowsBannersDraft = true;

        viewModel.SaveSettingsCommand.Execute(null);
        await WaitUntilAsync(() => settingsStore.SaveCount == 1 && rebuiltPoller.CallCount == 1);

        Assert.NotNull(factorySettings);
        Assert.Equal("http://0.0.0.0:19091", factorySettings.Endpoint);
        Assert.Equal("automation", factorySettings.Channel);
        Assert.Equal(17, viewModel.PollIntervalSeconds);
        Assert.Equal("http://0.0.0.0:19091", viewModel.Server);
        Assert.True(viewModel.PersistentWindowsToasts);
        Assert.Equal("idle", viewModel.ConnectionState);
    }

    [Fact]
    public async Task Polling_deduplicates_persists_and_marks_the_active_session_chip_unread()
    {
        var existing = Message("existing", "[alpha] existing", DateTimeOffset.UtcNow.AddMinutes(-3));
        var incoming = Message("incoming", "[beta] incoming", DateTimeOffset.UtcNow.AddSeconds(-5));
        var store = new FakeStore();
        var poller = new FakePoller(new RemoteNotificationPullResult("ok", [existing, incoming], ""));
        var toasts = new FakeToastPublisher();
        var viewModel = new RemoteNotificationsViewModel(
            new RemoteNotificationsSnapshot([existing], ["alpha"], null, false),
            store,
            poller,
            toasts);

        await viewModel.PollAsync();

        Assert.Equal(2, viewModel.Messages.Count);
        Assert.Equal("incoming", viewModel.Messages[0].Id);
        Assert.Equal("2", viewModel.Fetched);
        Assert.Equal("1", viewModel.Shown);
        Assert.Equal("ok", viewModel.ConnectionState);
        Assert.True(viewModel.Chips.Single(chip => chip.Label == "beta").IsUnread);
        Assert.Equal("beta", store.SavedLabels[0]);
        Assert.Equal(2, store.SavedMessages.Count);
        Assert.Equal("incoming", Assert.Single(toasts.Published).MessageId);
        Assert.Contains("incoming", store.SavedSeenIds);
        Assert.Contains(RemoteNotificationsLegacyStore.FallbackId(incoming), store.SavedSeenIds);

        viewModel.AcknowledgeMessage(viewModel.Messages[0]);
        Assert.False(viewModel.Chips.Single(chip => chip.Label == "beta").IsUnread);
    }

    [Fact]
    public async Task Persisted_seen_ring_drops_a_replayed_message_outside_the_visible_history()
    {
        var replay = Message("server-message-900", "[beta] replay", DateTimeOffset.UtcNow.AddSeconds(-2));
        var toasts = new FakeToastPublisher();
        var store = new FakeStore();
        var viewModel = new RemoteNotificationsViewModel(
            new RemoteNotificationsSnapshot([], [], null, false, ["server-message-900"]),
            store,
            new FakePoller(new RemoteNotificationPullResult("ok", [replay], "")),
            toasts);

        await viewModel.PollAsync();

        Assert.Equal("0", viewModel.Shown);
        Assert.Empty(viewModel.Messages);
        Assert.Empty(toasts.Published);
    }

    [Fact]
    public async Task Persistent_toggle_is_forwarded_to_the_single_accepted_windows_toast()
    {
        var incoming = Message("persistent-message", "[build] needs attention", DateTimeOffset.UtcNow.AddSeconds(-1));
        var toasts = new FakeToastPublisher();
        var viewModel = new RemoteNotificationsViewModel(
            new RemoteNotificationsSnapshot([], [], null, true, []),
            new FakeStore(),
            new FakePoller(new RemoteNotificationPullResult("ok", [incoming, incoming], "")),
            toasts);

        await viewModel.PollAsync();

        var published = Assert.Single(toasts.Published);
        Assert.Equal("persistent-message", published.MessageId);
        Assert.True(published.Persistent);
        Assert.Equal("1", viewModel.Shown);
    }

    [Fact]
    public async Task Background_message_event_presents_each_persisted_notification_once()
    {
        var first = Message("event-first", "[build] first", DateTimeOffset.UtcNow.AddSeconds(-2));
        var second = Message("event-second", "[build] second", DateTimeOffset.UtcNow.AddSeconds(-1));
        var store = new FakeStore(new RemoteNotificationsSnapshot(
            [first, second],
            ["build"],
            null,
            true));
        var toasts = new FakeToastPublisher();
        var viewModel = new RemoteNotificationsViewModel(
            new RemoteNotificationsSnapshot([], [], null, true),
            store,
            new FakePoller(),
            toasts);

        var firstPass = await viewModel.PresentPersistedAsync([second.Id, first.Id, second.Id]);
        var replay = await viewModel.PresentPersistedAsync([first.Id, second.Id]);

        Assert.Equal(2, firstPass);
        Assert.Equal(0, replay);
        Assert.Equal([second.Id, first.Id], toasts.Published.Select(item => item.MessageId));
        Assert.All(toasts.Published, item => Assert.True(item.Persistent));
        Assert.Equal([first.Id, second.Id], viewModel.Messages.Select(item => item.Id));
    }

    [Fact]
    public void Original_fallback_id_and_seen_ring_limits_are_preserved()
    {
        var notification = new RemoteNotificationRecord(
            "",
            "default",
            "[alpha] hello",
            "codex",
            "2026-07-11T00:00:00Z");
        var ring = new RemoteNotificationSeenIdRing(
            Enumerable.Range(0, 5100).Select(index => $"id-{index}"));

        Assert.Equal("n4963c7b80bdb97f2f7966938", RemoteNotificationsLegacyStore.FallbackId(notification));
        Assert.Equal(5000, ring.Count);
        Assert.False(ring.Contains("id-99"));
        Assert.True(ring.Contains("id-100"));
        Assert.True(ring.Contains("id-5099"));
    }

    [Fact]
    public void Toast_contract_maps_persistent_to_reminder_and_targets_the_exact_message()
    {
        var notification = Message("message/id 42", "[build] **Complete**\nOpen it.", DateTimeOffset.UtcNow);
        var persistent = RemoteNotificationWindowsToastPublisher.BuildEnvelope(notification, notification.Id, persistent: true);
        var transient = RemoteNotificationWindowsToastPublisher.BuildEnvelope(notification, notification.Id, persistent: false);
        var xml = persistent.ToXml();

        Assert.Equal("build", persistent.Title);
        Assert.Equal("**Complete** Open it.", persistent.Body);
        Assert.Equal("reminder", persistent.Scenario);
        Assert.Equal("", transient.Scenario);
        Assert.Equal("mypowertools://remote-notification?id=message%2Fid%2042", persistent.LaunchUri);
        Assert.Contains("scenario=\"reminder\"", xml, StringComparison.Ordinal);
        Assert.Contains("activationType=\"protocol\"", xml, StringComparison.Ordinal);
        Assert.DoesNotContain("scenario=", transient.ToXml(), StringComparison.Ordinal);
    }

    [Fact]
    public void Toast_omits_the_quoted_user_request_from_the_banner_body()
    {
        var notification = Message(
            "quoted/id 42",
            "> 把全文发给我\n> 第二行\n\n[build] **Complete**\nOpen it.",
            DateTimeOffset.UtcNow);
        var envelope = RemoteNotificationWindowsToastPublisher.BuildEnvelope(
            notification, notification.Id, persistent: false);

        Assert.Equal("build", envelope.Title);
        Assert.Equal("**Complete** Open it.", envelope.Body);
        Assert.DoesNotContain("把全文发给我", envelope.Body, StringComparison.Ordinal);
        Assert.DoesNotContain("第二行", envelope.Body, StringComparison.Ordinal);
        Assert.DoesNotContain(">", envelope.Body, StringComparison.Ordinal);
    }

    [Theory]
    [InlineData(false, "")]
    [InlineData(true, "reminder")]
    public async Task Windows_publisher_sends_exactly_one_native_envelope_for_each_banner_mode(
        bool persistent,
        string expectedScenario)
    {
        var notification = Message("native/message 42", "[build] complete", DateTimeOffset.UtcNow);
        var platform = new RecordingToastPlatform();
        var publisher = new RemoteNotificationWindowsToastPublisher(platform);

        var result = await publisher.PublishAsync(
            notification,
            notification.Id,
            persistent);

        Assert.True(result.Shown);
        var envelope = Assert.Single(platform.Envelopes);
        Assert.Equal(expectedScenario, envelope.Scenario);
        Assert.Equal("native/message 42", envelope.MessageId);
        Assert.Equal(
            "mypowertools://remote-notification?id=native%2Fmessage%2042",
            envelope.LaunchUri);
    }

    [Fact]
    public void Native_windows_toast_abi_builds_a_real_notification_without_showing_a_banner()
    {
        if (!OperatingSystem.IsWindows())
        {
            return;
        }

        var result = WindowsRemoteNotificationToastPlatform.ProbeRuntime();

        Assert.True(result.Shown, result.Error);
        Assert.Equal("ready", result.State);
    }

    [Fact]
    public void Activation_protocol_recovers_the_exact_encoded_message_id()
    {
        var parsed = RemoteNotificationActivationProtocol.Parse(
            ["--remote-notification-activation", "mypowertools://remote-notification?id=message%2Fid%2042"]);

        Assert.NotNull(parsed);
        Assert.Equal("message/id 42", parsed.MessageId);
        Assert.Equal(
            "message/id 42",
            RemoteNotificationActivationProtocol.ParseLaunchUri(parsed.LaunchUri).MessageId);
    }

    [Fact]
    public async Task Activation_pipe_forwards_the_exact_message_to_the_running_shell_endpoint()
    {
        if (!OperatingSystem.IsWindows())
        {
            return;
        }

        var pipeName = $"MyPowerTools.RemoteNotificationActivation.Tests.{Guid.NewGuid():N}";
        var received = new TaskCompletionSource<RemoteNotificationActivationRequest>(TaskCreationOptions.RunContinuationsAsynchronously);
        await using var pipe = new RemoteNotificationActivationPipe(
            request =>
            {
                received.TrySetResult(request);
                return Task.CompletedTask;
            },
            pipeName);
        pipe.Start();
        var request = new RemoteNotificationActivationRequest(
            "exact-message-42",
            "mypowertools://remote-notification?id=exact-message-42");

        var forwarded = await RemoteNotificationActivationPipe.TryForwardToRunningShellAsync(
            request,
            TimeSpan.FromSeconds(2),
            pipeName: pipeName);
        var delivered = await received.Task.WaitAsync(TimeSpan.FromSeconds(2));

        Assert.True(forwarded);
        Assert.Equal(request, delivered);
    }

    [Fact]
    public void Shell_instance_mutex_prevents_a_second_startup_owner()
    {
        if (!OperatingSystem.IsWindows())
        {
            return;
        }

        var mutexName = $@"Local\MyPowerTools.Shell.Tests.{Guid.NewGuid():N}";
        using var first = RemoteNotificationShellInstanceLock.Acquire(mutexName);
        var secondAcquired = true;
        Exception? contenderError = null;
        var contender = new Thread(() =>
        {
            try
            {
                using var second = RemoteNotificationShellInstanceLock.Acquire(mutexName);
                secondAcquired = second.Acquired;
            }
            catch (Exception exception)
            {
                contenderError = exception;
            }
        });
        contender.Start();

        Assert.True(first.Acquired);
        Assert.True(contender.Join(TimeSpan.FromSeconds(5)));
        Assert.Null(contenderError);
        Assert.False(secondAcquired);
    }

    [Fact]
    public void Independent_detail_window_resolves_server_and_fallback_message_ids()
    {
        var withServerId = Message("server-detail", "[build] server", DateTimeOffset.UtcNow);
        var fallbackOnly = Message("", "[build] fallback", DateTimeOffset.UtcNow.AddSeconds(-1));
        var fallbackId = RemoteNotificationsLegacyStore.FallbackId(fallbackOnly);

        Assert.True(RemoteNotificationDetailWindowService.TryFindRecord(
            [withServerId, fallbackOnly],
            withServerId.Id,
            out var serverRecord));
        Assert.Equal(withServerId, serverRecord);
        Assert.True(RemoteNotificationDetailWindowService.TryFindRecord(
            [withServerId, fallbackOnly],
            fallbackId,
            out var fallbackRecord));
        Assert.Equal(fallbackOnly, fallbackRecord);
    }

    [Fact]
    public void Remote_notifications_implementation_tracks_the_original_source_contract()
    {
        var integrationRoot = Path.Combine(
            Root,
            "tools",
            "remote-notifications",
            "current-integration",
            "src",
            "MyPowerTools.Shell.Avalonia");
        var store = File.ReadAllText(Path.Combine(
            integrationRoot, "Services", "RemoteNotificationsLegacyStore.cs"));
        var viewModel = File.ReadAllText(Path.Combine(
            integrationRoot, "ViewModels", "RemoteNotificationsViewModels.cs"));
        var toast = File.ReadAllText(Path.Combine(
            integrationRoot, "Services", "RemoteNotificationToastPublisher.cs"));
        var activation = File.ReadAllText(Path.Combine(
            integrationRoot, "Services", "RemoteNotificationActivationService.cs"));
        var detailWindows = File.ReadAllText(Path.Combine(
            integrationRoot, "Services", "RemoteNotificationDetailWindowService.cs"));
        var notificationView = File.ReadAllText(Path.Combine(
            integrationRoot, "Views", "RemoteNotificationsView.axaml.cs"));
        var detailView = File.ReadAllText(Path.Combine(
            integrationRoot, "Views", "RemoteNotificationDetailWindow.axaml"));
        var toastPlatform = File.ReadAllText(Path.Combine(
            integrationRoot, "Services", "WindowsRemoteNotificationToastPlatform.cs"));
        var program = File.ReadAllText(Path.Combine(Root, "src", "MyPowerTools.Shell.Avalonia", "Program.cs"));
        var app = File.ReadAllText(Path.Combine(Root, "src", "MyPowerTools.Shell.Avalonia", "App.cs"));

        Assert.Contains("MaximumMessages = 500", store, StringComparison.Ordinal);
        Assert.Contains("MaximumRecentHashes = 200", store, StringComparison.Ordinal);
        Assert.Contains("MaximumSeenMessageIds = 5000", store, StringComparison.Ordinal);
        Assert.Contains("FallbackId", viewModel, StringComparison.Ordinal);
        Assert.Contains("PublishAsync", viewModel, StringComparison.Ordinal);
        Assert.Contains("persistent ? \"reminder\"", toast, StringComparison.Ordinal);
        Assert.Contains("mypowertools://remote-notification?id=", toast, StringComparison.Ordinal);
        Assert.Contains("MyPowerTools.RemoteNotificationActivation", activation, StringComparison.Ordinal);
        Assert.Contains("_detailWindows.TryOpen(request.MessageId)", activation, StringComparison.Ordinal);
        Assert.Contains("detail.Show();", detailWindows, StringComparison.Ordinal);
        Assert.Contains("_detailWindows.Open(message)", notificationView, StringComparison.Ordinal);
        Assert.DoesNotContain("ShowDialog(owner)", notificationView, StringComparison.Ordinal);
        Assert.DoesNotContain("detail.Show(owner)", notificationView, StringComparison.Ordinal);
        Assert.Contains("WindowStartupLocation=\"CenterScreen\"", detailView, StringComparison.Ordinal);
        Assert.Contains("WindowsToastAbi.Show", toastPlatform, StringComparison.Ordinal);
        Assert.Contains("SetCurrentProcessExplicitAppUserModelID", toastPlatform, StringComparison.Ordinal);
        Assert.Contains("TryForwardToRunningShellAsync", program, StringComparison.Ordinal);
        Assert.Contains("RemoteNotificationShellInstanceLock.Acquire", program, StringComparison.Ordinal);
        Assert.True(
            program.IndexOf("--smoke", StringComparison.Ordinal) <
            program.IndexOf("RemoteNotificationShellInstanceLock.Acquire", StringComparison.Ordinal));
        Assert.Contains("RemoteNotificationActivationCoordinator", app, StringComparison.Ordinal);
        Assert.Contains("detailOnlyStartup", app, StringComparison.Ordinal);
        Assert.Contains("mainWindow is null ? null : mainWindow.FocusCommandPaletteAsync", app, StringComparison.Ordinal);

        var originalRoot = Path.Combine(Root, "..", "androidtools");
        if (!Directory.Exists(originalRoot))
        {
            return;
        }

        var page1 = File.ReadAllText(Path.Combine(originalRoot, "powertool", "page1.py"));
        var qt = File.ReadAllText(Path.Combine(originalRoot, "qt.py"));
        var windowsNotifications = File.ReadAllText(Path.Combine(originalRoot, "powertool", "windows_notifications.py"));
        Assert.Contains("MAX_SEEN_MESSAGE_IDS = 5000", page1, StringComparison.Ordinal);
        Assert.Contains("accepted_msg_signal", qt, StringComparison.Ordinal);
        Assert.Contains("toast_launch_for_message", qt, StringComparison.Ordinal);
        Assert.Contains("scenario=\"{html.escape(scenario", windowsNotifications, StringComparison.Ordinal);
    }

    [Fact]
    public async Task Sync_errors_use_a_compact_summary_with_expandable_technical_details_and_retry()
    {
        const string traceback = "Traceback (most recent call last):\n  File \"pull.py\", line 4\nHttpRequestException: Connection reset by peer";
        var poller = new FakePoller(
            new RemoteNotificationPullResult("error", [], traceback),
            new RemoteNotificationPullResult("idle", [], ""));
        var viewModel = new RemoteNotificationsViewModel(
            new RemoteNotificationsSnapshot([], [], null, false),
            new FakeStore(),
            poller);

        await viewModel.PollAsync();

        Assert.True(viewModel.HasSyncError);
        Assert.Equal("Notifications could not sync", viewModel.ErrorTitle);
        Assert.Contains("Connection reset by peer", viewModel.ErrorSummary, StringComparison.OrdinalIgnoreCase);
        Assert.DoesNotContain("Traceback", viewModel.ErrorSummary, StringComparison.OrdinalIgnoreCase);
        Assert.Contains("Traceback", viewModel.TechnicalErrorDetails, StringComparison.Ordinal);
        Assert.True(viewModel.RetryCommand.CanExecute(null));

        viewModel.ToggleErrorDetailsCommand.Execute(null);
        Assert.True(viewModel.IsErrorDetailsExpanded);
        Assert.Equal("Hide details", viewModel.ErrorDetailsActionLabel);

        await viewModel.RetryAsync();

        Assert.Equal(2, poller.CallCount);
        Assert.Equal("idle", viewModel.ConnectionState);
        Assert.False(viewModel.HasSyncError);
        Assert.False(viewModel.IsErrorDetailsExpanded);
        Assert.Equal("No new messages", viewModel.SyncResultText);
    }

    [Fact]
    public void Persistent_and_clear_actions_write_the_legacy_Page1_store_contract()
    {
        var store = new FakeStore();
        var viewModel = new RemoteNotificationsViewModel(
            new RemoteNotificationsSnapshot(
                [Message("one", "[alpha] one", DateTimeOffset.UtcNow)],
                ["alpha"],
                null,
                false),
            store,
            new FakePoller());

        viewModel.PersistentWindowsToasts = true;
        viewModel.ClearMessages();

        Assert.True(store.SavedPersistent);
        Assert.True(store.WasCleared);
        Assert.Empty(viewModel.Messages);
        Assert.Equal("Waiting for notifications…", viewModel.EmptyOverlayText);
    }

    private static RemoteNotificationRecord Message(string id, string body, DateTimeOffset timestamp)
    {
        return new RemoteNotificationRecord(
            id,
            "default",
            body,
            "codex",
            timestamp.ToUniversalTime().ToString("O"));
    }

    private sealed class FakeStore : IRemoteNotificationsStore
    {
        private readonly RemoteNotificationsSnapshot _snapshot;

        public FakeStore(RemoteNotificationsSnapshot? snapshot = null)
        {
            _snapshot = snapshot ?? new RemoteNotificationsSnapshot([], [], null, false);
        }

        public string? SavedFilter { get; private set; }
        public IReadOnlyList<string> SavedLabels { get; private set; } = [];
        public IReadOnlyList<RemoteNotificationRecord> SavedMessages { get; private set; } = [];
        public bool SavedPersistent { get; private set; }
        public bool WasCleared { get; private set; }
        public IReadOnlyList<string> SavedSeenIds { get; private set; } = [];

        public RemoteNotificationsSnapshot Load() => _snapshot;

        public void SaveMessages(IReadOnlyList<RemoteNotificationRecord> messagesOldestFirst)
        {
            SavedMessages = messagesOldestFirst;
        }

        public void SaveFilter(string? label)
        {
            SavedFilter = label;
        }

        public void SaveKnownLabels(IReadOnlyList<string> labels)
        {
            SavedLabels = labels.ToArray();
        }

        public void SavePersistentWindowsToasts(bool enabled)
        {
            SavedPersistent = enabled;
        }

        public void SaveSeenMessageIds(IReadOnlyList<string> messageIdsOldestFirst)
        {
            SavedSeenIds = messageIdsOldestFirst.ToArray();
        }

        public void ClearMessages()
        {
            WasCleared = true;
        }
    }

    private sealed class FakeSettingsStore(RemoteNotificationSettings settings) : IRemoteNotificationSettingsStore
    {
        private RemoteNotificationSettings _settings = settings;

        public string SettingsPath => "memory://remote-notifications/settings.json";
        public int SaveCount { get; private set; }

        public RemoteNotificationSettings Load() => _settings;

        public void Save(RemoteNotificationSettings value)
        {
            _settings = value;
            SaveCount++;
        }
    }

    private sealed class FakeToastPublisher : IRemoteNotificationToastPublisher
    {
        public List<(string MessageId, bool Persistent)> Published { get; } = [];

        public Task<RemoteNotificationToastPublishResult> PublishAsync(
            RemoteNotificationRecord notification,
            string messageId,
            bool persistent,
            CancellationToken cancellationToken = default)
        {
            cancellationToken.ThrowIfCancellationRequested();
            Published.Add((messageId, persistent));
            return Task.FromResult(new RemoteNotificationToastPublishResult(true, "shown"));
        }
    }

    private sealed class RecordingToastPlatform : IRemoteNotificationToastPlatform
    {
        public List<RemoteNotificationToastEnvelope> Envelopes { get; } = [];

        public RemoteNotificationToastPublishResult Show(RemoteNotificationToastEnvelope envelope)
        {
            Envelopes.Add(envelope);
            return new RemoteNotificationToastPublishResult(true, "shown");
        }

        public bool ClearHistory() => true;
    }

    private sealed class FakePoller : IRemoteNotificationPoller
    {
        private readonly Queue<RemoteNotificationPullResult> _results;
        private RemoteNotificationPullResult _lastResult;

        public FakePoller(params RemoteNotificationPullResult[] results)
        {
            _results = new Queue<RemoteNotificationPullResult>(results);
            _lastResult = results.LastOrDefault() ?? new RemoteNotificationPullResult("idle", [], "");
        }

        public int CallCount { get; private set; }

        public Task<RemoteNotificationPullResult> PullAsync(string since, CancellationToken cancellationToken = default)
        {
            CallCount++;
            if (_results.Count > 0)
            {
                _lastResult = _results.Dequeue();
            }

            return Task.FromResult(_lastResult);
        }
    }

    private static string FindRepositoryRoot()
    {
        var current = new DirectoryInfo(AppContext.BaseDirectory);
        while (current is not null)
        {
            if (File.Exists(Path.Combine(current.FullName, "MyPowerTools.slnx")))
            {
                return current.FullName;
            }

            current = current.Parent;
        }

        throw new DirectoryNotFoundException("MyPowerTools repository root was not found.");
    }

    private static async Task WaitUntilAsync(Func<bool> condition)
    {
        var deadline = DateTimeOffset.UtcNow.AddSeconds(3);
        while (!condition())
        {
            if (DateTimeOffset.UtcNow >= deadline)
            {
                throw new TimeoutException("The asynchronous settings operation did not complete.");
            }

            await Task.Delay(10);
        }
    }
}
