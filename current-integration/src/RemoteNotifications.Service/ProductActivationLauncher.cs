using System.Diagnostics;
using System.Buffers.Binary;
using System.IO.Pipes;
using System.Runtime.InteropServices;
using System.Text.Json;
using Microsoft.Win32.SafeHandles;
using MyPowerTools.Abstractions;

namespace RemoteNotifications.Service;

internal static class ProductActivationLauncher
{
    public const string ArgumentName = "--remote-notification-activation";
    public const string ToolId = "remote-notifications";
    public const string RouteId = "inbox";
    private const string InstallRootEnvironmentVariable = "MPT_INSTALL_ROOT";

    public static string? GetLaunchUri(IReadOnlyList<string> arguments)
    {
        for (var index = 0; index < arguments.Count - 1; index++)
        {
            if (string.Equals(arguments[index], ArgumentName, StringComparison.OrdinalIgnoreCase))
            {
                return arguments[index + 1].Trim().Trim('"');
            }
        }

        var prefix = ArgumentName + "=";
        var combined = arguments.FirstOrDefault(argument =>
            argument.StartsWith(prefix, StringComparison.OrdinalIgnoreCase));
        if (!string.IsNullOrWhiteSpace(combined))
        {
            return combined[prefix.Length..].Trim().Trim('"');
        }

        return null;
    }

    public static string? ResolveProductExecutable()
    {
        var configuredRoot = Environment.GetEnvironmentVariable(InstallRootEnvironmentVariable);
        if (!string.IsNullOrWhiteSpace(configuredRoot))
        {
            var configuredExecutable = Path.Combine(configuredRoot, ExecutableName("MyPowerTools"));
            if (File.Exists(configuredExecutable))
            {
                return configuredExecutable;
            }
        }

        var directory = new DirectoryInfo(AppContext.BaseDirectory);
        while (directory is not null)
        {
            var installedExecutable = Path.Combine(directory.FullName, ExecutableName("MyPowerTools"));
            var installedShell = Path.Combine(directory.FullName, "Shell", ExecutableName("MyPowerTools.Shell.Avalonia"));
            if (File.Exists(installedExecutable) && File.Exists(installedShell))
            {
                return installedExecutable;
            }

            if (File.Exists(Path.Combine(directory.FullName, "MyPowerTools.slnx")))
            {
                var releaseBuild = AppContext.BaseDirectory.Contains(
                    $"{Path.DirectorySeparatorChar}Release{Path.DirectorySeparatorChar}",
                    StringComparison.OrdinalIgnoreCase);
                foreach (var configuration in releaseBuild
                             ? new[] { "Release", "Debug" }
                             : new[] { "Debug", "Release" })
                {
                    var developmentExecutable = Path.Combine(
                        directory.FullName,
                        "src",
                        "MyPowerTools.App",
                        "bin",
                        configuration,
                        "net10.0",
                        ExecutableName("MyPowerTools"));
                    if (File.Exists(developmentExecutable))
                    {
                        return developmentExecutable;
                    }
                }
            }

            directory = directory.Parent;
        }

        var localProgramsExecutable = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "Programs",
            "MyPowerTools",
            ExecutableName("MyPowerTools"));
        return File.Exists(localProgramsExecutable) ? localProgramsExecutable : null;
    }

    private static string ExecutableName(string baseName) =>
        OperatingSystem.IsWindows() ? baseName + ".exe" : baseName;

    public static bool TryLaunch(string launchUri)
    {
        var activation = new ToolActivationRequest(ToolId, RouteId, launchUri)
        {
            SuppressShellWindow = true
        };
        if (TryActivateRunningShellAsync(activation).GetAwaiter().GetResult())
        {
            return true;
        }

        var executable = ResolveProductExecutable();
        if (string.IsNullOrWhiteSpace(executable))
        {
            return false;
        }

        try
        {
            var startInfo = new ProcessStartInfo
            {
                FileName = executable,
                WorkingDirectory = Path.GetDirectoryName(executable) ?? AppContext.BaseDirectory,
                UseShellExecute = false
            };
            startInfo.ArgumentList.Add(ToolActivationProtocol.ArgumentName);
            startInfo.ArgumentList.Add(ToolActivationProtocol.Serialize(activation));
            var process = Process.Start(startInfo);
            if (process is null)
            {
                return false;
            }

            TransferForegroundPermission(process.Id);
            return true;
        }
        catch
        {
            return false;
        }
    }

    internal static async Task<bool> TryActivateRunningShellAsync(
        ToolActivationRequest activation,
        string pipeName = "MyPowerTools.ShellActivation",
        TimeSpan? timeout = null)
    {
        if (!OperatingSystem.IsWindows()) return false;

        try
        {
            using var cancellation = new CancellationTokenSource(timeout ?? TimeSpan.FromSeconds(1));
            await using var pipe = new NamedPipeClientStream(
                ".", pipeName, PipeDirection.InOut, PipeOptions.Asynchronous);
            using var connectTimeout = CancellationTokenSource.CreateLinkedTokenSource(cancellation.Token);
            connectTimeout.CancelAfter(TimeSpan.FromMilliseconds(200));
            await pipe.ConnectAsync(connectTimeout.Token).ConfigureAwait(false);

            // The protocol process owns the user's foreground activation. Grant it to
            // the actual window host BEFORE delivering the request. A fast launcher
            // can otherwise forward and exit before receiving this permission.
            if (GetNamedPipeServerProcessId(pipe.SafePipeHandle, out var shellProcessId))
            {
                TransferForegroundPermission((int)shellProcessId);
            }

            var payload = JsonSerializer.SerializeToUtf8Bytes(new
            {
                ToolActivation = activation,
                ShowShell = !activation.SuppressShellWindow,
                ShutdownShell = false
            });
            var header = new byte[sizeof(int)];
            BinaryPrimitives.WriteInt32LittleEndian(header, payload.Length);
            await pipe.WriteAsync(header, cancellation.Token).ConfigureAwait(false);
            await pipe.WriteAsync(payload, cancellation.Token).ConfigureAwait(false);
            await pipe.FlushAsync(cancellation.Token).ConfigureAwait(false);
            var response = new byte[1];
            return await pipe.ReadAsync(response, cancellation.Token).ConfigureAwait(false) == 1 &&
                   response[0] == 0x06;
        }
        catch (Exception exception) when (exception is IOException or TimeoutException or
                                          OperationCanceledException or UnauthorizedAccessException)
        {
            // An absent or unresponsive Shell still gets the normal cold-start path.
            return false;
        }
    }

    private static void TransferForegroundPermission(int processId)
    {
        if (OperatingSystem.IsWindows() && processId > 0)
        {
            _ = AllowSetForegroundWindow((uint)processId);
        }
    }

    [DllImport("user32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool AllowSetForegroundWindow(uint processId);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GetNamedPipeServerProcessId(SafePipeHandle pipe, out uint serverProcessId);
}
