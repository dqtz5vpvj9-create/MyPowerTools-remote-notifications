using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Globalization;
using Microsoft.Win32;
using MyPowerTools.RemoteNotifications.Configuration;

namespace MyPowerTools.Shell.Avalonia.Services;

#if ANDROID_TOOLS_ADAPTER
internal
#else
public
#endif
sealed record RemoteNotificationRecord(
    string Id,
    string Channel,
    string Message,
    string Icon,
    string Timestamp,
    string ServerTimestamp = "",
    string SessionId = "",
    string SessionName = "",
    string SourceClient = "",
    string SourceEventId = "",
    string SourceMessageId = "",
    string ContentKind = "",
    string StopReason = "");

#if ANDROID_TOOLS_ADAPTER
internal
#else
public
#endif
sealed record RemoteNotificationsSnapshot(
    IReadOnlyList<RemoteNotificationRecord> MessagesOldestFirst,
    IReadOnlyList<string> KnownLabels,
    string? FilterLabel,
    bool PersistentWindowsToasts,
    IReadOnlyList<string>? SeenMessageIds = null);

#if ANDROID_TOOLS_ADAPTER
internal
#else
public
#endif
sealed record RemoteNotificationPullResult(
    string State,
    IReadOnlyList<RemoteNotificationRecord> Notifications,
    string Error)
{
    public bool IsSuccess => State is "ok" or "idle";
}

#if ANDROID_TOOLS_ADAPTER
internal
#else
public
#endif
interface IRemoteNotificationsStore
{
    RemoteNotificationsSnapshot Load();
    void SaveMessages(IReadOnlyList<RemoteNotificationRecord> messagesOldestFirst);
    void SaveFilter(string? label);
    void SaveKnownLabels(IReadOnlyList<string> labels);
    void SavePersistentWindowsToasts(bool enabled);
    void SaveSeenMessageIds(IReadOnlyList<string> messageIdsOldestFirst)
    {
    }
    void ClearMessages();
}

#if ANDROID_TOOLS_ADAPTER
internal
#else
public
#endif
interface IRemoteNotificationPoller
{
    Task<RemoteNotificationPullResult> PullAsync(string since, CancellationToken cancellationToken = default);
}

#if ANDROID_TOOLS_ADAPTER
internal
#else
public
#endif
sealed class RemoteNotificationNoopPoller : IRemoteNotificationPoller
{
    public Task<RemoteNotificationPullResult> PullAsync(string since, CancellationToken cancellationToken = default)
    {
        cancellationToken.ThrowIfCancellationRequested();
        return Task.FromResult(new RemoteNotificationPullResult("idle", [], ""));
    }
}

#if ANDROID_TOOLS_ADAPTER
internal
#else
public
#endif
sealed class RemoteNotificationsLegacyStore : IRemoteNotificationsStore
{
    public const string DefaultChannel = "default";
    public const string FilterAll = "__all__";
    public const string TaskCompletedText = "Task completed";
    public const int MaximumMessages = 500;
    public const int MaximumRecentHashes = 200;
    public const int MaximumSeenMessageIds = 5000;
    public const int ClaudeDuplicateWindowMinutes = 15;

    private const string RegistryPath = @"Software\AndroidTools\Page1";
    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNameCaseInsensitive = true
    };

    private readonly IRemoteNotificationSettingsStore _settingsStore;

    public RemoteNotificationsLegacyStore(IRemoteNotificationSettingsStore? settingsStore = null)
    {
        _settingsStore = settingsStore ?? new RemoteNotificationSettingsStore();
    }

    public RemoteNotificationsSnapshot Load()
    {
        var productSettings = _settingsStore.Load();
        if (!OperatingSystem.IsWindows())
        {
            return new RemoteNotificationsSnapshot([], [], null, productSettings.KeepWindowsBanners, []);
        }

        using var key = Registry.CurrentUser.OpenSubKey(RegistryPath, writable: true);
        if (key is null)
        {
            return new RemoteNotificationsSnapshot([], [], null, productSettings.KeepWindowsBanners, []);
        }

        var originalMessages = ParseMessages(ReadText(key.GetValue("messages")));
        var messages = MergeTaskCompletedRecords(CollapseClaudeStopDuplicates(originalMessages));
        if (!messages.SequenceEqual(originalMessages))
        {
            key.SetValue(
                "messages",
                JsonSerializer.Serialize(messages, JsonOptions),
                RegistryValueKind.String);
        }
        var knownLabels = ReadText(key.GetValue("known_labels"))
            .Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
            .Distinct(StringComparer.Ordinal)
            .ToList();

        // Page1 replays the oldest-first history and moves the latest active
        // session to the front. Reproduce that ordering even when known_labels
        // came from an older build.
        foreach (var message in messages)
        {
            var label = ExtractLabel(message.Message);
            knownLabels.Remove(label);
            knownLabels.Insert(0, label);
        }

        var savedFilter = ReadText(key.GetValue("filter_label"));
        var filter = string.IsNullOrWhiteSpace(savedFilter) ||
                     string.Equals(savedFilter, FilterAll, StringComparison.Ordinal)
            ? null
            : savedFilter;
        var legacyPersistent = ParseBoolean(key.GetValue("windows_toast_reminder"));
        if (!File.Exists(_settingsStore.SettingsPath) && legacyPersistent)
        {
            productSettings = productSettings with { KeepWindowsBanners = true };
            _settingsStore.Save(productSettings);
        }
        var persistent = productSettings.KeepWindowsBanners;
        var seen = ParseStringList(ReadText(key.GetValue("seen_message_ids")))
            .TakeLast(MaximumSeenMessageIds)
            .ToList();
        foreach (var recent in ParseStringList(ReadText(key.GetValue("recent_hashes"))).TakeLast(MaximumRecentHashes))
        {
            Remember(seen, recent, MaximumSeenMessageIds);
        }
        foreach (var message in messages)
        {
            Remember(seen, StableId(message), MaximumSeenMessageIds);
            Remember(seen, FallbackId(message), MaximumSeenMessageIds);
        }

        return new RemoteNotificationsSnapshot(messages, knownLabels, filter, persistent, seen);
    }

    public void SaveMessages(IReadOnlyList<RemoteNotificationRecord> messagesOldestFirst)
    {
        if (!OperatingSystem.IsWindows())
        {
            return;
        }

        var collapsed = MergeTaskCompletedRecords(CollapseClaudeStopDuplicates(messagesOldestFirst));
        var retained = collapsed.Count <= MaximumMessages
            ? collapsed
            : collapsed.Skip(collapsed.Count - MaximumMessages).ToArray();
        var payload = retained.Select(message => new PersistedNotification
        {
            Id = message.Id,
            Channel = message.Channel,
            Message = message.Message,
            Icon = message.Icon,
            Timestamp = message.Timestamp,
            ServerTimestamp = message.ServerTimestamp,
            SessionId = message.SessionId,
            SessionName = message.SessionName,
            SourceClient = message.SourceClient,
            SourceEventId = message.SourceEventId,
            SourceMessageId = message.SourceMessageId,
            ContentKind = message.ContentKind,
            StopReason = message.StopReason
        }).ToArray();

        using var key = Registry.CurrentUser.CreateSubKey(RegistryPath, writable: true);
        key.SetValue("messages", JsonSerializer.Serialize(payload, JsonOptions), RegistryValueKind.String);

        var recent = ParseStringList(ReadText(key.GetValue("recent_hashes")));
        var seen = ParseStringList(ReadText(key.GetValue("seen_message_ids")));
        foreach (var message in retained)
        {
            Remember(recent, StableId(message), MaximumRecentHashes);
            Remember(seen, StableId(message), MaximumSeenMessageIds);
            Remember(recent, FallbackId(message), MaximumRecentHashes);
            Remember(seen, FallbackId(message), MaximumSeenMessageIds);
        }

        key.SetValue("recent_hashes", JsonSerializer.Serialize(recent), RegistryValueKind.String);
        key.SetValue("seen_message_ids", JsonSerializer.Serialize(seen), RegistryValueKind.String);
    }

    public void SaveFilter(string? label)
    {
        WriteString("filter_label", string.IsNullOrWhiteSpace(label) ? FilterAll : label);
    }

    public void SaveKnownLabels(IReadOnlyList<string> labels)
    {
        WriteString("known_labels", string.Join(',', labels));
    }

    public void SavePersistentWindowsToasts(bool enabled)
    {
        var settings = _settingsStore.Load() with { KeepWindowsBanners = enabled };
        _settingsStore.Save(settings);
        WriteString("windows_toast_reminder", enabled ? "true" : "false");
    }

    public void SaveSeenMessageIds(IReadOnlyList<string> messageIdsOldestFirst)
    {
        if (!OperatingSystem.IsWindows())
        {
            return;
        }

        var seen = new List<string>();
        foreach (var messageId in messageIdsOldestFirst.TakeLast(MaximumSeenMessageIds))
        {
            Remember(seen, messageId, MaximumSeenMessageIds);
        }

        using var key = Registry.CurrentUser.CreateSubKey(RegistryPath, writable: true);
        key.SetValue("seen_message_ids", JsonSerializer.Serialize(seen), RegistryValueKind.String);
        key.SetValue(
            "recent_hashes",
            JsonSerializer.Serialize(seen.TakeLast(MaximumRecentHashes)),
            RegistryValueKind.String);
    }

    public void ClearMessages()
    {
        WriteString("messages", "[]");
    }

    public static IReadOnlyList<RemoteNotificationRecord> CollapseClaudeStopDuplicates(
        IReadOnlyList<RemoteNotificationRecord> messagesOldestFirst)
    {
        if (messagesOldestFirst.Count < 2)
        {
            return messagesOldestFirst;
        }

        var kept = new List<RemoteNotificationRecord>(messagesOldestFirst.Count);
        var recent = new Dictionary<string, (int Index, DateTimeOffset Time)>(StringComparer.Ordinal);
        foreach (var message in messagesOldestFirst)
        {
            if (!TryClaudeDuplicateKey(message, out var key) ||
                !DateTimeOffset.TryParse(
                    string.IsNullOrWhiteSpace(message.ServerTimestamp) ? message.Timestamp : message.ServerTimestamp,
                    CultureInfo.InvariantCulture,
                    DateTimeStyles.AssumeUniversal | DateTimeStyles.AdjustToUniversal,
                    out var timestamp))
            {
                kept.Add(message);
                continue;
            }

            if (recent.TryGetValue(key, out var previous) &&
                timestamp >= previous.Time &&
                timestamp - previous.Time <= TimeSpan.FromMinutes(ClaudeDuplicateWindowMinutes))
            {
                kept[previous.Index] = message;
                recent[key] = (previous.Index, timestamp);
            }
            else
            {
                recent[key] = (kept.Count, timestamp);
                kept.Add(message);
            }
        }

        return kept
            .OrderBy(message => DateTimeOffset.TryParse(
                string.IsNullOrWhiteSpace(message.ServerTimestamp) ? message.Timestamp : message.ServerTimestamp,
                CultureInfo.InvariantCulture,
                DateTimeStyles.AssumeUniversal | DateTimeStyles.AdjustToUniversal,
                out var timestamp) ? timestamp : DateTimeOffset.MinValue)
            .ToArray();
    }

    /// <summary>
    /// Completion-only Claude/Cursor hook records still produce one banner,
    /// yet they carry no conversational content for the inbox. Attach that
    /// status to the nearest reply in the same session, preferring an earlier
    /// reply, and keep the event out of the card list. Seen IDs are tracked by
    /// the caller, so a merged event remains one-shot for banner delivery.
    /// </summary>
    public static IReadOnlyList<RemoteNotificationRecord> MergeTaskCompletedRecords(
        IReadOnlyList<RemoteNotificationRecord> messagesOldestFirst)
    {
        if (messagesOldestFirst.Count == 0)
        {
            return messagesOldestFirst;
        }

        var indexed = messagesOldestFirst
            .Select((record, index) => (Record: record, Index: index))
            .ToArray();
        var replies = indexed
            .Where(entry => !IsTaskCompletedRecord(entry.Record))
            .ToArray();
        var completionForReply = new Dictionary<int, RemoteNotificationRecord>();

        foreach (var entry in indexed.Where(entry => IsTaskCompletedRecord(entry.Record)))
        {
            var sessionId = entry.Record.SessionId?.Trim() ?? "";
            if (sessionId.Length == 0)
            {
                // The event still reaches the banner path, but it has no
                // stable conversation owner for inbox attachment.
                continue;
            }

            var target = replies.LastOrDefault(candidate =>
                candidate.Index < entry.Index &&
                string.Equals(candidate.Record.SessionId?.Trim(), sessionId, StringComparison.Ordinal));
            if (target.Record is null)
            {
                target = replies.FirstOrDefault(candidate =>
                    candidate.Index > entry.Index &&
                    string.Equals(candidate.Record.SessionId?.Trim(), sessionId, StringComparison.Ordinal));
            }
            if (target.Record is not null)
            {
                completionForReply[target.Index] = entry.Record;
            }
        }

        return replies
            .Select(entry =>
            {
                if (!completionForReply.TryGetValue(entry.Index, out var completion))
                {
                    return entry.Record;
                }

                return entry.Record with
                {
                    Message = AppendTaskCompleted(entry.Record.Message),
                    Timestamp = string.IsNullOrWhiteSpace(completion.Timestamp)
                        ? entry.Record.Timestamp
                        : completion.Timestamp,
                    ServerTimestamp = string.IsNullOrWhiteSpace(completion.ServerTimestamp)
                        ? entry.Record.ServerTimestamp
                        : completion.ServerTimestamp
                };
            })
            .ToArray();
    }

    public static bool IsTaskCompletedRecord(RemoteNotificationRecord message)
    {
        var source = message.SourceClient?.Trim() ?? "";
        var icon = message.Icon?.Trim() ?? "";
        var isClaudeFamily = source.Equals("claude", StringComparison.OrdinalIgnoreCase) ||
                             source.Equals("cursor", StringComparison.OrdinalIgnoreCase) ||
                             icon.Equals("claude", StringComparison.OrdinalIgnoreCase) ||
                             icon.Equals("cursor", StringComparison.OrdinalIgnoreCase);
        if (!isClaudeFamily)
        {
            return false;
        }

        var body = StripLeadingQuotedRequest(message.Message ?? "").Trim();
        var label = ExtractLabel(body);
        if (!string.Equals(label, "(unlabeled)", StringComparison.Ordinal) &&
            body.StartsWith($"[{label}]", StringComparison.Ordinal))
        {
            body = body[(label.Length + 2)..].TrimStart();
        }

        return body.Equals(TaskCompletedText, StringComparison.OrdinalIgnoreCase) ||
               body.Equals($"{TaskCompletedText}.", StringComparison.OrdinalIgnoreCase);
    }

    private static string AppendTaskCompleted(string message)
    {
        var trimmed = (message ?? "").TrimEnd();
        if (trimmed.Length == 0 ||
            trimmed.EndsWith(TaskCompletedText, StringComparison.OrdinalIgnoreCase))
        {
            return trimmed;
        }

        return $"{trimmed}\n\n{TaskCompletedText}";
    }

    private static bool TryClaudeDuplicateKey(RemoteNotificationRecord message, out string key)
    {
        var source = message.SourceClient.Trim().ToLowerInvariant();
        var icon = message.Icon.Trim().ToLowerInvariant();
        if (source != "claude" && icon != "claude")
        {
            key = "";
            return false;
        }
        var body = StripLeadingQuotedRequest(message.Message ?? "").Trim();
        var label = ExtractLabel(body);
        if (label != "(unlabeled)" && body.StartsWith($"[{label}]", StringComparison.Ordinal))
        {
            body = body[(label.Length + 2)..].TrimStart();
        }
        if (string.IsNullOrWhiteSpace(body))
        {
            key = "";
            return false;
        }
        key = $"{message.SessionId.Trim()}\0{body}";
        return true;
    }

    public static string ExtractLabel(string message)
    {
        if (!string.IsNullOrEmpty(message) && message[0] == '[')
        {
            var closingBracket = message.IndexOf(']');
            if (closingBracket > 1)
            {
                return message[1..closingBracket];
            }
        }

        return "(unlabeled)";
    }

    /// <summary>
    /// Inbox messages may quote the user request as a leading <c>&gt; ...</c>
    /// block. OS banners should show the <c>[label] result</c> that follows,
    /// not the question.
    /// </summary>
    public static string StripLeadingQuotedRequest(string message)
    {
        if (string.IsNullOrEmpty(message))
        {
            return message ?? "";
        }

        var lines = message.Replace("\r\n", "\n", StringComparison.Ordinal).Replace('\r', '\n').Split('\n');
        var index = 0;
        while (index < lines.Length && IsMarkdownQuoteLine(lines[index]))
        {
            index++;
        }

        if (index == 0)
        {
            return message;
        }

        while (index < lines.Length && string.IsNullOrWhiteSpace(lines[index]))
        {
            index++;
        }

        var remainder = string.Join('\n', lines.Skip(index));
        return string.Equals(ExtractLabel(remainder), "(unlabeled)", StringComparison.Ordinal)
            ? message
            : remainder;
    }

    private static bool IsMarkdownQuoteLine(string line)
    {
        var trimmed = line.TrimStart();
        return trimmed.StartsWith('>');
    }

    public static string StableId(RemoteNotificationRecord message)
    {
        if (!string.IsNullOrWhiteSpace(message.Id))
        {
            return message.Id;
        }

        return FallbackId(message);
    }

    public static string FallbackId(RemoteNotificationRecord message)
    {
        var channel = string.IsNullOrWhiteSpace(message.Channel) ? DefaultChannel : message.Channel;
        var icon = string.IsNullOrWhiteSpace(message.Icon) ? "info" : message.Icon;
        var payload = string.Join('\0', channel, message.Message ?? "", icon, message.Timestamp ?? "");
        var bytes = SHA1.HashData(Encoding.UTF8.GetBytes(payload));
        return $"n{Convert.ToHexString(bytes)[..24].ToLowerInvariant()}";
    }

    private static IReadOnlyList<RemoteNotificationRecord> ParseMessages(string json)
    {
        if (string.IsNullOrWhiteSpace(json))
        {
            return [];
        }

        try
        {
            var parsed = JsonSerializer.Deserialize<List<PersistedNotification>>(json, JsonOptions) ?? [];
            return parsed
                .Where(message => !string.IsNullOrWhiteSpace(message.Message))
                .Select(message => new RemoteNotificationRecord(
                    message.Id ?? "",
                    string.IsNullOrWhiteSpace(message.Channel) ? DefaultChannel : message.Channel,
                    message.Message ?? "",
                    string.IsNullOrWhiteSpace(message.Icon) ? "info" : message.Icon,
                    message.Timestamp ?? "",
                    message.ServerTimestamp ?? "",
                    message.SessionId ?? "",
                    message.SessionName ?? "",
                    message.SourceClient ?? "",
                    message.SourceEventId ?? "",
                    message.SourceMessageId ?? "",
                    message.ContentKind ?? "",
                    message.StopReason ?? ""))
                .TakeLast(MaximumMessages)
                .ToArray();
        }
        catch (JsonException)
        {
            return [];
        }
    }

    private static List<string> ParseStringList(string json)
    {
        try
        {
            return JsonSerializer.Deserialize<List<string>>(json, JsonOptions) ?? [];
        }
        catch (JsonException)
        {
            return [];
        }
    }

    private static void Remember(List<string> values, string value, int maximum)
    {
        if (string.IsNullOrWhiteSpace(value) || values.Contains(value, StringComparer.Ordinal))
        {
            return;
        }

        values.Add(value);
        if (values.Count > maximum)
        {
            values.RemoveRange(0, values.Count - maximum);
        }
    }

    private static string ReadText(object? value)
    {
        return value switch
        {
            null => "",
            byte[] bytes => Encoding.UTF8.GetString(bytes).TrimEnd('\0'),
            _ => Convert.ToString(value, System.Globalization.CultureInfo.InvariantCulture) ?? ""
        };
    }

    private static bool ParseBoolean(object? value)
    {
        return ReadText(value).Trim().ToLowerInvariant() is "1" or "true" or "yes" or "on";
    }

    private static void WriteString(string name, string value)
    {
        if (!OperatingSystem.IsWindows())
        {
            return;
        }

        using var key = Registry.CurrentUser.CreateSubKey(RegistryPath, writable: true);
        key.SetValue(name, value, RegistryValueKind.String);
    }

    private sealed class PersistedNotification
    {
        [JsonPropertyName("id")]
        public string? Id { get; init; }

        [JsonPropertyName("channel")]
        public string? Channel { get; init; }

        [JsonPropertyName("message")]
        public string? Message { get; init; }

        [JsonPropertyName("icon")]
        public string? Icon { get; init; }

        [JsonPropertyName("timestamp")]
        public string? Timestamp { get; init; }

        [JsonPropertyName("server_timestamp")]
        public string? ServerTimestamp { get; init; }

        [JsonPropertyName("session_id")]
        public string? SessionId { get; init; }

        [JsonPropertyName("session_name")]
        public string? SessionName { get; init; }

        [JsonPropertyName("source_client")]
        public string? SourceClient { get; init; }

        [JsonPropertyName("source_event_id")]
        public string? SourceEventId { get; init; }

        [JsonPropertyName("source_message_id")]
        public string? SourceMessageId { get; init; }

        [JsonPropertyName("content_kind")]
        public string? ContentKind { get; init; }

        [JsonPropertyName("stop_reason")]
        public string? StopReason { get; init; }
    }
}
