using System.Diagnostics;
using System.IO;
using System.Net;
using Avalonia.Controls;
using Avalonia.Input;
using Avalonia.Interactivity;
using Avalonia.Markup.Xaml;
using Avalonia.Platform;
using Avalonia.Styling;
using Markdig;
using MyPowerTools.AvaloniaSdk;
using RemoteNotifications.Surface.Services;
using RemoteNotifications.Surface.ViewModels;

namespace RemoteNotifications.Surface.Views;

public sealed partial class RemoteNotificationDetailWindow : Window
{
    private static readonly MarkdownPipeline MarkdownPipeline = new MarkdownPipelineBuilder()
        .UseAdvancedExtensions()
        .DisableHtml()
        .Build();

    private const string HtmlTemplate = """
        <!doctype html>
        <html data-theme="__THEME__">
        <head>
          <meta charset="utf-8">
          <meta name="viewport" content="width=device-width, initial-scale=1">
          <style>
            html[data-theme="light"] { --bg: #FFFFFF; --fg: #1F2328; --muted: #656D76; --border: #D0D7DE; --code-bg: #F6F8FA; --link: #0969DA; --selection: rgba(9, 105, 218, 0.25); }
            html[data-theme="dark"] { --bg: #1E1E1E; --fg: #E6EDF3; --muted: #9198A1; --border: #3D444D; --code-bg: #2D333B; --link: #539BF5; --selection: rgba(83, 155, 245, 0.30); }
            html, body { background: var(--bg); color: var(--fg); }
            body { margin: 0; padding: 16px; font-family: "Segoe UI", "Helvetica Neue", Arial, sans-serif; font-size: 14px; line-height: 1.55; overflow-wrap: break-word; }
            .label { margin-bottom: 10px; font-size: 12px; font-weight: 600; letter-spacing: 0.06em; text-transform: uppercase; color: var(--muted); }
            h1, h2, h3, h4 { line-height: 1.3; margin: 1em 0 0.5em; }
            h1 { font-size: 20px; }
            h2 { font-size: 17px; }
            h3 { font-size: 15px; }
            p { margin: 0.5em 0; }
            ul, ol { margin: 0.5em 0; padding-left: 1.5em; }
            li { margin: 0.2em 0; }
            pre { margin: 0.5em 0; padding: 10px; overflow: auto; background: var(--code-bg); border: 1px solid var(--border); border-radius: 6px; font-size: 12.5px; }
            code { padding: 0.1em 0.35em; border-radius: 4px; background: var(--code-bg); font-family: "Cascadia Code", Consolas, monospace; font-size: 0.9em; }
            pre code { padding: 0; background: transparent; }
            blockquote { margin: 0.5em 0; padding-left: 1em; border-left: 3px solid var(--border); color: var(--muted); }
            table { width: 100%; margin: 0.5em 0; border-collapse: collapse; }
            th, td { padding: 6px 10px; border: 1px solid var(--border); text-align: left; }
            th { background: var(--code-bg); }
            a { color: var(--link); }
            hr { margin: 1em 0; border: none; border-top: 1px solid var(--border); }
            .task-list-item { list-style: none; }
            .task-list-item input { margin-right: 0.4em; }
            img { max-width: 100%; }
            ::selection { background: var(--selection); }
          </style>
        </head>
        <body>
        {{CONTENT}}
        <script>
          function post(message) {
            if (window.chrome && window.chrome.webview) {
              window.chrome.webview.postMessage(message);
            }
            if (window.webkit && window.webkit.messageHandlers &&
                window.webkit.messageHandlers.close) {
              window.webkit.messageHandlers.close.postMessage(message);
            }
          }
          function isEditable(node) {
            while (node) {
              if (node.isContentEditable) { return true; }
              var tag = node.tagName;
              if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") { return true; }
              node = node.parentElement;
            }
            return false;
          }
          document.addEventListener("keydown", function (event) {
            if (event.key === "Escape") {
              post("close");
              return;
            }
            if (isEditable(event.target)) { return; }
            if (event.ctrlKey || event.metaKey || event.altKey || event.shiftKey) { return; }
            if (event.key === "ArrowLeft") {
              event.preventDefault();
              post("previous");
            } else if (event.key === "ArrowRight") {
              event.preventDefault();
              post("next");
            }
          });
        </script>
        </body>
        </html>
        """;

    private readonly NativeWebView _markdownWebView;
    private readonly ScrollViewer _fallbackViewer;
    private readonly TextBlock _fallbackStatus;
    private IRemoteNotificationsStore _sessionStore = new RemoteNotificationsLegacyStore();
    private RemoteNotificationSessionPosition? _sessionPosition;
    private bool _webViewReady;
    private bool _themeChangedBeforeReady;

    public RemoteNotificationDetailWindow()
    {
        AvaloniaXamlLoader.Load(this);
        Icon = new WindowIcon(AssetLoader.Open(new Uri("avares://MyPowerTools.Shell.Avalonia/Assets/MyPowerTools.ico")));
        _markdownWebView = this.FindControl<NativeWebView>("MarkdownWebView")
            ?? throw new InvalidOperationException("Markdown web view was not found.");
        _fallbackViewer = this.FindControl<ScrollViewer>("FallbackViewer")
            ?? throw new InvalidOperationException("Markdown fallback viewer was not found.");
        _fallbackStatus = this.FindControl<TextBlock>("FallbackStatus")
            ?? throw new InvalidOperationException("Markdown fallback status was not found.");
        _markdownWebView.WebMessageReceived += OnWebMessageReceived;
    }

    public RemoteNotificationDetailWindow(
        RemoteNotificationMessageViewModel message,
        IRemoteNotificationsStore? sessionStore = null)
        : this()
    {
        _sessionStore = sessionStore ?? new RemoteNotificationsLegacyStore();
        ActualThemeVariantChanged += OnActualThemeVariantChanged;
        Closed += OnClosed;
        SetMessage(message);
        if (IsWebViewAvailable())
        {
            RenderMarkdown();
            return;
        }

        ShowFallback("The web-based markdown viewer is unavailable on this system. Showing plain text instead.");
    }

    /// <summary>
    /// Store used to resolve the current message's session chain. The detail
    /// window service injects its own store so navigation sees the same
    /// history as the feed.
    /// </summary>
    public IRemoteNotificationsStore SessionStore
    {
        get => _sessionStore;
        set
        {
            _sessionStore = value ?? throw new ArgumentNullException(nameof(value));
            RefreshSessionPosition();
        }
    }

    public void NavigatePrevious()
    {
        Navigate(-1);
    }

    public void NavigateNext()
    {
        Navigate(1);
    }

    private void Navigate(int delta)
    {
        if (_sessionPosition is not { } position ||
            !RemoteNotificationSessionChain.TryNavigate(position, delta, out var target))
        {
            return;
        }

        SetMessage(new RemoteNotificationMessageViewModel(target));
    }

    private void SetMessage(RemoteNotificationMessageViewModel message)
    {
        DataContext = message;
        Title = message.DetailWindowTitle;
        RefreshSessionPosition();
        if (_webViewReady)
        {
            RenderMarkdown();
        }
    }

    private void RefreshSessionPosition()
    {
        var position = ResolveSessionPosition();
        _sessionPosition = position;
        if (DataContext is RemoteNotificationMessageViewModel message)
        {
            message.UpdateSessionPosition(position);
        }
    }

    private RemoteNotificationSessionPosition? ResolveSessionPosition()
    {
        if (DataContext is not RemoteNotificationMessageViewModel message || !message.HasSession)
        {
            return null;
        }

        try
        {
            return RemoteNotificationSessionChain.Resolve(
                SessionStore.Load().MessagesOldestFirst,
                message.Source);
        }
        catch (Exception exception) when (exception is IOException or UnauthorizedAccessException or TimeoutException)
        {
            // Session position is a convenience; a failing store must not break the window.
            return null;
        }
    }

    private void RenderMarkdown()
    {
        if (DataContext is not RemoteNotificationMessageViewModel message)
        {
            return;
        }

        var label = message.Label;
        var body = string.IsNullOrWhiteSpace(label) ? message.Message : message.DisplayMessage;
        var bodyHtml = Markdown.ToHtml(body, MarkdownPipeline);
        _markdownWebView.NavigateToString(BuildHtmlDocument(label, bodyHtml));
    }

    private string BuildHtmlDocument(string label, string bodyHtml)
    {
        var content = string.IsNullOrWhiteSpace(label)
            ? bodyHtml
            : $"<div class=\"label\">{WebUtility.HtmlEncode(label)}</div>{bodyHtml}";
        var theme = ActualThemeVariant == ThemeVariant.Dark ? "dark" : "light";
        return HtmlTemplate
            .Replace("__THEME__", theme, StringComparison.Ordinal)
            .Replace("{{CONTENT}}", content, StringComparison.Ordinal);
    }

    private static bool IsWebViewAvailable()
    {
        WebViewAdapterType[] candidates = OperatingSystem.IsWindows()
            ? [WebViewAdapterType.WebView2, WebViewAdapterType.WebView1]
            : OperatingSystem.IsMacOS()
                ? [WebViewAdapterType.WkWebView]
                : OperatingSystem.IsLinux()
                    ? [WebViewAdapterType.WpeWebKit, WebViewAdapterType.WebKitGtk]
                    : Array.Empty<WebViewAdapterType>();

        foreach (var candidate in candidates)
        {
            try
            {
                var info = WebViewAdapterInfo.GetAdapterInfo(candidate);
                if (info.IsSupported && info.IsInstalled)
                {
                    return true;
                }
            }
            catch (Exception)
            {
                // A failed probe only means this adapter is not usable; keep checking.
            }
        }

        return false;
    }

    private void OnWebViewAdapterCreated(object? sender, WebViewAdapterEventArgs e)
    {
        _webViewReady = true;
        ShowWebView();
        if (_themeChangedBeforeReady)
        {
            _themeChangedBeforeReady = false;
            RenderMarkdown();
        }
    }

    private void OnWebViewAdapterDestroyed(object? sender, WebViewAdapterEventArgs e)
    {
        _webViewReady = false;
        ShowFallback("The web-based markdown viewer was disconnected. Showing plain text instead.");
    }

    private void OnWebMessageReceived(object? sender, WebMessageReceivedEventArgs e)
    {
        switch (e.Body?.Trim().ToLowerInvariant())
        {
            case "close":
                Close();
                break;
            case "previous":
                NavigatePrevious();
                break;
            case "next":
                NavigateNext();
                break;
        }
    }

    private void OnWindowKeyDown(object? sender, KeyEventArgs e)
    {
        switch (e.Key)
        {
            case Key.Escape:
                Close();
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

    private void ShowWebView()
    {
        _markdownWebView.IsVisible = true;
        _fallbackViewer.IsVisible = false;
        _fallbackStatus.IsVisible = false;
    }

    private void ShowFallback(string status)
    {
        _markdownWebView.IsVisible = false;
        _fallbackViewer.IsVisible = true;
        _fallbackStatus.IsVisible = !string.IsNullOrWhiteSpace(status);
        _fallbackStatus.Text = status;
    }

    private void OnActualThemeVariantChanged(object? sender, EventArgs eventArgs)
    {
        if (_webViewReady)
        {
            RenderMarkdown();
            return;
        }

        _themeChangedBeforeReady = true;
    }

    private void OnClosed(object? sender, EventArgs eventArgs)
    {
        ActualThemeVariantChanged -= OnActualThemeVariantChanged;
        Closed -= OnClosed;
    }

    private void OnWebViewNavigationStarted(object? sender, WebViewNavigationStartingEventArgs e)
    {
        if (e.Request is not { Scheme: "http" or "https" })
        {
            return;
        }

        e.Cancel = true;
        OpenExternal(e.Request);
    }

    private void OnWebViewNewWindowRequested(object? sender, WebViewNewWindowRequestedEventArgs e)
    {
        e.Handled = true;
        if (e.Request is { Scheme: "http" or "https" })
        {
            OpenExternal(e.Request);
        }
    }

    private static void OpenExternal(Uri uri)
    {
        try
        {
            Process.Start(new ProcessStartInfo
            {
                FileName = uri.AbsoluteUri,
                UseShellExecute = true
            });
        }
        catch (Exception)
        {
            // Opening the system browser must never break the detail window.
        }
    }

    private void OnPreviousClick(object? sender, RoutedEventArgs e)
    {
        NavigatePrevious();
    }

    private void OnNextClick(object? sender, RoutedEventArgs e)
    {
        NavigateNext();
    }

    private void OnCopyClick(object? sender, RoutedEventArgs e)
    {
        if (DataContext is not RemoteNotificationMessageViewModel message || Clipboard is null)
        {
            return;
        }

        MptCommandFaultBoundary.Run(
            this,
            "Copy remote notification details",
            async () =>
            {
                var transfer = new DataTransfer();
                transfer.Add(DataTransferItem.CreateText(message.Message));
                await Clipboard.SetDataAsync(transfer);
                await Clipboard.FlushAsync();
            });
    }

    private void OnCopySessionClick(object? sender, RoutedEventArgs e)
    {
        if (DataContext is not RemoteNotificationMessageViewModel message ||
            !message.HasSession ||
            Clipboard is null)
        {
            return;
        }

        MptCommandFaultBoundary.Run(
            this,
            "Copy remote notification Session ID",
            async () =>
            {
                var transfer = new DataTransfer();
                transfer.Add(DataTransferItem.CreateText(message.SessionId));
                await Clipboard.SetDataAsync(transfer);
                await Clipboard.FlushAsync();
            });
    }

    private void OnCloseClick(object? sender, RoutedEventArgs e)
    {
        Close();
    }
}
