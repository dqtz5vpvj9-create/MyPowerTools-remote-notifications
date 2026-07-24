using System.Diagnostics;
using System.Runtime.InteropServices;
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
        var executable = ResolveProductExecutable();
        if (string.IsNullOrWhiteSpace(executable))
        {
            return false;
        }

        try
        {
            var activation = new ToolActivationRequest(ToolId, RouteId, launchUri)
            {
                SuppressShellWindow = true
            };
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
}
