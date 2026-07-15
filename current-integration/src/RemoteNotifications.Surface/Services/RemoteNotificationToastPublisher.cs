using System.Security;
using Avalonia;
using Avalonia.Controls.ApplicationLifetimes;

namespace RemoteNotifications.Surface.Services;

public sealed record RemoteNotificationToastEnvelope(
    string MessageId,
    string Title,
    string Body,
    string Scenario,
    string Tag,
    string Group,
    string LaunchUri)
{
    public string ToXml()
    {
        var scenario = Scenario.Length > 0
            ? $" scenario=\"{Escape(Scenario)}\""
            : "";
        return $"<toast{scenario} activationType=\"protocol\" launch=\"{Escape(LaunchUri)}\">" +
               "<visual><binding template=\"ToastGeneric\">" +
               $"<text>{Escape(Title)}</text><text>{Escape(Body)}</text>" +
               "</binding></visual><audio silent=\"true\"/></toast>";
    }

    private static string Escape(string value) => SecurityElement.Escape(value) ?? "";
}

public sealed record RemoteNotificationToastPublishResult(bool Shown, string State, string Error = "");

public interface IRemoteNotificationToastPublisher
{
    Task<RemoteNotificationToastPublishResult> PublishAsync(
        RemoteNotificationRecord notification,
        string messageId,
        bool persistent,
        CancellationToken cancellationToken = default);
}

public interface IRemoteNotificationToastPlatform
{
    RemoteNotificationToastPublishResult Show(RemoteNotificationToastEnvelope envelope);

    bool ClearHistory();
}

public sealed class RemoteNotificationNoopToastPublisher : IRemoteNotificationToastPublisher
{
    public Task<RemoteNotificationToastPublishResult> PublishAsync(
        RemoteNotificationRecord notification,
        string messageId,
        bool persistent,
        CancellationToken cancellationToken = default)
    {
        cancellationToken.ThrowIfCancellationRequested();
        return Task.FromResult(new RemoteNotificationToastPublishResult(false, "disabled"));
    }
}

public sealed class RemoteNotificationWindowsToastPublisher : IRemoteNotificationToastPublisher
{
    private readonly IRemoteNotificationToastPlatform _platform;

    public RemoteNotificationWindowsToastPublisher(IRemoteNotificationToastPlatform? platform = null)
    {
        _platform = platform ?? new WindowsRemoteNotificationToastPlatform();
    }

    public Task<RemoteNotificationToastPublishResult> PublishAsync(
        RemoteNotificationRecord notification,
        string messageId,
        bool persistent,
        CancellationToken cancellationToken = default)
    {
        cancellationToken.ThrowIfCancellationRequested();
        var envelope = BuildEnvelope(notification, messageId, persistent);
        try
        {
            return Task.FromResult(_platform.Show(envelope));
        }
        catch (Exception exception)
        {
            return Task.FromResult(new RemoteNotificationToastPublishResult(false, "error", exception.Message));
        }
    }

    public static RemoteNotificationToastEnvelope BuildEnvelope(
        RemoteNotificationRecord notification,
        string messageId,
        bool persistent)
    {
        var sourceMessage = notification.Message ?? "";
        var label = RemoteNotificationsLegacyStore.ExtractLabel(sourceMessage);
        var hasLabel = !string.Equals(label, "(unlabeled)", StringComparison.Ordinal);
        var prefix = hasLabel ? $"[{label}]" : "";
        var title = hasLabel
            ? label
            : string.IsNullOrWhiteSpace(notification.Channel) ? "Notification" : notification.Channel;
        var body = hasLabel && sourceMessage.StartsWith(prefix, StringComparison.Ordinal)
            ? sourceMessage[prefix.Length..].TrimStart()
            : sourceMessage;
        title = NormalizeToastText(title, 140);
        body = NormalizeToastText(body, 900);
        var stableId = string.IsNullOrWhiteSpace(messageId)
            ? RemoteNotificationsLegacyStore.StableId(notification)
            : messageId;
        return new RemoteNotificationToastEnvelope(
            stableId,
            title.Length > 0 ? title : "MyPowerTools",
            body,
            persistent ? "reminder" : "",
            stableId.Length <= 16 ? stableId : stableId[..16],
            "page1",
            $"mypowertools://remote-notification?id={Uri.EscapeDataString(stableId)}");
    }

    private static string NormalizeToastText(string value, int maximumLength)
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

public static class RemoteNotificationToastPublisherFactory
{
    public static IRemoteNotificationToastPublisher CreateForCurrentRuntime()
    {
        return OperatingSystem.IsWindows() &&
               Application.Current?.ApplicationLifetime is IClassicDesktopStyleApplicationLifetime
            ? new RemoteNotificationWindowsToastPublisher()
            : new RemoteNotificationNoopToastPublisher();
    }
}
