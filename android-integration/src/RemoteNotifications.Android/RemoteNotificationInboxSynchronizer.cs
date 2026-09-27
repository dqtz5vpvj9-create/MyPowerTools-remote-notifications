using System.Globalization;
using MyPowerTools.RemoteNotifications.Configuration;
using RemoteNotifications.Surface.Services;

namespace RemoteNotifications.Android;

/// <summary>
/// Android equivalent of the desktop <c>RemoteNotificationBackgroundReceiver</c>: one signed pull
/// folded into the shared persisted inbox.
///
/// The merge rules are the product rules, not an Android local dialect:
/// <list type="bullet">
///   <item>the <c>since</c> waterline is the newest <c>server_timestamp</c> already in history,
///   clamped to "not in the future" so a clock-skewed server cannot freeze the feed;</item>
///   <item>records more than two minutes in the future are dropped as insane;</item>
///   <item>new records are deduplicated against the persisted seen-id ring (stable id and
///   fallback id), merged by stable id, trimmed to <c>MaximumMessages</c> and re-sorted by
///   notification time;</item>
///   <item>the reference-block/label semantics come from <c>RemoteNotificationsLegacyStore</c>,
///   the same shipped source the desktop client compiles.</item>
/// </list>
/// The desktop receiver is compiled into the Android test project so a mock-server test can
/// assert both implementations accept exactly the same records for the same responses.
/// </summary>
internal sealed class RemoteNotificationInboxSynchronizer
{
    private readonly RemoteNotificationsLegacyStore _store;
    private readonly IRemoteNotificationPoller _poller;
    private string _waterline;

    public RemoteNotificationInboxSynchronizer(
        RemoteNotificationsLegacyStore store,
        IRemoteNotificationPoller poller)
    {
        _store = store ?? throw new ArgumentNullException(nameof(store));
        _poller = poller ?? throw new ArgumentNullException(nameof(poller));
        _waterline = ResolveWaterline(_store.Load().MessagesOldestFirst);
    }

    public string Waterline => _waterline;

    public async Task<RemoteNotificationInboxOutcome> PollAsync(CancellationToken cancellationToken)
    {
        var result = await _poller.PullAsync(_waterline, cancellationToken).ConfigureAwait(false);
        var sane = result.Notifications
            .Where(IsSane)
            .OrderBy(NotificationTime)
            .ToArray();

        if (sane.Length > 0)
        {
            _waterline = NotificationTime(sane[^1]).ToUniversalTime()
                .ToString("O", CultureInfo.InvariantCulture);
        }

        if (!result.IsSuccess || sane.Length == 0)
        {
            return new RemoteNotificationInboxOutcome(result, [], _waterline);
        }

        // Reload on every cycle so the module's background loop and the open surface share one
        // seen-id ring and one persisted inbox.
        var snapshot = _store.Load();
        var seen = new RemoteNotificationSeenIdRing(snapshot.SeenMessageIds);
        foreach (var message in snapshot.MessagesOldestFirst)
        {
            seen.TryAccept(
                RemoteNotificationsLegacyStore.StableId(message),
                RemoteNotificationsLegacyStore.FallbackId(message));
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
            return new RemoteNotificationInboxOutcome(result, [], _waterline);
        }

        var merged = snapshot.MessagesOldestFirst
            .Concat(accepted)
            .GroupBy(RemoteNotificationsLegacyStore.StableId, StringComparer.Ordinal)
            .Select(group => group.Last())
            .OrderBy(NotificationTime)
            .TakeLast(RemoteNotificationsLegacyStore.MaximumMessages)
            .ToArray();
        _store.SaveMessages(merged);
        _store.SaveSeenMessageIds(seen.OldestFirst);

        var labels = snapshot.KnownLabels.ToList();
        foreach (var notification in accepted)
        {
            var label = RemoteNotificationsLegacyStore.ExtractLabel(notification.Message);
            labels.Remove(label);
            labels.Insert(0, label);
        }

        _store.SaveKnownLabels(labels);

        return new RemoteNotificationInboxOutcome(result, accepted, _waterline);
    }

    internal static string ResolveWaterline(IReadOnlyList<RemoteNotificationRecord> messages)
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

    internal static bool IsSane(RemoteNotificationRecord notification)
    {
        var timestamp = string.IsNullOrWhiteSpace(notification.ServerTimestamp)
            ? notification.Timestamp
            : notification.ServerTimestamp;
        return TryParse(timestamp, out var parsed) && parsed <= DateTimeOffset.UtcNow.AddMinutes(2);
    }

    internal static DateTimeOffset NotificationTime(RemoteNotificationRecord notification)
    {
        var timestamp = string.IsNullOrWhiteSpace(notification.ServerTimestamp)
            ? notification.Timestamp
            : notification.ServerTimestamp;
        return TryParse(timestamp, out var parsed) ? parsed : DateTimeOffset.MinValue;
    }

    private static bool TryParse(string value, out DateTimeOffset parsed)
    {
        return DateTimeOffset.TryParse(
            value,
            CultureInfo.InvariantCulture,
            DateTimeStyles.AllowWhiteSpaces | DateTimeStyles.AssumeUniversal,
            out parsed);
    }
}

internal sealed record RemoteNotificationInboxOutcome(
    RemoteNotificationPullResult Pull,
    IReadOnlyList<RemoteNotificationRecord> Accepted,
    string Waterline);
