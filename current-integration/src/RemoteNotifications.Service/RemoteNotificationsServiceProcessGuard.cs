using System.Runtime.CompilerServices;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json;
using MyPowerTools.Abstractions;

namespace RemoteNotifications.Service;

/// <summary>
/// Prevents two Remote Notifications workers from owning the signed-pull waterline and
/// desktop-notification stream at the same time. Protocol-activation invocations bypass
/// the worker lock because they only forward one notification click to MyPowerTools and exit.
/// </summary>
internal static class RemoteNotificationsServiceProcessGuard
{
    private const string LegacyActivationArgument = "--remote-notification-activation";
    private const uint OwnerOnlyMask = 0x3F; // octal 077
    private static FileStream? _lockStream;

    [ModuleInitializer]
    internal static void Acquire()
    {
        if (IsActivationInvocation(Environment.GetCommandLineArgs()))
        {
            return;
        }

        ApplyOwnerOnlyUmask();

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
                FileShare.None,
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
        AppDomain.CurrentDomain.ProcessExit += static (_, _) => Release();
    }

    internal static bool IsActivationInvocation(IReadOnlyList<string> arguments)
    {
        return HasArgument(arguments, ToolActivationProtocol.ArgumentName) ||
               HasArgument(arguments, LegacyActivationArgument);
    }

    private static bool HasArgument(IReadOnlyList<string> arguments, string argumentName)
    {
        var prefix = argumentName + "=";
        for (var index = 1; index < arguments.Count; index++)
        {
            var argument = arguments[index];
            if (string.Equals(argument, argumentName, StringComparison.OrdinalIgnoreCase))
            {
                return index + 1 < arguments.Count &&
                       !string.IsNullOrWhiteSpace(arguments[index + 1]);
            }

            if (argument.StartsWith(prefix, StringComparison.OrdinalIgnoreCase) &&
                !string.IsNullOrWhiteSpace(argument[prefix.Length..]))
            {
                return true;
            }
        }

        return false;
    }

    private static void ApplyOwnerOnlyUmask()
    {
        if (OperatingSystem.IsMacOS())
        {
            _ = MacUmask(OwnerOnlyMask);
        }
        else if (OperatingSystem.IsLinux())
        {
            _ = LinuxUmask(OwnerOnlyMask);
        }
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

    [DllImport("libSystem.B.dylib", EntryPoint = "umask", CallingConvention = CallingConvention.Cdecl)]
    private static extern uint MacUmask(uint mask);

    [DllImport("libc", EntryPoint = "umask", CallingConvention = CallingConvention.Cdecl)]
    private static extern uint LinuxUmask(uint mask);
}
