namespace MyPowerTools.Shell.Avalonia.Services;

public sealed class RemoteNotificationSeenIdRing
{
    private readonly int _capacity;
    private readonly Queue<string> _order = new();
    private readonly HashSet<string> _ids = new(StringComparer.Ordinal);

    public RemoteNotificationSeenIdRing(
        IEnumerable<string>? initialIds = null,
        int capacity = RemoteNotificationsLegacyStore.MaximumSeenMessageIds)
    {
        _capacity = Math.Max(1, capacity);
        if (initialIds is null)
        {
            return;
        }

        foreach (var messageId in initialIds)
        {
            Remember(messageId);
        }
    }

    public int Count => _order.Count;

    public IReadOnlyList<string> OldestFirst => _order.ToArray();

    public bool Contains(string messageId)
    {
        return !string.IsNullOrWhiteSpace(messageId) && _ids.Contains(messageId);
    }

    public bool TryAccept(params string[] messageIds)
    {
        var duplicate = messageIds.Any(Contains);
        foreach (var messageId in messageIds)
        {
            Remember(messageId);
        }

        return !duplicate;
    }

    public void Remember(string messageId)
    {
        if (string.IsNullOrWhiteSpace(messageId) || !_ids.Add(messageId))
        {
            return;
        }

        if (_order.Count == _capacity)
        {
            _ids.Remove(_order.Dequeue());
        }

        _order.Enqueue(messageId);
    }
}
