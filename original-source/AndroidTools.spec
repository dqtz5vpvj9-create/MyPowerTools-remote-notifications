# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec file for AndroidTools.

Build:
    pip install pyinstaller
    pyinstaller AndroidTools.spec

Output goes to dist/AndroidTools.exe (--onefile) or dist/AndroidTools/ (--onedir).
Switch between modes by changing the EXE/COLLECT section at the bottom.
"""

import sys
import os
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files

block_cipher = None


def _build_toast_activator():
    if sys.platform != 'win32':
        return
    source = Path('native') / 'ToastActivator.cs'
    output = Path('dist') / 'ToastActivator.exe'
    if not source.exists():
        return
    candidates = [
        Path(os.environ.get('WINDIR', r'C:\Windows')) / 'Microsoft.NET' / 'Framework64' / 'v4.0.30319' / 'csc.exe',
        Path(os.environ.get('WINDIR', r'C:\Windows')) / 'Microsoft.NET' / 'Framework' / 'v4.0.30319' / 'csc.exe',
        Path(r'C:\Program Files\Microsoft Visual Studio\2022\Community\MSBuild\Current\Bin\Roslyn\csc.exe'),
        Path(r'C:\Program Files\Microsoft Visual Studio\2022\BuildTools\MSBuild\Current\Bin\Roslyn\csc.exe'),
    ]
    csc = next((path for path in candidates if path.exists()), None)
    if csc is None:
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            str(csc),
            '/nologo',
            '/optimize+',
            '/target:winexe',
            f'/out:{output}',
            str(source),
        ],
        check=True,
    )


_build_toast_activator()


def _write_build_info():
    output = Path('build') / 'generated' / 'build_info.json'
    output.parent.mkdir(parents=True, exist_ok=True)
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone()
    output.write_text(
        json.dumps(
            {
                'built_at_utc': now_utc.strftime('%Y-%m-%d %H:%M:%S UTC'),
                'built_at_local': now_local.strftime('%Y-%m-%d %H:%M:%S %z'),
            },
            indent=2,
        )
        + '\n',
        encoding='utf-8',
    )
    return output


BUILD_INFO_PATH = _write_build_info()

# ── Analysis ────────────────────────────────────────────────────────────────

a = Analysis(
    ['qt.py'],
    pathex=['.'],
    binaries=[],

    # Bundled data files: (source, dest_dir_inside_bundle)
    # history.db is created at runtime next to the .exe, not bundled.
    # commands.yaml is optional — the app creates a template if missing.
    datas=[
        x for x in [
            ('powertool/commands.yaml', 'powertool') if os.path.exists('powertool/commands.yaml') else None,
            ('powertool/style.qss', 'powertool') if os.path.exists('powertool/style.qss') else None,
            ('powertool/icons', 'powertool/icons') if os.path.isdir('powertool/icons') else None,
            ('dist/ToastActivator.exe', '.') if os.path.exists('dist/ToastActivator.exe') else None,
            (str(BUILD_INFO_PATH), 'powertool'),
        ] if x is not None
    ] + collect_data_files('rfc3987_syntax'),

    # Packages that PyInstaller's import analysis sometimes misses.
    hiddenimports=[
        # PyQt6
        'PyQt6.QtWidgets',
        'PyQt6.QtGui',
        'PyQt6.QtCore',
        'PyQt6.sip',

        # WiFi
        'pywifi',
        'pywifi.const',

        # System
        'psutil',
        'psutil._pswindows',

        # Network
        'requests',
        'jsonschema',

        # Data
        'yaml',
        'sqlite3',
        'markdown_it',
        'mdit_py_plugins',
        'mdit_py_plugins.tasklists',
        'linkify_it',
        'uc_micro',
        'uc_micro.categories',
        'uc_micro.properties',
        'pygments',

        # Win32 (Page2 — conditional import, safe to list unconditionally)
        'win32con',
        'win32gui',
        'win32process',
        'win32file',
        'win32pipe',
        'win32com.client',
        'win32com.propsys.propsys',
        'win32com.propsys.pscon',
        'win32com.shell.shell',
        'pythoncom',
        'pywintypes',

        # Config
        'configparser',
    ],

    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Keep PyInstaller on the single Qt binding used by the app.
        # Some optional dependencies probe installed Qt bindings and can
        # otherwise trigger PyInstaller's "multiple Qt bindings" abort.
        'PyQt5',
        'PyQt5.QtCore',
        'PyQt5.QtGui',
        'PyQt5.QtWidgets',
        'PySide2',
        'PySide6',
        'qtpy',

        # Trim things we definitely don't need
        'tkinter',
        'matplotlib',
        'numpy',
        'pandas',
        'scipy',
        'PIL',
        'cv2',
        'IPython',
        'notebook',
        'pytest',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# ── PYZ (bytecode archive) ─────────────────────────────────────────────────

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# ── EXE ─────────────────────────────────────────────────────────────────────

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='AndroidTools',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,           # --windowed: no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,

    # Uncomment and set to use a custom icon:
    # icon='path/to/icon.ico',
)

# ── ONE-DIR alternative ─────────────────────────────────────────────────────
# To build as a directory (faster startup, easier to debug), comment out the
# EXE block above and uncomment the following:
#
# exe = EXE(
#     pyz,
#     a.scripts,
#     [],
#     exclude_binaries=True,
#     name='AndroidTools',
#     debug=False,
#     strip=False,
#     upx=True,
#     console=False,
# )
#
# coll = COLLECT(
#     exe,
#     a.binaries,
#     a.zipfiles,
#     a.datas,
#     strip=False,
#     upx=True,
#     upx_exclude=[],
#     name='AndroidTools',
# )
