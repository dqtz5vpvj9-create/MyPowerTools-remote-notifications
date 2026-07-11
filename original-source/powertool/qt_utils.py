import importlib, sys, os
from os.path import dirname, pardir
from pathlib import Path
def import_parents(level: int = 1) -> None:
    global __package__
    file = Path(__file__).resolve()
    parent, top = file.parent, file.parents[level]
    
    sys.path.append(str(top))
#    try:
#        sys.path.remove(str(parent))
#    except ValueError: # already removed
#        pass

    __package__ = '.'.join(parent.parts[len(top.parts):])
    importlib.import_module(__package__) # won't be needed after that

if __name__ == '__main__' and (__package__ is None or len(__package__) == 0):
    import_parents()
sys.path.append(dirname(__file__) + os.sep + pardir)
from py_modules.logging_lib import setup_logging, LogLevel, MyLogger
logger = setup_logging()
from py_modules.check_interpreter import check_conda_interpreter, CONDA_ENV_NAME

import logging
import sys
from PyQt6.QtWidgets import *
from PyQt6.QtGui import *
from PyQt6.QtCore import Qt, pyqtBoundSignal, pyqtSignal, QObject, QAbstractListModel, QModelIndex, pyqtSlot, QThread, QRectF
from typing import cast, Any, Optional

import threading
import time
from py_modules.logging_lib import setup_logging
from py_modules.simple_http_notification_conf import cloud_server_protocol, cloud_server_ip, cloud_server_port
from py_modules.simple_http_notification_receiver import SimpleHttpNotificationReceiver
from py_modules.lib_network import WifiStatus, check_wifi_status, get_wifi_interface

def create_pixmap(status: WifiStatus, ssid: str) -> QPixmap:
    color = {
        WifiStatus.NOT_CONNECTED: "red",
        WifiStatus.CONNECTED_TO_HOTSPOT: "yellow",
        WifiStatus.CONNECTED_WITH_INTERNET: "green",
    }[status]

    # Create a circle pixmap of the specified color
    pixmap = QPixmap(32, 32)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(color))
    painter.drawEllipse(0, 0, 32, 32)

    if ssid:
        # Draw the first unicode character of the SSID on the pixmap
        painter.setPen(QColor("black"))
        painter.setFont(QFont("Arial", 16))
        l = min(len(ssid), 3)
        painter.drawText(QRectF(0, 0, 32, 32), Qt.AlignmentFlag.AlignCenter, ssid[0:l])

    painter.end()

    return pixmap



def connect_safe(signal: Any, slot: 'PYQT_SLOT') -> None:
    bound_signal = cast(pyqtBoundSignal, signal)
    bound_signal.connect(slot)


def safe_slot(fn):
    """Wrap a Qt slot so any Python exception is logged but not propagated.
    PyQt6 ≤6.4 turns unhandled slot exceptions into qFatal()/abort(); apply
    this to anything dispatched via QueuedConnection (e.g. cross-thread
    signal targets) so a one-off ValueError can't kill the app."""
    import functools
    import traceback
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception:
            traceback.print_exc()
            return None
    return wrapper