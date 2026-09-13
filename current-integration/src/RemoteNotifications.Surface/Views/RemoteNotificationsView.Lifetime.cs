using RemoteNotifications.Surface.Services;

namespace RemoteNotifications.Surface.Views;

public sealed partial class RemoteNotificationsView : IDisposable
{
    private int _surfaceDisposed;

    public void Dispose()
    {
        if (Interlocked.Exchange(ref _surfaceDisposed, 1) != 0) return;
        // Loader disposal, not ordinary tab detachment, ends the tool lifetime.
        UnsubscribeFromMessageChanges();
        ClearPendingScrollAnchor();
        if (_hostScroller is not null)
        {
            _hostScroller.SizeChanged -= OnHostScrollerSizeChanged;
            _hostScroller = null;
        }
        if (!ReferenceEquals(_detailWindows, RemoteNotificationDetailWindowService.Shared))
            _detailWindows.Dispose();
    }
}
