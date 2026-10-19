# -*- mode: python ; coding: utf-8 -*-
r"""PyInstaller spec for ScriptDB - a single portable .exe.

Build it with the batch file next to this one:

    builder\\build.bat

The result is builder\\dist\\ScriptDB.exe: one self-contained file, no installer,
no Python needed on the target machine. Nothing is written to the registry.

What this spec does
-------------------
* one-file mode (``EXE`` with ``a.binaries``/``a.datas`` inside it), so the exe
  unpacks itself into a temporary folder at start-up and cleans up on exit;
* ``console=False`` -- a GUI program, no console window;
* the icon and the version resource are embedded in the exe;
* ``sdb.ico`` is also shipped as an *included file*, because the running program
  loads it at start-up to set the window icon (see app_icon_path() in
  sdb_scripts.py, which looks for it in ``_MEIPASS`` and in ``builder/``);
* ``app_paths.py`` is bundled as a hidden import. It is the module that owns the
  application data folder, so it must travel inside the exe -- the program keeps
  the databases in ``%APPDATA%\.polsoft\ScriptDB\db`` and everything else it
  writes one folder up, and nowhere else. The profile is the one of whoever runs
  the exe, so the file can be handed to another user;
* the databases shipped with the source (``scripts_db.json``, ``settings_db.json``,
  ``todo_db.json`` and friends) travel inside the exe as *read-only defaults*. On
  first run ``app_paths.seed_data_dir()`` copies them into the application data
  folder, and never overwrites a file that is already there.

Paths are resolved from this file's own location, so the build works no matter
which directory you launch it from.
"""

import os
import sys
# PyInstaller eval()s the version file, so the names it uses have to exist in
# the spec's namespace (see the note at the top of version_info.py).
from PyInstaller.utils.win32.versioninfo import (  # noqa: F401
    FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo,
    VarStruct, VSVersionInfo)

# --------------------------------------------------------------------------
# Paths - everything is relative to builder/, which sits next to sdb_scripts.py
# --------------------------------------------------------------------------
BUILDER_DIR = os.path.abspath(os.path.dirname(SPEC))
PROJECT_DIR = os.path.abspath(os.path.join(BUILDER_DIR, os.pardir))

ENTRY_SCRIPT = os.path.join(PROJECT_DIR, "sdb_scripts.py")
ICON_FILE = os.path.join(BUILDER_DIR, "sdb.ico")
VERSION_FILE = os.path.join(BUILDER_DIR, "version_info.txt")

# Default databases bundled *inside* the exe. On first run the program copies
# them into %APPDATA%\.polsoft\ScriptDB (see app_paths.seed_data_dir), so the
# bundle itself is never written to. This list is app_paths.SEED_FILES, read
# from the source instead of repeated here so the two cannot drift apart.
sys.path.insert(0, PROJECT_DIR)
from app_paths import SEED_FILES  # noqa: E402

BUNDLED_DATA = [
    (os.path.join(PROJECT_DIR, name), ".")
    for name in SEED_FILES
    if os.path.isfile(os.path.join(PROJECT_DIR, name))
]

# The whole tabs/ package has to travel with the program: PyInstaller follows
# the imports it can see, but the syntax/ modules are loaded dynamically by the
# registry, so they are collected explicitly.
TABS_DIR = os.path.join(PROJECT_DIR, "tabs")
SYNTAX_DIR = os.path.join(TABS_DIR, "syntax")
SOURCE_DATA = [
    (TABS_DIR, "tabs"),
    (SYNTAX_DIR, "tabs/syntax"),
    (ICON_FILE, "builder"),
]

# PyQt5 ships more than this app needs. Dropping the heavy, unused plugins keeps
# the exe small; the ones listed below are the platforms and styles that are
# actually imported.
EXCLUDES = [
    "PyQt5.QtWebEngineWidgets", "PyQt5.QtWebEngineCore", "PyQt5.QtWebEngine",
    "PyQt5.QtBluetooth", "PyQt5.QtDesigner", "PyQt5.QtHelp", "PyQt5.QtLocation",
    "PyQt5.QtMultimedia", "PyQt5.QtMultimediaWidgets", "PyQt5.QtNfc",
    "PyQt5.QtOpenGL", "PyQt5.QtPositioning", "PyQt5.QtQml", "PyQt5.QtQuick",
    "PyQt5.QtQuickWidgets", "PyQt5.QtRemoteObjects", "PyQt5.QtSensors",
    "PyQt5.QtSerialPort", "PyQt5.QtSql", "PyQt5.QtTest", "PyQt5.QtWebChannel",
    "PyQt5.QtWebSockets", "PyQt5.QtXml", "PyQt5.QtXmlPatterns",
    "tkinter", "unittest", "pydoc", "doctest", "pdb",
    "matplotlib", "numpy", "PIL", "scipy", "pandas",
]

# A few packages print a banner (or slow the start-up) the moment they are
# imported. They are not used by ScriptDB, so they are kept out of the build
# even when they happen to be installed in the interpreter running it.
EXCLUDES += ["pygame", "setuptools", "pkg_resources", "pip"]

# --------------------------------------------------------------------------
# The build itself
# --------------------------------------------------------------------------
a = Analysis(
    [ENTRY_SCRIPT],
    pathex=[PROJECT_DIR],
    binaries=[],
    datas=BUNDLED_DATA + SOURCE_DATA,
    hiddenimports=[
        # The module that owns the application data folder. Imported by name so a
        # missing bundle fails at build time rather than at the user's first run.
        "app_paths",
        # The tab registry imports these by name at run time.
        "tabs",
        "tabs.editor",
        "tabs.analyzer",
        "tabs.audit",
        "tabs.search_tab",
        "tabs.search_engine",
        "tabs.nav_engine",
        "tabs.syntax",
        "tabs.syntax.registry",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)

# Byte-code compile step, then the archive and the final exe.
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="ScriptDB",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    # A GUI program: no console window flashes up when the exe starts.
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # Icon shown in Explorer, the taskbar and the title bar.
    icon=ICON_FILE,
    # Product name / version / copyright: Properties -> Details.
    version=VERSION_FILE,
)
