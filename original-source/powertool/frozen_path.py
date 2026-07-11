"""
Path resolution helper for PyInstaller-frozen executables.

When bundled with PyInstaller (--onefile or --onedir), __file__ points inside
the temp extraction directory (_MEIPASS), which is read-only. Data files that
need to be writable at runtime (history.db, commands.yaml) should live next to
the .exe instead. Read-only bundled resources can stay in _MEIPASS.

Usage:
    from powertool.frozen_path import data_dir, bundled_dir

    # For writable files (DB, user config):
    db_path = os.path.join(data_dir(), 'history.db')

    # For read-only bundled resources:
    icon_path = os.path.join(bundled_dir(), 'icon.png')
"""

import os
import sys


def is_frozen() -> bool:
    return getattr(sys, 'frozen', False)


def bundled_dir() -> str:
    """Directory where PyInstaller extracted bundled files (_MEIPASS).
    Falls back to the powertool package directory when running from source."""
    if is_frozen():
        return os.path.join(sys._MEIPASS, 'powertool')  # type: ignore[attr-defined]
    return os.path.dirname(os.path.abspath(__file__))


def data_dir() -> str:
    """Directory for writable data files (DB, user YAML configs).
    When frozen, this is the directory containing the .exe.
    When running from source, same as bundled_dir()."""
    if is_frozen():
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))
