using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Globalization;
using Microsoft.Win32;
using MyPowerTools.RemoteNotifications.Configuration;

namespace RemoteNotifications.Surface.Services;

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
    public const string ClaudeTaskLabel = "Claude Task";
    public const int MaximumMessages = 500;
    public const int MaximumRecentHashes = 200;
    public const int MaximumSeenMessageIds = 5000;
    public const int ClaudeDuplicateWindowMinutes = 15;

    /** Fingerprints of a Claude Stop notification whose trigger was an
     *  auto-injected user turn (task-notification / isMeta / other
     *  synthetic wrapper). Matching runs on the body AFTER the leading
     *  quoted user request, and only for short system-shaped payloads — a
     *  long user report may legitimately mention these words in prose and
     *  must never be routed to the Claude Task page. Legacy items that
     *  predate the sender-side label routing move off the per-project chip;
     *  items the v1 matcher wrongly relabeled are repaired back. */
    private static readonly string[] ClaudeTaskFingerprints =
    [
        "<task-notification>",
        "Your previous response had no visible output",
        "<command-message>",
        "<command-name>",
        "<local-command-",
        "Stop hook feedback:",
        "[Request interrupted",
        "This session is being continued",
    ];

    internal static RemoteNotificationRecord RewriteAsClaudeTaskIfHistoricalTaskTriggered(
        RemoteNotificationRecord message)
    {
        var body = message.Message ?? "";
        var stripped = StripLeadingQuotedRequest(body);
        var existingLabel = ExtractLabel(stripped);
        if (string.Equals(existingLabel, ClaudeTaskLabel, StringComparison.Ordinal))
        {
            if (!IsTaskTriggerBody(stripped))
            {
                // v1 substring matcher routed a long real payload here.
                // Drop the synthetic label so the message is visible again.
                return message with { Message = RemoveLeadingLabel(stripped) };
            }
            return message;
        }
        if (!IsTaskTriggerBody(stripped))
        {
            return message;
        }
        var relabelled = ReplaceLeadingLabel(stripped, ClaudeTaskLabel);
        return message with { Message = relabelled };
    }

    /** Synthetic Claude Stops are short system-injected tips. A long payload
     *  that merely mentions a marker is a real user/assistant message. */
    private static bool IsTaskTriggerBody(string body)
    {
        if (body.Length > 2000)
        {
            return false;
        }
        return ClaudeTaskFingerprints.Any(fp => body.Contains(fp, StringComparison.Ordinal));
    }

    private static string RemoveLeadingLabel(string body)
    {
        if (body.StartsWith("[", StringComparison.Ordinal))
        {
            var close = body.IndexOf(']');
            if (close > 0)
            {
                return body[(close + 1)..].TrimStart();
            }
        }
        return body;
    }

    private static string ReplaceLeadingLabel(string body, string newLabel)
    {
        if (body.StartsWith("[", StringComparison.Ordinal))
        {
            var close = body.IndexOf(']');
            if (close > 0)
            {
                return $"[{newLabel}]{body[(close + 1)..]}";
            }
        }
        return $"[{newLabel}] {body}";
    }

    private const string RegistryPath = @"Software\AndroidTools\Page1";
    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNameCaseInsensitive = true
    };

    private readonly IRemoteNotificationSettingsStore _settingsStore;
    private readonly string? _statePath;
    private readonly bool _importLegacyRegistry;

    public RemoteNotificationsLegacyStore(
        IRemoteNotificationSettingsStore? settingsStore = null,
        string? dataRoot = null)
    {
        _settingsStore = settingsStore ?? new RemoteNotificationSettingsStore();
        var resolvedDataRoot = string.IsNullOrWhiteSpace(dataRoot)
            ? Environment.GetEnvironmentVariable("MPT_TOOL_DATA_ROOT")
            : dataRoot;
        _statePath = string.IsNullOrWhiteSpace(resolvedDataRoot)
            ? null
            : Path.Combine(
                Path.GetFullPath(Environment.ExpandEnvironmentVariables(resolvedDataRoot)),
                "history.json");
        _importLegacyRegistry = !string.Equals(
            Environment.GetEnvironmentVariable("MPT_REMOTE_NOTIFICATIONS_SKIP_LEGACY_IMPORT"),
            "1",
            StringComparison.Ordinal);
    }

    public RemoteNotificationsSnapshot Load()
    {
        var productSettings = _settingsStore.Load();
        if (_statePath is not null)
        {
            return WithFileLock(() =>
            {
                if (File.Exists(_statePath))
                {
                    var state = ReadFileStateUnsafe();
                    var before = state.Messages;
                    state.Messages = CleanupClaudeStopNoise(state.Messages).ToList();
                    if (!state.Messages.SequenceEqual(before))
                    {
                        state.KnownLabels = LabelsFor(state.Messages);
                        WriteFileStateUnsafe(state);
                    }
                    return ToSnapshot(state, productSettings.KeepWindowsBanners);
                }

                var imported = _importLegacyRegistry
                    ? LoadRegistry(productSettings)
                    : new RemoteNotificationsSnapshot([], [], null, productSettings.KeepWindowsBanners, []);
                var cleanedMessages = CleanupClaudeStopNoise(imported.MessagesOldestFirst)
                    .Where(message => !message.Id.StartsWith("test-inject-", StringComparison.Ordinal))
                    .ToArray();
                var cleanedLabels = cleanedMessages
                    .Select(message => ExtractLabel(message.Message))
                    .Reverse()
                    .Distinct(StringComparer.Ordinal)
                    .ToArray();
                var cleaned = imported with
                {
                    MessagesOldestFirst = cleanedMessages,
                    KnownLabels = cleanedLabels,
                    SeenMessageIds = cleanedMessages
                        .SelectMany(message => new[] { StableId(message), FallbackId(message) })
                        .Distinct(StringComparer.Ordinal)
                        .TakeLast(MaximumSeenMessageIds)
                        .ToArray()
                };
                WriteFileStateUnsafe(FromSnapshot(cleaned));
                return cleaned;
            });
        }

        return LoadRegistry(productSettings);
    }

    private RemoteNotificationsSnapshot LoadRegistry(RemoteNotificationSettings productSettings)
    {
        if (!OperatingSystem.IsWindows())
        {
            return new RemoteNotificationsSnapshot([], [], null, productSettings.KeepWindowsBanners, []);
        }

        using var key = Registry.CurrentUser.OpenSubKey(RegistryPath, writable: false);
        if (key is null)
        {
            return new RemoteNotificationsSnapshot([], [], null, productSettings.KeepWindowsBanners, []);
        }

        var messages = CleanupClaudeStopNoise(ParseMessages(ReadText(key.GetValue("messages"))));
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
        if (_statePath is not null)
        {
            WithFileLock(() =>
            {
                var state = ReadFileStateUnsafe();
                state.Messages = CleanupClaudeStopNoise(messagesOldestFirst)
                    .TakeLast(MaximumMessages)
                    .ToList();
                foreach (var message in state.Messages)
                {
                    Remember(state.SeenMessageIds, StableId(message), MaximumSeenMessageIds);
                    Remember(state.SeenMessageIds, FallbackId(message), MaximumSeenMessageIds);
                }
                WriteFileStateUnsafe(state);
            });
            return;
        }

        if (!OperatingSystem.IsWindows())
        {
            return;
        }

        var collapsed = CleanupClaudeStopNoise(messagesOldestFirst);
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
        if (_statePath is not null)
        {
            WithFileLock(() =>
            {
                var state = ReadFileStateUnsafe();
                state.FilterLabel = string.IsNullOrWhiteSpace(label) ? null : label;
                WriteFileStateUnsafe(state);
            });
            return;
        }
        WriteString("filter_label", string.IsNullOrWhiteSpace(label) ? FilterAll : label);
    }

    public void SaveKnownLabels(IReadOnlyList<string> labels)
    {
        if (_statePath is not null)
        {
            WithFileLock(() =>
            {
                var state = ReadFileStateUnsafe();
                state.KnownLabels = labels.Distinct(StringComparer.Ordinal).ToList();
                WriteFileStateUnsafe(state);
            });
            return;
        }
        WriteString("known_labels", string.Join(',', labels));
    }

    public void SavePersistentWindowsToasts(bool enabled)
    {
        var settings = _settingsStore.Load() with { KeepWindowsBanners = enabled };
        _settingsStore.Save(settings);
        if (_statePath is null)
        {
            WriteString("windows_toast_reminder", enabled ? "true" : "false");
        }
    }

    public void SaveSeenMessageIds(IReadOnlyList<string> messageIdsOldestFirst)
    {
        if (_statePath is not null)
        {
            WithFileLock(() =>
            {
                var state = ReadFileStateUnsafe();
                state.SeenMessageIds = messageIdsOldestFirst
                    .Where(messageId => !string.IsNullOrWhiteSpace(messageId))
                    .Distinct(StringComparer.Ordinal)
                    .TakeLast(MaximumSeenMessageIds)
                    .ToList();
                WriteFileStateUnsafe(state);
            });
            return;
        }

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
        if (_statePath is not null)
        {
            WithFileLock(() =>
            {
                var state = ReadFileStateUnsafe();
                state.Messages.Clear();
                WriteFileStateUnsafe(state);
            });
            return;
        }
        WriteString("messages", "[]");
    }

    private T WithFileLock<T>(Func<T> action)
    {
        var mutexName = $"Local\\MyPowerTools.RemoteNotifications.{Convert.ToHexString(
            SHA256.HashData(Encoding.UTF8.GetBytes(_statePath!)))[..24]}";
        using var mutex = new Mutex(false, mutexName);
        var acquired = false;
        try
        {
            try
            {
                acquired = mutex.WaitOne(TimeSpan.FromSeconds(10));
            }
            catch (AbandonedMutexException)
            {
                acquired = true;
            }

            if (!acquired)
            {
                throw new TimeoutException("Remote notification history is busy.");
            }
            return action();
        }
        finally
        {
            if (acquired)
            {
                mutex.ReleaseMutex();
            }
        }
    }

    private void WithFileLock(Action action) => WithFileLock(() =>
    {
        action();
        return true;
    });

    private PersistedState ReadFileStateUnsafe()
    {
        if (!File.Exists(_statePath))
        {
            return new PersistedState();
        }

        try
        {
            return JsonSerializer.Deserialize<PersistedState>(
                File.ReadAllText(_statePath),
                JsonOptions) ?? new PersistedState();
        }
        catch (Exception exception) when (exception is IOException or UnauthorizedAccessException or JsonException)
        {
            return new PersistedState();
        }
    }

    private void WriteFileStateUnsafe(PersistedState state)
    {
        var statePath = _statePath
            ?? throw new InvalidOperationException("Remote notification history path is unavailable.");
        var directory = Path.GetDirectoryName(statePath)
            ?? throw new InvalidOperationException("Remote notification history directory is invalid.");
        Directory.CreateDirectory(directory);
        var temporary = $"{statePath}.{Guid.NewGuid():N}.tmp";
        try
        {
            File.WriteAllText(temporary, JsonSerializer.Serialize(state, JsonOptions));
            File.Move(temporary, statePath, overwrite: true);
        }
        finally
        {
            if (File.Exists(temporary))
            {
                File.Delete(temporary);
            }
        }
    }

    private static PersistedState FromSnapshot(RemoteNotificationsSnapshot snapshot) => new()
    {
        Messages = snapshot.MessagesOldestFirst.TakeLast(MaximumMessages).ToList(),
        KnownLabels = snapshot.KnownLabels.Distinct(StringComparer.Ordinal).ToList(),
        FilterLabel = snapshot.FilterLabel,
        SeenMessageIds = (snapshot.SeenMessageIds ?? [])
            .Distinct(StringComparer.Ordinal)
            .TakeLast(MaximumSeenMessageIds)
            .ToList()
    };

    private static RemoteNotificationsSnapshot ToSnapshot(PersistedState state, bool persistent)
    {
        var messages = CleanupClaudeStopNoise(state.Messages)
            .Where(message => !string.IsNullOrWhiteSpace(message.Message))
            .TakeLast(MaximumMessages)
            .ToArray();
        var labels = state.KnownLabels
            .Where(label => !string.IsNullOrWhiteSpace(label))
            .Distinct(StringComparer.Ordinal)
            .ToList();
        foreach (var message in messages)
        {
            var label = ExtractLabel(message.Message);
            labels.Remove(label);
            labels.Insert(0, label);
        }

        var seen = state.SeenMessageIds
            .Where(messageId => !string.IsNullOrWhiteSpace(messageId))
            .Distinct(StringComparer.Ordinal)
            .TakeLast(MaximumSeenMessageIds)
            .ToList();
        foreach (var message in messages)
        {
            Remember(seen, StableId(message), MaximumSeenMessageIds);
            Remember(seen, FallbackId(message), MaximumSeenMessageIds);
        }
        return new RemoteNotificationsSnapshot(messages, labels, state.FilterLabel, persistent, seen);
    }

    /// <summary>
    /// Removes the burst produced when Claude Stop hooks replay one assistant
    /// snapshot while a tool turn is still active.  Identity-aware sender
    /// records are deduped by source event; legacy records use the stable
    /// session/body tuple and a short time window so repeated notifications
    /// from unrelated days remain visible.
    /// </summary>
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
                !TryNotificationTime(message, out var timestamp))
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
                continue;
            }

            recent[key] = (kept.Count, timestamp);
            kept.Add(message);
        }

        return kept
            .OrderBy(message => TryNotificationTime(message, out var time) ? time : DateTimeOffset.MinValue)
            .ToArray();
    }

    /// <summary>
    /// Keeps synthetic legacy Claude events on the dedicated Claude Task page
    /// and removes ordinary legacy Claude Stop replies from the Inbox. Those
    /// records have no source event identity, so they cannot be distinguished
    /// from stale background-agent reports after the fact.
    /// </summary>
    public static IReadOnlyList<RemoteNotificationRecord> CleanupClaudeStopNoise(
        IReadOnlyList<RemoteNotificationRecord> messagesOldestFirst)
    {
        var cleaned = new List<RemoteNotificationRecord>(messagesOldestFirst.Count);
        foreach (var message in CollapseClaudeStopDuplicates(messagesOldestFirst))
        {
            var rewritten = RewriteAsClaudeTaskIfHistoricalTaskTriggered(message);
            if (IsLegacyClaudeStopNoise(rewritten))
            {
                continue;
            }

            cleaned.Add(rewritten);
        }

        return cleaned;
    }

    private static bool IsLegacyClaudeStopNoise(RemoteNotificationRecord message)
    {
        var source = message.SourceClient ?? "";
        var icon = message.Icon ?? "";
        var isClaude = string.Equals(source, "claude", StringComparison.OrdinalIgnoreCase) ||
                       string.Equals(icon, "claude", StringComparison.OrdinalIgnoreCase);
        if (!isClaude ||
            !string.IsNullOrWhiteSpace(message.SourceEventId) ||
            !string.IsNullOrWhiteSpace(message.SourceMessageId) ||
            !string.IsNullOrWhiteSpace(message.ContentKind))
        {
            return false;
        }

        return !string.Equals(
            ExtractLabel(message.Message),
            ClaudeTaskLabel,
            StringComparison.Ordinal);
    }

    private static bool TryClaudeDuplicateKey(RemoteNotificationRecord message, out string key)
    {
        var source = message.SourceClient.Trim().ToLowerInvariant();
        var icon = message.Icon.Trim().ToLowerInvariant();
        if (!string.Equals(source, "claude", StringComparison.Ordinal) &&
            !string.Equals(icon, "claude", StringComparison.Ordinal))
        {
            key = "";
            return false;
        }
        if (string.IsNullOrWhiteSpace(message.SessionId))
        {
            key = "";
            return false;
        }

        var body = StripLeadingQuotedRequest(message.Message ?? "").Trim();
        var label = ExtractLabel(body);
        if (!string.Equals(label, "(unlabeled)", StringComparison.Ordinal) &&
            body.StartsWith($"[{label}]", StringComparison.Ordinal))
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

    private static bool TryNotificationTime(RemoteNotificationRecord message, out DateTimeOffset timestamp)
    {
        return DateTimeOffset.TryParse(
            string.IsNullOrWhiteSpace(message.ServerTimestamp) ? message.Timestamp : message.ServerTimestamp,
            CultureInfo.InvariantCulture,
            DateTimeStyles.AssumeUniversal | DateTimeStyles.AdjustToUniversal,
            out timestamp);
    }

    private static List<string> LabelsFor(IEnumerable<RemoteNotificationRecord> messages)
    {
        return messages
            .Select(message => ExtractLabel(message.Message))
            .Reverse()
            .Distinct(StringComparer.Ordinal)
            .ToList();
    }

    public static string ExtractLabel(string message)
    {
        if (!string.IsNullOrEmpty(message))
        {
            var openingBracket = message.IndexOf('[');
            while (openingBracket >= 0)
            {
                var isLineStart = openingBracket == 0
                    || message[openingBracket - 1] == '\n'
                    || message[openingBracket - 1] == '\r';
                if (isLineStart)
                {
                    var closingBracket = message.IndexOf(']', openingBracket + 1);
                    if (closingBracket > openingBracket + 1)
                    {
                        return message[(openingBracket + 1)..closingBracket];
                    }
                }
                openingBracket = message.IndexOf('[', openingBracket + 1);
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

    private sealed class PersistedState
    {
        public List<RemoteNotificationRecord> Messages { get; set; } = [];
        public List<string> KnownLabels { get; set; } = [];
        public string? FilterLabel { get; set; }
        public List<string> SeenMessageIds { get; set; } = [];
    }
}
