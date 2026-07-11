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

import sys
import os
import json

import ctypes
if os.name == "nt":
    import win32con
    import win32gui
    import win32process
    import psutil


    class WindowController(QObject):
        @staticmethod
        def find_window_by_process_name(process_name: str) -> Optional[int]:
            def callback(hwnd: int, hwnds: list[int]) -> bool:
                _, found_pid = win32process.GetWindowThreadProcessId(hwnd)
                if found_pid in pids:
                    hwnds.append(hwnd)
                return True

            hwnds: list[int] = []
            pids = [p.pid for p in psutil.process_iter() if p.name().lower() == process_name.lower()]
            win32gui.EnumWindows(callback, hwnds)
            if hwnds:
                return hwnds[0]
            return None

        @staticmethod
        def disable_close_button(hWnd: int) -> None:
            menu = win32gui.GetSystemMenu(hWnd, False)
            if menu:
                win32gui.EnableMenuItem(menu, win32con.SC_CLOSE, win32con.MF_BYCOMMAND | win32con.MF_GRAYED)
            win32gui.SetWindowPos(hWnd, win32con.NULL, 0, 0, 0, 0,
                                win32con.SWP_NOSIZE | win32con.SWP_NOMOVE | win32con.SWP_NOZORDER | win32con.SWP_DRAWFRAME)
            win32gui.UpdateWindow(hWnd)

        @staticmethod
        def find_windows_by_title_text(text: str) -> list[int]:
            windows: list[int] = []

            def callback(hwnd: int, _: Any) -> None:
                title = win32gui.GetWindowText(hwnd)
                if text in title:
                    windows.append(hwnd)

            win32gui.EnumWindows(callback, None)
            return windows


    class ProcessInfo:
        def __init__(self, name: str) -> None:
            self.name = name
            self.proceed = False


    class ProcessListModel(QAbstractListModel):
        model_updated = pyqtSignal(list)

        def __init__(self) -> None:
            super().__init__()
            self.processes: list[ProcessInfo] = []

        def rowCount(self, parent: Optional[QModelIndex] = None) -> int:
            return len(self.processes)

        def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
            if role == Qt.ItemDataRole.DisplayRole:
                return self.processes[index.row()].name
            elif role == Qt.ItemDataRole.CheckStateRole:
                return Qt.CheckState.Checked if self.processes[index.row()].proceed else Qt.CheckState.Unchecked
            return None

        def to_list_names(self) -> list[str]:
            return [p.name for p in self.processes]

        def add_process(self, name: str) -> None:
            self.beginInsertRows(QModelIndex(), self.rowCount(), self.rowCount())
            self.processes.append(ProcessInfo(name))
            self.endInsertRows()
            assert isinstance(self.model_updated, pyqtBoundSignal)
            print("emit ", self.to_list_names())
            self.model_updated.emit(self.to_list_names())

        def remove_process(self, row: int) -> None:
            self.beginRemoveRows(QModelIndex(), row, row)
            self.processes.pop(row)
            self.endRemoveRows()
            assert isinstance(self.model_updated, pyqtBoundSignal)
            print("emit ", self.to_list_names())
            self.model_updated.emit(self.to_list_names())


    class Page2Worker(QThread):
        def __init__(self, parent: Optional[QObject] = None):
            super().__init__(parent)
            self.process_names: list[str] = []

        def run(self) -> None:
            while True:
                print("run", self.process_names)
                for process in self.process_names:
                    hwnds = WindowController.find_windows_by_title_text(process)
                    if hwnds:
                        for hwnd in hwnds:
                            WindowController.disable_close_button(hwnd)
                time.sleep(20)
            pass

        @pyqtSlot(list)  # type: ignore
        def set_processes(self, process_names: list[str]) -> None:
            print("set_processes", process_names)
            self.process_names = process_names


    class Page2(QWidget):
        def __init__(self, parent: Optional[QWidget] = None) -> None:
            super().__init__(parent)
            self.model = ProcessListModel()
            self.process: list[str] = []
            self.init_ui()
            # load the processes list from the preferences file
            self.start_task()
            print("task started")
            self.load_processes()

        def init_ui(self) -> None:
            self.resize(400, 300)
            self.setWindowTitle("Process Monitor")

            self.setLayout(QVBoxLayout(self))
            self.layout().addWidget(QLabel("Monitored Processes"))
            layout: QBoxLayout = cast(QBoxLayout, self.layout())

            self.list_view = QListView(self)
            self.list_view.setModel(self.model)
            layout.addWidget(self.list_view)

            hbox = QHBoxLayout()
            layout.addLayout(hbox)

            self.process_edit = QLineEdit(self)
            hbox.addWidget(self.process_edit)

            add_button = QPushButton("Add", self)
            connect_safe(add_button.clicked, self.add_process)

            hbox.addWidget(add_button)

            remove_button = QPushButton("Remove", self)
            connect_safe(remove_button.clicked, self.remove_process)

            hbox.addWidget(remove_button)

        def add_process(self) -> None:
            process_name = self.process_edit.text()
            if process_name:
                self.model.add_process(process_name)
                self.process_edit.clear()
                self.save_processes()

        def remove_process(self) -> None:
            indexes = self.list_view.selectedIndexes()
            if indexes:
                index = indexes[0]
                self.model.remove_process(index.row())
                self.save_processes()

        def save_processes(self) -> None:
            with open("processes.json", "w") as f:
                process_names = []
                for process in self.model.processes:
                    process_names.append(process.name)
                json.dump(process_names, f)

        def load_processes(self) -> None:
            if os.path.exists("processes.json"):
                with open("processes.json") as f:
                    processes = json.load(f)
                    for process in processes:
                        self.model.add_process(process)

        def start_task(self) -> None:
            # start a new worker thread
            self.worker = Page2Worker(self)
            assert (isinstance(self.model.model_updated, pyqtBoundSignal))
            print("connected self.worker.set_processes")
            self.model.model_updated.connect(self.worker.set_processes)
            self.worker.start()
else:
    class Page2Void(QWidget):
        def __init__(self, parent: Optional[QWidget] = None) -> None:
            super().__init__(parent)
            self.setLayout(QVBoxLayout())
            self.layout().addWidget(QLabel("Process Monitor (Windows only)"))