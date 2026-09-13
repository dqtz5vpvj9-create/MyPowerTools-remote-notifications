using System.Diagnostics;
using Avalonia.Controls;
using Avalonia.Input;
using Avalonia.Interactivity;
using Avalonia.Markup.Xaml;
using Avalonia.Platform;
using Avalonia.Styling;
using Avalonia.Threading;
using MyPowerTools.AvaloniaSdk;
using RemoteNotifications.Surface.Services;
using RemoteNotifications.Surface.ViewModels;

namespace RemoteNotifications.Surface.Views;

public sealed partial class RemoteNotificationDetailWindow : Window
{
    private readonly string _diagnosticId = Guid.NewGuid().ToString("N");
    private readonly ContentControl _documentHost;
    private readonly ScrollViewer _fallbackViewer;
    private readonly TextBlock _fallbackStatus;
    private IRemoteNotificationsStore? _sessionStore;
    private readonly IMptWebSurfaceService? _webSurfaces;
    private RemoteNotificationSessionPosition? _sessionPosition;
    private IMptWebSurfaceSession? _document;
    private bool _opened;
    private bool _closed;
    private long _generation;

    public RemoteNotificationDetailWindow()
    {
        TraceDetail("construct.begin");
        AvaloniaXamlLoader.Load(this);
        _documentHost = this.FindControl<ContentControl>("DocumentHost")
            ?? throw new InvalidOperationException("Document host was not found.");
        _fallbackViewer = this.FindControl<ScrollViewer>("FallbackViewer")
            ?? throw new InvalidOperationException("Markdown fallback viewer was not found.");
        _fallbackStatus = this.FindControl<TextBlock>("FallbackStatus")
            ?? throw new InvalidOperationException("Markdown fallback status was not found.");
        try
        {
            using var icon = AssetLoader.Open(new Uri("avares://MyPowerTools.Shell.Avalonia/Assets/MyPowerTools.ico"));
            Icon = new WindowIcon(icon);
        }
        catch (Exception ex) { Trace.WriteLine($"Notification window icon: {ex}"); }
        Opened += OnOpened;
        Closed += OnClosed;
        ActualThemeVariantChanged += OnActualThemeVariantChanged;
        TraceDetail("construct.ready");
    }

    public RemoteNotificationDetailWindow(
        RemoteNotificationMessageViewModel message,
        IRemoteNotificationsStore? sessionStore = null,
        IMptWebSurfaceService? webSurfaces = null) : this()
    {
        _sessionStore = sessionStore ?? new RemoteNotificationsLegacyStore();
        _webSurfaces = webSurfaces;
        SetMessage(message);
    }

    public IRemoteNotificationsStore SessionStore
    {
        get => _sessionStore ??= new RemoteNotificationsLegacyStore();
        set
        {
            _sessionStore = value ?? throw new ArgumentNullException(nameof(value));
            RefreshSessionPosition();
        }
    }

    public void NavigatePrevious() => Navigate(-1);
    public void NavigateNext() => Navigate(1);

    private void Navigate(int delta)
    {
        if (_closed || _sessionPosition is not { } position ||
            !RemoteNotificationSessionChain.TryNavigate(position, delta, out var target)) return;
        TraceDetail(delta < 0 ? "navigate.previous" : "navigate.next");
        SetMessage(new RemoteNotificationMessageViewModel(target));
    }

    internal void SetMessage(RemoteNotificationMessageViewModel message)
    {
        ArgumentNullException.ThrowIfNull(message);
        if (_closed) return;
        DataContext = message;
        Title = message.DetailWindowTitle;
        TraceDetail("message.selected");
        RefreshSessionPosition();
        if (_opened) RenderMarkdown();
    }

    private void RefreshSessionPosition()
    {
        _sessionPosition = null;
        if (DataContext is not RemoteNotificationMessageViewModel message) return;
        if (message.HasSession)
        {
            try
            {
                TraceDetail("history.load");
                _sessionPosition = RemoteNotificationSessionChain.Resolve(
                    SessionStore.Load().MessagesOldestFirst, message.Source);
            }
            catch (Exception ex)
            {
                // Session lookup is optional; the selected message remains readable.
                Trace.WriteLine($"Notification session lookup: {ex}");
            }
        }
        message.UpdateSessionPosition(_sessionPosition);
    }

    private void OnOpened(object? sender, EventArgs e)
    {
        _opened = true;
        TraceDetail("window.opened");
        RenderMarkdown();
    }

    private void RenderMarkdown()
    {
        if (!_opened || _closed || DataContext is not RemoteNotificationMessageViewModel message) return;
        TraceDetail("render.begin");
        ReleaseDocument();
        var generation = _generation;
        try
        {
            var label = message.Label;
            var body = string.IsNullOrWhiteSpace(label) ? message.Message : message.DisplayMessage;
            var html = RemoteNotificationHtmlDocument.Build(label, body, ActualThemeVariant == ThemeVariant.Dark);
            // Always enqueue commands: Close/navigation must not dispose the native view
            // on the stack of a WebKit callback. Ignore commands from replaced documents.
            void OnMessage(string value) => Dispatcher.UIThread.Post(() =>
            {
                if (!_closed && generation == _generation) HandleDocumentMessage(value);
            });
            TraceDetail("renderer.create");
            _document = OperatingSystem.IsMacOS() || _webSurfaces is not null
                ? new RemoteNotificationHostedDocument(
                    _webSurfaces ?? throw new PlatformNotSupportedException("The Shell does not provide a macOS web-surface service."),
                    html, OnMessage)
                : new RemoteNotificationNativeDocument(html, OnMessage);
            _document.StateChanged += OnDocumentStateChanged;
            _fallbackStatus.Text = "Loading formatted message…";
            _fallbackStatus.IsVisible = true;
            _fallbackViewer.IsVisible = true;
            _documentHost.IsVisible = true;
            TraceDetail("renderer.attach");
            _documentHost.Content = _document.View;
            TraceDetail("renderer.attached");
            ApplyDocumentState(_document.State, "");
        }
        catch (Exception ex)
        {
            Trace.WriteLine($"Notification Markdown viewer: {ex}");
            ShowFallback("The formatted viewer could not be loaded. The full message is available below.");
        }
    }

    private void OnDocumentStateChanged(object? sender, MptWebSurfaceStateChangedEventArgs e)
    {
        // A failing provider can report synchronously while Avalonia is attaching its
        // native child. Defer removal until that attachment has finished.
        var generation = _generation;
        Dispatcher.UIThread.Post(() =>
        {
            if (!_closed && generation == _generation && ReferenceEquals(sender, _document))
                ApplyDocumentState(e.State, e.Message);
        });
    }

    private void ApplyDocumentState(MptWebSurfaceState state, string message)
    {
        TraceDetail("renderer.state." + state);
        if (state == MptWebSurfaceState.Ready)
        {
            _fallbackViewer.IsVisible = false;
            _fallbackStatus.IsVisible = false;
        }
        else if (state is MptWebSurfaceState.Failed or MptWebSurfaceState.Unavailable)
        {
            Trace.WriteLine($"Notification document {state}: {message}");
            ShowFallback("The formatted viewer is unavailable. The full message is available below.");
        }
    }

    private void ShowFallback(string status)
    {
        TraceDetail("renderer.fallback");
        ReleaseDocument();
        _fallbackViewer.IsVisible = true;
        _fallbackStatus.IsVisible = !string.IsNullOrWhiteSpace(status);
        var logPath = Environment.GetEnvironmentVariable("MPT_SHELL_DIAGNOSTIC_LOG");
        _fallbackStatus.Text = string.IsNullOrWhiteSpace(logPath)
            ? status
            : status + Environment.NewLine + "Diagnostic log: " + logPath;
    }

    private void ReleaseDocument()
    {
        ++_generation;
        var document = _document;
        _document = null;
        if (document is not null)
        {
            TraceDetail("renderer.release");
            document.StateChanged -= OnDocumentStateChanged;
        }
        try
        {
            _documentHost.Content = null;
            _documentHost.IsVisible = false;
        }
        finally
        {
            try { document?.Dispose(); }
            catch (Exception ex) { Trace.WriteLine($"Notification viewer cleanup: {ex}"); }
        }
    }

    private void HandleDocumentMessage(string value)
    {
        MptCommandFaultBoundary.Run(this, "Notification detail action", () =>
        {
            var message = value.Trim();
            const string prefix = "open-external:";
            if (message.StartsWith(prefix, StringComparison.Ordinal))
            {
                if (Uri.TryCreate(message[prefix.Length..], UriKind.Absolute, out var uri) &&
                    uri.Scheme is "http" or "https")
                    Process.Start(new ProcessStartInfo(uri.AbsoluteUri) { UseShellExecute = true });
                return;
            }
            switch (message)
            {
                case "close": Close(); break;
                case "previous": NavigatePrevious(); break;
                case "next": NavigateNext(); break;
            }
        });
    }

    private void OnWindowKeyDown(object? sender, KeyEventArgs e)
    {
        if (e.Handled || e.Source is TextBox) return;
        switch (e.Key)
        {
            case Key.Escape:
                Close();
                e.Handled = true;
                break;
            case Key.Left when e.KeyModifiers == KeyModifiers.None && _sessionPosition is not null:
                NavigatePrevious();
                e.Handled = true;
                break;
            case Key.Right when e.KeyModifiers == KeyModifiers.None && _sessionPosition is not null:
                NavigateNext();
                e.Handled = true;
                break;
        }
    }

    private void OnActualThemeVariantChanged(object? sender, EventArgs e)
    {
        if (_opened && !_closed) RenderMarkdown();
    }

    private void OnClosed(object? sender, EventArgs e)
    {
        _closed = true;
        TraceDetail("window.closed");
        Opened -= OnOpened;
        Closed -= OnClosed;
        ActualThemeVariantChanged -= OnActualThemeVariantChanged;
        ReleaseDocument();
    }

    private void TraceDetail(string phase) => RemoteNotificationDiagnostics.Write(
        phase, DataContext as RemoteNotificationMessageViewModel, _diagnosticId, _generation, _webSurfaces?.GetType().FullName);

    private void OnPreviousClick(object? sender, RoutedEventArgs e) => NavigatePrevious();
    private void OnNextClick(object? sender, RoutedEventArgs e) => NavigateNext();
    private void OnCloseClick(object? sender, RoutedEventArgs e) => Close();

    private void OnCopyClick(object? sender, RoutedEventArgs e)
    {
        if (DataContext is RemoteNotificationMessageViewModel message) CopyText(message.Message);
    }
    private void OnCopySessionClick(object? sender, RoutedEventArgs e)
    {
        if (DataContext is RemoteNotificationMessageViewModel { HasSession: true } message) CopyText(message.SessionId);
    }
    private void CopyText(string text)
    {
        if (Clipboard is not { } clipboard) return;
        MptCommandFaultBoundary.Run(this, "Copy remote notification details", async () =>
        {
            var transfer = new DataTransfer();
            transfer.Add(DataTransferItem.CreateText(text));
            await clipboard.SetDataAsync(transfer);
            await clipboard.FlushAsync();
        });
    }
}
