using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text.Json;
using MyPowerTools.Platform.Abstractions;

// Program.cs intentionally resolves this service-local type before the imported
// MyPowerTools.Platform.Mac type. Remote Notifications needs a synchronous delivery
// acknowledgement so its worker never reports a banner as shown while macOS is still
// deciding authorization or while addNotificationRequest later fails.
internal sealed class MacUserNotificationService : INotificationService
{
    private const string TestBackendFlag = "MPT_REMOTE_NOTIFICATIONS_ALLOW_TEST_BACKEND";
    private const string NotificationModeVariable = "MPT_REMOTE_NOTIFICATIONS_NOTIFICATION_MODE";
    private const string NotificationRecordPathVariable = "MPT_REMOTE_NOTIFICATIONS_NOTIFICATION_RECORD_PATH";
    private const int NativeTimeoutMilliseconds = 10000;

    private const int NativeOk = 0;
    private const int NativeOsUnsupported = -1;
    private const int NativeAuthorizationDenied = -30;
    private const int NativeRequestFailed = -31;
    private const int NativeTimedOut = -32;
    private const int NativeNoBundle = 2;
    private const int NativeUnavailable = 3;

    private readonly int _initializationStatus;

    public MacUserNotificationService()
    {
        if (!OperatingSystem.IsMacOS())
        {
            _initializationStatus = NativeOsUnsupported;
            return;
        }

        try
        {
            // Apple requires the UNUserNotificationCenter delegate to be registered during
            // application launch. Program.cs creates this service before the polling loop and
            // control socket start, so notification actions remain routable even when the first
            // message arrives much later. The recording backend still performs this check: the
            // production gate must prove the worker runs from its real helper bundle and loads
            // the native bridge before it may substitute deterministic notification storage.
            _initializationStatus = Native.Initialize();
        }
        catch (DllNotFoundException)
        {
            _initializationStatus = NativeUnavailable;
        }
        catch (EntryPointNotFoundException)
        {
            _initializationStatus = NativeUnavailable;
        }
    }

    public Task PublishAsync(string title, string body, CancellationToken cancellationToken)
    {
        return PublishAsync(
            new DesktopNotificationRequest(Guid.NewGuid().ToString("N"), title, body),
            cancellationToken);
    }

    public async Task PublishAsync(
        DesktopNotificationRequest request,
        CancellationToken cancellationToken)
    {
        ArgumentNullException.ThrowIfNull(request);
        cancellationToken.ThrowIfCancellationRequested();
        if (!OperatingSystem.IsMacOS())
        {
            throw new PlatformNotSupportedException("Remote Notifications native banners require macOS.");
        }

        if (UseRecordingBackend())
        {
            EnsureNativeInitializationSucceeded();
            await RecordNotificationAsync(request, cancellationToken).ConfigureAwait(false);
            return;
        }

        var identifier = string.IsNullOrWhiteSpace(request.Id)
            ? Guid.NewGuid().ToString("N")
            : request.Id;
        var title = request.Title ?? "";
        var body = request.Body ?? "";
        var activationUri = request.ActivationUri ?? "";

        var status = _initializationStatus;
        if (status == NativeOk)
        {
            try
            {
                status = await Task.Run(
                        () => Native.Publish(
                            identifier,
                            title,
                            body,
                            activationUri,
                            NativeTimeoutMilliseconds),
                        cancellationToken)
                    .ConfigureAwait(false);
            }
            catch (DllNotFoundException)
            {
                status = NativeUnavailable;
            }
            catch (EntryPointNotFoundException)
            {
                status = NativeUnavailable;
            }
        }

        if (status == NativeOk)
        {
            return;
        }

        if (status == NativeAuthorizationDenied)
        {
            throw new InvalidOperationException(
                "macOS notification permission is denied for MyPowerTools. Enable notifications in System Settings, then retry.");
        }

        // Preserve message delivery when UserNotifications is temporarily unavailable. The
        // worker logs every fallback because osascript cannot provide exact-message activation.
        if (status is NativeRequestFailed or NativeTimedOut)
        {
            Console.Error.WriteLine(
                $"RemoteNotifications.Service native notification status {status}; using osascript fallback.");
            await PublishThroughOsascriptAsync(title, body, cancellationToken).ConfigureAwait(false);
            return;
        }

        throw new InvalidOperationException(NativeInitializationFailure(status));
    }

    private void EnsureNativeInitializationSucceeded()
    {
        if (_initializationStatus != NativeOk)
        {
            throw new InvalidOperationException(
                $"The macOS production test backend refused to run: {NativeInitializationFailure(_initializationStatus)}");
        }
    }

    private static string NativeInitializationFailure(int status)
    {
        return status switch
        {
            NativeNoBundle =>
                "Remote Notifications is not running from the signed MyPowerTools helper bundle, so macOS notification identity is unavailable.",
            NativeUnavailable =>
                "The Remote Notifications native notification bridge could not be loaded from the helper bundle.",
            NativeOsUnsupported =>
                "The installed macOS version does not support the required notification API.",
            _ => $"macOS rejected notification initialization with status {status}."
        };
    }

    private static bool UseRecordingBackend()
    {
        return string.Equals(
                   Environment.GetEnvironmentVariable(TestBackendFlag),
                   "1",
                   StringComparison.Ordinal) &&
               string.Equals(
                   Environment.GetEnvironmentVariable(NotificationModeVariable),
                   "record",
                   StringComparison.OrdinalIgnoreCase);
    }

    private static async Task RecordNotificationAsync(
        DesktopNotificationRequest request,
        CancellationToken cancellationToken)
    {
        var path = Environment.GetEnvironmentVariable(NotificationRecordPathVariable);
        if (string.IsNullOrWhiteSpace(path))
        {
            throw new InvalidOperationException(
                $"{NotificationRecordPathVariable} is required when the test notification backend is enabled.");
        }

        path = Path.GetFullPath(Environment.ExpandEnvironmentVariables(path));
        Directory.CreateDirectory(Path.GetDirectoryName(path)
            ?? throw new InvalidOperationException("Notification record directory could not be resolved."));
        var payload = JsonSerializer.SerializeToUtf8Bytes(new
        {
            request.Id,
            request.Title,
            request.Body,
            request.ActivationUri,
            PublishedAt = DateTimeOffset.UtcNow.ToString("O")
        });

        await using var stream = new FileStream(
            path,
            FileMode.Append,
            FileAccess.Write,
            FileShare.Read,
            bufferSize: 4096,
            FileOptions.Asynchronous | FileOptions.WriteThrough);
        await stream.WriteAsync(payload, cancellationToken).ConfigureAwait(false);
        await stream.WriteAsync("\n"u8.ToArray(), cancellationToken).ConfigureAwait(false);
        await stream.FlushAsync(cancellationToken).ConfigureAwait(false);
    }

    private static async Task PublishThroughOsascriptAsync(
        string title,
        string body,
        CancellationToken cancellationToken)
    {
        var script = $"display notification {EscapeAppleScriptString(body)} with title {EscapeAppleScriptString(title)}";
        var startInfo = new ProcessStartInfo
        {
            FileName = "/usr/bin/osascript",
            UseShellExecute = false,
            RedirectStandardError = true,
            CreateNoWindow = true
        };
        startInfo.ArgumentList.Add("-e");
        startInfo.ArgumentList.Add(script);

        using var process = Process.Start(startInfo)
            ?? throw new InvalidOperationException("Could not start osascript to publish the notification.");
        var errorTask = process.StandardError.ReadToEndAsync(cancellationToken);
        await process.WaitForExitAsync(cancellationToken).ConfigureAwait(false);
        if (process.ExitCode != 0)
        {
            var error = (await errorTask.ConfigureAwait(false)).Trim();
            throw new InvalidOperationException(
                error.Length > 0
                    ? $"osascript could not publish the notification: {error}"
                    : $"osascript could not publish the notification with exit code {process.ExitCode}.");
        }
    }

    private static string EscapeAppleScriptString(string value)
    {
        return "\"" + (value ?? "")
            .Replace("\\", "\\\\", StringComparison.Ordinal)
            .Replace("\"", "\\\"", StringComparison.Ordinal) + "\"";
    }

    private static class Native
    {
        [DllImport(
            "RemoteNotificationsMac",
            EntryPoint = "remote_notifications_mac_initialize",
            CallingConvention = CallingConvention.Cdecl)]
        internal static extern int Initialize();

        [DllImport(
            "RemoteNotificationsMac",
            EntryPoint = "remote_notifications_mac_publish",
            CallingConvention = CallingConvention.Cdecl)]
        internal static extern int Publish(
            [MarshalAs(UnmanagedType.LPUTF8Str)] string identifier,
            [MarshalAs(UnmanagedType.LPUTF8Str)] string title,
            [MarshalAs(UnmanagedType.LPUTF8Str)] string body,
            [MarshalAs(UnmanagedType.LPUTF8Str)] string activationUri,
            int timeoutMilliseconds);
    }
}
