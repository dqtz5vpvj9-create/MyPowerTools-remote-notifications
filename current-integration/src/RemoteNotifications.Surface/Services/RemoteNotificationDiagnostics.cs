using System.Diagnostics;
using System.Security.Cryptography;
using System.Text;
using Avalonia.Threading;
using RemoteNotifications.Surface.ViewModels;

namespace RemoteNotifications.Surface.Services;

/// <summary>Lifecycle breadcrumbs without notification body, title, session ID, or raw message ID.</summary>
internal static class RemoteNotificationDiagnostics
{
    public static void Write(string phase, RemoteNotificationMessageViewModel? message = null,
        string? windowId = null, long generation = 0, string? provider = null)
    {
        try
        {
            var id = message is null ? "" : string.IsNullOrWhiteSpace(message.Id) ? message.FallbackId : message.Id;
            var hash = id.Length == 0 ? "none" : Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(id)))[..16];
            Trace.WriteLine($"notification.detail phase={phase} window={windowId ?? "pending"} generation={generation} selectedMessageHash={hash} messageLength={message?.Message.Length ?? 0} uiThread={Dispatcher.UIThread.CheckAccess()} provider={provider ?? "not-selected"}");
        }
        catch
        {
            // Observability must not introduce another failure in the UI path.
        }
    }
}
