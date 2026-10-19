# File: app_paths.py
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Where the program keeps the files it creates.

The application ships as a single frozen executable, so the folder containing
it (and PyInstaller's temporary unpack folder) is not a place to keep anything:
it can be read-only, replaced on every update, or removed behind the app's back.

The locations are fixed and always belong to the user running the program. On
Windows that is the per-user application data folder:

    C:\\Users\\<userprofile>\\AppData\\Roaming\\.polsoft\\ScriptDB\\        files
    C:\\Users\\<userprofile>\\AppData\\Roaming\\.polsoft\\ScriptDB\\db\\     databases

Everything the program writes goes under that one root: the databases in the
``db`` subfolder, and the artifacts it produces (reports, exports, logs,
snapshots, generated projects) in the root itself.

The user folder is resolved once, from ``%APPDATA%`` and then
``%USERPROFILE%``, and falls back to the profile name when neither environment
variable is set. It is never hardcoded to a particular account, so the program
lands in the right place for whoever runs it -- including an exe handed to
another user. A domain account (``DOMAIN\\name``) works as well as a local one.
"""

import os
import shutil
import sys

#: Folder the vendor keeps its application folders in, below the roaming
#: application data folder. A leading dot keeps it out of the way in Explorer.
VENDOR_DIR = ".polsoft"

#: The program's own folder below that.
APP_DIR = "ScriptDB"

#: Subfolder holding the databases, below the application data folder.
DB_SUBDIR = "db"

# The databases and settings files the program reads and writes. They all live
# in the db subfolder, and the ones shipped with the source are copied there on
# first run -- only when they are missing, so an existing installation is never
# overwritten.
SEED_FILES = (
    "scripts_db.json",
    "settings_db.json",
    "connections_db.json",
    "todo_db.json",
    "shopping_db.json",
    "apps_db.json",
    "debugger_db.json",
    "batch_templates.json",
)

# Directory holding this file: where the read-only resources live and, when the
# program is not frozen, the source tree itself.
SOURCE_DIR = os.path.dirname(os.path.abspath(__file__))


def resource_dir():
    """Folder holding the bundled read-only resources.

    A one-file PyInstaller build unpacks them into a temporary folder, which is
    why the frozen case does not simply use SOURCE_DIR.
    """
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return sys._MEIPASS
    return SOURCE_DIR


def user_profile_dir():
    """Return the current user's profile folder (``C:\\Users\\<name>``).

    ``%USERPROFILE%`` is the reliable source, and it is simply what Windows sets
    for whoever is running the program. When it is missing -- a service account,
    a locked-down environment -- the folder is recovered from ``%HOMEDRIVE%`` +
    ``%HOMEPATH%``, then from ``~``, and only then from the account name. The
    path therefore always belongs to the user running the program, never to a
    name baked into the source, so an exe handed to someone else lands under
    their profile. A domain account (``DOMAIN\\name``) works as well as a local
    one.

    Only an *empty* value is rejected, never a valid path that happens not to
    exist yet: a redirected profile on a drive that is not mounted should still
    be reported the way Windows reports it.
    """
    profile = os.environ.get("USERPROFILE")
    if profile:
        return profile
    drive = os.environ.get("HOMEDRIVE")
    path = os.environ.get("HOMEPATH")
    if drive and path:
        return drive + path
    return os.path.expanduser("~")


def roaming_app_data():
    """Return the roaming application data folder for the current user.

    ``%APPDATA%`` already points at ``<profile>\\AppData\\Roaming``, so it is
    used as-is; the profile folder is the fallback for an environment that
    does not define it.
    """
    appdata = os.environ.get("APPDATA")
    if appdata:
        return appdata
    return os.path.join(user_profile_dir(), "AppData", "Roaming")


def data_dir():
    """Return the program files folder: reports, exports, logs, snapshots."""
    if sys.platform == "win32":
        return os.path.join(roaming_app_data(), VENDOR_DIR, APP_DIR)
    local = os.environ.get("XDG_DATA_HOME") or os.path.expanduser(
        "~/.local/share")
    return os.path.join(local, VENDOR_DIR, APP_DIR)


def db_dir():
    """Return the databases folder: ``<data_dir>\\db``."""
    return os.path.join(data_dir(), DB_SUBDIR)


def _ensure(directory):
    """Create a folder (and its parents) if it is missing; never raise."""
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError:
        pass
    return directory


def ensure_data_dir():
    """Create the program files folder and the databases folder."""
    return _ensure(_ensure(data_dir()))


def ensure_db_dir():
    """Create the databases folder and return it."""
    return _ensure(db_dir())


def data_file(name):
    """Return a full path for a program file (report, export, log)."""
    return os.path.join(ensure_data_dir(), name)


def db_file(name):
    """Return the full path of a database inside the ``db`` subfolder."""
    return os.path.join(ensure_db_dir(), name)


def seed_data_dir():
    """Copy the bundled databases into ``db`` when they are missing.

    The source is the *resource* folder, not this module's own folder: a frozen
    one-file build unpacks the bundled databases into ``_MEIPASS`` and deletes
    it on exit, so that is the only copy of the defaults a frozen program has.
    """
    target_dir = ensure_db_dir()
    source_dir = resource_dir()
    for name in SEED_FILES:
        target = os.path.join(target_dir, name)
        if os.path.exists(target):
            continue
        source = os.path.join(source_dir, name)
        if os.path.isfile(source):
            try:
                shutil.copyfile(source, target)
            except OSError:
                pass
    return target_dir
