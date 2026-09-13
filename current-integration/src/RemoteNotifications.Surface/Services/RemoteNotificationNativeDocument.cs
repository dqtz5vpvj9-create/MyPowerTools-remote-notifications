using System.Diagnostics;
using Avalonia.Controls;
using Avalonia.Platform;
using MyPowerTools.AvaloniaSdk;

namespace RemoteNotifications.Surface.Services;

/// <summary>Legacy Windows/Linux viewer. Never instantiate this adapter on macOS.</summary>
internal sealed class RemoteNotificationNativeDocument : IMptWebSurfaceSession
{
    private readonly NativeWebView _view;
    private readonly Action<string> _onMessage;
    private bool _disposed;

    public RemoteNotificationNativeDocument(string html, Action<string> onMessage)
    {
        if (OperatingSystem.IsMacOS())
            throw new PlatformNotSupportedException("macOS document views must use the Shell web-surface service.");
        if (!IsAvailable())
            throw new PlatformNotSupportedException("A native Markdown viewer is unavailable on this system.");
        _onMessage = onMessage;
        _view = new NativeWebView { Focusable = true, ClipToBounds = true };
        _view.WebMessageReceived += OnMessage;
        _view.NavigationStarted += OnNavigation;
        _view.NewWindowRequested += OnNewWindow;
        _view.NavigationCompleted += OnCompleted;
        _view.AdapterDestroyed += OnDestroyed;
        _view.NavigateToString(html, new Uri("about:blank"));
    }

    public Control View => _view;
    public MptWebSurfaceState State { get; private set; } = MptWebSurfaceState.Loading;
    public event EventHandler<MptWebSurfaceStateChangedEventArgs>? StateChanged;
    public void Reload() => _view.Refresh();

    private static bool IsAvailable()
    {
        WebViewAdapterType[] candidates = OperatingSystem.IsWindows()
            ? [WebViewAdapterType.WebView2, WebViewAdapterType.WebView1]
            : OperatingSystem.IsLinux()
                ? [WebViewAdapterType.WpeWebKit, WebViewAdapterType.WebKitGtk] : [];
        foreach (var candidate in candidates)
        {
            try
            {
                var info = WebViewAdapterInfo.GetAdapterInfo(candidate);
                if (info.IsSupported && info.IsInstalled) return true;
            }
            catch (Exception ex) { Trace.WriteLine($"WebView probe: {ex}"); }
        }
        return false;
    }

    private void OnMessage(object? sender, WebMessageReceivedEventArgs e)
    {
        if (!_disposed && e.Body is { } body) _onMessage(body);
    }
    private void OnNavigation(object? sender, WebViewNavigationStartingEventArgs e)
    {
        if (e.Request is { Scheme: "http" or "https" }) e.Cancel = true;
    }
    private void OnNewWindow(object? sender, WebViewNewWindowRequestedEventArgs e) => e.Handled = true;
    private void OnCompleted(object? sender, WebViewNavigationCompletedEventArgs e) =>
        SetState(e.IsSuccess ? MptWebSurfaceState.Ready : MptWebSurfaceState.Failed);
    private void OnDestroyed(object? sender, WebViewAdapterEventArgs e) => SetState(MptWebSurfaceState.Unavailable);
    private void SetState(MptWebSurfaceState state)
    {
        if (_disposed) return;
        State = state;
        StateChanged?.Invoke(this, new MptWebSurfaceStateChangedEventArgs(state));
    }
    public void Dispose()
    {
        if (_disposed) return;
        _disposed = true;
        _view.WebMessageReceived -= OnMessage;
        _view.NavigationStarted -= OnNavigation;
        _view.NewWindowRequested -= OnNewWindow;
        _view.NavigationCompleted -= OnCompleted;
        _view.AdapterDestroyed -= OnDestroyed;
        StateChanged = null;
        // The Window removes View from the visual tree before disposing this session.
        // NativeControlHost then releases its platform handle using its normal lifecycle.
    }
}
