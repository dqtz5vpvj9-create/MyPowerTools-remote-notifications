import json

from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication

from powertool.notification_ids import notification_message_id
from powertool.page1 import MessageDetailDialog, SETTINGS_APP, SETTINGS_ORG


def open_persisted_message(notification_id: str) -> bool:
    if not notification_id:
        return False
    settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
    try:
        messages = json.loads(settings.value("messages", "[]"))
    except Exception:
        messages = []

    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        message_id = message.get("id") or notification_message_id(
            message.get("channel", "default"),
            message.get("message", ""),
            message.get("icon", "info"),
            message.get("timestamp", ""),
        )
        if message_id != notification_id:
            continue

        app = QApplication.instance() or QApplication([])
        dialog = MessageDetailDialog(
            message.get("channel", "default"),
            message.get("message", ""),
            message.get("icon", "info"),
            message.get("timestamp", ""),
            None,
        )
        dialog.exec()
        return True
    return False
