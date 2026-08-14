namespace RemoteNotifications.Surface.Services;

/// <summary>
/// The chronological chain of messages that share one session id, ordered
/// oldest first, together with the position of one message inside it.
/// </summary>
public sealed record RemoteNotificationSessionPosition(
    int Index,
    int Count,
    IReadOnlyList<RemoteNotificationRecord> MessagesOldestFirst);

/// <summary>
/// Resolves session chains for the detail window. Messages with the same
/// non-empty <c>session_id</c> form one chain in arrival order; the window
/// shows "N / M" and supports ← / → browsing along the chain.
/// </summary>
public static class RemoteNotificationSessionChain
{
    public static RemoteNotificationSessionPosition? Resolve(
        IReadOnlyList<RemoteNotificationRecord> messagesOldestFirst,
        RemoteNotificationRecord current)
    {
        if (string.IsNullOrWhiteSpace(current.SessionId))
        {
            return null;
        }

        var chain = messagesOldestFirst
            .Where(message => string.Equals(message.SessionId, current.SessionId, StringComparison.Ordinal))
            .ToArray();
        if (chain.Length == 0)
        {
            return null;
        }

        var currentStableId = RemoteNotificationsLegacyStore.StableId(current);
        var currentFallbackId = RemoteNotificationsLegacyStore.FallbackId(current);
        var index = Array.FindIndex(chain, message =>
            string.Equals(RemoteNotificationsLegacyStore.StableId(message), currentStableId, StringComparison.Ordinal) ||
            string.Equals(RemoteNotificationsLegacyStore.FallbackId(message), currentFallbackId, StringComparison.Ordinal));
        return new RemoteNotificationSessionPosition(index, chain.Length, chain);
    }

    /// <summary>
    /// Steps along the chain by <paramref name="delta"/> (-1 = previous/older,
    /// 1 = next/newer) and returns false at either end.
    /// </summary>
    public static bool TryNavigate(
        RemoteNotificationSessionPosition position,
        int delta,
        out RemoteNotificationRecord target)
    {
        var index = position.Index + delta;
        if (index < 0 || index >= position.Count)
        {
            target = null!;
            return false;
        }

        target = position.MessagesOldestFirst[index];
        return true;
    }
}
