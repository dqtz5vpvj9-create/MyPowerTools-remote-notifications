using System.Diagnostics;
using System.Net;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using Avalonia.Controls;
using MyPowerTools.AvaloniaSdk;

namespace RemoteNotifications.Surface.Services;

/// <summary>
/// An immutable document owned by one detail-window render. The native implementation
/// comes from the Shell, never from a collectible tool AssemblyLoadContext.
/// </summary>
internal sealed class RemoteNotificationHostedDocument : IMptWebSurfaceSession
{
    private readonly DirectoryInfo _directory;
    private readonly IMptWebSurfaceSession _session;
    private int _disposed;

    public RemoteNotificationHostedDocument(
        IMptWebSurfaceService service,
        string html,
        Action<string> onMessage)
    {
        _directory = Directory.CreateTempSubdirectory("mpt-notification-");
        IMptWebSurfaceSession? created = null;
        try
        {
            var path = Path.Combine(_directory.FullName, "message.html");
            File.WriteAllText(path, html, new UTF8Encoding(false));
            var request = new MptWebSurfaceRequest(
                "remote-notifications", "detail", new Uri(path),
                GetOrigins(_directory.FullName, html),
                (json, _) =>
                {
                    if (Volatile.Read(ref _disposed) != 0) return Task.FromResult("null");
                    try
                    {
                        // WKWebView serializes a string as a JSON string, including quotes.
                        using var message = JsonDocument.Parse(json);
                        if (message.RootElement.ValueKind == JsonValueKind.String &&
                            message.RootElement.GetString() is { } value)
                            onMessage(value);
                    }
                    catch (JsonException) { }
                    return Task.FromResult("null");
                });
            _session = created = service is IMptWindowWebSurfaceService windows
                ? windows.CreateWindowSession(request)
                : service.CreateSession(request);
            _session.StateChanged += OnStateChanged;
        }
        catch
        {
            try { created?.Dispose(); }
            finally { DeleteDocument(); }
            throw;
        }
    }

    public Control View => _session.View;
    public MptWebSurfaceState State => _session.State;
    public event EventHandler<MptWebSurfaceStateChangedEventArgs>? StateChanged;
    public void Reload() => _session.Reload();

    private void OnStateChanged(object? sender, MptWebSurfaceStateChangedEventArgs e)
    {
        if (Volatile.Read(ref _disposed) == 0) StateChanged?.Invoke(this, e);
    }

    private static IReadOnlyList<Uri> GetOrigins(string directory, string html)
    {
        var origins = new List<Uri> { new(directory + Path.DirectorySeparatorChar) };
        // Markdig emits quoted src attributes. Preserve remote Markdown images when
        // the host applies its origin-based CSP; links are still opened by the bridge.
        foreach (Match match in Regex.Matches(html, "<img\\b[^>]*?\\bsrc=\"([^\"]*)\"",
                     RegexOptions.IgnoreCase | RegexOptions.CultureInvariant,
                     TimeSpan.FromSeconds(1)))
        {
            if (Uri.TryCreate(WebUtility.HtmlDecode(match.Groups[1].Value), UriKind.Absolute, out var uri) &&
                uri.Scheme is "https" or "http")
            {
                var origin = new Uri(uri.GetLeftPart(UriPartial.Authority) + "/");
                if (!origins.Contains(origin)) origins.Add(origin);
            }
        }
        return origins;
    }

    public void Dispose()
    {
        if (Interlocked.Exchange(ref _disposed, 1) != 0) return;
        _session.StateChanged -= OnStateChanged;
        StateChanged = null;
        try { _session.Dispose(); }
        finally { DeleteDocument(); }
    }

    private void DeleteDocument()
    {
        try { _directory.Delete(recursive: true); }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            Trace.WriteLine($"Remote notification document cleanup: {ex}");
        }
    }
}
