# -*- coding: utf-8 -*-
"""Offline document types for the DOCs tab.

This module is the single place that knows which kinds of document the "DOCs"
tab supports offline, and how each one behaves. A type is described by a
:class:`DocumentType` record so the picker, the importer, the previewer and the
exports all agree without guessing from the file name:

    DocumentType(key, label, extensions, group, binary, preview)
        key         stable identifier stored in the database
        label       the human label shown in the picker
        extensions  the file extensions mapped to this type
        group       one of TEXT / CONFIG / LOG / REPORT / AUDIT / PROJECT /
                    SNAPSHOT / SCRIPT / SYSTEM (see GROUPS)
        binary      True when the body is not text (stored as a note only)
        preview     the preview strategy the engine should use (see PREVIEW_*)

Everything here is a plain data table -- no third-party dependency and no
network -- so the supported-type list can be enumerated, tested and rendered
without a GUI.
"""


import os

# Type groups, in the order the UI lists them. Each group is (key, label_key,
# fallback) so the tab can translate the label from the shared language table.
GROUPS = (
    ("text", "docs_group_text", "Text documents"),
    ("config", "docs_group_config", "Configuration documents"),
    ("log", "docs_group_log", "Log documents"),
    ("report", "docs_group_report", "Report documents"),
    ("audit", "docs_group_audit", "Audit documents"),
    ("project", "docs_group_project", "Project documents"),
    ("snapshot", "docs_group_snapshot", "Snapshot documents"),
    ("script", "docs_group_script", "Script documents"),
    ("system", "docs_group_system", "System documents"),
)

# Preview strategies the engine understands.
PREVIEW_TEXT = "text"          # plain / ASCII text preview
PREVIEW_STRUCTURE = "structure"  # headings, sections, blocks
PREVIEW_CONFIG = "config"      # key/value outline of a config file
PREVIEW_LOG = "log"            # level counts + chronological tail
PREVIEW_REPORT = "report"      # section outline + summary counts
PREVIEW_TABLE = "table"        # row/column outline of a delimited file
PREVIEW_NONE = "none"          # binary: metadata only


class DocumentType(object):
    """One offline document type (see the module docstring)."""


    __slots__ = ("key", "label", "label_key", "extensions", "group",
                 "binary", "preview")

    def __init__(self, key, label, extensions, group, label_key="",
                 binary=False, preview=PREVIEW_TEXT):
        self.key = key
        self.label = label
        self.label_key = label_key or ("docs_type_" + key)
        self.extensions = tuple(extensions)
        self.group = group
        self.binary = binary
        self.preview = preview

    def __repr__(self):
        return "DocumentType(%r, %r)" % (self.key, self.group)


# ---------------------------------------------------------------------------
# The supported types (offline). One record per type; the extension tables and
# the group lists are derived from this table below.
# ---------------------------------------------------------------------------

DOCUMENT_TYPES = (
    # --- text documents ----------------------------------------------------
    DocumentType("text", "Text", ("txt",), "text"),
    DocumentType("markdown", "Markdown", ("md", "markdown", "mdown"), "text",
                 preview=PREVIEW_STRUCTURE),
    DocumentType("rst", "reStructuredText", ("rst",), "text",
                 preview=PREVIEW_STRUCTURE),
    # --- configuration documents ------------------------------------------
    DocumentType("json", "JSON", ("json", "jsonl", "geojson"), "config",
                 preview=PREVIEW_CONFIG),
    DocumentType("yaml", "YAML", ("yaml", "yml"), "config",
                 preview=PREVIEW_CONFIG),
    DocumentType("ini", "INI / TOML", ("ini", "cfg", "conf", "toml", "env"),
                 "config", preview=PREVIEW_CONFIG),
    DocumentType("xml", "XML / HTML", ("xml", "html", "htm", "xhtml"), "config",
                 preview=PREVIEW_STRUCTURE),
    # --- log documents -----------------------------------------------------
    DocumentType("log", "Log", ("log", "out", "err"), "log",
                 preview=PREVIEW_LOG),
    DocumentType("syslog", "System log", ("syslog",), "log",
                 preview=PREVIEW_LOG),
    # --- report documents --------------------------------------------------
    DocumentType("report", "Report", ("report", "rpt"), "report"),
    DocumentType("table", "CSV / table", ("csv", "tsv"), "report",
                 preview=PREVIEW_TABLE),
    DocumentType("spreadsheet", "Spreadsheet", ("xls", "xlsx", "ods"), "report",
                 binary=True, preview=PREVIEW_NONE),
    # --- audit documents ---------------------------------------------------
    DocumentType("audit", "Audit trail", ("audit",), "audit"),
    DocumentType("changelog", "Changelog", ("changelog", "changes"), "audit"),
    DocumentType("policy", "Policy / compliance", ("policy",), "audit",
                 preview=PREVIEW_STRUCTURE),
    # --- project documents -------------------------------------------------
    DocumentType("project", "Project note", ("proj",), "project"),
    DocumentType("readme", "README", ("readme",), "project",
                 preview=PREVIEW_STRUCTURE),
    DocumentType("plan", "Plan / roadmap", ("plan", "roadmap", "todo"), "project",
                 preview=PREVIEW_STRUCTURE),
    DocumentType("diagram", "Diagram (text)", ("puml", "mmd", "dot"), "project",
                 preview=PREVIEW_STRUCTURE),
    # --- snapshot documents ------------------------------------------------
    DocumentType("snapshot", "Snapshot", ("snapshot",), "snapshot",
                 preview=PREVIEW_CONFIG),
    DocumentType("backup", "Backup", ("bak", "backup"), "snapshot",
                 preview=PREVIEW_TEXT),
    # --- script documents (code as document) -------------------------------
    DocumentType("script", "Script / source", ("py", "js", "ts", "java", "c",
                                               "cpp", "cs", "rb", "go", "rs",
                                               "sh", "ps1", "bat", "cmd",
                                               "php", "pl", "lua"), "script",
                 preview=PREVIEW_STRUCTURE),
    DocumentType("sql", "SQL script", ("sql",), "script",
                 preview=PREVIEW_STRUCTURE),
    DocumentType("notebook", "Notebook", ("ipynb",), "script",
                 preview=PREVIEW_CONFIG),
    # --- system documents --------------------------------------------------
    DocumentType("manifest", "Manifest", ("manifest",), "system",
                 preview=PREVIEW_CONFIG),
    DocumentType("metadata", "Metadata", ("meta", "metadata"), "system",
                 preview=PREVIEW_CONFIG),
    DocumentType("system", "System config", ("reg", "service", "desktop",
                                             "plist"), "system",
                 preview=PREVIEW_CONFIG),
    DocumentType("binary", "Binary / other", ("bin", "dat"), "system",
                 binary=True, preview=PREVIEW_NONE),
)

# The picker shows a friendly default order: grouped by group, table order.
_TYPE_BY_KEY = dict((t.key, t) for t in DOCUMENT_TYPES)
_TYPE_BY_EXTENSION = {}
for _type in DOCUMENT_TYPES:
    for _ext in _type.extensions:
        _TYPE_BY_EXTENSION[_ext.lower()] = _type

# Extra names that are not extensions but common file names (README, LICENSE).
FILENAME_TYPES = {
    "readme": "readme", "license": "text", "licence": "text",
    "changelog": "changelog", "changes": "changelog",
    "makefile": "script", "dockerfile": "script", "manifest": "manifest",
    "todo": "plan",
}


def type_for_key(key):
    """The DocumentType registered under a key (falls back to Text)."""
    return _TYPE_BY_KEY.get(key or "", _TYPE_BY_KEY["text"])


def default_type():
    """The type used when nothing else matches."""
    return _TYPE_BY_KEY["text"]


def guess_type(filename):
    """The DocumentType for a file name or path (never None).

    The extension wins, then a known file name without one (README,
    Dockerfile, package-lock.json...). So `readme.md` stays a Markdown
    document while a bare `README` resolves to the README type.
    """
    name = os.path.basename(filename or "").lower()
    if name in FILENAME_TYPES:
        return type_for_key(FILENAME_TYPES[name])
    dot = name.rfind(".")
    extension = name[dot + 1:] if dot >= 0 else ""
    found = _TYPE_BY_EXTENSION.get(extension)
    if found is not None:
        return found
    # A few multi-part names (package-lock.json) still resolve on their tail.
    for ext in sorted(_TYPE_BY_EXTENSION, key=len, reverse=True):
        if ext and name.endswith("." + ext):
            return _TYPE_BY_EXTENSION[ext]
    # No extension match: fall back to a known stem (a bare LICENSE).
    stem = name.split(".")[0]
    if stem in FILENAME_TYPES:
        return type_for_key(FILENAME_TYPES[stem])
    return default_type()


def guess_type_label(filename):
    """The type label for a file name (convenience for importers)."""
    return guess_type(filename).label


def extensions_for(key):
    """The extensions mapped to one type key."""
    return type_for_key(key).extensions


def types_in_group(group):
    """Every type belonging to one group, in table order."""
    return [t for t in DOCUMENT_TYPES if t.group == group]


def all_types():
    """The whole type table (picker order)."""
    return list(DOCUMENT_TYPES)


def is_binary_type(key):
    """True when a type stores a note instead of readable text."""
    return type_for_key(key).binary


def preview_for(key):
    """The preview strategy for a type key."""
    return type_for_key(key).preview


def type_label(key, table=None):
    """A translated label for a type key (uses the language table if given)."""
    doc_type = type_for_key(key)
    if table:
        return table.get(doc_type.label_key, doc_type.label)
    return doc_type.label


def capabilities():
    """Describe the supported types for the audit/report layer."""
    groups = {}
    for key, _label_key, _fallback in GROUPS:
        groups[key] = len(types_in_group(key))
    return {
        "types": len(DOCUMENT_TYPES),
        "groups": groups,
        "offline": True,
        "extensions": len(_TYPE_BY_EXTENSION),
        "binary_types": sum(1 for t in DOCUMENT_TYPES if t.binary),
    }
