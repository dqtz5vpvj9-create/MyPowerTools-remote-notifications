using System.Text.Json;
using MyPowerTools.Platform.Abstractions;
using MyPowerTools.Platform.Mac;

namespace RemoteNotifications.Service;

internal sealed record RemoteNotificationDeliverySnapshot(
    string State,
    string Authorization,
    string Error,
    string LastDeliveredAtUtc,
    string LastMessageId);

/// <summary>
/// Creates the desktop notification publisher used by the background worker and records
/// delivery diagnostics returned by the control socket. Production macOS uses
/// UserNotifications. CI may select the recording provider to exercise the complete polling
/// and persistence path without requiring an interactive Notification Center session.
/// </summary>
internal static class RemoteNotificationDesktopServiceFactory
{
    private static readonly object Gate = new();
    private static RemoteNotificationDeliverySnapshot _snapshot = new(
        "not-attempted",
        "unknown",
        "",
        "",
        "");

    public static INotificationService? Create()
    {
        if (string.Equals(
            Environment.GetEnvironmentVariable("MPT_REMOTE_NOTIFICATIONS_DISABLE_DESKTOP"),
            "1",
            StringComparison.Ordinal))
        {
            SetSnapshot(new RemoteNotificationDeliverySnapshot(
                "disabled", "disabled", "", "", ""));
            return null;
        }

        var recordPath = Environment.GetEnvironmentVariable(
            "MPT_REMOTE_NOTIFICATIONS_NOTIFICATION_RECORD_PATH");
        if (!string.IsNullOrWhiteSpace(recordPath))
        {
            SetSnapshot(new RemoteNotificationDeliverySnapshot(
                "recording", "test-provider", "", "", ""));
            return new RecordingNotificationService(
                Path.GetFullPath(Environment.ExpandEnvironmentVariables(recordPath)));
        }

        if (!OperatingSystem.IsMacOS())
        {
            return null;
        }

        var service = new MacUserNotificationService();
        var authorization = service.GetAuthorizationStatus();
        SetSnapshot(new RemoteNotificationDeliverySnapshot(
            authorization switch
            {
                "authorized" or "provisional" => "ready",
                "denied" => "permission-denied",
                "not-determined" => "permission-not-requested",
                _ => "unavailable"
            },
            authorization,
            authorization == "denied"
                ? "macOS notification permission is disabled for MyPowerTools."
                : "",
            "",
            ""));
        return new ReportingNotificationService(service);
    }

    public static RemoteNotificationDeliverySnapshot Snapshot()
    {
        lock (Gate)
        {
            return _snapshot;
        }
    }

    private static void RecordSuccess(
        string messageId,
        string authorization,
        string state = "delivered")
    {
        SetSnapshot(new RemoteNotificationDeliverySnapshot(
            state,
            authorization,
            "",
            DateTimeOffset.UtcNow.ToString("O"),
            messageId));
    }

    private static void RecordFailure(
        string messageId,
        string authorization,
        Exception exception)
    {
        SetSnapshot(new RemoteNotificationDeliverySnapshot(
            exception is UnauthorizedAccessException
                ? "permission-denied"
                : "delivery-failed",
            authorization,
            exception.Message,
            "",
            messageId));
    }

    private static void SetSnapshot(RemoteNotificationDeliverySnapshot snapshot)
    {
        lock (Gate)
        {
            _snapshot = snapshot;
        }
        WriteDiagnosticFile(snapshot);
    }

    private static void WriteDiagnosticFile(RemoteNotificationDeliverySnapshot snapshot)
    {
        var dataRoot = Environment.GetEnvironmentVariable("MPT_TOOL_DATA_ROOT");
        if (string.IsNullOrWhiteSpace(dataRoot))
        {
            return;
        }

        try
        {
            var root = Path.GetFullPath(Environment.ExpandEnvironmentVariables(dataRoot));
            Directory.CreateDirectory(root);
            var path = Path.Combine(root, "notification-delivery.json");
            var temporary = $"{path}.{Environment.ProcessId}.tmp";
            File.WriteAllText(
                temporary,
                JsonSerializer.Serialize(
                    snapshot,
                    new JsonSerializerOptions { WriteIndented = true }));
            File.Move(temporary, path, overwrite: true);
            if (!OperatingSystem.IsWindows())
            {
                File.SetUnixFileMode(
                    path,
                    UnixFileMode.UserRead | UnixFileMode.UserWrite);
            }
        }
        catch (Exception exception) when (
            exception is IOException or UnauthorizedAccessException or
                PlatformNotSupportedException)
        {
            try
            {
                Console.Error.WriteLine(
                    $"RemoteNotifications.Service could not write notification diagnostics: " +
                    exception.Message);
            }
            catch
            {
            }
        }
    }

    private sealed class ReportingNotificationService(
        MacUserNotificationService inner) : INotificationService
    {
        public Task PublishAsync(
            string title,
            string body,
            CancellationToken cancellationToken)
        {
            return PublishAsync(
                new DesktopNotificationRequest(
                    Guid.NewGuid().ToString("N"), title, body),
                cancellationToken);
        }

        public async Task PublishAsync(
            DesktopNotificationRequest request,
            CancellationToken cancellationToken)
        {
            var authorization = inner.GetAuthorizationStatus();
            try
            {
                await inner.PublishAsync(request, cancellationToken).ConfigureAwait(false);
                authorization = inner.GetAuthorizationStatus();
                RecordSuccess(request.Id, authorization);
            }
            catch (Exception exception) when (
                exception is UnauthorizedAccessException or InvalidOperationException or
                    DllNotFoundException or EntryPointNotFoundException)
            {
                authorization = inner.GetAuthorizationStatus();
                RecordFailure(request.Id, authorization, exception);
                throw;
            }
        }
    }

    private sealed class RecordingNotificationService(string path) : INotificationService
    {
        private readonly object _writeGate = new();

        public Task PublishAsync(
            string title,
            string body,
            CancellationToken cancellationToken)
        {
            return PublishAsync(
                new DesktopNotificationRequest(
                    Guid.NewGuid().ToString("N"), title, body),
                cancellationToken);
        }

        public Task PublishAsync(
            DesktopNotificationRequest request,
            CancellationToken cancellationToken)
        {
            cancellationToken.ThrowIfCancellationRequested();
            var directory = Path.GetDirectoryName(path)
                ?? throw new InvalidOperationException(
                    "Notification recording path has no parent directory.");
            Directory.CreateDirectory(directory);
            var line = JsonSerializer.Serialize(new
            {
                recordedAtUtc = DateTimeOffset.UtcNow.ToString("O"),
                id = request.Id,
                title = request.Title,
                body = request.Body,
                activationUri = request.ActivationUri ?? ""
            });

            lock (_writeGate)
            {
                File.AppendAllText(path, line + Environment.NewLine);
                if (!OperatingSystem.IsWindows())
                {
                    File.SetUnixFileMode(
                        path,
                        UnixFileMode.UserRead | UnixFileMode.UserWrite);
                }
            }

            RecordSuccess(request.Id, "test-provider", "recorded");
            return Task.CompletedTask;
        }
    }
}
