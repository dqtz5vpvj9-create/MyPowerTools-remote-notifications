import ctypes
import hashlib
import html
import os
import sys
from pathlib import Path
from typing import Optional
from urllib.parse import quote


APP_USER_MODEL_ID = "AndroidTools.Notifications"
APP_DISPLAY_NAME = "Android Tools"
START_MENU_FOLDER = "Android Tools"
START_MENU_SHORTCUT = "Android Tools Notifications.lnk"
PROTOCOL_SCHEME = "androidtools"
TOAST_ACTIVATION_ARG = '--androidtools-toast-activation="%1"'

S_OK = 0
S_FALSE = 1
RPC_E_CHANGED_MODE = 0x80010106
RO_INIT_MULTITHREADED = 1
STGM_READWRITE = 2

_setup_done = False
_winrt_initialized = False


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    def __init__(self, value: str) -> None:
        import uuid

        guid = uuid.UUID(value)
        data4 = (ctypes.c_ubyte * 8).from_buffer_copy(guid.bytes[8:])
        super().__init__(guid.time_low, guid.time_mid, guid.time_hi_version, data4)


HSTRING = ctypes.c_void_p
IInspectable = ctypes.c_void_p


def setup_windows_notifications() -> bool:
    """Register the process and Start Menu shortcut used by WinRT toasts."""
    global _setup_done
    if sys.platform != "win32":
        return False
    if _setup_done:
        return True

    ok = False
    try:
        from win32com.shell import shell

        shell.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
        ok = True
    except Exception:
        pass

    try:
        _ensure_start_menu_shortcuts()
        _register_protocol_handler()
        ok = True
    except Exception:
        pass

    _setup_done = ok
    return ok


def show_windows_toast(
    title: str,
    body: str,
    *,
    scenario: str = "",
    tag: Optional[str] = None,
    group: str = "page1",
    launch: str = "androidtools://notification",
) -> bool:
    if sys.platform != "win32":
        return False
    setup_windows_notifications()

    title = _toast_text(title, 140)
    body = _toast_text(body, 900)
    if not title and not body:
        return False

    if tag is None:
        digest = hashlib.sha1(f"{title}\n{body}".encode("utf-8", errors="ignore")).hexdigest()
        tag = f"p1{digest[:14]}"

    xml = _toast_xml(title or APP_DISPLAY_NAME, body, scenario, launch)
    handles: list[HSTRING] = []
    refs: list[IInspectable] = []
    try:
        _ensure_winrt_initialized()

        xml_doc = _activate_instance("Windows.Data.Xml.Dom.XmlDocument")
        refs.append(xml_doc)
        xml_doc_io = _query_interface(xml_doc, "6CD0E74E-EE65-4489-9EBF-CA43E87BA637")
        refs.append(xml_doc_io)

        xml_h = _hstring(xml)
        handles.append(xml_h)
        _check(_vcall(xml_doc_io, 6, ctypes.c_long, HSTRING)(xml_doc_io, xml_h))

        factory = _activation_factory(
            "Windows.UI.Notifications.ToastNotification",
            "04124B20-82C6-4229-B109-FD9ED4662B53",
        )
        refs.append(factory)
        notification = IInspectable()
        _check(
            _vcall(factory, 6, ctypes.c_long, IInspectable, ctypes.POINTER(IInspectable))(
                factory,
                xml_doc,
                ctypes.byref(notification),
            )
        )
        refs.append(notification)

        notification2 = _query_interface(notification, "9DFB9FD1-143A-490E-90BF-B9FBA7132DE7")
        refs.append(notification2)
        tag_h = _hstring((tag or "")[:16])
        group_h = _hstring((group or "")[:64])
        handles.extend([tag_h, group_h])
        _check(_vcall(notification2, 6, ctypes.c_long, HSTRING)(notification2, tag_h))
        _check(_vcall(notification2, 8, ctypes.c_long, HSTRING)(notification2, group_h))

        manager = _activation_factory(
            "Windows.UI.Notifications.ToastNotificationManager",
            "50AC103F-D235-4598-BBEF-98FE4D1A3AD4",
        )
        refs.append(manager)
        app_id_h = _hstring(APP_USER_MODEL_ID)
        handles.append(app_id_h)
        notifier = IInspectable()
        _check(
            _vcall(manager, 7, ctypes.c_long, HSTRING, ctypes.POINTER(IInspectable))(
                manager,
                app_id_h,
                ctypes.byref(notifier),
            )
        )
        refs.append(notifier)
        _check(_vcall(notifier, 6, ctypes.c_long, IInspectable)(notifier, notification))
        return True
    except Exception:
        return False
    finally:
        for ref in reversed(refs):
            _release(ref)
        for handle in handles:
            _delete_hstring(handle)


def clear_windows_toasts() -> bool:
    if sys.platform != "win32":
        return False
    try:
        _ensure_winrt_initialized()
        manager = _activation_factory(
            "Windows.UI.Notifications.ToastNotificationManager",
            "50AC103F-D235-4598-BBEF-98FE4D1A3AD4",
        )
        app_id_h = _hstring(APP_USER_MODEL_ID)
        try:
            # IToastNotificationManagerStatics2::get_History() then
            # ToastNotificationHistory::ClearWithId(appUserModelId).
            history = IInspectable()
            _check(_vcall(manager, 8, ctypes.c_long, ctypes.POINTER(IInspectable))(manager, ctypes.byref(history)))
            try:
                _check(_vcall(history, 4, ctypes.c_long, HSTRING)(history, app_id_h))
            finally:
                _release(history)
        finally:
            _delete_hstring(app_id_h)
            _release(manager)
        return True
    except Exception:
        return False


def _ensure_start_menu_shortcuts() -> None:
    folder = _start_menu_folder()
    folder.mkdir(parents=True, exist_ok=True)

    shortcut_path = folder / START_MENU_SHORTCUT
    _create_shortcut(shortcut_path)
    _set_shortcut_app_id(shortcut_path)


def _start_menu_folder() -> Path:
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / START_MENU_FOLDER
    return Path.home() / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / START_MENU_FOLDER


def _create_shortcut(shortcut_path: Path) -> None:
    import win32com.client

    shell = win32com.client.Dispatch("WScript.Shell")
    shortcut = shell.CreateShortcut(str(shortcut_path))
    target, arguments, working_dir = _launch_command()
    shortcut.TargetPath = target
    shortcut.Arguments = _shortcut_arguments(arguments)
    shortcut.WorkingDirectory = working_dir
    shortcut.IconLocation = f"{_app_icon_target()},0"
    shortcut.Description = APP_DISPLAY_NAME
    shortcut.Save()


def _launch_command() -> tuple[str, str, str]:
    target = sys.executable
    working_dir = os.getcwd()
    if getattr(sys, "frozen", False):
        app_dir = os.path.dirname(target)
        helper = os.path.join(app_dir, "ToastActivator.exe")
        return (helper if os.path.exists(helper) else target), "", app_dir

    repo_dist = Path(__file__).resolve().parents[1] / "dist"
    helper = repo_dist / "ToastActivator.exe"
    if helper.exists():
        return str(helper), "", str(repo_dist)

    script = os.path.abspath(sys.argv[0]) if sys.argv else ""
    arguments = f'"{script}"' if script.lower().endswith(".py") else ""
    if script:
        working_dir = os.path.dirname(script)
    return target, arguments, working_dir


def _shortcut_arguments(base_arguments: str) -> str:
    if getattr(sys, "frozen", False):
        return TOAST_ACTIVATION_ARG
    if base_arguments:
        return f"{base_arguments} {TOAST_ACTIVATION_ARG}"
    return TOAST_ACTIVATION_ARG


def _app_icon_target() -> str:
    if getattr(sys, "frozen", False):
        return sys.executable
    return str(Path(__file__).resolve().parents[1] / "dist" / "AndroidTools.exe")


def _register_protocol_handler() -> None:
    import winreg

    target, arguments, _working_dir = _launch_command()
    icon_target = _app_icon_target()
    command_parts = [f'"{target}"']
    if arguments:
        command_parts.append(arguments)
    command_parts.append(TOAST_ACTIVATION_ARG)
    command = " ".join(command_parts)

    key_path = rf"Software\Classes\{PROTOCOL_SCHEME}"
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "URL:AndroidTools Notification Protocol")
        winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path + r"\DefaultIcon") as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, f"{icon_target},0")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path + r"\shell\open\command") as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, command)


def _set_shortcut_app_id(shortcut_path: Path) -> None:
    import pythoncom
    from win32com.propsys import propsys, pscon
    from win32com.shell import shell

    link = pythoncom.CoCreateInstance(
        shell.CLSID_ShellLink,
        None,
        pythoncom.CLSCTX_INPROC_SERVER,
        shell.IID_IShellLink,
    )
    persist = link.QueryInterface(pythoncom.IID_IPersistFile)
    persist.Load(str(shortcut_path), STGM_READWRITE)
    store = link.QueryInterface(propsys.IID_IPropertyStore)
    store.SetValue(pscon.PKEY_AppUserModel_ID, propsys.PROPVARIANTType(APP_USER_MODEL_ID))

    target, arguments = _shortcut_command(link)
    relaunch = f'"{target}" {arguments}'.strip()
    icon = _shortcut_icon(link) or f"{target},0"
    store.SetValue(pscon.PKEY_AppUserModel_RelaunchCommand, propsys.PROPVARIANTType(relaunch))
    store.SetValue(
        pscon.PKEY_AppUserModel_RelaunchDisplayNameResource,
        propsys.PROPVARIANTType(APP_DISPLAY_NAME),
    )
    store.SetValue(
        pscon.PKEY_AppUserModel_RelaunchIconResource,
        propsys.PROPVARIANTType(icon),
    )
    store.Commit()
    persist.Save(str(shortcut_path), 1)


def _shortcut_command(link) -> tuple[str, str]:
    try:
        target = link.GetPath(0)[0]
        arguments = link.GetArguments()
    except Exception:
        target, arguments, _working_dir = _launch_command()
    return target, arguments


def _shortcut_icon(link) -> str:
    try:
        icon_path, icon_index = link.GetIconLocation()
    except Exception:
        return ""
    if not icon_path:
        return ""
    return f"{icon_path},{icon_index}"


def _ensure_winrt_initialized() -> None:
    global _winrt_initialized
    if _winrt_initialized:
        return
    combase = ctypes.WinDLL("combase")
    combase.RoInitialize.argtypes = [ctypes.c_int]
    combase.RoInitialize.restype = ctypes.c_long
    hr = combase.RoInitialize(RO_INIT_MULTITHREADED)
    if _hresult_u32(hr) not in (S_OK, S_FALSE, RPC_E_CHANGED_MODE):
        _check(hr)
    _winrt_initialized = True


def _hstring(text: str) -> HSTRING:
    combase = ctypes.WinDLL("combase")
    combase.WindowsCreateString.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.POINTER(HSTRING)]
    combase.WindowsCreateString.restype = ctypes.c_long
    handle = HSTRING()
    _check(combase.WindowsCreateString(text, len(text), ctypes.byref(handle)))
    return handle


def _delete_hstring(handle: HSTRING) -> None:
    if not handle:
        return
    combase = ctypes.WinDLL("combase")
    combase.WindowsDeleteString.argtypes = [HSTRING]
    combase.WindowsDeleteString.restype = ctypes.c_long
    combase.WindowsDeleteString(handle)


def _activate_instance(class_name: str) -> IInspectable:
    combase = ctypes.WinDLL("combase")
    combase.RoActivateInstance.argtypes = [HSTRING, ctypes.POINTER(IInspectable)]
    combase.RoActivateInstance.restype = ctypes.c_long
    class_h = _hstring(class_name)
    instance = IInspectable()
    try:
        _check(combase.RoActivateInstance(class_h, ctypes.byref(instance)))
        return instance
    finally:
        _delete_hstring(class_h)


def _activation_factory(class_name: str, iid: str) -> IInspectable:
    combase = ctypes.WinDLL("combase")
    combase.RoGetActivationFactory.argtypes = [
        HSTRING,
        ctypes.POINTER(GUID),
        ctypes.POINTER(IInspectable),
    ]
    combase.RoGetActivationFactory.restype = ctypes.c_long
    class_h = _hstring(class_name)
    factory = IInspectable()
    try:
        _check(combase.RoGetActivationFactory(class_h, ctypes.byref(GUID(iid)), ctypes.byref(factory)))
        return factory
    finally:
        _delete_hstring(class_h)


def _query_interface(obj: IInspectable, iid: str) -> IInspectable:
    result = IInspectable()
    _check(
        _vcall(
            obj,
            0,
            ctypes.c_long,
            ctypes.POINTER(GUID),
            ctypes.POINTER(IInspectable),
        )(obj, ctypes.byref(GUID(iid)), ctypes.byref(result))
    )
    return result


def _release(obj: IInspectable) -> None:
    if not obj:
        return
    try:
        _vcall(obj, 2, ctypes.c_ulong)(obj)
    except Exception:
        pass


def _vcall(obj: IInspectable, index: int, restype, *argtypes):
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    prototype = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    return prototype(vtable[index])


def _check(hr: int) -> None:
    if hr < 0:
        raise ctypes.WinError(ctypes.c_long(hr).value)


def _hresult_u32(hr: int) -> int:
    return ctypes.c_ulong(hr).value


def _toast_text(text: str, max_len: int) -> str:
    cleaned = "".join(
        ch if ch in "\t\n\r" or ord(ch) >= 0x20 else " "
        for ch in (text or "")
    )
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > max_len:
        cleaned = cleaned[: max_len - 3] + "..."
    return cleaned


def toast_launch_for_message(notification_id: str) -> str:
    return f"{PROTOCOL_SCHEME}://notification?id={quote(notification_id or '', safe='')}"


def _toast_xml(title: str, body: str, scenario: str, launch: str) -> str:
    escaped_title = html.escape(title, quote=True)
    escaped_body = html.escape(body, quote=True)
    escaped_launch = html.escape(launch or "androidtools://notification", quote=True)
    scenario_attr = f' scenario="{html.escape(scenario, quote=True)}"' if scenario else ""
    return (
        f'<toast{scenario_attr} activationType="protocol" launch="{escaped_launch}">'
        "<visual>"
        '<binding template="ToastGeneric">'
        f"<text>{escaped_title}</text>"
        f"<text>{escaped_body}</text>"
        "</binding>"
        "</visual>"
        '<audio silent="true"/>'
        "</toast>"
    )
