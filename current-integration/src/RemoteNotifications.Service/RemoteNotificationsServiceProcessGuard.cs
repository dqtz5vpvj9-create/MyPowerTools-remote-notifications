using System.Runtime.CompilerServices;
using System.Text;
using System.Text.Json;

namespace RemoteNotifications.Service;

/// <summary>
/// Prevents two Remote Notifications workers from owning the signed-pull waterline and
/// desktop-notification stream at the same time. Protocol-activation invocations bypass
/// the worker lock because they only forward one notification click to MyPowerTools and exit.
/// </summary>
internal static class RemoteNotificationsServiceProcessGuard
{
    private const string ActivationArgument = "--remote-notification-activation";
    private static FileStream? _lockStream;

    [ModuleInitializer]
    internal static void Acquire()
    {
        if (IsActivationInvocation(Environment.GetCommandLineArgs()))
        {
            return;
        }

        var dataRoot = ResolveDataRoot();
        Directory.CreateDirectory(dataRoot);
        HardenDirectory(dataRoot);

        var lockPath = Path.Combine(dataRoot, "remote-notifications.service.lock");
        try
        {
            _lockStream = new FileStream(
                lockPath,
                FileMode.OpenOrCreate,
                FileAccess.ReadWrite,
                FileShare.Read,
                bufferSize: 4096,
                FileOptions.WriteThrough);
        }
        catch (IOException exception)
        {
            Console.Error.WriteLine(
                $"RemoteNotifications.Service refused a second polling owner: {exception.Message}");
            Environment.Exit(17);
            return;
        }

        HardenFile(lockPath);
        var owner = JsonSerializer.SerializeToUtf8Bytes(new
        {
            pid = Environment.ProcessId,
            startedAtUtc = DateTimeOffset.UtcNow.ToString("O")
        });
        _lockStream.SetLength(0);
        _lockStream.Write(owner);
        _lockStream.Write(Encoding.UTF8.GetBytes(Environment.NewLine));
        _lockStream.Flush(flushToDisk: true);
        AppDomain.CurrentDomain.ProcessExit += (_, _) => Release();
    }

    internal static bool IsActivationInvocation(IReadOnlyList<string> arguments)
    {
        for (var index = 1; index < arguments.Count; index++)
        {
            var argument = arguments[index];
            if (string.Equals(argument, ActivationArgument, StringComparison.OrdinalIgnoreCase))
            {
                return index + 1 < arguments.Count &&
                    !string.IsNullOrWhiteSpace(arguments[index + 1]);
            }

            var prefix = ActivationArgument + "=";
            if (argument.StartsWith(prefix, StringComparison.OrdinalIgnoreCase) &&
                !string.IsNullOrWhiteSpace(argument[prefix.Length..]))
            {
                return true;
            }
        }

        return false;
    }

    private static string ResolveDataRoot()
    {
        var configured = Environment.GetEnvironmentVariable("MPT_TOOL_DATA_ROOT");
        if (!string.IsNullOrWhiteSpace(configured))
        {
            return ExpandPath(configured);
        }

        var localAppData = Environment.GetFolderPath(
            Environment.SpecialFolder.LocalApplicationData);
        if (string.IsNullOrWhiteSpace(localAppData))
        {
            localAppData = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
                ".mypowertools");
        }

        return Path.Combine(localAppData, "MyPowerTools", "RemoteNotifications");
    }

    private static string ExpandPath(string path)
    {
        var expanded = Environment.ExpandEnvironmentVariables(path.Trim());
        if (expanded == "~")
        {
            expanded = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
        }
        else if (expanded.StartsWith("~/", StringComparison.Ordinal) ||
                 expanded.StartsWith("~\\", StringComparison.Ordinal))
        {
            expanded = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
                expanded[2..]);
        }

        return Path.GetFullPath(expanded);
    }

    private static void HardenDirectory(string path)
    {
        if (OperatingSystem.IsWindows())
        {
            return;
        }

        try
        {
            File.SetUnixFileMode(
                path,
                UnixFileMode.UserRead |
                UnixFileMode.UserWrite |
                UnixFileMode.UserExecute);
        }
        catch (Exception exception) when (
            exception is IOException or UnauthorizedAccessException or
                PlatformNotSupportedException)
        {
            Console.Error.WriteLine(
                $"RemoteNotifications.Service could not harden data directory permissions: " +
                exception.Message);
        }
    }

    private static void HardenFile(string path)
    {
        if (OperatingSystem.IsWindows())
        {
            return;
        }

        try
        {
            File.SetUnixFileMode(
                path,
                UnixFileMode.UserRead |
                UnixFileMode.UserWrite);
        }
        catch (Exception exception) when (
            exception is IOException or UnauthorizedAccessException or
                PlatformNotSupportedException)
        {
            Console.Error.WriteLine(
                $"RemoteNotifications.Service could not harden lock-file permissions: " +
                exception.Message);
        }
    }

    private static void Release()
    {
        Interlocked.Exchange(ref _lockStream, null)?.Dispose();
    }
}
