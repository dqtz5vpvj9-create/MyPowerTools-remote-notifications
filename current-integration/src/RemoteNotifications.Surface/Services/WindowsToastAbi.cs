using System.Runtime.InteropServices;

#if REMOTE_NOTIFICATIONS_SERVICE
namespace RemoteNotifications.Service;
#else
namespace RemoteNotifications.Surface.Services;
#endif

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

#if !REMOTE_NOTIFICATIONS_SERVICE
    public static unsafe void Probe(string appUserModelId)
    {
        CreateNotification(
            "<toast><visual><binding template=\"ToastGeneric\"><text>MyPowerTools</text>" +
            "<text>Notification runtime probe</text></binding></visual><audio silent=\"true\"/></toast>",
            "mpt-probe",
            "page1",
            appUserModelId,
            show: false);
    }
#endif

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
