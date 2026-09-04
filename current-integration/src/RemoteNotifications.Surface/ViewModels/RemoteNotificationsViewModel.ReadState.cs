namespace RemoteNotifications.Surface.ViewModels;

public sealed partial class RemoteNotificationsViewModel
{
    public int MarkVisibleMessagesAsRead()
    {
        var labels = RemoteNotificationReadSelection.FindUnreadLabels(
            VisibleMessages.Select(message => message.Label),
            _unreadLabels);
        foreach (var label in labels)
        {
            _unreadLabels.Remove(label);
        }

        if (labels.Count > 0)
        {
            RebuildChips();
        }

        return labels.Count;
    }
}

public static class RemoteNotificationReadSelection
{
    public static IReadOnlyList<string> FindUnreadLabels(
        IEnumerable<string> visibleLabels,
        IEnumerable<string> unreadLabels)
    {
        ArgumentNullException.ThrowIfNull(visibleLabels);
        ArgumentNullException.ThrowIfNull(unreadLabels);

        var unread = new HashSet<string>(unreadLabels, StringComparer.Ordinal);
        return visibleLabels
            .Where(label => unread.Contains(label))
            .Distinct(StringComparer.Ordinal)
            .ToArray();
    }
}
