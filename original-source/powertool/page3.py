import os
import sys
import importlib
import types
import subprocess
import tempfile
import sqlite3
import multiprocessing
import threading
from datetime import datetime
from typing import Optional

import yaml
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTextEdit, QPlainTextEdit,
    QPushButton, QLabel, QComboBox, QCheckBox, QListView, QSplitter,
    QToolBar, QLineEdit, QDialog, QDialogButtonBox, QApplication,
    QMessageBox, QSizePolicy, QStyle,
)
from PyQt6.QtGui import (
    QFont, QDesktopServices, QShortcut, QKeySequence, QAction,
)
from PyQt6.QtCore import (
    Qt, pyqtSignal, QObject, QAbstractListModel, QModelIndex,
    pyqtSlot, QThread, QUrl, QSettings, QSortFilterProxyModel,
    QRegularExpression,
)

from py_modules.logging_lib import setup_logging
from py_modules.lib_sh import shell_run, ssh_command, scp_command
from py_modules.lib_aosp_base import aosp_host_working_dir, miniconda
from powertool.frozen_path import data_dir, bundled_dir

logger = setup_logging()

SETTINGS_ORG = "AndroidTools"
SETTINGS_APP = "Page3"
DEFAULT_HOST = "r743"
COMMAND_TOOLS_FILE = "command_tools.py"
COMMAND_TOOLS_MODULE_NAME = "_powertool_runtime_command_tools"


def get_settings() -> QSettings:
    return QSettings(SETTINGS_ORG, SETTINGS_APP)


def load_commands_from_yaml(file_path='commands.yaml'):
    file_path = os.path.join(data_dir(), file_path)
    try:
        with open(file_path, 'r') as file:
            data = yaml.safe_load(file)
        return data['commands']
    except (FileNotFoundError, yaml.YAMLError, KeyError, TypeError) as e:
        logger.error(f"Failed to load commands from {file_path}: {e}")
        return []


def save_commands_to_yaml(commands, file_path='commands.yaml'):
    file_path = os.path.join(data_dir(), file_path)
    with open(file_path, 'w') as file:
        yaml.dump({'commands': commands}, file, default_flow_style=False, allow_unicode=True)


def load_command_tools_namespace() -> dict[str, object]:
    modules = load_command_tools_modules()
    public_values: dict[str, object] = {}
    for module in modules:
        public_values.update(
            {
                name: value
                for name, value in vars(module).items()
                if not name.startswith("_")
            }
        )

    command_tools_proxy = types.ModuleType(COMMAND_TOOLS_MODULE_NAME)
    for name, value in public_values.items():
        setattr(command_tools_proxy, name, value)

    namespace = dict(public_values)
    namespace["command_tools"] = command_tools_proxy
    namespace["command_tools_files"] = [
        getattr(module, "__file__", "")
        for module in modules
        if getattr(module, "__file__", "")
    ]
    command_tools_proxy.__file__ = ", ".join(namespace["command_tools_files"])
    return namespace


def load_command_tools_modules():
    modules = []
    seen_paths = set()
    for root in (bundled_dir(), data_dir()):
        path = os.path.join(root, COMMAND_TOOLS_FILE)
        if path in seen_paths:
            continue
        seen_paths.add(path)
        if not os.path.exists(path):
            continue

        module = types.ModuleType(f"{COMMAND_TOOLS_MODULE_NAME}_{len(modules)}")
        module.__file__ = path
        module.__package__ = "powertool"
        sys.modules[module.__name__] = module
        with open(path, "r", encoding="utf-8") as file:
            source = file.read()
        exec(compile(source, path, "exec"), module.__dict__)
        modules.append(module)

    if modules:
        return modules

    from powertool import command_tools
    return [importlib.reload(command_tools)]


def load_command_tools_module():
    return load_command_tools_modules()[-1]


def run_python_command(command: str, input_text: str) -> object:
    namespace = dict(globals())
    namespace.update(load_command_tools_namespace())
    try:
        command_func = eval(command.strip(), namespace)
    except NameError as e:
        loaded_files = namespace.get("command_tools_files", [])
        if isinstance(loaded_files, list) and loaded_files:
            files_text = ", ".join(str(path) for path in loaded_files)
            raise NameError(f"{e}. Loaded command_tools.py: {files_text}") from e
        raise
    if not callable(command_func):
        raise TypeError(f"Python command must resolve to a callable: {command}")
    return command_func(input_text)


class ReloadableComboBox(QComboBox):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._load_commands()

    def _load_commands(self):
        self.commands = load_commands_from_yaml()
        if self.commands:
            for command in self.commands:
                host = command.get('host', DEFAULT_HOST)
                self.addItem(f"[{host}] {command['label']}: {command['command']}")
        else:
            self.addItem("(no commands available)")

    def showPopup(self):
        self.clear()
        self._load_commands()
        super().showPopup()


class YamlEditorDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit commands.yaml")
        self.resize(750, 550)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        self.editor = QPlainTextEdit(self)
        self.editor.setFont(_preferred_font())
        layout.addWidget(self.editor)

        hint = QLabel(
            "Format: YAML with top-level 'commands' list.\n"
            "Each entry: label, command, type (py/shell), host (optional, default: r743).\n"
            "py commands can name a function from command_tools.py or use a lambda/expression.",
            self,
        )
        hint.setObjectName("hintLabel")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        cancel_btn = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        if cancel_btn:
            cancel_btn.setObjectName("secondaryButton")
        layout.addWidget(buttons)

        self._load()

    def _load(self):
        path = os.path.join(data_dir(), 'commands.yaml')
        try:
            with open(path, 'r') as f:
                self.editor.setPlainText(f.read())
        except FileNotFoundError:
            self.editor.setPlainText(
                "commands:\n"
                "  - label: Example\n"
                "    command: echo hello\n"
                "    type: shell\n"
                "    host: r743\n"
            )

    def _save(self):
        text = self.editor.toPlainText()
        try:
            data = yaml.safe_load(text)
            if not isinstance(data, dict) or 'commands' not in data:
                raise ValueError("YAML must have a top-level 'commands' key")
        except Exception as e:
            QMessageBox.warning(self, "Invalid YAML", str(e))
            return
        path = os.path.join(data_dir(), 'commands.yaml')
        with open(path, 'w') as f:
            f.write(text)
        self.accept()


class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.resize(450, 220)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        # External editor
        editor_row = QHBoxLayout()
        editor_row.addWidget(QLabel("External editor command:"))
        self.editor_input = QLineEdit(self)
        self.editor_input.setPlaceholderText("code")
        settings = get_settings()
        self.editor_input.setText(settings.value("external_editor", "code"))
        editor_row.addWidget(self.editor_input)
        layout.addLayout(editor_row)

        # Default host
        host_row = QHBoxLayout()
        host_row.addWidget(QLabel("Default SSH host:"))
        self.host_input = QLineEdit(self)
        self.host_input.setPlaceholderText(DEFAULT_HOST)
        self.host_input.setText(settings.value("default_host", DEFAULT_HOST))
        host_row.addWidget(self.host_input)
        layout.addLayout(host_row)

        layout.addStretch()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        cancel_btn = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        if cancel_btn:
            cancel_btn.setObjectName("secondaryButton")
        layout.addWidget(buttons)

    def _save(self):
        settings = get_settings()
        settings.setValue("external_editor", self.editor_input.text().strip() or "code")
        settings.setValue("default_host", self.host_input.text().strip() or DEFAULT_HOST)
        self.accept()


class SSHWorker(QThread):
    result_signal = pyqtSignal(str)
    append_signal = pyqtSignal(str)
    status_signal = pyqtSignal(str)

    def __init__(self, command_data: dict, input1: str, input2: str,
                 host: str, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.command_data = command_data
        self.input1 = input1
        self.input2 = input2
        self.host = host
        self._cancelled = False
        if os.name == 'nt':
            self._stop_event = multiprocessing.Event()
        else:
            self._stop_event = threading.Event()  # type: ignore[assignment]

    def cancel(self) -> None:
        self._cancelled = True
        self._stop_event.set()

    def run(self) -> None:
        command_type = self.command_data['type']
        command = self.command_data['command']

        if command_type == "py":
            try:
                result = run_python_command(command, self.input1)
                self.result_signal.emit(str(result))
            except Exception as e:
                self.result_signal.emit(f"Error: {e}")
        elif command_type == "shell":
            tmp1 = tempfile.mktemp()
            tmp2 = tempfile.mktemp()
            with open(tmp1, "w") as f1, open(tmp2, "w") as f2:
                f1.write(self.input1)
                f2.write(self.input2)
            file1_base = os.path.basename(tmp1)
            file2_base = os.path.basename(tmp2)
            host = self.host
            try:
                if self._cancelled:
                    return
                self.status_signal.emit("Uploading input files...")
                subprocess.run(scp_command(f'{tmp1} {tmp2} {host}:/tmp/'), shell=True)
                if self._cancelled:
                    self.status_signal.emit("Cancelled")
                    return
                self.status_signal.emit("Executing remote command...")
                cmd = ssh_command(host, f"CONDA_EXE={miniconda}/bin/conda {command} --file1 /tmp/{file1_base} --file2 /tmp/{file2_base}", True)
                try:
                    stdout, stderr = shell_run(
                        cmd,
                        callback=lambda line: self.append_signal.emit(f"{line}\n"),
                        silent=False,
                        check_error=True,
                        stop_event=self._stop_event,
                    )
                    if self._cancelled:
                        self.status_signal.emit("Cancelled")
                    else:
                        self.status_signal.emit("Execution complete")
                        msg = f"{stdout}\n{stderr}"
                        self.result_signal.emit(msg)
                except Exception as e:
                    if self._cancelled:
                        self.status_signal.emit("Cancelled")
                    else:
                        logger.exception(f"Command execution error: {e}")
                        self.status_signal.emit("Execution failed")
                        self.result_signal.emit(f"Error: {e}")
            except Exception as e:
                self.status_signal.emit("Execution failed")
                err_msg = f"{e}"
                if hasattr(e, 'stdout'):
                    err_msg += f"\n{e.stdout}\n{e.stderr}"
                self.result_signal.emit(err_msg)
            finally:
                for f in (tmp1, tmp2):
                    try:
                        os.unlink(f)
                    except OSError:
                        pass
                try:
                    cleanup_cmd = ssh_command(host, f"rm -f /tmp/{file1_base} /tmp/{file2_base}", False)
                    subprocess.run(cleanup_cmd, shell=True, timeout=10)
                except Exception:
                    pass


def _preferred_font() -> QFont:
    preferred_fonts = ["JetBrains Mono", "Consolas", "Courier New", "Microsoft YaHei", "monospace"]
    font = QFont()
    for family in preferred_fonts:
        font.setFamily(family)
        if font.family() == family:
            break
    return font


class HistoryItem:
    def __init__(
        self,
        history_id: int,
        input1: str,
        input2: str,
        timestamp: str,
        command_idx: Optional[int] = None,
        command_label: str = "",
        command_text: str = "",
        command_type: str = "",
        command_host: str = "",
        second_input_enabled: bool = False,
        output: str = "",
    ):
        self.history_id = history_id
        self.input1 = input1
        self.input2 = input2
        self.timestamp = timestamp
        self.command_idx = command_idx
        self.command_label = command_label
        self.command_text = command_text
        self.command_type = command_type
        self.command_host = command_host
        self.second_input_enabled = second_input_enabled
        self.output = output


class HistoryListModel(QAbstractListModel):
    def __init__(self, *args, cursor=None, chunk_size=20, **kwargs):
        super().__init__(*args, **kwargs)
        self.items: list[HistoryItem] = []
        self.cursor = cursor
        self.chunk_size = chunk_size
        self.total_count = self.get_total_count()

    def data(self, index, role):
        if not index.isValid() or index.row() >= len(self.items):
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            item = self.items[index.row()]
            input1_preview = self.get_preview(item.input1)
            input2_preview = self.get_preview(item.input2)
            command_preview = item.command_label or item.command_text or "(unknown command)"
            output_preview = self.get_preview(item.output, max_lines=2, max_chars=400)
            return (
                f"Item {index.row()}: {item.timestamp}\n"
                f"Command: {command_preview}\n"
                f"Input1:\n{input1_preview}\n"
                f"Input2:\n{input2_preview}\n"
                f"Output:\n{output_preview}"
            )

    def get_preview(self, text, max_lines=3, max_chars=1000):
        lines = text.splitlines()
        if len(lines) > max_lines:
            lines = lines[:max_lines]
            text = '\n'.join(lines) + '\n...'
        else:
            text = '\n'.join(lines)
        if len(text) > max_chars:
            text = text[:max_chars] + '...'
        magic_replace = {
            aosp_host_working_dir: "WD",
        }
        for key, value in magic_replace.items():
            text = text.replace(key, value)
        return text

    def rowCount(self, index=QModelIndex()):
        return len(self.items)

    def canFetchMore(self, index):
        return len(self.items) < self.total_count

    def fetchMore(self, index):
        remainder = self.total_count - len(self.items)
        items_to_fetch = min(remainder, self.chunk_size)
        if items_to_fetch > 0:
            self.beginInsertRows(QModelIndex(), len(self.items), len(self.items) + items_to_fetch - 1)
            self.load_history_chunk(len(self.items), items_to_fetch)
            self.endInsertRows()
            logger.info(f"Loaded {items_to_fetch} more items")

    def get_total_count(self):
        self.cursor.execute("SELECT COUNT(*) FROM history")
        return self.cursor.fetchone()[0]

    def load_history_chunk(self, offset, limit):
        self.cursor.execute(
            """
            SELECT
                id,
                input1,
                input2,
                timestamp,
                command_idx,
                command_label,
                command_text,
                command_type,
                command_host,
                second_input_enabled,
                output
            FROM history
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        )
        data = self.cursor.fetchall()
        items = [
            HistoryItem(
                history_id,
                input1,
                input2,
                timestamp,
                command_idx,
                command_label or "",
                command_text or "",
                command_type or "",
                command_host or "",
                bool(second_input_enabled),
                output or "",
            )
            for (
                history_id,
                input1,
                input2,
                timestamp,
                command_idx,
                command_label,
                command_text,
                command_type,
                command_host,
                second_input_enabled,
                output,
            ) in data
        ]
        self.items.extend(items)


class HistoryFilterProxy(QSortFilterProxyModel):
    def filterAcceptsRow(self, source_row, source_parent):
        index = self.sourceModel().index(source_row, 0, source_parent)
        data = self.sourceModel().data(index, Qt.ItemDataRole.DisplayRole)
        if data is None:
            return False
        pattern = self.filterRegularExpression().pattern()
        if not pattern:
            return True
        return pattern.lower() in data.lower()


class Page3(QWidget):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.worker: Optional[SSHWorker] = None
        self._current_history_id: Optional[int] = None
        self._last_command_idx: Optional[int] = None
        self._last_input1: Optional[str] = None
        self._last_input2: Optional[str] = None

        settings = get_settings()
        preferred_font = _preferred_font()

        # --- Top-level layout: splitter between left (work area) and right (history) ---
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(4, 4, 4, 4)

        # Toolbar
        toolbar = QToolBar(self)
        toolbar.setMovable(False)
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        toolbar.setIconSize(toolbar.iconSize())  # keep default icon size
        style = QApplication.style()

        self.execute_action = QAction(
            style.standardIcon(QStyle.StandardPixmap.SP_MediaPlay), "Execute", self)
        self.execute_action.setToolTip("Execute selected command (Ctrl+Enter)")
        self.execute_action.triggered.connect(self._execute_command)
        toolbar.addAction(self.execute_action)

        self.rerun_action = QAction(
            style.standardIcon(QStyle.StandardPixmap.SP_BrowserReload), "Re-run", self)
        self.rerun_action.setToolTip("Re-run last command (Ctrl+Shift+Enter)")
        self.rerun_action.setEnabled(False)
        self.rerun_action.triggered.connect(self._rerun_last)
        toolbar.addAction(self.rerun_action)

        self.cancel_action = QAction(
            style.standardIcon(QStyle.StandardPixmap.SP_MediaStop), "Cancel", self)
        self.cancel_action.setToolTip("Cancel running command (Ctrl+.)")
        self.cancel_action.setEnabled(False)
        self.cancel_action.triggered.connect(self._cancel_execution)
        toolbar.addAction(self.cancel_action)

        toolbar.addSeparator()

        self.copy_action = QAction(
            style.standardIcon(QStyle.StandardPixmap.SP_FileDialogDetailedView), "Copy Output", self)
        self.copy_action.setToolTip("Copy output to clipboard (Ctrl+Shift+C)")
        self.copy_action.triggered.connect(self._copy_output)
        toolbar.addAction(self.copy_action)

        self.clear_action = QAction(
            style.standardIcon(QStyle.StandardPixmap.SP_DialogResetButton), "Clear", self)
        self.clear_action.setToolTip("Clear output (Ctrl+L)")
        self.clear_action.triggered.connect(self._clear_output)
        toolbar.addAction(self.clear_action)

        toolbar.addSeparator()

        self.edit_yaml_action = QAction(
            style.standardIcon(QStyle.StandardPixmap.SP_FileDialogContentsView), "Edit Commands", self)
        self.edit_yaml_action.setToolTip("Edit commands.yaml in-app")
        self.edit_yaml_action.triggered.connect(self._edit_yaml_inapp)
        toolbar.addAction(self.edit_yaml_action)

        self.edit_external_action = QAction(
            style.standardIcon(QStyle.StandardPixmap.SP_DirOpenIcon), "Open in Editor", self)
        self.edit_external_action.setToolTip("Open commands.yaml in external editor")
        self.edit_external_action.triggered.connect(self._edit_config_external)
        toolbar.addAction(self.edit_external_action)

        toolbar.addSeparator()

        self.two_pane_action = QAction("2nd Input", self)
        self.two_pane_action.setCheckable(True)
        saved_two_pane = settings.value("two_pane_checked", False, type=bool)
        self.two_pane_action.setChecked(saved_two_pane)
        self.two_pane_action.setToolTip("Toggle second input pane")
        self.two_pane_action.toggled.connect(self._toggle_second_input_pane)
        toolbar.addAction(self.two_pane_action)

        toolbar.addSeparator()

        self.settings_action = QAction(
            style.standardIcon(QStyle.StandardPixmap.SP_FileDialogInfoView), "Settings", self)
        self.settings_action.setToolTip("Configure editor, default host, etc.")
        self.settings_action.triggered.connect(self._open_settings)
        toolbar.addAction(self.settings_action)

        main_layout.addWidget(toolbar)

        # Horizontal splitter: left (inputs/output) | right (history)
        hsplitter = QSplitter(Qt.Orientation.Horizontal, self)
        main_layout.addWidget(hsplitter)

        # --- Left panel ---
        left_widget = QWidget(self)
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 0, 0)

        # Command selection row
        cmd_row = QHBoxLayout()
        cmd_row.setSpacing(8)
        cmd_row.addWidget(QLabel("Command:"))
        self.command_selection = ReloadableComboBox(self)
        self.command_selection.setObjectName("commandCombo")
        self.command_selection.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        cmd_row.addWidget(self.command_selection)

        cmd_row.addWidget(QLabel("Host:"))
        self.host_edit = QLineEdit(self)
        self.host_edit.setObjectName("hostEdit")
        self.host_edit.setPlaceholderText(DEFAULT_HOST)
        self.host_edit.setFixedWidth(150)
        saved_host = settings.value("last_host", "")
        if saved_host:
            self.host_edit.setText(saved_host)
        cmd_row.addWidget(self.host_edit)

        left_layout.addLayout(cmd_row)

        # Vertical splitter for input/output
        vsplitter = QSplitter(Qt.Orientation.Vertical, self)

        self.input_text_edit = QPlainTextEdit(self)
        self.input_text_edit.setFont(preferred_font)
        self.input_text_edit.setPlaceholderText("Input 1...")
        vsplitter.addWidget(self.input_text_edit)

        self.second_input_text_edit = QPlainTextEdit(self)
        self.second_input_text_edit.setFont(preferred_font)
        self.second_input_text_edit.setPlaceholderText("Input 2...")
        vsplitter.addWidget(self.second_input_text_edit)

        self.output_text_edit = QTextEdit(self)
        self.output_text_edit.setFont(preferred_font)
        self.output_text_edit.setReadOnly(True)
        self.output_text_edit.setPlaceholderText("Output will appear here...")
        vsplitter.addWidget(self.output_text_edit)

        # Default vertical splitter proportions
        saved_vsplit = settings.value("vsplitter_sizes")
        if saved_vsplit:
            vsplitter.restoreState(saved_vsplit)
        else:
            vsplitter.setSizes([200, 200, 300])
        self.vsplitter = vsplitter

        left_layout.addWidget(vsplitter)

        # Execute button
        self.execute_button = QPushButton("Run", self)
        self.execute_button.setMinimumHeight(40)
        self.execute_button.clicked.connect(self._execute_command)
        left_layout.addWidget(self.execute_button)

        # Status bar
        self.status_label = QLabel("Idle", self)
        self.status_label.setObjectName("statusLabel")
        self._set_status("idle", "Idle")
        left_layout.addWidget(self.status_label)

        left_widget.setMinimumWidth(300)
        hsplitter.addWidget(left_widget)

        # --- Right panel: history ---
        right_widget = QWidget(self)
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(0, 0, 0, 0)

        history_heading = QLabel("History")
        history_heading.setObjectName("historyHeading")
        right_layout.addWidget(history_heading)

        self.history_search = QLineEdit(self)
        self.history_search.setPlaceholderText("Search history...")
        self.history_search.setClearButtonEnabled(True)
        right_layout.addWidget(self.history_search)

        self.history_list = QListView(self)
        right_layout.addWidget(self.history_list)

        right_widget.setMinimumWidth(200)
        hsplitter.addWidget(right_widget)

        # Left panel gets stretch priority
        hsplitter.setStretchFactor(0, 3)
        hsplitter.setStretchFactor(1, 1)

        # Restore splitter sizes
        saved_hsplit = settings.value("hsplitter_sizes")
        if saved_hsplit:
            hsplitter.restoreState(saved_hsplit)
        else:
            hsplitter.setSizes([700, 300])

        self.hsplitter = hsplitter

        # --- Signals ---
        self.history_list.doubleClicked.connect(self._restore_history_item)
        self.history_search.textChanged.connect(self._filter_history)

        # Second pane visibility
        self.second_input_text_edit.setVisible(self.two_pane_action.isChecked())

        # --- Keyboard shortcuts ---
        QShortcut(QKeySequence("Ctrl+Return"), self).activated.connect(self._execute_command)
        QShortcut(QKeySequence("Ctrl+Shift+Return"), self).activated.connect(self._rerun_last)
        QShortcut(QKeySequence("Ctrl+."), self).activated.connect(self._cancel_execution)
        QShortcut(QKeySequence("Ctrl+Shift+C"), self).activated.connect(self._copy_output)
        QShortcut(QKeySequence("Ctrl+L"), self).activated.connect(self._clear_output)

        # Restore last command index
        saved_cmd_idx = settings.value("last_command_idx", 0, type=int)
        if saved_cmd_idx < self.command_selection.count():
            self.command_selection.setCurrentIndex(saved_cmd_idx)

        # --- Database ---
        self.init_db()
        self.load_history()

    def _set_status(self, status: str, text: str) -> None:
        """Update status label with dynamic QSS styling."""
        self.status_label.setText(text)
        self.status_label.setProperty("status", status)
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)

    def _get_host(self) -> str:
        """Get the effective host: per-command override > host edit box > settings default > DEFAULT_HOST."""
        command_idx = self.command_selection.currentIndex()
        commands = load_commands_from_yaml()
        if commands and 0 <= command_idx < len(commands):
            per_cmd_host = commands[command_idx].get('host')
            if per_cmd_host:
                return per_cmd_host
        text = self.host_edit.text().strip()
        if text:
            return text
        settings = get_settings()
        return settings.value("default_host", DEFAULT_HOST)

    def _toggle_second_input_pane(self, checked: bool):
        self.second_input_text_edit.setVisible(checked)
        self.second_input_text_edit.setReadOnly(not checked)

    @pyqtSlot()  # type: ignore
    def _execute_command(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            return

        command_idx = self.command_selection.currentIndex()
        commands = load_commands_from_yaml()
        if not commands or command_idx >= len(commands):
            self._set_status("error", "No commands available")
            return
        selected_command = commands[command_idx]

        input1 = self.input_text_edit.toPlainText()
        input2 = self.second_input_text_edit.toPlainText()
        host = self._get_host()
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        second_input_enabled = self.two_pane_action.isChecked()

        self._current_history_id = self.save_history(
            command_idx,
            selected_command,
            input1,
            input2,
            second_input_enabled,
            timestamp,
        )

        # Remember for re-run
        self._last_command_idx = command_idx
        self._last_input1 = input1
        self._last_input2 = input2
        self.rerun_action.setEnabled(True)

        self._start_worker(selected_command, input1, input2, host)

    @pyqtSlot()
    def _rerun_last(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            return
        if self._last_command_idx is None:
            return

        commands = load_commands_from_yaml()
        if not commands or self._last_command_idx >= len(commands):
            return
        selected_command = commands[self._last_command_idx]
        host = self._get_host()

        # Re-populate inputs
        self.input_text_edit.setPlainText(self._last_input1 or "")
        self.second_input_text_edit.setPlainText(self._last_input2 or "")

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._current_history_id = self.save_history(
            self._last_command_idx,
            selected_command,
            self._last_input1 or "",
            self._last_input2 or "",
            self.two_pane_action.isChecked(),
            timestamp,
        )

        self._start_worker(selected_command, self._last_input1 or "", self._last_input2 or "", host)

    def _start_worker(self, command_data: dict, input1: str, input2: str, host: str) -> None:
        self.output_text_edit.clear()
        self.output_text_edit.setPlaceholderText("Waiting for output...")
        self.execute_action.setEnabled(False)
        self.execute_button.setEnabled(False)
        self.rerun_action.setEnabled(False)
        self.cancel_action.setEnabled(True)
        self.command_selection.setEnabled(False)
        self.host_edit.setEnabled(False)
        self._set_status("running", "Running...")

        self.worker = SSHWorker(command_data, input1, input2, host, self)
        self.worker.result_signal.connect(self._display_result)
        self.worker.append_signal.connect(self._append_result)
        self.worker.status_signal.connect(self._update_status)
        self.worker.finished.connect(self._on_worker_finished)
        self.worker.start()

    @pyqtSlot()
    def _cancel_execution(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            self.worker.cancel()
            self._set_status("error", "Cancelling...")

    @pyqtSlot()
    def _on_worker_finished(self):
        self.execute_action.setEnabled(True)
        self.execute_button.setEnabled(True)
        self.rerun_action.setEnabled(self._last_command_idx is not None)
        self.cancel_action.setEnabled(False)
        self.command_selection.setEnabled(True)
        self.host_edit.setEnabled(True)
        self.output_text_edit.setPlaceholderText("Output will appear here...")
        # Set final status based on current text
        current = self.status_label.text()
        if "Cancel" in current or "failed" in current.lower():
            self._set_status("error", current)
        else:
            timestamp = datetime.now().strftime("%H:%M:%S")
            self._set_status("complete", f"Complete at {timestamp}")
        self._current_history_id = None

    @pyqtSlot(str)  # type: ignore
    def _display_result(self, result: str) -> None:
        self.output_text_edit.setPlainText(result)
        self._update_history_output(result)

    @pyqtSlot(str)  # type: ignore
    def _append_result(self, result: str) -> None:
        self.output_text_edit.append(result)
        self._update_history_output(self.output_text_edit.toPlainText())

    @pyqtSlot(str)
    def _update_status(self, status: str):
        if "fail" in status.lower() or "error" in status.lower():
            self._set_status("error", status)
        else:
            self._set_status("running", status)

    @pyqtSlot()
    def _copy_output(self):
        text = self.output_text_edit.toPlainText()
        if text:
            QApplication.clipboard().setText(text)
            self._set_status("complete", "Output copied to clipboard")

    @pyqtSlot()
    def _clear_output(self):
        self.output_text_edit.clear()

    @pyqtSlot()
    def _edit_yaml_inapp(self):
        dlg = YamlEditorDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            # Reload combo box
            self.command_selection.clear()
            self.command_selection._load_commands()

    @pyqtSlot()
    def _open_settings(self):
        dlg = SettingsDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            # Update host placeholder with new default
            settings = get_settings()
            self.host_edit.setPlaceholderText(settings.value("default_host", DEFAULT_HOST))

    @pyqtSlot()
    def _edit_config_external(self):
        file_path = os.path.join(data_dir(), 'commands.yaml')
        settings = get_settings()
        editor = settings.value("external_editor", "code")
        try:
            subprocess.Popen([editor, file_path])
        except FileNotFoundError:
            QDesktopServices.openUrl(QUrl.fromLocalFile(file_path))

    def _filter_history(self, text: str):
        if hasattr(self, '_proxy_model'):
            self._proxy_model.setFilterRegularExpression(
                QRegularExpression(text)
            )

    def init_db(self):
        db_path = os.path.join(data_dir(), 'history.db')
        self.conn = sqlite3.connect(db_path)
        self.cursor = self.conn.cursor()
        self.cursor.execute('''CREATE TABLE IF NOT EXISTS history
                            (id INTEGER PRIMARY KEY AUTOINCREMENT,
                            input1 TEXT,
                            input2 TEXT,
                            timestamp TEXT,
                            command_idx INTEGER,
                            command_label TEXT,
                            command_text TEXT,
                            command_type TEXT,
                            command_host TEXT,
                            second_input_enabled INTEGER DEFAULT 0,
                            output TEXT DEFAULT '')''')
        self._ensure_history_columns()
        self.conn.commit()

    def _ensure_history_columns(self) -> None:
        self.cursor.execute("PRAGMA table_info(history)")
        existing_columns = {row[1] for row in self.cursor.fetchall()}
        required_columns = {
            "command_idx": "INTEGER",
            "command_label": "TEXT",
            "command_text": "TEXT",
            "command_type": "TEXT",
            "command_host": "TEXT",
            "second_input_enabled": "INTEGER DEFAULT 0",
            "output": "TEXT DEFAULT ''",
        }
        for column_name, column_type in required_columns.items():
            if column_name not in existing_columns:
                self.cursor.execute(f"ALTER TABLE history ADD COLUMN {column_name} {column_type}")

    def save_history(self, command_idx, command_data, input1, input2, second_input_enabled, timestamp):
        self.cursor.execute(
            """
            INSERT INTO history (
                input1,
                input2,
                timestamp,
                command_idx,
                command_label,
                command_text,
                command_type,
                command_host,
                second_input_enabled,
                output
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                input1,
                input2,
                timestamp,
                command_idx,
                command_data.get("label", ""),
                command_data.get("command", ""),
                command_data.get("type", ""),
                command_data.get("host", ""),
                int(second_input_enabled),
                "",
            ),
        )
        history_id = self.cursor.lastrowid
        self.conn.commit()
        self.load_history()
        return history_id

    def _update_history_output(self, output: str) -> None:
        if self._current_history_id is None:
            return
        self.cursor.execute(
            "UPDATE history SET output = ? WHERE id = ?",
            (output, self._current_history_id),
        )
        self.conn.commit()

    def _find_command_index(self, item: HistoryItem) -> Optional[int]:
        commands = load_commands_from_yaml()
        for idx, command in enumerate(commands):
            if (
                command.get("label", "") == item.command_label
                and command.get("command", "") == item.command_text
                and command.get("type", "") == item.command_type
                and command.get("host", DEFAULT_HOST) == (item.command_host or DEFAULT_HOST)
            ):
                return idx
        if item.command_idx is not None and 0 <= item.command_idx < len(commands):
            return item.command_idx
        return None

    def load_history(self):
        self.history_model = HistoryListModel(cursor=self.cursor)
        self._proxy_model = HistoryFilterProxy(self)
        self._proxy_model.setSourceModel(self.history_model)
        self.history_list.setModel(self._proxy_model)
        self.history_model.fetchMore(QModelIndex())
        # Re-apply current search filter
        current_search = self.history_search.text() if hasattr(self, 'history_search') else ""
        if current_search:
            self._proxy_model.setFilterRegularExpression(
                QRegularExpression(current_search)
            )

    @pyqtSlot(QModelIndex)
    def _restore_history_item(self, proxy_index):
        source_index = self._proxy_model.mapToSource(proxy_index)
        item = self.history_model.items[source_index.row()]
        command_idx = self._find_command_index(item)
        if command_idx is not None:
            self.command_selection.setCurrentIndex(command_idx)
        self.input_text_edit.setPlainText(item.input1)
        self.two_pane_action.setChecked(item.second_input_enabled or bool(item.input2))
        self.second_input_text_edit.setPlainText(item.input2)
        self.output_text_edit.setPlainText(item.output)

    def closeEvent(self, event):
        # Save settings
        settings = get_settings()
        settings.setValue("last_command_idx", self.command_selection.currentIndex())
        settings.setValue("last_host", self.host_edit.text())
        settings.setValue("two_pane_checked", self.two_pane_action.isChecked())
        settings.setValue("hsplitter_sizes", self.hsplitter.saveState())
        settings.setValue("vsplitter_sizes", self.vsplitter.saveState())

        if hasattr(self, 'conn') and self.conn:
            self.conn.close()
        super().closeEvent(event)
