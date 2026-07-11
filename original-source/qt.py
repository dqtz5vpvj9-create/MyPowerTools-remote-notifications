import os
import sys
import threading
import time
from typing import Optional
from urllib.parse import parse_qs, urlparse

ACTIVATION_PIPE_NAME = "\\\\.\\pipe\\AndroidToolsToastActivation"
ACTIVATION_PREFIX = "--androidtools-toast-activation="
INSTANCE_MUTEX_NAME = "Local\\AndroidTools.MainInstance"
_INSTANCE_MUTEX_HANDLE = None


def _activation_payload_from_argv(argv: list[str]) -> str:
    for index, arg in enumerate(argv):
        if arg == "--androidtools-toast-activation" and index + 1 < len(argv):
            return _strip_activation_quotes(argv[index + 1])
        if arg.startswith(ACTIVATION_PREFIX):
            return _strip_activation_quotes(arg[len(ACTIVATION_PREFIX):])
        if "androidtools://notification" in arg:
            return _strip_activation_quotes(arg)
    return ""


def _strip_activation_quotes(payload: str) -> str:
    payload = payload.strip()
    if len(payload) >= 2 and payload[0] == payload[-1] == '"':
        return payload[1:-1]
    return payload


def _parse_activation_payload(payload: str) -> str:
    if not payload:
        return ""
    if payload.startswith("androidtools://notification"):
        parsed = urlparse(payload)
        return parse_qs(parsed.query).get("id", [""])[0]
    if payload.startswith("id="):
        return parse_qs(payload).get("id", [""])[0]
    return payload


def _send_activation_to_existing_instance(payload: str, timeout_ms: int = 120) -> bool:
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return False

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
    kernel32.WaitNamedPipeW.restype = wintypes.BOOL
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.WriteFile.argtypes = [
        wintypes.HANDLE,
        wintypes.LPCVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    kernel32.WriteFile.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    if not kernel32.WaitNamedPipeW(ACTIVATION_PIPE_NAME, timeout_ms):
        return False

    generic_write = 0x40000000
    open_existing = 3
    handle = kernel32.CreateFileW(
        ACTIVATION_PIPE_NAME,
        generic_write,
        0,
        None,
        open_existing,
        0,
        None,
    )
    if handle == wintypes.HANDLE(-1).value:
        return False

    try:
        payload_bytes = payload.encode("utf-8")
        written = wintypes.DWORD(0)
        ok = kernel32.WriteFile(
            handle,
            payload_bytes,
            len(payload_bytes),
            ctypes.byref(written),
            None,
        )
        return bool(ok) and written.value == len(payload_bytes)
    finally:
        kernel32.CloseHandle(handle)


def _send_activation_with_retries(payload: str, total_timeout_ms: int = 2000) -> bool:
    deadline = time.monotonic() + (total_timeout_ms / 1000.0)
    while time.monotonic() < deadline:
        if _send_activation_to_existing_instance(payload, timeout_ms=200):
            return True
        time.sleep(0.05)
    return False


def _acquire_main_instance_lock() -> bool:
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return True

    global _INSTANCE_MUTEX_HANDLE
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.CreateMutexW(None, True, INSTANCE_MUTEX_NAME)
    if not handle:
        return True
    if ctypes.get_last_error() == 183:
        kernel32.CloseHandle(handle)
        return False
    _INSTANCE_MUTEX_HANDLE = handle
    return True


def _release_main_instance_lock() -> None:
    if sys.platform != "win32":
        return
    global _INSTANCE_MUTEX_HANDLE
    if not _INSTANCE_MUTEX_HANDLE:
        return
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
        kernel32.ReleaseMutex.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.ReleaseMutex(_INSTANCE_MUTEX_HANDLE)
        kernel32.CloseHandle(_INSTANCE_MUTEX_HANDLE)
    except Exception:
        pass
    _INSTANCE_MUTEX_HANDLE = None


_EARLY_ACTIVATION_PAYLOAD = ""
if __name__ == "__main__":
    _EARLY_ACTIVATION_PAYLOAD = _activation_payload_from_argv(sys.argv[1:])
    if os.environ.get("ANDROIDTOOLS_DEBUG_ACTIVATION_ARGV"):
        try:
            with open(os.path.join(os.environ.get("TEMP", "."), "androidtools_activation_argv.log"), "w", encoding="utf-8") as f:
                f.write(repr(sys.argv) + "\n")
                f.write(repr(_EARLY_ACTIVATION_PAYLOAD) + "\n")
        except Exception:
            pass
    if _EARLY_ACTIVATION_PAYLOAD and _send_activation_to_existing_instance(_EARLY_ACTIVATION_PAYLOAD):
        sys.exit(0)
    if not _acquire_main_instance_lock():
        if not _EARLY_ACTIVATION_PAYLOAD:
            _send_activation_with_retries("__androidtools_show__", total_timeout_ms=2000)
        sys.exit(0)
    if not _EARLY_ACTIVATION_PAYLOAD and _send_activation_to_existing_instance("__androidtools_show__", timeout_ms=120):
        _release_main_instance_lock()
        sys.exit(0)


from PyQt6.QtWidgets import *
from PyQt6.QtGui import *
from PyQt6.QtCore import QObject, Qt, pyqtBoundSignal, pyqtSignal, pyqtSlot

from powertool.page1 import Page1, windows_toast_reminder_enabled
_page2_import_error: Optional[str] = None
try:
    from powertool.page2 import Page2
except ImportError as e:
    Page2 = None  # type: ignore
    _page2_import_error = f"{type(e).__name__}: {e}"
try:
    from powertool.page2 import Page2Void
except ImportError:
    Page2Void = None  # type: ignore
from powertool.page3 import Page3
from powertool.build_info import build_display_text
from powertool.qt_utils import connect_safe, safe_slot
from powertool.windows_notifications import (
    setup_windows_notifications,
    show_windows_toast,
    toast_launch_for_message,
)
from powertool.notification_ids import notification_message_id


class ActivationBridge(QObject):
    activated = pyqtSignal(str)


def _make_page2_placeholder(reason: Optional[str]) -> QWidget:
    w = QWidget()
    layout = QVBoxLayout(w)
    layout.addWidget(QLabel("Process Monitor unavailable"))
    if reason:
        detail = QLabel(reason)
        detail.setWordWrap(True)
        detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(detail)
    layout.addStretch(1)
    return w


class MainWindow(QMainWindow):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._build_display_text = build_display_text()
        self.setWindowTitle(f"Android Tools - {self._build_display_text}")
        self.setMinimumSize(900, 600)
        self.resize(1100, 700)

        # Create pages
        self.page1 = Page1()
        if os.name == 'nt' and Page2 is not None:
            self.page2 = Page2()
        elif Page2Void is not None:
            self.page2 = Page2Void()
        else:
            self.page2 = _make_page2_placeholder(_page2_import_error)
        self.page3 = Page3()

        # Register pages with descriptive nav labels
        page_definitions: list[tuple[str, QWidget]] = [
            ("Notifications", self.page1),
            ("Process Monitor", self.page2),
            ("Remote Commands", self.page3),
        ]

        self.pages = QStackedWidget(self)
        self.nav_list = QListWidget(self)
        self.nav_list.setObjectName("navList")

        for name, widget in page_definitions:
            self.pages.addWidget(widget)
            self.nav_list.addItem(name)

        connect_safe(self.nav_list.currentRowChanged, self.pages.setCurrentIndex)

        # Layout
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.nav_list.setFixedWidth(170)
        self.nav_list.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        layout.setStretchFactor(self.nav_list, 0)
        layout.addWidget(self.nav_list)
        layout.addWidget(self.pages)

        central_widget = QWidget(self)
        central_widget.setLayout(layout)
        self.setCentralWidget(central_widget)
        self.statusBar().showMessage(self._build_display_text)

        # System tray icon — use the app's standard messagebox-information icon
        # so we don't drag in the wifi status pixmap (pywifi has no macOS backend).
        self.tray_icon = QSystemTrayIcon(self)
        style = self.style()
        if style is not None:
            self.tray_icon.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_MessageBoxInformation))
        self.tray_icon.setToolTip("Android Tools")
        self.tray_icon.setVisible(True)
        setup_windows_notifications()
        self._activation_bridge = ActivationBridge(self)
        self._activation_bridge.activated.connect(self.open_notification_message)
        self._activation_stop = threading.Event()
        self._activation_thread: Optional[threading.Thread] = None
        self._start_activation_server()

        # Tray menu
        tray_menu = QMenu()
        self.tray_icon.setContextMenu(tray_menu)

        show_action = QAction("Show", self)
        assert isinstance(show_action.triggered, pyqtBoundSignal)
        show_action.triggered.connect(self.show_self)
        tray_menu.addAction(show_action)

        quit_action = QAction("Quit", self)
        assert isinstance(quit_action.triggered, pyqtBoundSignal)
        quit_action.triggered.connect(QApplication.instance().quit)
        tray_menu.addAction(quit_action)

        tray_menu.addSeparator()

        dummy_action = QAction("", self)
        dummy_action.setEnabled(False)
        tray_menu.addAction(dummy_action)

        connect_safe(self.tray_icon.activated, self.on_tray_icon_activated)
        connect_safe(self.tray_icon.messageClicked, self.on_system_tray_message_clicked)

        # Show desktop toasts only after Page1 accepts the message. Page1 owns
        # deduplication, so subscribing to the raw worker signal would toast
        # duplicates that the list itself correctly drops.
        connect_safe(self.page1.accepted_msg_signal, self.on_received_msg)

    @pyqtSlot(tuple) # type: ignore
    @safe_slot
    def on_received_msg(self, msg: tuple) -> None:
        channel = msg[0]
        content = msg[1]
        icon_name = msg[2] if len(msg) > 2 else "info"
        timestamp = msg[3] if len(msg) > 3 else ""
        from powertool.page1 import load_notification_icon, ICONS_DIR

        # send_notification.py prefixes every hook message with "[label] body"
        # where label is the codex thread_name / claude session name. Promote
        # that label to the toast title so the desktop notification surfaces
        # which agent is asking for attention; the channel (always "default")
        # is only useful when no label is present.
        import re
        m = re.match(r'^\[([^\]]+)\]\s*', content)
        if m:
            title = m.group(1)
            body = content[m.end():]
        else:
            title = channel or "Notification"
            body = content
        notification_id = str(msg[4]) if len(msg) > 4 and msg[4] else ""
        if not notification_id:
            notification_id = notification_message_id(channel, content, icon_name, timestamp)

        # Remember the source payload so a later messageClicked can open the
        # full MessageDetailDialog for this exact notification rather than
        # just raising the main window.
        self._last_toast_data = {
            "id": notification_id,
            "channel": channel,
            "message": content,
            "icon": icon_name,
            "timestamp": timestamp,
        }

        if sys.platform == 'darwin':
            self._show_mac_notification(title, body)
            return

        if sys.platform == 'win32':
            toast_scenario = "reminder" if windows_toast_reminder_enabled() else ""
            if show_windows_toast(
                title,
                body,
                scenario=toast_scenario,
                tag=notification_id[:16],
                group="page1",
                launch=toast_launch_for_message(notification_id),
            ):
                return

        # Cap the body so Windows toast XML / tray bubble don't choke on
        # long codex last_assistant_message payloads.
        body_capped = " ".join((body or "").split())
        if len(body_capped) > 240:
            body_capped = body_capped[:237] + "..."

        pixmap = load_notification_icon(icon_name)
        if pixmap:
            self.tray_icon.showMessage(title, body_capped, QIcon(pixmap), 10000)
        else:
            self.tray_icon.showMessage(title, body_capped, QSystemTrayIcon.MessageIcon.Information, 10000)

    def _show_mac_notification(self, title: str, content: str) -> None:
        """Native macOS banner via `osascript`. Goes through Notification
        Center, no app bundle required. AppleScript's `display notification`
        can't carry a custom icon (it uses the sender app's icon, which for
        non-bundled python is generic), so icon_name is dropped on this path.
        """
        import subprocess
        # AppleScript string literals: escape backslash first, then quote.
        def esc(s: str) -> str:
            return s.replace("\\", "\\\\").replace('"', '\\"')
        # The banner truncates hard — collapse whitespace so the preview is
        # one tidy line instead of a multi-line markdown blob.
        body = " ".join((content or "").split())
        script = f'display notification "{esc(body)}" with title "{esc(title)}"'
        try:
            subprocess.Popen(
                ["osascript", "-e", script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            self.tray_icon.showMessage(title, content, QSystemTrayIcon.MessageIcon.Information)

    @pyqtSlot() # type: ignore
    def on_system_tray_message_clicked(self) -> None:
        # The user clicked the most recent toast: open the full message
        # detail dialog for that notification, mirroring the double-click
        # behavior in page1's notification list. The dialog is a top-level
        # window (parent=None) so its z-order is independent of the main
        # window, and we never call show_self() — clicking the toast should
        # surface only the message, not drag the entire app forward.
        data = getattr(self, "_last_toast_data", None)
        if not data:
            self.show_self()
            return
        try:
            from powertool.page1 import MessageDetailDialog
            dialog = MessageDetailDialog(
                data.get("channel", "default"),
                data.get("message", ""),
                data.get("icon", "info"),
                data.get("timestamp", ""),
                None,
            )
            dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
            # Without a parent the QDialog has no C++ owner, so Python must
            # keep a reference until the user closes it; otherwise the GC
            # collects the wrapper while Qt is still using the widget.
            if not hasattr(self, "_open_toast_dialogs"):
                self._open_toast_dialogs = []
            self._open_toast_dialogs.append(dialog)
            dialog.finished.connect(
                lambda _result, d=dialog: self._open_toast_dialogs.remove(d)
                if d in self._open_toast_dialogs else None
            )
            # Windows blocks SetForegroundWindow from background callers as
            # focus-stealing protection. The transient WindowStaysOnTopHint
            # toggle below is the most reliable Qt-only workaround: we show
            # once with the hint to grab z-order, then immediately drop it
            # so the user can stack other windows over the dialog normally.
            dialog.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
            dialog.show()
            dialog.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, False)
            dialog.show()
            dialog.activateWindow()
            dialog.raise_()
        except Exception:
            pass

    def _start_activation_server(self) -> None:
        if sys.platform != "win32":
            return
        if self._activation_thread is not None:
            return
        self._activation_thread = threading.Thread(
            target=self._activation_server_loop,
            name="AndroidToolsToastActivation",
            daemon=True,
        )
        self._activation_thread.start()

    def _activation_server_loop(self) -> None:
        try:
            import win32file
            import win32pipe
            import pywintypes
        except Exception:
            return

        while not self._activation_stop.is_set():
            pipe = None
            try:
                pipe = win32pipe.CreateNamedPipe(
                    ACTIVATION_PIPE_NAME,
                    win32pipe.PIPE_ACCESS_INBOUND,
                    win32pipe.PIPE_TYPE_MESSAGE | win32pipe.PIPE_READMODE_MESSAGE | win32pipe.PIPE_WAIT,
                    1,
                    4096,
                    4096,
                    1000,
                    None,
                )
                try:
                    win32pipe.ConnectNamedPipe(pipe, None)
                except pywintypes.error as e:
                    if e.winerror != 535:
                        raise
                _hr, data = win32file.ReadFile(pipe, 4096)
                payload = (data or b"").decode("utf-8", errors="replace").strip()
                if payload == "__androidtools_show__":
                    self._activation_bridge.activated.emit("")
                    continue
                notification_id = _parse_activation_payload(payload)
                self._activation_bridge.activated.emit(notification_id)
            except Exception:
                if self._activation_stop.is_set():
                    break
            finally:
                if pipe is not None:
                    try:
                        win32pipe.DisconnectNamedPipe(pipe)
                    except Exception:
                        pass
                    try:
                        pipe.Close()
                    except Exception:
                        pass

    def open_notification_message(self, notification_id: str) -> bool:
        if not notification_id:
            self.show_self()
            return True
        opened = self.page1.open_message_by_id(notification_id)
        if opened:
            return True
        data = getattr(self, "_last_toast_data", None)
        if isinstance(data, dict) and data.get("id") == notification_id:
            self.on_system_tray_message_clicked()
            return True
        self.show_self()
        return False

    def show_self(self) -> None:
        self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMinimized)
        self.showNormal()
        self.raise_()
        self.activateWindow()
        if sys.platform == "win32":
            try:
                import ctypes

                hwnd = int(self.winId())
                user32 = ctypes.WinDLL("user32", use_last_error=True)
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                user32.SetForegroundWindow(hwnd)
                user32.BringWindowToTop(hwnd)
            except Exception:
                pass

    def closeEvent(self, event: QCloseEvent) -> None:
        event.ignore()
        self.hide()

    def shutdown(self) -> None:
        """Drain background workers before Qt/Python tear objects down.
        Without this, tray-Quit (which goes through QApplication.quit() rather
        than closeEvent) leaves Page1Worker mid-poll; a queued signal then
        fires on a half-destroyed widget and PyQt6 turns the resulting Python
        exception into qFatal()/abort(). If a worker is stuck inside a
        blocking requests.get (15s timeout), force-exit to avoid the same."""
        self.page1.shutdown()
        self._activation_stop.set()
        _release_main_instance_lock()
        for w in self.page1.workers:
            if w.isRunning():
                os._exit(0)

    def on_tray_icon_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick or reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.show_self()


if __name__ == '__main__':
    activation_payload = _EARLY_ACTIVATION_PAYLOAD

    app = QApplication(sys.argv)
    setup_windows_notifications()
    if activation_payload:
        notification_id = _parse_activation_payload(activation_payload)
        from powertool.toast_activation import open_persisted_message

        if open_persisted_message(notification_id):
            sys.exit(0)

    # Load light modern theme
    from powertool.frozen_path import bundled_dir
    qss_path = os.path.join(bundled_dir(), 'style.qss')
    if os.path.exists(qss_path):
        with open(qss_path, encoding='utf-8') as f:
            app.setStyleSheet(f.read())

    window = MainWindow()
    window.show()
    app.aboutToQuit.connect(window.shutdown)
    sys.exit(app.exec())
