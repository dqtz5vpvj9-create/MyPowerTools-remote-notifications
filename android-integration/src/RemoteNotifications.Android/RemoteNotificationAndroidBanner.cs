using MyPowerTools.Platform.Abstractions;
using RemoteNotifications.Surface.Services;

namespace RemoteNotifications.Android;

/// <summary>
/// Notification-tray text for Android, mirroring the desktop toast rules:
/// the banner shows <c>[label] result</c> (never the quoted request), Claude Task bursts and
/// system-health records stay silent, whitespace is collapsed and text is truncated to the same
/// limits. <c>RemoteNotificationWindowsToastPublisher.BuildEnvelope</c> is compiled into the test
/// project and compared field by field, so the phone and the desktop cannot drift apart.
/// </summary>
internal static class RemoteNotificationAndroidBanner
{
    public const string ClaudeTaskLabel = "Claude Task";
    public const string SilentSystemHealth = "silent-system-health";
    public const string SilentClaudeTask = "silent-claude-task";
    public const int TitleLimit = 140;
    public const int BodyLimit = 900;
    public const string NotificationActivationPrefix = "mypowertools://remote-notification?id=";

    public static RemoteNotificationBanner Build(
        RemoteNotificationRecord notification,
        string messageId,
        bool persistent = false)
    {
        ArgumentNullException.ThrowIfNull(notification);
        var stableId = string.IsNullOrWhiteSpace(messageId)
            ? RemoteNotificationsLegacyStore.StableId(notification)
            : messageId;
        if (RemoteNotificationsLegacyStore.IsSystemHealthRecord(notification))
        {
            return new RemoteNotificationBanner(stableId, "", "", "", SilentSystemHealth, Suppressed: true);
        }

        var sourceMessage = RemoteNotificationsLegacyStore.StripLeadingQuotedRequest(notification.Message ?? "");
        var label = RemoteNotificationsLegacyStore.ExtractLabel(sourceMessage);
        if (string.Equals(label, ClaudeTaskLabel, StringComparison.Ordinal))
        {
            return new RemoteNotificationBanner(stableId, "", "", "", SilentClaudeTask, Suppressed: true);
        }

        var hasLabel = !string.Equals(label, "(unlabeled)", StringComparison.Ordinal);
        var prefix = hasLabel ? $"[{label}]" : "";
        var title = hasLabel
            ? label
            : string.IsNullOrWhiteSpace(notification.Channel) ? "Notification" : notification.Channel;
        var body = hasLabel && sourceMessage.StartsWith(prefix, StringComparison.Ordinal)
            ? sourceMessage[prefix.Length..].TrimStart()
            : sourceMessage;
        title = Normalize(title, TitleLimit);
        body = Normalize(body, BodyLimit);
        return new RemoteNotificationBanner(
            stableId,
            title.Length > 0 ? title : "MyPowerTools",
            body,
            $"{NotificationActivationPrefix}{Uri.EscapeDataString(stableId)}",
            persistent ? "reminder" : "banner",
            Suppressed: false);
    }

    private static string Normalize(string value, int maximumLength)
    {
        var cleaned = new string((value ?? "")
            .Select(character => character is '\t' or '\n' or '\r' || character >= ' '
                ? character
                : ' ')
            .ToArray());
        cleaned = string.Join(' ', cleaned.Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries));
        return cleaned.Length <= maximumLength
            ? cleaned
            : $"{cleaned[..(maximumLength - 3)]}...";
    }
}

internal sealed record RemoteNotificationBanner(
    string MessageId,
    string Title,
    string Body,
    string LaunchUri,
    string State,
    bool Suppressed)
{
    /// <summary>Publishes through the already-implemented <c>notification.desktop</c> capability.</summary>
    public async Task<bool> PublishAsync(INotificationService notifications, CancellationToken cancellationToken)
    {
        if (Suppressed)
        {
            return false;
        }

        await notifications.PublishAsync(
            new DesktopNotificationRequest(MessageId, Title, Body, LaunchUri),
            cancellationToken).ConfigureAwait(false);
        return true;
    }
}
