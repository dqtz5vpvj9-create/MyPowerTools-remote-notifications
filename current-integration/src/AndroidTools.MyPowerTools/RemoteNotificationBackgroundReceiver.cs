using System.Globalization;
using MyPowerTools.RemoteNotifications.Configuration;
using MyPowerTools.Shell.Avalonia.Services;

namespace AndroidTools.MyPowerTools;

internal sealed record RemoteNotificationBackgroundPoll(
    RemoteNotificationPullResult Pull,
    IReadOnlyList<RemoteNotificationRecord> Accepted,
    string Waterline);

internal sealed class RemoteNotificationBackgroundReceiver
{
    private readonly RemoteNotificationsLegacyStore _store;
    private readonly RemoteNotificationHttpPoller _poller;
    private string _waterline;

    public RemoteNotificationBackgroundReceiver(RemoteNotificationSettings settings)
    {
        _store = new RemoteNotificationsLegacyStore(new RemoteNotificationSettingsStore());
        _poller = new RemoteNotificationHttpPoller(settings);
        _waterline = ResolveWaterline(_store.Load().MessagesOldestFirst);
    }

    public async Task<RemoteNotificationBackgroundPoll> PollAsync(CancellationToken cancellationToken)
    {
        var result = await _poller.PullAsync(_waterline, cancellationToken).ConfigureAwait(false);
        var sane = result.Notifications
            .Where(IsSane)
            .OrderBy(NotificationTime)
            .ToArray();

        if (sane.Length > 0)
        {
            _waterline = NotificationTime(sane[^1]).ToUniversalTime().ToString("O", CultureInfo.InvariantCulture);
        }

        if (!result.IsSuccess || sane.Length == 0)
        {
            return new RemoteNotificationBackgroundPoll(result, [], _waterline);
        }

        // Reload on every cycle so a concurrently open Shell and the Runner
        // share one seen-id ring and one persisted inbox.
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
            return new RemoteNotificationBackgroundPoll(result, [], _waterline);
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

        return new RemoteNotificationBackgroundPoll(result, accepted, _waterline);
    }

    private static string ResolveWaterline(IReadOnlyList<RemoteNotificationRecord> messages)
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

    private static bool IsSane(RemoteNotificationRecord notification)
    {
        var timestamp = string.IsNullOrWhiteSpace(notification.ServerTimestamp)
            ? notification.Timestamp
            : notification.ServerTimestamp;
        return TryParse(timestamp, out var parsed) && parsed <= DateTimeOffset.UtcNow.AddMinutes(2);
    }

    private static DateTimeOffset NotificationTime(RemoteNotificationRecord notification)
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
