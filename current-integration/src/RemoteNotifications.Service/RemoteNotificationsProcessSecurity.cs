using System.Runtime.CompilerServices;
using System.Runtime.InteropServices;
using System.Text;
using MyPowerTools.RemoteNotifications.Configuration;

namespace RemoteNotifications.Service;

/// <summary>
/// Establishes process-wide filesystem security before the worker creates its
/// control socket or persistent state, and holds an exclusive single-instance
/// lock for the lifetime of the polling process.
/// </summary>
internal static class RemoteNotificationsProcessSecurity
{
    private const string ActivationArgument = "--remote-notification-activation";
    private const uint OwnerOnlyMask = 0x3F; // octal 077
    private static FileStream? _instanceLock;

    [ModuleInitializer]
    internal static void Initialize()
    {
        if (IsActivationProcess(Environment.GetCommandLineArgs()))
        {
            return;
        }

        if (!OperatingSystem.IsWindows())
        {
            _ = Umask(OwnerOnlyMask);
        }

        var dataRoot = ResolveDataRoot();
        Directory.CreateDirectory(dataRoot);
        if (!OperatingSystem.IsWindows())
        {
            File.SetUnixFileMode(
                dataRoot,
                UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute);
        }

        var lockPath = Path.Combine(dataRoot, "remote-notifications.service.lock");
        try
        {
            _instanceLock = new FileStream(
                lockPath,
                FileMode.OpenOrCreate,
                FileAccess.ReadWrite,
                FileShare.None,
                bufferSize: 4096,
                FileOptions.WriteThrough);
        }
        catch (IOException exception)
        {
            throw new InvalidOperationException(
                $"Another Remote Notifications worker already owns '{lockPath}'.",
                exception);
        }

        if (!OperatingSystem.IsWindows())
        {
            File.SetUnixFileMode(lockPath, UnixFileMode.UserRead | UnixFileMode.UserWrite);
        }

        _instanceLock.SetLength(0);
        using (var writer = new StreamWriter(
                   _instanceLock,
                   new UTF8Encoding(encoderShouldEmitUTF8Identifier: false),
                   bufferSize: 1024,
                   leaveOpen: true))
        {
            writer.WriteLine($"pid={Environment.ProcessId}");
            writer.WriteLine($"startedAt={DateTimeOffset.UtcNow:O}");
            writer.Flush();
        }
        _instanceLock.Flush(flushToDisk: true);
        AppDomain.CurrentDomain.ProcessExit += static (_, _) => Release();
    }

    private static bool IsActivationProcess(IReadOnlyList<string> arguments)
    {
        var prefix = ActivationArgument + "=";
        return arguments.Any(argument =>
            string.Equals(argument, ActivationArgument, StringComparison.OrdinalIgnoreCase) ||
            argument.StartsWith(prefix, StringComparison.OrdinalIgnoreCase));
    }

    private static string ResolveDataRoot()
    {
        var configured = Environment.GetEnvironmentVariable("MPT_TOOL_DATA_ROOT");
        if (!string.IsNullOrWhiteSpace(configured))
        {
            return Path.GetFullPath(Environment.ExpandEnvironmentVariables(configured));
        }

        return Path.GetDirectoryName(RemoteNotificationSettingsStore.GetDefaultSettingsPath())
            ?? throw new InvalidOperationException("Remote Notifications data directory could not be resolved.");
    }

    private static void Release()
    {
        var stream = Interlocked.Exchange(ref _instanceLock, null);
        stream?.Dispose();
    }

    [DllImport("libc", EntryPoint = "umask", CallingConvention = CallingConvention.Cdecl)]
    private static extern uint Umask(uint mask);
}
