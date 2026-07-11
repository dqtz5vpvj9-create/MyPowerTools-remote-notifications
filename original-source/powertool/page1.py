import importlib, sys, os
from os.path import dirname, pardir
from pathlib import Path
def import_parents(level: int = 1) -> None:
    global __package__
    file = Path(__file__).resolve()
    parent, top = file.parent, file.parents[level]

    sys.path.append(str(top))

    __package__ = '.'.join(parent.parts[len(top.parts):])
    importlib.import_module(__package__)

if __name__ == '__main__' and (__package__ is None or len(__package__) == 0):
    import_parents()
sys.path.append(dirname(__file__) + os.sep + pardir)
from py_modules.logging_lib import setup_logging, LogLevel, MyLogger
logger = setup_logging()
from py_modules.check_interpreter import check_conda_interpreter, CONDA_ENV_NAME


import hashlib
import html
import json
import logging
import re
import sys
import time
from collections import deque
from datetime import datetime as datetime_class, timezone, timedelta
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem, QToolBar,
    QLabel, QApplication, QMenu, QStyle, QSizePolicy, QGridLayout, QTextBrowser,
    QFrame, QAbstractItemView, QScroller, QDialog, QPushButton, QButtonGroup,
    QScrollArea,
)
from PyQt6.QtGui import QAction, QFont, QIcon, QPixmap
from PyQt6.QtCore import (
    Qt, pyqtBoundSignal, pyqtSignal, QObject, pyqtSlot, QThread, QSize, QSettings,
    QAbstractAnimation, QEasingCurve, QEvent, QPropertyAnimation, QTimer,
)
from typing import Optional
from markdown_it import MarkdownIt
from mdit_py_plugins.tasklists import tasklists_plugin
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name
from pygments.util import ClassNotFound

from py_modules.logging_lib import setup_logging
from py_modules.simple_http_notification_conf import cloud_server_protocol, cloud_server_ip, cloud_server_port
from py_modules.simple_http_notification_receiver import (
    NotificationAuthError, NotificationPullError, SimpleHttpNotificationReceiver,
)
from py_modules.lib_network import WifiStatus, check_wifi_status, get_wifi_interface
from powertool.frozen_path import bundled_dir
from powertool.notification_ids import notification_message_id
from powertool.qt_utils import safe_slot

ICONS_DIR = os.path.join(bundled_dir(), 'icons')

SETTINGS_ORG = "AndroidTools"
SETTINGS_APP = "Page1"
WINDOWS_TOAST_REMINDER_KEY = "windows_toast_reminder"
MAX_RECENT_HASHES = 200
MAX_SEEN_MESSAGE_IDS = 5000
MAX_PERSISTED_MESSAGES = 500
PULL_INCREMENTAL_LIMIT = 20
MAX_MESSAGE_BODY_WIDTH = 1360
MESSAGE_ROW_HORIZONTAL_RESERVE = 56

# Session-label filter chip support. The label is the bracketed prefix that
# send_notification.py prepends to every Stop/Notification hook message:
# "[label] body". Extracting it lets the user filter the stream by Claude
# session. Messages without a prefix collapse onto a single "(unlabeled)" chip.
_LABEL_REGEX = re.compile(r"^\[([^\]]+)\]")
LABEL_UNLABELED = "(unlabeled)"
FILTER_ALL = "__all__"


def extract_label(message: str) -> str:
    m = _LABEL_REGEX.match(message or "")
    return m.group(1) if m else LABEL_UNLABELED


def _settings_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def windows_toast_reminder_enabled(settings: Optional[QSettings] = None) -> bool:
    settings = settings or QSettings(SETTINGS_ORG, SETTINGS_APP)
    return _settings_bool(settings.value(WINDOWS_TOAST_REMINDER_KEY, False), False)


_PYGMENTS_FORMATTER = HtmlFormatter(nowrap=False, noclasses=True, style="friendly")

_DOC_CSS = """
body {
    margin: 0;
    color: #212121;
    font-size: 13px;
}
p {
    margin: 0 0 6px 0;
}
ul, ol {
    margin-top: 2px;
    margin-bottom: 6px;
}
li {
    margin-bottom: 2px;
}
code {
    background: #f3f3f3;
    color: #c7254e;
    padding: 1px 4px;
    border-radius: 3px;
    font-family: "JetBrains Mono", Consolas, monospace;
    font-size: 12px;
}
pre {
    background: #f7f7f7;
    border: 1px solid #e6e6e6;
    border-radius: 4px;
    padding: 8px;
    margin: 4px 0 8px 0;
    white-space: pre-wrap;
}
pre code {
    background: transparent;
    color: inherit;
    padding: 0;
}
a {
    color: #2979ff;
    text-decoration: none;
}
blockquote {
    border-left: 3px solid #bbdefb;
    color: #616161;
    margin: 4px 0;
    padding-left: 10px;
}
table {
    border-collapse: collapse;
    margin: 4px 0 8px 0;
}
th, td {
    border: 1px solid #e0e0e0;
    padding: 4px 8px;
}
th {
    background: #fafafa;
}
.task-list-checkbox {
    color: #2979ff;
    font-family: "JetBrains Mono", Consolas, monospace;
}
.task-list-item {
    list-style: none;
}
div.highlight {
    background: transparent;
}
"""

_MD = (
    MarkdownIt("commonmark", {"linkify": True, "html": False, "breaks": True})
    .enable(["table", "strikethrough", "linkify"])
    .use(tasklists_plugin)
)


def _render_fence(tokens, idx, options, env) -> str:
    token = tokens[idx]
    code = token.content
    info = (token.info or "").strip().split()[0] if token.info else ""
    try:
        lexer = get_lexer_by_name(info) if info else None
    except ClassNotFound:
        lexer = None
    if lexer is None:
        return f"<pre class='code-block'><code>{html.escape(code)}</code></pre>\n"
    return highlight(code, lexer, _PYGMENTS_FORMATTER)


def _render_html_inline(tokens, idx, options, env) -> str:
    content = tokens[idx].content
    if "task-list-item-checkbox" in content:
        checked = "checked" in content
        symbol = "&#9745;" if checked else "&#9744;"
        return f'<span class="task-list-checkbox">{symbol}</span>'
    return content


_MD.renderer.rules["fence"] = _render_fence
_MD.renderer.rules["html_inline"] = _render_html_inline


def render_markdown(text: str) -> str:
    return _MD.render(text or "")


def format_display_ts(ts: str) -> str:
    if not ts:
        return "never"
    try:
        return parse_server_ts(ts).astimezone().strftime("%Y/%m/%d %H:%M:%S")
    except Exception:
        return ts


def format_relative_ts(ts: str) -> tuple[str, str]:
    """Return (display, tooltip) where display is a compact relative or
    date-scoped string and tooltip is the full absolute timestamp."""
    if not ts:
        now = datetime_class.now().astimezone()
        return now.strftime("%H:%M"), now.strftime("%Y-%m-%d %H:%M:%S")
    try:
        d = parse_server_ts(ts).astimezone()
    except Exception:
        return ts, ts
    now = datetime_class.now().astimezone()
    tooltip = d.strftime("%Y-%m-%d %H:%M:%S")
    delta = (now - d).total_seconds()
    if delta < 0:
        return d.strftime("%H:%M"), tooltip
    if delta < 60:
        return "just now", tooltip
    if delta < 3600:
        return f"{int(delta // 60)}m ago", tooltip
    if d.date() == now.date():
        return d.strftime("%H:%M"), tooltip
    if d.year == now.year:
        return d.strftime("%m/%d %H:%M"), tooltip
    return d.strftime("%Y/%m/%d"), tooltip


def load_notification_icon(icon_name: str) -> QPixmap | None:
    """Load a notification icon by name from the icons directory.
    Supports: info, warning, error, success, claude, or any custom name with a matching PNG."""
    if not icon_name:
        icon_name = "info"
    icon_path = os.path.join(ICONS_DIR, f"{icon_name}.png")
    if os.path.exists(icon_path):
        return QPixmap(icon_path)
    return None


def content_hash(channel: str, message: str) -> str:
    return hashlib.sha256(f"{channel}|{message}".encode()).hexdigest()[:16]


def parse_server_ts(ts: str) -> datetime_class:
    """Parse server ISO timestamp (Z, +00:00, or naive) to aware UTC datetime."""
    if ts.endswith('Z'):
        ts = ts[:-1] + '+00:00'
    d = datetime_class.fromisoformat(ts)
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d


def sane_pull_waterline(ts: str, max_future_seconds: int = 120) -> str:
    if not ts:
        return ""
    try:
        parsed = parse_server_ts(ts)
    except Exception:
        return ""
    now = datetime_class.now(timezone.utc)
    if parsed > now + timedelta(seconds=max_future_seconds):
        return ""
    return parsed.isoformat()


def notification_cursor_ts(notification: dict) -> str:
    return str(notification.get("server_timestamp") or notification.get("timestamp") or "")


def newest_sane_pull_waterline(notifications: list[dict]) -> str:
    newest: datetime_class | None = None
    now = datetime_class.now(timezone.utc)
    max_future = now + timedelta(seconds=120)
    for notification in notifications:
        try:
            parsed = parse_server_ts(notification_cursor_ts(notification))
        except Exception:
            continue
        if parsed > max_future:
            continue
        if newest is None or parsed > newest:
            newest = parsed
    return newest.isoformat() if newest is not None else ""


def is_sane_server_ts(ts: str, max_future_seconds: int = 120) -> bool:
    try:
        parsed = parse_server_ts(ts)
    except Exception:
        return False
    now = datetime_class.now(timezone.utc)
    return parsed <= now + timedelta(seconds=max_future_seconds)


class Page1Worker(QThread):
    """Polls the server /pull endpoint for new notifications."""
    received_msg_signal = pyqtSignal(tuple)
    status_signal = pyqtSignal(str)
    diagnostics_signal = pyqtSignal(dict)

    def __init__(self, logger: MyLogger, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.logger = logger
        self._running = True
        self._since = datetime_class.now(timezone.utc).isoformat()

    def stop(self) -> None:
        self._running = False

    def run(self) -> None:
        receiver = SimpleHttpNotificationReceiver(
            cloud_server_protocol, cloud_server_ip, cloud_server_port, self.logger)
        while self._running:
            try:
                poll_time = datetime_class.now().astimezone().strftime("%Y/%m/%d %H:%M:%S")
                self.status_signal.emit("running")
                notifications = receiver.pull(
                    "default",
                    since=self._since,
                    limit=PULL_INCREMENTAL_LIMIT,
                )
                emitted_count = 0
                sane_notifications = [
                    n for n in notifications
                    if is_sane_server_ts(notification_cursor_ts(n))
                ]
                latest_ts = sane_notifications[0].get("timestamp", "") if sane_notifications else ""
                latest_waterline = newest_sane_pull_waterline(sane_notifications)
                if sane_notifications:
                    # Server returns newest-first; iterate oldest-first so UI gets them in order
                    for n in reversed(sane_notifications):
                        ts = n.get("timestamp", "")
                        message = n.get("message", "")
                        icon = n.get("icon", "info")
                        channel = n.get("channel", "default")
                        notification_id = str(n.get("id") or n.get("message_id") or "")
                        if not message:
                            continue
                        self.received_msg_signal.emit((channel, message, icon, ts, notification_id, True))
                        emitted_count += 1
                    self.status_signal.emit("ok")
                    if latest_waterline:
                        self._since = latest_waterline
                else:
                    self.status_signal.emit("idle")
                self.diagnostics_signal.emit({
                    "status": "ok" if notifications else "idle",
                    "last_poll": poll_time,
                    "fetched": str(len(notifications)),
                    "emitted": str(emitted_count),
                    "latest": latest_ts,
                    "error": "",
                })
            except NotificationAuthError as e:
                self.logger.warning(f"Page1Worker auth failed: {e}")
                self.status_signal.emit("auth")
                self.diagnostics_signal.emit({
                    "status": "auth",
                    "last_poll": datetime_class.now().astimezone().strftime("%Y/%m/%d %H:%M:%S"),
                    "fetched": "0",
                    "emitted": "0",
                    "latest": "",
                    "error": str(e),
                })
            except NotificationPullError as e:
                self.logger.warning(f"Page1Worker pull HTTP failed: {e}")
                self.status_signal.emit("error")
                self.diagnostics_signal.emit({
                    "status": "error",
                    "last_poll": datetime_class.now().astimezone().strftime("%Y/%m/%d %H:%M:%S"),
                    "fetched": "0",
                    "emitted": "0",
                    "latest": "",
                    "error": str(e),
                })
            except Exception as e:
                self.logger.warning(f"Page1Worker pull failed: {e}")
                self.status_signal.emit("error")
                self.diagnostics_signal.emit({
                    "status": "error",
                    "last_poll": datetime_class.now().astimezone().strftime("%Y/%m/%d %H:%M:%S"),
                    "fetched": "0",
                    "emitted": "0",
                    "latest": "",
                    "error": str(e),
                })
            # Poll every 5 seconds
            for _ in range(50):
                if not self._running:
                    return
                self.msleep(100)


class NotificationListWidget(QListWidget):
    resized = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.verticalScrollBar().setSingleStep(36)
        self._wheel_step_px = 72
        self._wheel_anim = QPropertyAnimation(self.verticalScrollBar(), b"value", self)
        self._wheel_anim.setDuration(180)
        self._wheel_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        # Skip QScroller on macOS only: trackpad-synthesized touch events get
        # hijacked by QScroller's kinetic animation, fighting Qt's native
        # pixelDelta path → stuttery scroll. Other platforms (Windows/Linux,
        # incl. real touchscreens) keep the touch-gesture grab.
        if sys.platform != "darwin":
            QScroller.grabGesture(self.viewport(), QScroller.ScrollerGestureType.TouchGesture)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.resized.emit()

    def wheelEvent(self, event) -> None:
        scroll_bar = self.verticalScrollBar()
        pixel_delta = event.pixelDelta()
        if not pixel_delta.isNull() and pixel_delta.y() != 0:
            self._wheel_anim.stop()
            scroll_bar.setValue(scroll_bar.value() - pixel_delta.y())
            event.accept()
            return

        angle_delta = event.angleDelta().y()
        if angle_delta == 0:
            super().wheelEvent(event)
            return

        if self._wheel_anim.state() == QAbstractAnimation.State.Running:
            base = int(self._wheel_anim.endValue())
        else:
            base = scroll_bar.value()
        step = int(round((angle_delta / 120.0) * self._wheel_step_px))
        target = base - step
        target = max(scroll_bar.minimum(), min(scroll_bar.maximum(), target))

        self._wheel_anim.stop()
        self._wheel_anim.setStartValue(scroll_bar.value())
        self._wheel_anim.setEndValue(target)
        self._wheel_anim.start()
        event.accept()


class MessageBrowser(QTextBrowser):
    context_menu_requested = pyqtSignal(object)
    double_clicked = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._html_content = ""
        self.setObjectName("messageBody")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setOpenExternalLinks(True)
        self.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextBrowserInteraction
            | Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMinimumWidth(0)
        self.setAutoFillBackground(False)
        self.viewport().setAutoFillBackground(False)
        self.viewport().installEventFilter(self)
        self.document().setDocumentMargin(0)
        self.document().setDefaultStyleSheet(_DOC_CSS)

    def set_markdown_content(self, html_content: str, width_hint: int) -> None:
        self._html_content = html_content
        self.setHtml(html_content)
        self.reflow(width_hint)

    def reflow(self, width_hint: int) -> None:
        self.document().setTextWidth(max(120, width_hint))
        height = int(self.document().size().height()) + 4
        self.setFixedHeight(max(1, height))

    def wheelEvent(self, event) -> None:
        # Body is fixed-height with scrollbars disabled; forward wheel up to
        # the enclosing list so hovering over a message doesn't swallow scroll.
        event.ignore()

    def contextMenuEvent(self, event) -> None:
        self.context_menu_requested.emit(event.globalPos())
        event.accept()

    def eventFilter(self, watched, event) -> bool:
        # Any Python exception that escapes here is turned into qFatal()/abort()
        # by PyQt6 via sip_api_parse_result_ex. Observed once on macOS during
        # the first repaint after sleep/wake, so swallow + log instead of dying.
        try:
            if watched is self.viewport():
                etype = event.type()
                if etype == QEvent.Type.ContextMenu:
                    self.context_menu_requested.emit(event.globalPos())
                    event.accept()
                    return True
                if etype == QEvent.Type.MouseButtonDblClick:
                    if event.button() == Qt.MouseButton.LeftButton:
                        self.double_clicked.emit()
                        event.accept()
                        return True
            return super().eventFilter(watched, event)
        except Exception:
            import traceback
            traceback.print_exc()
            return False


class MessageDetailDialog(QDialog):
    def __init__(self, channel: str, message: str, icon_name: str,
                 timestamp: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._message = message
        self.setWindowTitle(channel or "Notification")
        self.resize(920, 720)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(10)

        rel_display, abs_tooltip = format_relative_ts(timestamp)
        channel_display = channel if channel else "default"

        meta_parts = [
            f'<span style="color:#757575; font-size:12px;">{html.escape(rel_display)}</span>'
        ]
        if channel_display and channel_display != "default":
            meta_parts.append(
                f'<span style="background:#bbdefb; color:#1565c0; padding:1px 6px; '
                f'border-radius:3px; font-size:11px; font-weight:bold;">'
                f'{html.escape(channel_display)}</span>'
            )

        meta_row = QHBoxLayout()
        meta_row.setSpacing(8)
        pixmap = load_notification_icon(icon_name)
        if pixmap:
            icon_label = QLabel()
            icon_label.setPixmap(pixmap.scaled(28, 28, Qt.AspectRatioMode.KeepAspectRatio,
                                               Qt.TransformationMode.SmoothTransformation))
            icon_label.setFixedSize(28, 28)
            icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            meta_row.addWidget(icon_label)

        meta_label = QLabel()
        meta_label.setTextFormat(Qt.TextFormat.RichText)
        meta_label.setText("&nbsp;&nbsp;".join(meta_parts))
        meta_label.setToolTip(abs_tooltip)
        meta_row.addWidget(meta_label)
        meta_row.addStretch(1)
        layout.addLayout(meta_row)

        body = QTextBrowser()
        body.setObjectName("messageDetailBody")
        body.setOpenExternalLinks(True)
        body.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextBrowserInteraction
            | Qt.TextInteractionFlag.TextSelectableByMouse
        )
        body.document().setDocumentMargin(8)
        body.document().setDefaultStyleSheet(_DOC_CSS)
        body.setHtml(render_markdown(message))
        layout.addWidget(body, 1)

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        copy_button = QPushButton("Copy message")
        copy_button.clicked.connect(self._copy_message)
        close_button = QPushButton("Close")
        close_button.setObjectName("secondaryButton")
        close_button.clicked.connect(self.accept)
        button_row.addWidget(copy_button)
        button_row.addWidget(close_button)
        layout.addLayout(button_row)

    def _copy_message(self) -> None:
        clipboard = QApplication.clipboard()
        if clipboard:
            clipboard.setText(self._message)


class Page1(QWidget):
    accepted_msg_signal = pyqtSignal(tuple)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.logger = setup_logging("Page1")
        self._settings = QSettings(SETTINGS_ORG, SETTINGS_APP)

        # Load persisted state. `recent_hashes` is a legacy compact ring; the
        # full seen-id set prevents a 500-item /pull replay from cycling the
        # 200-item ring and re-toasting old messages every poll.
        self._recent_hashes: deque[str] = deque(
            json.loads(self._settings.value("recent_hashes", "[]")),
            maxlen=MAX_RECENT_HASHES,
        )
        self._seen_message_ids: set[str] = set()
        self._seen_message_order: deque[str] = deque(maxlen=MAX_SEEN_MESSAGE_IDS)
        try:
            seen_ids = json.loads(self._settings.value("seen_message_ids", "[]"))
        except Exception:
            seen_ids = []
        if isinstance(seen_ids, list):
            for seen_id in seen_ids:
                self._remember_seen_id(str(seen_id))
        for recent_id in list(self._recent_hashes):
            self._remember_seen_id(str(recent_id))
        persisted_msgs = json.loads(self._settings.value("messages", "[]"))
        for message in persisted_msgs:
            if not isinstance(message, dict):
                continue
            fallback_id = notification_message_id(
                message.get("channel", "default"),
                message.get("message", ""),
                message.get("icon", "info"),
                message.get("timestamp", ""),
            )
            persisted_id = message.get("id") or fallback_id
            if persisted_id:
                persisted_id = str(persisted_id)
                self._recent_hashes.append(persisted_id)
                self._remember_seen_id(persisted_id)
            self._remember_seen_id(fallback_id)

        # Session-label filter state (chip row).
        known_labels_raw: str = self._settings.value("known_labels", "") or ""
        self._known_labels: list[str] = [
            l for l in known_labels_raw.split(",") if l
        ]
        saved_filter: str = self._settings.value("filter_label", "") or ""
        self._filter_label: Optional[str] = (
            saved_filter if saved_filter and saved_filter != FILTER_ALL else None
        )
        self._chip_buttons: dict[str, QPushButton] = {}
        # Labels with at least one unviewed message since user last engaged with
        # them. Ephemeral — not persisted across restarts. Cleared by
        # _clear_unread on chip click or opening that label's detail.
        self._unread_labels: set[str] = set()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # -- Toolbar --
        toolbar = QToolBar(self)
        toolbar.setIconSize(QSize(16, 16))
        toolbar.setMovable(False)

        style = self.style()
        assert style is not None

        clear_action = QAction("Clear", self)
        clear_action.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_DialogResetButton))
        clear_action.setToolTip("Clear all notifications")
        assert isinstance(clear_action.triggered, pyqtBoundSignal)
        clear_action.triggered.connect(self._clear_all)
        toolbar.addAction(clear_action)

        self._windows_reminder_action = QAction("Persistent", self)
        self._windows_reminder_action.setCheckable(True)
        self._windows_reminder_action.setChecked(windows_toast_reminder_enabled(self._settings))
        self._windows_reminder_action.setToolTip("Keep Windows toast banners on screen until dismissed")
        assert isinstance(self._windows_reminder_action.toggled, pyqtBoundSignal)
        self._windows_reminder_action.toggled.connect(self._set_windows_toast_reminder)
        toolbar.addAction(self._windows_reminder_action)

        toolbar.addSeparator()

        self._status_label = QLabel("● Starting")
        self._status_label.setObjectName("statusLabel")
        self._status_label.setProperty("status", "running")
        toolbar.addWidget(self._status_label)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        toolbar.addWidget(spacer)

        # Message count label
        self._count_label = QLabel("0 messages")
        self._count_label.setObjectName("hintLabel")
        toolbar.addWidget(self._count_label)

        layout.addWidget(toolbar)

        # -- Diagnostics --
        diagnostics = QWidget(self)
        diagnostics.setObjectName("diagnosticsPanel")
        diag_root = QVBoxLayout(diagnostics)
        diag_root.setContentsMargins(10, 6, 10, 6)
        diag_root.setSpacing(4)

        self._diag_values: dict[str, QLabel] = {}

        row1 = QHBoxLayout()
        row1.setSpacing(18)
        self._add_diag_pair(row1, "Server",
                            f"{cloud_server_protocol}://{cloud_server_ip}:{cloud_server_port}")
        self._add_diag_pair(row1, "Last poll", "never")
        self._add_diag_pair(row1, "Fetched", "0")
        self._add_diag_pair(row1, "Shown", "0")
        row1.addStretch(1)
        diag_root.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(18)
        self._add_diag_pair(row2, "Latest", "never")

        error_caption = QLabel("Last error")
        error_caption.setObjectName("diagLabel")
        row2.addWidget(error_caption)
        self._error_value = QLabel("none")
        self._error_value.setObjectName("diagValue")
        self._error_value.setWordWrap(False)
        self._error_value.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._error_value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        row2.addWidget(self._error_value, 1)
        diag_root.addLayout(row2)

        layout.addWidget(diagnostics)

        self._msg_count = 0
        self._reflow_pending = False

        # -- Session-label filter chips --
        self._chip_scroll = QScrollArea(self)
        self._chip_scroll.setWidgetResizable(True)
        self._chip_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._chip_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._chip_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._chip_scroll.setFixedHeight(36)
        chip_inner = QWidget()
        self._chip_layout = QHBoxLayout(chip_inner)
        self._chip_layout.setContentsMargins(10, 4, 10, 4)
        self._chip_layout.setSpacing(6)
        self._chip_layout.addStretch(1)
        self._chip_scroll.setWidget(chip_inner)
        self._chip_button_group = QButtonGroup(self)
        self._chip_button_group.setExclusive(True)
        self._rebuild_chip_row()
        layout.addWidget(self._chip_scroll)

        # -- Notification list --
        self._list = NotificationListWidget(self)
        self._list.setObjectName("notificationList")
        self._list.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self._list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        assert isinstance(self._list.customContextMenuRequested, pyqtBoundSignal)
        self._list.customContextMenuRequested.connect(self._show_context_menu)
        self._list.itemDoubleClicked.connect(self._open_item_detail)
        self._list.resized.connect(self._schedule_message_reflow)
        self._list.resized.connect(self._position_empty_state)
        self._list.setWordWrap(True)
        self._list.setSpacing(0)
        layout.addWidget(self._list)

        # -- Empty state overlay --
        self._empty_label = QLabel("Waiting for notifications…", self._list.viewport())
        self._empty_label.setObjectName("emptyState")
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_label.hide()

        # Restore persisted messages (no dedup — they're already deduped).
        # Backfill chip row from persisted history first so chips are present
        # before any restored-item filter check runs. persisted_msgs is stored
        # oldest-first; iterate in that order and bubble each label so the
        # last (newest) label ends up leftmost — same LRU rule as live arrival.
        for m in persisted_msgs:
            label = extract_label(m.get("message", ""))
            if self._known_labels[:1] == [label]:
                continue
            try:
                self._known_labels.remove(label)
            except ValueError:
                pass
            self._known_labels.insert(0, label)
        if self._known_labels:
            self._rebuild_chip_row()
            self._persist_chip_state()

        for m in persisted_msgs:
            self._append_item(
                m.get("channel", "default"),
                m.get("message", ""),
                m.get("icon", "info"),
                m.get("timestamp", ""),
                m.get("id", ""),
                persist=False,
            )

        self._update_empty_state()

        # -- Start worker --
        worker = Page1Worker(self.logger, parent=self)
        assert isinstance(worker.received_msg_signal, pyqtBoundSignal)
        worker.received_msg_signal.connect(self.on_received_msg)
        worker.status_signal.connect(self._on_status)
        worker.diagnostics_signal.connect(self._on_diagnostics)
        worker.start()
        self.workers: list[Page1Worker] = [worker]

    def _add_diag_pair(self, layout: QHBoxLayout, label: str, value: str) -> None:
        label_widget = QLabel(label)
        label_widget.setObjectName("diagLabel")
        value_widget = QLabel(value)
        value_widget.setObjectName("diagValue")
        value_widget.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(label_widget)
        layout.addWidget(value_widget)
        self._diag_values[label] = value_widget

    def _message_width_hint(self) -> int:
        viewport_width = self._list.viewport().width()
        if viewport_width < 400:
            viewport_width = 400
        # Row margins (16) + icon column (32) + spacing (8) leave this much
        # for text. Only cap on truly wide windows; normal desktop widths
        # should not wrap early and leave a large blank right edge.
        available_width = viewport_width - MESSAGE_ROW_HORIZONTAL_RESERVE
        return max(120, min(MAX_MESSAGE_BODY_WIDTH, available_width))

    def _position_empty_state(self) -> None:
        vp = self._list.viewport()
        self._empty_label.resize(vp.size())
        self._empty_label.move(0, 0)

    def _update_empty_state(self) -> None:
        is_empty = self._visible_count() == 0
        if is_empty:
            if self._filter_label is not None and self._list.count() > 0:
                self._empty_label.setText(
                    f"No notifications match filter “{self._filter_label}”"
                )
            else:
                self._empty_label.setText("Waiting for notifications…")
            self._position_empty_state()
            self._empty_label.show()
            self._empty_label.raise_()
        else:
            self._empty_label.hide()

    def _schedule_message_reflow(self) -> None:
        if self._reflow_pending:
            return
        self._reflow_pending = True
        QTimer.singleShot(0, self._reflow_message_items)

    def _reflow_message_items(self) -> None:
        self._reflow_pending = False
        width_hint = self._message_width_hint()
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item is None:
                continue
            row_widget = self._list.itemWidget(item)
            if row_widget is None:
                continue
            body = row_widget.findChild(MessageBrowser, "messageBody")
            if body is None:
                continue
            body.reflow(width_hint)
            item.setSizeHint(row_widget.sizeHint())

    def _rebuild_chip_row(self) -> None:
        """Wipe + rebuild the chip row from self._known_labels.

        Cheap (label set stays small). Always reflects current self._filter_label
        as the checked button. The 'All' chip is the leftmost button; its tag
        is FILTER_ALL.
        """
        # Clear existing buttons (keep the trailing stretch).
        for btn in list(self._chip_buttons.values()):
            self._chip_button_group.removeButton(btn)
            btn.deleteLater()
        self._chip_buttons.clear()
        # Layout has [chips...][stretch] — drop everything except the last stretch.
        while self._chip_layout.count() > 1:
            child = self._chip_layout.takeAt(0)
            w = child.widget() if child is not None else None
            if w is not None:
                w.deleteLater()

        # Hide the row entirely until at least one label is known.
        if not self._known_labels:
            self._chip_scroll.setVisible(False)
            return
        self._chip_scroll.setVisible(True)

        def make_chip(text: str, tag: str, checked: bool) -> QPushButton:
            btn = QPushButton(text)
            btn.setObjectName("chipButton")
            btn.setCheckable(True)
            btn.setChecked(checked)
            btn.setProperty("chipTag", tag)
            btn.setProperty(
                "unread", tag != FILTER_ALL and tag in self._unread_labels
            )
            btn.clicked.connect(lambda _checked, t=tag: self._on_chip_clicked(t))
            self._chip_button_group.addButton(btn)
            self._chip_buttons[tag] = btn
            return btn

        all_chip = make_chip("All", FILTER_ALL, checked=self._filter_label is None)
        self._chip_layout.insertWidget(self._chip_layout.count() - 1, all_chip)
        for label in self._known_labels:
            chip = make_chip(label, label, checked=label == self._filter_label)
            self._chip_layout.insertWidget(self._chip_layout.count() - 1, chip)

    def _on_chip_clicked(self, tag: str) -> None:
        # Engaging with a chip = acknowledging it — clear the unread badge.
        if tag != FILTER_ALL:
            self._clear_unread(tag)
        new_filter = None if tag == FILTER_ALL else tag
        if new_filter == self._filter_label:
            return
        self._filter_label = new_filter
        self._apply_filter_all()
        self._persist_chip_state()
        self._refresh_count_label()
        self._update_empty_state()

    def _mark_unread(self, label: str) -> None:
        """Set the unread badge on a single chip without rebuilding the row."""
        if label == FILTER_ALL:
            return
        self._unread_labels.add(label)
        btn = self._chip_buttons.get(label)
        if btn is not None and not bool(btn.property("unread")):
            btn.setProperty("unread", True)
            s = btn.style()
            if s is not None:
                s.unpolish(btn)
                s.polish(btn)

    def _clear_unread(self, label: str) -> None:
        """Clear the unread badge on a single chip without rebuilding the row."""
        if label not in self._unread_labels:
            return
        self._unread_labels.discard(label)
        btn = self._chip_buttons.get(label)
        if btn is not None:
            btn.setProperty("unread", False)
            s = btn.style()
            if s is not None:
                s.unpolish(btn)
                s.polish(btn)

    def _touch_label(self, content: str) -> None:
        """Bubble the message's session label to the front of the chip row.

        Most-recently-active session ends up leftmost (right after All), so
        the chip you most likely want to drill into is closest to your eye.
        Skip the rebuild if the label is already at the front — avoids
        flicker on consecutive messages from the same session.

        Also flags the label as "unread" so the chip is visually highlighted
        until the user engages with it (clicks the chip or opens one of its
        notifications).
        """
        label = extract_label(content)
        if self._known_labels[:1] == [label]:
            # Already at front — just update the unread state on this chip.
            self._mark_unread(label)
            return
        try:
            self._known_labels.remove(label)
        except ValueError:
            pass
        self._known_labels.insert(0, label)
        self._unread_labels.add(label)  # picked up by _rebuild_chip_row
        self._rebuild_chip_row()
        self._persist_chip_state()

    def _matches_current_filter(self, content: str) -> bool:
        if self._filter_label is None:
            return True
        return extract_label(content) == self._filter_label

    def _apply_filter_to_item(self, item: QListWidgetItem, content: str) -> None:
        item.setHidden(not self._matches_current_filter(content))

    def _apply_filter_all(self) -> None:
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item is None:
                continue
            data = item.data(Qt.ItemDataRole.UserRole)
            content = data.get("message", "") if isinstance(data, dict) else ""
            self._apply_filter_to_item(item, content)

    def _visible_count(self) -> int:
        n = 0
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item is not None and not item.isHidden():
                n += 1
        return n

    def _refresh_count_label(self) -> None:
        n = self._visible_count() if self._filter_label is not None else self._msg_count
        suffix = "s" if n != 1 else ""
        if self._filter_label is not None:
            self._count_label.setText(f"{n} of {self._msg_count} message{suffix}")
        else:
            self._count_label.setText(f"{n} message{suffix}")

    def _persist_chip_state(self) -> None:
        self._settings.setValue("known_labels", ",".join(self._known_labels))
        self._settings.setValue(
            "filter_label", self._filter_label if self._filter_label else FILTER_ALL
        )

    @pyqtSlot(bool)
    @safe_slot
    def _set_windows_toast_reminder(self, enabled: bool) -> None:
        self._settings.setValue(WINDOWS_TOAST_REMINDER_KEY, bool(enabled))

    @pyqtSlot(tuple)  # type: ignore
    @safe_slot
    def on_received_msg(self, msg: tuple) -> None:
        # (channel, content, icon, timestamp, id, should_notify)
        channel = msg[0] if msg[0] else "default"
        content = msg[1]
        icon_name = msg[2] if len(msg) > 2 else "info"
        timestamp = msg[3] if len(msg) > 3 else ""
        notification_id = str(msg[4]) if len(msg) > 4 and msg[4] else ""
        should_notify = bool(msg[5]) if len(msg) > 5 else True

        # Prefer the server message id. Fall back only for legacy server data.
        fallback_id = notification_message_id(channel, content, icon_name, timestamp)
        h = notification_id or fallback_id
        if h in self._seen_message_ids or fallback_id in self._seen_message_ids:
            self.logger.debug(f"Dropping duplicate message: {content[:50]}")
            self._remember_seen_id(h)
            self._remember_seen_id(fallback_id)
            return
        self._remember_seen_id(h)
        self._remember_seen_id(fallback_id)

        self._touch_label(content)
        self._append_item(channel, content, icon_name, timestamp, notification_id, persist=True)
        self._persist_state()
        if should_notify:
            self.accepted_msg_signal.emit((channel, content, icon_name, timestamp, h))

    def _append_item(self, channel: str, content: str, icon_name: str,
                     timestamp: str, notification_id: str = "", persist: bool = False) -> None:
        rel_display, abs_tooltip = format_relative_ts(timestamp)

        channel_display = channel if channel else "default"

        # Capture scroll position before inserting so we only jump to the new
        # message when the user was already watching the top of the stream.
        scroll_bar = self._list.verticalScrollBar()
        was_at_top = True
        previous_scroll_value = 0
        if scroll_bar is not None:
            previous_scroll_value = scroll_bar.value()
            was_at_top = previous_scroll_value <= 40

        # Build the notification widget with icon
        item = QListWidgetItem()
        notification_id = notification_id or notification_message_id(channel, content, icon_name, timestamp)
        # Store full message dict for persistence/copy
        item.setData(Qt.ItemDataRole.UserRole, {
            "id": notification_id,
            "channel": channel,
            "message": content,
            "icon": icon_name,
            "timestamp": timestamp,
        })

        row_widget = QWidget()
        row_layout = QHBoxLayout(row_widget)
        row_layout.setContentsMargins(8, 8, 8, 8)
        row_layout.setSpacing(8)

        # Icon
        pixmap = load_notification_icon(icon_name)
        icon_label = QLabel()
        if pixmap:
            icon_label.setPixmap(pixmap.scaled(32, 32, Qt.AspectRatioMode.KeepAspectRatio,
                                               Qt.TransformationMode.SmoothTransformation))
        icon_label.setFixedSize(32, 32)
        icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row_layout.addWidget(icon_label, 0, Qt.AlignmentFlag.AlignTop)

        # Text content
        text_container = QWidget()
        text_layout = QVBoxLayout(text_container)
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(4)

        meta_parts = [
            f'<span style="color:#757575; font-size:12px;">{html.escape(rel_display)}</span>'
        ]
        if channel_display and channel_display != "default":
            meta_parts.append(
                f'<span style="background:#bbdefb; color:#1565c0; padding:1px 6px; '
                f'border-radius:3px; font-size:11px; font-weight:bold;">'
                f'{html.escape(channel_display)}</span>'
            )
        meta_label = QLabel()
        meta_label.setTextFormat(Qt.TextFormat.RichText)
        meta_label.setText("&nbsp;&nbsp;".join(meta_parts))
        meta_label.setToolTip(abs_tooltip)
        text_layout.addWidget(meta_label)

        body = MessageBrowser()
        body.context_menu_requested.connect(
            lambda global_pos, item=item: self._show_context_menu_for_item(item, global_pos)
        )
        body.double_clicked.connect(lambda item=item: self._open_item_detail(item))
        body.set_markdown_content(render_markdown(content), self._message_width_hint())
        text_layout.addWidget(body)
        row_layout.addWidget(text_container, 1)

        self._list.insertItem(0, item)
        self._list.setItemWidget(item, row_widget)
        item.setSizeHint(row_widget.sizeHint())
        self._apply_filter_to_item(item, content)
        if was_at_top:
            self._list.scrollToTop()
        elif scroll_bar is not None:
            scroll_bar.setValue(previous_scroll_value + item.sizeHint().height() + self._list.spacing())

        self._msg_count += 1
        self._refresh_count_label()
        self._update_empty_state()

    def _persist_state(self) -> None:
        """Save messages and hash ring to QSettings."""
        # Store oldest-first so restore can replay into the newest-first UI.
        messages = []
        limit = min(self._list.count(), MAX_PERSISTED_MESSAGES)
        for i in range(limit - 1, -1, -1):
            item = self._list.item(i)
            if item:
                data = item.data(Qt.ItemDataRole.UserRole)
                if isinstance(data, dict):
                    messages.append(data)
        self._settings.setValue("messages", json.dumps(messages))
        self._settings.setValue("recent_hashes", json.dumps(list(self._recent_hashes)))
        self._settings.setValue("seen_message_ids", json.dumps(list(self._seen_message_order)))

    def _remember_seen_id(self, message_id: str) -> None:
        message_id = str(message_id or "")
        if not message_id or message_id in self._seen_message_ids:
            return
        if len(self._seen_message_order) == self._seen_message_order.maxlen:
            old_id = self._seen_message_order[0]
            self._seen_message_ids.discard(old_id)
        self._seen_message_order.append(message_id)
        self._seen_message_ids.add(message_id)
        self._recent_hashes.append(message_id)

    @pyqtSlot(str)
    @safe_slot
    def _on_status(self, status: str) -> None:
        status_text = {
            "running": "Polling",
            "ok": "Connected",
            "idle": "Idle",
            "auth": "Auth failed",
            "error": "Error",
        }.get(status, status)
        style_state = {
            "ok": "complete",
            "auth": "error",
        }.get(status, status if status in {"running", "idle", "error"} else "idle")
        self._status_label.setText(f"● {status_text}")
        self._status_label.setToolTip(f"{cloud_server_protocol}://{cloud_server_ip}:{cloud_server_port}")
        self._status_label.setProperty("status", style_state)
        s = self._status_label.style()
        if s is not None:
            s.unpolish(self._status_label)
            s.polish(self._status_label)

    @pyqtSlot(dict)
    @safe_slot
    def _on_diagnostics(self, data: dict) -> None:
        values = {
            "Last poll": str(data.get("last_poll", "never") or "never"),
            "Fetched": str(data.get("fetched", "0") or "0"),
            "Shown": str(data.get("emitted", "0") or "0"),
            "Latest": format_display_ts(str(data.get("latest", "") or "")),
        }
        for key, value in values.items():
            label = self._diag_values.get(key)
            if label is not None:
                label.setText(value)
                label.setToolTip("")
        error = str(data.get("error", "") or "")
        self._error_value.setText(error if error else "none")
        self._error_value.setToolTip(error)

    def _clear_all(self) -> None:
        if self._msg_count > 0:
            from PyQt6.QtWidgets import QMessageBox
            reply = QMessageBox.question(
                self,
                "Clear notifications",
                f"Remove all {self._msg_count} stored notifications? This cannot be undone.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
        self._list.clear()
        self._msg_count = 0
        self._refresh_count_label()
        self._persist_state()
        self._update_empty_state()

    def _show_context_menu(self, pos) -> None:
        item = self._list.itemAt(pos)
        if item is None:
            return
        self._show_context_menu_for_item(item, self._list.viewport().mapToGlobal(pos))

    def _open_item_detail(self, item: QListWidgetItem) -> None:
        data = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(data, dict):
            return
        # Opening this notification = acknowledging its session.
        self._clear_unread(extract_label(data.get("message", "")))
        dialog = MessageDetailDialog(
            data.get("channel", "default"),
            data.get("message", ""),
            data.get("icon", "info"),
            data.get("timestamp", ""),
            self,
        )
        dialog.exec()

    def open_message_by_id(self, notification_id: str) -> bool:
        if not notification_id:
            return False
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item is None:
                continue
            data = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(data, dict) and data.get("id") == notification_id:
                self._open_item_detail(item)
                return True
        return False

    def _show_context_menu_for_item(self, item: QListWidgetItem, global_pos) -> None:
        menu = QMenu(self)
        copy_action = menu.addAction("Copy message")
        action = menu.exec(global_pos)
        if action == copy_action:
            data = item.data(Qt.ItemDataRole.UserRole)
            content = data.get("message", "") if isinstance(data, dict) else str(data or "")
            if content:
                clipboard = QApplication.clipboard()
                if clipboard:
                    clipboard.setText(content)

    def shutdown(self) -> None:
        """Persist state and stop workers. Safe to call from aboutToQuit so the
        tray-Quit path goes through the same cleanup as a real window close."""
        self._persist_state()
        for w in self.workers:
            w.stop()
        for w in self.workers:
            w.wait(2000)

    def closeEvent(self, event) -> None:
        self.shutdown()
        super().closeEvent(event)
