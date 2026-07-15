using MyPowerTools.Shell.Avalonia.ViewModels;
using MyPowerTools.Shell.Avalonia.Views;

namespace MyPowerTools.Shell.Avalonia.Services;

public sealed class RemoteNotificationDetailWindowService : IDisposable
{
    private readonly IRemoteNotificationsStore _store;
    private readonly Dictionary<string, RemoteNotificationDetailWindow> _windows = new(StringComparer.Ordinal);
    private int _disposed;

    public RemoteNotificationDetailWindowService(IRemoteNotificationsStore? store = null)
    {
        _store = store ?? new RemoteNotificationsLegacyStore();
    }

    public static RemoteNotificationDetailWindowService Shared { get; } = new();

    public IReadOnlyList<RemoteNotificationDetailWindow> OpenWindows => _windows.Values.ToArray();

    public bool CanOpen(string messageId)
    {
        return TryFindRecord(_store.Load().MessagesOldestFirst, messageId, out _);
    }

    public bool TryOpen(string messageId)
    {
        ObjectDisposedException.ThrowIf(Volatile.Read(ref _disposed) != 0, this);
        return TryFindRecord(_store.Load().MessagesOldestFirst, messageId, out var record) &&
               Open(new RemoteNotificationMessageViewModel(record));
    }

    public bool Open(RemoteNotificationMessageViewModel message)
    {
        ObjectDisposedException.ThrowIf(Volatile.Read(ref _disposed) != 0, this);
        var key = string.IsNullOrWhiteSpace(message.Id) ? message.FallbackId : message.Id;
        if (_windows.TryGetValue(key, out var existing))
        {
            if (!existing.IsVisible)
            {
                existing.Show();
            }
            existing.WindowState = global::Avalonia.Controls.WindowState.Normal;
            existing.Activate();
            return true;
        }

        var detail = new RemoteNotificationDetailWindow(message);
        detail.Closed += OnClosed;
        _windows[key] = detail;
        detail.Show();
        detail.Activate();
        return true;

        void OnClosed(object? sender, EventArgs eventArgs)
        {
            detail.Closed -= OnClosed;
            _windows.Remove(key);
        }
    }

    public static bool TryFindRecord(
        IReadOnlyList<RemoteNotificationRecord> messagesOldestFirst,
        string messageId,
        out RemoteNotificationRecord record)
    {
        record = messagesOldestFirst.LastOrDefault(message =>
            string.Equals(RemoteNotificationsLegacyStore.StableId(message), messageId, StringComparison.Ordinal) ||
            string.Equals(RemoteNotificationsLegacyStore.FallbackId(message), messageId, StringComparison.Ordinal))!;
        return record is not null;
    }

    public void Dispose()
    {
        if (Interlocked.Exchange(ref _disposed, 1) != 0)
        {
            return;
        }

        foreach (var window in _windows.Values.ToArray())
        {
            window.Close();
        }
        _windows.Clear();
    }
}
