# -*- coding: utf-8 -*-
"""Metadata manager for scripts -- the engine behind the Metadata tab.

This is a universal metadata manager for script files. It reads, writes and
validates the metadata that lives in the header comment of a script, across the
languages this application already knows about, and it never rewrites a file it
cannot parse back to the same meaning.

    MetadataFields      the field set, including custom fields
    detect_language     work out what a file is, from its name then its bytes
    parse_header        read the metadata out of a file
    render_header       build the header block for a language
    write_header        put that block into a file, changing nothing else
    validate            check the fields against the rules for a status
    MetadataStore       scan, cache, bulk edit, audit, export

It reuses what the application already has rather than duplicating it:

    tabs.debug_langs    the language registry: keys, labels, extensions and
                        comment markers all come from there, so a language
                        added for the debugger is a language here too
    tabs.theme          the palette, so a dialog can colour a state the way the
                        rest of the window does

The header convention is deliberately a simple, readable one -- one ``key:``
field per line inside the file's own comment syntax, wrapped in a start and an
end marker. That is what every language can express, and it is what a person
reading the file by hand can edit without a tool.
"""

import datetime as dt
import json
import os
import re

from . import debug_langs


def _now():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# The field set
# ---------------------------------------------------------------------------
# Every field is (key, label key, fallback label, kind, group).  ``kind`` is
# what the editor renders it as and what validation checks:
#
#     text      a free line of text
#     multiline a description or a dependency list
#     version   a version number, checked as x.y.z
#     date      a timestamp in the application's own format
#     tags      a comma-separated list
#     choice    one of a fixed set
#     bool      a yes/no flag
METADATA_FIELDS = (
    # ---- standard --------------------------------------------------------
    ("name", "meta_f_name", "Script name", "text", "standard"),
    ("description", "meta_f_description", "Description / purpose",
     "multiline", "standard"),
    ("author", "meta_f_author", "Author / owner", "text", "standard"),
    ("version", "meta_f_version", "Version / build", "version", "standard"),
    ("created", "meta_f_created", "Created", "date", "standard"),
    ("modified", "meta_f_modified", "Modified", "date", "standard"),
    ("tags", "meta_f_tags", "Tags / categories / modules", "tags",
     "standard"),
    ("environment", "meta_f_environment", "Environment / platform", "text",
     "standard"),
    ("dependencies", "meta_f_dependencies", "Requirements (dependencies)",
     "multiline", "standard"),
    # ---- technical -------------------------------------------------------
    ("inputs", "meta_f_inputs", "Inputs (data, files, parameters)",
     "multiline", "technical"),
    ("outputs", "meta_f_outputs", "Outputs", "multiline", "technical"),
    ("status", "meta_f_status", "Status", "choice", "technical"),
    ("privileges", "meta_f_privileges", "Privilege level", "choice",
     "technical"),
    ("links", "meta_f_links", "Related scripts, services, jobs, pipelines",
     "multiline", "technical"),
    ("kind", "meta_f_kind", "Runnable as", "choice", "technical"),
    ("schedule", "meta_f_schedule", "Schedule / cron", "text", "technical"),
)

METADATA_FIELD_KEYS = tuple(key for key, _lk, _f, _kind, _group
                            in METADATA_FIELDS)
METADATA_FIELD_LABELS = {key: fallback for key, _lk, fallback, _kind, _group
                         in METADATA_FIELDS}
METADATA_FIELD_KINDS = {key: kind for key, _lk, _f, kind, _group
                        in METADATA_FIELDS}
METADATA_FIELD_GROUPS = {key: group for key, _lk, _f, _kind, group
                         in METADATA_FIELDS}
METADATA_GROUPS = ("standard", "technical", "custom")

#: The values a choice field accepts, with the label each one shows.
METADATA_CHOICES = {
    "status": (
        ("experimental", "meta_status_experimental", "Experiment"),
        ("stable", "meta_status_stable", "Stable"),
        ("production", "meta_status_production", "Production"),
        ("deprecated", "meta_status_deprecated", "Deprecated"),
    ),
    "privileges": (
        ("user", "meta_priv_user", "User"),
        ("elevated", "meta_priv_elevated", "Elevated"),
        ("admin", "meta_priv_admin", "Administrator"),
        ("system", "meta_priv_system", "System"),
    ),
    "kind": (
        ("script", "meta_kind_script", "Script"),
        ("job", "meta_kind_job", "Job"),
        ("task", "meta_kind_task", "Task"),
        ("hook", "meta_kind_hook", "Hook"),
        ("service", "meta_kind_service", "Service"),
    ),
}

#: The roles a person can hold.  Each entry is
#: (key, label key, fallback, may_edit, may_configure).
METADATA_ROLES = (
    ("viewer", "meta_role_viewer", "Viewer", False, False),
    ("editor", "meta_role_editor", "Editor", True, False),
    ("admin", "meta_role_admin", "Administrator", True, True),
)

METADATA_ROLE_KEYS = tuple(key for key, _lk, _f, _edit, _cfg
                           in METADATA_ROLES)
METADATA_ROLE_LABELS = {key: fallback for key, _lk, fallback, _edit, _cfg
                        in METADATA_ROLES}
# ---------------------------------------------------------------------------
# Marker and comment conventions
# ---------------------------------------------------------------------------
#: The block the manager looks for, and writes.  Two markers, one line each, so
#: a person can see exactly where the managed region starts and ends.
HEADER_START = "SCRIPTDB-META"
HEADER_END = "SCRIPTDB-META-END"

#: How a language expresses a comment and a header.  The values come from the
#: debugger's own language registry, so nothing here re-lists a language.
def comment_style(key):
    """The comment conventions of a language, as a plain dictionary.

    Returns ``line`` (the marker for a single-line comment), ``block`` (a
    ``(open, close)`` pair when the language has one, or None) and ``label``
    (the language as a person reads it).  An unknown language falls back to
    ``#``, which is the most widely accepted marker there is.
    """
    adapter = debug_langs.get(key) or debug_langs.adapter_for_extension("x." + key)
    if adapter is None:
        adapter = debug_langs.adapter_for("")
    markers = [marker for marker in (getattr(adapter, "comment_markers", ()) or ())
               if marker]
    line = "#"
    for marker in markers:
        if marker in ("#", "//", "'", ";", "--", "rem", "::"):
            line = marker
            break
    else:
        if markers:
            line = markers[0]
    block = None
    # The languages with a real block comment are the ones where a whole
    # header reads better inside it.
    if line == "//":
        block = ("/*", "*/")
    elif line in ("#",) and getattr(adapter, "key", "") == "powershell":
        block = ("<#", "#>")
    return {"line": line, "block": block,
            "label": getattr(adapter, "label", key or "Unknown"),
            "key": getattr(adapter, "key", "text")}


#: Signatures in a file's own text, used when the extension is inconclusive.
#: Each entry is (language key, regex, weight); a weight above 1 means the
#: signature is specific enough to decide on its own.
_SIGNATURES = (
    ("python", r"^#!.*\bpython[0-9.]*\b", 3),
    ("python", r"^\s*(?:from\s+[\w.]+\s+)?import\s+\w+", 2),
    ("python", r"^\s*def\s+\w+\s*\(.*\)\s*:", 2),
    ("bash", r"^#!.*\b(?:ba|z|k)?sh\b", 3),
    ("bash", r"^\s*(?:if\s+\[|for\s+\w+\s+in\b)", 1),
    ("powershell", r"(?i)^\s*(?:param\s*\(|\$[A-Za-z_]\w*\s*=)", 2),
    ("powershell", r"(?i)\bWrite-(?:Host|Output)\b", 2),
    ("batch", r"(?i)^\s*@?echo\s+off\b", 3),
    ("batch", r"(?i)^\s*(?:set|call|goto)\s+\w+", 2),
    ("vbscript", r"(?i)^\s*(?:Option\s+Explicit|Dim\s+\w+)", 2),
    ("vbscript", r"(?i)\bWScript\b", 3),
    ("javascript", r"(?i)^\s*(?:const|let|var|function)\s+\w+", 2),
    ("javascript", r"^#!.*\bnode\b", 3),
    ("java", r"^\s*(?:public|private|protected)\s+class\s+\w+", 2),
    ("csharp", r"(?i)^\s*using\s+System\b", 3),
    ("cpp", r"^\s*#include\s*[<\"]", 3),
    ("ruby", r"(?i)^\s*(?:require|def\s+\w+|puts)\b", 1),
    ("ruby", r"^#!.*\bruby\b", 3),
)


def detect_language(path="", text=""):
    """Work out what a file is: its name first, then its own bytes.

    Returns ``(key, how)`` where ``how`` says what decided it -- ``"extension"``
    or ``"content"`` -- so the interface can be honest about a guess.
    """
    # The registry answers with a fallback adapter for an unknown extension, so
    # the answer is only trusted when the extension is one the registry lists.
    if path:
        extension = os.path.splitext(path)[1].lower()
        known = script_extensions()
        if extension and extension in known:
            adapter = debug_langs.adapter_for_extension(path)
            if adapter is not None:
                return adapter.key, "extension"
        name = os.path.basename(path).lower().lstrip(".")
        direct = debug_langs.get(name)
        if direct is not None:
            return direct.key, "extension"
    scored = {}
    for key, pattern, weight in _SIGNATURES:
        try:
            if re.search(pattern, text or "", re.MULTILINE):
                scored[key] = scored.get(key, 0) + weight
        except re.error:
            continue
    if scored:
        best = max(scored, key=lambda key: (scored[key], key))
        return best, "content"
    return "text", "default"
# ---------------------------------------------------------------------------
# Reading the header out of a file
# ---------------------------------------------------------------------------
def _strip_comment(line, style):
    """One line with its comment marker removed, or None when it is not one."""
    text = (line or "").rstrip()
    stripped = text.lstrip()
    for marker in (style["line"],) + (style["block"] or ()):
        if not marker:
            continue
        if stripped.lower().startswith(marker.lower()):
            return stripped[len(marker):].strip()
    return None


def parse_header(text, language="text"):
    """Read the metadata out of a file body.

    The managed block is found by its two markers, inside whatever comment
    syntax the language uses.  ``key: value`` lines are read into a mapping;
    everything else in the block is kept in ``extra`` so nothing a person wrote
    is thrown away by a tool that did not recognise it.
    """
    style = comment_style(language)
    lines = (text or "").replace("\r\n", "\n").split("\n")
    fields = {}
    extra = []
    start = None
    end = None
    for index, line in enumerate(lines):
        bare = _strip_comment(line, style)
        if bare is None:
            continue
        # The end marker is tested first: it starts with the same text as the
        # start marker, so the other order would read the end as a second start
        # and never close the block.
        if bare.strip().upper().startswith(HEADER_END):
            end = index
            break
        if bare.strip().upper().startswith(HEADER_START):
            start = index
            continue
        if start is None:
            continue
        if not bare:
            extra.append("")
            continue
        if ":" in bare:
            key, _, value = bare.partition(":")
            key = key.strip().lower().replace(" ", "_")
            value = value.strip()
            if key:
                # A continuation line indents under its key; it is folded back
                # into the value so a multi-line field survives the round trip.
                fields[key] = value
                continue
        extra.append(bare)
    # Fold indented continuations into the field above them.
    body = lines[start:end + 1] if start is not None and end is not None else []
    fields = _fold_continuations(body, style, fields)
    return {
        "found": start is not None and end is not None,
        "start": start, "end": end,
        "fields": fields, "extra": extra,
        "style": style,
        "lines_before": lines[:start] if start is not None else lines,
        "lines_after": lines[end + 1:] if end is not None else [],
    }


def _fold_continuations(body, style, fields):
    """Join indented lines onto the value of the field above them."""
    current = None
    for line in body:
        bare = _strip_comment(line, style)
        if bare is None:
            continue
        if bare.strip().upper().startswith((HEADER_START, HEADER_END)):
            continue
        if not bare:
            continue
        indented = line[:1].isspace() or (line[:2].isspace() and not
                                          line.lstrip().startswith("#"))
        if ":" in bare and not indented:
            key, _, value = bare.partition(":")
            current = key.strip().lower().replace(" ", "_")
            fields[current] = value.strip()
            continue
        if current and bare:
            joined = "%s\n%s" % (fields.get(current, ""), bare)
            fields[current] = joined.strip()
    return fields


def header_values(fields, defaults=None):
    """The parsed fields as the editor's own field set.

    Anything the field set does not know is reported separately, so a custom
    field is never confused with a typo in a standard one.
    """
    known = {}
    custom = {}
    for key, value in (fields or {}).items():
        if key in METADATA_FIELD_KEYS:
            known[key] = value
        else:
            custom[key] = value
    if defaults:
        for key, value in defaults.items():
            known.setdefault(key, value)
    return known, custom
# ---------------------------------------------------------------------------
# Writing the header back
# ---------------------------------------------------------------------------
def render_header(values, language="text", custom=None, width=76):
    """Build the managed block for one language, as a list of lines.

    A multi-line value is written with a continuation indent, which is what
    ``parse_header`` folds back together -- so a round trip through this pair
    returns exactly what it started with.
    """
    style = comment_style(language)
    marker = style["line"] or "#"
    order = list(METADATA_FIELD_KEYS)
    for key in sorted(custom or {}):
        if key not in order:
            order.append(key)
    lines = []
    lines.append("%s %s" % (marker, HEADER_START))
    for key in order:
        if key in METADATA_FIELD_KEYS:
            value = (values or {}).get(key, "")
        else:
            value = (custom or {}).get(key, "")
        if value in (None, ""):
            continue
        text = str(value)
        if "\n" in text:
            first, rest = text.split("\n", 1)
            lines.append("%s %s: %s" % (marker, key, first))
            for piece in rest.split("\n"):
                lines.append("%s     %s" % (marker, piece))
        else:
            lines.append("%s %s: %s" % (marker, key, text))
    lines.append("%s %s" % (marker, HEADER_END))
    return lines


def apply_header(text, values, language="text", custom=None):
    """Put the header into a file body, changing nothing else.

    Three cases, in order: an existing managed block is replaced in place; a
    file with no header yet gets one after its shebang and its own leading
    comment or docstring; a file that is empty simply gets one at the top.
    """
    parsed = parse_header(text, language)
    block = render_header(values, language, custom)
    body = (text or "").replace("\r\n", "\n")
    if parsed["found"]:
        lines = body.split("\n")
        merged = lines[:parsed["start"]] + block + lines[parsed["end"] + 1:]
        return "\n".join(_tidy(merged)), True
    lines = body.split("\n")
    insert_at = _header_anchor(lines, language)
    merged = lines[:insert_at] + block + ([""] if insert_at < len(lines)
                                          and lines[insert_at].strip() else []) \
        + lines[insert_at:]
    return "\n".join(_tidy(merged)), False


def _header_anchor(lines, language):
    """Where a new header belongs: after the shebang and any leading block."""
    index = 0
    if lines and lines[0].startswith("#!"):
        index = 1
    style = comment_style(language)
    # Skip a leading docstring, so the header is not put inside it.
    while index < len(lines) and not lines[index].strip():
        index += 1
    if language == "python" and index < len(lines):
        head = lines[index].lstrip()
        if head.startswith(('"""', "'''")):
            quote = head[:3]
            index += 1
            while index < len(lines):
                if lines[index].rstrip().endswith(quote):
                    index += 1
                    break
                index += 1
            return index
    # Skip a licence or banner comment block at the top.
    while index < len(lines) and lines[index].lstrip().startswith(
            style["line"] or "#"):
        index += 1
    return index


def _tidy(lines):
    """Collapse the blank-line runs a replace can leave behind."""
    out = []
    for line in lines:
        if line.strip() == "" and out and out[-1].strip() == "":
            continue
        out.append(line.rstrip())
    while out and out[0].strip() == "":
        out.pop(0)
    while out and out[-1].strip() == "":
        out.pop()
    return out + [""]
# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
#: The rules applied to every record.  Each entry is
#: (key, label key, fallback, what it needs).
VALIDATION_RULES = (
    ("required", "meta_rule_required", "Required fields",
     "name, author, version and description must not be empty"),
    ("version", "meta_rule_version", "Version format",
     "a version must read as x.y.z"),
    ("date", "meta_rule_date", "Date format",
     "a date must read as YYYY-MM-DD HH:MM:SS"),
    ("status", "meta_rule_status", "Known status",
     "the status must be one of the known values"),
    ("production", "meta_rule_production", "Production rules",
     "a script marked production must name an author, a version, "
     "a description and a status"),
    ("deprecated", "meta_rule_deprecated", "Deprecation rules",
     "a deprecated script should name what replaces it in the links field"),
    ("dependencies", "meta_rule_dependencies", "Dependency rules",
     "a script with privileges above user must list its requirements"),
)

VALIDATION_RULE_KEYS = tuple(key for key, _lk, _f, _d in VALIDATION_RULES)
VALIDATION_RULE_LABELS = {key: fallback for key, _lk, fallback, _d
                          in VALIDATION_RULES}

#: The fields every script should carry, whatever its status.
REQUIRED_FIELDS = ("name", "author", "version", "description")

_VERSION = re.compile(r"^\d+(?:\.\d+){1,3}(?:[-+][0-9A-Za-z.-]+)?$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?$")


def validate(values, custom=None, rules=None):
    """Check one record; return the findings, worst first.

    Each finding is ``{"rule", "field", "severity", "detail"}``.  Severity is
    ``"error"`` for something that must be fixed and ``"warning"`` for
    something worth a look -- so the interface can colour the two differently
    and a CI job can fail on errors alone.
    """
    active = set(rules or VALIDATION_RULE_KEYS)
    values = values or {}
    findings = []

    def add(rule, field, severity, detail):
        findings.append({"rule": rule, "field": field,
                         "severity": severity, "detail": detail})

    if "required" in active:
        for field in REQUIRED_FIELDS:
            if not str(values.get(field) or "").strip():
                add("required", field, "error",
                    "%s is empty" % METADATA_FIELD_LABELS.get(field, field))

    if "version" in active:
        version = str(values.get("version") or "").strip()
        if version and not _VERSION.match(version):
            add("version", "version", "error",
                "a version must read as x.y.z, not %r" % version)

    if "date" in active:
        for field in ("created", "modified"):
            stamp = str(values.get(field) or "").strip()
            if stamp and not _DATE.match(stamp):
                add("date", field, "warning",
                    "%s should read as YYYY-MM-DD HH:MM:SS" % field)

    if "status" in active:
        status = str(values.get("status") or "").strip()
        known = {key for key, _lk, _f in METADATA_CHOICES["status"]}
        if status and status not in known:
            add("status", "status", "warning",
                "unknown status %r" % status)

    if "production" in active:
        status = str(values.get("status") or "").strip()
        if status in ("production", "stable"):
            missing = [field for field in
                       ("author", "version", "description", "status")
                       if not str(values.get(field) or "").strip()]
            for field in missing:
                add("production", field, "error",
                    "a %s script must carry %s" % (status, field))

    if "deprecated" in active:
        if str(values.get("status") or "").strip() == "deprecated":
            if not str(values.get("links") or "").strip():
                add("deprecated", "links", "warning",
                    "say what replaces this script in the links field")

    if "dependencies" in active:
        privileges = str(values.get("privileges") or "").strip()
        if privileges in ("admin", "system", "elevated"):
            if not str(values.get("dependencies") or "").strip():
                add("dependencies", "dependencies", "warning",
                    "a %s script should list its requirements" % privileges)

    order = {"error": 0, "warning": 1}
    findings.sort(key=lambda item: (order.get(item["severity"], 9),
                                    item["field"]))
    return findings


def validation_summary(findings):
    """A one-line summary of a validation result."""
    errors = sum(1 for item in findings if item["severity"] == "error")
    warnings = sum(1 for item in findings if item["severity"] == "warning")
    if not findings:
        return "valid"
    return "%d error(s), %d warning(s)" % (errors, warnings)


def is_valid(findings):
    """True when nothing must be fixed (warnings are allowed)."""
    return not any(item["severity"] == "error" for item in findings)
# ---------------------------------------------------------------------------
# The store: scan, cache, edit, audit
# ---------------------------------------------------------------------------
#: The extensions the scanner looks at by default, taken from the language
#: registry so a language the application already supports is scanned too.
def script_extensions():
    """Every extension the language registry knows, lower-case, with the dot."""
    found = []
    for adapter in debug_langs.adapters() or ():
        for extension in getattr(adapter, "extensions", ()) or ():
            text = str(extension).lower()
            if not text.startswith("."):
                text = "." + text
            if text not in found:
                found.append(text)
    for extra in (".py", ".ps1", ".bat", ".cmd", ".sh", ".vbs", ".rb", ".java",
                  ".js", ".ts", ".php", ".pl", ".sql", ".yaml", ".json"):
        if extra not in found:
            found.append(extra)
    return tuple(sorted(found))


#: Folders the scanner never walks into.
SKIP_FOLDERS = (".git", "__pycache__", ".venv", "venv", "node_modules",
                ".idea", ".vscode", "dist", "build", ".mypy_cache")

#: Where a record can keep its metadata, as (key, label key, fallback).
METADATA_STORES = (
    ("header", "meta_store_header", "In the file header",
     "the managed block inside the file itself"),
    ("sidecar", "meta_store_sidecar", "In a sidecar file",
     "a metadata.json next to the script, leaving the code untouched"),
)

METADATA_STORE_KEYS = tuple(key for key, _lk, _f, _d in METADATA_STORES)
METADATA_STORE_LABELS = {key: fallback for key, _lk, fallback, _d
                         in METADATA_STORES}


class MetadataStore(object):
    """Every script under a root, with its metadata, and the edits to it.

    The store owns the cache and the audit trail.  It never writes a file
    unless it is asked to, and every write goes through :meth:`save`, which
    keeps a copy of the old text so an undo is exact.
    """

    #: The file a record can use instead of touching the code.
    SIDECAR_NAME = "metadata.json"

    def __init__(self, root="", role="editor", read_only=False):
        self.root = os.path.abspath(root) if root else ""
        self.role = role if role in METADATA_ROLE_KEYS else "editor"
        self.read_only = bool(read_only)
        self.records = {}            # path -> record
        self.custom_fields = {}      # key -> label
        self.audit = []              # newest first
        self.undo_stack = []         # (path, previous text, previous record)
        self.profile = "default"

    # ------------------------------------------------------------- roles
    def may_edit(self):
        """True when the current role is allowed to change metadata."""
        for key, _lk, _f, edit, _cfg in METADATA_ROLES:
            if key == self.role:
                return edit and not self.read_only
        return False

    def may_configure(self):
        """True when the current role may change fields and adapters."""
        for key, _lk, _f, _edit, configure in METADATA_ROLES:
            if key == self.role:
                return configure and not self.read_only
        return False

    def set_role(self, role, read_only=None):
        if role not in METADATA_ROLE_KEYS:
            return {"ok": False, "error": "unknown role: %s" % role}
        self.role = role
        if read_only is not None:
            self.read_only = bool(read_only)
        self._log("role", "role set to %s%s" % (
            role, " (read-only)" if self.read_only else ""))
        return {"ok": True, "role": role, "read_only": self.read_only}

    def _allowed_folder(self, path):
        """The folders this role may edit, from the record's own settings."""
        allowed = (self.records.get(path) or {}).get("editable")
        return allowed is not False

    # -------------------------------------------------------------- scan
    def scan(self, root=None, kinds=None, with_git=True, limit=4000):
        """Walk a folder and read every script's metadata.

        Returns a summary rather than the records themselves: the records are
        held in the store, so a caller can filter or sort them without the scan
        work being repeated.
        """
        target = os.path.abspath(root or self.root or ".")
        if not os.path.isdir(target):
            return {"ok": False, "error": "not a folder: %s" % target}
        self.root = target
        wanted = set(kinds or script_extensions())
        found = 0
        skipped = 0
        for base, folders, files in os.walk(target):
            folders[:] = sorted(name for name in folders
                                if name not in SKIP_FOLDERS)
            for name in sorted(files):
                if os.path.splitext(name)[1].lower() not in wanted:
                    continue
                if found >= limit:
                    break
                path = os.path.join(base, name)
                record = self.read_record(path, with_git=with_git)
                if record is None:
                    skipped += 1
                    continue
                self.records[path] = record
                found += 1
        self._log("scan", "%d script(s) under %s" % (found, target))
        return {"ok": True, "root": target, "found": found, "skipped": skipped,
                "detail": "%d script(s) scanned" % found}

    def read_record(self, path, with_git=True):
        """Read one file into a record, or None when it cannot be read."""
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except OSError:
            return None
        language, how = detect_language(path, text)
        parsed = parse_header(text, language)
        values, custom = header_values(parsed["fields"])
        sidecar = self._read_sidecar(path)
        if sidecar:
            # A sidecar wins over the header: it is the explicit, deliberate
            # place, and the header may be absent or older.
            for key, value in (sidecar.get("fields") or {}).items():
                values[key] = value
            custom.update(sidecar.get("custom") or {})
        defaults = {
            "name": os.path.splitext(os.path.basename(path))[0],
            "created": _file_time(path, "created"),
            "modified": _file_time(path, "modified"),
        }
        for key, value in defaults.items():
            if not str(values.get(key) or "").strip():
                values[key] = value
        record = {
            "path": path,
            "name": os.path.basename(path),
            "language": language,
            "language_label": comment_style(language)["label"],
            "detected_by": how,
            "values": values,
            "custom": custom,
            "found_header": parsed["found"],
            "store": "sidecar" if sidecar else "header",
            "editable": True,
            "git": self._git_context(path) if with_git else {},
        }
        findings = validate(values, custom)
        record["findings"] = findings
        record["valid"] = is_valid(findings)
        return record

    def _read_sidecar(self, path):
        """The sidecar mapping next to a file, when there is one."""
        sidecar = os.path.join(os.path.dirname(path), self.SIDECAR_NAME)
        if not os.path.isfile(sidecar):
            return None
        try:
            with open(sidecar, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        for key, entry in data.items():
            if isinstance(entry, dict) and entry.get("path", "").endswith(
                    os.path.basename(path)):
                return {"fields": entry.get("fields") or {},
                        "custom": entry.get("custom") or {}}
        return None

    def _git_context(self, path):
        """What git can say about a file: repository, branch and a guess.

        Git is read by running its own commands with a timeout, and every
        failure is silent: a machine without git simply gets an empty context,
        which is the honest answer.
        """
        import subprocess
        folder = os.path.dirname(path)
        context = {}
        for key, command in (
                ("repository", ["rev-parse", "--show-toplevel"]),
                ("branch", ["rev-parse", "--abbrev-ref", "HEAD"]),
                ("commit", ["log", "-1", "--format=%h %s", "--", path])):
            context[key] = _git(command, folder)
        return {key: value for key, value in context.items() if value}

    def records_list(self, section="all", needle="", language="", status="",
                     tag="", author="", only_invalid=False, sort="name"):
        """The cache as a filtered, sorted list, for the table."""
        out = []
        needle = (needle or "").strip().lower()
        for record in self.records.values():
            values = record.get("values") or {}
            if section == "invalid" and record.get("valid"):
                continue
            if language and record.get("language") != language:
                continue
            if status and (values.get("status") or "") != status:
                continue
            if author and needle and False:
                continue
            if author and author.lower() not in str(
                    values.get("author") or "").lower():
                continue
            if tag and tag.lower() not in str(values.get("tags") or "").lower():
                continue
            if only_invalid and record.get("valid"):
                continue
            if needle:
                haystack = " ".join(str(values.get(key) or "") for key in
                                    ("name", "description", "author", "tags",
                                     "status", "dependencies", "links"))
                haystack += " " + str(record.get("path", ""))
                if needle not in haystack.lower():
                    continue
            out.append(record)
        keys = {
            "name": lambda item: (item.get("name") or "").lower(),
            "language": lambda item: (item.get("language") or ""),
            "version": lambda item: (item["values"].get("version") or ""),
            "author": lambda item: (item["values"].get("author") or "").lower(),
            "status": lambda item: (item["values"].get("status") or ""),
            "path": lambda item: (item.get("path") or "").lower(),
        }
        return sorted(out, key=keys.get(sort, keys["name"]))

    def facets(self):
        """The values the filters can offer, from what is actually present."""
        languages, statuses, authors, tags = {}, {}
        for record in self.records.values():
            languages[record["language"]] = languages.get(
                record["language"], 0) + 1
            values = record.get("values") or {}
            status = values.get("status") or "(none)"
            statuses[status] = statuses.get(status, 0) + 1
            author = values.get("author") or "(none)"
            authors[author] = authors.get(author, 0) + 1
            for tag in str(values.get("tags") or "").replace(",", " ").split():
                tags[tag] = tags.get(tag, 0) + 1
        return {"languages": languages, "statuses": statuses,
                "authors": authors, "tags": tags}
    # -------------------------------------------------------------- editing
    def _log(self, action, detail, path=""):
        """Record one change in the audit trail (newest first)."""
        self.audit.insert(0, {"time": _now(), "action": action,
                              "detail": detail, "path": path,
                              "role": self.role})
        del self.audit[2000:]
        return self.audit[0]

    def save(self, path, values, custom=None, store="header", force=False):
        """Write the metadata of one file, keeping the old text for undo.

        The file is only rewritten through this method, so the audit trail and
        the undo stack can never disagree with what is on disk.
        """
        if not self.may_edit():
            return {"ok": False, "error": "the current role may not edit"}
        record = self.records.get(path)
        if record is None:
            record = self.read_record(path)
            if record is None:
                return {"ok": False, "error": "the file cannot be read"}
            self.records[path] = record
        if not self._allowed_folder(path):
            return {"ok": False, "error": "this folder is not editable"}
        findings = validate(values, custom)
        if not is_valid(findings) and not force:
            # A write that would leave the file invalid is refused rather than
            # done quietly: the interface offers an explicit override instead.
            return {"ok": False, "error": "the record would not be valid",
                    "findings": findings}
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                previous = handle.read()
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        if store == "sidecar":
            written = self._write_sidecar(path, values, custom)
            if not written["ok"]:
                return written
            new_text = previous
        else:
            new_text, _replaced = apply_header(previous, values,
                                               record["language"], custom)
            try:
                _atomic_write(path, new_text)
            except OSError as exc:
                return {"ok": False, "error": str(exc)}
        self.undo_stack.append({"path": path, "text": previous,
                                "record": dict(record)})
        del self.undo_stack[:-200]
        refreshed = self.read_record(path, with_git=False)
        if refreshed is not None:
            self.records[path] = refreshed
        self._log("save", "metadata written for %s"
                  % os.path.basename(path), path)
        return {"ok": True, "path": path, "store": store,
                "detail": "saved %s" % os.path.basename(path)}

    def _write_sidecar(self, path, values, custom):
        """Merge one file's metadata into the sidecar beside it."""
        sidecar = os.path.join(os.path.dirname(path), self.SIDECAR_NAME)
        data = {}
        if os.path.isfile(sidecar):
            try:
                with open(sidecar, "r", encoding="utf-8") as handle:
                    loaded = json.load(handle)
                if isinstance(loaded, dict):
                    data = loaded
            except (OSError, ValueError):
                data = {}
        data[os.path.basename(path)] = {"path": path, "fields": values,
                                        "custom": custom or {}}
        try:
            _atomic_write(sidecar, json.dumps(data, indent=2,
                                              ensure_ascii=False,
                                              sort_keys=True))
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        self._log("sidecar", "sidecar updated for %s" % os.path.basename(path),
                  path)
        return {"ok": True}

    def preview(self, path, values, custom=None):
        """The before-and-after of a change, without writing anything."""
        record = self.records.get(path)
        if record is None:
            return {"ok": False, "error": "no such record"}
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                before = handle.read()
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        after, replaced = apply_header(before, values, record["language"],
                                       custom)
        return {"ok": True, "path": path, "before": before, "after": after,
                "replaced": replaced,
                "diff": _simple_diff(before, after)}

    def undo(self):
        """Undo the last write, restoring the exact previous text."""
        if not self.undo_stack:
            return {"ok": False, "error": "nothing to undo"}
        entry = self.undo_stack.pop()
        try:
            _atomic_write(entry["path"], entry["text"])
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        refreshed = self.read_record(entry["path"], with_git=False)
        if refreshed is not None:
            self.records[entry["path"]] = refreshed
        self._log("undo", "restored %s" % os.path.basename(entry["path"]),
                  entry["path"])
        return {"ok": True, "path": entry["path"],
                "detail": "restored %s" % os.path.basename(entry["path"])}

    def can_undo(self):
        return bool(self.undo_stack)

    # ---------------------------------------------------------- bulk edits
    def bulk_set(self, paths, field, value, append=False):
        """Set one field on many records.

        ``append`` adds to the existing value instead of replacing it, which is
        what a tag should do: adding "legacy" must not remove the tags already
        there.
        """
        if not self.may_edit():
            return {"ok": False, "error": "the current role may not edit"}
        changed = 0
        refused = []
        for path in paths:
            record = self.records.get(path)
            if record is None:
                refused.append(path)
                continue
            values = dict(record.get("values") or {})
            if append and METADATA_FIELD_KINDS.get(field) == "tags":
                existing = [piece.strip() for piece
                            in str(values.get(field) or "").split(",")
                            if piece.strip()]
                if value not in existing:
                    existing.append(value)
                values[field] = ", ".join(existing)
            else:
                values[field] = value
            # A bulk edit changes one field, so it is written even when another
            # field is still missing: refusing it would mean a tag could never
            # be added to a script that still needs an author.  The problem is
            # recorded and reported instead of blocking the whole batch.
            outcome = self.save(path, values, record.get("custom"), force=True)
            if outcome.get("ok"):
                changed += 1
            else:
                refused.append(path)
        summary = {"ok": True, "changed": changed, "refused": len(refused),
                   "detail": "%d record(s) changed" % changed}
        self._log("bulk", "bulk %s=%r on %d record(s)"
                  % (field, value, changed))
        return summary

    def refactor(self, paths, field, pattern, replacement):
        """Rewrite one field across many records with a regular expression."""
        if not self.may_edit():
            return {"ok": False, "error": "the current role may not edit"}
        try:
            expression = re.compile(pattern)
        except re.error as exc:
            return {"ok": False, "error": "bad pattern: %s" % exc}
        changed = 0
        sample = []
        for path in paths:
            record = self.records.get(path)
            if record is None:
                continue
            values = dict(record.get("values") or {})
            before = str(values.get(field) or "")
            after = expression.sub(replacement, before)
            if after == before:
                continue
            values[field] = after
            if self.save(path, values, record.get("custom")).get("ok"):
                changed += 1
                if len(sample) < 5:
                    sample.append("%s: %r -> %r" % (record.get("name"),
                                                    before, after))
        self._log("refactor", "%s rewritten on %d record(s)" % (field, changed))
        return {"ok": True, "changed": changed, "sample": sample,
                "detail": "%d record(s) rewritten" % changed}

    # ------------------------------------------------------------ reporting
    def summary(self):
        """Counts the header and the reports are built from."""
        total = len(self.records)
        valid = sum(1 for record in self.records.values()
                    if record.get("valid"))
        errors = sum(1 for record in self.records.values()
                     for finding in record.get("findings") or []
                     if finding["severity"] == "error")
        warnings = sum(1 for record in self.records.values()
                       for finding in record.get("findings") or []
                       if finding["severity"] == "warning")
        without = sum(1 for record in self.records.values()
                      if not record.get("found_header"))
        languages = {}
        for record in self.records.values():
            key = record.get("language_label") or record.get("language")
            languages[key] = languages.get(key, 0) + 1
        return {"total": total, "valid": valid, "invalid": total - valid,
                "errors": errors, "warnings": warnings,
                "without_header": without, "languages": languages,
                "detail": "%d script(s), %d valid, %d error(s)"
                          % (total, valid, errors)}

    def report(self):
        """The metadata inventory, as plain text."""
        stats = self.summary()
        lines = ["ScriptDB metadata report",
                 "generated: %s" % _now(), "root: %s" % self.root,
                 "role: %s%s" % (self.role,
                                 " (read-only)" if self.read_only else ""),
                 "",
                 "scripts:        %d" % stats["total"],
                 "valid:          %d" % stats["valid"],
                 "with errors:    %d" % stats["invalid"],
                 "without header: %d" % stats["without_header"],
                 ""]
        if stats["languages"]:
            lines.append("Languages")
            for key in sorted(stats["languages"]):
                lines.append("  %-18s %d" % (key, stats["languages"][key]))
            lines.append("")
        broken = [record for record in self.records.values()
                  if not record.get("valid")]
        if broken:
            lines.append("Records that need attention")
            for record in sorted(broken, key=lambda item: item.get("name") or ""):
                reasons = [finding["detail"] for finding
                           in record.get("findings") or []
                           if finding["severity"] == "error"]
                lines.append("  %s" % record.get("path"))
                for reason in reasons[:4]:
                    lines.append("      %s" % reason)
            lines.append("")
        lines += ["", "Export", "",
                  "  JSON: the whole inventory, for a pipeline",
                  "  YAML: the same, in the format a scheduler reads",
                  "  CSV:  one row per script, for a spreadsheet"]
        return "\n".join(lines) + "\n"

    def export(self, path, fmt="json"):
        """Write the inventory out as JSON, YAML or CSV.

        The records are written in a shape a pipeline can consume directly:
        a plain mapping per script, with the metadata flat and the context
        (language, path, git) beside it.
        """
        fmt = (fmt or os.path.splitext(path)[1].lstrip(".") or "json").lower()
        rows = [self._export_record(record)
                for record in sorted(self.records.values(),
                                     key=lambda item: item.get("path") or "")]
        try:
            if fmt == "json":
                _atomic_write(path, json.dumps(
                    {"generated": _now(), "root": self.root,
                     "count": len(rows), "scripts": rows},
                    indent=2, ensure_ascii=False, sort_keys=False))
            elif fmt in ("yaml", "yml"):
                _atomic_write(path, render_yaml(rows))
            elif fmt == "csv":
                _atomic_write(path, render_csv(rows))
            else:
                return {"ok": False, "error": "export writes json, yaml or csv"}
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        self._log("export", "%d record(s) exported to %s" % (len(rows), path))
        return {"ok": True, "path": path, "count": len(rows), "format": fmt,
                "detail": "%d record(s) exported as %s" % (len(rows), fmt)}

    @staticmethod
    def _export_record(record):
        values = record.get("values") or {}
        out = {key: values.get(key, "") for key in METADATA_FIELD_KEYS}
        out.update(record.get("custom") or {})
        out["_path"] = record.get("path", "")
        out["_language"] = record.get("language", "")
        out["_valid"] = bool(record.get("valid"))
        out["_kind"] = values.get("kind") or "script"
        return out

    def audit_text(self):
        """The audit trail as plain text, for the log panel and a report."""
        lines = ["ScriptDB metadata changes", "generated: %s" % _now(), ""]
        if not self.audit:
            lines.append("No change has been recorded yet.")
            return "\n".join(lines) + "\n"
        lines.append("%-20s %-10s %-12s %s" % ("time", "role", "action",
                                               "detail"))
        for entry in self.audit[:500]:
            lines.append("%-20s %-10s %-12s %s"
                         % (entry.get("time", ""), entry.get("role", ""),
                            entry.get("action", ""), entry.get("detail", "")))
        return "\n".join(lines) + "\n"
# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------
def _file_time(path, which):
    """A file's created or modified time, in the application's own format."""
    try:
        stamp = os.path.getmtime(path) if which == "modified" \
            else os.path.getctime(path)
    except OSError:
        return ""
    return dt.datetime.fromtimestamp(stamp).strftime("%Y-%m-%d %H:%M:%S")


def _git(arguments, folder):
    """Run one git command quietly; an empty string when it cannot answer."""
    import subprocess
    try:
        result = subprocess.run(["git"] + arguments, cwd=folder,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL,
                                timeout=4)
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.decode("utf-8", errors="replace").strip()


def _atomic_write(path, text):
    """Write a file so a crash cannot leave it half-written."""
    import tempfile
    folder = os.path.dirname(path) or "."
    handle, temp = tempfile.mkstemp(dir=folder, prefix=".meta-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    except Exception:
        try:
            os.remove(temp)
        except OSError:
            pass
        raise


def _simple_diff(before, after, context=1):
    """A unified-style diff of two bodies, as a list of lines.

    This is the same shape a person reads in a terminal, so the preview panel
    can show it without a second diff implementation.
    """
    import difflib
    lines = list(difflib.unified_diff(
        (before or "").splitlines(),
        (after or "").splitlines(),
        fromfile="before", tofile="after", n=context, lineterm=""))
    return lines[:400]


def render_yaml(rows):
    """The inventory as YAML, written by hand so there is no dependency.

    Only the shapes the inventory actually contains are emitted -- mappings,
    lists and scalars -- which is all a scheduler needs to read.
    """
    def scalar(value):
        if value is None or value == "":
            return '""'
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return str(value)
        text = str(value)
        if "\n" in text:
            body = "\n".join("      " + line for line in text.split("\n"))
            return "|\n" + body
        if any(char in text for char in ":#{}[],&*?|-<>=!%@`\"'"):
            return chr(34) + text.replace(chr(34), chr(92) + chr(34)) + chr(34)
        return text

    lines = ["# ScriptDB metadata inventory",
             "generated: %s" % _now(),
             "count: %d" % len(rows),
             "scripts:"]
    for row in rows:
        first = row.get("name") or row.get("_path") or "script"
        lines.append("  - name: %s" % scalar(first))
        for key in sorted(row):
            if key == "name":
                continue
            lines.append("    %s: %s" % (key, scalar(row[key])))
    return "\n".join(lines) + "\n"


def render_csv(rows):
    """The inventory as CSV, one row per script."""
    import csv
    import io
    if not rows:
        return "name\n"
    columns = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    # The identifying columns first, so a spreadsheet opens on something useful.
    preferred = [key for key in ("name", "version", "author", "status", "tags",
                                 "_language", "_path", "_valid")
                 if key in columns]
    ordered = preferred + [key for key in columns if key not in preferred]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=ordered, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: row.get(key, "") for key in ordered})
    return buffer.getvalue()


def capabilities():
    """The metadata manager as a plugin-safe catalogue."""
    return {
        "fields": {key: {"label": METADATA_FIELD_LABELS[key],
                         "kind": METADATA_FIELD_KINDS[key],
                         "group": METADATA_FIELD_GROUPS[key]}
                   for key in METADATA_FIELD_KEYS},
        "groups": list(METADATA_GROUPS),
        "roles": {key: METADATA_ROLE_LABELS[key] for key in METADATA_ROLE_KEYS},
        "stores": {key: METADATA_STORE_LABELS[key] for key in METADATA_STORE_KEYS},
        "rules": list(VALIDATION_RULE_KEYS),
        "choices": {key: [value for value, _lk, _f in options]
                    for key, options in METADATA_CHOICES.items()},
        "operations": ["scan", "read_record", "records_list", "save",
                       "preview", "undo", "bulk_set", "refactor", "export",
                       "summary", "report", "audit_text"],
        "status": "available",
    }
