using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Runtime.Versioning;
using Microsoft.Win32;

namespace RemoteNotifications.Surface.Services;

public sealed class WindowsRemoteNotificationToastPlatform : IRemoteNotificationToastPlatform
{
    public const string AppUserModelId = "MyPowerTools.Desktop";
    public const string ProtocolScheme = "mypowertools";

    private static readonly object SetupGate = new();
    private static bool _setupComplete;

    public RemoteNotificationToastPublishResult Show(RemoteNotificationToastEnvelope envelope)
    {
        if (!OperatingSystem.IsWindows())
        {
            return new RemoteNotificationToastPublishResult(false, "unsupported");
        }

        try
        {
            EnsureRegistered();
            WindowsToastAbi.Show(envelope.ToXml(), envelope.Tag, envelope.Group, AppUserModelId);
            return new RemoteNotificationToastPublishResult(true, "shown");
        }
        catch (Exception exception)
        {
            return new RemoteNotificationToastPublishResult(false, "error", exception.Message);
        }
    }

    public bool ClearHistory() => false;

    public static RemoteNotificationToastPublishResult ProbeRuntime()
    {
        if (!OperatingSystem.IsWindows())
        {
            return new RemoteNotificationToastPublishResult(false, "unsupported");
        }

        try
        {
            WindowsToastAbi.Probe(AppUserModelId);
            return new RemoteNotificationToastPublishResult(true, "ready");
        }
        catch (Exception exception)
        {
            return new RemoteNotificationToastPublishResult(false, "error", exception.Message);
        }
    }

    [SupportedOSPlatform("windows")]
    private static void EnsureRegistered()
    {
        lock (SetupGate)
        {
            if (_setupComplete)
            {
                return;
            }

            var executable = ResolveShellExecutable();
            Marshal.ThrowExceptionForHR(SetCurrentProcessExplicitAppUserModelID(AppUserModelId));
            EnsureStartMenuShortcut(executable);
            RegisterProtocol(executable);
            _setupComplete = true;
        }
    }

    private static string ResolveShellExecutable()
    {
        var assemblyPath = typeof(WindowsRemoteNotificationToastPlatform).Assembly.Location;
        if (!string.IsNullOrWhiteSpace(assemblyPath))
        {
            var appHost = Path.ChangeExtension(assemblyPath, ".exe");
            if (File.Exists(appHost))
            {
                return appHost;
            }
        }

        return Environment.ProcessPath ?? throw new InvalidOperationException("The Shell executable path is unavailable.");
    }

    [SupportedOSPlatform("windows")]
    private static void RegisterProtocol(string executable)
    {
        var rootPath = $@"Software\Classes\{ProtocolScheme}";
        using (var root = Registry.CurrentUser.CreateSubKey(rootPath, writable: true))
        {
            root.SetValue("", "URL:MyPowerTools Remote Notification", RegistryValueKind.String);
            root.SetValue("URL Protocol", "", RegistryValueKind.String);
        }

        using (var icon = Registry.CurrentUser.CreateSubKey($@"{rootPath}\DefaultIcon", writable: true))
        {
            icon.SetValue("", $"{executable},0", RegistryValueKind.String);
        }

        using var command = Registry.CurrentUser.CreateSubKey($@"{rootPath}\shell\open\command", writable: true);
        command.SetValue(
            "",
            $"\"{executable}\" --remote-notification-activation \"%1\"",
            RegistryValueKind.String);
    }

    [SupportedOSPlatform("windows")]
    private static void EnsureStartMenuShortcut(string executable)
    {
        var folder = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.StartMenu),
            "Programs",
            "MyPowerTools");
        Directory.CreateDirectory(folder);
        var path = Path.Combine(folder, "MyPowerTools.lnk");
        var shortcutExists = File.Exists(path);
        var link = (IShellLinkW)(object)new ShellLink();
        try
        {
            var persist = (IPersistFile)link;
            if (shortcutExists)
            {
                persist.Load(path, 2);
            }
            else
            {
                link.SetPath(executable);
                link.SetWorkingDirectory(Path.GetDirectoryName(executable) ?? AppContext.BaseDirectory);
                link.SetDescription("MyPowerTools");
                link.SetIconLocation(executable, 0);
            }

            var propertyStore = (IPropertyStore)link;
            SetStringProperty(propertyStore, PropertyKeys.AppUserModelId, AppUserModelId);
            if (!shortcutExists)
            {
                SetStringProperty(propertyStore, PropertyKeys.RelaunchCommand, $"\"{executable}\"");
                SetStringProperty(propertyStore, PropertyKeys.RelaunchDisplayNameResource, "MyPowerTools");
                SetStringProperty(propertyStore, PropertyKeys.RelaunchIconResource, $"{executable},0");
            }
            Marshal.ThrowExceptionForHR(propertyStore.Commit());
            persist.Save(path, true);
        }
        finally
        {
            Marshal.FinalReleaseComObject(link);
        }
    }

    private static void SetStringProperty(IPropertyStore store, PropertyKey key, string value)
    {
        using var property = PropVariant.FromString(value);
        var mutableKey = key;
        var mutableProperty = property;
        Marshal.ThrowExceptionForHR(store.SetValue(ref mutableKey, ref mutableProperty));
    }

    [DllImport("shell32.dll", CharSet = CharSet.Unicode)]
    private static extern int SetCurrentProcessExplicitAppUserModelID(string appId);

    [ComImport]
    [Guid("00021401-0000-0000-C000-000000000046")]
    private sealed class ShellLink;

    [ComImport]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    [Guid("000214F9-0000-0000-C000-000000000046")]
    private interface IShellLinkW
    {
        void GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] string file, int maximumPath, nint findData, uint flags);
        void GetIDList(out nint itemIdList);
        void SetIDList(nint itemIdList);
        void GetDescription([Out, MarshalAs(UnmanagedType.LPWStr)] string name, int maximumName);
        void SetDescription([MarshalAs(UnmanagedType.LPWStr)] string name);
        void GetWorkingDirectory([Out, MarshalAs(UnmanagedType.LPWStr)] string directory, int maximumPath);
        void SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string directory);
        void GetArguments([Out, MarshalAs(UnmanagedType.LPWStr)] string arguments, int maximumPath);
        void SetArguments([MarshalAs(UnmanagedType.LPWStr)] string arguments);
        void GetHotkey(out short hotkey);
        void SetHotkey(short hotkey);
        void GetShowCmd(out int showCommand);
        void SetShowCmd(int showCommand);
        void GetIconLocation([Out, MarshalAs(UnmanagedType.LPWStr)] string iconPath, int iconPathLength, out int iconIndex);
        void SetIconLocation([MarshalAs(UnmanagedType.LPWStr)] string iconPath, int iconIndex);
        void SetRelativePath([MarshalAs(UnmanagedType.LPWStr)] string path, uint reserved);
        void Resolve(nint windowHandle, uint flags);
        void SetPath([MarshalAs(UnmanagedType.LPWStr)] string path);
    }

    [ComImport]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    [Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99")]
    private interface IPropertyStore
    {
        [PreserveSig]
        int GetCount(out uint propertyCount);

        [PreserveSig]
        int GetAt(uint propertyIndex, out PropertyKey key);

        [PreserveSig]
        int GetValue(ref PropertyKey key, out PropVariant value);

        [PreserveSig]
        int SetValue(ref PropertyKey key, ref PropVariant value);

        [PreserveSig]
        int Commit();
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct PropertyKey(Guid formatId, uint propertyId)
    {
        public Guid FormatId = formatId;
        public uint PropertyId = propertyId;
    }

    private static class PropertyKeys
    {
        private static readonly Guid AppUserModelFormat = new("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3");
        public static PropertyKey RelaunchCommand => new(AppUserModelFormat, 2);
        public static PropertyKey RelaunchIconResource => new(AppUserModelFormat, 3);
        public static PropertyKey RelaunchDisplayNameResource => new(AppUserModelFormat, 4);
        public static PropertyKey AppUserModelId => new(AppUserModelFormat, 5);
    }

    [StructLayout(LayoutKind.Explicit, Size = 16)]
    private struct PropVariant : IDisposable
    {
        [FieldOffset(0)]
        private ushort _variantType;

        [FieldOffset(8)]
        private nint _pointer;

        public static PropVariant FromString(string value)
        {
            return new PropVariant
            {
                _variantType = 31,
                _pointer = Marshal.StringToCoTaskMemUni(value)
            };
        }

        public void Dispose()
        {
            if (_pointer != 0)
            {
                Marshal.FreeCoTaskMem(_pointer);
                _pointer = 0;
            }
        }
    }
}
