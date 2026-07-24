using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Runtime.Versioning;
using System.Security;
using Microsoft.Win32;

namespace RemoteNotifications.Service;

// Toast envelope + result + Windows COM platform, free of the Avalonia dependency that the
// Surface's RemoteNotificationToastPublisher carries (it checks Application.Current's lifetime).
// The worker has no Avalonia lifetime, so it talks to the WinRT toast COM ABI directly. The
// XML/template is identical to RemoteNotificationToastEnvelope.ToXml so toasts render the same
// regardless of sender. The COM interop (WindowsToastAbi) is byte-for-byte the Surface impl.
internal sealed record WorkerToastEnvelope(
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

internal sealed record WorkerToastResult(bool Shown, string State, string Error = "");

internal static class WorkerToastPlatform
{
    public const string AppUserModelId = "MyPowerTools.Desktop";
    public const string ProtocolScheme = "mypowertools";

    private static readonly object SetupGate = new();
    private static bool _setupComplete;

    public static WorkerToastResult Show(WorkerToastEnvelope envelope)
    {
        if (!OperatingSystem.IsWindows())
        {
            return new WorkerToastResult(false, "unsupported");
        }

        try
        {
            EnsureRegistered();
            WindowsToastAbi.Show(envelope.ToXml(), envelope.Tag, envelope.Group, AppUserModelId);
            return new WorkerToastResult(true, "shown");
        }
        catch (Exception exception)
        {
            return new WorkerToastResult(false, "error", exception.Message);
        }
    }

    [SupportedOSPlatform("windows")]
    internal static void EnsureRegistered()
    {
        lock (SetupGate)
        {
            if (_setupComplete)
            {
                return;
            }

            var workerExecutable = ResolveExecutable();
            Marshal.ThrowExceptionForHR(SetCurrentProcessExplicitAppUserModelID(AppUserModelId));
            EnsureStartMenuShortcut(workerExecutable);
            RegisterProtocol(workerExecutable);
            _setupComplete = true;
        }
    }

    private static string ResolveExecutable()
    {
        var assemblyName = typeof(WorkerToastPlatform).Assembly.GetName().Name;
        if (!string.IsNullOrWhiteSpace(assemblyName))
        {
            var appHost = Path.Combine(AppContext.BaseDirectory, $"{assemblyName}.exe");
            if (File.Exists(appHost))
            {
                return appHost;
            }
        }

        return Environment.ProcessPath ?? throw new InvalidOperationException("Executable path is unavailable.");
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

// WinRT toast COM ABI. Identical to the Surface's WindowsToastAbi so the same toast renders
// whether sent by the worker or the Shell.
internal static class WindowsToastAbi
{
    private const int Succeeded = 0;
    private const int AlreadyInitialized = 1;
    private const uint RpcChangedMode = 0x80010106;
    private const uint RoInitMultithreaded = 1;

    public static unsafe void Show(string xml, string tag, string group, string appUserModelId)
    {
        CreateNotification(xml, tag, group, appUserModelId, show: true);
    }

    private static unsafe void CreateNotification(
        string xml,
        string tag,
        string group,
        string appUserModelId,
        bool show)
    {
        var initialization = RoInitialize(RoInitMultithreaded);
        var initializationCode = unchecked((uint)initialization);
        if (initialization is not Succeeded and not AlreadyInitialized && initializationCode != RpcChangedMode)
        {
            Marshal.ThrowExceptionForHR(initialization);
        }

        var references = new List<nint>();
        var strings = new List<nint>();
        try
        {
            var xmlDocument = ActivateInstance("Windows.Data.Xml.Dom.XmlDocument", strings);
            references.Add(xmlDocument);
            var xmlDocumentIo = QueryInterface(xmlDocument, new Guid("6CD0E74E-EE65-4489-9EBF-CA43E87BA637"));
            references.Add(xmlDocumentIo);
            var xmlString = CreateString(xml);
            strings.Add(xmlString);
            InvokeOnePointer(xmlDocumentIo, 6, xmlString);

            var notificationFactory = GetActivationFactory(
                "Windows.UI.Notifications.ToastNotification",
                new Guid("04124B20-82C6-4229-B109-FD9ED4662B53"),
                strings);
            references.Add(notificationFactory);
            var notification = InvokeFactory(notificationFactory, 6, xmlDocument);
            references.Add(notification);

            var notification2 = QueryInterface(notification, new Guid("9DFB9FD1-143A-490E-90BF-B9FBA7132DE7"));
            references.Add(notification2);
            var tagString = CreateString(tag.Length <= 16 ? tag : tag[..16]);
            var groupString = CreateString(group.Length <= 64 ? group : group[..64]);
            strings.Add(tagString);
            strings.Add(groupString);
            InvokeOnePointer(notification2, 6, tagString);
            InvokeOnePointer(notification2, 8, groupString);

            var manager = GetActivationFactory(
                "Windows.UI.Notifications.ToastNotificationManager",
                new Guid("50AC103F-D235-4598-BBEF-98FE4D1A3AD4"),
                strings);
            references.Add(manager);
            var appId = CreateString(appUserModelId);
            strings.Add(appId);
            var notifier = InvokeFactory(manager, 7, appId);
            references.Add(notifier);
            if (show)
            {
                InvokeOnePointer(notifier, 6, notification);
            }
        }
        finally
        {
            for (var index = references.Count - 1; index >= 0; index--)
            {
                Release(references[index]);
            }
            foreach (var value in strings)
            {
                if (value != 0)
                {
                    _ = WindowsDeleteString(value);
                }
            }
        }
    }

    private static nint ActivateInstance(string className, ICollection<nint> strings)
    {
        var classId = CreateString(className);
        strings.Add(classId);
        Marshal.ThrowExceptionForHR(RoActivateInstance(classId, out var instance));
        return instance;
    }

    private static nint GetActivationFactory(string className, Guid interfaceId, ICollection<nint> strings)
    {
        var classId = CreateString(className);
        strings.Add(classId);
        Marshal.ThrowExceptionForHR(RoGetActivationFactory(classId, ref interfaceId, out var factory));
        return factory;
    }

    private static nint CreateString(string value)
    {
        Marshal.ThrowExceptionForHR(WindowsCreateString(value, value.Length, out var result));
        return result;
    }

    private static unsafe nint QueryInterface(nint instance, Guid interfaceId)
    {
        nint result = 0;
        var function = (delegate* unmanaged[Stdcall]<nint, Guid*, nint*, int>)GetVtable(instance)[0];
        Marshal.ThrowExceptionForHR(function(instance, &interfaceId, &result));
        return result;
    }

    private static unsafe nint InvokeFactory(nint instance, int index, nint argument)
    {
        nint result = 0;
        var function = (delegate* unmanaged[Stdcall]<nint, nint, nint*, int>)GetVtable(instance)[index];
        Marshal.ThrowExceptionForHR(function(instance, argument, &result));
        return result;
    }

    private static unsafe void InvokeOnePointer(nint instance, int index, nint argument)
    {
        var function = (delegate* unmanaged[Stdcall]<nint, nint, int>)GetVtable(instance)[index];
        Marshal.ThrowExceptionForHR(function(instance, argument));
    }

    private static unsafe void Release(nint instance)
    {
        if (instance == 0)
        {
            return;
        }

        var function = (delegate* unmanaged[Stdcall]<nint, uint>)GetVtable(instance)[2];
        _ = function(instance);
    }

    private static unsafe nint* GetVtable(nint instance) => *(nint**)instance;

    [DllImport("combase.dll")]
    private static extern int RoInitialize(uint initializationType);

    [DllImport("combase.dll", CharSet = CharSet.Unicode)]
    private static extern int WindowsCreateString(string sourceString, int length, out nint value);

    [DllImport("combase.dll")]
    private static extern int WindowsDeleteString(nint value);

    [DllImport("combase.dll")]
    private static extern int RoActivateInstance(nint classId, out nint instance);

    [DllImport("combase.dll")]
    private static extern int RoGetActivationFactory(nint classId, ref Guid interfaceId, out nint factory);
}
