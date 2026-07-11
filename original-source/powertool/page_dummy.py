
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
from powertool.qt_utils import connect_safe
from py_modules.lib_sh import shell_run, ssh_command

import sys
import os
import json

from PyQt6.QtCore import QThread, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QTextEdit, QPushButton
import subprocess
import tempfile
import os

class DummyPage(QWidget):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setLayout(QVBoxLayout(self))
        self.label = QLabel(self)
        self.label.setText("Dummy Page")

        # Command selection
        self.layout().addWidget(self.label)
