using System.Runtime.InteropServices;
using Avalonia.Controls;
using Avalonia.Platform;
using RemoteNotifications.Surface.ViewModels;
using RemoteNotifications.Surface.Views;

namespace RemoteNotifications.Surface.Services;

public sealed class RemoteNotificationDetailWindowService : IDisposable
{
    private const int SwShow = 5;
    private const uint SwpNoSize = 0x0001;
    private const uint SwpNoMove = 0x0002;
    private const uint SwpShowWindow = 0x0040;
    private static readonly IntPtr HwndTopmost = new(-1);
    private static readonly IntPtr HwndNotTopmost = new(-2);

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
            Present(existing);
            return true;
        }

        var detail = new RemoteNotificationDetailWindow(message)
        {
            SessionStore = _store
        };
        detail.Closed += OnClosed;
        _windows[key] = detail;
        Present(detail);
        return true;

        void OnClosed(object? sender, EventArgs eventArgs)
        {
            detail.Closed -= OnClosed;
            _windows.Remove(key);
        }
    }

    private static void Present(RemoteNotificationDetailWindow window)
    {
        if (!window.IsVisible)
        {
            window.Show();
        }

        window.WindowState = WindowState.Normal;
        window.Activate();
        BringToForeground(window);
    }

    private static void BringToForeground(RemoteNotificationDetailWindow window)
    {
        if (!OperatingSystem.IsWindows())
        {
            return;
        }

        var handle = window.TryGetPlatformHandle()?.Handle ?? IntPtr.Zero;
        if (handle == IntPtr.Zero)
        {
            return;
        }

        _ = ShowWindow(handle, SwShow);
        var foregroundSet = SetForegroundWindow(handle);
        _ = BringWindowToTop(handle);
        if (foregroundSet && GetForegroundWindow() == handle)
        {
            return;
        }

        const uint flags = SwpNoMove | SwpNoSize | SwpShowWindow;
        _ = SetWindowPos(handle, HwndTopmost, 0, 0, 0, 0, flags);
        _ = SetWindowPos(handle, HwndNotTopmost, 0, 0, 0, 0, flags);
        _ = SetForegroundWindow(handle);
        _ = BringWindowToTop(handle);
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

    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool ShowWindow(IntPtr windowHandle, int command);

    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool SetForegroundWindow(IntPtr windowHandle);

    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool BringWindowToTop(IntPtr windowHandle);

    [DllImport("user32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool SetWindowPos(
        IntPtr windowHandle,
        IntPtr insertAfter,
        int x,
        int y,
        int width,
        int height,
        uint flags);

    [DllImport("user32.dll")]
    private static extern IntPtr GetForegroundWindow();
}
