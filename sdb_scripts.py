# File: sdb_scripts.py
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ScriptDB - Scripts tab (standalone application)

The "Scripts" tab extracted from sdb.py as a self-contained program. The other
tabs (Search, Editor, Modules, Database, Metadata, Compare) are not included
here yet and will be added later.
"""

import collections
import datetime as dt
import json
import os
import re
import sys

# These standard-library modules cost real time to import and none of them is
# needed to build the window, so they are resolved on first use through
# ``__getattr__`` further down.
#
# ``shutil`` is the expensive one: it pulls in glob, fnmatch, bz2, lzma and
# zstd, which together measured ~24 ms -- the single biggest import cost after
# PyQt5 itself. It is only needed to find a bash/pwsh/node executable and to
# unlink a temporary file, so both now go through the lazy path.
# ``tempfile`` has the same problem for a smaller win: it pulls in random and
# (on Windows) the _winapi machinery. It is used by the runner and by the atomic
# JSON writer, so the writer falls back to writing beside the target when the
# module has not been loaded yet.
_LAZY_STDLIB = (
    "argparse",
    "contextlib",
    "difflib",
    "shutil",
    "sqlite3",
    "subprocess",
    "tempfile",
    "threading",
    "traceback",
)

try:
    from PyQt5 import QtCore, QtGui, QtWidgets
    from PyQt5.QtGui import QFont
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        """PyQt5 is required. Install it with: pip install PyQt5\n"""
        "Original import error: " + str(exc)
    )


BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Make the per-tab package importable both when run from source and when the
# whole project is frozen, since the tabs folder ships next to this file.
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# Read-only resources (the tabs package, the icon) stay next to the executable;
# everything the program writes goes to a fixed per-user application data folder.
# See app_paths.py -- the tabs modules use it too, so every feature agrees on
# one location and the exe can be installed anywhere.
try:
    import app_paths
except ImportError:  # pragma: no cover - frozen without the module bundled
    app_paths = None

if app_paths is not None:
    # DB_DIR holds the databases; DATA_DIR holds what the program produces
    # (reports, exports, logs, snapshots) and is the folder it runs scripts in.
    DB_DIR = app_paths.seed_data_dir()
    DATA_DIR = app_paths.ensure_data_dir()
    APP_DATA_DIR = DATA_DIR
else:  # fall back to the old next-to-the-exe behaviour
    DB_DIR = DATA_DIR = APP_DATA_DIR = BASE_DIR

try:
    from tabs import theme as app_theme
    # Only the theme is needed at import time. The tab classes are resolved
    # through tabs.__getattr__ on first access, so importing them here would
    # load all fifteen tab modules (and their engines) on every start.
except ImportError:  # pragma: no cover - tab package missing
    app_theme = None
_APP_ICON = None  # cached QIcon for load_app_icon()
_ABOUT_ICON = None  # cached QIcon for load_about_icon()
DATA_FILE = os.path.join(DB_DIR, "scripts_db.json")
CONN_FILE = os.path.join(DB_DIR, "connections_db.json")
SETTINGS_FILE = os.path.join(DB_DIR, "settings_db.json")

# Marker stored in a script entry that represents a visual separator row.
SEPARATOR_KIND = "separator"


def __getattr__(name):
    """Import the deferred standard-library modules on first use (PEP 562).

    ``traceback``, ``shutil``, ``difflib``, ``argparse`` and ``sqlite3`` are
    only needed once the matching feature runs, and together they account for
    roughly 90 ms of start-up. Resolving them here keeps that cost off the
    path that runs before the window appears.
    """
    if name in _LAZY_STDLIB:
        import importlib
        module = importlib.import_module(name)
        globals()[name] = module  # cache: later lookups skip __getattr__
        return module
    if name == "TRANSLATIONS":
        from tabs.i18n import TRANSLATIONS as _table
        globals()[name] = _table  # cache: later lookups skip __getattr__
        return _table
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")



def translation_table():
    """Return the translation tables, importing them on first use.

    Code *inside* this module must not refer to ``TRANSLATIONS`` directly: a
    bare global name is resolved by the interpreter's global lookup and never
    reaches the module ``__getattr__`` above, so it would raise NameError even
    though ``sdb_scripts.TRANSLATIONS`` resolves fine from outside.
    """
    from tabs.i18n import TRANSLATIONS
    return TRANSLATIONS


def _resource_base_dir():
    # Directory holding bundled resources. When frozen into a one-file exe,
    # PyInstaller unpacks everything into a temporary _MEIPASS folder.
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return sys._MEIPASS
    return BASE_DIR


def app_icon_path():
    # Locate sdb.ico both when run from source and from the frozen executable.
    # It normally lives in the 'builder' folder next to sdb.py.
    base = _resource_base_dir()
    candidates = [
        os.path.join(base, "sdb.ico"),
        os.path.join(base, "builder", "sdb.ico"),
        os.path.join(BASE_DIR, "sdb.ico"),
        os.path.join(BASE_DIR, "builder", "sdb.ico"),
    ]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return ""


def load_app_icon():
    # Build a QIcon from sdb.ico, or return an empty QIcon when not found.
    # sdb.ico is a 200+ KB multi-resolution file and decoding it costs ~30 ms,
    # so the icon is built once and shared by every caller.
    global _APP_ICON
    if _APP_ICON is None:
        path = app_icon_path()
        _APP_ICON = QtGui.QIcon(path) if path else QtGui.QIcon()
    return _APP_ICON


def app_icon_pixmap(size=64):
    # Render the application icon into a pixmap, or None when there is no icon.
    icon = load_app_icon()
    if icon.isNull():
        return None
    return icon.pixmap(QtCore.QSize(size, size))



def load_about_icon(size=32):
    # Icon for the About button: the application icon when available, otherwise
    # an info glyph drawn at run time so the button is never blank.
    # Cached: this sits on the start-up path and rendering the ico twice was
    # the single most expensive step of building the window.
    global _ABOUT_ICON
    if _ABOUT_ICON is None:
        pixmap = app_icon_pixmap(size)
        if pixmap is not None and not pixmap.isNull():
            _ABOUT_ICON = QtGui.QIcon(pixmap)
        else:
            _ABOUT_ICON = QtGui.QIcon(draw_info_glyph(size))
    return _ABOUT_ICON


def draw_info_glyph(size=32):
    # Paint a simple "i" badge so About has an icon even without sdb.ico.
    pixmap = QtGui.QPixmap(size, size)
    pixmap.fill(QtCore.Qt.transparent)
    painter = QtGui.QPainter(pixmap)
    try:
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
        painter.setPen(QtCore.Qt.NoPen)
        painter.setBrush(QtGui.QColor("#8ab4f8"))
        painter.drawEllipse(1, 1, size - 2, size - 2)
        painter.setPen(QtGui.QColor("#1e1e1e"))
        font = QFont("Segoe UI", int(size * 0.65))
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(pixmap.rect(), QtCore.Qt.AlignCenter, "i")
    finally:
        painter.end()
    return pixmap



# The English and Polish translation tables live in tabs/i18n.py: the table
# is thousands of lines of data, and this module has to stay cheap to
# import. It is resolved on first use through __getattr__ below.


CATEGORIES = [
    "Python",
    "Bash",
    "PowerShell",
    "VBScript",
    "JavaScript",
    "Java",
    "C++",
    "Batch",
    "HTML",
    "HTA",
    "Registry (REG)",
    "JSON",
    "YAML",
    "INI",
    "System config",
    "SQL",
    "Ruby",
    "Other",
]

# Maps a category to the file extension used when a script is saved/loaded as a
# plain source file (import/export in its natural form).
CATEGORY_EXTENSIONS = {
    "Python": ".py",
    "Bash": ".sh",
    "PowerShell": ".ps1",
    "VBScript": ".vbs",
    "JavaScript": ".js",
    "Java": ".java",
    "C++": ".cpp",
    "Batch": ".bat",
    "HTML": ".html",
    "HTA": ".hta",
    "Registry (REG)": ".reg",
    "JSON": ".json",
    "YAML": ".yaml",
    "INI": ".ini",
    "System config": ".cfg",
    "SQL": ".sql",
    "Ruby": ".rb",
    "Other": ".txt",
}
# Reverse lookup: file extension (lower-case, with dot) -> category.
EXTENSION_CATEGORIES = {ext: cat for cat, ext in CATEGORY_EXTENSIONS.items()}

# Extra extension aliases that map onto an existing category.
EXTENSION_ALIASES = {
    ".pyw": "Python", ".pyi": "Python", ".pyx": "Python",
    ".bash": "Bash", ".zsh": "Bash", ".ksh": "Bash", ".shell": "Bash",
    ".psm1": "PowerShell", ".psd1": "PowerShell",
    ".jsx": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript",
    ".ts": "JavaScript", ".tsx": "JavaScript",
    ".vbe": "VBScript", ".wsf": "VBScript",
    ".cmd": "Batch", ".nt": "Batch",
    ".htm": "HTML", ".xhtml": "HTML",
    ".reg": "Registry (REG)",
    ".c": "C++", ".cc": "C++", ".cxx": "C++", ".hpp": "C++", ".h": "C++",
    ".sqlite": "SQL", ".sqlite3": "SQL",
    ".jsonc": "JSON", ".json5": "JSON",
    ".yml": "YAML",
    ".toml": "INI",
    ".conf": "System config", ".inf": "System config",
    ".env": "System config",
    ".rake": "Ruby",
}
EXTENSION_ALIASES.update(EXTENSION_CATEGORIES)


TEMPLATES = {
    "Python": (
        "Python - Hello World",
        "#!/usr/bin/env python3\n# -*- coding: utf-8 -*-\n\ndef main():\n    print('Hello, World!')\n\nif __name__ == '__main__':\n    main()\n",
    ),
    "Bash": (
        "Bash - Hello World",
        "#!/usr/bin/env bash\nset -euo pipefail\n\necho \"Hello, World!\"\n",
    ),
    "PowerShell": (
        "PowerShell - Hello World",
        "Write-Host 'Hello, World!'\n",
    ),
    "JavaScript": (
        "JavaScript - Hello World",
        "console.log('Hello, World!');\n",
    ),
    "Java": (
        "Java - Hello World",
        "public class Main {\n    public static void main(String[] args) {\n        System.out.println(\"Hello, World!\");\n    }\n}\n",
    ),
    "C++": (
        "C++ - Hello World",
        "#include <iostream>\n\nint main() {\n    std::cout << \"Hello, World!\" << std::endl;\n    return 0;\n}\n",
    ),
    "Batch": (
        "Batch - Hello World",
        "@echo off\nsetlocal\n\necho Hello, World!\n",
    ),
    "HTML": (
        "HTML - Starter Page",
        "<!DOCTYPE html>\n<html>\n<head>\n <meta charset=\"UTF-8\">\n <title>Hello</title>\n</head>\n<body>\n <h1>Hello, World!</h1>\n</body>\n</html>\n",
    ),
    "JSON": (
        "JSON - Example",
        '{\n  "app": "ScriptDB",\n  "version": "1.0.0",\n  "debug": false\n}\n',
    ),
    "SQL": (
        "SQL - Example",
        "CREATE TABLE IF NOT EXISTS users (\n  id INTEGER PRIMARY KEY AUTOINCREMENT,\n  name TEXT NOT NULL\n);\nINSERT INTO users (name) VALUES ('Alice');\nSELECT * FROM users;\n",
    ),
    "Other": (
        "Other - Note",
        "# Note\n# Created: " + dt.date.today().isoformat() + "\n",
    ),
}


def _load_json_list(path, warnings, kind):
    """Read a JSON file expected to contain a list (kind = noun in warnings)."""
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        if warnings is not None:
            warnings.append(f"Could not read {os.path.basename(path)}: {exc}")
        return []
    if isinstance(data, list):
        return data
    if warnings is not None:
        warnings.append(
            f"{os.path.basename(path)} does not contain a list of {kind} "
            f"(found {type(data).__name__})."
        )
    return []


def _save_json_list(path, data, atomic=True):
    # Atomic write so a crash or a second instance cannot corrupt the database.
    if not atomic:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        return
    directory = os.path.dirname(path) or "."
    # tempfile is deferred to start-up (see _LAZY_STDLIB). This writer is on
    # the close path and on every settings change, so it resolves the module
    # here -- but only once, and only for the first write of a session.
    import tempfile as _tempfile
    fd, temp_path = _tempfile.mkstemp(dir=directory, prefix=".sdb-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except Exception:
        try:
            os.remove(temp_path)
        except OSError:
            pass
        raise
class _PathStore:
    # JSON-list store bound to a path, a warning noun and a write mode.
    def __init__(self, path, kind, mode=None):
        self.path = path
        self.kind = kind
        self.mode = mode

    def load(self, warnings=None):
        return _load_json_list(self.path, warnings, self.kind)

    def save(self, data):
        _save_json_list(self.path, data, atomic=True)
        if self.mode is not None:
            try:
                os.chmod(self.path, self.mode)
            except OSError:
                pass


def load_connections(path, warnings=None):
    """Read the saved connections, reporting unreadable files via `warnings`."""
    return _PathStore(path, "connections", mode=0o600).load(warnings)


def save_connections(path, data):
    _PathStore(path, "connections", mode=0o600).save(data)


def load_data(path, warnings=None):
    """Read the script database, reporting unreadable files via `warnings`."""
    return _PathStore(path, "scripts").load(warnings)


def save_data(path, data):
    _PathStore(path, "scripts").save(data)


def is_separator(entry):
    """True when a list entry is a visual separator row, not a script."""
    return bool(isinstance(entry, dict) and entry.get("kind") == SEPARATOR_KIND)


# Monotonic counter guaranteeing unique separator ids even when several
# separators are created inside the same clock tick.
_SEPARATOR_SEQUENCE = [0]


def next_separator_serial():
    _SEPARATOR_SEQUENCE[0] += 1
    return _SEPARATOR_SEQUENCE[0]


def make_separator(label=""):
    """Build a new separator row with a unique id."""
    now = dt.datetime.now()
    return {
        "kind": SEPARATOR_KIND,
        "id": f"sep-{now.timestamp()}-{next_separator_serial()}",
        "name": (label or "").strip(),
        "category": "",
        "tags": "",
        "desc": "",
        "code": "",
        "date": now.strftime("%Y-%m-%d %H:%M:%S"),
    }
def load_settings(path, warnings=None):
    """Read the UI settings dict, falling back to an empty dict."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        if warnings is not None:
            warnings.append(f"Could not read {os.path.basename(path)}: {exc}")
        return {}
    return data if isinstance(data, dict) else {}


def clean_settings(merged):
    """Drop settings that are unusable, so a default is used instead.

    ``settings_db.json`` is a plain JSON file a user can edit by hand, and an
    older or newer build can leave a key out or write a value the program cannot
    use. Two things are checked: the *type* (a string where a boolean belongs is
    dropped, not applied) and, for the settings that have a fixed set of legal
    values, the *value* itself. A bad entry falls back to its default rather
    than reaching a widget and breaking the layout.
    """
    for key, expected in SETTINGS_TYPES.items():
        if key not in merged:
            continue
        value = merged[key]
        if expected is int and isinstance(value, bool):
            continue
        if not isinstance(value, expected):
            merged.pop(key)
            continue
        if key == "lang" and value not in LANGUAGES:
            merged.pop(key)
    return merged


def save_settings(path, data):
    _save_json_list(path, data, atomic=True)


# Column keys of the Scripts list, in display order.
LIST_COLUMNS = ("name", "category", "tags", "date", "favorite")

# Default user-interface preferences; merged with the saved values on load.
DEFAULT_SETTINGS = {
    # Language and appearance. Both are remembered, so the program opens the way
    # it was last left: language first, then the palette and the compact layout.
    "lang": "en",
    "theme": "dark",
    "compact_ui": False,
    # The Scripts list.
    "columns": {key: True for key in LIST_COLUMNS},
    "group_by_category": False,
    "separators_enabled": True,
    "show_separators": True,
    "zebra_stripes": True,
    "show_row_numbers": False,
    "bold_separators": True,
    "empty_separator_label": "",
    # Personalisation remembered across a restart.
    "sort_ascending": True,
    "last_tab": 0,
    "filter_favorites": False,
    "filter_text": "",
}

# Settings that are not part of the list layout and must survive a damaged or
# partial settings file: a value of the wrong type is dropped rather than
# applied, so a hand-edited file cannot put the window into an unusable state.
SETTINGS_TYPES = {
    "lang": str,
    "theme": str,
    "compact_ui": bool,
    "group_by_category": bool,
    "separators_enabled": bool,
    "show_separators": bool,
    "zebra_stripes": bool,
    "show_row_numbers": bool,
    "bold_separators": bool,
    "empty_separator_label": str,
    "sort_ascending": bool,
    "last_tab": int,
    "filter_favorites": bool,
    "filter_text": str,
    "column_width_shares": dict,
    "window_geometry": dict,
}

# The two languages the program ships with.
LANGUAGES = ("en", "pl")


def script_to_dict(script):
    """Return only the meaningful fields of a script entry."""
    return {
        "id": script.get("id"),
        "name": script.get("name", ""),
        "category": script.get("category", ""),
        "tags": script.get("tags", ""),
        "desc": script.get("desc", ""),
        "code": script.get("code", ""),
        "date": script.get("date", ""),
        "meta": script.get("meta", {}),
    }


def unified_diff(old_entry, new_entry):
    """Human readable unified diff between two script versions."""
    import difflib
    old_lines = json.dumps(script_to_dict(old_entry), ensure_ascii=False, indent=2, sort_keys=True).splitlines()
    new_lines = json.dumps(script_to_dict(new_entry), ensure_ascii=False, indent=2, sort_keys=True).splitlines()
    return "\n".join(
        difflib.unified_diff(old_lines, new_lines, fromfile="previous", tofile="current", lineterm="")
    )
def _clean_meta(raw):
    # Sanitise an imported metadata dict into known string fields.
    result = {}
    if not isinstance(raw, dict):
        return result
    for key, value in raw.items():
        if value is None:
            continue
        result[str(key)] = value if isinstance(value, (int, float, bool)) else str(value)
    return result


def guess_category(code):
    """Very lightweight language guess used only to preselect a category."""
    text = code.strip()
    if not text:
        return None
    if text.startswith("#!") and ("bash" in text.splitlines()[0] or "sh" in text.splitlines()[0]):
        return "Bash"
    if text.startswith("#!") and "python" in text.splitlines()[0]:
        return "Python"
    if re.search(r"^\s*<(\?xml|!DOCTYPE html|html)", text, re.IGNORECASE | re.MULTILINE):
        return "HTML"
    if re.search(r"<\s*scriptlanguage|<\s*hta:", text, re.IGNORECASE):
        return "HTA"
    if re.search(r"(Windows Registry Editor|^\s*\[HKEY_)", text, re.IGNORECASE | re.MULTILINE):
        return "Registry (REG)"
    if re.search(r"^\s*(SELECT|INSERT|UPDATE|DELETE|CREATE TABLE|PRAGMA|ALTER TABLE)\b", text, re.IGNORECASE | re.MULTILINE):
        return "SQL"
    if re.search(r"\b(Write-Host|Get-ChildItem|param\s*\(|\$env:)", text):
        return "PowerShell"
    if re.match(r"^\s*@echo\s+off|\b(setlocal|goto\s+:\w+)", text, re.IGNORECASE):
        return "Batch"
    if re.search(r"\b(console\.log|document\.(getElementById|querySelector)|function\s+\w+\s*\()", text):
        return "JavaScript"
    if re.search(r"\b(System\.out\.println|public\s+static\s+void\s+main)\b", text):
        return "Java"
    if re.search(r"#include\s*<|std::", text):
        return "C++"
    if re.match(r"^\s*[\{\[]", text) and text.rstrip().endswith(("}", "]")):
        try:
            json.loads(text)
            return "JSON"
        except Exception:
            pass
    if re.search(r"\b(Option Explicit|Sub\s+\w+|End Sub|WScript\.)", text, re.IGNORECASE):
        return "VBScript"
    if re.search(r"^(import|from|def|class|if __name__)\b", text, re.MULTILINE):
        return "Python"
    return None
def highlight_text(text, category):
    """Return HTML with a very small, safe syntax highlight for a code block."""
    placeholders = []

    def stash(value, css):
        token = f"\x00{len(placeholders)}\x00"
        placeholders.append((token, f'<span style="color:{css}">{escape_html(value)}</span>'))
        return token

    escaped = escape_html(text)
    # Comments first so keywords inside them are not highlighted.
    escaped = re.sub(r"#[^\n]*", lambda m: stash(m.group(0), "#6a9955"), escaped)
    escaped = re.sub(r"//[^\n]*", lambda m: stash(m.group(0), "#6a9955"), escaped)
    escaped = re.sub(r"&quot;.*?&quot;|&#x27;.*?&#x27;", lambda m: stash(m.group(0), "#ce9178"), escaped)
    for pattern in (r"\b\d+(?:\.\d+)?\b",):
        escaped = re.sub(pattern, lambda m: stash(m.group(0), "#b5cea8"), escaped)
    for pattern in (
        r"\b(def|class|import|from|return|if|elif|else|for|while|try|except|finally|with|as|lambda|None|True|False|self)\b",
        r"\b(function|const|let|var|new|typeof|async|await|SELECT|INSERT|UPDATE|DELETE|FROM|WHERE|TABLE|CREATE)\b",
    ):
        escaped = re.sub(pattern, lambda m: stash(m.group(0), "#569cd6"), escaped)
    for token, html in reversed(placeholders):
        escaped = escaped.replace(token, html)
    return f'<pre style="white-space:pre-wrap; margin:0; font-family:Consolas, monospace;">{escaped}</pre>'


def escape_html(text):
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
    )
def normalize_entry(raw):
    """Coerce an imported dict into a valid script entry (or None)."""
    if not isinstance(raw, dict):
        return None
    # Separators belong to the user's own list layout, so keep them as they
    # are instead of running them through the script normalization below.
    if is_separator(raw):
        separator = make_separator(str(raw.get("name", "")))
        separator["id"] = str(raw.get("id") or separator["id"])
        separator["date"] = raw.get("date") or separator["date"]
        return separator
    code = raw.get("code") or ""
    name = (raw.get("name") or "").strip()
    if not name and not code:
        return None
    if not name:
        name = f"Imported script {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    category = raw.get("category") or ""
    if category not in CATEGORIES:
        # The name decides first: a script called deploy.ps1 is PowerShell even
        # when the content heuristic would say something else. A category the
        # import did not carry is kept as a new one rather than thrown away.
        guessed = category_for_script(name, code)
        if guessed != "Other":
            category = guessed
        elif category:
            CATEGORIES.append(category)
        else:
            category = "Other"
    tag_value = raw.get("tags") or ""
    if isinstance(tag_value, (list, tuple)):
        tag_value = ", ".join(str(t) for t in tag_value)
    return {
        "id": str(raw.get("id") or dt.datetime.now().timestamp()),
        "name": name,
        "category": category,
        "tags": str(tag_value).strip(),
        "desc": (raw.get("desc") or "").strip(),
        "code": code,
        "date": raw.get("date") or dt.datetime.now().strftime("%Y-%m-%d"),
        "meta": _clean_meta(raw.get("meta")),
    }


def extension_for_category(category):
    """Return the natural file extension (with dot) for a script category."""
    return CATEGORY_EXTENSIONS.get(category, ".txt")


def category_for_extension(filename):
    """Guess the script category from a file name's extension (or None).

    The canonical table is checked first, then the alias table, so
    secondary spellings such as .yml, .toml or .rake still resolve.
    """
    extension = os.path.splitext(filename)[1].lower()
    category = EXTENSION_CATEGORIES.get(extension)
    if category is not None:
        return category
    return EXTENSION_ALIASES.get(extension)


def category_for_script(name, code="", default="Other"):
    """Return the category/language for a script, extension first.

    A file name carries the strongest signal about what a script is: `deploy.ps1`
    is PowerShell no matter what the first line looks like, and a script with a
    shebang or a specific API is still recognised when the extension is missing
    or unfamiliar. The order is therefore:

    1. the file extension (canonical table, then the alias table),
    2. the content heuristic (shebang, ``<html>``, ``@echo off``, ...),
    3. `default`.

    This is what every import path uses, so a script imported from a file, a
    folder or a JSON export always lands in the same category.
    """
    filename = os.path.basename((name or "").strip().replace("\\", "/"))
    return (category_for_extension(filename)
            or guess_category(code or "")
            or default)


def safe_file_name(name, default="script"):
    """Turn a script name into a file-name stem safe on every platform."""
    stem = re.sub('[\\/:*?"<>|]+', "_", (name or "").strip()).strip(". ")
    return stem or default


def script_export_path(script, directory):
    """Build a destination path that keeps the script's original extension."""
    ext = extension_for_category(script.get("category", ""))
    return os.path.join(directory, safe_file_name(script.get("name", "")) + ext)
def build_script_from_file(path):
    """Read a plain source file and return a script entry (or None).

    The category (language) is derived from the file extension first, so the
    file keeps its natural form, falling back to the content heuristic.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            code = handle.read()
    except OSError as exc:
        raise OSError(str(exc))
    filename = os.path.basename(path)
    category = category_for_script(filename, code)
    now = dt.datetime.now()
    return {
        "id": str(now.timestamp()),
        "name": os.path.splitext(filename)[0] or filename,
        "category": category,
        "tags": "",
        "desc": f"Imported from {filename}",
        "code": code,
        "date": now.strftime("%Y-%m-%d %H:%M:%S"),
        "date_created": now.strftime("%Y-%m-%d %H:%M:%S"),
        "favorite": False,
        "history": [],
    }


class RunScriptDialog(QtWidgets.QDialog):
    """Modal runner for a script: executes it and shows live output.

    Workers run in a background thread and never touch Qt widgets directly;
    every UI update is marshalled through a thread-safe queue that is drained
    on the GUI thread by a timer. The dialog can stop a running process, shows
    elapsed time and the exit code, and lets the user feed stdin to a script
    that is waiting for input.
    """

    RUN_TIMEOUT_SECONDS = 300
    PUMP_INTERVAL_MS = 40
    ANSI_RESET = "\x1b[0m"
    ANSI_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
    ANSI_COLOURS = {
        30: "#1f1f1f", 31: "#ff7b72", 32: "#9be9a8", 33: "#f7c66d",
        34: "#8ab4f8", 35: "#d2a8ff", 36: "#79c0ff", 37: "#dfe7f5",
        90: "#6e7681", 91: "#ffa198", 92: "#56d364", 93: "#e3b341",
        94: "#79c0ff", 95: "#d2a8ff", 96: "#a5d6ff", 97: "#ffffff",
    }

    def __init__(self, parent, title, script_text, language, translator=None):
        super().__init__(parent)
        # The runner is the only place threading is needed, and it is opened on
        # demand, so the deferred module is resolved here rather than paid for
        # on every start of the program.
        import threading
        self._t = translator or (lambda key, **kw: key)
        self.script_text = script_text
        self.language = (language or "").lower().strip()
        self.title_text = title
        # Worker -> GUI marshalling.
        self._output_queue = collections.deque()
        self._queue_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._process_lock = threading.Lock()
        self._process = None          # Popen of the running child, if any
        self._stdin_handle = None     # file object wrapping the child's stdin
        self._running = False
        self._started_at = None
        self._ansi_colour = None
        self._ansi_bold = False
        self._ansi_pending = ""
        self.setWindowTitle(f"{self._t('run')} - {title}")
        self.resize(820, 560)
        self.setWindowFlag(QtCore.Qt.WindowContextHelpButtonHint, False)
        layout = QtWidgets.QVBoxLayout(self)

        header = QtWidgets.QHBoxLayout()
        lang_badge = QtWidgets.QLabel(self.language.title() or "Script")
        lang_badge.setStyleSheet(
            """background:#2d2d2d; border:1px solid #404040; border-radius:3px;"""
            """padding:2px 6px; color:#8ab4f8; font-weight:bold;"""
        )
        title_label = QtWidgets.QLabel(f"\u25b6 {title}")
        title_label.setStyleSheet("font-weight: bold; color: #8ab4f8;")
        header.addWidget(lang_badge)
        header.addWidget(title_label)
        header.addStretch()
        self.elapsed_label = QtWidgets.QLabel("")
        self.elapsed_label.setStyleSheet("color: #9aa0a6;")
        header.addWidget(self.elapsed_label)
        self.status_label = QtWidgets.QLabel("")
        self.status_label.setStyleSheet("color: #f7c66d; font-weight: bold;")
        header.addWidget(self.status_label)
        layout.addLayout(header)

        self.output = QtWidgets.QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setFont(QFont("Consolas", 10))
        self.output.setStyleSheet("background-color: #1f1f1f; color: #dfe7f5;")
        self.output.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
        layout.addWidget(self.output, 1)

        # stdin row - lets a long-running/waiting script receive input.
        stdin_row = QtWidgets.QHBoxLayout()
        self.stdin_label = QtWidgets.QLabel("stdin")
        self.stdin_label.setStyleSheet("color:#9aa0a6;")
        self.stdin_edit = QtWidgets.QLineEdit()
        self.stdin_edit.setPlaceholderText(self._t("runner_stdin_placeholder"))
        self.stdin_edit.setFont(QFont("Consolas", 10))
        self.stdin_edit.returnPressed.connect(self.send_stdin)
        self.btn_send = QtWidgets.QPushButton(self._t("runner_send"))
        self.btn_send.clicked.connect(self.send_stdin)
        stdin_row.addWidget(self.stdin_label)
        stdin_row.addWidget(self.stdin_edit, 1)
        stdin_row.addWidget(self.btn_send)
        layout.addLayout(stdin_row)
        footer = QtWidgets.QHBoxLayout()
        self.btn_rerun = QtWidgets.QPushButton(self._t("runner_rerun"))
        self.btn_rerun.clicked.connect(self.start_run)
        self.btn_stop = QtWidgets.QPushButton(self._t("runner_stop"))
        self.btn_stop.clicked.connect(self.stop_run)
        self.btn_stop.setEnabled(False)
        self.btn_copy = QtWidgets.QPushButton(self._t("copy"))
        self.btn_copy.clicked.connect(self.copy_output)
        self.btn_clear = QtWidgets.QPushButton(self._t("clear"))
        self.btn_clear.clicked.connect(self.output.clear)
        self.btn_wrap = QtWidgets.QPushButton(self._t("runner_wrap"))
        self.btn_wrap.setCheckable(True)
        self.btn_wrap.toggled.connect(self._toggle_wrap)
        self.btn_close = QtWidgets.QPushButton("Close")
        self.btn_close.clicked.connect(self.close)
        footer.addWidget(self.btn_rerun)
        footer.addWidget(self.btn_stop)
        footer.addStretch()
        footer.addWidget(self.btn_copy)
        footer.addWidget(self.btn_clear)
        footer.addWidget(self.btn_wrap)
        footer.addWidget(self.btn_close)
        layout.addLayout(footer)

        self._pump_timer = QtCore.QTimer(self)
        self._pump_timer.setInterval(self.PUMP_INTERVAL_MS)
        self._pump_timer.timeout.connect(self._drain_output_queue)
        self._pump_timer.start()

        self._elapsed_timer = QtCore.QTimer(self)
        self._elapsed_timer.setInterval(200)
        self._elapsed_timer.timeout.connect(self._update_elapsed)

        QtCore.QTimer.singleShot(50, self.start_run)

    # ----------------------------------------------------------- queueing API
    def append_text(self, text, color=None):
        """Queue output text for display (safe from worker threads)."""
        with self._queue_lock:
            self._output_queue.append(("text", text, color))

    def set_status(self, text, color=None):
        """Queue a status-label update (safe from worker threads)."""
        with self._queue_lock:
            self._output_queue.append(("status", text, color))

    def set_running(self, running):
        """Queue a running/idle transition (safe from worker threads)."""
        with self._queue_lock:
            self._output_queue.append(("running", running, None))

    @QtCore.pyqtSlot()
    def _drain_output_queue(self):
        items = []
        with self._queue_lock:
            while self._output_queue:
                items.append(self._output_queue.popleft())
        for kind, value, color in items:
            if kind == "status":
                self.status_label.setText(value)
                if color:
                    self.status_label.setStyleSheet(f"color: {color}; font-weight: bold;")
            elif kind == "running":
                self._apply_running(bool(value))
            else:
                self._apply_text(value, color)

    def _apply_text(self, text, color=None):
        # QPlainTextEdit has no setTextColor(); colour must be applied via a
        # QTextCharFormat on the cursor (or by setting the widget default).
        text = self._ansi_pending + text
        self._ansi_pending = ""
        last_escape = text.rfind("\x1b")
        if last_escape >= 0:
            last_match = None
            for last_match in self.ANSI_CSI_RE.finditer(text):
                pass
            if last_match is None or last_match.end() <= last_escape:
                self._ansi_pending = text[last_escape:]
                text = text[:last_escape]
        cursor = self.output.textCursor()
        cursor.movePosition(QtGui.QTextCursor.End)
        position = 0
        for match in self.ANSI_CSI_RE.finditer(text):
            self._insert_output_segment(cursor, text[position:match.start()], color)
            sequence = match.group(0)
            if sequence.endswith("m"):
                self._apply_ansi_codes(sequence[2:-1])
            position = match.end()
        self._insert_output_segment(cursor, text[position:], color)
        self.output.setTextCursor(cursor)
        self.output.ensureCursorVisible()

    def _insert_output_segment(self, cursor, text, fallback_color):
        if not text:
            return
        fmt = QtGui.QTextCharFormat()
        foreground = self._ansi_colour or fallback_color or "#ffffff"
        fmt.setForeground(QtGui.QColor(foreground))
        if self._ansi_bold:
            fmt.setFontWeight(QtGui.QFont.Bold)
        cursor.setCharFormat(fmt)
        cursor.insertText(text)

    def _apply_ansi_codes(self, codes):
        """Apply ANSI SGR state without exposing escape sequences in output."""
        values = [int(value or 0) for value in codes.split(";")]
        for value in values:
            if value == 0:
                self._ansi_colour = None
                self._ansi_bold = False
            elif value == 1:
                self._ansi_bold = True
            elif value == 22:
                self._ansi_bold = False
            elif value == 39:
                self._ansi_colour = None
            elif value in self.ANSI_COLOURS:
                self._ansi_colour = self.ANSI_COLOURS[value]

    def _apply_running(self, running):
        self._running = running
        self.btn_stop.setEnabled(running)
        self.btn_rerun.setEnabled(not running)
        if running:
            self._started_at = dt.datetime.now()
            self._elapsed_timer.start()
        else:
            self._elapsed_timer.stop()

    # ------------------------------------------------------------- utilities
    def _update_elapsed(self):
        if not self._running or self._started_at is None:
            return
        seconds = (dt.datetime.now() - self._started_at).total_seconds()
        self.elapsed_label.setText(self._t("runner_elapsed", time=self._fmt_duration(seconds)))

    @staticmethod
    def _fmt_duration(seconds):
        if seconds < 60:
            return f"{seconds:.2f}s"
        minutes, rest = divmod(seconds, 60)
        return f"{int(minutes)}m {rest:.1f}s"

    def copy_output(self):
        QtWidgets.QApplication.clipboard().setText(self.output.toPlainText())

    def _toggle_wrap(self, checked):
        self.output.setLineWrapMode(
            QtWidgets.QPlainTextEdit.WidgetWidth if checked
            else QtWidgets.QPlainTextEdit.NoWrap
        )
        self.btn_wrap.setText("Wrap: on" if checked else self._t("runner_wrap"))

    def closeEvent(self, event):
        """Make sure we never leave a child process running behind us."""
        self._pump_timer.stop()
        self._elapsed_timer.stop()
        self.stop_run(silent=True)
        super().closeEvent(event)
    def _report_unavailable(self, message):
        self.append_text(message + "\n", "#ff7b72")
        self.set_status(self._t("runner_status_unavailable"), "#ff7b72")
        self.set_running(False)

    def _run_in_thread(self, target, *args):
        import threading
        self.set_running(True)
        threading.Thread(target=target, args=args, daemon=True).start()

    def _finish(self, status_key, status_color, **kwargs):
        """Common end-of-run bookkeeping for every runner."""
        self.set_status(self._t(status_key, **kwargs), status_color)
        self.set_running(False)

    # --------------------------------------------------------------- stdin
    def send_stdin(self):
        """Forward a line of text to a running child process' stdin."""
        with self._process_lock:
            handle = self._stdin_handle
        if handle is None:
            return
        text = self.stdin_edit.text()
        self.stdin_edit.clear()
        try:
            handle.write(text + "\n")
            handle.flush()
            self.append_text(f"{text}\n", "#c8c8c8")
        except Exception as exc:
            self.append_text(f"stdin error: {exc}\n", "#ff7b72")

    # ------------------------------------------------------- process control
    def stop_run(self, silent=False):
        """Request the running process to stop (or interrupt in-process code)."""
        import subprocess
        self._stop_event.set()
        with self._process_lock:
            process = self._process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
            except Exception:
                pass
        if not silent:
            self.set_status(self._t("runner_status_stopped"), "#ff7b72")
        self.set_running(False)

    # ------------------------------------------------------- language routing
    def start_run(self):
        """Clear previous output and dispatch to the right runner."""
        if self._running:
            return
        self._stop_event.clear()
        self.output.clear()
        self._ansi_colour = None
        self._ansi_bold = False
        self._ansi_pending = ""
        self.elapsed_label.setText("")
        self.status_label.setText(self._t("runner_status_running"))
        self.status_label.setStyleSheet("color: #f7c66d; font-weight: bold;")
        self._runner_for_language(self.language)()
    def _runner_for_language(self, language):
        if language in {"bash", "sh", "shell"}:
            return self._run_bash
        if language in {"powershell", "pwsh"}:
            return self._run_powershell
        if language in {"javascript", "js", "node"}:
            return self._run_node
        if language in {"batch", "bat", "cmd"}:
            return self._run_batch
        if language in {"vbscript", "vbs"}:
            return self._run_windows_script
        if language == "json":
            return self._run_json
        if language == "sql":
            return self._run_sql_threaded
        if language in {"python", "py", ""}:
            return self._run_python_threaded
        return self._run_as_python_fallback

    def write_temp_file(self, suffix):
        import tempfile
        fd, path = tempfile.mkstemp(suffix=suffix)
        os.close(fd)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(self.script_text)
        return path

    # ------------------------------------------------------------ interpreters
    def _run_bash(self):
        import shutil
        exe = shutil.which("bash")
        if not exe:
            self._report_unavailable(self._t("runner_bash_unavailable"))
            return
        self._run_in_thread(self.run_command, [exe, "-c", self.script_text])

    def _run_powershell(self):
        import shutil
        exe = shutil.which("pwsh") or shutil.which("powershell")
        if not exe:
            self._report_unavailable(self._t("runner_powershell_unavailable"))
            return
        cmd = [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
               "-Command", self.script_text]
        self._run_in_thread(self.run_command, cmd)

    def _run_node(self):
        import shutil
        exe = shutil.which("node") or shutil.which("nodejs")
        if not exe:
            self._report_unavailable(self._t("runner_node_unavailable"))
            return
        tmp = self.write_temp_file(".js")
        # Keep async failures visible and make stack traces point back to the
        # original source locations when Node supports source maps.
        command = [exe, "--enable-source-maps",
                   "--unhandled-rejections=strict", "--trace-uncaught",
                   "--trace-warnings", tmp]
        self._run_in_thread(self.run_command, command, tmp)

    def _run_batch(self):
        if os.name != "nt":
            self._report_unavailable(self._t("runner_batch_unavailable"))
            return
        tmp = self.write_temp_file(".bat")
        self._run_in_thread(self.run_command, ["cmd.exe", "/c", tmp], tmp)

    def _run_windows_script(self):
        """Run a VBScript/HTA file through the Windows Script Host."""
        if os.name != "nt":
            self._report_unavailable(self._t("runner_batch_unavailable"))
            return
        suffix = ".vbs" if self.language in {"vbscript", "vbs"} else ".hta"
        tmp = self.write_temp_file(suffix)
        self._run_in_thread(self.run_command, ["cscript.exe", "//nologo", tmp], tmp)
    # ---------------------------------------------------------------- Python
    def _run_python_threaded(self):
        # Run Python outside the GUI process so input(), sys.exit(), crashes
        # and infinite loops cannot block or corrupt ScriptDB itself.
        tmp = self.write_temp_file(".py")
        self._run_in_thread(
            self.run_command,
            [sys.executable, "-u", "-X", "faulthandler", tmp],
            tmp,
        )

    def run_python(self):
        """Execute the script in-process, capturing stdout/stderr live."""
        import contextlib
        import traceback
        start = dt.datetime.now()
        stream = _QueueStream(self.append_text)
        namespace = {"__name__": "__main__", "__file__": "<script>"}
        try:
            code = compile(self.script_text, "<script>", "exec")
        except SyntaxError:
            self.append_text(traceback.format_exc(), "#ff7b72")
            self._finish("runner_status_error", "#ff7b72")
            return
        try:
            with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
                exec(code, namespace)
            stream.flush()
            elapsed = (dt.datetime.now() - start).total_seconds()
            if not stream.wrote_anything:
                self.append_text(self._t("runner_no_output") + "\n", "#9aa0a6")
            self.set_status(
                self._t("runner_status_done", time=self._fmt_duration(elapsed)),
                "#9be9a8",
            )
            self.set_status(
                self._t("runner_status_done", time=self._fmt_duration(elapsed)),
                "#9be9a8",
            )
            self.set_running(False)
        except KeyboardInterrupt:
            self.append_text(self._t("runner_stopped_note") + "\n", "#ff7b72")
            self._finish("runner_status_stopped", "#ff7b72")
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
            stream.flush()
            self.append_text(self._t("runner_exit_note", code=code,
                                     time=self._fmt_duration((dt.datetime.now() - start).total_seconds())) + "\n",
                             "#8ab4f8" if code == 0 else "#ff7b72")
            self._finish("runner_status_exit", "#9be9a8" if code == 0 else "#ff7b72", code=code)
        except Exception:
            stream.flush()
            self.append_text(traceback.format_exc(), "#ff7b72")
            self._finish("runner_status_error", "#ff7b72")
    # ---------------------------------------------------------------- SQLite
    def _run_sql_threaded(self):
        self._run_in_thread(self.run_sqlite)

    def run_sqlite(self):
        """Run the script against an in-memory SQLite database and print rows."""
        import sqlite3
        import traceback
        try:
            connection = sqlite3.connect(":memory:")
            connection.isolation_level = None  # autocommit, like the sqlite3 CLI
            cursor = connection.cursor()
            statements = [s for s in _split_sql_statements(self.script_text)]
            if not statements:
                self.append_text(self._t("runner_no_output") + "\n", "#9aa0a6")
            for statement in statements:
                cursor.execute(statement)
                if cursor.description:
                    self._print_result_table(cursor)
                else:
                    affected = getattr(cursor, "rowcount", -1)
                    if affected is not None and affected >= 0:
                        self.append_text(f"-- {affected} row(s) affected\n", "#8ab4f8")
            connection.close()
            self._finish("runner_status_done", "#9be9a8", time="0.00s")
        except Exception:
            self.append_text(traceback.format_exc(), "#ff7b72")
            self._finish("runner_status_error", "#ff7b72")

    def _print_result_table(self, cursor):
        headers = [d[0] for d in cursor.description]
        rows = cursor.fetchall()
        table = "\t".join(headers)
        self.append_text(table + "\n", "#8ab4f8")
        self.append_text("-" * max(4, len(table)) + "\n", "#8ab4f8")
        for row in rows:
            self.append_text("\t".join(str(v) for v in row) + "\n", "#dfe7f5")
        self.append_text(f"-- {len(rows)} row(s)\n", "#9aa0a6")

    # ------------------------------------------------------------------ JSON
    def _run_json(self):
        try:
            parsed = json.loads(self.script_text)
            pretty = json.dumps(parsed, ensure_ascii=False, indent=2)
            self.append_text(pretty + "\n", "#8ab4f8")
            self._finish("runner_status_done", "#9be9a8", time="0.00s")
        except Exception as exc:
            self.append_text(f"JSON error: {exc}\n", "#ff7b72")
            self._finish("runner_status_error", "#ff7b72")

    # ----------------------------------------------------- external process
    def _run_as_python_fallback(self):
        self.append_text(self._t("runner_python_fallback") + "\n", "#f7c66d")
        self._run_python_threaded()
    def run_command(self, command, temp_path=None):
        """Run an external command with consistent runner safeguards."""
        import subprocess
        import threading
        process = None
        start = dt.datetime.now()
        environment = os.environ.copy()
        environment.setdefault("PYTHONUTF8", "1")
        environment.setdefault("PYTHONIOENCODING", "utf-8")
        launch_options = {
            # The application data folder, not the folder the executable sits in
            # (which is PyInstaller's temporary unpack folder once frozen).
            "cwd": DATA_DIR,
            "env": environment,
        }
        if os.name == "nt":
            launch_options["creationflags"] = (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            launch_options["start_new_session"] = True
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                shell=False,
                **launch_options,
            )
        except FileNotFoundError:
            self.append_text(
                self._t("runner_exe_unavailable", name=command[0]) + "\n", "#ff7b72",
            )
            self._finish("runner_status_unavailable", "#ff7b72")
            if temp_path:
                _safe_remove(temp_path)
            return
        except Exception as exc:
            self.append_text(str(exc) + "\n", "#ff7b72")
            self._finish("runner_status_error", "#ff7b72")
            if temp_path:
                _safe_remove(temp_path)
            return
        with self._process_lock:
            self._process = process
            self._stdin_handle = process.stdin
        reader = threading.Thread(
            target=self._stream_process_output, args=(process,), daemon=True
        )
        reader.start()

        timed_out = False
        try:
            while True:
                try:
                    process.wait(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    if self._stop_event.is_set():
                        break
                    if (dt.datetime.now() - start).total_seconds() > self.RUN_TIMEOUT_SECONDS:
                        timed_out = True
                        break
            reader.join(timeout=2)
            elapsed = (dt.datetime.now() - start).total_seconds()
            if timed_out:
                self._kill(process)
                self.append_text(
                    "\n" + self._t("runner_timeout_note", seconds=self.RUN_TIMEOUT_SECONDS) + "\n",
                    "#ff7b72",
                )
                self._finish("runner_status_timeout", "#ff7b72", seconds=self.RUN_TIMEOUT_SECONDS)
            elif self._stop_event.is_set():
                self._kill(process)
                self.append_text("\n" + self._t("runner_stopped_note") + "\n", "#ff7b72")
                self._finish("runner_status_stopped", "#ff7b72")
            else:
                rc = process.returncode
                self.append_text(
                    "\n" + self._t("runner_exit_note", code=rc,
                                   time=self._fmt_duration(elapsed)) + "\n",
                    "#8ab4f8" if rc == 0 else "#ff7b72",
                )
                self._finish(
                    "runner_status_exit",
                    "#9be9a8" if rc == 0 else "#ff7b72",
                    code=rc,
                )
        finally:
            self._kill(process)
            with self._process_lock:
                self._process = None
                self._stdin_handle = None
            if temp_path:
                _safe_remove(temp_path)

    def _stream_process_output(self, process):
        """Read the child's merged stdout/stderr and queue each line."""
        try:
            for line in iter(process.stdout.readline, ""):
                self.append_text(line)
        except (ValueError, OSError):
            pass
        finally:
            try:
                process.stdout.close()
            except Exception:
                pass

    @staticmethod
    def _kill(process):
        if process is None or process.poll() is not None:
            return
        import subprocess
        try:
            if os.name == "nt":
                # A script can spawn children; kill the complete process tree
                # so a stopped runner cannot leave work running in the background.
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            else:
                os.killpg(process.pid, 9)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass
def _safe_remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _split_sql_statements(script):
    """Split a SQL script into individual statements, honouring string literals."""
    statements = []
    current = []
    quote = None
    i = 0
    text = script or ""
    while i < len(text):
        char = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if quote:
            current.append(char)
            if char == quote:
                if nxt == quote:          # doubled quote = escaped quote
                    current.append(nxt)
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if char in ("'", '"', "`"):
            quote = char
            current.append(char)
        elif char == "-" and nxt == "-":      # line comment
            while i < len(text) and text[i] not in "\r\n":
                i += 1
            continue
        elif char == ";":
            statement = "".join(current).strip()
            if statement:
                statements.append(statement)
            current = []
        else:
            current.append(char)
        i += 1
    tail = "".join(current).strip()
    if tail:
        statements.append(tail)
    return statements


class _QueueStream:
    """File-like object that pushes written text onto the runner's display queue."""

    def __init__(self, emit):
        self._emit = emit
        self._buffer = ""
        self.wrote_anything = False

    def write(self, text):
        if not text:
            return 0
        self.wrote_anything = True
        self._buffer += text
        self._flush_lines()
        return len(text)

    def _flush_lines(self):
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self._emit(line + "\n")

    def flush(self):
        if self._buffer:
            self._emit(self._buffer)
            self._buffer = ""

    def isatty(self):
        return False

    def writable(self):
        return True

    def readable(self):
        return False
class TemplateDialog(QtWidgets.QDialog):
    def __init__(self, parent, existing_form):
        super().__init__(parent)
        self.setWindowTitle("Templates")
        self.resize(640, 440)
        self.existing_form = existing_form
        self.template_names = list(TEMPLATES.keys())

        layout = QtWidgets.QVBoxLayout(self)

        list_widget = QtWidgets.QListWidget()
        for name in self.template_names:
            list_widget.addItem(name)
        self.list_widget = list_widget
        self.preview = QtWidgets.QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setFont(QFont("Consolas", 10))

        list_widget.currentRowChanged.connect(self.on_select)
        list_widget.setCurrentRow(0)

        buttons = QtWidgets.QHBoxLayout()
        use_btn = QtWidgets.QPushButton("Use")
        use_btn.clicked.connect(self.use_template)
        copy_btn = QtWidgets.QPushButton("Copy")
        copy_btn.clicked.connect(self.copy_template)
        close_btn = QtWidgets.QPushButton("Close")
        close_btn.clicked.connect(self.close)
        buttons.addWidget(use_btn)
        buttons.addWidget(copy_btn)
        buttons.addStretch()
        buttons.addWidget(close_btn)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        splitter.addWidget(list_widget)
        splitter.addWidget(self.preview)
        splitter.setSizes([180, 460])

        layout.addWidget(splitter)
        layout.addLayout(buttons)

    def on_select(self, index):
        if index < 0:
            return
        name = self.template_names[index]
        _, code = TEMPLATES[name]
        self.preview.setPlainText(code)

    def use_template(self):
        current = self.list_widget.currentItem()
        if not current:
            return
        name = current.text()
        title, code = TEMPLATES[name]
        self.existing_form.script_name.setText(title)
        combo = self.existing_form.category_combo
        combo.setCurrentText(name if combo.findText(name) >= 0 else "Python")
        self.existing_form.code_edit.setPlainText(code)
        self.close()

    def copy_template(self):
        current = self.list_widget.currentItem()
        if not current:
            return
        _, code = TEMPLATES[current.text()]
        QtWidgets.QApplication.clipboard().setText(code)
class CodePreviewDialog(QtWidgets.QDialog):
    """Read-only, syntax highlighted preview of a script."""

    def __init__(self, parent, title, code, category="Python"):
        super().__init__(parent)
        self.setWindowTitle(f"Preview - {title}")
        self.resize(760, 560)
        self.setWindowFlag(QtCore.Qt.WindowContextHelpButtonHint, False)

        layout = QtWidgets.QVBoxLayout(self)
        header = QtWidgets.QLabel(f"{category} - {title}")
        header.setStyleSheet("font-weight:bold; color:#8ab4f8;")
        layout.addWidget(header)

        self.browser = QtWidgets.QTextBrowser()
        self.browser.setOpenExternalLinks(False)
        self.browser.setFont(QFont("Consolas", 10))
        self.browser.setStyleSheet("background:#1f1f1f; color:#dfe7f5;")
        body = [highlight_text(line, category) for line in code.splitlines()]
        self.browser.setHtml("<br>".join(body) or "(empty)")
        layout.addWidget(self.browser)

        footer = QtWidgets.QHBoxLayout()
        copy_btn = QtWidgets.QPushButton("Copy")
        copy_btn.clicked.connect(lambda: QtWidgets.QApplication.clipboard().setText(code))
        close_btn = QtWidgets.QPushButton("Close")
        close_btn.clicked.connect(self.close)
        footer.addStretch()
        footer.addWidget(copy_btn)
        footer.addWidget(close_btn)
        layout.addLayout(footer)


class DiffDialog(QtWidgets.QDialog):
    """Show a unified diff between two revisions of the same script."""

    def __init__(self, parent, title, diff_text):
        super().__init__(parent)
        self.setWindowTitle(f"Diff - {title}")
        self.resize(820, 560)
        self.setWindowFlag(QtCore.Qt.WindowContextHelpButtonHint, False)

        layout = QtWidgets.QVBoxLayout(self)
        self.view = QtWidgets.QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setFont(QFont("Consolas", 10))
        self.view.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
        self.view.setPlainText(diff_text or "(no changes)")
        layout.addWidget(self.view)

        close_btn = QtWidgets.QPushButton("Close")
        close_btn.clicked.connect(self.close)
        footer = QtWidgets.QHBoxLayout()
        footer.addStretch()
        footer.addWidget(close_btn)
        layout.addLayout(footer)
class HistoryDialog(QtWidgets.QDialog):
    """Browse saved revisions of a script and restore / diff any of them."""

    def __init__(self, parent, window, script):
        super().__init__(parent)
        self.window = window
        self.script = script
        self.revisions = [dict(item) for item in script.get("history", [])]
        self.setWindowTitle(f"{window.t('version_history')} - {script.get('name', '')}")
        self.resize(760, 520)
        self.setWindowFlag(QtCore.Qt.WindowContextHelpButtonHint, False)

        layout = QtWidgets.QVBoxLayout(self)
        self.list_widget = QtWidgets.QListWidget()
        current = script_to_dict(script)
        self.list_widget.addItem(self._row_text(current, "current"))
        for index, entry in enumerate(reversed(self.revisions)):
            revision = len(self.revisions) - index
            self.list_widget.addItem(self._row_text(entry, f"#{revision}"))
        self.list_widget.currentRowChanged.connect(self.on_select)
        layout.addWidget(self.list_widget)

        self.preview = QtWidgets.QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setFont(QFont("Consolas", 10))
        layout.addWidget(self.preview)

        buttons = QtWidgets.QHBoxLayout()
        self.btn_diff = QtWidgets.QPushButton(window.t("diff"))
        self.btn_restore = QtWidgets.QPushButton(window.t("restore"))
        close_btn = QtWidgets.QPushButton("Close")
        self.btn_diff.clicked.connect(self.show_diff)
        self.btn_restore.clicked.connect(self.restore_version)
        close_btn.clicked.connect(self.close)
        buttons.addWidget(self.btn_diff)
        buttons.addWidget(self.btn_restore)
        buttons.addStretch()
        buttons.addWidget(close_btn)
        layout.addLayout(buttons)

        self.list_widget.setCurrentRow(0)
        if not self.revisions:
            self.preview.setPlainText(window.t("no_history"))

    @staticmethod
    def _row_text(entry, label):
        return f"[{label}] {entry.get('date', '')} - {entry.get('name', '')} ({entry.get('category', '')})"

    def current_entry(self):
        row = self.list_widget.currentRow()
        if row <= 0:
            return dict(self.script)
        index = len(self.revisions) - row
        if 0 <= index < len(self.revisions):
            return dict(self.revisions[index])
        return dict(self.script)

    def on_select(self, row):
        if row < 0:
            return
        entry = self.current_entry()
        self.preview.setPlainText(entry.get("code", ""))
    def show_diff(self):
        row = self.list_widget.currentRow()
        if row <= 0:
            self.window.show_message(self.window.t("info"), self.window.t("no_history"))
            return
        selected = self.current_entry()
        index = len(self.revisions) - row
        if index - 1 >= 0:
            previous = self.revisions[index - 1]
        else:
            previous = self.script
        DiffDialog(self, self.script.get("name", ""), unified_diff(previous, selected)).exec_()

    def restore_version(self):
        row = self.list_widget.currentRow()
        if row <= 0:
            return
        if QtWidgets.QMessageBox.question(
            self, self.window.t("confirm"), self.window.t("confirm_restore"),
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
        ) != QtWidgets.QMessageBox.Yes:
            return
        entry = self.current_entry()
        self.window._open_script_editor_dialog(entry)
        self.window.show_message(self.window.t("info"), self.window.t("restored"))
        self.close()


class WheelScrollFilter(QtCore.QObject):
    """Application-wide wheel handler so scrolling works everywhere.

    Widgets that scroll themselves (text views, trees, lists, combo popups,
    scroll areas) keep their normal behaviour. When the wheel turns over a
    widget that cannot scroll on its own (labels, plain widgets, layout
    background, toolbars), the event is redirected to the nearest scrollable
    ancestor so the whole program responds to the mouse wheel.
    """

    # Widgets that handle the wheel themselves and must never be hijacked.
    SCROLLABLE = (QtWidgets.QAbstractScrollArea,)
    # Popups (menus, combo dropdowns) must keep the wheel for their own list.
    POPUP_CLASSES = (QtWidgets.QMenu, QtWidgets.QComboBox)

    def eventFilter(self, obj, event):
        if event.type() != QtCore.QEvent.Wheel:
            return False
        if not isinstance(obj, QtWidgets.QWidget):
            return False
        # Let anything that scrolls itself (and popups) behave normally.
        if isinstance(obj, self.SCROLLABLE):
            return False
        if isinstance(obj, self.POPUP_CLASSES):
            return False
        delta = event.pixelDelta().y() or event.angleDelta().y()
        if not delta:
            return False
        # Start at the nearest container, but let the wheel bubble outward
        # when that container is already at the relevant edge.
        for target in self._scroll_areas(obj):
            bar = target.verticalScrollBar()
            if not bar.isVisible() or bar.minimum() == bar.maximum():
                continue
            next_value = max(bar.minimum(), min(bar.maximum(),
                                                bar.value() - delta))
            if next_value == bar.value():
                continue
            bar.setValue(next_value)
            event.accept()
            return True
        return False

    @staticmethod
    def _scroll_areas(widget):
        # Walk up the parent chain so nested panels scroll naturally.
        parent = widget.parentWidget()
        while parent is not None:
            if isinstance(parent, QtWidgets.QAbstractScrollArea):
                yield parent
            parent = parent.parentWidget()


class AboutDialog(QtWidgets.QDialog):
    """A compact, visual About panel that remains readable at small sizes."""

    def __init__(self, window):
        super().__init__(window)
        self.setWindowTitle(window.t("about_title", "About ScriptDB"))
        self.setWindowIcon(load_app_icon())
        self.setMinimumSize(620, 470)
        self.resize(700, 540)

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(22, 22, 22, 18)
        root.setSpacing(14)

        header = QtWidgets.QFrame()
        header.setObjectName("aboutHeader")
        header_layout = QtWidgets.QHBoxLayout(header)
        header_layout.setContentsMargins(18, 16, 18, 16)
        icon = QtWidgets.QLabel()
        icon.setPixmap(app_icon_pixmap(76) or draw_info_glyph(76))
        icon.setFixedSize(76, 76)
        icon.setScaledContents(True)
        header_layout.addWidget(icon)
        heading = QtWidgets.QVBoxLayout()
        title = QtWidgets.QLabel(window.t("about_title", "About ScriptDB"))
        title.setObjectName("aboutTitle")
        version = QtWidgets.QLabel(window.t("about_version", "2.5.0"))
        version.setObjectName("aboutVersion")
        subtitle = QtWidgets.QLabel("Local code workspace and script database")
        subtitle.setObjectName("aboutSubtitle")
        heading.addWidget(title)
        heading.addWidget(version)
        heading.addWidget(subtitle)
        heading.addStretch()
        header_layout.addLayout(heading, 1)
        root.addWidget(header)

        stats = QtWidgets.QHBoxLayout()
        stats.setSpacing(10)
        for label, value in (
                ("Scripts", len(window.scripts)),
                ("Connections", len(window.connections)),
                ("Engine", "PyQt5")):
            tile = QtWidgets.QFrame()
            tile.setObjectName("aboutTile")
            tile_layout = QtWidgets.QVBoxLayout(tile)
            tile_layout.setContentsMargins(12, 9, 12, 9)
            number = QtWidgets.QLabel(str(value))
            number.setObjectName("aboutMetric")
            caption = QtWidgets.QLabel(label)
            caption.setObjectName("aboutCaption")
            tile_layout.addWidget(number)
            tile_layout.addWidget(caption)
            stats.addWidget(tile, 1)
        root.addLayout(stats)

        details = QtWidgets.QTextBrowser()
        details.setOpenExternalLinks(True)
        details.setPlaceholderText("About ScriptDB")
        details.setPlainText(window.t(
            "about_text",
            version=window.t("about_version", "2.5.0"),
            scripts=len(window.scripts),
            connections=len(window.connections),
            data=DATA_FILE,
            conn=CONN_FILE,
        ))
        root.addWidget(details, 1)

        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self.setStyleSheet("""
            QFrame#aboutHeader {
                background: palette(midlight);
                border: 1px solid palette(mid);
                border-radius: 8px;
            }
            QLabel#aboutTitle { font-size: 24px; font-weight: bold; }
            QLabel#aboutVersion { color: palette(highlight); font-weight: bold; }
            QLabel#aboutSubtitle, QLabel#aboutCaption { color: palette(mid); }
            QFrame#aboutTile {
                background: palette(base);
                border: 1px solid palette(mid);
                border-radius: 6px;
            }
            QLabel#aboutMetric { font-size: 18px; font-weight: bold; }
            QTextBrowser { padding: 10px; }
        """)


def install_wheel_scrolling(app):
    """Install the global wheel filter so scrolling works in every tab.

    Returns the filter instance so the caller can keep a reference (Qt does
    not own the Python object, so dropping it would disable the filter).
    """
    filt = WheelScrollFilter(app)
    app.installEventFilter(filt)
    app._wheel_scroll_filter = filt
    return filt

def apply_application_theme(app, theme=None, compact=False,
                            low_graphics=False):
    # Give the whole application one look, once, before any window is built.
    #
    # This is the single place the theme is installed.  It has to happen on the
    # QApplication rather than on the window, because Qt draws several things --
    # tooltips, the item-view drop indicator, the file dialog's own widgets --
    # from the application palette, and a window-level stylesheet cannot reach
    # them.
    if app_theme is None:                       # the tabs package is missing
        return False
    chosen = theme or getattr(app, "_sdb_theme", None) or app_theme.DEFAULT_THEME
    app._sdb_theme = chosen
    app._sdb_compact = bool(compact)
    palette = app_theme.build_palette(chosen, compact)
    if palette is not None:
        app.setPalette(palette)
    sheet = (app_theme.build_low_graphics_stylesheet(chosen) if low_graphics
             else app_theme.build_stylesheet(chosen, compact))
    app.setStyleSheet(sheet)
        # The fonts the tabs ask for by name (Consolas) may be missing on a bare
        # Linux box; asking here means every widget gets a monospaced fallback
        # instead of each tab guessing.  Nothing is set when the tool already has a
        # monospace family, so a properly configured machine is left alone.
    try:
        database = QtGui.QFontDatabase()
        families = set(database.families())
        if not ({"Consolas", "Cascadia Mono", "DejaVu Sans Mono",
                 "Liberation Mono", "Courier New"} & families):
            for candidate in ("Menlo", "Monaco", "Ubuntu Mono", "Courier"):
                if candidate in families:
                    font = app.font()
                    font.setFamily(candidate)
                    app.setFont(font)
                    break
    except Exception:
        pass
    return True


class ScriptForm(QtWidgets.QWidget):
    def __init__(self, parent, on_save, on_clear, on_copy):
        super().__init__(parent)
        self.on_save = on_save
        self.on_clear = on_clear
        self.on_copy = on_copy

        self.setLayout(QtWidgets.QVBoxLayout())
        form = QtWidgets.QFormLayout()
        self.script_name = QtWidgets.QLineEdit()
        self.category_combo = QtWidgets.QComboBox()
        self.category_combo.addItems(CATEGORIES)
        self.favorite_btn = QtWidgets.QPushButton("\u2606 Favorite")
        self.favorite_btn.setCheckable(True)
        self.favorite_btn.setToolTip("Mark this script as a favorite (Ctrl+F)")
        self.tags_edit = QtWidgets.QLineEdit()
        self.description_edit = QtWidgets.QTextEdit()
        self.code_edit = QtWidgets.QPlainTextEdit()
        self.code_edit.setFont(QFont("Consolas", 10))
        self.code_edit.setTabChangesFocus(False)

        name_row = QtWidgets.QHBoxLayout()
        name_row.addWidget(self.script_name, 1)
        name_row.addWidget(self.favorite_btn)

        form.addRow("Script Name:", name_row)
        form.addRow("Category:", self.category_combo)
        form.addRow("Tags (comma separated):", self.tags_edit)
        form.addRow("Description:", self.description_edit)
        form.addRow("Code:", self.code_edit)
        self.layout().addLayout(form)
        buttons = QtWidgets.QHBoxLayout()
        save_btn = QtWidgets.QPushButton("Save")
        clear_btn = QtWidgets.QPushButton("Clear")
        copy_btn = QtWidgets.QPushButton("Copy")
        preview_btn = QtWidgets.QPushButton("Preview")
        buttons.addWidget(save_btn)
        buttons.addWidget(clear_btn)
        buttons.addWidget(preview_btn)
        buttons.addStretch()
        buttons.addWidget(copy_btn)
        self.layout().addLayout(buttons)

        save_btn.clicked.connect(self.on_save)
        clear_btn.clicked.connect(self.on_clear)
        copy_btn.clicked.connect(self.on_copy)
        preview_btn.clicked.connect(self.preview_code)

    def preview_code(self):
        """Show the current code in a read-only, syntax highlighting window."""
        dialog = CodePreviewDialog(self, self.script_name.text().strip() or "Preview",
                                   self.code_edit.toPlainText(), self.category_combo.currentText())
        dialog.exec_()

    def clear_fields(self):
        self.script_name.clear()
        self.tags_edit.clear()
        self.description_edit.clear()
        self.code_edit.clear()
        self.favorite_btn.setChecked(False)
        if self.category_combo.count():
            self.category_combo.setCurrentIndex(0)

    def load_entry(self, entry):
        self.script_name.setText(entry.get("name", ""))
        category = entry.get("category", "Python")
        if category in CATEGORIES:
            self.category_combo.setCurrentText(category)
        else:
            self.category_combo.setCurrentIndex(0)
        self.tags_edit.setText(entry.get("tags", ""))
        self.description_edit.setPlainText(entry.get("desc", ""))
        self.code_edit.setPlainText(entry.get("code", ""))
        self.favorite_btn.setChecked(bool(entry.get("favorite")))

    def get_entry_data(self):
        return {
            "name": self.script_name.text().strip(),
            "category": self.category_combo.currentText(),
            "tags": self.tags_edit.text().strip(),
            "desc": self.description_edit.toPlainText().strip(),
            "code": self.code_edit.toPlainText().strip(),
        }


class ScriptPreview(QtWidgets.QWidget):
    """Read-only details and source preview for the selected script."""

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        self.title = QtWidgets.QLabel(window.t("preview", "Preview"))
        self.title.setProperty("role", "title")
        layout.addWidget(self.title)
        self.meta = QtWidgets.QLabel("")
        self.meta.setProperty("role", "hint")
        self.meta.setWordWrap(True)
        layout.addWidget(self.meta)
        self.description = QtWidgets.QPlainTextEdit()
        self.description.setReadOnly(True)
        self.description.setMaximumHeight(90)
        layout.addWidget(self.description)
        self.code = QtWidgets.QPlainTextEdit(self)
        self.code.setReadOnly(True)
        self.code.setFont(QFont("Consolas", 10))
        self.layout().addWidget(self.code, 1)
        self._code_upgraded = False
        self.show_empty()

    def _upgrade_code_widget(self):
        """Swap in the syntax-highlighting editor, once, when it is worth it.

        tabs.editor pulls in the whole editor engine (navigation, templates,
        generators and every syntax module), so it is imported only after a
        real script is being previewed, not while the window is built.
        """
        if self._code_upgraded:
            return
        self._code_upgraded = True
        try:
            from tabs.editor import CodeEditor
        except Exception:
            return
        if isinstance(self.code, CodeEditor):
            return
        upgraded = CodeEditor(self)
        upgraded.setReadOnly(True)
        upgraded.setFont(QFont("Consolas", 10))
        text = self.code.toPlainText()
        self.layout().replaceWidget(self.code, upgraded)
        self.code.deleteLater()
        self.code = upgraded
        self.code.setPlainText(text)

    def _set_code_category(self, category):
        """Highlight the code view, but only if it supports it."""
        setter = getattr(self.code, "set_category", None)
        if callable(setter):
            setter(category)

    def show_empty(self):
        self.title.setText(self.window.t("preview", "Preview"))
        self.meta.clear()
        self.description.clear()
        self.code.setPlainText(self.window.t("select_script", "Select a script"))
        self._set_code_category("Python")

    def show_script(self, script):
        self._upgrade_code_widget()
        self.title.setText(script.get("name", "Untitled"))
        self.meta.setText("%s  |  %s  |  %s" % (
            script.get("category", ""), script.get("tags", ""),
            script.get("date", "")))
        self.description.setPlainText(script.get("desc", ""))
        self.code.setPlainText(script.get("code", ""))
        self._set_code_category(script.get("category", "Python"))
class ScriptDatabaseWindow(QtWidgets.QMainWindow):
    # Attribute name -> (tab label, module path, class name). Each tab is built
    # on first access instead of during __init__: the window used to construct
    # every tab eagerly, which created ~4700 widgets and dominated start-up.
    # Modules are imported lazily too, for the same reason.
    LAZY_TABS = {
        "editor_tab": ("Editor", "tabs.editor", "EditorTab"),
        "analyzer_tab": ("Analyzer", "tabs.analyzer", "ScriptAnalyzerTab"),
        "audit_tab": ("Audit", "tabs.audit", "AuditTab"),
        "debugger_tab": ("Debugger", "tabs.debugger", "DebuggerTab"),
        "sandbox_tab": ("Sandbox", "tabs.sandbox", "SandboxTab"),
        "diff_tab": ("Diff", "tabs.editor", "DiffTab"),
        "vault_tab": ("Vault", "tabs.vault_tab", "VaultTab"),
        "security_tab": ("Security", "tabs.secure_tab", "SecurityTab"),
        "structure_tab": ("Structure", "tabs.structure_tab", "StructureTab"),
        "docs_tab": ("DOCs", "tabs.docs_tab", "DocsTab"),
        "sqlite_tab": ("SQLite", "tabs.sqlite_tab", "SqliteTab"),
        "reports_tab": ("Reports", "tabs.reports_tab", "ReportsTab"),
        "metadata_tab": ("Metadata", "tabs.metadata_tab", "MetadataTab"),
        "search_tab": ("Search", "tabs.search_tab", "SearchTab"),
        "apps_tab": ("Apps", "tabs.apps", "AppsTab"),
    }

    def __getattr__(self, name):
        """Build a tab the first time something asks for it.

        The placeholder is deliberately kept out of __dict__ (see
        _lazy_placeholder): a real attribute there would shadow __getattr__ and
        the tab would never be built.
        """
        if name not in self.LAZY_TABS:
            raise AttributeError(name)
        # Cache the real widget in __dict__ so the next access is a plain
        # attribute lookup: __getattr__ only runs when the key is missing.
        cached = self.__dict__.get(name)
        if cached is not None:
            return cached
        placeholder = self._lazy_placeholders.get(name)
        if placeholder is None:
            raise AttributeError(name)
        label, module_path, class_name = self.LAZY_TABS[name]
        widget = self._build_lazy_tab(name, label, module_path, class_name)
        self.__dict__[name] = widget
        return widget

    def tab_built(self, name):
        """True when the real tab widget exists yet (no build is triggered)."""
        return self.__dict__.get(name) is not None

    def _build_lazy_tab(self, name, label, module_path, class_name):
        """Import one tab module and instantiate it into the strip."""
        placeholder = self._lazy_placeholders.get(name)
        if placeholder is None:
            placeholder = QtWidgets.QWidget()
        try:
            import importlib
            module = importlib.import_module(module_path)
            factory = getattr(module, class_name)
            if factory is not None:
                index = self.tabs.indexOf(placeholder)
                # Reparent before inserting: setParent() on an already added
                # tab would remove it from the strip.
                fresh = factory(self)
                fresh.setParent(self.tabs)
                if index >= 0:
                    self.tabs.removeTab(index)
                    self.tabs.insertTab(index, fresh, label)
                else:
                    self.tabs.addTab(fresh, label)
                self._lazy_placeholders[name] = fresh
                self._pending_tab_init(name, fresh)
                return fresh
        except Exception:
            # A missing or broken tab module must not stop the window: the
            # placeholder keeps the strip and the rest of the app working.
            pass
        self._pending_tab_init(name, placeholder)
        return placeholder

    def _pending_tab_init(self, name, tab):
        """Run the small amount of start-up wiring each tab expects."""
        if name == "diff_tab":
            refresh = getattr(tab, "refresh_pickers", None)
            if callable(refresh):
                refresh()

    def ensure_tab_built(self, name):
        """Force a lazy tab into existence (used by the tab strip)."""
        return getattr(self, name)

    def __init__(self):
        super().__init__()
        # Every tab reads its files from here, so a frozen exe can be dropped
        # anywhere on disk. data_dir is where the program puts the files it
        # produces (reports, exports, logs, snapshots); db_dir is the folder
        # holding the databases.
        self.base_dir = DATA_DIR
        self.db_dir = DB_DIR
        # Collect load warnings so the user learns about a damaged data file
        # instead of silently starting empty and overwriting it on the next save.
        self.load_warnings = []
        self.scripts = [normalize_entry(s) or s for s in load_data(DATA_FILE, self.load_warnings)]
        self.connections = load_connections(CONN_FILE, self.load_warnings)
        # User interface preferences (language, palette, list layout, the tab
        # left open). Loaded before any widget exists so the window opens the
        # way the user left it, without a visible flash of the defaults.
        self.settings = clean_settings(dict(DEFAULT_SETTINGS))
        self.settings.update(
            clean_settings(load_settings(SETTINGS_FILE, self.load_warnings)))
        if not isinstance(self.settings.get("columns"), dict):
            self.settings["columns"] = dict(DEFAULT_SETTINGS["columns"])
        for column in LIST_COLUMNS:
            self.settings["columns"].setdefault(column, True)
        # The remembered language, before apply_language() reads it. An unknown
        # code in a hand-edited file falls back to English rather than reaching
        # the translation table with a key it does not have.
        saved_lang = self.settings.get("lang")
        self.lang = saved_lang if saved_lang in LANGUAGES else "en"
        self.settings["lang"] = self.lang
        # Apply the remembered theme before any widget is built, so the whole
        # window -- including the tabs that read a colour while constructing
        # themselves -- is drawn with the right palette the first time rather
        # than repainting after a visible flash of the default one.
        if app_theme is not None:
            saved_theme = self.settings.get("theme", app_theme.DEFAULT_THEME)
            if saved_theme in app_theme.THEMES:
                application = QtWidgets.QApplication.instance()
                if application is not None:
                    apply_application_theme(
                        application, saved_theme,
                        compact=bool(self.settings.get("compact_ui", False)))
        self.editing_id = None
        # False until the list has been sized once from the saved layout.
        # sectionResized fires while the tree is still being laid out, and a
        # width read at that moment is the default equal split -- storing it
        # would overwrite the proportions the user actually chose.
        self._column_layout_ready = False
        # The filter and sort the user last applied, restored from the settings
        # rather than reset, so the list opens on the view they were looking at.
        self.filter_text = str(self.settings.get("filter_text", ""))
        self.filter_favorites = bool(self.settings.get("filter_favorites", False))
        self.sort_ascending = bool(self.settings.get("sort_ascending", True))
        # True while the columns are being resized by the code, so the
        # sectionResized handler can tell that apart from a user drag.
        self._setting_column_widths = False
        self.setWindowTitle("ScriptDB")
        # Decoding the 200+ KB sdb.ico costs ~40 ms. The window is usable
        # without it, so the icon is loaded and attached after the first event
        # loop pass. The call itself is deferred, not just the assignment.
        QtCore.QTimer.singleShot(0, self._attach_window_icon)
        # Remembered window geometry, so a size the user picked (and which
        # keeps every tab on the strip) survives a restart.
        geometry = self.setting("window_geometry", None)
        if isinstance(geometry, dict):
            try:
                self.restoreGeometry(QtCore.QByteArray.fromBase64(
                    geometry["geometry"].encode("ascii")))
                self.restoreState(QtCore.QByteArray.fromBase64(
                    geometry["state"].encode("ascii")))
            except (KeyError, ValueError, TypeError):
                pass
        else:
            self.resize(1180, 760)
        # Never let the window become smaller than the widest toolbar it has to
        # show: below this size Qt simply clips the buttons on the right edge.
        self.setMinimumSize(960, 850)

        self.central_widget = QtWidgets.QWidget()
        self.setCentralWidget(self.central_widget)
        self.central_layout = QtWidgets.QVBoxLayout(self.central_widget)

        self.top_bar = QtWidgets.QHBoxLayout()
        self.title_label = QtWidgets.QLabel("ScriptDB")
        self.title_label.setProperty("role", "title")
        self.top_bar.addWidget(self.title_label)
        self.top_bar.addStretch()
        self.count_label = QtWidgets.QLabel("Total scripts: 0")
        self.top_bar.addWidget(self.count_label)
        self.stats_btn = QtWidgets.QPushButton("Statistics")
        self.stats_btn.clicked.connect(self.show_statistics)
        self.top_bar.addWidget(self.stats_btn)
        # The ToDo button sits right next to Statistics, as asked: it opens the
        # task workspace and, when there is something waiting, says so on the
        # button itself so the count is visible from any tab.
        self.todo_btn = QtWidgets.QPushButton("\u2611 ToDo")
        self.todo_btn.setToolTip("Tasks, calendar and reminders (Ctrl+T)")
        self.todo_btn.setShortcut(QtCore.Qt.CTRL + QtCore.Qt.Key_T)
        # The button toggles the popup window: it opens it, and closes it when
        # the list is already the window in front, so the same key gets you in
        # and out without reaching for the mouse.
        self.todo_btn.clicked.connect(self.toggle_todo_window)
        self.top_bar.addWidget(self.todo_btn)
        # The theme button cycles Dark -> Light -> Monochrome.  It is a menu
        # rather than a toggle so a third palette did not need a second button,
        # and so the current choice is visible without clicking.
        self.theme_btn = QtWidgets.QToolButton()
        self.theme_btn.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.theme_btn.setToolTip("Colour theme (Ctrl+Shift+T)")
        self.theme_menu = QtWidgets.QMenu(self.theme_btn)
        self._theme_actions = {}
        if app_theme is not None:
            for name in app_theme.theme_names():
                action = self.theme_menu.addAction(app_theme.theme_label(name))
                action.setCheckable(True)
                action.triggered.connect(
                    lambda _checked=False, key=name: self.set_theme(key))
                self._theme_actions[name] = action
        self.theme_btn.setMenu(self.theme_menu)
        self.theme_btn.setShortcut(QtCore.Qt.CTRL + QtCore.Qt.SHIFT + QtCore.Qt.Key_T)
        self.top_bar.addWidget(self.theme_btn)
        self.lang_btn = QtWidgets.QPushButton("\U0001f310 PL")
        self.lang_btn.clicked.connect(self.toggle_language)
        self.top_bar.addWidget(self.lang_btn)
        self.lang_btn.setShortcut(QtCore.Qt.CTRL + QtCore.Qt.Key_L)
        self.about_btn = QtWidgets.QPushButton("\u24d8")
        # The About icon is rendered from sdb.ico too, so it is attached after
        # the first event loop pass rather than during construction. The call
        # itself is deferred, not just the assignment.
        QtCore.QTimer.singleShot(0, self._attach_about_icon)
        self.about_btn.setToolTip("About ScriptDB")
        self.about_btn.setShortcut(QtCore.Qt.CTRL + QtCore.Qt.Key_F1)
        self.about_btn.clicked.connect(self.show_about)
        self.top_bar.addWidget(self.about_btn)
        self.shortcut_new = QtWidgets.QShortcut(QtCore.Qt.CTRL + QtCore.Qt.Key_N, self)
        self.shortcut_new.activated.connect(self.add_new_script)
        self.shortcut_save = QtWidgets.QShortcut(QtCore.Qt.CTRL + QtCore.Qt.Key_S, self)
        self.shortcut_save.activated.connect(self.save_script)
        self.shortcut_fav = QtWidgets.QShortcut(QtCore.Qt.CTRL + QtCore.Qt.Key_F, self)
        self.shortcut_fav.activated.connect(self.toggle_favorite_selected)
        self.shortcut_find = QtWidgets.QShortcut(QtCore.Qt.CTRL + QtCore.Qt.Key_K, self)
        self.shortcut_find.activated.connect(self.focus_filter)
        self.central_layout.addLayout(self.top_bar)

        # Only the Scripts tab is present for now; the other tabs will be added
        # later as separate widgets on this same QTabWidget.
        self.tabs = QtWidgets.QTabWidget()
        self.central_layout.addWidget(self.tabs, 1)

        self.list_tab = QtWidgets.QWidget()
        self.tabs.addTab(self.list_tab, "Scripts")

        # Every other tab starts as an empty placeholder. The real widget is
        # built the first time it is touched (see __getattr__ / LAZY_TABS), so
        # start-up only pays for the Scripts list itself. The placeholder keeps
        # the tab order, the strip labels and saveState() identical.
        self._lazy_placeholders = {}
        for name, (label, _module, _cls) in self.LAZY_TABS.items():
            placeholder = QtWidgets.QWidget()
            layout = QtWidgets.QVBoxLayout(placeholder)
            layout.setContentsMargins(0, 0, 0, 0)
            holder = self.t("tab_loading", "Loading...")
            placeholder.setToolTip(holder)
            self._lazy_placeholders[name] = placeholder
            self.tabs.addTab(placeholder, label)

        # The Editor tab sits right after the Scripts list: it edits one script
        # at a time and writes changes back to the list.  Every non-list tab is
        # a lazy placeholder at this point (see the LAZY_TABS table above); the
        # descriptive notes for each tab used to sit here and are kept with the
        # table so the build order stays readable.

        # The ToDo workspace is NOT a tab: it is a popup window opened from the
        # ToDo button beside Statistics (and Ctrl+T). A task list is consulted
        # while working on something else, so it has to float and be closable
        # without losing the tab you were on. Nothing is added to the strip
        # here; the window is created below, after the tabs are in place.
        # Todo is created on demand. Its calendar, reminders and shopping
        # view are not needed until the user opens the workspace.
        self.todo_window = None
        self.todo_tab = None

        self.build_list_tab()
        self.apply_language()
        self.refresh_list()
        self.install_context_menus()
        # Open on the tab the user was last working in, then start remembering
        # every change to it. The remembered index is clamped: a build that has
        # more or fewer tabs than the number was saved in must still start.
        self.restore_last_tab()
        self.tabs.currentChanged.connect(self.on_tab_changed)

        # Announce a damaged data file once the window is on screen.
        if self.load_warnings:
            QtCore.QTimer.singleShot(0, self.report_load_warnings)
        # Once the window is laid out, make sure the whole tab strip fits.
        QtCore.QTimer.singleShot(0, self.ensure_tabs_visible)

    def _attach_about_icon(self):
        """Render and set the About button icon, off the start-up path."""
        self.about_btn.setIcon(load_about_icon())

    def _attach_window_icon(self):
        """Load and set the window icon, off the start-up path."""
        self.setWindowIcon(load_app_icon())

    def restore_last_tab(self):
        """Select the remembered tab (0 is always a valid fallback)."""
        try:
            index = int(self.settings.get("last_tab", 0))
        except (TypeError, ValueError):
            index = 0
        if 0 <= index < self.tabs.count():
            self.tabs.setCurrentIndex(index)

    def on_tab_changed(self, index):
        # Remember the tab, coalesced so switching around does not hammer the
        # settings file. Building the tab on first visit is handled elsewhere.
        self.settings["last_tab"] = int(index)
        self.save_settings_deferred()

    def closeEvent(self, event):
        # Store the window size and layout so the next start looks the same.
        try:
            # Flush the column widths first: they are normally written by a
            # 150 ms timer that a drag may still be waiting on, and closing the
            # window inside that window would lose the last layout the user
            # chose. An exception here must not stop the settings being saved.
            try:
                self.remember_column_widths()
            except Exception:
                pass
            geometry = self.saveGeometry().toBase64().data().decode("ascii")
            state = self.saveState().toBase64().data().decode("ascii")
            self.settings["window_geometry"] = {
                "geometry": geometry, "state": state}
            self.save_settings()
        except Exception:
            pass
        super().closeEvent(event)

    def t(self, key, fallback=None, **kwargs):
        """Look up a translation, optionally formatting {placeholders}."""
        table = translation_table()
        table = table.get(self.lang, table["en"])
        if key in table:
            text = table[key]
        elif fallback is not None:
            text = fallback
        else:
            text = key
        if kwargs:
            try:
                return text.format(**kwargs)
            except (KeyError, IndexError, ValueError):
                return text
        return text
    # ------------------------------------------------- audit integration
    # The audit tab and the rest of the application form one program: every
    # data change here is announced on the shared audit event bus, which both
    # records it in the timeline and drives the automatic audits.
    def audit_available(self):
        """True when the real AuditTab (not the placeholder) is loaded."""
        return self.tab_built("audit_tab") and hasattr(self.audit_tab,
                                                       "emit_audit_event")

    def notify_audit(self, event, **payload):
        """Announce a change to the audit subsystem (timeline + auto audits).

        This is the single entry point every mutation in the application uses,
        so the change history and the automatic audits stay complete without
        each action having to know about the audit internals.
        """
        if not self.audit_available():
            return 0
        try:
            # Record the change in the timeline first, then let the event bus
            # run whatever automatic audit the event implies.
            recorder = getattr(self.audit_tab, "_recording_hook", None)
            if callable(recorder):
                recorder()
            return self.audit_tab.emit_audit_event(event, **payload)
        except Exception:
            return 0

    def _announce_script_save(self, was_edit, entry):
        """Pick the right audit event for a just-saved script."""
        event = "module_updated" if was_edit else "module_installed"
        self.notify_audit(event, where=entry.get("name", ""),
                          detail=entry.get("category", ""))

    def open_audit_tab(self, key=None):
        """Bring the Audit tab to the front, optionally selecting a sub-tab."""
        if not self.audit_available():
            return
        self.tabs.setCurrentWidget(self.audit_tab)
        if key is not None:
            index = self.audit_tab._index_of_key(key)
            if index is not None:
                self.audit_tab.subtabs.setCurrentIndex(index)
        self.audit_tab.refresh()

    def audit_script(self, script=None, focus="audit_tab_modules"):
        """Audit a specific script (or the current selection) and show it."""
        if not self.audit_available():
            self.show_message(self.t("info"), self.t("audit_unavailable"))
            return
        script = script or self.selected_script()
        if script is not None:
            # Remember the selection so the report's "selection" section and
            # the audit operations act on exactly this script.
            self.audit_tab._audit_target_id = script.get("id")
            self.audit_tab.log_change(
                "modules", script.get("name", ""),
                self.t("audit_script_audited", "audited"),
                self.t("audit_tl_how_manual", "manual"))
        self.open_audit_tab(focus)

    def run_security_audit(self):
        """Jump straight to the graded security audit page."""
        self.open_audit_tab("audit_tab_premium")

    def export_audit(self, fmt="txt"):
        """Export the audit from the main window (used by menus/shortcuts)."""
        if not self.audit_available():
            return ""
        path = self.audit_tab.export_report(fmt)
        if path:
            self.show_message(self.t("info"),
                              self.t("audit_exported").format(path=path))
        return path

    def report_load_warnings(self):
        """Tell the user which data files could not be read, and why."""
        if not self.load_warnings:
            return
        detail = "\n".join(f"\u2022 {item}" for item in self.load_warnings)
        QtWidgets.QMessageBox.warning(
            self,
            self.t("warning"),
            f"{self.t('load_problem')}\n\n{detail}\n\n{self.t('load_problem_hint')}",
        )

    def show_message(self, title, message):
        """Single funnel for user notifications (easy to silence in tests)."""
        QtWidgets.QMessageBox.information(self, title, message)
    # ------------------------------------------------------- todo integration
    def resizeEvent(self, event):
        # Re-split the columns when the window (and with it the list) grows
        # or shrinks, otherwise the name column keeps its old pixel width.
        super().resizeEvent(event)
        tree = getattr(self, "script_tree", None)
        if tree is not None:
            # A programmatic resize must not overwrite what the user dragged.
            self._setting_column_widths = True
            try:
                self.apply_name_column_width(list(self.visible_columns()),
                                             self.show_row_numbers())
            finally:
                self._setting_column_widths = False

    def todo_available(self):
        # True when the real ToDo workspace (not the placeholder) is loaded.
        return self.todo_tab is not None and hasattr(self.todo_tab, "store")

    def open_todo_tab(self, view=None):
        # Open the ToDo popup window, optionally on a specific view. A reminder
        # that came due while the list was closed is fired on the way in,
        # because that is when the user is looking for it.
        if self.todo_window is None:
            try:
                from tabs.todo_tab import TodoWindow
                self.todo_window = TodoWindow(self)
            except Exception:
                # No window class (an older build, or a broken module): the
                # message below tells the user instead of failing silently.
                self.todo_window = None
        if self.todo_window is not None:
            self.todo_tab = self.todo_window.todo
            self.todo_window.restore_geometry(self.settings)
        if not self.todo_available():
            self.show_message(self.t("info"),
                              self.t("todo_unavailable",
                                     "The ToDo workspace is not available"))
            return
        if self.todo_window is not None:
            self.todo_window.open_view(view)
        else:
            # No window class (an older build): fall back to the tab, if any.
            index = self.tabs.indexOf(self.todo_tab)
            if index >= 0:
                self.tabs.setCurrentIndex(index)
            if view is not None and hasattr(self.todo_tab, "set_view"):
                self.todo_tab.set_view(view)
        try:
            self.todo_tab.check_reminders()
            self.todo_tab.refresh()
        except Exception:
            pass
        self.refresh_todo_badge()

    def toggle_todo_window(self, view=None):
        # The ToDo button: show the popup, or hide it when it is already up.
        self.refresh_todo_badge()
        if self.todo_window is None:
            self.open_todo_tab(view)
            return
        self.todo_window.toggle(view)

    def refresh_todo_badge(self):
        # Say how much is waiting on the ToDo button itself. The badge counts
        # what is due today and what is late, because those are what a person
        # acts on; a quiet day leaves the button plain rather than showing 0.
        if not hasattr(self, "todo_btn"):
            return 0
        label = "\u2611 " + self.t("todo", "ToDo")
        if not self.todo_available():
            self.todo_btn.setText(label)
            return 0
        try:
            buckets = self.todo_tab.store.today()
        except Exception:
            self.todo_btn.setText(label)
            return 0
        due_today = len(buckets.get("today", []))
        late = len(buckets.get("overdue", []))
        waiting = due_today + late
        if not waiting:
            self.todo_btn.setText(label)
            self.todo_btn.setStyleSheet("")
            return 0
        self.todo_btn.setText("%s  %d" % (label, waiting))
        colour = "#f7768e" if late else "#f7c66d"
        self.todo_btn.setStyleSheet("color: %s; font-weight: bold;" % colour)
        self.todo_btn.setToolTip(self.t(
            "todo_badge_hint",
            "{due} due today, {late} late (Ctrl+T)",
            due=due_today, late=late))
        return waiting

    def show_about(self):
        """Show the About dialog: version, data paths and storage counts."""
        AboutDialog(self).exec_()

    def install_context_menus(self):
        """Add consistent menus to tabs and views without a custom menu."""
        self.tabs.tabBar().setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.tabs.tabBar().customContextMenuRequested.connect(
            self.show_tab_context_menu)
        for view in self.findChildren(QtWidgets.QAbstractItemView):
            if view.contextMenuPolicy() == QtCore.Qt.CustomContextMenu:
                continue
            view.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
            view.customContextMenuRequested.connect(
                lambda point, view=view: self.show_view_context_menu(view, point))

    def _copy_view_selection(self, view):
        indexes = view.selectionModel().selectedIndexes()
        if not indexes:
            return
        rows = {}
        for index in indexes:
            rows.setdefault(index.row(), {})[index.column()] = str(
                index.data() or "")
        lines = []
        for row in sorted(rows):
            columns = rows[row]
            lines.append("\t".join(columns.get(column, "")
                                    for column in range(max(columns) + 1)))
        QtWidgets.QApplication.clipboard().setText("\n".join(lines))

    def _refresh_current_tab(self, widget=None):
        widget = widget or self.tabs.currentWidget()
        refresh = getattr(widget, "refresh", None)
        if callable(refresh):
            try:
                refresh()
                return
            except TypeError:
                pass
        refresh_list = getattr(widget, "refresh_list", None)
        if callable(refresh_list):
            refresh_list()

    def show_view_context_menu(self, view, point):
        """Show standard actions for a list, table or tree view."""
        menu = QtWidgets.QMenu(view)
        copy_action = menu.addAction(self.t("context_copy", "Copy selection"),
                                     lambda: self._copy_view_selection(view))
        copy_action.setEnabled(bool(view.selectionModel().selectedIndexes()))
        menu.addAction(self.t("context_select_all", "Select all"),
                       view.selectAll)
        clear_action = menu.addAction(
            self.t("context_clear_selection", "Clear selection"),
            view.clearSelection)
        clear_action.setEnabled(bool(view.selectionModel().selectedIndexes()))
        menu.addSeparator()
        menu.addAction(self.t("context_refresh", "Refresh"),
                       lambda: self._refresh_current_tab())
        menu.exec_(view.viewport().mapToGlobal(point))

    def show_tab_context_menu(self, point):
        """Navigate and refresh tabs from the tab strip."""
        bar = self.tabs.tabBar()
        index = bar.tabAt(point)
        if index < 0:
            return
        self.tabs.setCurrentIndex(index)
        menu = QtWidgets.QMenu(bar)
        menu.addAction(self.t("context_refresh", "Refresh"),
                       lambda: self._refresh_current_tab(self.tabs.widget(index)))
        menu.addAction(self.t("context_copy_tab", "Copy tab name"),
                       lambda: QtWidgets.QApplication.clipboard().setText(
                           self.tabs.tabText(index)))
        menu.addSeparator()
        previous = menu.addAction(self.t("context_previous_tab", "Previous tab"),
                                  lambda: self.tabs.setCurrentIndex(
                                      max(0, index - 1)))
        previous.setEnabled(index > 0)
        following = menu.addAction(self.t("context_next_tab", "Next tab"),
                                   lambda: self.tabs.setCurrentIndex(
                                       min(self.tabs.count() - 1, index + 1)))
        following.setEnabled(index < self.tabs.count() - 1)
        menu.addAction(self.t("about", "About ScriptDB"), self.show_about)
        menu.exec_(bar.mapToGlobal(point))

    def apply_language(self):
        T = translation_table()[self.lang]
        self.setWindowTitle(T["title"])
        self.title_label.setText(T["title"])
        # Show the restored filter and favourite state, not just the saved one:
        # the widgets are rebuilt from scratch on every start, so without this
        # the list would be filtered while the search box looked empty.
        if getattr(self, "filter_input", None) is not None:
            blocked = self.filter_input.blockSignals(True)
            self.filter_input.setText(self.filter_text)
            self.filter_input.blockSignals(blocked)
        if getattr(self, "filter_favorites_btn", None) is not None:
            self.filter_favorites_btn.setChecked(self.filter_favorites)
            self.filter_favorites_btn.setText(
                self.t("favorites_only") if self.filter_favorites
                else f"\u2606 {self.t('favorites_only')}")
        if getattr(self, "btn_sort_scripts", None) is not None:
            self.btn_sort_scripts.setText(
                "Sort Z-A" if not self.sort_ascending else "Sort A-Z")
        self.lang_btn.setText("\U0001f310 EN" if self.lang == "pl" else "\U0001f310 PL")
        # The tab labels are addressed by the widget they belong to, not by a
        # position: a hard-coded index silently mislabels every tab after the
        # one that was inserted, which is exactly what happened when the Vault
        # was added between Diff and DOCs.
        for widget, fallback, label_key in (
                (self.list_tab, "Scripts", "scripts"),
                (self._lazy_placeholders.get("apps_tab"), "Apps", "apps"),
                (self._lazy_placeholders.get("editor_tab"), "Editor", "editor"),
                (self._lazy_placeholders.get("analyzer_tab"), "Analyzer", "analyzer"),
                (self._lazy_placeholders.get("audit_tab"), "Audit", "audit"),
                (self._lazy_placeholders.get("debugger_tab"), "Debugger", "debugger"),
                (self._lazy_placeholders.get("sandbox_tab"), "Sandbox", "sandbox"),
                (self._lazy_placeholders.get("diff_tab"), "Diff", "diff"),
                (self._lazy_placeholders.get("vault_tab"), "Vault", "vault"),
                (self._lazy_placeholders.get("security_tab"), "Security", "security"),
                (self._lazy_placeholders.get("structure_tab"), "Structure", "structure"),
                (self._lazy_placeholders.get("sqlite_tab"), "SQLite", "sqlite"),
                (self._lazy_placeholders.get("docs_tab"), "DOCs", "docs"),
                (self._lazy_placeholders.get("reports_tab"), "Reports", "reports"),
                (self._lazy_placeholders.get("search_tab"), "Search", "search")):
            index = self.tabs.indexOf(widget)
            if index >= 0:
                self.tabs.setTabText(index, T.get(label_key, fallback))
        self.stats_btn.setText(T["statistics"])
        self.theme_btn.setText(app_theme.theme_label(self.current_theme())
                               if app_theme is not None else "")
        for key, action in getattr(self, "_theme_actions", {}).items():
            action.setText(app_theme.theme_label(key))
            action.setChecked(key == self.current_theme())
        self.todo_btn.setText("\u2611 " + T.get("todo", "ToDo"))
        # Retranslate only the tabs that are already built: touching an
        # attribute here would force every lazy tab into existence on start.
        for name in self.LAZY_TABS:
            if self.tab_built(name):
                tab = self.__dict__[name]
                retranslate = getattr(tab, "retranslate", None)
                if callable(retranslate):
                    retranslate(T)
        if hasattr(self, "todo_tab") and hasattr(self.todo_tab, "retranslate"):
            self.todo_tab.retranslate(T)
        # The ToDo workspace is a window of its own, so its chrome is renamed
        # here rather than through the tab strip.
        if getattr(self, "todo_window", None) is not None:
            self.todo_window.retranslate()
            self.todo_window.setWindowTitle("ScriptDB - " + T.get("todo", "ToDo"))
        self.count_label.setText(f"{T['total_scripts']} {len(self.scripts)}")
        self.btn_preview_script.setText(T["preview"])
        self.btn_duplicate_script.setText(T["duplicate"])
        self.btn_history_script.setText(T["history"])
        self.btn_favorite_script.setText("\u2605 " + T["favorites"])
        self.btn_run_script.setText(T.get("run_menu", "Run..."))
        self.act_run_window.setText(T.get("run_menu_window", "In the runner window"))
        self.act_run_console.setText(T.get("run_menu_console", "In a new console window"))
        self.btn_import.setText(T.get("import_menu", "Import..."))
        self.act_import_json.setText(T.get("import_menu_json", "Database JSON (.json)"))
        self.act_import_file.setText(T.get("import_menu_file", "Source file(s)..."))
        self.act_import_folder.setText(T.get("import_menu_folder", "Whole folder..."))
        self.btn_export.setText(T.get("export_menu", "Export..."))
        self.act_export_json.setText(T.get("export_menu_json", "Database JSON (.json)"))
        self.act_export_file.setText(T.get("export_menu_file", "Script file..."))
        self.act_export_report.setText(T.get("export_menu_report", "Report (.md)"))
        self.filter_favorites_btn.setText(T["favorites_only"] if self.filter_favorites else "\u2606 " + T["favorites_only"])
        self.btn_sort_scripts.setText("Sort Z-A" if not self.sort_ascending else "Sort A-Z")
        self.update_list_columns()
        # A longer tab name (or a wider font in one of the two languages) can
        # push the last tabs behind the scroll arrows, so the window is grown
        # just enough to keep the whole strip visible. Deferred, because the
        # new labels are only measured after the layout has caught up.
        QtCore.QTimer.singleShot(0, self.ensure_tabs_visible)

    def tabs_required_width(self):
        """Pixel width the whole tab strip needs to be shown at once."""
        bar = self.tabs.tabBar()
        if bar is None:
            return 0
        margins = self.tabs.contentsMargins()
        return (sum(bar.tabRect(i).width() for i in range(bar.count()))
                + margins.left() + margins.right() + 8)

    def ensure_tabs_visible(self):
        """Grow the window so no tab is hidden behind the strip's arrows."""
        bar = self.tabs.tabBar()
        if bar is None:
            return False
        needed = self.tabs_required_width()
        if not needed:
            return False
        # Room for the tab strip plus the window frame and a small margin.
        # QWidget has no frameWidth(), so the decoration is a fixed estimate.
        required = needed + 32
        available = self.width()
        if available >= required:
            return False
        screen = QtWidgets.QApplication.desktop().availableGeometry() \
            if hasattr(QtWidgets.QApplication, "desktop") else None
        target = required
        if screen is not None and target > screen.width():
            target = screen.width()
        if target > available:
            self.resize(target, self.height())
        return True

    def toggle_language(self):
        self.set_language("pl" if self.lang == "en" else "en")

    def set_language(self, lang, save=True):
        """Switch to `lang` ("en" or "pl") and remember the choice."""
        if lang not in LANGUAGES:
            return False
        self.lang = lang
        self.apply_language()
        if save:
            self.set_setting("lang", lang, refresh=False)
        return True

    # --------------------------------------------------------------- theme
    def current_theme(self):
        # The theme in force, falling back to the default.
        if app_theme is None:
            return ""
        return self.setting("theme", app_theme.DEFAULT_THEME)

    def set_theme(self, name, save=True):
        # Switch the whole window to another palette and remember it.
        #
        # The sheet is re-applied to the *application*, not to this window: the
        # dialogs, tooltips and the file explorer are separate top-level windows
        # and would keep the old palette otherwise.  The view then has to be
        # rebuilt because several tabs cache a colour they read from the palette
        # when they were first built.
        if app_theme is None or name not in app_theme.THEMES:
            return False
        app = QtWidgets.QApplication.instance()
        compact = bool(self.setting("compact_ui", False))
        if app is not None:
            apply_application_theme(app, name, compact=compact)
        if save:
            self.set_setting("theme", name, refresh=False)
        for key, action in getattr(self, "_theme_actions", {}).items():
            action.setChecked(key == name)
        self.apply_language()
        self.refresh_list()
        return True

    def update_list_columns(self):
        tree = self.script_tree
        headers = [self.t("name"), self.t("category_col"), self.t("tags_col"), self.t("date")]
        tree.setHeaderLabels(headers)
    def build_list_tab(self):
        layout = QtWidgets.QVBoxLayout(self.list_tab)

        toolbar = QtWidgets.QHBoxLayout()
        self.btn_add_script = QtWidgets.QPushButton("Add Script")
        self.btn_templates = QtWidgets.QPushButton("Templates")
        # A single "Run..." button whose menu offers the two ways to execute.
        self.btn_run_script = QtWidgets.QToolButton()
        self.btn_run_script.setText("Run...")
        self.btn_run_script.setPopupMode(QtWidgets.QToolButton.MenuButtonPopup)
        self.run_menu = QtWidgets.QMenu(self.btn_run_script)
        self.act_run_window = self.run_menu.addAction("In the runner window")
        self.act_run_console = self.run_menu.addAction("In a new console window")
        self.act_run_window.triggered.connect(self.run_selected_script)
        self.act_run_console.triggered.connect(self.run_selected_script_console)
        self.btn_run_script.setMenu(self.run_menu)
        # The button itself opens the menu (the arrow toggles it as well).
        self.btn_run_script.clicked.connect(self.show_run_menu)
        self.btn_preview_script = QtWidgets.QPushButton("Preview")
        self.btn_duplicate_script = QtWidgets.QPushButton("Duplicate")
        self.btn_history_script = QtWidgets.QPushButton("History")
        self.btn_favorite_script = QtWidgets.QPushButton("\u2605 Favorite")
        self.btn_sort_scripts = QtWidgets.QPushButton("Sort A-Z")
        self.btn_delete_script = QtWidgets.QPushButton("Delete")
        # A single "Import..." button whose menu holds every import source.
        self.btn_import = QtWidgets.QToolButton()
        self.btn_import.setText("Import...")
        self.btn_import.setPopupMode(QtWidgets.QToolButton.MenuButtonPopup)
        self.import_menu = QtWidgets.QMenu(self.btn_import)
        self.act_import_json = self.import_menu.addAction("Database JSON (.json)")
        self.act_import_file = self.import_menu.addAction("Source file(s)...")
        self.act_import_folder = self.import_menu.addAction("Whole folder...")
        self.act_import_json.triggered.connect(self.import_json)
        self.act_import_file.triggered.connect(self.import_script_file)
        self.act_import_folder.triggered.connect(self.import_script_folder)
        self.btn_import.setMenu(self.import_menu)
        # The button itself opens the menu (the arrow toggles it as well).
        self.btn_import.clicked.connect(self.show_import_menu)

        # A single "Export..." button whose menu holds every export target.
        self.btn_export = QtWidgets.QToolButton()
        self.btn_export.setText("Export...")
        self.btn_export.setPopupMode(QtWidgets.QToolButton.MenuButtonPopup)
        self.export_menu = QtWidgets.QMenu(self.btn_export)
        self.act_export_json = self.export_menu.addAction("Database JSON (.json)")
        self.act_export_file = self.export_menu.addAction("Script file...")
        self.act_export_report = self.export_menu.addAction("Report (.md)")
        self.act_export_json.triggered.connect(self.export_json)
        self.act_export_file.triggered.connect(self.export_script_file)
        self.act_export_report.triggered.connect(self.export_report)
        self.btn_export.setMenu(self.export_menu)
        # The button itself opens the menu (the arrow toggles it as well).
        self.btn_export.clicked.connect(self.show_export_menu)

        self.btn_add_script.clicked.connect(self.add_new_script)
        self.btn_templates.clicked.connect(self.show_templates)
        self.btn_preview_script.clicked.connect(self.preview_selected_script)
        self.btn_duplicate_script.clicked.connect(self.duplicate_selected_script)
        self.btn_history_script.clicked.connect(self.show_script_history)
        self.btn_favorite_script.clicked.connect(self.toggle_favorite_selected)
        self.btn_sort_scripts.clicked.connect(self.sort_scripts)
        self.btn_delete_script.clicked.connect(self.delete_selected_script)
        toolbar.addWidget(self.btn_add_script)
        toolbar.addWidget(self.btn_templates)
        toolbar.addWidget(self.btn_run_script)
        toolbar.addWidget(self.btn_preview_script)
        toolbar.addWidget(self.btn_duplicate_script)
        toolbar.addWidget(self.btn_history_script)
        toolbar.addWidget(self.btn_favorite_script)
        toolbar.addWidget(self.btn_sort_scripts)
        toolbar.addWidget(self.btn_delete_script)
        toolbar.addStretch()
        toolbar.addWidget(self.btn_import)
        toolbar.addWidget(self.btn_export)
        layout.addLayout(toolbar)
        filter_row = QtWidgets.QHBoxLayout()
        self.filter_input = QtWidgets.QLineEdit()
        self.filter_input.setPlaceholderText("Filter by name, tag, description... (Ctrl+K)")
        self.filter_input.textChanged.connect(self.on_filter_changed)
        self.filter_favorites_btn = QtWidgets.QPushButton("\u2606 Favorites only")
        self.filter_favorites_btn.setCheckable(True)
        self.filter_favorites_btn.toggled.connect(self.on_favorites_filter_toggled)
        filter_row.addWidget(self.filter_input)
        filter_row.addWidget(self.filter_favorites_btn)
        layout.addLayout(filter_row)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        form_widget = QtWidgets.QWidget()
        form_layout = QtWidgets.QVBoxLayout(form_widget)

        self.script_tree = QtWidgets.QTreeWidget()
        self.script_tree.setAlternatingRowColors(True)
        self.script_tree.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.script_tree.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.script_tree.setColumnCount(4)
        self.script_tree.setHeaderLabels(["Name", "Category", "Tags", "Date"])
        self.script_tree.itemDoubleClicked.connect(self.edit_selected_script)
        self.script_tree.itemSelectionChanged.connect(self.on_list_selection_changed)
        self.script_tree.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.script_tree.customContextMenuRequested.connect(self.show_context_menu)
        # Remember the column proportions whenever the user drags a border.
        self.script_tree.header().sectionResized.connect(
            lambda _section, _old, _new: self.remember_column_widths())

        form_layout.addWidget(self.script_tree)
        splitter.addWidget(form_widget)

        self.script_form = ScriptForm(self, self.save_script, self.clear_form, self.copy_form_code)
        self.script_form.hide()
        self.script_preview = ScriptPreview(self)
        splitter.addWidget(self.script_preview)
        splitter.setSizes([430, 670])
        layout.addWidget(splitter)

    # ---------------------------------------------------------------- filtering
    def visible_scripts(self):
        """Apply the quick text filter and the favorites filter to the list."""
        query = self.filter_text.strip().lower()
        result = []
        for script in self.scripts:
            if is_separator(script):
                continue
            if self.filter_favorites and not script.get("favorite"):
                continue
            if query:
                haystack = " ".join([
                    script.get("name", ""), script.get("category", ""),
                    script.get("tags", ""), script.get("desc", ""),
                ]).lower()
                if query not in haystack:
                    continue
            result.append(script)
        return result
    def focus_filter(self):
        self.tabs.setCurrentIndex(0)
        self.filter_input.setFocus()
        self.filter_input.selectAll()

    def on_filter_changed(self, text):
        self.filter_text = text
        # Remember the text, so the list opens on the same filtered view.
        self.settings["filter_text"] = text
        self.save_settings_deferred()
        self.refresh_list()

    def on_favorites_filter_toggled(self, checked):
        self.filter_favorites = bool(checked)
        self.filter_favorites_btn.setText(self.t("favorites_only") if checked else f"\u2606 {self.t('favorites_only')}")
        self.settings["filter_favorites"] = self.filter_favorites
        self.save_settings_deferred()
        self.refresh_list()

    def toggle_favorite_selected(self):
        scripts = self.selected_scripts()
        if not scripts:
            QtWidgets.QMessageBox.information(self, self.t("info"), self.t("select_script"))
            return
        # If every selected script is already a favorite, clear them all;
        # otherwise mark them all as favorites.
        make_favorite = not all(s.get("favorite") for s in scripts)
        for script in scripts:
            script["favorite"] = make_favorite
        save_data(DATA_FILE, self.scripts)
        self.refresh_list()

    def sort_scripts(self):
        self.sort_ascending = not self.sort_ascending
        reverse = not self.sort_ascending
        # Sort only the scripts; separators keep their place so the manual
        # layout the user built is not shuffled along with the names.
        separators = self.separator_rows()
        ordered = sorted(self.scripts_only(),
                         key=lambda s: s.get("name", "").lower(),
                         reverse=reverse)
        self.scripts = ordered + separators
        self.save_data_now()
        # Remember the direction, so the list opens the way the user sorted it.
        self.settings["sort_ascending"] = self.sort_ascending
        self.save_settings_deferred()
        self.refresh_list()
        self.btn_sort_scripts.setText("Sort Z-A" if reverse else "Sort A-Z")

    def selected_scripts(self):
        """Return every selected script entry, in list order (deduplicated)."""
        ids = []
        for item in self.script_tree.selectedItems():
            script_id = item.data(0, QtCore.Qt.UserRole)
            if script_id is not None:
                ids.append(script_id)
        by_id = {s.get("id"): s for s in self.scripts if not is_separator(s)}
        return [by_id[i] for i in ids if i in by_id]

    def selected_script(self):
        """Return the primary selected script entry (first selection), or None."""
        selected = self.script_tree.selectedItems()
        if not selected:
            return None
        script_id = selected[0].data(0, QtCore.Qt.UserRole)
        return self.find_script(script_id)

    def find_script(self, script_id):
        """Look a script up by id (separators excluded)."""
        return next((s for s in self.scripts
                     if s.get("id") == script_id and not is_separator(s)), None)

    def open_in_diff(self):
        """Send the selected script to the Diff tab and show it there."""
        script = self.selected_script()
        if script is None:
            self.show_message(self.t("info"), self.t("select_script"))
            return
        self.diff_tab._right = self.diff_tab._entry_to_source(script)
        self.diff_tab.recompute()
        self.tabs.setCurrentWidget(self.diff_tab)

    def show_context_menu(self, point):
        index = self.script_tree.indexAt(point)
        if not index.isValid():
            # Right-click on empty space: offer the list customization actions.
            self.show_empty_area_menu(point)
            return
        item = self.script_tree.itemAt(point)
        if item is not None and item.data(0, QtCore.Qt.UserRole + 1) == SEPARATOR_KIND:
            self.show_separator_menu(point, item)
            return
        menu = QtWidgets.QMenu(self)
        menu.addAction(self.t("edit"), self.edit_selected_script)
        # "Run..." mirrors the toolbar button: a submenu with both ways to run.
        run_menu = menu.addMenu(self.t("run_menu"))
        run_menu.addAction(self.t("run_menu_window"), self.run_selected_script)
        run_menu.addAction(self.t("run_menu_console"), self.run_selected_script_console)
        menu.addAction(self.t("preview"), self.preview_selected_script)
        menu.addAction(self.t("toggle_favorite"), self.toggle_favorite_selected)
        menu.addAction(self.t("duplicate"), self.duplicate_selected_script)
        menu.addAction(self.t("history"), self.show_script_history)
        menu.addAction(self.t("diff"), self.open_in_diff)
        # --- audit integration: audit the script / jump to the audit tab ---
        menu.addSeparator()
        menu.addAction(self.t("audit_this_script", "Audit this script"),
                       lambda: self.audit_script(self.selected_script()))
        menu.addAction(self.t("audit_open_tab", "Open Audit tab"),
                       lambda: self.open_audit_tab())
        menu.addAction(self.t("audit_run_security", "Security audit"),
                       self.run_security_audit)
        menu.addAction(self.t("audit_export_menu", "Export audit report"),
                       lambda: self.export_audit("txt"))
        menu.addAction(self.t("export_file"), self.export_script_file)
        menu.addSeparator()
        menu.addAction(self.t("copy"), self.copy_form_code)
        menu.addAction(self.t("copy") + " (" + self.t("name") + " + " + self.t("code") + ")", self.copy_script_as_text)
        menu.addSeparator()
        menu.addAction(self.t("delete"), self.delete_selected_script)
        menu.exec_(self.script_tree.mapToGlobal(point))

    # --------------------------------------------------- list customization
    def show_empty_area_menu(self, point):
        """Context menu for the empty area below the last row."""
        menu = QtWidgets.QMenu(self)
        menu.addAction(self.t("menu_add_script"), self.add_new_script)
        menu.addSeparator()

        separators = menu.addMenu(self.t("menu_separators"))
        separators.addAction(self.t("sep_add_top"), lambda: self.add_separator(0))
        separators.addAction(self.t("sep_add_bottom"), lambda: self.add_separator(len(self.scripts_only())))
        separators.addAction(self.t("sep_add_before_first"), lambda: self.add_separator(0))
        edit_separator = separators.addMenu(self.t("sep_edit"))
        rows = self.separator_rows()
        if rows:
            for separator in rows:
                label = separator.get("name", "") or self.t("sep_unnamed")
                edit_separator.addAction(label, lambda sid=separator.get("id"): self.edit_separator(sid))
        else:
            edit_separator.setEnabled(False)
        remove_separator = separators.addMenu(self.t("sep_remove"))
        if rows:
            for separator in rows:
                label = separator.get("name", "") or self.t("sep_unnamed")
                remove_separator.addAction(label, lambda sid=separator.get("id"): self.remove_separator(sid))
        else:
            remove_separator.setEnabled(False)
        separators.addSeparator()
        toggle = separators.addAction(self.t("sep_hide_all"), self.toggle_separators)
        toggle.setCheckable(True)
        toggle.setChecked(not self.setting("separators_enabled", True))
        clear = separators.addAction(self.t("sep_clear_all"), self.clear_separators)
        clear.setEnabled(bool(rows))
        menu.addSeparator()
        columns = menu.addMenu(self.t("menu_columns"))
        for key, title in (("favorite", "favorite_col"), ("date", "date"),
                           ("tags", "tags_col"), ("category", "category_col"),
                           ("name", "name")):
            action = columns.addAction(self.t(title), lambda k=key: self.toggle_column(k))
            action.setCheckable(True)
            action.setChecked(bool((self.setting("columns", {}) or {}).get(key, True)))
            if key == "name":
                action.setEnabled(False)
        columns.addSeparator()
        row_numbers = columns.addAction(self.t("col_row_numbers"), self.toggle_row_numbers)
        row_numbers.setCheckable(True)
        row_numbers.setChecked(self.show_row_numbers())
        columns.addAction(self.t("col_reset"), self.reset_columns)
        menu.addSeparator()

        group = menu.addAction(self.t("group_by_category"), self.toggle_grouping)
        group.setCheckable(True)
        group.setChecked(bool(self.setting("group_by_category", False)))
        zebra = menu.addAction(self.t("zebra_stripes"), self.toggle_zebra)
        zebra.setCheckable(True)
        zebra.setChecked(bool(self.setting("zebra_stripes", True)))
        bold = menu.addAction(self.t("bold_separators"), self.toggle_bold_separators)
        bold.setCheckable(True)
        bold.setChecked(bool(self.setting("bold_separators", True)))
        menu.addAction(self.t("sep_default_label"), self.set_default_separator_label)
        menu.addSeparator()

        expand = menu.addAction(self.t("list_expand_all"), self.script_tree.expandAll)
        collapse = menu.addAction(self.t("list_collapse_all"), self.script_tree.collapseAll)
        if not self.setting("group_by_category", False):
            expand.setEnabled(False)
            collapse.setEnabled(False)
        menu.addAction(self.t("list_reset_view"), self.reset_list_customization)
        menu.addSeparator()
        menu.addAction(self.t("statistics"), self.show_statistics)
        menu.addAction(self.t("report"), self.export_report)
        menu.exec_(self.script_tree.viewport().mapToGlobal(point))

    def show_separator_menu(self, point, item):
        """Context menu shown when a separator row itself is right-clicked."""
        separator_id = item.data(0, QtCore.Qt.UserRole)
        menu = QtWidgets.QMenu(self)
        menu.addAction(self.t("sep_rename"), lambda: self.edit_separator(separator_id))
        menu.addAction(self.t("sep_move_up"), lambda: self.move_separator(separator_id, -1))
        menu.addAction(self.t("sep_move_down"), lambda: self.move_separator(separator_id, 1))
        menu.addSeparator()
        menu.addAction(self.t("sep_remove"), lambda: self.remove_separator(separator_id))
        menu.exec_(self.script_tree.viewport().mapToGlobal(point))
    def add_separator(self, position):
        """Insert a separator at the given block position, asking for its label."""
        label, accepted = QtWidgets.QInputDialog.getText(
            self, self.t("sep_add"), self.t("sep_prompt"),
            QtWidgets.QLineEdit.Normal, self.setting("empty_separator_label", ""),
        )
        if not accepted:
            return
        separator = self.insert_separator_at(position, label)
        if not self.setting("separators_enabled", True):
            self.set_setting("separators_enabled", True, refresh=False)
        self.save_data_now()
        self.refresh_list()
        self.script_tree.scrollToItem(
            next((self.script_tree.topLevelItem(i)
                  for i in range(self.script_tree.topLevelItemCount())
                  if self.script_tree.topLevelItem(i).data(0, QtCore.Qt.UserRole) == separator["id"]),
                 None) or self.script_tree.topLevelItem(0)
        )

    def edit_separator(self, separator_id):
        separator = next((s for s in self.scripts if is_separator(s) and s.get("id") == separator_id), None)
        if separator is None:
            return
        label, accepted = QtWidgets.QInputDialog.getText(
            self, self.t("sep_rename"), self.t("sep_prompt"),
            QtWidgets.QLineEdit.Normal, separator.get("name", ""),
        )
        if not accepted:
            return
        separator["name"] = label.strip()
        self.save_data_now()
        self.refresh_list()

    def remove_separator(self, separator_id):
        self.scripts = [s for s in self.scripts
                        if not (is_separator(s) and s.get("id") == separator_id)]
        self.save_data_now()
        self.refresh_list()

    def move_separator(self, separator_id, direction):
        """Move a separator one script-block boundary up or down."""
        separators = {s.get('id'): s for s in self.separator_rows()}
        if separator_id not in separators:
            return

        labels = {}          # boundary number -> [(id, name), ...]
        scripts = []
        for entry in self.scripts:
            if is_separator(entry):
                labels.setdefault(len(scripts), []).append(entry)
            else:
                scripts.append(entry)

        # Find the boundary this label currently sits on.
        current = None
        for boundary, group in labels.items():
            for entry in group:
                if entry.get('id') == separator_id:
                    current = boundary
        if current is None:
            return
        moving = next(e for e in labels[current] if e.get('id') == separator_id)
        target = current + (-1 if direction < 0 else 1)
        if target < 0 or target > len(scripts):
            return

        labels[current].remove(moving)
        if not labels[current]:
            del labels[current]
        labels.setdefault(target, []).append(moving)

        rebuilt = []
        for boundary in range(len(scripts) + 1):
            rebuilt.extend(labels.get(boundary, []))
            if boundary < len(scripts):
                rebuilt.append(scripts[boundary])
        self.scripts = rebuilt
        self.save_data_now()
        self.refresh_list()
    def toggle_separators(self):
        self.set_setting("separators_enabled", not self.setting("separators_enabled", True))

    def clear_separators(self):
        rows = self.separator_rows()
        if not rows:
            return
        if QtWidgets.QMessageBox.question(
            self, self.t("confirm"),
            self.t("sep_confirm_clear", count=len(rows)),
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
        ) != QtWidgets.QMessageBox.Yes:
            return
        self.scripts = [s for s in self.scripts if not is_separator(s)]
        self.save_data_now()
        self.refresh_list()

    def toggle_column(self, key):
        columns = dict(self.setting("columns", {}) or {})
        columns[key] = not columns.get(key, True)
        self.set_setting("columns", columns)

    def toggle_row_numbers(self):
        self.set_setting("show_row_numbers", not self.show_row_numbers())

    def reset_columns(self):
        self.settings["columns"] = dict(DEFAULT_SETTINGS["columns"])
        self.set_setting("show_row_numbers", False, refresh=False)
        self.settings.pop("column_width_shares", None)
        self.save_settings()
        self.refresh_list()

    def toggle_grouping(self):
        self.set_setting("group_by_category", not self.setting("group_by_category", False))

    def toggle_zebra(self):
        self.set_setting("zebra_stripes", not self.setting("zebra_stripes", True))

    def toggle_bold_separators(self):
        self.set_setting("bold_separators", not self.setting("bold_separators", True))

    def set_default_separator_label(self):
        label, accepted = QtWidgets.QInputDialog.getText(
            self, self.t("sep_default_label"), self.t("sep_prompt"),
            QtWidgets.QLineEdit.Normal, self.setting("empty_separator_label", ""),
        )
        if accepted:
            self.set_setting("empty_separator_label", label.strip(), refresh=False)

    def reset_list_customization(self):
        """Restore the default view, leaving the user's separators in place."""
        self.settings["columns"] = dict(DEFAULT_SETTINGS["columns"])
        for key in ("group_by_category", "separators_enabled", "zebra_stripes",
                    "show_row_numbers", "bold_separators"):
            self.settings[key] = DEFAULT_SETTINGS[key]
        self.save_settings()
        self.refresh_list()

    def save_data_now(self):
        """Persist the script list, reporting a failure instead of crashing."""
        try:
            save_data(DATA_FILE, self.scripts)
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, self.t("export_error"), str(exc))
    def copy_script_as_text(self, item=None):
        """Copy a script (header + code) to the clipboard, not just the code."""
        if isinstance(item, QtWidgets.QTreeWidgetItem):
            script_id = item.data(0, QtCore.Qt.UserRole)
        else:
            script = self.selected_script()
            script_id = script.get("id") if script else None
        script = next((s for s in self.scripts if s.get("id") == script_id), None)
        if not script:
            return
        block = (
            f"# {script.get('name', '')}\n"
            f"# Category: {script.get('category', '')}\n"
            f"# Tags: {script.get('tags', '')}\n"
            f"# {script.get('desc', '')}\n\n"
            f"{script.get('code', '')}\n"
        )
        QtWidgets.QApplication.clipboard().setText(block)
        self.show_message(self.t("info"), self.t("copied_script"))

    def add_new_script(self):
        """Open a blank script editor dialog."""
        self.clear_form()
        self._open_script_editor_dialog()

    def _open_script_editor_dialog(self, entry=None):
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle(self.t("edit", "Edit script"))
        dialog.resize(720, 620)
        form = ScriptForm(dialog, self.save_script, self.clear_form,
                          self.copy_form_code)
        self.script_form = form
        self._script_editor_dialog = dialog
        if entry is not None:
            self.editing_id = entry.get("id")
            form.load_entry(entry)
        form.setEnabled(True)
        layout = QtWidgets.QVBoxLayout(dialog)
        layout.addWidget(form)
        form.script_name.setFocus()
        dialog.finished.connect(lambda: setattr(self, "_script_editor_dialog", None))
        dialog.show()

    def clear_form(self):
        self.editing_id = None
        self.script_form.clear_fields()
        self.script_form.setEnabled(True)

    def save_script(self):
        data = self.script_form.get_entry_data()
        name = data["name"]
        code = data["code"]
        if not name or not code:
            QtWidgets.QMessageBox.warning(self, self.t("warning"), self.t("fill_required"))
            return

        entry = {
            "id": self.editing_id or str(dt.datetime.now().timestamp()),
            "name": name,
            "category": data["category"],
            "tags": data["tags"],
            "desc": data["desc"],
            "code": code,
            "date": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "favorite": bool(self.script_form.favorite_btn.isChecked()),
            "history": [],
        }

        was_edit = bool(self.editing_id)
        if self.editing_id:
            for index, existing in enumerate(self.scripts):
                if existing.get("id") != self.editing_id:
                    continue
                history = list(existing.get("history", []))
                # Only keep a revision when something actually changed.
                if script_to_dict(existing) != script_to_dict(entry):
                    history.append(script_to_dict(existing))
                entry["history"] = history[-25:]
                if len(history) > 25:
                    entry["history_truncated"] = True
                entry["favorite"] = entry["favorite"] or bool(existing.get("favorite"))
                entry["date_created"] = existing.get("date_created", existing.get("date", entry["date"]))
                self.scripts[index] = entry
                break
        else:
            entry["date_created"] = entry["date"]
            self.scripts.append(entry)
        try:
            save_data(DATA_FILE, self.scripts)
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, self.t("export_error"), str(exc))
            return
        self.refresh_list()
        # Announce the save on the shared audit bus so the timeline records
        # it and the matching automatic audit runs.
        self._announce_script_save(was_edit, entry)
        self.clear_form()
        self.show_message(self.t("information"), self.t("saved"))
        dialog = getattr(self, "_script_editor_dialog", None)
        if dialog is not None:
            dialog.close()
    # ------------------------------------------------- list customization
    def setting(self, key, default=None):
        """Read one saved preference, falling back to its default."""
        return self.settings.get(key, DEFAULT_SETTINGS.get(key, default))

    def save_settings(self):
        """Persist the UI preferences; failures must not break the session."""
        try:
            save_settings(SETTINGS_FILE, self.settings)
        except Exception as exc:
            self.show_message(self.t("export_error"), str(exc))

    def save_settings_deferred(self):
        """Persist the preferences once, after the current burst of changes.

        The column handler fires for every section while the list is laid out,
        and each call used to rewrite settings_db.json. Coalescing them keeps
        start-up off the disk.
        """
        if getattr(self, "_settings_save_timer", None) is None:
            self._settings_save_timer = QtCore.QTimer(self)
            self._settings_save_timer.setSingleShot(True)
            self._settings_save_timer.setInterval(150)
            self._settings_save_timer.timeout.connect(self.save_settings)
        self._settings_save_timer.start()

    def set_setting(self, key, value, refresh=True):
        """Write one preference, persist it and rebuild the list."""
        self.settings[key] = value
        self.save_settings()
        if refresh:
            self.refresh_list()

    def separator_rows(self):
        """Every separator entry currently stored in the list, in order."""
        return [entry for entry in self.scripts if is_separator(entry)]

    def scripts_only(self):
        """Only the real script entries (separators filtered out)."""
        return [entry for entry in self.scripts if not is_separator(entry)]

    def show_row_numbers(self):
        return bool(self.setting("show_row_numbers", False))

    def list_row_values(self, script):
        """Build the cell texts for one script, in the fixed column order."""
        return {
            "name": script.get("name", ""),
            "category": script.get("category", ""),
            "tags": script.get("tags", ""),
            "date": script.get("date", ""),
            "favorite": "\u2605" if script.get("favorite") else "",
        }

    def visible_columns(self):
        """Column keys the user left enabled, always keeping the name column."""
        columns = self.setting("columns", {}) or {}
        active = [key for key in LIST_COLUMNS if columns.get(key, True)]
        if "name" not in active:
            active.insert(0, "name")
        return active

    def _make_list_item(self, script):
        """One tree row for a script, honouring the visible columns."""
        values = self.list_row_values(script)
        cells = [values[key] for key in self.visible_columns()]
        if self.show_row_numbers():
            cells.insert(0, str(self.script_tree.topLevelItemCount() + 1))
        item = QtWidgets.QTreeWidgetItem(cells)
        item.setData(0, QtCore.Qt.UserRole, script.get("id"))
        if script.get("favorite"):
            item.setForeground(0, QtGui.QColor("#f7c66d"))
        return item
    def _make_separator_item(self, separator):
        """A full-width, non-selectable row carrying a separator label."""
        label = separator.get("name", "") or self.t("sep_unnamed")
        cells = [label] + [""] * (len(self.visible_columns()) - 1)
        if self.show_row_numbers():
            cells.insert(0, "")
        item = QtWidgets.QTreeWidgetItem(cells)
        item.setData(0, QtCore.Qt.UserRole, separator.get("id"))
        item.setData(0, QtCore.Qt.UserRole + 1, SEPARATOR_KIND)
        font = item.font(0)
        font.setBold(bool(self.setting("bold_separators", True)))
        item.setFont(0, font)
        item.setForeground(0, QtGui.QColor("#8ab4f8"))
        item.setBackground(0, QtGui.QColor("#2a2d2e"))
        # Separators are labels, not scripts: keep them out of the selection.
        item.setFlags(QtCore.Qt.ItemIsEnabled)
        return item

    def apply_list_headers(self):
        """Column headers matching the visible columns and language."""
        labels = {
            "name": self.t("name"),
            "category": self.t("category_col"),
            "tags": self.t("tags_col"),
            "date": self.t("date"),
            "favorite": self.t("favorite_col"),
        }
        columns = list(self.visible_columns())
        headers = [labels[key] for key in columns]
        row_numbers = self.show_row_numbers()
        if row_numbers:
            headers.insert(0, "#")
        self.script_tree.setColumnCount(len(headers))
        self.script_tree.setHeaderLabels(headers)
        self.apply_name_column_width(columns, row_numbers)
        # From here on a resize really is the user changing the layout, so it
        # may be stored.
        self._column_layout_ready = True

    # Share of the list width reserved for the script name column.
    NAME_COLUMN_SHARE = 0.4

    def column_width_shares(self, columns):
        """Column widths as fractions of the list width, from the saved values.

        A user who drags a column border gets those proportions back on the
        next start; until then the defaults apply, with the name column taking
        NAME_COLUMN_SHARE and the rest splitting what is left.

        Proportions rather than pixels are stored on purpose: the same layout
        has to look right in a 960 px window and in a 2560 px one, and on a
        screen with a different scaling. A width that no longer adds up to a
        full list is ignored, so a stale file from a build with fewer columns
        cannot squeeze the header.
        """
        total = max(1, len(columns))
        default_name = self.NAME_COLUMN_SHARE
        saved = self.setting("column_width_shares", None)
        if isinstance(saved, dict) and len(saved) == total:
            shares = []
            for key in columns:
                value = saved.get(key)
                shares.append(float(value) if isinstance(value, (int, float))
                              and value > 0 else None)
            if all(value is not None for value in shares):
                return shares
        shares = []
        rest = max(1, total - 1)
        for key in columns:
            if key == "name":
                shares.append(default_name)
            else:
                shares.append((1.0 - default_name) / rest)
        return shares

    def remember_column_widths(self):
        """Save the current column proportions after the user dragged a border."""
        if self._setting_column_widths or not self._column_layout_ready:
            return
        header = self.script_tree.header()
        if header is None:
            return
        total = float(header.length() or 1)
        if total <= 0:
            return
        columns = list(self.visible_columns())
        if self.show_row_numbers():
            # The row-number column keeps its automatic width.
            columns = columns[1:]
        shares = {}
        for section, key in enumerate(columns):
            if section < header.count():
                shares[key] = round(max(0.0, header.sectionSize(section)) / total, 4)
        if len(shares) == len(columns):
            # Compare against what is already stored: remember_column_widths is
            # also called on close, and writing the file when nothing changed
            # would touch it on every single start.
            if shares != self.settings.get("column_width_shares"):
                self.settings["column_width_shares"] = shares
                self.save_settings_deferred()

    def apply_name_column_width(self, columns, row_numbers=False):
        """Give the name column 40% of the list and split the rest evenly.

        The name is what the list is read for, so it used to lose its space to
        the other columns; here the widths are computed from the viewport width
        and the proportions the user last dragged are reused when they exist.
        The sections stay in Interactive mode so a drag is still possible.
        """
        tree = self.script_tree
        header = tree.header()
        if header is None:
            return
        total = max(1, tree.viewport().width() or tree.width())
        for section in range(header.count()):
            header.setSectionResizeMode(section, QtWidgets.QHeaderView.Interactive)
        shares = self.column_width_shares(list(columns))
        # Guard the whole resize so the sectionResized handler, which stores
        # the widths the user dragged, stays quiet while we set them ourselves.
        self._setting_column_widths = True
        try:
            for section, key in enumerate(columns):
                header.resizeSection(section, max(40, int(total * shares[section])))
        finally:
            self._setting_column_widths = False

    def separator_block_index(self, separator_id):
        """Position of a separator's block inside the ordered script list."""
        seen = 0
        for entry in self.scripts:
            if is_separator(entry):
                if entry.get("id") == separator_id:
                    return seen
            else:
                seen += 1
        return seen

    def insert_separator_at(self, position, label=""):
        """Insert a separator at the given script-block boundary."""
        separator = make_separator(label or self.setting("empty_separator_label", ""))
        boundary = max(0, position)
        scripts_seen = 0
        index = len(self.scripts)          # default: after everything
        for cursor, entry in enumerate(self.scripts):
            if is_separator(entry):
                continue
            if scripts_seen == boundary:
                index = cursor
                while index < len(self.scripts) and is_separator(self.scripts[index]):
                    index += 1
                break
            scripts_seen += 1
            if scripts_seen == boundary:
                index = cursor + 1
                while index < len(self.scripts) and is_separator(self.scripts[index]):
                    index += 1
                break
        self.scripts.insert(index, separator)
        return separator
    def refresh_list(self):
        self.apply_list_headers()
        self.script_tree.clear()
        self.script_tree.setAlternatingRowColors(bool(self.setting("zebra_stripes", True)))
        shown = self.visible_scripts()
        shown_ids = {script.get("id") for script in shown}
        separators_enabled = bool(self.setting("separators_enabled", True))
        if self.setting("group_by_category", False):
            self._populate_grouped(shown, separators_enabled)
        else:
            self._populate_flat(shown_ids, separators_enabled)
        total = len(self.scripts_only())
        if len(shown) != total:
            self.count_label.setText(f"{self.t('total_scripts')} {len(shown)} / {total}")
        else:
            self.count_label.setText(f"{self.t('total_scripts')} {total}")

    def _populate_flat(self, shown_ids, separators_enabled):
        """Render the list in the user's own order, separators included."""
        for entry in self.scripts:
            if is_separator(entry):
                if separators_enabled:
                    self.script_tree.addTopLevelItem(self._make_separator_item(entry))
                continue
            if entry.get("id") in shown_ids:
                self.script_tree.addTopLevelItem(self._make_list_item(entry))

    def _populate_grouped(self, shown, separators_enabled):
        """Fill the tree grouped by category."""
        by_category = collections.OrderedDict()
        for script in shown:
            by_category.setdefault(script.get("category", "") or "?", []).append(script)
        for category in sorted(by_category, key=lambda name: name.lower()):
            if separators_enabled:
                self.script_tree.addTopLevelItem(
                    self._make_separator_item(make_separator(category))
                )
            else:
                header = QtWidgets.QTreeWidgetItem([category])
                header.setForeground(0, QtGui.QColor("#8ab4f8"))
                header.setFlags(QtCore.Qt.ItemIsEnabled)
                self.script_tree.addTopLevelItem(header)
            for script in by_category[category]:
                self.script_tree.addTopLevelItem(self._make_list_item(script))

    def edit_selected_script(self, item=None):
        if item is None:
            selected = self.script_tree.selectedItems()
            if not selected:
                QtWidgets.QMessageBox.information(self, "Info", self.t("select_script"))
                return
            item = selected[0]
        script_id = item.data(0, QtCore.Qt.UserRole)
        if script_id is None:
            return
        entry = next((s for s in self.scripts if s.get("id") == script_id), None)
        if entry is None:
            return
        self._open_script_editor_dialog(entry)
    def on_list_selection_changed(self):
        selected = self.script_tree.selectedItems()
        if not selected:
            self.script_preview.show_empty()
            return
        if len(selected) > 1:
            self.script_preview.show_empty()
            self.count_label.setText(self.t("multi_selected").format(len(selected)))
            return
        script_id = selected[0].data(0, QtCore.Qt.UserRole)
        entry = next((s for s in self.scripts if s.get("id") == script_id), None)
        if entry is not None:
            self.script_preview.show_script(entry)

    def run_selected_script(self):
        scripts = self.selected_scripts()
        if not scripts:
            self.show_message(self.t("info"), self.t("select_script"))
            return
        if len(scripts) > 1:
            self.show_message(self.t("info"), self.t("multi_run_hint"))
            return
        entry = scripts[0]
        RunScriptDialog(self, entry.get("name", "Untitled"), entry.get("code", ""),
                        entry.get("category", "Python"), self.t).exec_()

    def run_selected_script_console(self):
        """Start the selected script in a separate console window."""
        script = self.selected_script()
        if not script:
            self.show_message(self.t("info"), self.t("select_script"))
            return
        language = (script.get("category", "Python") or "").lower().strip()
        temp_path = self.write_console_script(language)
        if temp_path is None:
            # No console form for this language: the runner window is the only
            # way to execute it, so fall back to it rather than doing nothing.
            self.show_message(self.t("info"),
                              self.t("run_console_no_language"))
            self.run_selected_script()
            return
        process = self.launch_console(temp_path, language)
        if process is None:
            self._safe_remove(temp_path)
            return
        # The notice is modeless: it closes itself the moment the user closes
        # the console window, instead of waiting for an OK click.
        self._watch_console(process, temp_path)

    def _watch_console(self, process, temp_path):
        """Show a self-closing notice tied to the console window lifetime.

        A frameless toast is used instead of QMessageBox so the notice never
        blocks the main window and closes by itself once the console is gone.
        """
        parent = self if self.isVisible() else None
        toast = QtWidgets.QLabel(self.t("run_console_started"), parent)
        toast.setStyleSheet(
            "QLabel { background: palette(window); color: palette(text);"
            " border: 1px solid palette(mid); border-radius: 6px;"
            " padding: 10px 18px; }"
        )
        toast.setWindowFlags(QtCore.Qt.ToolTip | QtCore.Qt.FramelessWindowHint
                             | QtCore.Qt.WindowStaysOnTopHint)
        toast.setAlignment(QtCore.Qt.AlignCenter)
        # No WA_DeleteOnClose here: the toast is deleted with the main window,
        # and tearing the C++ object down from the timer tick is a crash risk.
        timer = QtCore.QTimer(toast)

        def check():
            if process.poll() is not None:
                timer.stop()
                toast.hide()
                # Module-level helper, not a window method: an unhandled
                # exception inside a Qt slot aborts the whole process.
                _safe_remove(temp_path)

        timer.timeout.connect(check)
        timer.start(500)
        # If the console already ended before the notice appeared, do not make
        # the user wait for the first tick.
        QtCore.QTimer.singleShot(0, check)

        def place():
            toast.adjustSize()
            if parent is not None:
                anchor = parent.mapToGlobal(QtCore.QPoint(0, parent.height()))
            else:
                screen = QtWidgets.QApplication.primaryScreen()
                area = screen.availableGeometry() if screen else QtCore.QRect(0, 0, 800, 600)
                anchor = QtCore.QPoint(area.center().x() - toast.width() // 2,
                                       area.bottom() - 120)
            toast.move(anchor.x() + 40, anchor.y() + 40)

        place()
        toast.show()
        self._console_notice = toast
        return toast

    def write_console_script(self, language):
        """Write the script to a temp file for an external console run."""
        import tempfile
        script = self.selected_script()
        if script is None:
            return None
        suffixes = {
            "python": ".py", "py": ".py", "": ".py",
            "bash": ".sh", "sh": ".sh", "shell": ".sh",
            "powershell": ".ps1", "pwsh": ".ps1",
            "javascript": ".js", "js": ".js", "node": ".js",
            "batch": ".bat", "bat": ".bat", "cmd": ".bat",
            "vbscript": ".vbs", "vbs": ".vbs",
        }
        suffix = suffixes.get(language)
        if suffix is None:
            return None
        fd, path = tempfile.mkstemp(suffix=suffix)
        os.close(fd)
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(script.get("code", ""))
        except OSError:
            _safe_remove(path)
            return None
        return path

    def launch_console(self, temp_path, language):
        """Start a detached console running the temp script.

        Returns the live Popen handle so the caller can watch the window, or
        None when the console could not be started.
        """
        import subprocess
        try:
            if os.name == "nt":
                # The interpreter is launched directly, in its own console
                # (CREATE_NEW_CONSOLE), with /k for cmd-based languages so the
                # window stays open after the script ends and the output can
                # be read. Wrapping this in "cmd /c start" would only add a
                # second, instantly closing window.
                argv = self.console_argv_for(temp_path, language)
                # CREATE_NO_WINDOW must not be combined with
                # CREATE_NEW_CONSOLE: it means "start without any visible
                # console", which is exactly what this button must not do.
                return subprocess.Popen(
                    argv,
                    cwd=DATA_DIR,
                    shell=False,
                    creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
                )
            else:
                import shutil
                env = os.environ.copy()
                if shutil.which("x-terminal-emulator"):
                    return subprocess.Popen(
                        ["x-terminal-emulator", "--", "sh", temp_path],
                        cwd=DATA_DIR, env=env, start_new_session=True)
                return subprocess.Popen(
                    ["sh", temp_path],
                    cwd=DATA_DIR, env=env, start_new_session=True)
        except Exception:
            return None

    def console_argv_for(self, temp_path, language):
        """Argument list that runs the temp script inside a console window."""
        if language in {"python", "py", ""}:
            return [sys.executable, temp_path]
        if language in {"bash", "sh", "shell"}:
            return ["bash", temp_path]
        if language in {"powershell", "pwsh"}:
            return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-NoExit", "-File", temp_path]
        if language in {"javascript", "js", "node"}:
            return ["node", temp_path]
        if language in {"batch", "bat", "cmd"}:
            return ["cmd.exe", "/k", temp_path]
        if language in {"vbscript", "vbs"}:
            return ["cscript", "//nologo", temp_path]
        return [temp_path]

    def console_command_for(self, temp_path, language):
        """The shell command line that runs the temp script in a console."""
        if language in {"python", "py", ""}:
            return '"' + sys.executable + '" "' + temp_path + '"'
        if language in {"bash", "sh", "shell"}:
            return 'bash "' + temp_path + '"'
        if language in {"powershell", "pwsh"}:
            return 'powershell -NoProfile -ExecutionPolicy Bypass -File "' + temp_path + '"'
        if language in {"javascript", "js", "node"}:
            return 'node "' + temp_path + '"'
        if language in {"batch", "bat", "cmd"}:
            return '"' + temp_path + '"'
        if language in {"vbscript", "vbs"}:
            return 'cscript //nologo "' + temp_path + '"'
        return '"' + temp_path + '"'

    def delete_selected_script(self):
        scripts = self.selected_scripts()
        if not scripts:
            self.show_message(self.t("info"), self.t("select_script"))
            return
        if len(scripts) > 1:
            question = self.t("confirm_delete_multi").format(len(scripts))
        else:
            question = self.t("confirm_delete")
        if not QtWidgets.QMessageBox.question(self, self.t("confirm"), question, QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No):
            return
        ids = {s.get("id") for s in scripts}
        removed_names = [s.get("name", "") for s in scripts]
        self.scripts = [s for s in self.scripts if s.get("id") not in ids]
        save_data(DATA_FILE, self.scripts)
        self.refresh_list()
        # Tell the audit subsystem that modules were removed.
        self.notify_audit("module_updated",
                          where=", ".join(removed_names),
                          detail=self.t("audit_changed", "changed"))
        if len(ids) > 1:
            self.show_message(self.t("info"), self.t("deleted_multi").format(len(ids)))
        else:
            self.show_message(self.t("info"), self.t("deleted"))

    def copy_form_code(self):
        form = getattr(self, "script_form", None)
        code = form.code_edit.toPlainText().strip() if form is not None else ""
        if not code:
            selected = self.selected_script()
            code = selected.get("code", "").strip() if selected else ""
        if not code:
            return
        QtWidgets.QApplication.clipboard().setText(code)
        self.show_message(self.t("info"), self.t("copied"))

    def preview_selected_script(self):
        script = self.selected_script()
        if not script:
            QtWidgets.QMessageBox.information(self, self.t("info"), self.t("select_script"))
            return
        CodePreviewDialog(self, script.get("name", ""), script.get("code", ""),
                          script.get("category", "Python")).exec_()
    def duplicate_selected_script(self):
        scripts = self.selected_scripts()
        if not scripts:
            QtWidgets.QMessageBox.information(self, self.t("info"), self.t("select_script"))
            return
        now = dt.datetime.now()
        for offset, script in enumerate(scripts):
            clone = normalize_entry(dict(script))
            clone["id"] = str(now.timestamp() + offset)
            clone["name"] = f"{script.get('name', '')} (copy)"
            clone["date"] = now.strftime("%Y-%m-%d %H:%M:%S")
            clone["date_created"] = clone["date"]
            clone["history"] = []
            self.scripts.append(clone)
        save_data(DATA_FILE, self.scripts)
        self.refresh_list()
        self.notify_audit("module_installed",
                          where=", ".join(s.get("name", "") for s in scripts),
                          detail=self.t("duplicated", "duplicated"))
        if len(scripts) > 1:
            QtWidgets.QMessageBox.information(self, self.t("info"), self.t("duplicated_multi").format(len(scripts)))
        else:
            QtWidgets.QMessageBox.information(self, self.t("info"), self.t("duplicated"))

    def show_script_history(self):
        script = self.selected_script()
        if not script:
            QtWidgets.QMessageBox.information(self, self.t("info"), self.t("select_script"))
            return
        HistoryDialog(self, self, script).exec_()

    def show_run_menu(self):
        """Drop the run menu down from the Run... button."""
        self.run_menu.exec_(
            self.btn_run_script.mapToGlobal(
                QtCore.QPoint(0, self.btn_run_script.height())))

    def show_import_menu(self):
        """Drop the import menu down from the Import... button."""
        self.import_menu.exec_(
            self.btn_import.mapToGlobal(
                QtCore.QPoint(0, self.btn_import.height())))

    def show_export_menu(self):
        """Drop the export menu down from the Export... button."""
        self.export_menu.exec_(
            self.btn_export.mapToGlobal(
                QtCore.QPoint(0, self.btn_export.height())))

    def export_report(self):
        """Save a Markdown report with all scripts, tags and descriptions."""
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, self.t("report"), "scripts_report.md", "Markdown files (*.md)")
        if not path:
            return
        lines = ["# ScriptDB report", "", f"Generated: {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", "", f"Total scripts: {len(self.scripts)}", ""]
        for script in self.scripts:
            lines.append(f"## {script.get('name', '')}")
            lines.append(f"- Category: {script.get('category', '')}")
            lines.append(f"- Tags: {script.get('tags', '') or '-'}")
            lines.append(f"- Favorite: {'yes' if script.get('favorite') else 'no'}")
            lines.append(f"- Updated: {script.get('date', '')}")
            lines.append("")
            lines.append(script.get("desc", "").strip() or "(no description)")
            lines.append("")
            lines.append("```" + (script.get("category", "").lower() or ""))
            lines.append(script.get("code", ""))
            lines.append("```")
            lines.append("")
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("\n".join(lines))
            QtWidgets.QMessageBox.information(self, self.t("info"), self.t("exported"))
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, self.t("export_error"), str(exc))
    def show_statistics(self):
        """Summarise the database: totals, categories, tags and favorites."""
        per_category = {}
        per_tag = {}
        for script in self.scripts_only():
            per_category[script.get("category", "?")] = per_category.get(script.get("category", "?"), 0) + 1
            for tag in [t.strip() for t in script.get("tags", "").split(",") if t.strip()]:
                per_tag[tag] = per_tag.get(tag, 0) + 1
        total_scripts = len(self.scripts_only())
        favorite_count = sum(1 for s in self.scripts if s.get("favorite"))
        total_lines = sum(len(s.get('code', '').splitlines()) for s in self.scripts)
        history_count = sum(1 for s in self.scripts if s.get("history"))
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle(self.t("stats_title"))
        dialog.setMinimumSize(680, 520)
        dialog.resize(760, 600)
        layout = QtWidgets.QVBoxLayout(dialog)

        header = QtWidgets.QFrame()
        header.setObjectName("statsHeader")
        header_layout = QtWidgets.QVBoxLayout(header)
        header_layout.setContentsMargins(20, 16, 20, 16)
        title = QtWidgets.QLabel(self.t("stats_title", "Database statistics"))
        title.setObjectName("statsTitle")
        subtitle = QtWidgets.QLabel(
            "A quick overview of your local script collection")
        subtitle.setObjectName("statsSubtitle")
        header_layout.addWidget(title)
        header_layout.addWidget(subtitle)
        layout.addWidget(header)

        metrics = QtWidgets.QHBoxLayout()
        metrics.setSpacing(10)
        for value, label in (
                (total_scripts, "Scripts"),
                (total_lines, "Code lines"),
                (favorite_count, "Favorites")):
            card = QtWidgets.QFrame()
            card.setObjectName("statsCard")
            card_layout = QtWidgets.QVBoxLayout(card)
            card_layout.setContentsMargins(14, 11, 14, 11)
            number = QtWidgets.QLabel(f"{value:,}")
            number.setObjectName("statsNumber")
            caption = QtWidgets.QLabel(label)
            caption.setObjectName("statsCaption")
            card_layout.addWidget(number)
            card_layout.addWidget(caption)
            metrics.addWidget(card, 1)
        layout.addLayout(metrics)

        content = QtWidgets.QHBoxLayout()
        content.setSpacing(12)

        category_box = QtWidgets.QGroupBox("By category")
        category_layout = QtWidgets.QVBoxLayout(category_box)
        category_layout.setSpacing(8)
        max_category = max(per_category.values(), default=1)
        for category, count in sorted(per_category.items(),
                                      key=lambda item: (-item[1], item[0].lower())):
            row = QtWidgets.QHBoxLayout()
            name = QtWidgets.QLabel(category)
            name.setMinimumWidth(92)
            bar = QtWidgets.QProgressBar()
            bar.setRange(0, max_category)
            bar.setValue(count)
            bar.setFormat(f"{count}  ({count / max(total_scripts, 1):.0%})")
            bar.setTextVisible(True)
            row.addWidget(name)
            row.addWidget(bar, 1)
            category_layout.addLayout(row)
        if not per_category:
            category_layout.addWidget(QtWidgets.QLabel("No categories yet."))
        category_layout.addStretch()
        content.addWidget(category_box, 3)

        details_box = QtWidgets.QGroupBox("Collection details")
        details_layout = QtWidgets.QVBoxLayout(details_box)
        details_layout.addWidget(QtWidgets.QLabel(f"With history: {history_count}"))
        details_layout.addWidget(QtWidgets.QLabel(f"Unique tags: {len(per_tag)}"))
        details_layout.addSpacing(8)
        details_layout.addWidget(QtWidgets.QLabel("Top tags"))
        tag_view = QtWidgets.QListWidget()
        tag_view.setMinimumWidth(190)
        for tag, count in sorted(per_tag.items(), key=lambda item: (-item[1], item[0].lower()))[:10]:
            tag_view.addItem(f"{tag}  ·  {count}")
        if not per_tag:
            tag_view.addItem("No tags yet.")
        details_layout.addWidget(tag_view, 1)
        content.addWidget(details_box, 2)
        layout.addLayout(content, 1)

        close_btn = QtWidgets.QPushButton("Close")
        close_btn.clicked.connect(dialog.close)
        footer = QtWidgets.QHBoxLayout()
        footer.addStretch()
        footer.addWidget(close_btn)
        layout.addLayout(footer)

        palette = dialog.palette()
        text_color = palette.color(QtGui.QPalette.WindowText).name()
        muted_color = palette.color(QtGui.QPalette.Mid).name()
        panel_color = palette.color(QtGui.QPalette.Base).name()
        header_color = palette.color(QtGui.QPalette.Midlight).name()
        border_color = palette.color(QtGui.QPalette.Mid).name()
        accent_color = palette.color(QtGui.QPalette.Highlight).name()
        dialog.setStyleSheet("""
            QFrame#statsHeader {
                background: %(header)s;
                border: 1px solid %(border)s;
                border-radius: 8px;
            }
            QLabel#statsTitle {
                color: %(text)s;
                font-size: 22px;
                font-weight: bold;
            }
            QLabel#statsSubtitle, QLabel#statsCaption { color: %(muted)s; }
            QFrame#statsCard {
                background: %(panel)s;
                border: 1px solid %(border)s;
                border-radius: 6px;
            }
            QLabel#statsNumber { color: %(text)s; font-size: 20px; font-weight: bold; }
            QGroupBox { color: %(text)s; }
            QListWidget { color: %(text)s; background: %(panel)s; }
            QProgressBar {
                color: %(text)s;
                min-height: 20px;
                text-align: right;
            }
            QProgressBar::chunk { background: %(accent)s; }
        """ % {"text": text_color, "muted": muted_color,
               "panel": panel_color, "header": header_color,
               "border": border_color, "accent": accent_color})
        dialog.exec_()

    def show_templates(self):
        self.clear_form()
        self._open_script_editor_dialog()
        TemplateDialog(self, self.script_form).exec_()
    def import_json(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Import JSON", "", "JSON files (*.json)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if not isinstance(data, list):
                return
            existing = {s.get("id") for s in self.scripts}
            added = 0
            for entry in data:
                if not isinstance(entry, dict):
                    continue
                normalized = normalize_entry(entry)
                if normalized is None or normalized.get("id") in existing:
                    continue
                self.scripts.append(normalized)
                existing.add(normalized.get("id"))
                added += 1
            save_data(DATA_FILE, self.scripts)
            self.refresh_list()
            QtWidgets.QMessageBox.information(self, self.t("info"), f"{self.t('imported')} ({added})")
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, self.t("import_error"), str(exc))

    def export_json(self):
        """Export the whole database or only the scripts currently visible."""
        answer = QtWidgets.QMessageBox.question(
            self, self.t("confirm"),
            f"{self.t('export_json')}: {self.t('all_scripts')} (Yes) / {self.t('favorites_only')} -> visible (No)?",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No | QtWidgets.QMessageBox.Cancel,
        )
        if answer == QtWidgets.QMessageBox.Cancel:
            return
        payload = self.scripts if answer == QtWidgets.QMessageBox.Yes else self.visible_scripts()
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, self.t("export_json"), "scripts_export.json", "JSON files (*.json)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            QtWidgets.QMessageBox.information(self, self.t("info"), f"{self.t('exported')} ({len(payload)})")
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, self.t("export_error"), str(exc))
    def import_script_file(self):
        """Import one or more plain source files in their natural form."""
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self, self.t("import_file"), "", self.t("all_source_files")
        )
        if not paths:
            return
        existing = {s.get("id") for s in self.scripts}
        added = 0
        failed = []
        for path in paths:
            try:
                entry = build_script_from_file(path)
            except OSError as exc:
                failed.append(f"{os.path.basename(path)}: {exc}")
                continue
            if entry is None:
                continue
            while entry["id"] in existing:
                entry["id"] = str(float(entry["id"]) + 1e-6)
            existing.add(entry["id"])
            self.scripts.append(entry)
            added += 1
        if added:
            save_data(DATA_FILE, self.scripts)
            self.refresh_list()
        if failed:
            QtWidgets.QMessageBox.warning(
                self, self.t("warning"),
                f"{self.t('import_error')}\n" + "\n".join(failed),
            )
        QtWidgets.QMessageBox.information(self, self.t("info"), f"{self.t('imported')} ({added})")

    def import_script_folder(self):
        """Import recognized source files from a folder and its subfolders."""
        folder = QtWidgets.QFileDialog.getExistingDirectory(
            self, self.t("import_menu_folder", "Whole folder..."))
        if not folder:
            return
        supported = set(EXTENSION_ALIASES) | {".txt"}
        paths = []
        for root, _directories, filenames in os.walk(folder):
            for filename in filenames:
                if os.path.splitext(filename)[1].lower() in supported:
                    paths.append(os.path.join(root, filename))
        paths.sort(key=lambda path: os.path.relpath(path, folder).lower())
        if not paths:
            QtWidgets.QMessageBox.information(
                self, self.t("info"), self.t("import_folder_empty",
                                               "No supported source files found."))
            return

        existing = {s.get("id") for s in self.scripts}
        added = 0
        failed = []
        for path in paths:
            try:
                entry = build_script_from_file(path)
            except OSError as exc:
                failed.append(f"{os.path.relpath(path, folder)}: {exc}")
                continue
            if entry is None:
                continue
            while entry["id"] in existing:
                entry["id"] = str(float(entry["id"]) + 1e-6)
            existing.add(entry["id"])
            relative = os.path.relpath(path, folder)
            entry["desc"] = (entry.get("desc", "") + "\n"
                              + "Imported from: " + relative).strip()
            self.scripts.append(entry)
            added += 1
        if added:
            save_data(DATA_FILE, self.scripts)
            self.refresh_list()
        if failed:
            QtWidgets.QMessageBox.warning(
                self, self.t("warning"),
                f"{self.t('import_error')}\n" + "\n".join(failed),
            )
        QtWidgets.QMessageBox.information(
            self, self.t("info"), f"{self.t('imported')} ({added})")

    def export_script_file(self):
        """Export the selected script(s) as plain files keeping the extension."""
        scripts = self.selected_scripts()
        if not scripts:
            self.show_message(self.t("info"), self.t("select_script"))
            return
        if len(scripts) == 1:
            script = scripts[0]
            ext = extension_for_category(script.get("category", ""))
            suggested = safe_file_name(script.get("name", ""), "script") + ext
            path, _ = QtWidgets.QFileDialog.getSaveFileName(
                self, self.t("export_file"), suggested, f"*{ext};;{self.t('all_source_files')}"
            )
            if not path:
                return
            targets = [(path, script)]
        else:
            directory = QtWidgets.QFileDialog.getExistingDirectory(self, self.t("export_file"))
            if not directory:
                return
            targets = [(script_export_path(script, directory), script) for script in scripts]
        try:
            for path, script in targets:
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(script.get("code", ""))
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, self.t("export_error"), str(exc))
            return
        if len(targets) == 1:
            self.show_message(self.t("info"), f"{self.t('exported_file')}\n{targets[0][0]}")
        else:
            self.show_message(self.t("info"), f"{self.t('exported')} ({len(targets)})")


# ===========================================================================
# Diff engine - algorithms (Myers / Patience / Histogram), normalization,
# token/byte/AST comparison and directory walking.
# ===========================================================================

DIFF_VIEWS = ("side", "unified", "inline", "blocks", "ast", "analysis",
              "files", "merge3", "merge4", "live", "sysrest", "env", "hex")
DIFF_ALGORITHMS = ("myers", "patience", "histogram")
DIFF_MODES = ("text", "token", "bytes")
# The "special" modes reachable from the Modes... menu (view key -> label key).
DIFF_SPECIAL_MODES = (
    ("merge3", "diff_mode_merge3"),
    ("merge4", "diff_mode_merge4"),
    ("live", "diff_mode_live"),
    ("sysrest", "diff_mode_sysrest"),
    ("env", "diff_mode_env"),
    ("hex", "diff_mode_hex"),
)

COMMENT_MARK_SETS = {
    "Python": ("#",), "Bash": ("#",), "PowerShell": ("#",),
    "VBScript": ("'",), "JavaScript": ("//",), "Java": ("//",),
    "C++": ("//",), "Batch": ("rem",), "SQL": ("--",),
    "HTML": ("<!--", "//"), "HTA": ("//", "<!--"),
}
# A neutral superset of comment markers, used when the language is unknown.
COMMENT_MARKS = ("#", "//", "--", ";", "'", "rem", "/*", "*", "<!--")


def _comment_key(line, language):
    # Lower-cased comparison key used to decide whether a line is a comment.
    stripped = (line or "").lstrip()
    if not stripped:
        return None
    markers = COMMENT_MARK_SETS.get(language or "", ())
    lowered = stripped.lower()
    for mark in markers:
        if mark in ("//", "/*", "<!--"):
            if lowered.startswith(mark):
                return mark
        elif lowered.startswith(mark):
            return mark
    return None


def is_comment_line(line, language=""):
    """True when a line is (mostly) a comment in the given language."""
    marker = _comment_key(line, language)
    if marker:
        return True
    stripped = (line or "").strip().lower()
    return any(stripped.startswith(m) for m in COMMENT_MARKS)


def prepare_diff_lines(text, ignore_case=False, ignore_ws=False,
                       ignore_blank=False, ignore_comments=False, language=""):
    """Normalise a code body into the list of lines the differ will compare."""
    lines = (text or "").splitlines()
    if ignore_comments:
        lines = [line for line in lines if not is_comment_line(line, language)]
    if ignore_blank:
        lines = [line for line in lines if line.strip()]
    if ignore_ws:
        lines = [re.sub(r"\s+", " ", line).strip() for line in lines]
    if ignore_case:
        lines = [line.lower() for line in lines]
    return lines


def _myers_ops(left, right):
    """Line-level opcodes using difflib's Myers-like matcher."""
    import difflib
    matcher = difflib.SequenceMatcher(a=left, b=right, autojunk=False)
    return matcher.get_opcodes()


def _patience_ops(left, right):
    """Patience-style diff: anchor on unique common lines, recurse on gaps.

    A recursion-depth guard and an explicit progress check stop the walk from
    looping forever when a gap has no usable anchors (which would otherwise
    recurse on the very same range and overflow the stack).
    """
    def anchors(lo1, hi1, lo2, hi2):
        count_left = collections.Counter(left[lo1:hi1])
        count_right = collections.Counter(right[lo2:hi2])
        found = []
        for line in dict.fromkeys(left[lo1:hi1]):
            if count_left[line] == 1 and count_right.get(line) == 1:
                found.append((line, right.index(line, lo2, hi2)))
        found.sort(key=lambda kv: kv[1])
        return found

    def longest_increasing(values):
        # Indices forming a longest strictly increasing subsequence of `values`.
        tails = []
        prev = [-1] * len(values)
        for index, value in enumerate(values):
            lo, hi = 0, len(tails)
            while lo < hi:
                mid = (lo + hi) // 2
                if values[tails[mid]] < value:
                    lo = mid + 1
                else:
                    hi = mid
            prev[index] = tails[lo - 1] if lo > 0 else -1
            if lo == len(tails):
                tails.append(index)
            else:
                tails[lo] = index
        result = []
        node = tails[-1] if tails else -1
        while node >= 0:
            result.append(node)
            node = prev[node]
        result.reverse()
        return result

    ops = []

    def walk(lo1, hi1, lo2, hi2, depth=0):
        if lo1 >= hi1 and lo2 >= hi2:
            return
        common = anchors(lo1, hi1, lo2, hi2)
        if not common or depth > 40 or lo1 >= hi1 or lo2 >= hi2:
            ops.append(("replace", lo1, hi1, lo2, hi2))
            return
        keep = [common[k] for k in longest_increasing([p for _l, p in common])]
        if not keep:
            ops.append(("replace", lo1, hi1, lo2, hi2))
            return
        prev1, prev2 = lo1, lo2
        for line, _position in keep:
            j = right.index(line, prev2, hi2)
            i = left.index(line, prev1, hi1)
            walk(prev1, i, prev2, j, depth + 1)
            ops.append(("equal", i, i + 1, j, j + 1))
            prev1, prev2 = i + 1, j + 1
        walk(prev1, hi1, prev2, hi2, depth + 1)

    walk(0, len(left), 0, len(right))
    merged = []
    for op in ops:
        if (merged and op[0] == "equal" and merged[-1][0] == "equal"
                and merged[-1][2] == op[1] and merged[-1][4] == op[3]):
            previous = merged[-1]
            merged[-1] = ("equal", previous[1], op[2], previous[3], op[4])
        else:
            merged.append(op)
    return merged


def _histogram_ops(left, right):
    """Histogram-style diff: difflib with autojunk, a good histogram stand-in."""
    import difflib
    matcher = difflib.SequenceMatcher(a=left, b=right, autojunk=False)
    return matcher.get_opcodes()


DIFF_ALGORITHM_HANDLERS = {
    "myers": _myers_ops,
    "patience": _patience_ops,
    "histogram": _histogram_ops,
}


def word_diff_segments(left_text, right_text):
    """Split two lines into tokens and mark which pieces differ.

    Returns (left_segments, right_segments) where each segment is a
    (text, changed) pair, so the inline view can highlight the exact changes.
    """
    token_re = re.compile(r"\w+|\s+|[^\w\s]")
    left_tokens = [m.group(0) for m in token_re.finditer(left_text)]
    right_tokens = [m.group(0) for m in token_re.finditer(right_text)]
    import difflib
    matcher = difflib.SequenceMatcher(a=left_tokens, b=right_tokens, autojunk=False)
    left_segments = []
    right_segments = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        left_piece = "".join(left_tokens[i1:i2])
        right_piece = "".join(right_tokens[j1:j2])
        if tag == "equal":
            left_segments.append((left_piece, False))
            right_segments.append((right_piece, False))
        else:
            if left_piece:
                left_segments.append((left_piece, True))
            if right_piece:
                right_segments.append((right_piece, True))
    return left_segments, right_segments


def align_lines(left, right, algorithm="myers"):
    """Return diff rows describing how two line lists relate.

    Rows carry tag ('equal'/'replace'/'delete'/'insert'), line numbers on both
    sides and word-level segments for 'replace' rows so the views can highlight
    the exact changes inside a line.
    """
    handler = DIFF_ALGORITHM_HANDLERS.get(algorithm, _myers_ops)
    rows = []
    for tag, i1, i2, j1, j2 in handler(left, right):
        if tag == "equal":
            for offset in range(i2 - i1):
                rows.append({
                    "tag": "equal",
                    "left_no": i1 + offset + 1, "left": left[i1 + offset],
                    "right_no": j1 + offset + 1, "right": right[j1 + offset],
                    "left_segments": None, "right_segments": None,
                })
        elif tag == "replace":
            overlap = min(i2 - i1, j2 - j1)
            for offset in range(overlap):
                lseg, rseg = word_diff_segments(left[i1 + offset], right[j1 + offset])
                rows.append({
                    "tag": "replace",
                    "left_no": i1 + offset + 1, "left": left[i1 + offset],
                    "right_no": j1 + offset + 1, "right": right[j1 + offset],
                    "left_segments": lseg, "right_segments": rseg,
                })
            for li in range(i1 + overlap, i2):
                rows.append({
                    "tag": "delete", "left_no": li + 1, "left": left[li],
                    "right_no": None, "right": "",
                    "left_segments": None, "right_segments": None,
                })
            for rj in range(j1 + overlap, j2):
                rows.append({
                    "tag": "insert", "left_no": None, "left": "",
                    "right_no": rj + 1, "right": right[rj],
                    "left_segments": None, "right_segments": None,
                })
        elif tag == "delete":
            for li in range(i1, i2):
                rows.append({
                    "tag": "delete", "left_no": li + 1, "left": left[li],
                    "right_no": None, "right": "",
                    "left_segments": None, "right_segments": None,
                })
        elif tag == "insert":
            for rj in range(j1, j2):
                rows.append({
                    "tag": "insert", "left_no": None, "left": "",
                    "right_no": rj + 1, "right": right[rj],
                    "left_segments": None, "right_segments": None,
                })
    return rows
# A token of the "token" comparison mode: identifiers, numbers, strings,
# operators and whitespace, so only the meaningful pieces are diffed.
TOKEN_PATTERN = re.compile(r"[A-Za-z_]\w*|\d+(?:\.\d+)?|\s+|.", re.UNICODE)


def tokenize(text):
    """Split text into diffable tokens (word / number / space / punctuation)."""
    return TOKEN_PATTERN.findall(text or "")


def bytes_diff_lines(data, other):
    """Compare two byte blobs and return human readable difference lines."""
    if data == other:
        return []
    result = []
    limit = max(len(data), len(other))
    step = 16
    for base in range(0, limit, step):
        chunk_a = data[base:base + step]
        chunk_b = other[base:base + step]
        if chunk_a == chunk_b:
            continue
        result.append(f"@{base:08x}")
        result.append("- " + " ".join(f"{b:02x}" for b in chunk_a))
        result.append("+ " + " ".join(f"{b:02x}" for b in chunk_b))
    return result


def token_diff_lines(left, right, algorithm="myers"):
    """Diff two texts at token level; returns display rows."""
    left_tokens = tokenize(left)
    right_tokens = tokenize(right)
    rows = align_lines(left_tokens, right_tokens, algorithm)
    return rows, left_tokens, right_tokens


def ast_symbols(text, language):
    """Extract top-level symbols (functions/classes) with a light parser.

    This is not a full parser: it recognises the common definition forms of
    Python, JavaScript/Java/C++ and shell so the AST view can group the diff by
    function or class without pulling in a language grammar.
    """
    language = (language or "").lower()
    symbols = []
    if language in ("python", "py", ""):
        # `\s*` before the keyword would swallow the preceding blank lines, so
        # only horizontal whitespace is allowed; the group limit keeps the
        # reported offset on the `def`/`class` keyword itself.
        pattern = re.compile(r"^[ \t]*(def|class)\s+(\w+)\s*(\([^)]*\))?\s*:", re.MULTILINE)
        for match in pattern.finditer(text or ""):
            keyword_at = match.start(1)
            symbols.append((match.group(2), "class" if match.group(1) == "class" else "func",
                            keyword_at, _python_block_end(text, keyword_at)))
    else:
        pattern = re.compile(
            r"^\s*(?:function\s+(\w+)|(?:public|private|protected|static|final|\s)*"
            r"(?:class|interface|struct)\s+(\w+)|(\w+)\s*\([^)]*\)\s*\{)",
            re.MULTILINE)
        for match in pattern.finditer(text or ""):
            name = match.group(1) or match.group(2) or match.group(3)
            kind = "class" if match.group(2) else "func"
            symbols.append((name, kind, match.start(), _brace_block_end(text, match.start())))
    return symbols


def _python_block_end(text, start):
    # End offset of an indented Python block that begins at `start`.
    lines = text[start:].splitlines(keepends=True)
    if not lines:
        return len(text)
    header = lines[0]
    base_indent = len(header) - len(header.lstrip())
    offset = start + len(header)
    for line in lines[1:]:
        if not line.strip():
            offset += len(line)
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= base_indent:
            break
        offset += len(line)
    return offset


def _brace_block_end(text, start):
    # End offset of a brace-delimited block beginning at `start`.
    depth = 0
    seen = False
    for index in range(start, len(text or "")):
        char = text[index]
        if char == "{":
            depth += 1
            seen = True
        elif char == "}":
            depth -= 1
            if seen and depth <= 0:
                return index + 1
    return len(text)
CHANGE_KINDS = ("added", "removed", "changed")


def change_kind(row):
    """Map a diff row tag onto a user facing change category."""
    tag = row.get("tag")
    if tag == "insert":
        return "added"
    if tag == "delete":
        return "removed"
    if tag == "replace":
        return "changed"
    return "equal"


def iter_change_blocks(rows):
    """Group consecutive non-equal rows into change blocks.

    Returns (start_index, rows) tuples so the sidebar, the filters and the
    progress bar all work from the same notion of "one change".
    """
    blocks = []
    current = []
    start = None
    for index, row in enumerate(rows):
        if row.get("tag") == "equal":
            if current:
                blocks.append((start, current))
                current = []
                start = None
            continue
        if start is None:
            start = index
        current.append(row)
    if current:
        blocks.append((start, current))
    return blocks


def symbols_in_range(symbols, start, end):
    """Names of the symbols overlapping the document range [start, end)."""
    return [
        name for name, _kind, sym_start, sym_end in symbols
        if sym_start < end and sym_end > start
    ]


def _structural_name_change(left_line, right_line):
    """True when a definition line changed its declared name only."""
    pattern = re.compile(
        r"\b(?:def|class|function|interface|struct)\s+([A-Za-z_]\w*)")
    left_match = pattern.search(left_line or "")
    right_match = pattern.search(right_line or "")
    if left_match and right_match:
        return left_match.group(1) != right_match.group(1)
    return False


def renamed_definitions(left_code, right_code):
    """Pairs of symbol names that were renamed between the two sides."""
    left_names = {name for name, kind, _a, _b in ast_symbols(left_code, "Python")
                  if kind == "func"}
    right_names = {name for name, kind, _a, _b in ast_symbols(right_code, "Python")
                   if kind == "func"}
    return sorted(left_names - right_names), sorted(right_names - left_names)


# ---------------------------------------------------------------------------
# Code analysis: language detection, AST-level comparison, refactor and
# duplicate detection.
# ---------------------------------------------------------------------------

LANGUAGE_SIGNATURES = [
    # (kind, [regex patterns], [file extensions])
    ("python", [r"^\s*def\s+\w+\s*\(", r"^\s*class\s+\w+", r"^\s*import\s+\w+",
                r"if\s+__name__\s*=="], [".py", ".pyw", ".pyi"]),
    ("csharp", [r"\busing\s+[\w.]+;", r"\bnamespace\s+[\w.]+", r"\bpublic\s+(?:class|void|int)\b"],
               [".cs"]),
    ("cpp", [r"#include\s*<", r"\bstd::", r"\bint\s+main\s*\("], [".cpp", ".cc", ".cxx", ".h", ".hpp", ".c"]),
    ("powershell", [r"\bWrite-Host\b", r"\bGet-ChildItem\b", r"\$env:", r"^\s*param\s*\("],
                   [".ps1", ".psm1", ".psd1"]),
    ("bash", [r"^#!.*\b(?:bash|sh)\b", r"\becho\s+", r"\bfi\b", r"\bdone\b"],
             [".sh", ".bash", ".zsh", ".ksh"]),
    ("batch", [r"^\s*@echo\s+off", r"\bsetlocal\b", r"\bgoto\s+:\w+"], [".bat", ".cmd", ".nt"]),
    ("vbscript", [r"\bOption\s+Explicit\b", r"\bSub\s+\w+", r"\bEnd\s+Sub\b", r"\bWScript\."],
                 [".vbs", ".vbe", ".wsf"]),
    ("javascript", [r"\bconsole\.log\b", r"\bfunction\s+\w+\s*\(",
                    r"\bconst\s+\w+\s*=", r"=>"], [".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"]),
    ("java", [r"\bpublic\s+static\s+void\s+main\b", r"\bSystem\.out\.println\b",
              r"\bpackage\s+[\w.]+;"], [".java"]),
]

LANGUAGE_LABELS = {
    "python": "Python", "csharp": "C#", "cpp": "C++", "java": "Java",
    "javascript": "JavaScript", "powershell": "PowerShell", "bash": "Bash",
    "batch": "Batch", "vbscript": "VBScript", "text": "Plain text",
}


def detect_language_kind(code, filename="", hint=""):
    """Identify the language of a code block (kind + human label).

    The file extension wins when it is known; otherwise the content is scored
    against the language signatures and the best match is returned.
    """
    ext = os.path.splitext(filename or "")[1].lower()
    for kind, _patterns, extensions in LANGUAGE_SIGNATURES:
        if ext and ext in extensions:
            return kind, LANGUAGE_LABELS[kind]
    text = (code or "").lower()
    if not text.strip():
        return "text", LANGUAGE_LABELS["text"]
    best_kind, best_score = "text", 0
    for kind, patterns, _extensions in LANGUAGE_SIGNATURES:
        score = sum(1 for pattern in patterns if re.search(pattern, code, re.IGNORECASE | re.MULTILINE))
        if score > best_score:
            best_kind, best_score = kind, score
    if best_score == 0 and hint:
        kind = (hint or "").lower()
        if kind in LANGUAGE_LABELS:
            return kind, LANGUAGE_LABELS[kind]
    return best_kind, LANGUAGE_LABELS[best_kind]


IMPORT_PATTERNS = {
    "python": re.compile(r"^\s*(?:from\s+([\w.]+)\s+import\s+(.+)|import\s+(\S+)([^\n]*))", re.MULTILINE),
    "javascript": re.compile(r"^\s*import\s+(?:.*?from\s+)?[\"\']([^\"\']+)[\"\']|^\s*const\s+\w+\s*=\s*require\([\"\']([^\"\']+)[\"\']\)", re.MULTILINE),
    "java": re.compile(r"^\s*import\s+([\w.*]+);", re.MULTILINE),
    "csharp": re.compile(r"^\s*using\s+([\w.]+);", re.MULTILINE),
    "cpp": re.compile(r'^\s*#include\s*[<"]([^>"]+)[>"]', re.MULTILINE),
    "powershell": re.compile(r"^\s*(?:Import-Module|using\s+module)\s+([\w.\\/-]+)", re.MULTILINE | re.IGNORECASE),
    "bash": re.compile(r"^\s*(?:source|\.)\s+(\S+)", re.MULTILINE),
}


def extract_imports(code, kind):
    """Return the set of imported module names for the given language."""
    pattern = IMPORT_PATTERNS.get(kind)
    if pattern is None:
        return set()
    names = set()
    for match in pattern.finditer(code or ""):
        if kind == "python":
            if match.group(1):                       # from X import a, b
                names.add(match.group(1).split(".")[0])
                for part in (match.group(2) or "").split(","):
                    cleaned = part.strip().split(" as ")[0].strip()
                    if cleaned:
                        names.add(cleaned)
            else:                                    # import a, b  /  import a.b
                for part in (match.group(3) + (match.group(4) or "")).split(","):
                    cleaned = part.strip().split(" as ")[0].strip()
                    if cleaned:
                        names.add(cleaned.split(".")[0])
            continue
        for group in match.groups():
            if group:
                names.add(group.strip())
    return {name for name in names if name}


DEFINITION_PATTERNS = {
    "python": re.compile(r"^(\s*)(?:def)\s+(\w+)\s*(\([^)]*\))?", re.MULTILINE),
    "javascript": re.compile(r"^(\s*)(?:function\s+(\w+)|(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?(?:function|\())", re.MULTILINE),
    "java": re.compile(r"^(\s*)(?:public|private|protected|static|final|\s)*([\w<>\[\]]+)\s+(\w+)\s*(\([^)]*\))", re.MULTILINE),
    "csharp": re.compile(r"^(\s*)(?:public|private|protected|static|internal|\s)*(?:class|void|[\w<>\[\]]+)\s+(\w+)\s*(\([^)]*\))?", re.MULTILINE),
    "cpp": re.compile(r"^(\s*)(?:[\w:<>\*&]+\s+)+(\w+)\s*(\([^)]*\))", re.MULTILINE),
    "powershell": re.compile(r"^(\s*)function\s+([\w-]+)\s*(\([^)]*\))?", re.MULTILINE | re.IGNORECASE),
    "bash": re.compile(r"^(\s*)(?:function\s+)?([\w-]+)\s*\(\s*\)", re.MULTILINE),
    "vbscript": re.compile(r"^(\s*)(?:Sub|Function)\s+(\w+)\s*(\([^)]*\))?", re.MULTILINE | re.IGNORECASE),
}


def extract_functions(code, kind):
    """Return function definitions as (name, params) for the given language."""
    pattern = DEFINITION_PATTERNS.get(kind)
    if pattern is None:
        return []
    functions = []
    for match in pattern.finditer(code or ""):
        groups = [g for g in match.groups() if g is not None]
        # Last named group is the function name, an earlier ()-group the params.
        name = None
        params = ""
        for group in reversed(groups):
            if group.startswith("("):
                params = group
                continue
            if re.match(r"^[\w-]+$", group):
                name = group
                break
        if name:
            functions.append((name, params))
    return functions


def extract_classes(code, kind):
    """Return class names found in the code (language aware)."""
    if kind == "python":
        return [m.group(1) for m in re.finditer(r"^\s*class\s+(\w+)", code or "", re.MULTILINE)]
    if kind in ("javascript",):
        return [m.group(1) for m in re.finditer(r"^\s*class\s+(\w+)", code or "", re.MULTILINE)]
    if kind in ("java", "csharp", "cpp"):
        return [m.group(1) for m in re.finditer(r"\b(?:class|interface|struct|enum)\s+(\w+)", code or "")]
    return []


def _params_of(params_text):
    inner = (params_text or "").strip().strip("()")
    if not inner:
        return []
    parts = [p.strip() for p in inner.split(",")]
    names = []
    for part in parts:
        token = part.split("=")[0].strip().split()[-1] if part.strip() else ""
        if token:
            names.append(token)
    return names


def compare_code_ast(left_code, right_code, left_name="", right_name=""):
    """Structured AST-level comparison of two code bodies.

    Returns a dict describing what changed at the level of functions, classes,
    imports and parameters, which the Analysis view renders as a report.
    """
    left_kind, _ = detect_language_kind(left_code, left_name)
    right_kind, _ = detect_language_kind(right_code, right_name)

    left_funcs = dict(extract_functions(left_code, left_kind))
    right_funcs = dict(extract_functions(right_code, right_kind))
    left_classes = set(extract_classes(left_code, left_kind))
    right_classes = set(extract_classes(right_code, right_kind))
    left_imports = extract_imports(left_code, left_kind)
    right_imports = extract_imports(right_code, right_kind)

    functions_added = sorted(set(right_funcs) - set(left_funcs))
    functions_removed = sorted(set(left_funcs) - set(right_funcs))
    functions_common = sorted(set(left_funcs) & set(right_funcs))
    functions_changed = sorted(
        name for name in functions_common
        if _params_of(left_funcs[name]) != _params_of(right_funcs[name])
    )

    parameter_deltas = []
    for name in functions_common:
        before = _params_of(left_funcs[name])
        after = _params_of(right_funcs[name])
        if before != after:
            parameter_deltas.append((name, before, after))

    return {
        "left_kind": left_kind,
        "right_kind": right_kind,
        "imports_added": sorted(right_imports - left_imports),
        "imports_removed": sorted(left_imports - right_imports),
        "classes_added": sorted(right_classes - left_classes),
        "classes_removed": sorted(left_classes - right_classes),
        "functions_added": functions_added,
        "functions_removed": functions_removed,
        "functions_changed": functions_changed,
        "parameter_deltas": parameter_deltas,
        "renames": renamed_definitions(left_code, right_code),
    }


def detect_refactorings(left_code, right_code, left_name="", right_name=""):
    """Detect rename / move / extract refactorings between two code bodies.

    - rename: same body, different name.
    - move:   the same block appears at a different line on the other side.
    - extract: a block that exists inline on one side appears as a new
      function on the other.
    """
    events = []
    left_kind, _ = detect_language_kind(left_code, left_name)
    right_kind, _ = detect_language_kind(right_code, right_name)
    left_funcs = dict(extract_functions(left_code, left_kind))
    right_funcs = dict(extract_functions(right_code, right_kind))
    left_lines = (left_code or "").splitlines()
    right_lines = (right_code or "").splitlines()

    # --- rename: identical bodies under different names -------------------
    left_bodies = {name: _function_body_text(left_code, left_kind, name) for name in left_funcs}
    right_bodies = {name: _function_body_text(right_code, right_kind, name) for name in right_funcs}
    for old_name, body in left_bodies.items():
        if not body.strip():
            continue
        for new_name, new_body in right_bodies.items():
            if new_name == old_name:
                continue
            if body == new_body:
                events.append({"kind": "rename", "detail": f"{old_name} -> {new_name}"})
                break

    # --- move: an unchanged line block relocated --------------------------
    common = [line for line in left_lines if line.strip() and line in right_lines]
    moved = 0
    for line in common:
        if left_lines.index(line) != right_lines.index(line):
            moved += 1
    if moved >= 3:
        events.append({"kind": "move",
                       "detail": f"{moved} unchanged line(s) at a different position"})

    # --- extract: a new function whose body already existed inline --------
    for new_name in sorted(set(right_funcs) - set(left_funcs)):
        body = right_bodies.get(new_name, "")
        if body and body in (left_code or ""):
            events.append({"kind": "extract",
                           "detail": f"inline block extracted into {new_name}()"})
    return events


def _function_body_text(code, kind, name):
    """Indented (or braced) body of a named function, without its signature."""
    for match in re.finditer(
            r"(?:def|function|Sub|Function)\s+" + re.escape(name) + r"\b", code or "", re.IGNORECASE):
        end = _python_block_end(code, match.start())
        block = code[match.start():end]
        lines = block.splitlines()
        return "\n".join(line.strip() for line in lines[1:] if line.strip())
    return ""


def detect_duplicate_blocks(left_code, right_code, min_lines=4):
    """Find blocks that appear more than once across both sides.

    Returns (block_text, [locations]) for every block of at least `min_lines`
    consecutive non-blank lines that shows up in more than one place.
    """
    def blocks_of(lines, side):
        # A sliding window over every run of `min_lines` non-blank lines, so a
        # duplicated snippet is found wherever it sits, not only between blanks.
        found = []
        stripped = [("" if not line.strip() else line.strip()) for line in lines]
        for start in range(len(stripped) - min_lines + 1):
            window = stripped[start:start + min_lines]
            if "" in window:
                continue
            found.append(("\n".join(window), (side, start + 1)))
        return found
    seen = {}
    for block, location in blocks_of((left_code or "").splitlines(), "left"):
        seen.setdefault(block, []).append(location)
    for block, location in blocks_of((right_code or "").splitlines(), "right"):
        seen.setdefault(block, []).append(location)
    duplicates = []
    for block, locations in seen.items():
        if len(locations) > 1:
            duplicates.append((block, locations))
    duplicates.sort(key=lambda item: len(item[0].splitlines()), reverse=True)
    # Keep only the widest block for each location set, so the report does not
    # list every shorter sub-window of the same repeated run.
    longest = []
    covered = set()
    for block, locations in duplicates:
        key = frozenset(locations)
        if key in covered:
            continue
        covered.add(key)
        longest.append((block, locations))
    return longest


def search_in_rows(rows, needle, ignore_case=True):
    """Return the row indices whose text contains `needle`."""
    if not needle:
        return []
    query = needle.lower() if ignore_case else needle
    hits = []
    for index, row in enumerate(rows):
        for text in (row.get("left"), row.get("right")):
            haystack = (text or "").lower() if ignore_case else (text or "")
            if haystack and query in haystack:
                hits.append(index)
                break
    return hits


def directory_tree(root):
    """Return a sorted relative-path -> absolute-path map for a directory."""
    mapping = {}
    for base, _dirs, files in os.walk(root):
        for name in files:
            full = os.path.join(base, name)
            mapping[os.path.relpath(full, root).replace("\\", "/")] = full
    return collections.OrderedDict(sorted(mapping.items()))


def compare_directories(left_root, right_root):
    """Compare two directory trees; returns per-file status rows."""
    left = directory_tree(left_root)
    right = directory_tree(right_root)
    rows = []
    for path in sorted(set(left) | set(right)):
        in_left = path in left
        in_right = path in right
        if in_left and not in_right:
            status = "only-left"
        elif in_right and not in_left:
            status = "only-right"
        else:
            try:
                with open(left[path], "rb") as handle:
                    a = handle.read()
                with open(right[path], "rb") as handle:
                    b = handle.read()
                status = "same" if a == b else "different"
            except OSError:
                status = "error"
        # Every path produces exactly one row, whatever its status, and the
        # loop must run to the end: returning inside it made the Folders view
        # show at most one file.
        rows.append({
            "path": path,
            "status": status,
            "left": left.get(path, ""),
            "right": right.get(path, ""),
        })
    return rows
# ---------------------------------------------------------------------------
# Special diff modes: three/four-way merge, live, system configuration,
# environment comparison and a binary hex view.
# ---------------------------------------------------------------------------


def merge_three_way(base, ours, theirs):
    """Line-level three-way merge of `ours` and `theirs` against `base`.

    Returns a list of result rows, one per output line, each carrying:
      - ``tag``      : "ours", "theirs", "both" or "conflict" (both sides
                       changed the same region).
      - ``text``     : the resulting text for non-conflict rows.
      - ``ours`` / ``theirs`` : the competing versions for a conflict row.
    """
    base = (base or "").splitlines()
    ours = (ours or "").splitlines()
    theirs = (theirs or "").splitlines()
    rows = []

    import difflib
    ours_ops = difflib.SequenceMatcher(a=base, b=ours, autojunk=False).get_opcodes()
    theirs_ops = difflib.SequenceMatcher(a=base, b=theirs, autojunk=False).get_opcodes()

    ours_regions = [(i1, i2, ours[j1:j2])
                    for op, i1, i2, j1, j2 in ours_ops if op != "equal"]
    theirs_regions = [(i1, i2, theirs[j1:j2])
                      for op, i1, i2, j1, j2 in theirs_ops if op != "equal"]

    def next_event(index_a, index_b):
        # Which side's region starts first (or overlaps) from the cursor on.
        a_start = ours_regions[index_a][0] if index_a < len(ours_regions) else None
        b_start = theirs_regions[index_b][0] if index_b < len(theirs_regions) else None
        if a_start is None and b_start is None:
            return None
        if b_start is None or (a_start is not None and a_start <= b_start):
            return "ours"
        return "theirs"

    ai = bi = 0
    while ai < len(ours_regions) or bi < len(theirs_regions):
        side = next_event(ai, bi)
        if side == "ours":
            a_start, a_end, a_lines = ours_regions[ai]
            # Overlapping theirs region -> potential conflict.
            if bi < len(theirs_regions) and theirs_regions[bi][0] < a_end:
                b_start, b_end, b_lines = theirs_regions[bi]
                if a_lines == b_lines:
                    rows.append({"tag": "both", "text": "\n".join(a_lines),
                                 "ours": a_lines, "theirs": b_lines})
                else:
                    rows.append({"tag": "conflict", "text": "",
                                 "ours": a_lines, "theirs": b_lines,
                                 "ours_range": (a_start, a_end),
                                 "theirs_range": (b_start, b_end)})
                ai += 1
                bi += 1
                continue
            rows.append({"tag": "ours", "text": "\n".join(a_lines),
                         "ours": a_lines, "theirs": a_lines})
            ai += 1
        else:
            b_start, b_end, b_lines = theirs_regions[bi]
            rows.append({"tag": "theirs", "text": "\n".join(b_lines),
                         "ours": b_lines, "theirs": b_lines})
            bi += 1
    return rows


def merge_four_way(base, ours, theirs, extra):
    """Four-way merge: base vs ours vs theirs vs an extra (fourth) side.

    Returns (rows, conflicts). `rows` is the three-way merge of ours/theirs
    against base, then any line where the extra side also diverged is flagged
    as an additional conflict against the merged result.
    """
    rows = merge_three_way(base, ours, theirs)
    extra_lines = (extra or "").splitlines()
    merged_text = "\n".join(row.get("text", "") for row in rows)
    conflicts = [i for i, row in enumerate(rows) if row["tag"] == "conflict"]
    # A fourth side conflicts when it differs from the merged result too; it is
    # reported as an extra conflict row so the reviewer can decide.
    if extra_lines:
        import difflib
        merged_lines = merged_text.splitlines()
        for op, i1, i2, j1, j2 in difflib.SequenceMatcher(
                a=merged_lines, b=extra_lines, autojunk=False).get_opcodes():
            if op == "equal":
                continue
            conflicts.append(len(rows))
            rows.append({
                "tag": "conflict",
                "text": "",
                "ours": merged_lines[i1:i2],
                "theirs": extra_lines[j1:j2],
                "fourth": True,
            })
    return rows, conflicts


def environment_snapshot():
    """Return the current process environment as a sorted list of K=V lines."""
    return [f"{key}={value}" for key, value in sorted(os.environ.items(),
                                                      key=lambda kv: kv[0].lower())]


def system_configuration():
    """Collect a comparable snapshot of the system configuration as text lines.

    Everything here is best-effort and read-only, so it works on any platform
    without extra dependencies.
    """
    lines = ["# ScriptDB system configuration",
             f"# captured: {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
             "",
             "[platform]",
             f"system={os.name}",
             f"platform={sys.platform}",
             f"python={sys.version.split()[0]}",
             f"executable={sys.executable}",
             f"cwd={os.getcwd()}",
             "",
             "[environment]"]
    for key, value in sorted(os.environ.items(), key=lambda kv: kv[0].lower()):
        lines.append(f"{key}={value}")
    return lines


def hex_dump_rows(data, other, base_offset=0, width=16, limit=512):
    """Build a side-by-side hex+ASCII diff view of two byte blobs.

    Returns rows with the offset, each side's hex bytes, each side's ASCII and
    a ``changed`` flag, so the hex view can highlight differing bytes.
    """
    data = data or b""
    other = other or b""
    rows = []
    span = max(len(data), len(other))
    for base in range(0, span, width):
        if len(rows) >= limit:
            break
        chunk_a = data[base:base + width]
        chunk_b = other[base:base + width]
        rows.append({
            "offset": base + base_offset,
            "left_hex": " ".join(f"{b:02x}" for b in chunk_a),
            "right_hex": " ".join(f"{b:02x}" for b in chunk_b),
            "left_ascii": "".join(chr(b) if 32 <= b < 127 else "." for b in chunk_a),
            "right_ascii": "".join(chr(b) if 32 <= b < 127 else "." for b in chunk_b),
            "changed": chunk_a != chunk_b,
        })
    return rows


class DiffMinimap(QtWidgets.QWidget):
    """A narrow strip that shows where the changed blocks sit in a document."""

    WIDTH = 14

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(self.WIDTH)
        self.setSizePolicy(QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Expanding)
        self._rows = []
        self._view_row = 0
        self.setToolTip("Changed blocks overview")

    def set_rows(self, rows):
        self._rows = rows or []
        self.update()

    def set_view_row(self, row):
        self._view_row = max(0, int(row))
        self.update()

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        try:
            painter.fillRect(self.rect(), QtGui.QColor("#1b1b1b"))
            total = max(1, len(self._rows))
            height = max(1, self.height())
            colors = {
                "insert": "#2ea043",
                "delete": "#f85149",
                "replace": "#d29922",
            }
            for index, row in enumerate(self._rows):
                color = colors.get(row.get("tag"))
                if not color:
                    continue
                y = int(index * height / total)
                h = max(2, int(height / total))
                painter.fillRect(1, y, self.WIDTH - 2, h, QtGui.QColor(color))
            marker_y = int(self._view_row * height / total)
            painter.setPen(QtGui.QPen(QtGui.QColor("#8ab4f8"), 1))
            painter.drawLine(0, marker_y, self.WIDTH, marker_y)
        finally:
            painter.end()

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton and self._rows:
            ratio = event.pos().y() / max(1, self.height())
            row = int(ratio * len(self._rows))
            parent = self.parent()
            if hasattr(parent, "scroll_to_row"):
                parent.scroll_to_row(row)
            event.accept()
class DiffTab(QtWidgets.QWidget):
    """Professional diff viewer with several views and a rich control panel.

    Views: side-by-side, unified, inline, change blocks, AST tree and a
    directory-tree comparison. It also supports drag & drop of files, a
    history of recent comparisons and snapshots taken from the script list.
    """

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.setAcceptDrops(True)
        self._left = self._blank_source("(left)")
        self._right = self._blank_source("(right)")
        self._rows = []
        self._diff_positions = []
        self._current_pos = -1
        self._recent = []
        self._syncing_scroll = False
        self._build_ui()

    # --------------------------------------------------------------- helpers
    @staticmethod
    def _blank_source(name):
        return {"name": name, "path": "", "code": "", "category": "Other"}

    def t(self, key, fallback=None, **kwargs):
        return self.window.t(key, fallback, **kwargs)

    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)

        # ---- file panel: drop area, pickers and history ---------------
        file_row = QtWidgets.QHBoxLayout()
        self.left_picker = QtWidgets.QComboBox()
        self.left_picker.setMinimumWidth(230)
        self.right_picker = QtWidgets.QComboBox()
        self.right_picker.setMinimumWidth(230)
        self.left_picker.currentIndexChanged.connect(self.pick_left)
        self.right_picker.currentIndexChanged.connect(self.pick_right)
        self.btn_swap = QtWidgets.QPushButton("Swap")
        self.btn_swap.clicked.connect(self.swap_sides)
        file_row.addWidget(QtWidgets.QLabel("Left:"))
        file_row.addWidget(self.left_picker, 1)
        file_row.addSpacing(8)
        file_row.addWidget(QtWidgets.QLabel("Right:"))
        file_row.addWidget(self.right_picker, 1)
        file_row.addWidget(self.btn_swap)
        layout.addLayout(file_row)

        files_row = QtWidgets.QHBoxLayout()
        self.btn_open_left = QtWidgets.QPushButton("Open left file...")
        self.btn_open_right = QtWidgets.QPushButton("Open right file...")
        self.btn_open_dirs = QtWidgets.QPushButton("Compare folders...")
        self.btn_stdin = QtWidgets.QPushButton("From stdin...")
        self.btn_snapshot = QtWidgets.QPushButton("Snapshot ->")
        # Cross-tab: send this comparison to the Audit tab for review.
        self.btn_audit = QtWidgets.QPushButton("Audit comparison")
        self.recent_combo = QtWidgets.QComboBox()
        self.recent_combo.setMinimumWidth(200)
        self.recent_combo.addItem("Recent comparisons")
        self.recent_combo.activated.connect(self.load_recent)
        self.drop_hint = QtWidgets.QLabel("Drop files here (drag & drop)")
        self.drop_hint.setStyleSheet("color:#9aa0a6;")
        self.btn_open_left.clicked.connect(lambda: self.open_file("left"))
        self.btn_open_right.clicked.connect(lambda: self.open_file("right"))
        self.btn_open_dirs.clicked.connect(self.open_directories)
        self.btn_stdin.clicked.connect(self.load_from_stdin)
        self.btn_snapshot.clicked.connect(self.load_snapshot)
        self.btn_audit.clicked.connect(self.audit_comparison)
        files_row.addWidget(self.btn_open_left)
        files_row.addWidget(self.btn_open_right)
        files_row.addWidget(self.btn_open_dirs)
        files_row.addWidget(self.btn_stdin)
        files_row.addWidget(self.btn_snapshot)
        files_row.addWidget(self.btn_audit)
        files_row.addStretch()
        files_row.addWidget(self.drop_hint)
        files_row.addWidget(self.recent_combo)
        layout.addLayout(files_row)

        # ---- sessions: several diffs open at once ----------------------
        sessions_row = QtWidgets.QHBoxLayout()
        self.session_combo = QtWidgets.QComboBox()
        self.session_combo.setMinimumWidth(180)
        self.session_combo.activated.connect(self.switch_session)
        self.btn_new_session = QtWidgets.QPushButton("New session")
        self.btn_close_session = QtWidgets.QPushButton("Close session")
        self.btn_new_session.clicked.connect(lambda: self.new_session())
        self.btn_close_session.clicked.connect(self.close_session)
        sessions_row.addWidget(QtWidgets.QLabel("Sessions:"))
        sessions_row.addWidget(self.session_combo, 1)
        sessions_row.addWidget(self.btn_new_session)
        sessions_row.addWidget(self.btn_close_session)
        self.btn_stats = QtWidgets.QPushButton("Statistics")

        # A single "Export..." button whose menu holds every export target, so
        # the toolbar stays tidy as more formats are added.
        self.btn_export = QtWidgets.QToolButton()
        self.btn_export.setText("Export...")
        self.btn_export.setPopupMode(QtWidgets.QToolButton.MenuButtonPopup)
        self.export_menu = QtWidgets.QMenu(self.btn_export)
        self.act_export_patch = self.export_menu.addAction("Patch (.patch)")
        self.act_export_json = self.export_menu.addAction("JSON (.json)")
        self.act_export_txt = self.export_menu.addAction("TXT report (.txt)")
        self.export_menu.addSeparator()
        self.act_export_diag = self.export_menu.addAction("Diagnostics (.json)")
        self.act_export_patch.triggered.connect(lambda: self.export_report(".patch"))
        self.act_export_json.triggered.connect(lambda: self.export_report(".json"))
        self.act_export_txt.triggered.connect(lambda: self.export_report(".txt"))
        self.act_export_diag.triggered.connect(self.export_diagnostics)
        self.btn_export.setMenu(self.export_menu)
        # The button itself opens the menu (the arrow toggles it as well).
        self.btn_export.clicked.connect(self.show_export_menu)

        # A single "Modes..." button exposing the special diff modes.
        self.btn_modes = QtWidgets.QToolButton()
        self.btn_modes.setText("\U0001f9ea Modes...")
        self.btn_modes.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.modes_menu = QtWidgets.QMenu(self.btn_modes)
        self.mode_actions = {}
        for key, label_key in DIFF_SPECIAL_MODES:
            action = self.modes_menu.addAction(label_key)
            action.triggered.connect(lambda _checked=False, k=key: self.activate_mode(k))
            self.mode_actions[key] = action
        self.btn_modes.setMenu(self.modes_menu)

        self.btn_stats.clicked.connect(self.show_statistics)
        sessions_row.addSpacing(12)
        sessions_row.addWidget(self.btn_modes)
        sessions_row.addWidget(self.btn_stats)
        sessions_row.addWidget(self.btn_export)
        layout.addLayout(sessions_row)
        # ---- control panel: algorithm, options, modes, views ----------
        control = QtWidgets.QHBoxLayout()
        self.algorithm_combo = QtWidgets.QComboBox()
        for key, label in (("myers", "Myers"), ("patience", "Patience"), ("histogram", "Histogram")):
            self.algorithm_combo.addItem(label, key)
        self.algorithm_combo.currentIndexChanged.connect(self.recompute)

        self.mode_combo = QtWidgets.QComboBox()
        for key, label in (("text", "Text"), ("token", "Token"), ("bytes", "Bytes")):
            self.mode_combo.addItem(label, key)
        self.mode_combo.currentIndexChanged.connect(self.recompute)

        self.chk_ws = QtWidgets.QCheckBox("Ignore whitespace")
        self.chk_comments = QtWidgets.QCheckBox("Ignore comments")
        self.chk_blank = QtWidgets.QCheckBox("Ignore blank lines")
        self.chk_case = QtWidgets.QCheckBox("Ignore case")
        for chk in (self.chk_ws, self.chk_comments, self.chk_blank, self.chk_case):
            chk.toggled.connect(self.recompute)

        self.chk_fast = QtWidgets.QCheckBox("Fast mode")
        self.chk_fast.toggled.connect(self.recompute)

        control.addWidget(QtWidgets.QLabel("Algorithm:"))
        control.addWidget(self.algorithm_combo)
        control.addWidget(QtWidgets.QLabel("Compare:"))
        control.addWidget(self.mode_combo)
        control.addSpacing(10)
        control.addWidget(self.chk_ws)
        control.addWidget(self.chk_comments)
        control.addWidget(self.chk_blank)
        control.addWidget(self.chk_case)
        control.addSpacing(10)
        control.addWidget(self.chk_fast)
        layout.addLayout(control)

        # ---- view selector + navigation --------------------------------
        views = QtWidgets.QHBoxLayout()
        self.view_buttons = {}
        view_group = QtWidgets.QButtonGroup(self)
        for key, label in (("side", "Side-by-side"), ("unified", "Unified"),
                           ("inline", "Inline"), ("blocks", "Blocks"),
                           ("ast", "AST"), ("analysis", "Analysis"),
                           ("files", "Folders")):
            button = QtWidgets.QRadioButton(label)
            view_group.addButton(button)
            views.addWidget(button)
            self.view_buttons[key] = button
        # The "side" view is preselected at the very end of _build_ui(): its
        # toggled signal must not fire while self.stack is still missing.
        self._view_signal_bound = False
        views.addStretch()
        self.btn_prev = QtWidgets.QPushButton("Prev change")
        self.btn_next = QtWidgets.QPushButton("Next change")
        self.btn_wrap = QtWidgets.QPushButton("Wrap: off")
        self.btn_wrap.setCheckable(True)
        self.btn_wrap.toggled.connect(self.toggle_wrap)
        self.btn_prev.clicked.connect(lambda: self.goto_change(-1))
        self.btn_next.clicked.connect(lambda: self.goto_change(1))
        views.addWidget(self.btn_prev)
        views.addWidget(self.btn_next)
        views.addWidget(self.btn_wrap)
        layout.addLayout(views)

        # ---- change list sidebar + filters + search -------------------
        filters = QtWidgets.QHBoxLayout()
        self.filter_added = QtWidgets.QCheckBox("Added")
        self.filter_removed = QtWidgets.QCheckBox("Removed")
        self.filter_changed = QtWidgets.QCheckBox("Changed")
        self.filter_symbols = QtWidgets.QCheckBox("In functions")
        for chk in (self.filter_added, self.filter_removed,
                    self.filter_changed, self.filter_symbols):
            chk.setChecked(True)
            chk.toggled.connect(self.refresh_change_list)
        self.collapse_blocks = QtWidgets.QCheckBox("Collapse unchanged")
        self.collapse_blocks.toggled.connect(self.refresh_view)
        self.low_graphics = QtWidgets.QCheckBox("Low graphics")
        self.low_graphics.toggled.connect(self.refresh_view)
        filters.addWidget(QtWidgets.QLabel("Show:"))
        filters.addWidget(self.filter_added)
        filters.addWidget(self.filter_removed)
        filters.addWidget(self.filter_changed)
        filters.addWidget(self.filter_symbols)
        filters.addSpacing(10)
        filters.addWidget(self.collapse_blocks)
        filters.addWidget(self.low_graphics)

        self.search_edit = QtWidgets.QLineEdit()
        self.search_edit.setPlaceholderText("Search in diff...")
        self.search_edit.textChanged.connect(self.search_changes)
        self.search_prev = QtWidgets.QPushButton("Prev hit")
        self.search_next = QtWidgets.QPushButton("Next hit")
        self.search_prev.clicked.connect(lambda: self.goto_search(-1))
        self.search_next.clicked.connect(lambda: self.goto_search(1))
        filters.addStretch()
        filters.addWidget(self.search_edit, 1)
        filters.addWidget(self.search_prev)
        filters.addWidget(self.search_next)
        layout.addLayout(filters)

        self.progress_bar = QtWidgets.QProgressBar()
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)

        self.summary = QtWidgets.QLabel("")
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet(
            "background:#2d2d2d; border:1px solid #404040; padding:5px;"
            " color:#bfc7d5; font-family:Consolas, monospace;"
        )
        layout.addWidget(self.summary)
        # ---- result area: a stack of the different views ----------------
        body = QtWidgets.QHBoxLayout()
        self.stack = QtWidgets.QStackedWidget()

        # side-by-side view
        self.side_splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.left_view = QtWidgets.QPlainTextEdit()
        self.right_view = QtWidgets.QPlainTextEdit()
        for view, colour in ((self.left_view, "#241c1c"), (self.right_view, "#1c241c")):
            view.setReadOnly(True)
            view.setFont(QFont("Consolas", 10))
            view.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
            view.setStyleSheet(f"background:{colour}; color:#dfe7f5;")
        self.left_view.verticalScrollBar().valueChanged.connect(
            lambda v: self._sync_scroll(self.left_view, self.right_view, v))
        self.right_view.verticalScrollBar().valueChanged.connect(
            lambda v: self._sync_scroll(self.right_view, self.left_view, v))
        self.left_view.cursorPositionChanged.connect(self._update_minimap_marker)
        self.side_splitter.addWidget(self.left_view)
        self.side_splitter.addWidget(self.right_view)
        self.side_splitter.setSizes([480, 480])
        self.stack.addWidget(self.side_splitter)          # index 0 side

        # unified view
        self.unified_view = QtWidgets.QPlainTextEdit()
        self.unified_view.setReadOnly(True)
        self.unified_view.setFont(QFont("Consolas", 10))
        self.unified_view.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
        self.unified_view.setStyleSheet("background:#1f1f1f; color:#dfe7f5;")
        self.stack.addWidget(self.unified_view)           # index 1 unified

        # inline view
        self.inline_view = QtWidgets.QPlainTextEdit()
        self.inline_view.setReadOnly(True)
        self.inline_view.setFont(QFont("Consolas", 10))
        self.inline_view.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
        self.inline_view.setStyleSheet("background:#1f1f1f; color:#dfe7f5;")
        self.stack.addWidget(self.inline_view)            # index 2 inline

        # blocks view (collapse/expand unchanged sections)
        self.block_tree = QtWidgets.QTreeWidget()
        self.block_tree.setAlternatingRowColors(True)
        self.block_tree.setColumnCount(2)
        self.block_tree.setHeaderLabels(["Change", "Preview"])
        self.block_tree.setFont(QFont("Consolas", 10))
        self.stack.addWidget(self.block_tree)             # index 3 blocks

        # AST view
        self.ast_tree = QtWidgets.QTreeWidget()
        self.ast_tree.setAlternatingRowColors(True)
        self.ast_tree.setColumnCount(3)
        self.ast_tree.setHeaderLabels(["Symbol", "Kind", "Status"])
        self.stack.addWidget(self.ast_tree)               # index 4 ast

        # analysis view (language, AST diff, refactors, duplicates)
        self.analysis_tree = QtWidgets.QTreeWidget()
        self.analysis_tree.setAlternatingRowColors(True)
        self.analysis_tree.setColumnCount(2)
        self.analysis_tree.setHeaderLabels(["Item", "Detail"])
        self.analysis_tree.setFont(QFont("Consolas", 10))
        self.stack.addWidget(self.analysis_tree)          # index 5 analysis

        # directory view
        self.dir_tree = QtWidgets.QTreeWidget()
        self.dir_tree.setAlternatingRowColors(True)
        self.dir_tree.setColumnCount(3)
        self.dir_tree.setHeaderLabels(["Path", "Status", "Left / Right"])
        self.dir_tree.itemDoubleClicked.connect(self._open_dir_entry)
        self.stack.addWidget(self.dir_tree)               # index 6 files
        # ---- special modes: merge (3/4-way), live, system, env, hex ----
        # Each special mode is a plain widget so it can be added to the same
        # QStackedWidget the ordinary views use.
        self.merge3_panel = self._build_merge_panel(three_way=True)
        self.stack.addWidget(self.merge3_panel)           # index 7 merge3
        self.merge4_panel = self._build_merge_panel(three_way=False)
        self.stack.addWidget(self.merge4_panel)           # index 8 merge4
        self.live_panel = self._build_live_panel()
        self.stack.addWidget(self.live_panel)             # index 9 live
        self.sysrest_panel = self._build_collect_panel("sysrest")
        self.stack.addWidget(self.sysrest_panel)          # index 10 sysrest
        self.env_panel = self._build_collect_panel("env")
        self.stack.addWidget(self.env_panel)              # index 11 env
        self.hex_panel = self._build_hex_panel()
        self.stack.addWidget(self.hex_panel)              # index 12 hex
        self.change_list = QtWidgets.QListWidget()
        self.change_list.setMinimumWidth(220)
        self.change_list.currentRowChanged.connect(self.change_list_selected)
        self.change_panel = QtWidgets.QWidget()
        change_layout = QtWidgets.QVBoxLayout(self.change_panel)
        change_layout.addWidget(QtWidgets.QLabel("Changes"))
        change_layout.addWidget(self.change_list)
        body.addWidget(self.change_panel)
        body.addWidget(self.stack, 1)
        self.minimap = DiffMinimap(self)
        body.addWidget(self.minimap)
        layout.addLayout(body, 1)

        self.status = QtWidgets.QLabel("")
        layout.addWidget(self.status)

        for button in self.view_buttons.values():
            button.toggled.connect(self.refresh_view)
        self._view_signal_bound = True
        self.view_buttons["side"].setChecked(True)

        self.refresh_pickers()
        self.recompute()

    # ---------------------------------------------------- special mode UI
    def _build_merge_panel(self, three_way=True):
        """Build the three-/four-way merge panel with pickers and results."""
        panel = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(panel)
        rows = (("base", "diff_merge_base"), ("ours", "diff_merge_ours"),
                ("theirs", "diff_merge_theirs"))
        if not three_way:
            rows = rows + (("extra", "diff_merge_ours"),)
        pickers = {}
        for slot, label_key in rows:
            row = QtWidgets.QHBoxLayout()
            row.addWidget(QtWidgets.QLabel(self.t(label_key)))
            combo = QtWidgets.QComboBox()
            combo.setMinimumWidth(220)
            row.addWidget(combo, 1)
            open_btn = QtWidgets.QPushButton(self.t("diff_open_file"))
            open_btn.clicked.connect(lambda _c=False, s=slot: self._merge_open(s))
            row.addWidget(open_btn)
            pickers[slot] = combo
            layout.addLayout(row)
        out_row = QtWidgets.QHBoxLayout()
        out_row.addWidget(QtWidgets.QLabel(self.t("diff_merge_result")))
        out_row.addStretch()
        run_btn = QtWidgets.QPushButton(self.t("diff_merge_run"))
        run_btn.clicked.connect(lambda: self.run_merge())
        out_row.addWidget(run_btn)
        take = QtWidgets.QComboBox()
        take.addItem(self.t("diff_merge_take_left"), "left")
        take.addItem(self.t("diff_merge_take_right"), "right")
        take.addItem(self.t("diff_merge_take_both"), "both")
        out_row.addWidget(take)
        resolve_btn = QtWidgets.QPushButton("OK")
        resolve_btn.setToolTip("Resolve selected conflict")
        resolve_btn.clicked.connect(lambda: self.resolve_merge_conflict(take.currentData()))
        out_row.addWidget(resolve_btn)
        layout.addLayout(out_row)
        tree = QtWidgets.QTreeWidget()
        tree.setAlternatingRowColors(True)
        tree.setColumnCount(4)
        tree.setHeaderLabels(["Tag", "Ours", "Theirs", "Result"])
        tree.setFont(QFont("Consolas", 10))
        layout.addWidget(tree, 1)
        layout.addWidget(QtWidgets.QLabel("Pick two sides (with a common base) and press Merge."))
        panel.merge_three_way = three_way
        panel.merge_slots = {}          # slot -> source dict
        panel.merge_pickers = pickers
        panel.merge_tree = tree
        panel.merge_take = take
        return panel
    def _build_live_panel(self):
        """Build the live diff panel: in-memory buffer vs a file on disk."""
        panel = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(panel)
        top = QtWidgets.QHBoxLayout()
        panel.live_path = QtWidgets.QLabel(self.t("diff_live_no_file"))
        open_btn = QtWidgets.QPushButton(self.t("diff_open_file"))
        live_refresh = QtWidgets.QPushButton(self.t("diff_live_refresh"))
        auto = QtWidgets.QCheckBox(self.t("diff_live_auto"))
        top.addWidget(panel.live_path, 1)
        top.addWidget(open_btn)
        top.addWidget(live_refresh)
        top.addWidget(auto)
        layout.addLayout(top)
        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        memory = QtWidgets.QPlainTextEdit()
        memory.setPlaceholderText(self.t("diff_live_memory"))
        memory.setFont(QFont("Consolas", 10))
        disk = QtWidgets.QPlainTextEdit()
        disk.setReadOnly(True)
        disk.setFont(QFont("Consolas", 10))
        splitter.addWidget(memory)
        splitter.addWidget(disk)
        splitter.setSizes([480, 480])
        layout.addWidget(splitter, 1)
        panel.live_memory = memory
        panel.live_file_view = disk
        open_btn.clicked.connect(self.live_open_file)
        live_refresh.clicked.connect(self.refresh_live_diff)
        memory.textChanged.connect(self.refresh_live_diff)
        timer = QtCore.QTimer(panel)
        timer.setInterval(1000)
        timer.timeout.connect(self.refresh_live_diff)
        panel.live_timer = timer
        auto.toggled.connect(lambda on: timer.start() if on else timer.stop())
        panel.live_auto = auto
        return panel
    def _build_collect_panel(self, kind):
        """Build the environment / system-configuration collector panel."""
        panel = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(panel)
        top = QtWidgets.QHBoxLayout()
        run_btn = QtWidgets.QPushButton(
            self.t("diff_env_run" if kind == "env" else "diff_sysrest_run"))
        hint = QtWidgets.QLabel(
            self.t("diff_env_hint" if kind == "env" else "diff_sysrest_hint"))
        hint.setStyleSheet("color:#9aa0a6;")
        top.addWidget(run_btn)
        top.addWidget(hint, 1)
        layout.addLayout(top)
        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        left = QtWidgets.QPlainTextEdit()
        right = QtWidgets.QPlainTextEdit()
        for view in (left, right):
            view.setReadOnly(True)
            view.setFont(QFont("Consolas", 10))
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setSizes([480, 480])
        layout.addWidget(splitter, 1)
        panel.collect_kind = kind
        panel.collect_left = left
        panel.collect_right = right
        run_btn.clicked.connect(lambda: self.collect_special(kind))
        return panel
    def _build_hex_panel(self):
        """Build the binary hex diff panel."""
        panel = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(panel)
        top = QtWidgets.QHBoxLayout()
        open_left = QtWidgets.QPushButton(self.t("diff_open_file") + " (L)")
        open_right = QtWidgets.QPushButton(self.t("diff_open_file") + " (R)")
        refresh = QtWidgets.QPushButton(self.t("diff_live_refresh"))
        hint = QtWidgets.QLabel(self.t("diff_hex_hint"))
        hint.setStyleSheet("color:#9aa0a6;")
        top.addWidget(open_left)
        top.addWidget(open_right)
        top.addWidget(refresh)
        top.addWidget(hint, 1)
        layout.addLayout(top)
        tree = QtWidgets.QTreeWidget()
        tree.setAlternatingRowColors(True)
        tree.setColumnCount(5)
        tree.setHeaderLabels([self.t("diff_hex_offset"), self.t("diff_hex_side"),
                              self.t("diff_hex_bytes"), self.t("diff_hex_ascii"), ""])
        tree.setFont(QFont("Consolas", 10))
        layout.addWidget(tree, 1)
        panel.hex_tree = tree
        panel.hex_bytes = {"left": b"", "right": b""}
        open_left.clicked.connect(lambda: self.hex_open_file("left"))
        open_right.clicked.connect(lambda: self.hex_open_file("right"))
        refresh.clicked.connect(self.refresh_hex_diff)
        return panel
    # ------------------------------------------------------ special modes
    def activate_mode(self, key):
        """Switch to a special mode from the Modes... menu."""
        if key in DIFF_VIEWS:
            self.view_buttons_stack_mode = key
            # The special modes have no radio button; drive the stack directly.
            index = DIFF_VIEWS.index(key)
            self.stack.setCurrentIndex(index)
            self._current_special = key
            if key in ("merge3", "merge4"):
                self._fill_merge_pickers(key)
            elif key == "live":
                self.refresh_live_diff()
            elif key in ("sysrest", "env"):
                self.collect_special(key)
            elif key == "hex":
                self.refresh_hex_diff()
            self.status.setText(self.t(dict(DIFF_SPECIAL_MODES)[key]))

    def _current_special_mode(self):
        return getattr(self, "_current_special", None)

    # ---- three/four-way merge
    def _merge_field_sources(self):
        return [self._left, self._right]

    def _fill_merge_pickers(self, key):
        panel = self.merge4_panel if key == "merge4" else self.merge3_panel
        for slot, combo in panel.merge_pickers.items():
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("-", None)
            for script in self.window.scripts_only():
                combo.addItem(script.get("name", ""), ("script", script.get("id")))
            combo.blockSignals(False)
            # Sensible defaults: base=left, ours=right, theirs=first script.
            if slot == "base":
                combo.setCurrentIndex(min(1, combo.count() - 1))
            elif slot == "ours":
                combo.setCurrentIndex(min(2, combo.count() - 1))
            elif slot == "theirs":
                combo.setCurrentIndex(min(1, combo.count() - 1))

    def _merge_open(self, slot):
        panel = self.merge4_panel if getattr(self, "_current_special", "") == "merge4" \
            else self.merge3_panel
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, self.t("diff_open_file"), "", self.t("all_source_files"))
        if not path:
            return
        source = self._read_source(path, slot)
        panel.merge_slots[slot] = source
        panel.merge_pickers[slot].blockSignals(True)
        panel.merge_pickers[slot].insertItem(0, source["name"] + "  (file)", ("file", id(source)))
        panel.merge_pickers[slot].setCurrentIndex(0)
        panel.merge_pickers[slot].blockSignals(False)

    def _merge_slot_text(self, panel, slot):
        # A file opened explicitly wins, otherwise read the picker's script.
        if slot in panel.merge_slots:
            return panel.merge_slots[slot].get("code", "")
        combo = panel.merge_pickers.get(slot)
        if combo is None:
            return ""
        data = combo.currentData()
        entry = self._source_for_data(data)
        return entry.get("code", "") if entry else ""

    def run_merge(self):
        """Compute the merge and fill the result tree."""
        key = getattr(self, "_current_special", "merge3")
        panel = self.merge4_panel if key == "merge4" else self.merge3_panel
        base = self._merge_slot_text(panel, "base")
        ours = self._merge_slot_text(panel, "ours")
        theirs = self._merge_slot_text(panel, "theirs")
        tree = panel.merge_tree
        tree.clear()
        if key == "merge4":
            extra = self._merge_slot_text(panel, "extra")
            rows, conflicts = merge_four_way(base, ours, theirs, extra)
        else:
            rows = merge_three_way(base, ours, theirs)
            conflicts = [i for i, r in enumerate(rows) if r["tag"] == "conflict"]
        colours = {"both": "#9be9a8", "ours": "#8ab4f8", "theirs": "#c586c0",
                   "conflict": "#ff9a9a"}
        for row in rows:
            item = QtWidgets.QTreeWidgetItem([
                row["tag"],
                "\n".join(row.get("ours") if isinstance(row.get("ours"), list)
                            else [row.get("ours", "")]),
                "\n".join(row.get("theirs") if isinstance(row.get("theirs"), list)
                            else [row.get("theirs", "")]),
                row.get("text", ""),
            ])
            item.setForeground(0, QtGui.QColor(colours.get(row["tag"], "#dfe7f5")))
            tree.addTopLevelItem(item)
        tree.expandAll()
        if conflicts:
            self.status.setText(self.t("diff_merge_conflict", count=len(conflicts)))
        else:
            self.status.setText(self.t("diff_merge_clean"))
        panel.merge_conflicts = conflicts
        panel.merge_result_rows = rows
    def resolve_merge_conflict(self, choice):
        """Apply the chosen resolution to every conflict in the result tree."""
        key = getattr(self, "_current_special", "merge3")
        panel = self.merge4_panel if key == "merge4" else self.merge3_panel
        rows = getattr(panel, "merge_result_rows", None)
        if not rows:
            return
        for row in rows:
            if row["tag"] != "conflict":
                continue
            ours = row.get("ours") or []
            theirs = row.get("theirs") or []
            if choice == "left":
                row["text"] = "\n".join(ours)
            elif choice == "right":
                row["text"] = "\n".join(theirs)
            else:
                row["text"] = "\n".join(list(ours) + list(theirs))
            row["tag"] = "resolved"
        panel.merge_tree.clear()
        colours = {"both": "#9be9a8", "ours": "#8ab4f8", "theirs": "#c586c0",
                   "conflict": "#ff9a9a", "resolved": "#9be9a8"}
        for row in rows:
            item = QtWidgets.QTreeWidgetItem([
                row["tag"],
                "\n".join(row.get("ours") if isinstance(row.get("ours"), list)
                            else [row.get("ours", "")]),
                "\n".join(row.get("theirs") if isinstance(row.get("theirs"), list)
                            else [row.get("theirs", "")]),
                row.get("text", ""),
            ])
            item.setForeground(0, QtGui.QColor(colours.get(row["tag"], "#dfe7f5")))
            panel.merge_tree.addTopLevelItem(item)
        panel.merge_tree.expandAll()
        self.status.setText(self.t("diff_merge_clean"))

    # ---- live diff
    def live_open_file(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, self.t("diff_open_file"), "", self.t("all_source_files"))
        if not path:
            return
        self._live_path = path
        self.live_panel.live_path.setText(self.t("diff_live_file", name=path))
        self.refresh_live_diff()

    def refresh_live_diff(self):
        """Re-read the watched file and compare it with the in-memory buffer."""
        panel = self.live_panel
        memory = panel.live_memory.toPlainText()
        path = getattr(self, "_live_path", "")
        if not path or not os.path.isfile(path):
            panel.live_file_view.setPlainText(self.t("diff_live_no_file"))
            return
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                disk = handle.read()
        except OSError as exc:
            panel.live_file_view.setPlainText(str(exc))
            return
        self._left = {"name": self.t("diff_live_memory"), "path": "",
                      "code": memory, "category": "Other"}
        self._right = {"name": os.path.basename(path), "path": path,
                       "code": disk, "category": category_for_extension(path) or "Other"}
        rows = align_lines(memory.splitlines(), disk.splitlines(), "myers")
        marker = {"insert": "+", "delete": "-", "replace": "~", "equal": " "}
        panel.live_file_view.setPlainText("\n".join(
            f"{marker.get(r['tag'], ' ')} "
            f"{r.get('right') if r['tag'] == 'insert' else r.get('left')}"
            for r in rows))

    # ---- environment / system configuration
    def collect_special(self, kind):
        """Snapshot the current environment or system configuration."""
        panel = self.env_panel if kind == "env" else self.sysrest_panel
        lines = environment_snapshot() if kind == "env" else system_configuration()
        panel.collect_left.setPlainText("\n".join(lines))
        panel.collect_right.setPlainText("\n".join(lines))
        self.status.setText(self.t("diff_env_run" if kind == "env" else "diff_sysrest_run"))

    # ---- binary hex diff
    def hex_open_file(self, side):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, self.t("diff_open_file"), "", self.t("all_source_files"))
        if not path:
            return
        try:
            with open(path, "rb") as handle:
                data = handle.read()
        except OSError as exc:
            QtWidgets.QMessageBox.warning(self, self.t("warning"), str(exc))
            return
        self.hex_panel.hex_bytes[side] = data
        self.refresh_hex_diff()

    def refresh_hex_diff(self):
        """Rebuild the hex + ASCII diff tree from the two loaded blobs."""
        panel = self.hex_panel
        left = panel.hex_bytes.get("left", b"")
        right = panel.hex_bytes.get("right", b"")
        panel.hex_tree.clear()
        if not left and not right:
            return
        for row in hex_dump_rows(left, right):
            item = QtWidgets.QTreeWidgetItem([
                f"{row['offset']:08x}", "L", row["left_hex"], row["left_ascii"], ""])
            item2 = QtWidgets.QTreeWidgetItem([
                f"{row['offset']:08x}", "R", row["right_hex"], row["right_ascii"], ""])
            colour = "#ff9a9a" if row["changed"] else "#9be9a8"
            item.setForeground(1, QtGui.QColor(colour))
            item2.setForeground(1, QtGui.QColor(colour))
            panel.hex_tree.addTopLevelItem(item)
            panel.hex_tree.addTopLevelItem(item2)

    def audit_comparison(self):
        audit = getattr(self.window, "audit_tab", None)
        if audit is None or not hasattr(audit, "on_file_changed"):
            self.window.show_message(self.t("info"),
                                     self.t("audit_unavailable"))
            return
        left = self._left.get("name", "")
        right = self._right.get("name", "")
        # Record the comparison and send it to the audit as a change event.
        audit.on_file_changed("%s vs %s" % (left, right),
                              self.t("audit_diff_open", "diff reviewed"),
                              self._left.get("code", ""),
                              self._right.get("code", ""))
        self.window.open_audit_tab("audit_tab_timeline")

    # --------------------------------------------------------------- pickers
    def refresh_pickers(self):
        """Fill both pickers with the stored scripts and their revisions."""
        for combo in (self.left_picker, self.right_picker):
            previous = combo.currentData()
            combo.blockSignals(True)
            combo.clear()
            for script in self.window.scripts_only():
                combo.addItem(script.get("name", ""), ("script", script.get("id")))
                revisions = script.get("history", []) or []
                for index in range(len(revisions)):
                    label = f"    {script.get('name', '')} #{len(revisions) - index}"
                    combo.addItem(label, ("script", script.get("id"), index))
            combo.blockSignals(False)
            index = self._combo_index(combo, previous)
            if index >= 0:
                combo.setCurrentIndex(index)

    @staticmethod
    def _combo_index(combo, data):
        for index in range(combo.count()):
            if combo.itemData(index) == data:
                return index
        return -1

    def _source_for_data(self, data):
        if not data:
            return None
        script = self.window.find_script(data[1])
        if script is None:
            return None
        if len(data) > 2:
            revisions = script.get("history", []) or []
            index = data[2]
            if 0 <= index < len(revisions):
                entry = dict(revisions[index])
                entry.setdefault("name", script.get("name", ""))
                return entry
        return script

    def _entry_to_source(self, entry):
        return {
            "name": entry.get("name", ""),
            "path": "",
            "code": entry.get("code", ""),
            "category": entry.get("category", "Python"),
        }
    # ------------------------------------------------------------- options
    def options(self):
        return {
            "algorithm": self.algorithm_combo.currentData() or "myers",
            "mode": self.mode_combo.currentData() or "text",
            "ignore_ws": self.chk_ws.isChecked(),
            "ignore_comments": self.chk_comments.isChecked(),
            "ignore_blank": self.chk_blank.isChecked(),
            "ignore_case": self.chk_case.isChecked(),
            "fast": self.chk_fast.isChecked(),
        }

    def set_sources(self, left, right):
        """Set both sides at once (used by drag & drop and snapshots)."""
        self._left = left
        self._right = right
        self.drop_hint.setText(f"{left['name']}  vs  {right['name']}")
        self.recompute()

    # ------------------------------------------------------------- diffing
    def recompute(self, *args):
        opts = self.options()
        left_code = self._left.get("code", "") or ""
        right_code = self._right.get("code", "") or ""
        algorithm = "myers" if opts["fast"] else opts["algorithm"]

        if opts["mode"] == "bytes":
            self._rows = []
            self._byte_report = bytes_diff_lines(
                left_code.encode("utf-8", "replace"),
                right_code.encode("utf-8", "replace"),
            )
        elif opts["mode"] == "token":
            self._byte_report = None
            self._rows, _lt, _rt = token_diff_lines(left_code, right_code, algorithm)
        else:
            self._byte_report = None
            language = self._left.get("category", "")
            left_lines = prepare_diff_lines(
                left_code, opts["ignore_case"], opts["ignore_ws"],
                opts["ignore_blank"], opts["ignore_comments"], language)
            right_lines = prepare_diff_lines(
                right_code, opts["ignore_case"], opts["ignore_ws"],
                opts["ignore_blank"], opts["ignore_comments"], language)
            self._rows = align_lines(left_lines, right_lines, algorithm)

        self._diff_positions = [
            index for index, row in enumerate(self._rows)
            if row.get("tag") != "equal"
        ]
        self._current_pos = -1
        self.minimap.set_rows(self._rows)
        self._update_summary()
        self._update_symbol_flags()
        self._update_progress()
        self.refresh_view()
        self.refresh_change_list()
        self.search_changes()
        if hasattr(self, "session_manager"):
            self.snapshot_session()
        # Tell any registered plugin that this comparison finished, and log
        # every change block so a terminal front-end can audit the diff.
        # _blocks is read defensively: it is set by refresh_change_list above,
        # but that call bails out early when the sidebar is missing.
        blocks = getattr(self, "_blocks", [])
        HOOKS.fire("before_diff", left=self._left, right=self._right,
                   options=self.options())
        for start, block in blocks:
            HOOKS.fire("change_log", start=start, rows=block)
        HOOKS.fire("after_diff", report={
            "left": self._left, "right": self._right, "rows": self._rows,
            "blocks": blocks,
        })

    def _update_summary(self):
        added = sum(1 for row in self._rows if row.get("tag") == "insert")
        removed = sum(1 for row in self._rows if row.get("tag") == "delete")
        changed = sum(1 for row in self._rows if row.get("tag") == "replace")
        same = sum(1 for row in self._rows if row.get("tag") == "equal")
        identical = (added + removed + changed) == 0
        colour = "#9be9a8" if identical else "#f7c66d"
        self.summary.setStyleSheet(
            "background:#2d2d2d; border:1px solid #404040; padding:5px;"
            f" color:{colour}; font-family:Consolas, monospace;"
        )
        if self.options()["mode"] == "bytes":
            self.summary.setText(
                f"{self._left['name']}  vs  {self._right['name']}  |  "
                f"byte differences: {len(self._byte_report or [])}"
            )
        else:
            self.summary.setText(
                f"{self._left['name']}  vs  {self._right['name']}  |  "
                f"identical: {same}  changed: {changed}  added: {added}  "
                f"removed: {removed}  |  changes: {added + removed + changed}"
            )
    def retranslate(self, table):
        """Apply the language table to the Diff tab's own controls."""
        def label(key, fallback):
            return table.get(key, fallback)
        self.filter_added.setText(label("diff_filter_added", "Added"))
        self.filter_removed.setText(label("diff_filter_removed", "Removed"))
        self.filter_changed.setText(label("diff_filter_changed", "Changed"))
        self.filter_symbols.setText(label("diff_filter_symbols", "In functions"))
        self.collapse_blocks.setText(label("diff_collapse", "Collapse unchanged"))
        self.low_graphics.setText(label("diff_low_graphics", "Low graphics"))
        self.search_edit.setPlaceholderText(label("diff_search", "Search in diff..."))
        self.search_prev.setText(label("diff_prev_hit", "Prev hit"))
        self.search_next.setText(label("diff_next_hit", "Next hit"))
        self.btn_swap.setText(label("compare_swap", "Swap"))
        self.btn_prev.setText(label("compare_prev", "Prev diff"))
        self.btn_next.setText(label("compare_next", "Next diff"))
        if "analysis" in self.view_buttons:
            self.view_buttons["analysis"].setText(label("diff_view_analysis", "Analysis"))
        self.btn_stats.setText(label("statistics", "Statistics"))
        self.btn_export.setText(label("diff_export_menu", "Export..."))
        self.act_export_patch.setText(label("diff_export_patch", "Patch (.patch)"))
        self.act_export_json.setText(label("diff_export_json", "JSON (.json)"))
        self.act_export_txt.setText(label("diff_export_txt", "TXT report (.txt)"))
        self.act_export_diag.setText(label("diff_diagnostics", "Diagnostics (.json)"))
        self.btn_new_session.setText(label("diff_new_session", "New session"))
        self.btn_close_session.setText(label("diff_close_session", "Close session"))
        self.btn_modes.setText(label("diff_modes_menu", "\U0001f9ea Modes..."))
        for key, label_key in DIFF_SPECIAL_MODES:
            if key in self.mode_actions:
                self.mode_actions[key].setText(label(label_key, label_key))

    # ------------------------------------------------- change list & filters
    def _row_passes_filters(self, row):
        kind = change_kind(row)
        if kind == "added" and not self.filter_added.isChecked():
            return False
        if kind == "removed" and not self.filter_removed.isChecked():
            return False
        if kind == "changed" and not self.filter_changed.isChecked():
            return False
        if self.filter_symbols.isChecked():
            # Only keep changes that live inside a known symbol, or that look
            # like a definition rename.
            flags = getattr(self, "_symbol_spans", None) or []
            index = row.get("_index", -1)
            if not (0 <= index < len(flags)) or not flags[index]:
                return False
        return True

    def _current_symbol_names(self, row_index):
        """Symbols whose document range covers the given row (left or right)."""
        names = []
        for offsets, symbols in ((self._left_line_offsets, self._left_symbols),
                                 (self._right_line_offsets, self._right_symbols)):
            if not symbols or not offsets:
                continue
            index = max(0, min(len(offsets) - 1, row_index))
            offset = offsets[index]
            names.extend(symbols_in_range(symbols, offset, offset + 1))
        return sorted(set(names))

    @staticmethod
    def _line_start_offsets(code):
        # Character offset where each 1-based line begins. Built once per
        # comparison, not inside the per-row loop.
        offsets = []
        running = 0
        for line in (code or "").splitlines():
            offsets.append(running)
            running += len(line) + 1
        return offsets

    def refresh_change_list(self, *_args):
        """Rebuild the sidebar list of changes, honouring the filters."""
        if not hasattr(self, "change_list"):
            return
        self.change_list.blockSignals(True)
        self.change_list.clear()
        self._blocks = iter_change_blocks(self._rows)
        shown = 0
        for index, (start, rows) in enumerate(self._blocks):
            first = rows[0]
            first["_index"] = start
            if not self._row_passes_filters(first):
                continue
            added = sum(1 for r in rows if change_kind(r) == "added")
            removed = sum(1 for r in rows if change_kind(r) == "removed")
            changed = sum(1 for r in rows if change_kind(r) == "changed")
            names = self._current_symbol_names(start)
            label = f"#{index + 1}  +{added} -{removed} ~{changed}"
            if names:
                label += "  [" + ", ".join(names) + "]"
            item = QtWidgets.QListWidgetItem(label)
            colours = {"added": "#9be9a8", "removed": "#ff9a9a", "changed": "#f7c66d"}
            key = "added" if added else ("removed" if removed else "changed")
            item.setForeground(QtGui.QColor(colours.get(key, "#dfe7f5")))
            item.setData(QtCore.Qt.UserRole, start)
            self.change_list.addItem(item)
            shown += 1
        self.change_list.blockSignals(False)
        total = len(self._blocks)
        self.status.setText(f"Changes: {shown} / {total} shown")
        self._update_symbol_flags()

    def _update_symbol_flags(self):
        """Mark which rows sit inside a function/class (for the symbol filter)."""
        language = self._left.get("category", "")
        left = self._left.get("code", "") or ""
        right = self._right.get("code", "") or ""
        self._left_symbols = ast_symbols(left, language)
        self._right_symbols = ast_symbols(right, language)
        # Line start offsets are cached here so the per-row symbol lookup in
        # _current_symbol_names stays linear instead of rescanning the code.
        self._left_line_offsets = self._line_start_offsets(left)
        self._right_line_offsets = self._line_start_offsets(right)
        flags = []
        for index, row in enumerate(self._rows):
            inside = bool(self._current_symbol_names(index))
            definition = _structural_name_change(row.get("left"), row.get("right"))
            flags.append(inside or definition)
        self._symbol_spans = flags

    def change_list_selected(self, row):
        if row < 0:
            return
        item = self.change_list.item(row)
        if item is None:
            return
        start = item.data(QtCore.Qt.UserRole)
        if self.current_view() == "unified":
            self._scroll_view(self.unified_view, start)
        elif self.current_view() == "inline":
            self._scroll_view(self.inline_view, start)
        else:
            self._scroll_view(self.left_view, start)
            self._scroll_view(self.right_view, start)
        self.minimap.set_view_row(start)
        self._current_pos = -1

    # ------------------------------------------------------------ searching
    def search_changes(self, _text=None):
        """Highlight every row matching the search box and list the count."""
        needle = self.search_edit.text().strip()
        self._search_hits = search_in_rows(self._rows, needle)
        self._search_pos = -1
        self._apply_search_highlight()
        if needle:
            self.status.setText(
                f"Found {len(self._search_hits)} line(s) for '{needle}'"
            )

    def _search_views(self):
        # The panes a search hit is painted in.
        return (self.left_view, self.right_view,
                self.unified_view, self.inline_view)

    def _search_hit_lines(self, view):
        # Row indices whose text appears in a view, in that view's numbering.
        #
        # A diff row index is a block number only in the side-by-side panes. The
        # unified pane adds a file header and one @@ marker per hunk, and the
        # inline pane prints two blocks for every replaced line, so a shared
        # index highlighted the wrong line there.

        if view is self.left_view or view is self.right_view:
            return set(self._search_hits)
        wanted = []
        for row_index in self._search_hits:
            wanted.extend(self._search_row_texts(row_index, view))
        return self._texts_in_view(view, wanted)

    def _search_row_texts(self, row_index, view):
        # The body lines one diff row contributes to a rendered view.
        if row_index < 0 or row_index >= len(self._rows):
            return []
        row = self._rows[row_index]
        tag = row.get("tag")
        if tag == "equal":
            return [row.get("left", "")]
        if tag == "insert":
            return [] if view is self.left_view else [row.get("right", "")]
        if tag == "delete":
            return [] if view is self.right_view else [row.get("left", "")]
        # A replaced row shows the left text in the left pane and the unified
        # pane, and the right text in the right pane.
        if view is self.right_view:
            return [row.get("right", "")]
        if view is self.unified_view:
            return [row.get("left", ""), row.get("right", "")]
        return [row.get("left", "")]

    @staticmethod
    def _texts_in_view(view, wanted):
        # Block numbers whose text matches one of `wanted`, each taken once.
        #
        # Text is matched ignoring case and surrounding whitespace, so the
        # comparison survives the small rewriting each view applies. Only the
        # first block per distinct body text is kept, which keeps the hit list
        # aligned with the diff rows rather than with the number of panes a row
        # happens to be drawn in.
        targets = set()
        for text in wanted:
            stripped = (text or "").strip()
            if stripped:
                targets.add(stripped.lower())
        found = set()
        seen = set()
        document = view.document()
        for index in range(document.blockCount()):
            block = document.findBlockByNumber(index)
            if not block.isValid():
                continue
            key = block.text().strip().lower()
            if not key:
                continue
            # Each pane prefixes the body line with its own gutter ('- ', '+ ',
            # '@@', a line number), so the block is matched when it *ends* with
            # the body text rather than when it equals it.
            match = key in targets or any(key.endswith(t) for t in targets)
            if match and key not in seen:
                seen.add(key)
                found.add(index)
        return found
    def _apply_search_highlight(self):
        # Paint the search hits in every pane, each in its own numbering.
        hits = set(getattr(self, "_search_hits", []))
        if not hits:
            for view in self._search_views():
                view.setExtraSelections([])
            return
        selection_format = QtGui.QTextCharFormat()
        selection_format.setBackground(QtGui.QColor("#5a4b1a"))
        selection_format.setForeground(QtGui.QColor("#ffe08a"))
        for view in self._search_views():
            selections = []
            document = view.document()
            for index in sorted(self._search_hit_lines(view)):
                block = document.findBlockByNumber(index)
                if not block.isValid():
                    continue
                selection = QtWidgets.QTextEdit.ExtraSelection()
                selection.format = selection_format
                cursor = QtGui.QTextCursor(block)
                cursor.select(QtGui.QTextCursor.LineUnderCursor)
                selection.cursor = cursor
                selections.append(selection)
            # The highlight replaces the per-pane selections instead of being
            # appended to them: the panes mark changes by colour, and appending
            # left the change colours behind a stale highlight.
            view.setExtraSelections(selections)

    def goto_search(self, direction):
        hits = getattr(self, "_search_hits", [])
        if not hits:
            return
        self._search_pos = (self._search_pos + direction) % len(hits)
        row_index = hits[self._search_pos]
        if self.current_view() == "unified":
            self._scroll_view(self.unified_view, row_index)
        elif self.current_view() == "inline":
            self._scroll_view(self.inline_view, row_index)
        else:
            self._scroll_view(self.left_view, row_index)
            self._scroll_view(self.right_view, row_index)
        self.status.setText(f"Hit {self._search_pos + 1} / {len(hits)}")

    # ------------------------------------------------------------- progress
    def _update_progress(self):
        if not hasattr(self, "progress_bar"):
            return
        total = max(1, len(self._rows))
        done = sum(1 for row in self._rows if row.get("tag") == "equal")
        self.progress_bar.setValue(int(done * 100 / total))

    # --------------------------------------------------------------- views
    def _apply_low_graphics(self):
        """Switch the panes to a flat, ASCII-friendly look when enabled."""
        if not hasattr(self, "low_graphics"):
            return
        low = self.low_graphics.isChecked()
        plain = "background:#000; color:#e6e6e6;" if low else None
        defaults = {
            self.left_view: "background:#241c1c; color:#dfe7f5;",
            self.right_view: "background:#1c241c; color:#dfe7f5;",
            self.unified_view: "background:#1f1f1f; color:#dfe7f5;",
            self.inline_view: "background:#1f1f1f; color:#dfe7f5;",
        }
        for view, style in defaults.items():
            view.setStyleSheet(plain if low else style)
        if low:
            for view in defaults:
                view.setExtraSelections([])

    def current_view(self):
        for key, button in self.view_buttons.items():
            if button.isChecked():
                return key
        return "side"

    def refresh_view(self, *args):
        view = self.current_view()
        index = DIFF_VIEWS.index(view) if view in DIFF_VIEWS else 0
        self.stack.setCurrentIndex(index)
        self._apply_low_graphics()

        # Every text pane is rebuilt on each pass, not only the one on top.
        # A pane that was never rendered holds a single empty block, so a
        # search hit mapped into it would land nowhere; rendering them all
        # also makes switching views instant, because the shortcuts in
        # goto_change and change_list rely on those documents being current.
        if self.options()["mode"] == "bytes":
            self.render_side_by_side()
            self.render_blocks()
            return
        self.render_side_by_side()
        self.render_unified()
        self.render_inline()
        self.render_blocks()

        if view == "ast":
            self.render_ast()
        elif view == "analysis":
            self.render_analysis()
        elif view == "files":
            self.render_files()
        elif view == "blocks":
            self.render_blocks()
        # The search highlight is painted last so it is applied to the text
        # the panes now hold, not to the text they held before the rebuild.
        self._apply_search_highlight()

    def _backgrounds(self, view, rows, side):
        row_colour = {"delete": "#3a1d1d", "insert": "#17331f", "replace": "#3b3418"}
        grey = "#3a3d41"
        lines = []
        formats = []
        long_run = 0
        for row in rows:
            if side == "left":
                number, text = row["left_no"], row["left"]
            else:
                number, text = row["right_no"], row["right"]
            tag = row.get("tag")
            if tag == "equal" and self.collapse_blocks.isChecked():
                long_run += 1
                if long_run > 6:
                    # Represent a long unchanged stretch with a single marker
                    # instead of printing every line.
                    continue
            else:
                long_run = 0
            arrow = {"insert": "+", "delete": "-", "replace": "~"}.get(tag, " ")
            if self.low_graphics.isChecked():
                prefix = f"{'----' if number else '    '} "
                lines.append(f"{arrow} {prefix}{text}")
                formats.append(None)
                continue
            prefix = f"{number:>4} | " if number else "     | "
            lines.append(prefix + text)
            formats.append(tag)
        view.setPlainText("\n".join(lines))
        selections = []
        document = view.document()
        for index, tag in enumerate(formats):
            colour = row_colour.get(tag)
            if not colour:
                continue
            block = document.findBlockByNumber(index)
            if not block.isValid():
                continue
            selection = QtWidgets.QTextEdit.ExtraSelection()
            selection.format.setBackground(QtGui.QColor(colour))
            selection.format.setProperty(QtGui.QTextFormat.FullWidthSelection, True)
            cursor = QtGui.QTextCursor(block)
            cursor.clearSelection()
            selection.cursor = cursor
            selections.append(selection)
        view.setExtraSelections(selections)

    def render_side_by_side(self):
        if self.options()["mode"] == "bytes":
            report = "\n".join(self._byte_report or [])
            self.left_view.setPlainText(report)
            self.right_view.setPlainText("")
            return
        self._backgrounds(self.left_view, self._rows, "left")
        self._backgrounds(self.right_view, self._rows, "right")

    def render_unified(self):
        import difflib
        left_lines = [row["left"] for row in self._rows if row.get("tag") != "insert"]
        right_lines = [row["right"] for row in self._rows if row.get("tag") != "delete"]
        diff = "\n".join(difflib.unified_diff(
            left_lines, right_lines,
            fromfile=self._left.get("name", "left"),
            tofile=self._right.get("name", "right"), lineterm="",
        ))
        self.unified_view.setPlainText(diff or self.t("diff_identical"))
        self._paint_unified()

    def _paint_unified(self):
        selections = []
        document = self.unified_view.document()
        for index in range(document.blockCount()):
            block = document.findBlockByNumber(index)
            text = block.text()
            colour = None
            if text.startswith(("+++", "---")):
                colour = "#8ab4f8"
            elif text.startswith("+"):
                colour = "#9be9a8"
            elif text.startswith("-"):
                colour = "#ff9a9a"
            elif text.startswith("@@"):
                colour = "#c586c0"
            if not colour:
                continue
            selection = QtWidgets.QTextEdit.ExtraSelection()
            selection.format.setForeground(QtGui.QColor(colour))
            cursor = QtGui.QTextCursor(block)
            cursor.select(QtGui.QTextCursor.LineUnderCursor)
            selection.cursor = cursor
            selections.append(selection)
        self.unified_view.setExtraSelections(selections)
    def render_inline(self):
        """Show each change with word-level highlights inside the line.

        Definition lines whose declared name changed get an extra marker so a
        rename is obvious at a glance (structural change highlight).
        """
        cursor = QtGui.QTextCursor(self.inline_view.document())
        self.inline_view.clear()
        cursor = self.inline_view.textCursor()
        default = QtGui.QTextCharFormat()
        default.setForeground(QtGui.QColor("#dfe7f5"))
        add_fmt = QtGui.QTextCharFormat()
        add_fmt.setBackground(QtGui.QColor("#17331f"))
        add_fmt.setForeground(QtGui.QColor("#9be9a8"))
        del_fmt = QtGui.QTextCharFormat()
        del_fmt.setBackground(QtGui.QColor("#3a1d1d"))
        del_fmt.setForeground(QtGui.QColor("#ff9a9a"))
        word_fmt = QtGui.QTextCharFormat()
        word_fmt.setForeground(QtGui.QColor("#ffe08a"))
        word_fmt.setFontWeight(QFont.Bold)
        plain = QtGui.QTextCharFormat()
        plain.setForeground(QtGui.QColor("#dfe7f5"))
        for row in self._rows:
            tag = row.get("tag")
            if tag == "equal":
                cursor.insertText(f"  {row['left']}\n", plain)
            elif tag == "delete":
                cursor.insertText(f"- {row['left']}\n", del_fmt)
            elif tag == "insert":
                cursor.insertText(f"+ {row['right']}\n", add_fmt)
            else:
                marker = " (renamed)" if _structural_name_change(
                    row.get("left"), row.get("right")) else ""
                cursor.insertText("- ", del_fmt)
                for text, changed in (row.get("left_segments") or [(row["left"], False)]):
                    cursor.insertText(text, word_fmt if changed else del_fmt)
                cursor.insertText(marker + "\n")
                cursor.insertText("+ ", add_fmt)
                for text, changed in (row.get("right_segments") or [(row["right"], False)]):
                    cursor.insertText(text, word_fmt if changed else add_fmt)
                cursor.insertText(marker + "\n")
        self.inline_view.setTextCursor(cursor)
        # The inline body was just rebuilt, so the search highlight painted on
        # the previous text has to follow it.
        self._apply_search_highlight()

    def render_blocks(self):
        """Group the diff into changed blocks separated by unchanged sections."""
        self.block_tree.clear()
        block_index = 0
        current = None
        for row in self._rows:
            if row.get("tag") == "equal":
                if current is not None:
                    self._add_block_group(block_index, current)
                    block_index += 1
                    current = None
                continue
            if current is None:
                current = []
            current.append(row)
        if current is not None:
            self._add_block_group(block_index, current)
        self.block_tree.expandAll()

    def _add_block_group(self, index, rows):
        added = sum(1 for row in rows if row.get("tag") == "insert")
        removed = sum(1 for row in rows if row.get("tag") == "delete")
        changed = sum(1 for row in rows if row.get("tag") == "replace")
        first = rows[0]
        item = QtWidgets.QTreeWidgetItem([
            f"Block {index + 1}  (+{added} / -{removed} / ~{changed})",
            f"line {first.get('left_no') or first.get('right_no') or '?'}",
        ])
        item.setForeground(0, QtGui.QColor("#f7c66d"))
        for row in rows:
            preview = row.get("left") or row.get("right") or ""
            child = QtWidgets.QTreeWidgetItem([
                row.get("tag", ""), preview[:120],
            ])
            colours = {"insert": "#9be9a8", "delete": "#ff9a9a", "replace": "#f7c66d"}
            child.setForeground(0, QtGui.QColor(colours.get(row.get("tag"), "#dfe7f5")))
            item.addChild(child)
        self.block_tree.addTopLevelItem(item)
    def render_ast(self):
        """Show the diff grouped by top-level symbol (function / class)."""
        self.ast_tree.clear()
        language = self._left.get("category", "")
        left_code = self._left.get("code", "") or ""
        right_code = self._right.get("code", "") or ""
        left_symbols = {name: (kind, left_code[a:b])
                        for name, kind, a, b in ast_symbols(left_code, language)}
        right_symbols = {name: (kind, right_code[a:b])
                         for name, kind, a, b in ast_symbols(right_code, language)}
        names = list(dict.fromkeys(list(left_symbols) + list(right_symbols)))
        for name in names:
            if name in left_symbols and name in right_symbols:
                status = "same" if left_symbols[name][1] == right_symbols[name][1] else "changed"
                kind = right_symbols[name][0]
            elif name in left_symbols:
                status, kind = "removed", left_symbols[name][0]
            else:
                status, kind = "added", right_symbols[name][0]
            item = QtWidgets.QTreeWidgetItem([name, kind, status])
            colours = {"same": "#9be9a8", "changed": "#f7c66d",
                       "added": "#9be9a8", "removed": "#ff9a9a"}
            item.setForeground(2, QtGui.QColor(colours.get(status, "#dfe7f5")))
            if status == "changed":
                detail = QtWidgets.QTreeWidgetItem([
                    "diff", "", f"{len(left_symbols[name][1].splitlines())} -> "
                                f"{len(right_symbols[name][1].splitlines())} lines",
                ])
                item.addChild(detail)
            self.ast_tree.addTopLevelItem(item)
        self.ast_tree.expandAll()

    def render_analysis(self):
        """Language, AST-level diff, refactorings and duplicate blocks.

        Everything here is derived from the two code bodies, so the panel
        keeps working for file drops, snapshots and script revisions alike.
        """
        self.analysis_tree.clear()
        left_code = self._left.get("code", "") or ""
        right_code = self._right.get("code", "") or ""
        left_name = self._left.get("name", "")
        right_name = self._right.get("name", "")

        left_kind, left_label = detect_language_kind(left_code, left_name,
                                                     self._left.get("category", ""))
        _right_kind, right_label = detect_language_kind(right_code, right_name,
                                                        self._right.get("category", ""))

        root = QtWidgets.QTreeWidgetItem([
            "Language",
            f"{left_label}  ->  {right_label}" if left_label != right_label
            else left_label,
        ])
        root.setForeground(0, QtGui.QColor("#8ab4f8"))
        self.analysis_tree.addTopLevelItem(root)

        report = compare_code_ast(left_code, right_code, left_name, right_name)
        sections = [
            ("Imports added", report["imports_added"], "#9be9a8"),
            ("Imports removed", report["imports_removed"], "#ff9a9a"),
            ("Classes added", report["classes_added"], "#9be9a8"),
            ("Classes removed", report["classes_removed"], "#ff9a9a"),
            ("Functions added", report["functions_added"], "#9be9a8"),
            ("Functions removed", report["functions_removed"], "#ff9a9a"),
            ("Functions changed", report["functions_changed"], "#f7c66d"),
        ]
        for title, values, colour in sections:
            node = QtWidgets.QTreeWidgetItem([title, str(len(values))])
            node.setForeground(1, QtGui.QColor(colour if values else "#6a737d"))
            if values:
                node.setForeground(0, QtGui.QColor(colour))
                for value in values:
                    node.addChild(QtWidgets.QTreeWidgetItem(["", str(value)]))
            self.analysis_tree.addTopLevelItem(node)

        # Parameter changes are shown as before -> after for each function.
        if report["parameter_deltas"]:
            node = QtWidgets.QTreeWidgetItem(["Parameters changed",
                                              str(len(report["parameter_deltas"]))])
            node.setForeground(0, QtGui.QColor("#f7c66d"))
            for name, before, after in report["parameter_deltas"]:
                child = QtWidgets.QTreeWidgetItem([
                    name, f"({', '.join(before)})  ->  ({', '.join(after)})"])
                child.setForeground(1, QtGui.QColor("#f7c66d"))
                node.addChild(child)
            self.analysis_tree.addTopLevelItem(node)

        # Refactorings: rename / move / extract.
        refactors = detect_refactorings(left_code, right_code, left_name, right_name)
        node = QtWidgets.QTreeWidgetItem(["Refactorings", str(len(refactors))])
        node.setForeground(0, QtGui.QColor("#c586c0"))
        for event in refactors:
            child = QtWidgets.QTreeWidgetItem([event["kind"], event["detail"]])
            child.setForeground(0, QtGui.QColor("#c586c0"))
            node.addChild(child)
        if not refactors:
            placeholder = QtWidgets.QTreeWidgetItem(["", "none detected"])
            placeholder.setForeground(1, QtGui.QColor("#6a737d"))
            node.addChild(placeholder)
        self.analysis_tree.addTopLevelItem(node)

        # Duplicated / moved blocks.
        duplicates = detect_duplicate_blocks(left_code, right_code)
        node = QtWidgets.QTreeWidgetItem(["Duplicate blocks", str(len(duplicates))])
        node.setForeground(0, QtGui.QColor("#f7c66d"))
        for block, locations in duplicates:
            where = ", ".join(f"{side}:{line}" for side, line in locations)
            first_line = block.splitlines()[0] if block else ""
            child = QtWidgets.QTreeWidgetItem([where, first_line[:80]])
            child.setForeground(0, QtGui.QColor("#f7c66d"))
            for text in block.splitlines():
                child.addChild(QtWidgets.QTreeWidgetItem(["", text[:120]]))
            node.addChild(child)
        if not duplicates:
            placeholder = QtWidgets.QTreeWidgetItem(["", "none detected"])
            placeholder.setForeground(1, QtGui.QColor("#6a737d"))
            node.addChild(placeholder)
        self.analysis_tree.addTopLevelItem(node)

        self.analysis_tree.expandAll()

    def render_files(self):
        """Directory comparison view (filled by open_directories)."""
        if not getattr(self, "_dir_rows", None):
            self.dir_tree.clear()
            return
        self.dir_tree.clear()
        colours = {"same": "#9be9a8", "different": "#f7c66d",
                   "only-left": "#ff9a9a", "only-right": "#8ab4f8", "error": "#ff7b72"}
        for row in self._dir_rows:
            item = QtWidgets.QTreeWidgetItem([
                row["path"], row["status"],
                f"{'L' if row['left'] else '-'} / {'R' if row['right'] else '-'}",
            ])
            item.setForeground(1, QtGui.QColor(colours.get(row["status"], "#dfe7f5")))
            item.setData(0, QtCore.Qt.UserRole, row)
            self.dir_tree.addTopLevelItem(item)

    def _open_dir_entry(self, item, _column):
        row = item.data(0, QtCore.Qt.UserRole)
        if not row:
            return
        if row.get("left"):
            self._left = self._read_source(row["left"], "left")
        if row.get("right"):
            self._right = self._read_source(row["right"], "right")
        self.view_buttons["side"].setChecked(True)
        self.recompute()
    # ----------------------------------------------------------- file input
    def _read_source(self, path, side_name):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                code = handle.read()
        except OSError as exc:
            QtWidgets.QMessageBox.warning(self, self.t("warning"), str(exc))
            return self._blank_source(side_name)
        return {
            "name": os.path.basename(path),
            "path": path,
            "code": code,
            "category": category_for_script(path, code),
        }

    def open_file(self, side):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, self.t("diff_open_file"), "", self.t("all_source_files"))
        if not path:
            return
        source = self._read_source(path, side)
        if side == "left":
            self._left = source
        else:
            self._right = source
        self.recompute()

    def open_directories(self):
        left_dir = QtWidgets.QFileDialog.getExistingDirectory(self, self.t("diff_left_dir"))
        if not left_dir:
            return
        right_dir = QtWidgets.QFileDialog.getExistingDirectory(self, self.t("diff_right_dir"))
        if not right_dir:
            return
        self._dir_rows = compare_directories(left_dir, right_dir)
        self.view_buttons["files"].setChecked(True)
        self.render_files()
        self.status.setText(f"{left_dir} <->  {right_dir}")

    def load_from_stdin(self):
        """Read the current stdin content (piped) and compare it with the left."""
        try:
            text = sys.stdin.read()
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, self.t("warning"), str(exc))
            return
        if not text:
            QtWidgets.QMessageBox.information(
                self, self.t("info"), self.t("diff_empty_stdin"))
            return
        self._right = {"name": "stdin", "path": "", "code": text, "category": "Other"}
        self.recompute()

    def load_snapshot(self):
        """Copy the currently selected script on the Scripts tab to the right."""
        script = self.window.selected_script()
        if script is None:
            QtWidgets.QMessageBox.information(
                self, self.t("info"), self.t("select_script"))
            return
        self._right = self._entry_to_source(script)
        self.recompute()

    def pick_left(self, index):
        data = self.left_picker.itemData(index)
        entry = self._source_for_data(data)
        if entry is not None:
            self._left = self._entry_to_source(entry)
            self.recompute()

    def pick_right(self, index):
        data = self.right_picker.itemData(index)
        entry = self._source_for_data(data)
        if entry is not None:
            self._right = self._entry_to_source(entry)
            self.recompute()

    def swap_sides(self):
        self._left, self._right = self._right, self._left
        self.recompute()

    def push_recent(self, label):
        """Remember the current comparison so it can be re-run later."""
        entry = (label, self._left, self._right)
        self._recent = [item for item in self._recent if item[0] != label]
        self._recent.insert(0, entry)
        del self._recent[15:]
        self.recent_combo.blockSignals(True)
        self.recent_combo.clear()
        self.recent_combo.addItem(self.t("diff_recent"))
        for name, _l, _r in self._recent:
            self.recent_combo.addItem(name)
        self.recent_combo.blockSignals(False)

    def load_recent(self, index):
        if index <= 0 or index - 1 >= len(self._recent):
            return
        _label, left, right = self._recent[index - 1]
        self._left, self._right = left, right
        self.recompute()
    # ---------------------------------------------------------- drag & drop
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        paths = [p for p in paths if p]
        if not paths:
            return
        if len(paths) == 1 and os.path.isdir(paths[0]):
            self.status.setText(self.t("diff_need_two_files"))
            return
        if len(paths) == 1:
            # A single file drops onto the right side, keeping the left intact.
            self._right = self._read_source(paths[0], "right")
        else:
            self._left = self._read_source(paths[0], "left")
            self._right = self._read_source(paths[1], "right")
        self.push_recent(f"{self._left['name']}  vs  {self._right['name']}")
        self.recompute()
        event.acceptProposedAction()

    # ---------------------------------------------------------- navigation
    def goto_change(self, direction):
        if not self._diff_positions:
            return
        self._current_pos = (self._current_pos + direction) % len(self._diff_positions)
        row_index = self._diff_positions[self._current_pos]
        if self.current_view() == "unified":
            self._scroll_view(self.unified_view, row_index)
        elif self.current_view() == "inline":
            self._scroll_view(self.inline_view, row_index)
        else:
            self._scroll_view(self.left_view, row_index)
            self._scroll_view(self.right_view, row_index)
        self.minimap.set_view_row(row_index)

    @staticmethod
    def _scroll_view(view, row_index):
        cursor = QtGui.QTextCursor(view.document())
        cursor.movePosition(QtGui.QTextCursor.Start)
        cursor.movePosition(QtGui.QTextCursor.Down, QtGui.QTextCursor.MoveAnchor,
                            max(0, row_index))
        view.setTextCursor(cursor)
        view.ensureCursorVisible()

    def scroll_to_row(self, row_index):
        self._scroll_view(self.left_view, row_index)
        self._scroll_view(self.right_view, row_index)

    def _update_minimap_marker(self):
        self.minimap.set_view_row(self.left_view.textCursor().blockNumber())

    def _sync_scroll(self, source, follower, value):
        if self._syncing_scroll:
            return
        self._syncing_scroll = True
        try:
            follower.verticalScrollBar().setValue(value)
        finally:
            self._syncing_scroll = False

    def toggle_wrap(self, checked):
        mode = (QtWidgets.QPlainTextEdit.WidgetWidth if checked
                else QtWidgets.QPlainTextEdit.NoWrap)
        for view in (self.left_view, self.right_view, self.unified_view, self.inline_view):
            view.setLineWrapMode(mode)
        self.btn_wrap.setText("Wrap: on" if checked else "Wrap: off")

    # ------------------------------------------------------------- reporting
    def current_report(self):
        """Build a report dict for the current sides (used by every export)."""
        stats = None
        rows = getattr(self, "_rows", [])
        report = {
            "left": self._left,
            "right": self._right,
            "rows": rows,
            "blocks": iter_change_blocks(rows),
            "analysis": compare_code_ast(self._left.get("code", "") or "",
                                         self._right.get("code", "") or "",
                                         self._left.get("name", ""),
                                         self._right.get("name", "")),
            "refactors": detect_refactorings(self._left.get("code", "") or "",
                                             self._right.get("code", "") or ""),
            "duplicates": detect_duplicate_blocks(self._left.get("code", "") or "",
                                                  self._right.get("code", "") or ""),
        }
        report["statistics"] = stats or diff_statistics(report)
        return report

    def show_statistics(self):
        """Show the statistics window (counts, percent, histogram, per symbol)."""
        report = self.current_report()
        text = format_statistics(report["statistics"])
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle(self.t("diff_stats_title", "Diff statistics"))
        dialog.resize(520, 480)
        layout = QtWidgets.QVBoxLayout(dialog)
        view = QtWidgets.QPlainTextEdit(text)
        view.setReadOnly(True)
        view.setFont(QFont("Consolas", 10))
        layout.addWidget(view)
        close_btn = QtWidgets.QPushButton("Close")
        close_btn.clicked.connect(dialog.close)
        footer = QtWidgets.QHBoxLayout()
        footer.addStretch()
        footer.addWidget(close_btn)
        layout.addLayout(footer)
        dialog.exec_()

    def show_export_menu(self):
        """Drop the export menu down from the Export... button."""
        self.export_menu.exec_(
            self.btn_export.mapToGlobal(
                QtCore.QPoint(0, self.btn_export.height())))

    def export_report(self, extension=".txt"):
        """Save the current comparison as .patch, .json or .txt."""
        report = self.current_report()
        default = self.t("diff_report_name", "diff_report")
        filters = {
            ".patch": "Patch files (*.patch)",
            ".json": "JSON files (*.json)",
            ".txt": "Text files (*.txt)",
        }
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, self.t("diff_export", "Export report"),
            default + extension, filters.get(extension, "All files (*.*)"))
        if not path:
            return
        try:
            write_report(report, path)
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, self.t("export_error"), str(exc))
            return
        self.window.show_message(self.t("info"),
                                 f"{self.t('exported_file')}{chr(10)}{path}")

    def export_diagnostics(self):
        """Save the diagnostic artefacts (JSON) for other modules."""
        report = self.current_report()
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, self.t("diff_diagnostics", "Diagnostics"),
            self.t("diff_report_name", "diff_report") + "_diag.json",
            "JSON files (*.json)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(diagnostic_artifacts(report), handle,
                          ensure_ascii=False, indent=2)
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, self.t("export_error"), str(exc))
            return
        self.window.show_message(self.t("info"),
                                 f"{self.t('exported_file')}{chr(10)}{path}")

    # -------------------------------------------------------------- sessions
    def _ensure_session_manager(self):
        if not hasattr(self, "session_manager"):
            self.session_manager = SessionManager()
            combo = self.session_combo
            combo.blockSignals(True)
            combo.clear()
            combo.blockSignals(False)
        return self.session_manager

    def new_session(self, title="", activate=True):
        """Open a fresh diff session (keeps the current one in the list)."""
        manager = self._ensure_session_manager()
        session = manager.new_session(title, self._left, self._right)
        self._refresh_session_combo()
        if activate:
            self.session_combo.setCurrentIndex(self.session_combo.count() - 1)
        return session

    def _refresh_session_combo(self):
        manager = self._ensure_session_manager()
        self.session_combo.blockSignals(True)
        self.session_combo.clear()
        for session in manager.sessions.values():
            self.session_combo.addItem(session.title, session.id)
        index = self.session_combo.findData(manager.active_id)
        if index >= 0:
            self.session_combo.setCurrentIndex(index)
        self.session_combo.blockSignals(False)

    def switch_session(self, index):
        if index < 0:
            return
        session_id = self.session_combo.itemData(index)
        manager = self._ensure_session_manager()
        session = manager.activate(session_id)
        if session is None:
            return
        self._left, self._right = session.left, session.right
        self.recompute()

    def close_session(self):
        manager = self._ensure_session_manager()
        if manager.active_id is None:
            return
        manager.remove(manager.active_id)
        self._refresh_session_combo()
        active = manager.active()
        if active is not None:
            self._left, self._right = active.left, active.right
            self.recompute()

    def snapshot_session(self):
        """Store the current sides back into the active session."""
        manager = self._ensure_session_manager()
        session = manager.active()
        if session is not None:
            session.left, session.right = self._left, self._right

    # ------------------------------------------------------------- embedding
    def dock_as_panel(self, container, side="right", width=520):
        """Embed this diff view in a host widget as a side panel.

        Returns the container so a terminal front-end can keep a reference.
        ``side`` is "left" or "right" to choose which edge the panel sits on.
        The widget is detached from any previous parent first, so a front-end
        can switch between the panel/top/tab layouts at run time.
        """
        self.setParent(None)
        self.setParent(container)
        container.setMinimumWidth(width)
        self.show()
        self._embed_side = side
        return container

    def dock_as_top(self, container, height=260):
        """Embed this diff view as a top panel of a host widget."""
        self.setParent(None)
        self.setParent(container)
        container.setMinimumHeight(height)
        self.show()
        self._embed_side = "top"
        return container

    def embed_into_tab(self, tab_widget, title="Diff"):
        """Embed this diff view as a tab of an existing QTabWidget."""
        if tab_widget.indexOf(self) < 0:
            tab_widget.addTab(self, title)
        tab_widget.setCurrentWidget(self)
        self._embed_side = "tab"
        return tab_widget

    def open_paths(self, left_path, right_path):
        """Load two files straight from disk (used by the CLI entry point)."""
        self._left = self._read_source(left_path, "left")
        self._right = self._read_source(right_path, "right")
        self.push_recent(f"{self._left['name']}  vs  {self._right['name']}")
        self.recompute()

    def compare_entry(self, left_entry, right_entry):
        """Entry point used by the Scripts tab ("Compare with...")."""
        self._left = self._entry_to_source(left_entry)
        self._right = self._entry_to_source(right_entry)
        self.push_recent(f"{self._left['name']}  vs  {self._right['name']}")
        self.recompute()

# ===========================================================================
# Terminal integration: module API, hooks, sessions and the CLI entry point.
# ===========================================================================


class DiffHooks:
    """A tiny pub/sub bus so plugins can observe the diff lifecycle.

    Supported events:
      - before_diff : fired right before two sides are compared.
      - after_diff  : fired once the rows are ready.
      - change_log  : fired for every detected change block (audit log).
    """

    def __init__(self):
        self._hooks = {"before_diff": [], "after_diff": [], "change_log": []}

    def register(self, event, callback):
        """Subscribe `callback` to `event`; returns the callback for chaining."""
        if event not in self._hooks:
            self._hooks[event] = []
        self._hooks[event].append(callback)
        return callback

    def unregister(self, event, callback):
        if event in self._hooks and callback in self._hooks[event]:
            self._hooks[event].remove(callback)

    def fire(self, event, **payload):
        """Run every callback registered for `event`, ignoring their errors."""
        import traceback
        for callback in list(self._hooks.get(event, [])):
            try:
                callback(**payload)
            except Exception:
                traceback.print_exc()


# The process-wide bus. A plugin imports this module and calls
# HOOKS.register("after_diff", fn) to be notified after every comparison.
HOOKS = DiffHooks()

# Registry of callables exposed to plugins ("plugin-ready" API surface).
API_REGISTRY = {}


def register_api(name, func=None):
    """Expose func under name in the plugin API registry.

    Usable as register_api("name")(fn) and also directly as
    register_api("name", fn); the one-argument form is kept so existing
    plugin code keeps working.
    """
    if func is None:
        def decorator(real):
            API_REGISTRY[name] = real
            return real
        return decorator
    API_REGISTRY[name] = func
    return func


def read_text_source(path, side_name="file"):
    """Read a file into a diff source dict (used by the CLI and plugins)."""
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        code = handle.read()
    return {
        "name": os.path.basename(path),
        "path": path,
        "code": code,
        "category": category_for_script(path, code),
    }


def compare_files(left_path, right_path, algorithm="myers", mode="text", **flags):
    """Head-less comparison used by the CLI, plugins and tests.

    Fires the before_diff/after_diff/change_log hooks so a plugin sees exactly
    the same lifecycle whether the GUI or a script drives the comparison.
    """
    left = read_text_source(left_path, "left")
    right = read_text_source(right_path, "right")
    HOOKS.fire("before_diff", left=left, right=right, options=flags)

    if mode == "bytes":
        rows = []
    else:
        language = left.get("category", "")
        left_lines = prepare_diff_lines(
            left["code"], flags.get("ignore_case", False), flags.get("ignore_ws", False),
            flags.get("ignore_blank", False), flags.get("ignore_comments", False), language)
        right_lines = prepare_diff_lines(
            right["code"], flags.get("ignore_case", False), flags.get("ignore_ws", False),
            flags.get("ignore_blank", False), flags.get("ignore_comments", False), language)
        rows = align_lines(left_lines, right_lines, algorithm)

    report = {
        "left": left,
        "right": right,
        "rows": rows,
        "blocks": iter_change_blocks(rows),
        "analysis": compare_code_ast(left["code"], right["code"], left["name"], right["name"]),
        "refactors": detect_refactorings(left["code"], right["code"]),
        "duplicates": detect_duplicate_blocks(left["code"], right["code"]),
    }
    for start, block in report["blocks"]:
        HOOKS.fire("change_log", start=start, rows=block)
    HOOKS.fire("after_diff", report=report)
    return report


@register_api("compare_files")
def _api_compare_files(*args, **kwargs):
    return compare_files(*args, **kwargs)


@register_api("diff_tab_class")
def _api_diff_tab_class():
    return DiffTab


@register_api("hooks")
def _api_hooks():
    return HOOKS


@register_api("sessions")
def _api_sessions():
    return SessionManager


@register_api("compare_text")
def compare_text(left_text, right_text, algorithm="myers"):
    """Compare two in-memory strings and return the diff report."""
    left = {"name": "left", "path": "", "code": left_text, "category": "Other"}
    right = {"name": "right", "path": "", "code": right_text, "category": "Other"}
    HOOKS.fire("before_diff", left=left, right=right, options={})
    rows = align_lines(left_text.splitlines(), right_text.splitlines(), algorithm)
    report = {"left": left, "right": right, "rows": rows,
              "blocks": iter_change_blocks(rows)}
    HOOKS.fire("after_diff", report=report)
    return report


def format_report(report, style="text"):
    """Render a diff report as plain text, unified or JSON (for the CLI)."""
    if style == "json":
        return json.dumps({
            "left": report["left"]["name"],
            "right": report["right"]["name"],
            "rows": [{"tag": r["tag"], "left_no": r["left_no"], "right_no": r["right_no"],
                      "left": r["left"], "right": r["right"]} for r in report["rows"]],
            "analysis": report.get("analysis", {}),
        }, ensure_ascii=False, indent=2)
    if style == "unified":
        import difflib
        left_lines = [r["left"] for r in report["rows"] if r["tag"] != "insert"]
        right_lines = [r["right"] for r in report["rows"] if r["tag"] != "delete"]
        return "\n".join(difflib.unified_diff(
            left_lines, right_lines,
            fromfile=report["left"]["name"], tofile=report["right"]["name"], lineterm=""))
    marker = {"insert": "+", "delete": "-", "replace": "~", "equal": " "}
    return chr(10).join(f"{marker.get(r['tag'], ' ')} {r['left'] or r['right']}"
                      for r in report["rows"])


# ---------------------------------------------------------------------------
# Reports and export: statistics, .patch / JSON / TXT writers and diagnostic
# artefacts for other modules.
# ---------------------------------------------------------------------------


def _symbol_for_line(symbols, line_number, code=""):
    """Name of the symbol owning a 1-based line, or "(top level)".

    `ast_symbols` reports character offsets, so the line number is converted
    into a character offset before it is matched against the ranges.
    """
    lines = (code or "").splitlines()
    if not lines:
        return "(top level)"
    index = max(0, min(len(lines) - 1, line_number - 1))
    offset = sum(len(line) + 1 for line in lines[:index])
    for name, _kind, start, end in symbols:
        if start <= offset < end:
            return name
    return "(top level)"


def diff_statistics(report):
    """Compute the numbers every report and histogram is built from."""
    rows = report["rows"]
    added = sum(1 for row in rows if row.get("tag") == "insert")
    removed = sum(1 for row in rows if row.get("tag") == "delete")
    changed = sum(1 for row in rows if row.get("tag") == "replace")
    same = sum(1 for row in rows if row.get("tag") == "equal")
    total = max(1, len(rows))
    changes = added + removed + changed
    left_code = report["left"].get("code", "") or ""
    right_code = report["right"].get("code", "") or ""
    left_lines = max(1, len(left_code.splitlines()))
    right_lines = max(1, len(right_code.splitlines()))

    buckets = {"1": 0, "2-3": 0, "4-10": 0, "11+": 0}
    for _start, rows_in_block in iter_change_blocks(rows):
        size = len(rows_in_block)
        if size == 1:
            buckets["1"] += 1
        elif size <= 3:
            buckets["2-3"] += 1
        elif size <= 10:
            buckets["4-10"] += 1
        else:
            buckets["11+"] += 1

    per_symbol = {}
    left_symbols = ast_symbols(left_code, report["left"].get("category", ""))
    right_symbols = ast_symbols(right_code, report["right"].get("category", ""))
    for row in rows:
        if row.get("tag") == "equal":
            continue
        name = "(top level)"
        if row.get("left_no") is not None:
            name = _symbol_for_line(left_symbols, row["left_no"], left_code)
        elif row.get("right_no") is not None:
            name = _symbol_for_line(right_symbols, row["right_no"], right_code)
        per_symbol[name] = per_symbol.get(name, 0) + 1

    per_file = {}
    for side in ("left", "right"):
        name = report[side].get("name", side) or side
        lines = len((report[side].get("code", "") or "").splitlines())
        per_file[name] = {"lines": lines,
                          "changes": changes if side == "right" else removed}

    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "same": same,
        "total_lines": len(rows),
        "changes": changes,
        "percent_different": round(changes * 100.0 / total, 1),
        "left_lines": left_lines,
        "right_lines": right_lines,
        "histogram": buckets,
        "per_symbol": dict(sorted(per_symbol.items(), key=lambda kv: -kv[1])),
        "per_file": per_file,
    }


def format_statistics(stats):
    """Human readable statistics block shared by the TXT and report writers."""
    lines = [
        "Statistics",
        "----------",
        "Changes: {} (+{} -{} ~{})".format(
            stats["changes"], stats["added"], stats["removed"], stats["changed"]),
        "Unchanged lines: {} / {}".format(stats["same"], stats["total_lines"]),
        "Difference: {}%".format(stats["percent_different"]),
        "Lines: left {} -> right {}".format(stats["left_lines"], stats["right_lines"]),

        "Change histogram (block size -> count)",
    ]
    widest = max(1, max(stats["histogram"].values()))
    for label in ("1", "2-3", "4-10", "11+"):
        count = stats["histogram"][label]
        bar = "#" * int(round(count * 30.0 / widest)) if count else ""
        lines.append("  {:>5} | {} {}".format(label, bar, count))
    if stats["per_symbol"]:
        lines.extend(["", "Changes per function"])
        for name, count in stats["per_symbol"].items():
            lines.append("  {}: {}".format(name, count))
    lines.extend(["", "Changes per file"])
    for name, info in stats["per_file"].items():
        lines.append("  {}: {} change(s), {} line(s)".format(
            name, info["changes"], info["lines"]))
    return chr(10).join(lines)


def report_as_patch(report):
    """Render a unified diff suitable for `patch` / `git apply`."""
    import difflib
    left_lines = [r["left"] for r in report["rows"] if r.get("tag") != "insert"]
    right_lines = [r["right"] for r in report["rows"] if r.get("tag") != "delete"]
    left_name = report["left"].get("name", "left") or "left"
    right_name = report["right"].get("name", "right") or "right"
    # difflib already emits the ---/+++ header, so nothing is prepended here;
    # a real newline is kept as the last byte so the patch ends with a line
    # terminator the way patch(1) expects it.
    body = chr(10).join(difflib.unified_diff(
        left_lines, right_lines, fromfile=left_name, tofile=right_name, lineterm=""))
    return body + chr(10)


def report_as_txt(report, stats=None):
    """Plain ASCII report: header, statistics and the diff body."""
    stats = stats or diff_statistics(report)
    marker = {"insert": "+", "delete": "-", "replace": "~", "equal": " "}
    lines = [
        "ScriptDB diff report",
        "====================",
        "Left : " + report["left"].get("name", ""),
        "Right: " + report["right"].get("name", ""),
        "",
        format_statistics(stats),
        "",
        "Diff",
        "----",
    ]
    for row in report["rows"]:
        tag = row.get("tag")
        text = row.get("right") if tag == "insert" else row.get("left")
        lines.append(marker.get(tag, " ") + " " + (text or ""))
    return chr(10).join(lines) + chr(10)


def report_as_json(report, stats=None):
    """JSON with the diff, the statistics and the code analysis combined."""
    stats = stats or diff_statistics(report)
    return json.dumps({
        "left": {"name": report["left"].get("name", ""),
                 "path": report["left"].get("path", "")},
        "right": {"name": report["right"].get("name", ""),
                  "path": report["right"].get("path", "")},
        "statistics": stats,
        "analysis": report.get("analysis", {}),
        "refactors": report.get("refactors", []),
        "duplicates": [
            {"block": block, "locations": [list(loc) for loc in locations]}
            for block, locations in report.get("duplicates", [])
        ],
        "rows": [
            {"tag": row.get("tag"), "left_no": row.get("left_no"),
             "right_no": row.get("right_no"), "left": row.get("left"),
             "right": row.get("right")}
            for row in report["rows"]
        ],
    }, ensure_ascii=False, indent=2)


REPORT_WRITERS = {
    ".patch": report_as_patch,
    ".diff": report_as_patch,
    ".txt": report_as_txt,
    ".json": report_as_json,
}


def write_report(report, path):
    """Write a report to `path`, picking the writer from its extension."""
    extension = os.path.splitext(path)[1].lower() or ".txt"
    writer = REPORT_WRITERS.get(extension, report_as_txt)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(writer(report))
    return path


def diagnostic_artifacts(report):
    """Build machine readable artefacts for other modules (diagnostics)."""
    stats = diff_statistics(report)
    changes = [
        {"tag": row.get("tag"), "left_no": row.get("left_no"),
         "right_no": row.get("right_no"),
         "text": (row.get("right") if row.get("tag") == "insert" else row.get("left")) or ""}
        for row in report["rows"] if row.get("tag") != "equal"
    ]
    return {
        "version": 1,
        "generator": "ScriptDB diff",
        "summary": {
            "changes": stats["changes"],
            "added": stats["added"],
            "removed": stats["removed"],
            "changed": stats["changed"],
            "percent_different": stats["percent_different"],
        },
        "histogram": stats["histogram"],
        "per_file": stats["per_file"],
        "per_symbol": stats["per_symbol"],
        "analysis": report.get("analysis", {}),
        "refactors": report.get("refactors", []),
        "duplicates": len(report.get("duplicates", [])),
        "changes": changes,
        "status": "clean" if stats["changes"] == 0 else "modified",
    }


@register_api("statistics")
def _api_statistics(report):
    return diff_statistics(report)


@register_api("write_report")
def _api_write_report(report, path):
    return write_report(report, path)


@register_api("diagnostics")
def _api_diagnostics(report):
    return diagnostic_artifacts(report)


class DiffSession:
    """One diff comparison held as an independent, re-openable session."""

    _counter = [0]

    def __init__(self, title="", left=None, right=None):
        DiffSession._counter[0] += 1
        self.id = DiffSession._counter[0]
        self.title = title or f"Session {self.id}"
        self.left = left or {"name": "(left)", "path": "", "code": "", "category": "Other"}
        self.right = right or {"name": "(right)", "path": "", "code": "", "category": "Other"}
        self.options = {}

    def to_dict(self):
        return {"id": self.id, "title": self.title,
                "left": self.left, "right": self.right, "options": self.options}


class SessionManager:
    """Keeps several diff sessions alive at once and tracks the active one."""

    def __init__(self):
        self.sessions = collections.OrderedDict()
        self.active_id = None

    def add(self, session):
        self.sessions[session.id] = session
        self.active_id = session.id
        return session

    def new_session(self, title="", left=None, right=None):
        return self.add(DiffSession(title, left, right))

    def get(self, session_id):
        return self.sessions.get(session_id)

    def activate(self, session_id):
        if session_id in self.sessions:
            self.active_id = session_id
        return self.sessions.get(session_id)

    def remove(self, session_id):
        self.sessions.pop(session_id, None)
        if self.active_id == session_id:
            self.active_id = next(iter(self.sessions), None)

    def active(self):
        return self.sessions.get(self.active_id)

    def titles(self):
        return [session.title for session in self.sessions.values()]


def run_cli(argv):
    """Entry point used both by the CLI and by tests (returns an exit code).

    `diffgui file1 file2` compares two files. Output format is selected with
    --format (text/unified/json); a graphical run is the default unless the
    caller asks for --no-gui or the platform has no display.
    The CLI is one of the few entry points that never touches the window, so it
    pays nothing for the parser being deferred at import time: resolve it here,
    where it is used.
    """
    global argparse
    import argparse  # noqa: F811 - the lazy global lookup needs this binding
    parser = argparse.ArgumentParser(
        prog="diffgui",
        description="ScriptDB diff viewer - compare two files from the terminal.")
    parser.add_argument("left", nargs="?", help="left file")
    parser.add_argument("right", nargs="?", help="right file")
    parser.add_argument("--algorithm", default="myers",
                        choices=["myers", "patience", "histogram"])
    parser.add_argument("--format", default="gui",
                        choices=["gui", "text", "unified", "json"])
    parser.add_argument("--no-gui", action="store_true",
                        help="print the diff instead of opening the window")
    parser.add_argument("--ignore-ws", action="store_true")
    parser.add_argument("--ignore-case", action="store_true")
    parser.add_argument("--ignore-comments", action="store_true")
    parser.add_argument("--ignore-blank", action="store_true")
    parser.add_argument("--stats", action="store_true",
                        help="print only the diff statistics")
    parser.add_argument("--report", metavar="PATH",
                        help="write a report (.patch/.json/.txt) to PATH")
    parser.add_argument("--diagnostics", metavar="PATH",
                        help="write diagnostic artefacts (JSON) to PATH")
    args = parser.parse_args(argv)

    if not args.left or not args.right:
        parser.print_help()
        return 0

    flags = {
        "ignore_ws": args.ignore_ws,
        "ignore_case": args.ignore_case,
        "ignore_comments": args.ignore_comments,
        "ignore_blank": args.ignore_blank,
    }
    report = compare_files(args.left, args.right, args.algorithm, "text", **flags)

    # Report / diagnostics writers run head-less and then exit, so scripts can
    # generate artefacts without ever starting the GUI.
    wrote_artifact = False
    if args.report:
        write_report(report, args.report)
        print("report written: " + args.report)
        wrote_artifact = True
    if args.diagnostics:
        with open(args.diagnostics, "w", encoding="utf-8") as handle:
            json.dump(diagnostic_artifacts(report), handle, ensure_ascii=False, indent=2)
        print("diagnostics written: " + args.diagnostics)
        wrote_artifact = True
    if args.stats:
        print(format_statistics(diff_statistics(report)))
        wrote_artifact = True
    if wrote_artifact:
        return 0

    style = "text" if args.no_gui and args.format == "gui" else args.format

    if style in ("text", "unified", "json"):
        print(format_report(report, style))
        return 0

    # GUI mode: open the window with the two files pre-loaded.
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")
    apply_application_theme(app)
    # Mouse-wheel scrolling everywhere (also when launched from the CLI).
    if not hasattr(app, "_wheel_scroll_filter"):
        install_wheel_scrolling(app)
    win = ScriptDatabaseWindow()
    win.diff_tab.open_paths(args.left, args.right)
    win.tabs.setCurrentWidget(win.diff_tab)
    win.show()
    if QtWidgets.QApplication.instance() is None:
        return app.exec_()
    return 0


def main(argv=None):
    """Program entry point.

    With no arguments it opens the full GUI; with two file arguments it
    behaves like the `diffgui file1 file2` command, so the same script can
    be driven from a shell or embedded in another application.
"""

    argv = list(sys.argv[1:] if argv is None else argv)
    if argv:
        sys.exit(run_cli(argv))
    app = QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")
    apply_application_theme(app)
    app.setWindowIcon(load_app_icon())
    # Mouse-wheel scrolling everywhere, including over non-scrollable widgets.
    install_wheel_scrolling(app)
    win = ScriptDatabaseWindow()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
