using MyPowerTools.RemoteNotifications.Configuration;
using RemoteNotifications.Surface.Services;

namespace RemoteNotifications.Configuration.Tests;

public sealed class RemoteNotificationsLegacyStoreTests
{
    [Fact]
    public void Seen_ids_survive_a_record_that_history_merged_away()
    {
        using var fixture = TestDirectory.Create();
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

        var store = NewStore(fixture);
        store.SaveSeenMessageIds([reply.Id, completed.Id]);
        store.SaveMessages([reply, completed]);

        var reloaded = NewStore(fixture).Load();

        Assert.DoesNotContain(reloaded.MessagesOldestFirst, message => message.Id == completed.Id);
        Assert.Contains(completed.Id, reloaded.SeenMessageIds!);
        Assert.Contains(reply.Id, reloaded.SeenMessageIds!);
    }

    [Fact]
    public void Seen_ids_survive_a_record_pushed_past_the_history_cap()
    {
        using var fixture = TestDirectory.Create();
        var messages = Enumerable
            .Range(0, RemoteNotificationsLegacyStore.MaximumMessages + 1)
            .Select(index => Message(
                $"message-{index}",
                $"[build] step {index}",
                new DateTimeOffset(2026, 8, 18, 0, 0, 0, TimeSpan.Zero).AddSeconds(index)))
            .ToArray();

        var store = NewStore(fixture);
        store.SaveSeenMessageIds(messages.Select(message => message.Id).ToArray());
        store.SaveMessages(messages);

        var reloaded = NewStore(fixture).Load();

        Assert.Equal(
            RemoteNotificationsLegacyStore.MaximumMessages,
            reloaded.MessagesOldestFirst.Count);
        Assert.DoesNotContain(reloaded.MessagesOldestFirst, message => message.Id == "message-0");
        Assert.Contains("message-0", reloaded.SeenMessageIds!);
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
            "> 亲自巡查（不派子代理）：检查全部在跑实验 loop 日志。\n>\n[autodroid-52] 巡查小结。",
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

    private static RemoteNotificationsLegacyStore NewStore(TestDirectory fixture)
    {
        var settingsStore = new RemoteNotificationSettingsStore(
            Path.Combine(fixture.Path, "settings.json"));
        return new RemoteNotificationsLegacyStore(settingsStore, fixture.Path);
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
}
