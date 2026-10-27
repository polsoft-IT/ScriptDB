# -*- coding: utf-8 -*-
"""Reports tab for ScriptDB -- a repository of generated report artifacts.

This module owns the "Reports" tab: every artifact the application (or the
user) produces -- audit reports, analyzer output, diff reports, search exports,
document previews, log dumps -- is collected here as a *report* and can be
browsed, grouped and filtered without re-running anything. It follows the same
one-file-per-tab pattern as the other tabs:

    ReportsTab(window)            -- build the tab
    tab.refresh()                 -- rescan the report repository
    tab.retranslate(table)        -- apply a language table (see TRANSLATIONS)
    tab.visible_reports()         -- the reports matching the current filters
    tab.open_report(report)       -- show a report's content
    tab.send_to_docs(report)      -- store a report in the DOCs repository

The heavy lifting lives in :class:`ReportRepository`, a small dependency-free
class that scans one or more directories for report files and describes each
one (project, module, type, date, tags, size). Classification is filename and
content based, so a report dropped into a watched directory appears here on the
next scan without any registration step.

Every report can also be *previewed* offline through four views, all pure
functions in this module (see :data:`PREVIEW_KINDS`):

    ascii       the body as printable ASCII (optional transliteration)
    structure   the sections, headings and blocks in document order
    metadata    generation time, source (module / script / process), size, hash
    minimap     a compact density bar with the report's section blocks

Everything is offline: scanning, filtering, previewing and reading use the
standard library only, and nothing is ever uploaded.
"""

import datetime as dt
import hashlib
import json
import os
import re

from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtGui import QFont

# Where reports are looked for by default, under the project base directory.
REPORT_DIRS = ("reports", "audit_exports", "analyzer_exports", "exports")
# File extensions that count as a report artifact.
REPORT_EXTENSIONS = (".txt", ".md", ".json", ".log", ".csv", ".xml",
                     ".patch", ".diff", ".rst", ".yaml", ".yml", ".ini",
                     ".cfg", ".conf")
REPORTS_FONT = "Consolas"
REPORTS_FONT_SIZE = 10
# How often the premium watcher polls for a file change (milliseconds).
PREMIUM_POLL_MS = 3000
# The report directory generated reports are written into, under the project.
GENERATED_DIR = "reports"

# Grouping keys offered by the UI: (key, label key, fallback).
GROUPINGS = (
    ("project", "reports_group_project", "By project"),
    ("module", "reports_group_module", "By module"),
    ("type", "reports_group_type", "By report type"),
    ("date", "reports_group_date", "By date"),
    ("tags", "reports_group_tags", "By tag"),
    ("none", "reports_group_none", "Flat list"),
)

# Quick filters: (key, label key, fallback, predicate-name).
QUICK_FILTERS = (
    ("last24h", "reports_filter_24h", "Last 24h"),
    ("lastweek", "reports_filter_week", "Last week"),
    ("errors", "reports_filter_errors", "Errors"),
    ("tests", "reports_filter_tests", "Tests"),
    ("diff", "reports_filter_diff", "Diff"),
    ("analysis", "reports_filter_analysis", "Code analysis"),
)

# Report types recognised by the classifier, with the words that mark them.
# Order matters: the first matching entry wins, so the more specific markers
# (diff, audit, test, build, lint, deploy, diagnostics, performance, config,
# environment, snapshot) are listed before the broad "summary" fallback.
REPORT_TYPES = (
    ("diff", ("diff", "patch", "compare", "changes", "changeset")),
    ("audit", ("audit", "policy", "compliance", "security", "trail")),
    ("test", ("test", "suite", "pytest", "unittest", "coverage", "junit")),
    ("build", ("build", "package", "make", "compile", "cmake", "msbuild")),
    ("lint", ("lint", "flake8", "pylint", "eslint", "style", "checkstyle")),
    ("deploy", ("deploy", "release", "publish", "rollout", "provision")),
    ("diagnostic", ("diagnos", "doctor", "healthcheck", "health", "probe",
                    "troubleshoot")),
    ("performance", ("perform", "benchmark", "timing", "latency", "throughput",
                     "profil", "hotspot")),
    ("config", ("config", "conf", "settings", "ini", "toml", "yaml", "yml")),
    ("environment", ("environ", "env", "platform", "system_info", "runtime")),
    ("snapshot", ("snapshot", "manifest", "baseline", "inventory", "tree")),
    ("log", ("log", "journal", "console", "syslog")),
    ("search", ("search", "find", "match")),
    ("metrics", ("metric", "count", "stat", "aggregate")),
    ("error", ("error", "fail", "crash", "traceback", "exception")),
    ("analysis", ("analy", "quality", "structure", "score", "review")),
    ("document", ("preview", "readme")),
    ("summary", ("summary", "report", "overview", "result")),
)

# The preview views offered by the Reports tab, in menu order. Each entry is
# (key, label key, fallback), mirroring the DOCs preview menu.
PREVIEW_KINDS = (
    ("ascii", "reports_preview_ascii", "ASCII / plain text"),
    ("structure", "reports_preview_structure", "Structure (sections / blocks)"),
    ("metadata", "reports_preview_metadata", "Metadata"),
    ("minimap", "reports_preview_minimap", "Mini-map"),
)

# Words that mark a report as carrying errors or warnings (for the preview
# counts and the errors/warnings search).
ERROR_WORDS = ("error", "exception", "traceback", "fatal", "critical",
               "failed", "failure")
WARNING_WORDS = ("warning", "warn", "deprecated", "caution", "notice")
_DEFAULT_TYPE = "summary"

# Words that let a report be tagged automatically from its text.
TAG_WORDS = ("error", "warning", "fail", "pass", "timeout", "todo",
             "fixme", "deprecated", "security", "performance")


def _now():
    return dt.datetime.now()


def _fmt_stamp(value):
    return value.strftime("%Y-%m-%d %H:%M:%S")



class Report(object):
    """One report artifact (a file on disk plus what we learned about it)."""

    __slots__ = ("path", "name", "size", "modified", "modified_ts", "kind",
                 "project", "module", "tags", "summary", "content",
                 "error_count", "warning_count")

    def __init__(self, path, name, size, modified, modified_ts, kind, project,
                 module, tags, summary="", content="", error_count=0,
                 warning_count=0):
        self.path = path
        self.name = name
        self.size = size
        self.modified = modified
        self.modified_ts = modified_ts
        self.kind = kind
        self.project = project
        self.module = module
        self.tags = tags
        self.summary = summary
        # A lower-cased excerpt of the body, kept in memory so a content search
        # needs no disk read for every keystroke.
        self.content = content
        self.error_count = error_count
        self.warning_count = warning_count

    def matches_content(self, needle):
        """True when a search word appears in the report's text."""
        needle = (needle or "").strip().lower()
        if not needle:
            return True
        return needle in self.content.lower() or needle in self.summary.lower()

    def has_problems(self):
        """True when the report mentions errors or warnings."""
        return bool(self.error_count or self.warning_count)

    def group_value(self, key):
        """The value this report sorts under for one grouping key."""
        if key == "project":
            return self.project or "(none)"
        if key == "module":
            return self.module or "(none)"
        if key == "type":
            return self.kind
        if key == "date":
            return self.modified[:10]
        if key == "tags":
            return self.tags[0] if self.tags else "(untagged)"
        return ""

    def __repr__(self):
        return "Report(%r, %r)" % (self.name, self.kind)


class ReportRepository(object):
    """Scans directories and describes every report artifact it finds. The repository is read-only: it never writes into the watched directories, it only lists and reads them. Classification uses the file name first, then a short read of the head of the file for words that name the type and tags."""

    # How much of each file is read for a content search / problem count.
    content_limit = 65536

    def __init__(self, directories=(), extension_filter=REPORT_EXTENSIONS):
        self.directories = [d for d in directories if d]
        self.extensions = tuple(e.lower() for e in extension_filter)

    def add_directory(self, path):
        if path and path not in self.directories:
            self.directories.append(path)

    def scan(self, recursive=True, limit=2000):
        """Every report below the watched directories, newest first."""
        reports = []
        for directory in list(self.directories):
            if not os.path.isdir(directory):
                continue
            for report in self._scan_directory(directory, recursive, limit):
                reports.append(report)
                if len(reports) >= limit:
                    break
        reports.sort(key=lambda item: item.modified_ts, reverse=True)
        return reports

    def _scan_directory(self, directory, recursive, limit):
        out = []
        walker = os.walk(directory) if recursive else [(directory, [], [
            f for f in os.listdir(directory)
            if os.path.isfile(os.path.join(directory, f))])]
        for root, _dirs, files in walker:
            for filename in files:
                if not filename.lower().endswith(self.extensions):
                    continue
                path = os.path.join(root, filename)
                report = self.describe(path, root)
                if report is not None:
                    out.append(report)
                if len(out) >= limit:
                    return out
        return out

    def describe(self, path, root=""):
        """Build a :class:`Report` for one file (or None when unreadable)."""
        try:
            stat = os.stat(path)
        except OSError:
            return None
        modified = dt.datetime.fromtimestamp(stat.st_mtime)
        # Read a generous excerpt: enough for a content search and for the
        # error / warning counts, without loading a huge file into memory.
        body = self._read_head(path, limit=self.content_limit)
        name = os.path.basename(path)
        project = self._project_for(path, root)
        # The immediate folder is the module; when a file sits directly in the
        # project folder the two coincide, and the folder name is still the
        # most useful module label.
        module = os.path.basename(root) if root else ""
        kind = classify(name, body)
        tags = classify_tags(name, body)
        summary = self._summary(body)
        errors, warnings = count_problems(body)
        return Report(path, name, stat.st_size, _fmt_stamp(modified),
                      stat.st_mtime, kind, project, module, tags, summary,
                      content=body, error_count=errors,
                      warning_count=warnings)

    def _read_head(self, path, limit=4096):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                return handle.read(limit)
        except OSError:
            return ""

    def _project_for(self, path, root):
        # The first directory under a watched root names the project; a file
        # sitting directly in the root belongs to the root's own name.
        for directory in self.directories:
            if not path.startswith(directory):
                continue
            relative = os.path.relpath(path, directory)
            parts = relative.split(os.sep)
            if len(parts) > 1:
                return parts[0]
            return os.path.basename(directory.rstrip(os.sep)) or directory
        return os.path.basename(os.path.dirname(path)) or ""

    def _summary(self, head):
        for line in (head or "").splitlines():
            stripped = line.strip().lstrip("#-*= ")
            if stripped:
                return stripped[:120]
        return ""


def classify(name, head=""):
    """The report type key for a file name and its head text."""
    haystack = (name + "\n" + (head or "")).lower()
    for kind, words in REPORT_TYPES:
        for word in words:
            if word in haystack:
                return kind
    return _DEFAULT_TYPE


def count_problems(text):
    """(error_count, warning_count) found in a report body. The count is line based: a line is an error when it contains an error word and a warning when it contains a warning word. A line that is both counts as an error only, so the two totals never double-count a line."""
    errors = warnings = 0
    for line in (text or "").splitlines():
        lowered = line.lower()
        if any(word in lowered for word in ERROR_WORDS):
            errors += 1
        elif any(word in lowered for word in WARNING_WORDS):
            warnings += 1
    return errors, warnings


def classify_tags(name, head=""):
    """The tags detected in a report (deduplicated, in a stable order)."""
    haystack = (name + "\n" + (head or "")).lower()
    found = []
    for word in TAG_WORDS:
        if word in haystack:
            found.append(word)
    # A JSON report may carry its own kind/tags; surface them too.
    for key in ("error", "warning", "fail"):
        if key in haystack and key not in found:
            found.append(key)
    return found



# ---------------------------------------------------------------------------
# Offline preview
#
# Four views over a single report artifact, all standard-library only:
#
#     preview_ascii     the body as printable ASCII (optional transliteration)
#     report_structure  the sections, headings and blocks in document order
#     report_minimap    a compact density bar (the mini-map)
#     report_metadata   generation time, source, size, hash and counts
#
# They are pure functions over text (plus a small file read for the hash), so
# they can be unit tested without a GUI and never touch the network.
# ---------------------------------------------------------------------------

# A small transliteration table so preview_ascii(ascii_only=True) yields
# readable ASCII for the accented characters a European report usually has.
_ASCII_FOLD = {
    "\u00e0": "a", "\u00e1": "a", "\u00e2": "a", "\u00e3": "a", "\u00e4": "a",
    "\u00e5": "a", "\u00e7": "c", "\u00e8": "e", "\u00e9": "e", "\u00ea": "e",
    "\u00eb": "e", "\u00ec": "i", "\u00ed": "i", "\u00ee": "i", "\u00ef": "i",
    "\u00f1": "n", "\u00f2": "o", "\u00f3": "o", "\u00f4": "o", "\u00f5": "o",
    "\u00f6": "o", "\u00f9": "u", "\u00fa": "u", "\u00fb": "u", "\u00fc": "u",
    "\u00fd": "y", "\u00ff": "y", "\u0105": "a", "\u0107": "c", "\u0119": "e",
    "\u0142": "l", "\u0144": "n", "\u015b": "s", "\u017a": "z", "\u017c": "z",
    "\u0171": "u", "\u0111": "d", "\u0161": "s", "\u010d": "c", "\u0159": "r",
    "\u00e6": "ae", "\u0153": "oe", "\u00df": "ss", "\u2018": "'",
    "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2013": "-",
    "\u2014": "-", "\u2026": "...", "\u2022": "*",
}
_ASCII_MAP = str.maketrans(_ASCII_FOLD)
# Density symbols from lightest to heaviest, used by the mini-map.
_DENSITY = " .-=#"


def _fold_ascii(text):
    """Transliterate a string to printable ASCII (best effort)."""
    if not text:
        return ""
    folded = text.translate(_ASCII_MAP)
    # Drop any remaining non-ASCII character rather than emitting mojibake.
    return "".join(ch if ord(ch) < 128 else "" for ch in folded)


def preview_ascii(text, width=100, ascii_only=False, wrap=True):
    """A plain-text (ASCII) preview of a report body. Non-printable characters are dropped and long lines are wrapped to `width`. When `ascii_only` is set, characters outside ASCII are transliterated to a close ASCII equivalent. Returns one string; nothing is rendered and nothing leaves the machine."""
    if not text:
        return ""
    prepared = _fold_ascii(text) if ascii_only else text
    cleaned = "".join(
        ch if (ch == "\n" or ch == "\t" or ch >= " ") else ""
        for ch in prepared)
    if not wrap or width <= 0:
        return cleaned
    lines = []
    for raw in cleaned.splitlines():
        line = raw.expandtabs(4)
        while len(line) > width:
            lines.append(line[:width])
            line = line[width:]
        lines.append(line)
    return "\n".join(lines)


def report_structure(text):
    """The structure of a report: sections, headings and blocks in order.

    Recognises Markdown ATX (`#`) and Setext (`===`) headings, `== banner ==`
    sections, section titles ending in ':', numbered/lettered section titles,
    fenced and indented code blocks and list runs. Returns a list of records in
    document order, each a dict with:

        kind   heading / section / code / list / rule
        line   1-based line number
        level  heading depth (0 for non-headings)
        text   the trimmed heading / label

    A purely offline, line-based reader -- no parser dependency.
    """
    lines = (text or "").splitlines()
    records = []
    in_fence = False
    fence_start = 0
    list_items = 0
    list_start = 0
    def flush_list():
        # Emit the pending list run where it started (a list is a single
        # record, but the line keeps the document order meaningful).
        if list_items:
            records.append({"kind": "list", "line": list_start, "level": 0,
                            "text": "%d list item(s)" % list_items})
        return 0
    for number, raw in enumerate(lines, 1):
        stripped = raw.strip()
        # Fenced code blocks (``` or ~~~).
        if stripped.startswith("```") or stripped.startswith("~~~"):
            list_items = flush_list()
            if in_fence:
                records.append({"kind": "code", "line": fence_start, "level": 0,
                                "text": "%d code line(s)" % (number - fence_start)})
                in_fence = False
            else:
                in_fence = True
                fence_start = number
            continue
        if in_fence:
            continue
        if not stripped:
            list_items = flush_list()
            continue
        # Markdown ATX heading.
        if stripped.startswith("#"):
            level = len(stripped) - len(stripped.lstrip("#"))
            title = stripped[level:].strip().rstrip("#").strip()
            if title:
                list_items = flush_list()
                records.append({"kind": "heading", "line": number,
                                "level": min(level, 6), "text": title})
                continue
        # A `== banner ==` section title.
        if len(stripped) >= 5 and stripped[:2] in ("==", "--") \
                and stripped[-2:] == stripped[:2] and stripped[2:-2].strip():
            list_items = flush_list()
            records.append({"kind": "section", "line": number, "level": 0,
                            "text": stripped[2:-2].strip()})
            continue
        # A short line ending in ':' is a section title.
        if stripped.endswith(":") and len(stripped) <= 80:
            list_items = flush_list()
            records.append({"kind": "section", "line": number, "level": 0,
                            "text": stripped.rstrip(":").strip()})
            continue
        # A numbered / lettered section title, e.g. "1. Build" or "IV. Notes".
        if re.match(r"^(\d+|[IVXLC]+)[.)]\s+\S", stripped) \
                and len(stripped) <= 80:
            list_items = flush_list()
            records.append({"kind": "section", "line": number, "level": 0,
                            "text": stripped})
            continue
        # Setext heading: the line above a === or --- rule.
        if set(stripped) <= set("=-") and len(stripped) >= 3:
            previous = lines[number - 2].strip() if number >= 2 else ""
            list_items = flush_list()
            if previous:
                records.append({"kind": "heading", "line": number - 1,
                                "level": 1 if stripped[0] == "=" else 2,
                                "text": previous})
            else:
                records.append({"kind": "rule", "line": number, "level": 0,
                                "text": ""})
            continue
        # List run.
        if stripped[:2] in ("- ", "* ", "+ ") or (
                stripped[:1].isdigit() and ". " in stripped[:5]):
            if list_items == 0:
                list_start = number
            list_items += 1
            continue
        list_items = flush_list()
    list_items = flush_list()
    if in_fence:
        records.append({"kind": "code", "line": fence_start, "level": 0,
                        "text": "%d code line(s)" % (len(lines) - fence_start)})
    records.sort(key=lambda rec: (rec["line"], rec["kind"] != "heading"))
    return records


def render_structure(records):
    """The structure records as text lines (sections, headings, blocks)."""
    if not records:
        return []
    lines = []
    for rec in records:
        if rec["kind"] == "heading":
            lines.append("%s %s" % ("  " * rec["level"] + "#" * rec["level"],
                                    rec["text"]))
        elif rec["kind"] == "section":
            lines.append("== %s ==" % rec["text"])
        elif rec["kind"] == "code":
            lines.append("[code] line %d: %s" % (rec["line"], rec["text"]))
        elif rec["kind"] == "list":
            lines.append("[list] line %d: %s" % (rec["line"], rec["text"]))
        elif rec["kind"] == "rule":
            lines.append("-" * 40)
    return lines


def _density_row(lines, buckets):
    """One mini-map bar: the per-slice density of a group of lines."""
    if not lines:
        return ""
    widest = max((len(line) for line in lines), default=0)
    width = max(1, widest // buckets or 1)
    weights = []
    for index in range(buckets):
        weight = 0
        for line in lines:
            segment = line[index * width:index * width + width]
            weight += len(segment.strip())
        weights.append(weight)
    peak = max(weights) or 1
    span = len(_DENSITY) - 1
    cells = []
    for weight in weights:
        level = int(round(weight / peak * span))
        cells.append(_DENSITY[max(0, min(span, level))])
    return "".join(cells)


def report_minimap(text, rows=16, buckets=72):
    """A compact mini-map bar of a report as a list of `rows` strings. Each symbol shows how dense that slice of the report is: a space is empty, '.' light, '-' medium, '=' heavy and '#' very heavy. It gives an at-a- glance shape of the whole report without rendering anything."""
    lines = (text or "").splitlines()
    if not lines:
        return ["" for _ in range(rows)]
    step = max(1, len(lines) // rows)
    sample = [lines[i:i + step] for i in range(0, len(lines), step)][:rows]
    while len(sample) < rows:
        sample.append([])
    return [_density_row(group, buckets) for group in sample]


def report_section_blocks(text, blocks=40):
    """A one-symbol-per-block map of a report's sections (the mini-map bar).

    The report is split into `blocks` equal slices of lines; each slice becomes
    one symbol whose weight is how much text (and how many headings) it holds:

        ' empty   '.' light   '-' medium   '=' heavy   '#' very heavy

    The result is a single dense string, so a section that is mostly headings
    stands out in the bar next to the section that is mostly body text.
    """
    lines = (text or "").splitlines()
    if not lines:
        return ""
    span = max(1, len(lines) // blocks or 1)
    slices = [lines[i:i + span] for i in range(0, len(lines), span)][:blocks]
    cells = []
    for group in slices:
        body = sum(len(line.strip()) for line in group)
        headings = sum(1 for line in group if line.lstrip().startswith("#"))
        weight = body + headings * 20
        peak = max((sum(len(l.strip()) for l in g) + 20 * sum(
            1 for l in g if l.lstrip().startswith("#")) for g in slices),
            default=1) or 1
        level = int(round(weight / peak * (len(_DENSITY) - 1)))
        cells.append(_DENSITY[max(0, min(len(_DENSITY) - 1, level))])
    return "".join(cells)


def report_hash(path, algorithm="sha256", chunk=65536):
    """A hex digest of a report file (or "" when it cannot be read)."""
    digest = hashlib.new(algorithm)
    try:
        with open(path, "rb") as handle:
            while True:
                block = handle.read(chunk)
                if not block:
                    break
                digest.update(block)
    except (OSError, ValueError):
        return ""
    return digest.hexdigest()


def _read_report(report, limit=200000):
    """The text of a report (capped so a huge file cannot hang the UI)."""
    try:
        with open(report.path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read(limit)
    except OSError:
        return ""


def _header_field(body, names):
    """The value of the first header field from `names` in a report body."""
    pattern = r"^\s*(?:%s)\s*[:=]\s*(.+)$" % "|".join(names)
    for line in (body or "").splitlines()[:20]:
        match = re.match(pattern, line, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return ""


def report_metadata(report, body=None, algorithm="sha256"):
    """Rich metadata for the metadata preview (all offline). `report` is a :class:`Report`; `body` is the report text (read when not given). Returns a dict with the generation time, the source (module, script, process), the size, the content hash and the line / word counts."""
    if body is None:
        body = _read_report(report)
    body = body or ""
    words = re.findall(r"[A-Za-z0-9_]+", body)
    lines = body.splitlines()
    return {
        "name": report.name,
        "type": report.kind,
        "project": report.project,
        "module": report.module,
        "script": _header_field(body, ("script", "command", "cmd", "tool",
                                       "generator")),
        "process": _header_field(body, ("process", "pid", "runner", "job",
                                        "task")),
        "source": _header_field(body, ("source", "path", "file", "from",
                                       "origin")) or report.path,
        "path": report.path,
        "size": report.size,
        "size_human": _human_size(report.size),
        "generated": report.modified,
        "modified_ts": report.modified_ts,
        "tags": list(report.tags),
        "summary": report.summary,
        "characters": len(body),
        "lines": len(lines),
        "words": len(words),
        "unique_words": len(set(w.lower() for w in words)),
        "errors": report.error_count,
        "warnings": report.warning_count,
        "hash_algo": algorithm,
        "hash": report_hash(report.path, algorithm=algorithm),
    }


def render_metadata(meta, labels=None):
    """The metadata dict as aligned 'key : value' lines (labels optional)."""
    labels = labels or {}

    def lab(key, fallback):
        return labels.get(key, fallback)

    rows = [
        (lab("reports_meta_name", "Name"), meta["name"]),
        (lab("reports_meta_type", "Type"), meta["type"]),
        (lab("reports_meta_project", "Project"), meta["project"] or "-"),
        (lab("reports_meta_module", "Module"), meta["module"] or "-"),
        (lab("reports_meta_script", "Script"), meta["script"] or "-"),
        (lab("reports_meta_process", "Process"), meta["process"] or "-"),
        (lab("reports_meta_source", "Source"), meta["source"] or "-"),
        (lab("reports_meta_path", "Path"), meta["path"] or "-"),
        (lab("reports_meta_generated", "Generated"), meta["generated"]),
        (lab("reports_meta_size", "Size"),
         "%s (%d bytes)" % (meta["size_human"], meta["size"])),
        (lab("reports_meta_lines", "Lines"), meta["lines"]),
        (lab("reports_meta_words", "Words"), meta["words"]),
        (lab("reports_meta_unique", "Unique words"), meta["unique_words"]),
        (lab("reports_meta_chars", "Characters"), meta["characters"]),
        (lab("reports_meta_errors", "Errors"), meta["errors"]),
        (lab("reports_meta_warnings", "Warnings"), meta["warnings"]),
        (lab("reports_meta_tags", "Tags"), ", ".join(meta["tags"]) or "-"),
        (lab("reports_meta_hash", "Hash (%s)" % meta["hash_algo"]),
         meta["hash"] or "-"),
    ]
    width = max((len(str(label)) for label, _ in rows), default=0)
    return "\n".join("%-*s : %s" % (width, label, value)
                     for label, value in rows)


def preview_report(report, kind="ascii", body=None, ascii_only=False,
                   labels=None):
    """Render one preview view for a report as plain text (the dispatcher). `kind` is one of the keys in :data:`PREVIEW_KINDS`. This is the single entry point the tab uses, so every view is reachable from one call and can be exercised without a GUI."""
    if body is None:
        body = _read_report(report)
    body = body or ""
    if kind == "ascii":
        return preview_ascii(body, width=100, ascii_only=ascii_only)
    if kind == "structure":
        return "\n".join(render_structure(report_structure(body)))
    if kind == "minimap":
        return "\n".join(report_minimap(body))
    if kind == "metadata":
        return render_metadata(report_metadata(report, body=body),
                               labels=labels)
    return body



# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


def filter_reports(reports, now=None, quick=None, kind=None, text="",
                   project=None, module=None, tags=None, name="",
                   content="", problems=None, since=None):
    """Return the reports matching the given filters.

    `quick` is one of the keys in :data:`QUICK_FILTERS` (or None); `kind`,
    `project`, `module` and `tags` narrow further, and `text` matches the name,
    type, project or summary (case-insensitive).

    The dedicated searches are applied here too so the UI has one entry point:
    `name` (file name), `content` (body text), `problems`
    ('any'/'error'/'warning') and `since` (a date or datetime lower bound for
    the generation history).
    """
    now = now or _now()
    result = list(reports)
    if quick == "last24h":
        cutoff = (now - dt.timedelta(hours=24)).timestamp()
        result = [r for r in result if r.modified_ts >= cutoff]
    elif quick == "lastweek":
        cutoff = (now - dt.timedelta(days=7)).timestamp()
        result = [r for r in result if r.modified_ts >= cutoff]
    elif quick == "errors":
        result = [r for r in result if r.kind == "error"
                  or "error" in r.tags or "fail" in r.tags]
    elif quick == "tests":
        result = [r for r in result if r.kind == "test"]
    elif quick == "diff":
        result = [r for r in result if r.kind == "diff"]
    elif quick == "analysis":
        result = [r for r in result if r.kind in ("analysis", "summary")]
    if kind:
        result = [r for r in result if r.kind == kind]
    if project:
        result = [r for r in result if r.project == project]
    if module:
        result = [r for r in result if r.module == module]
    if tags:
        wanted = set(tags)
        result = [r for r in result if wanted & set(r.tags)]
    if name:
        result = search_by_name(result, name)
    if content:
        result = search_by_content(result, content)
    if problems:
        result = search_problems(result, level=problems, needle=text or "")
    if since is not None:
        stamp = _parse_since(since)
        if stamp is not None:
            result = [r for r in result if r.modified_ts >= stamp]
    needle = (text or "").strip().lower()
    if needle and not problems:
        result = [r for r in result
                  if needle in r.name.lower() or needle in r.kind.lower()
                  or needle in (r.project or "").lower()
                  or needle in r.summary.lower()
                  or r.matches_content(needle)]
    return result


def group_reports(reports, key):
    """Group reports by one grouping key -> [(group, [reports]), ...]. The groups are ordered: by name, except the date grouping which is ordered newest first, and the flat list which is a single group."""
    if key in ("", "none", None):
        return [("", list(reports))]
    buckets = {}
    for report in reports:
        value = report.group_value(key)
        buckets.setdefault(value, []).append(report)
    groups = sorted(buckets.items(), key=lambda item: str(item[0]).lower())
    if key == "date":
        groups.sort(key=lambda item: item[0], reverse=True)
    return groups


# ---------------------------------------------------------------------------
# Search
#
# The tab offers seven searches; each is a small pure function over a report
# list, so they can be combined and unit tested without a GUI:
#
#     name      the report file name
#     content   the report body (a word anywhere in the text)
#     tags      one or more tags
#     type      the report kind (audit, diff, test, build, deploy, lint...)
#     problems  errors and / or warnings inside the report
#     history   the generation timeline (reports grouped by the day they
#               were produced)
#     text      the free box that searches name, type, project and summary
# ---------------------------------------------------------------------------


def search_by_name(reports, needle):
    """Reports whose file name contains `needle` (case-insensitive)."""
    needle = (needle or "").strip().lower()
    if not needle:
        return list(reports)
    return [r for r in reports if needle in r.name.lower()]


def search_by_content(reports, needle):
    """Reports whose body contains `needle` (case-insensitive)."""
    needle = (needle or "").strip().lower()
    if not needle:
        return list(reports)
    return [r for r in reports if r.matches_content(needle)]


def search_by_tags(reports, tags, match_all=False):
    """Reports carrying the given tag(s). `tags` may be a string ('error,timeout') or an iterable. When `match_all` is true a report must carry every tag, otherwise any one is enough."""
    if isinstance(tags, str):
        wanted = [t.strip().lower() for t in re.split(r"[,\s]+", tags)
                  if t.strip()]
    else:
        wanted = [str(t).strip().lower() for t in (tags or []) if str(t).strip()]
    if not wanted:
        return list(reports)
    out = []
    for report in reports:
        have = set(t.lower() for t in report.tags)
        if (set(wanted) <= have) if match_all else (set(wanted) & have):
            out.append(report)
    return out


def search_by_type(reports, kind):
    """Reports of one type (or several, comma separated)."""
    if isinstance(kind, str):
        wanted = {k.strip().lower() for k in kind.split(",") if k.strip()}
    else:
        wanted = {str(k).strip().lower() for k in (kind or []) if str(k).strip()}
    if not wanted:
        return list(reports)
    return [r for r in reports if r.kind.lower() in wanted]


def search_problems(reports, level="any", needle=""):
    """Reports that mention errors and / or warnings. `level` is 'any', 'error' or 'warning'; `needle` narrows further by the report text, so "error: timeout" finds the reports whose errors mention a timeout."""
    needle = (needle or "").strip().lower()
    out = []
    for report in reports:
        if level == "error" and not report.error_count:
            continue
        if level == "warning" and not report.warning_count:
            continue
        if level == "any" and not report.has_problems():
            continue
        if needle and not report.matches_content(needle):
            continue
        out.append(report)
    return out


def generation_history(reports):
    """The report generation timeline: [(day, [reports]), ...]. Every day the application produced at least one report becomes an entry, newest first, with the reports of that day in it. This is the data behind the "search in the generation history" view."""
    buckets = {}
    for report in reports:
        day = report.modified[:10]
        buckets.setdefault(day, []).append(report)
    history = []
    for day in sorted(buckets, reverse=True):
        items = sorted(buckets[day], key=lambda r: r.modified_ts, reverse=True)
        history.append((day, items))
    return history


def search_history(reports, needle="", kind="", since=None):
    """Search the generation history and return the matching reports. `needle` matches the report name or content, `kind` narrows to a type and `since` is a datetime or a 'YYYY-MM-DD' string; without it the whole history is searched. The result is time ordered (newest first)."""
    result = list(reports)
    if kind:
        result = search_by_type(result, kind)
    if since is not None:
        stamp = _parse_since(since)
        if stamp is not None:
            result = [r for r in result if r.modified_ts >= stamp]
    needle = (needle or "").strip().lower()
    if needle:
        result = [r for r in result
                  if needle in r.name.lower() or r.matches_content(needle)]
    result.sort(key=lambda r: r.modified_ts, reverse=True)
    return result


def _parse_since(value):
    # Accept a datetime, a timestamp or a 'YYYY-MM-DD' string (or None).
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dt.datetime):
        return value.timestamp()
    for fmt, length in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d", 10)):
        try:
            return dt.datetime.strptime(str(value)[:length], fmt).timestamp()
        except ValueError:
            continue
    return None


def repository_capabilities():
    """Describe the report repository for the audit / about layer."""
    from . import reports_analysis
    capabilities = {
        "groupings": [key for key, _lk, _f in GROUPINGS],
        "quick_filters": [key for key, _lk, _f in QUICK_FILTERS],
        "report_types": [kind for kind, _words in REPORT_TYPES],
        "preview_kinds": [key for key, _lk, _f in PREVIEW_KINDS],
        "extensions": list(REPORT_EXTENSIONS),
        "offline": True,
    }
    from . import reports_premium
    capabilities.update(reports_analysis.analysis_capabilities())
    capabilities.update(reports_premium.premium_capabilities())
    return capabilities
# ---------------------------------------------------------------------------
# The Qt tab
# ---------------------------------------------------------------------------


class ReportsTab(QtWidgets.QWidget):
    """A repository of generated report artifacts: browse, group and filter. The tab never regenerates a report; it collects the artifacts produced elsewhere (audit, analyzer, diff, search, document preview) and makes them findable. Every control is offline and reads only local files."""

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.repo = None
        self._reports = []
        self._current = None
        self._build_ui()
        self.refresh()

    # --------------------------------------------------------------- helpers
    def t(self, key, fallback=None, **kwargs):
        """Look a label up in the main window's translations."""
        try:
            return self.window.t(key, fallback, **kwargs)
        except Exception:
            return fallback or key

    def _base_dir(self):
        return (getattr(self.window, "base_dir", None)
                or getattr(self.window, "BASE_DIR", None)
                or os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    def _default_dirs(self):
        base = self._base_dir()
        return [os.path.join(base, name) for name in REPORT_DIRS]

    # ------------------------------------------------------------------- UI
    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)

        header = QtWidgets.QHBoxLayout()
        self.title_label = QtWidgets.QLabel(self.t("reports", "Reports"))
        self.title_label.setStyleSheet(
            "font-size: 14px; font-weight: bold; color: #8ab4f8;")
        header.addWidget(self.title_label)
        self.count_label = QtWidgets.QLabel("")
        self.count_label.setStyleSheet("color: #9aa0a6;")
        header.addWidget(self.count_label)
        header.addStretch()
        self.btn_scan = QtWidgets.QPushButton(self.t("reports_scan", "Rescan"))
        self.btn_scan.clicked.connect(self.refresh)
        header.addWidget(self.btn_scan)
        self.btn_add_dir = QtWidgets.QPushButton(
            self.t("reports_add_dir", "Add folder..."))
        self.btn_add_dir.clicked.connect(self.add_directory)
        header.addWidget(self.btn_add_dir)
        layout.addLayout(header)

        # ---- grouping + quick filters ------------------------------------
        filter_row = QtWidgets.QHBoxLayout()
        filter_row.addWidget(QtWidgets.QLabel(self.t("reports_group", "Group:")))
        self.group_combo = QtWidgets.QComboBox()
        for key, label_key, fallback in GROUPINGS:
            self.group_combo.addItem(self.t(label_key, fallback), key)
        self.group_combo.currentIndexChanged.connect(self.refresh_view)
        filter_row.addWidget(self.group_combo)

        filter_row.addWidget(QtWidgets.QLabel(self.t("reports_quick", "Quick:")))
        self.quick_combo = QtWidgets.QComboBox()
        self.quick_combo.addItem(self.t("reports_filter_all", "All"), None)
        for key, label_key, fallback in QUICK_FILTERS:
            self.quick_combo.addItem(self.t(label_key, fallback), key)
        self.quick_combo.currentIndexChanged.connect(self.refresh_view)
        filter_row.addWidget(self.quick_combo)

        self.kind_combo = QtWidgets.QComboBox()
        self.kind_combo.addItem(self.t("reports_all_types", "All types"), "")
        for kind, _words in REPORT_TYPES:
            self.kind_combo.addItem(kind, kind)
        self.kind_combo.currentIndexChanged.connect(self.refresh_view)
        filter_row.addWidget(self.kind_combo)

        self.filter_edit = QtWidgets.QLineEdit()
        self.filter_edit.setPlaceholderText(
            self.t("reports_filter_text", "Filter reports..."))
        self.filter_edit.textChanged.connect(self.refresh_view)
        filter_row.addWidget(self.filter_edit, 1)
        layout.addLayout(filter_row)

        # ---- search row --------------------------------------------------
        # Seven searches: by name, by content, by tag, by type (the combo
        # above), by errors/warnings and in the generation history.
        search_row = QtWidgets.QHBoxLayout()
        search_row.addWidget(QtWidgets.QLabel(self.t("reports_search", "Search:")))
        self.name_edit = QtWidgets.QLineEdit()
        self.name_edit.setPlaceholderText(
            self.t("reports_search_name", "By name..."))
        self.name_edit.textChanged.connect(self.refresh_view)
        search_row.addWidget(self.name_edit, 1)

        self.content_edit = QtWidgets.QLineEdit()
        self.content_edit.setPlaceholderText(
            self.t("reports_search_content", "In content..."))
        self.content_edit.textChanged.connect(self.refresh_view)
        search_row.addWidget(self.content_edit, 2)

        self.tag_combo = QtWidgets.QComboBox()
        self.tag_combo.setEditable(True)
        self.tag_combo.addItem(self.t("reports_all_tags", "All tags"), "")
        self.tag_combo.currentIndexChanged.connect(self.refresh_view)
        self.tag_combo.editTextChanged.connect(self.refresh_view)
        search_row.addWidget(self.tag_combo)

        self.problems_combo = QtWidgets.QComboBox()
        self.problems_combo.addItem(self.t("reports_problems_any", "All"), None)
        self.problems_combo.addItem(
            self.t("reports_problems_errors", "Errors"), "error")
        self.problems_combo.addItem(
            self.t("reports_problems_warnings", "Warnings"), "warning")
        self.problems_combo.addItem(
            self.t("reports_problems_both", "Errors + warnings"), "any")
        self.problems_combo.currentIndexChanged.connect(self.refresh_view)
        search_row.addWidget(self.problems_combo)

        self.chk_history = QtWidgets.QCheckBox(
            self.t("reports_search_history", "Search history by day"))
        self.chk_history.stateChanged.connect(self.refresh_view)
        search_row.addWidget(self.chk_history)
        self.since_edit = QtWidgets.QLineEdit()
        self.since_edit.setPlaceholderText(
            self.t("reports_since", "Since (YYYY-MM-DD)"))
        self.since_edit.setMaximumWidth(140)
        self.since_edit.textChanged.connect(self.refresh_view)
        search_row.addWidget(self.since_edit)
        self.btn_clear_search = QtWidgets.QPushButton(
            self.t("reports_clear_search", "Clear search"))
        self.btn_clear_search.clicked.connect(self.clear_search)
        search_row.addWidget(self.btn_clear_search)
        layout.addLayout(search_row)

        # ---- split: list | preview ---------------------------------------
        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.tree = QtWidgets.QTreeWidget()
        self.tree.setColumnCount(5)
        self.tree.setHeaderLabels([
            self.t("reports_col_name", "Report"),
            self.t("reports_col_type", "Type"),
            self.t("reports_col_project", "Project"),
            self.t("reports_col_size", "Size"),
            self.t("reports_col_modified", "Modified"),
        ])
        self.tree.setAlternatingRowColors(True)
        self.tree.itemSelectionChanged.connect(self.on_select)
        self.tree.itemDoubleClicked.connect(lambda *_: self.open_report())
        splitter.addWidget(self.tree)

        content_side = QtWidgets.QWidget()
        content_layout = QtWidgets.QVBoxLayout(content_side)
        self.meta_label = QtWidgets.QLabel("")
        self.meta_label.setWordWrap(True)
        self.meta_label.setStyleSheet("color: #9aa0a6;")
        content_layout.addWidget(self.meta_label)

        # The preview menu: every report can be shown four offline ways.
        preview_row = QtWidgets.QHBoxLayout()
        preview_row.addWidget(
            QtWidgets.QLabel(self.t("reports_preview", "Preview:")))
        self.preview_kind = QtWidgets.QComboBox()
        for key, label_key, fallback in PREVIEW_KINDS:
            self.preview_kind.addItem(self.t(label_key, fallback), key)
        self.preview_kind.setCurrentIndex(self._preview_index("ascii"))
        self.preview_kind.currentIndexChanged.connect(self.refresh_preview)
        preview_row.addWidget(self.preview_kind)
        self.btn_preview = QtWidgets.QToolButton()
        self.btn_preview.setText(self.t("reports_preview_menu", "Preview..."))
        self.btn_preview.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.preview_menu = QtWidgets.QMenu(self.btn_preview)
        self.act_preview_run = self.preview_menu.addAction(
            self.t("reports_preview_run", "Refresh preview"))
        self.act_preview_ascii = self.preview_menu.addAction(
            self.t("reports_preview_ascii_only", "ASCII only"))
        self.act_preview_ascii.setCheckable(True)
        self.act_preview_ascii.setChecked(True)
        self.act_preview_export = self.preview_menu.addAction(
            self.t("reports_preview_export", "Export preview"))
        self.act_preview_run.triggered.connect(self.refresh_preview)
        self.act_preview_ascii.triggered.connect(
            lambda checked: self.chk_ascii_only.setChecked(checked))
        self.act_preview_export.triggered.connect(self.export_preview)
        self.btn_preview.setMenu(self.preview_menu)
        self.chk_ascii_only = QtWidgets.QCheckBox(
            self.t("reports_preview_ascii_only", "ASCII only"))
        self.chk_ascii_only.setChecked(True)
        self.chk_ascii_only.stateChanged.connect(self.refresh_preview)
        preview_row.addStretch()
        content_layout.addLayout(preview_row)

        self.content_view = QtWidgets.QPlainTextEdit()
        self.content_view.setReadOnly(True)
        self.content_view.setFont(QFont(REPORTS_FONT, REPORTS_FONT_SIZE))
        content_layout.addWidget(self.content_view, 1)

        # The mini-map bar (a vertical strip of section blocks), shown only
        # for the mini-map preview. It mirrors the report body alongside the
        # text so the shape of a long report is visible at a glance.
        self.minimap_view = QtWidgets.QPlainTextEdit()
        self.minimap_view.setReadOnly(True)
        self.minimap_view.setMaximumHeight(72)
        self.minimap_view.setFont(QFont(REPORTS_FONT, REPORTS_FONT_SIZE))
        self.minimap_view.setVisible(False)
        content_layout.addWidget(self.minimap_view)

        tools = QtWidgets.QHBoxLayout()
        self.btn_file_actions = QtWidgets.QToolButton()
        self.btn_file_actions.setText(self.t("reports_file_menu", "File..."))
        self.btn_file_actions.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.file_menu = QtWidgets.QMenu(self.btn_file_actions)
        self.btn_open_external = self.file_menu.addAction(
            self.t("reports_open_file", "Open file"))
        self.btn_send_docs = self.file_menu.addAction(
            self.t("reports_to_docs", "Store in DOCs"))
        self.btn_export = self.file_menu.addAction(
            self.t("reports_export", "Export index"))
        self.btn_delete = self.file_menu.addAction(
            self.t("reports_delete", "Delete report"))
        self.btn_open_external.triggered.connect(lambda: self.open_file())
        self.btn_send_docs.triggered.connect(lambda: self.send_to_docs())
        self.btn_export.triggered.connect(lambda: self.export_index())
        self.btn_delete.triggered.connect(lambda: self.delete_report())
        self.btn_file_actions.setMenu(self.file_menu)
        tools.addWidget(self.btn_preview)
        tools.addWidget(self.btn_file_actions)

        # ---- analysis / integration / operations --------------------------
        # Three dropdowns mirroring the three surfaces of reports_analysis:
        # what a report can be read *for*, where it can be *sent*, and what
        # can be done *to* it. They are built from the module's own tables, so
        # the menu and the function cannot drift apart.
        self.btn_analysis = QtWidgets.QToolButton()
        self.btn_analysis.setText(self.t("reports_analysis_menu", "Analysis..."))
        self.btn_analysis.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.analysis_menu = QtWidgets.QMenu(self.btn_analysis)
        self.analysis_actions = {}
        for key, label in self.analysis_toolbox():
            action = self.analysis_menu.addAction(label)
            action.triggered.connect(
                lambda _checked=False, k=key: self.run_analysis(k))
            self.analysis_actions[key] = action
        self.btn_analysis.setMenu(self.analysis_menu)
        tools.addWidget(self.btn_analysis)

        self.btn_open_in = QtWidgets.QToolButton()
        self.btn_open_in.setText(self.t("reports_open_in_menu", "Open in..."))
        self.btn_open_in.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.open_in_menu = QtWidgets.QMenu(self.btn_open_in)
        self.open_in_actions = {}
        for key, label in self.integration_targets():
            action = self.open_in_menu.addAction(label)
            action.triggered.connect(
                lambda _checked=False, k=key: self.open_in(k))
            self.open_in_actions[key] = action
        self.btn_open_in.setMenu(self.open_in_menu)
        tools.addWidget(self.btn_open_in)

        self.btn_generate = QtWidgets.QPushButton(
            self.t("reports_generate", "Generate from module"))
        self.btn_generate.clicked.connect(self.generate_from_module)
        tools.addWidget(self.btn_generate)

        self.btn_operations = QtWidgets.QToolButton()
        self.btn_operations.setText(
            self.t("reports_operations_menu", "Report actions..."))
        self.btn_operations.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.operations_menu = QtWidgets.QMenu(self.btn_operations)
        self.btn_important = self.operations_menu.addAction(
            self.t("reports_op_important", "Mark as important"))
        self.btn_favourite = self.operations_menu.addAction(
            self.t("reports_op_pin_favourite", "Pin to favourites"))
        self.btn_tag = self.operations_menu.addAction(
            self.t("reports_op_tag", "Add tags"))
        self.btn_compare = self.operations_menu.addAction(
            self.t("reports_op_compare", "Compare..."))
        self.btn_important.triggered.connect(self.mark_important)
        self.btn_favourite.triggered.connect(self.pin_to_favourite)
        self.btn_tag.triggered.connect(self.add_tags)
        self.btn_compare.triggered.connect(self.compare_with)
        self.btn_operations.setMenu(self.operations_menu)
        tools.addWidget(self.btn_operations)
        # ---- premium ---------------------------------------------------
        # The premium menu: automatic generation, the scheduler and the
        # three specialised reports, built from reports_premium's tables.
        self.btn_premium = QtWidgets.QToolButton()
        self.btn_premium.setText(self.t("reports_premium_menu",
                                        "Premium..."))
        self.btn_premium.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.premium_menu = QtWidgets.QMenu(self.btn_premium)
        self.premium_actions = {}
        for key, label in self.premium_features():
            action = self.premium_menu.addAction(label)
            action.triggered.connect(
                lambda _checked=False, k=key: self.run_premium(k))
            self.premium_actions[key] = action
        self.btn_premium.setMenu(self.premium_menu)
        tools.addWidget(self.btn_premium)
        self.btn_schedule = QtWidgets.QPushButton(
            self.t("reports_prem_run_due", "Run due reports"))
        self.btn_schedule.clicked.connect(self.run_due_reports)
        tools.addWidget(self.btn_schedule)
        self.btn_automate = QtWidgets.QPushButton(
            self.t("reports_prem_automate", "Toggle automation"))
        self.btn_automate.setCheckable(True)
        self.btn_automate.toggled.connect(self.toggle_automation)
        tools.addWidget(self.btn_automate)
        tools.addStretch()
        content_layout.addLayout(tools)
        splitter.addWidget(content_side)
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 5)
        layout.addWidget(splitter, 1)

        self.status = QtWidgets.QLabel("")
        self.status.setStyleSheet("color: #9aa0a6;")
        layout.addWidget(self.status)

    def _preview_index(self, kind):
        """The combo index for a preview key (0 when it is not offered)."""
        index = self.preview_kind.findData(kind)
        return index if index >= 0 else 0

    # ---------------------------------------------------------- repository
    def scan_directories(self):
        """The directories the repository currently watches."""
        if self.repo is None:
            return []
        return list(self.repo.directories)

    def refresh(self):
        """Rescan every watched directory and rebuild the report list."""
        dirs = self.scan_directories() or self._default_dirs()
        self.repo = ReportRepository(dirs)
        self._reports = self.repo.scan()
        self._reload_tags()
        self.refresh_view()
        self.status.setText(self.t(
            "reports_scanned", "scanned {count} report(s)").format(
            count=len(self._reports)))

    def add_directory(self):
        """Watch one more folder for report artifacts."""
        directory = QtWidgets.QFileDialog.getExistingDirectory(
            self, self.t("reports_add_dir", "Add folder..."), self._base_dir())
        if not directory:
            return ""
        if self.repo is None:
            self.repo = ReportRepository(self._default_dirs())
        self.repo.add_directory(directory)
        self.refresh()
        return directory

    def current_grouping(self):
        return self.group_combo.currentData() or "none"

    def visible_reports(self):
        """The reports matching every search the tab offers. The quick filter, the type, the free text, the name, the content, the tag and the errors/warnings are all applied here, together with the generation-history date bound when the history box is ticked."""
        since = None
        if getattr(self, "chk_history", None) is not None \
                and self.chk_history.isChecked():
            since = self.since_edit.text() or None
        tag = self.tag_combo.currentData() or self.tag_combo.currentText()
        if tag == self.t("reports_all_tags", "All tags"):
            tag = ""
        problems = self.problems_combo.currentData()
        return filter_reports(
            self._reports,
            quick=self.quick_combo.currentData(),
            kind=self.kind_combo.currentData() or "",
            text=self.filter_edit.text(),
            name=self.name_edit.text(),
            content=self.content_edit.text(),
            tags=[tag] if tag else None,
            problems=problems,
            since=since)

    def clear_search(self):
        """Reset every search field and refresh the list."""
        for widget in (getattr(self, "name_edit", None),
                       getattr(self, "content_edit", None),
                       getattr(self, "filter_edit", None),
                       getattr(self, "since_edit", None)):
            if widget is not None:
                widget.blockSignals(True)
                widget.clear()
                widget.blockSignals(False)
        if getattr(self, "chk_history", None) is not None:
            self.chk_history.blockSignals(True)
            self.chk_history.setChecked(False)
            self.chk_history.blockSignals(False)
        for combo in (getattr(self, "problems_combo", None),
                      getattr(self, "quick_combo", None),
                      getattr(self, "kind_combo", None),
                      getattr(self, "tag_combo", None)):
            if combo is not None:
                combo.blockSignals(True)
                combo.setCurrentIndex(0)
                combo.blockSignals(False)
        self.refresh_view()

    def _reload_tags(self):
        """Fill the tag picker with every tag the reports carry."""
        combo = getattr(self, "tag_combo", None)
        if combo is None:
            return
        current = combo.currentData()
        tags = set()
        for report in self._reports:
            tags.update(report.tags)
        combo.blockSignals(True)
        combo.clear()
        combo.addItem(self.t("reports_all_tags", "All tags"), "")
        for tag in sorted(tags):
            combo.addItem(tag, tag)
        index = combo.findData(current)
        if index >= 0:
            combo.setCurrentIndex(index)
        combo.blockSignals(False)

    def generation_history(self):
        """The generation timeline of the currently visible reports."""
        return generation_history(self.visible_reports())

    def search_history(self, needle="", kind="", since=None):
        """Search the generation history (name/content + type + date)."""
        return search_history(self._reports, needle=needle, kind=kind,
                              since=since)

    def _rebuild_directories(self, dirs):
        self.repo = ReportRepository(dirs)
        self._reports = self.repo.scan()
        self._reload_tags()

    # --------------------------------------------------------------- view
    def refresh_view(self):
        """Render the current grouping and filters into the tree."""
        tree = self.tree
        tree.blockSignals(True)
        tree.clear()
        reports = self.visible_reports()
        grouping = self.current_grouping()
        # The history box switches the view to the generation timeline, so
        # every group is the day a report was produced.
        if getattr(self, "chk_history", None) is not None \
                and self.chk_history.isChecked():
            grouping = "date"
        groups = group_reports(reports, grouping)
        for group, items in groups:
            if grouping == "none":
                parent = tree.invisibleRootItem()
            else:
                parent = QtWidgets.QTreeWidgetItem([str(group), "", ""])
                font = parent.font(0)
                font.setBold(True)
                parent.setFont(0, font)
                tree.addTopLevelItem(parent)
            for report in items:
                label = report.name
                problems = _problem_badge(report)
                if problems:
                    label = "%s  %s" % (label, problems)
                child = QtWidgets.QTreeWidgetItem([
                    label, report.kind, report.project or "",
                    _human_size(report.size), report.modified])
                child.setData(0, QtCore.Qt.UserRole, report)
                colour = REPORT_COLOURS.get(report.kind)
                if colour:
                    child.setForeground(1, QtGui.QColor(colour))
                if report.error_count:
                    child.setForeground(0, QtGui.QColor("#ff9a9a"))
                elif report.warning_count:
                    child.setForeground(0, QtGui.QColor("#f7c66d"))
                if parent is tree.invisibleRootItem():
                    tree.addTopLevelItem(child)
                else:
                    parent.addChild(child)
        if grouping != "none":
            tree.expandAll()
        for column in range(tree.columnCount()):
            tree.resizeColumnToContents(column)
        tree.blockSignals(False)
        self.count_label.setText(self.t(
            "reports_count", "{count} report(s)").format(count=len(reports)))

    def on_select(self):
        item = self.tree.currentItem()
        if item is None:
            return
        report = item.data(0, QtCore.Qt.UserRole)
        if report is not None:
            self.show_report(report)

    def show_report(self, report):
        """Load one report's metadata and content into the viewer."""
        self._current = report
        self.meta_label.setText(self.t(
            "reports_meta",
            "{type} | project: {project} | tags: {tags} | {size} | {modified}"
        ).format(type=report.kind, project=report.project or "-",
                 tags=", ".join(report.tags) or "-",
                 size=_human_size(report.size), modified=report.modified))
        self.refresh_preview()

    def current_report(self):
        return self._current

    def report_text(self, report, limit=200000):
        """The text of a report (capped so a huge file cannot hang the UI)."""
        return _read_report(report, limit=limit)

    # ------------------------------------------------------------- preview
    def current_preview_kind(self):
        """The preview view chosen in the combo."""
        combo = getattr(self, "preview_kind", None)
        return (combo.currentData() if combo is not None else "ascii") or "ascii"

    def _preview_labels(self):
        """The translation table for the metadata preview labels."""
        return {
            "reports_meta_name": self.t("reports_meta_name", "Name"),
            "reports_meta_type": self.t("reports_meta_type", "Type"),
            "reports_meta_project": self.t("reports_meta_project", "Project"),
            "reports_meta_module": self.t("reports_meta_module", "Module"),
            "reports_meta_script": self.t("reports_meta_script", "Script"),
            "reports_meta_process": self.t("reports_meta_process", "Process"),
            "reports_meta_source": self.t("reports_meta_source", "Source"),
            "reports_meta_path": self.t("reports_meta_path", "Path"),
            "reports_meta_generated": self.t("reports_meta_generated",
                                             "Generated"),
            "reports_meta_size": self.t("reports_meta_size", "Size"),
            "reports_meta_lines": self.t("reports_meta_lines", "Lines"),
            "reports_meta_words": self.t("reports_meta_words", "Words"),
            "reports_meta_unique": self.t("reports_meta_unique", "Unique words"),
            "reports_meta_chars": self.t("reports_meta_chars", "Characters"),
            "reports_meta_errors": self.t("reports_meta_errors", "Errors"),
            "reports_meta_warnings": self.t("reports_meta_warnings", "Warnings"),
            "reports_meta_tags": self.t("reports_meta_tags", "Tags"),
            "reports_meta_hash": self.t("reports_meta_hash", "Hash"),
        }

    def preview_text(self, report=None, kind=None, body=None):
        """Render the selected preview view for a report as plain text."""
        report = report or self._current
        if report is None:
            return ""
        kind = kind or self.current_preview_kind()
        ascii_only = (getattr(self, "chk_ascii_only", None) is not None
                      and self.chk_ascii_only.isChecked())
        return preview_report(report, kind=kind, body=body,
                              ascii_only=ascii_only,
                              labels=self._preview_labels())

    def minimap_text(self, report=None, body=None):
        """The mini-map bar for a report (one section block per symbol)."""
        report = report or self._current
        if report is None:
            return ""
        if body is None:
            body = self.report_text(report)
        return report_section_blocks(body)

    def refresh_preview(self):
        """Re-render the preview pane for the selected report."""
        if not hasattr(self, "content_view"):
            return
        report = self._current
        if report is None:
            self.content_view.setPlainText("")
            if hasattr(self, "minimap_view"):
                self.minimap_view.setPlainText("")
                self.minimap_view.setVisible(False)
            return
        kind = self.current_preview_kind()
        body = self.report_text(report)
        self.content_view.setPlainText(
            self.preview_text(report, kind=kind, body=body))
        if hasattr(self, "minimap_view"):
            show = kind == "minimap"
            self.minimap_view.setVisible(show)
            if show:
                self.minimap_view.setPlainText(self.minimap_text(report, body))

    def export_preview(self):
        """Write the current preview text to a file (offline)."""
        text = (self.content_view.toPlainText()
                if hasattr(self, "content_view") else "")
        if not text:
            self.status.setText(self.t("reports_preview_nothing_export",
                                       "nothing to export"))
            return ""
        name = self._current.name if self._current else "report"
        default = os.path.join(self._base_dir(), "%s.preview.txt" % name)
        path, _filter = QtWidgets.QFileDialog.getSaveFileName(
            self, self.t("reports_preview_export", "Export preview"), default,
            "Text (*.txt);;All files (*)")
        if not path:
            return ""
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(text)
        except OSError as exc:
            self.status.setText(str(exc))
            return ""
        self.status.setText(self.t("reports_preview_exported",
                                   "preview written: {path}").format(path=path))
        return path

    def open_report(self):
        """Show the selected report (double-click handler)."""
        report = self.current_report()
        if report is not None:
            self.show_report(report)
        return report

    def open_file(self):
        """Open the selected report with the OS default handler."""
        report = self.current_report()
        if report is None:
            return False
        return QtGui.QDesktopServices.openUrl(
            QtCore.QUrl.fromLocalFile(report.path))

    # ---------------------------------------------------------- cross-tab
    def send_to_docs(self):
        """Store the selected report in the DOCs repository as a document."""
        report = self.current_report()
        docs = getattr(self.window, "docs_tab", None)
        if report is None or docs is None or not hasattr(docs, "repo"):
            self.status.setText(self.t("reports_no_docs",
                                       "DOCs repository unavailable"))
            return None
        try:
            doc_id = docs.repo.add(
                report.name, self.report_text(report, limit=500000),
                "report", "Reports", ", ".join(report.tags),
                report.summary)
            indexer = getattr(docs, "index_document_into_engine", None)
            if callable(indexer):
                indexer(doc_id)
            docs.refresh()
        except Exception as exc:
            self.status.setText(str(exc))
            return None
        self.status.setText(self.t(
            "reports_stored_docs", "stored {name} in DOCs").format(
            name=report.name))
        return doc_id

    def export_index(self):
        """Write an index of the visible reports to a JSON file."""
        reports = self.visible_reports()
        base = self._base_dir()
        path = os.path.join(base, "reports_index.json")
        payload = {
            "generated": _fmt_stamp(_now()),
            "grouping": self.current_grouping(),
            "count": len(reports),
            "reports": [
                {"name": r.name, "type": r.kind, "project": r.project,
                 "module": r.module, "tags": r.tags, "size": r.size,
                 "modified": r.modified, "path": r.path,
                 "summary": r.summary}
                for r in reports
            ],
        }
        try:
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
        except OSError as exc:
            self.status.setText(str(exc))
            return ""
        self.status.setText(self.t(
            "reports_exported", "index written: {path}").format(path=path))
        return path

    def delete_report(self):
        """Delete the selected report file (with confirmation)."""
        report = self.current_report()
        if report is None:
            return False
        answer = QtWidgets.QMessageBox.question(
            self, self.t("reports_delete", "Delete report"),
            self.t("reports_delete_ask", "Delete {name}?").format(
                name=report.name),
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No)
        if answer != QtWidgets.QMessageBox.Yes:
            return False
        try:
            os.remove(report.path)
        except OSError as exc:
            self.status.setText(str(exc))
            return False
            self._current = None
            self.content_view.clear()
            self.refresh()
            self.status.setText(self.t("reports_deleted", "report deleted"))
            return True
    # -------------------------------------------------- report analysis
    def analysis_toolbox(self):
        """The seven analyses the tab can run (key, label), from the module. Read from `reports_analysis` rather than duplicated here, so the menu the tab shows and the functions a script calls cannot name different analyses."""
        from . import reports_analysis
        return [(key, self.t(label_key, fallback))
                for key, label_key, fallback, _r, _g
                in reports_analysis.ANALYSES]

    def run_analysis(self, key, series=None):
        """Run one report analysis and show it (metrics/trends/diff aware). A pair analysis (diff, changes) needs a second report; when one is not chosen the user is asked to pick it from the reports already scanned, which is the only place a "compare with..." can come from."""
        from . import reports_analysis
        reports = self.visible_reports()
        left = self.current_report()
        if left is None and reports:
            left = reports[0]
        right = None
        if key in ("diff", "changes"):
            right = self.pick_report(
                self.t("reports_pick_other", "Compare with which report?"))
            if right is None:
                self.status.setText(self.t("reports_need_second",
                                           "choose a second report"))
                return None
        outcome = reports_analysis.analyse(self.window, key, reports=reports,
                                           left=left, right=right,
                                           series=series)
        self._show_analysis(outcome)
        return outcome
    def _show_analysis(self, outcome):
        """Render an analysis result in the preview pane (offline)."""
        from . import reports_analysis as ra
        action = outcome.get("action", "")
        data = outcome.get("data", {}) or {}
        if not outcome.get("ok"):
            self.status.setText(str(outcome.get("detail", "")))
            return
        if action in ("errors", "warnings"):
            lines = ["line %5d  %s" % (row["line"], row["text"])
                     for row in data.get("lines", [])] or ["(none)"]
            text = "\n".join(lines)
        elif action == "duplicates":
            text = self._format_duplicates(data.get("groups", []))
        elif action == "diff":
            text = "\n".join(ra.render_diff(data))
        elif action == "changes":
            text = "\n".join(ra.render_diff(data))
        elif action == "trends":
            text = "\n".join(ra.render_trend(data,
                                             self.t("reports_an_trends",
                                                    "Trend analysis")))
        elif action == "metrics":
            text = "\n".join(ra.render_metrics(data))
        else:
            text = str(outcome.get("detail", ""))
        if hasattr(self, "content_view"):
            self.content_view.setPlainText(text)
            if hasattr(self, "minimap_view"):
                self.minimap_view.setVisible(False)
        self.status.setText(str(outcome.get("detail", "")))

    def _format_duplicates(self, groups):
        lines = []
        for entry in groups:
            lines.append("%d copies (%d bytes): %s"
                         % (len(entry["names"]), entry["size"],
                            entry["digest"][:12]))
            for name in entry["names"]:
                lines.append("    %s" % name)
        return "\n".join(lines) or "(no duplicates)"

    def pick_report(self, title=""):
        """Ask the user for one report out of the scanned list."""
        names = [r.name for r in self._reports]
        if not names:
            return None
        choice, accepted = QtWidgets.QInputDialog.getItem(
            self, title or self.t("reports_pick", "Choose a report"),
            self.t("reports_pick_label", "Report:"), names, 0, False)
        if not accepted:
            return None
        for report in self._reports:
            if report.name == choice:
                return report
        return None
    def analyse_errors(self):
        return self.run_analysis("errors")

    def analyse_warnings(self):
        return self.run_analysis("warnings")

    def analyse_duplicates(self):
        return self.run_analysis("duplicates")

    def analyse_diff(self):
        return self.run_analysis("diff")

    def analyse_trends(self, series="pass_fail"):
        return self.run_analysis("trends", series=series)

    def analyse_metrics(self):
        return self.run_analysis("metrics")

    # ------------------------------------------------- cross-module links
    def integration_targets(self):
        """The six modules a report can be sent to (key, label)."""
        from . import reports_analysis
        return [(key, self.t(label_key, fallback))
                for key, label_key, fallback, _r, _g
                in reports_analysis.INTEGRATIONS]

    def open_in(self, target, report=None, **kwargs):
        """Send a report to one other module (diff, search, debugger...)."""
        from . import reports_analysis
        report = report or self.current_report()
        outcome = reports_analysis.open_in(self.window, target, report,
                                           **kwargs)
        self.status.setText(str(outcome.get("detail", "")))
        self._announce_report("reports_int_" + str(target), outcome)
        return outcome
    def generate_report(self, source="editor", fmt="md"):
        """Ask one module to produce a report, in one click."""
        from . import reports_analysis
        outcome = reports_analysis.generate_report(self.window, source=source,
                                                   fmt=fmt)
        self.status.setText(str(outcome.get("detail", "")))
        if outcome.get("ok"):
            self.refresh()
        return outcome
    # ------------------------------------------------------- premium
    def premium_features(self):
        """The nine premium features as (key, label)."""
        from . import reports_premium
        return [(key, self.t(label_key, fallback))
                for key, label_key, fallback, _d
                in reports_premium.PREMIUM_FEATURES]

    def report_directory(self):
        """The directory generated reports are written into."""
        return os.path.join(self._base_dir(), GENERATED_DIR)

    def run_premium(self, key):
        """Run one premium feature and refresh the repository."""
        from . import reports_premium
        payload = {}
        if key == "comparative":
            payload["baseline"] = self._last_run_state()
            payload["current"] = self._current_run_state()
        elif key == "env_diff":
            payload["baseline"] = self._env_baseline()
        elif key == "security":
            payload["root"] = self._base_dir()
        outcome = reports_premium.run(key, directory=self.report_directory(),
                                      payload=payload)
        self.status.setText(str(outcome.get("detail", "")))
        self._announce_report("reports_prem_" + str(key), outcome)
        if outcome.get("ok"):
            self.refresh()
        return outcome

    def _current_run_state(self):
        """A small summary of the repository as the 'current' run state. The three numbers a comparative report wants: how many reports, the total error count and the total size. A snapshot of these is what "since the last run" compares against."""
        reports = self._reports
        return {"reports": len(reports),
                "errors": sum(r.error_count for r in reports),
                "warnings": sum(r.warning_count for r in reports),
                "size": sum(r.size for r in reports)}

    def _last_run_state(self):
        """The baseline the last comparative report used (or {})."""
        state = getattr(self, "_premium_state", None) or {}
        return state.get("baseline") or {}

    def _env_baseline(self):
        """The environment capture the env-diff compares against."""
        state = getattr(self, "_premium_state", None) or {}
        return state.get("env_baseline") or {}

    def run_due_reports(self, now=None):
        """Run every scheduler job that is due, then refresh."""
        from . import reports_premium
        state = self._premium_schedule()
        outcome = reports_premium.run_scheduled(
            self.report_directory(), state["jobs"], state["last"], now=now,
            env_baseline=self._env_baseline())
        self.status.setText(self.t(
            "reports_prem_due_done", "ran {count} scheduled report(s)"
        ).format(count=outcome["count"]))
        if outcome["count"]:
            self.refresh()
        return outcome

    def _premium_schedule(self):
        """The scheduler state: which jobs run, their cadence and last run. Kept on the tab (not on disk) on purpose: a schedule is a session's intent, and a persisted schedule that survives a restart is exactly the catch-up burst the cadence check exists to prevent."""
        from . import reports_premium
        state = getattr(self, "_premium_state", None)
        if state is None:
            state = {"jobs": {name: "daily"
                              for name in reports_premium.SCHEDULE_JOBS},
                     "last": {}, "baseline": {}, "env_baseline": {},
                     "automation": False}
            self._premium_state = state
        return state

    def set_cadence(self, job, cadence):
        """Set one job's cadence (hourly / daily / weekly)."""
        from . import reports_premium
        if cadence not in reports_premium.CADENCE_KEYS:
            return False
        self._premium_schedule()["jobs"][job] = cadence
        return True

    # ---- automatic generation --------------------------------------
    def toggle_automation(self, enabled):
        """Turn the automatic-generation watcher on or off."""
        self._premium_schedule()["automation"] = bool(enabled)
        if enabled:
            self._start_premium_timer()
        else:
            self._stop_premium_timer()
        self._announce_report("reports_prem_auto",
                              {"detail": "automation %s"
                               % ("on" if enabled else "off")})
        return bool(enabled)

    def _start_premium_timer(self):
        if getattr(self, "premium_timer", None) is None:
            timer = QtCore.QTimer(self)
            timer.setInterval(PREMIUM_POLL_MS)
            timer.timeout.connect(self._poll_premium)
            self.premium_timer = timer
        if not self.premium_timer.isActive():
            self.premium_timer.start()

    def _stop_premium_timer(self):
        timer = getattr(self, "premium_timer", None)
        if timer is not None and timer.isActive():
            timer.stop()

    def _poll_premium(self):
        """Timer callback: run due schedules and react to a file change."""
        state = self._premium_schedule()
        if not state.get("automation"):
            return
        self.run_due_reports()
        self.fire_trigger("after_change")

    def fire_trigger(self, trigger, payload=None):
        """Fire one automatic-generation trigger if it is enabled. A trigger the user has not enabled is ignored, which is what makes 'after_change' safe to fire on every poll: the cost of a disabled trigger is a dictionary lookup and nothing else."""
        from . import reports_premium
        if trigger not in reports_premium.TRIGGER_KEYS:
            return None
        state = self._premium_schedule()
        enabled = state.setdefault("triggers",
                                   dict(reports_premium.TRIGGER_DEFAULT))
        if not enabled.get(trigger):
            return None
        data = dict(payload or {})
        if trigger == "after_change":
            data.setdefault("current", self._current_run_state())
            data.setdefault("baseline", self._last_run_state())
        outcome = reports_premium.run(trigger,
                                      directory=self.report_directory(),
                                      payload=data)
        self._announce_report("reports_trig_" + str(trigger), outcome)
        if outcome.get("ok"):
            self.refresh()
        return outcome

    def set_trigger(self, trigger, enabled):
        """Enable or disable one automatic-generation trigger."""
        from . import reports_premium
        if trigger not in reports_premium.TRIGGER_KEYS:
            return False
        state = self._premium_schedule()
        state.setdefault("triggers",
                         dict(reports_premium.TRIGGER_DEFAULT))[trigger] = \
            bool(enabled)
        return True

    def generate_from_module(self):
        """Ask the user which module should generate a report, then do it."""
        sources = ("editor", "analyzer", "audit", "diff", "search", "docs",
                   "sandbox", "debugger")
        choice, accepted = QtWidgets.QInputDialog.getItem(
            self, self.t("reports_generate", "Generate from module"),
            self.t("reports_generate_label", "Module:"), list(sources), 0,
            False)
        if not accepted:
            return None
        return self.generate_report(source=choice)
    # ------------------------------------------------- report operations
    def report_operations(self):
        """The eight operations a report supports (key, label)."""
        from . import reports_analysis
        return [(key, self.t(label_key, fallback))
                for key, label_key, fallback, _a, _d
                in reports_analysis.OPERATIONS]

    def run_operation(self, key, report=None, **kwargs):
        """Run one report operation and refresh the view when it changed."""
        from . import reports_analysis
        report = report or self.current_report()
        outcome = reports_analysis.run_operation(self.window, key, report,
                                                 **kwargs)
        self.status.setText(str(outcome.get("detail", "")))
        self._announce_report("reports_op_" + str(key), outcome)
        if key in ("delete", "mark_important", "pin_project", "pin_favourite",
                   "tag"):
            self.refresh_view()
        return outcome
    def mark_important(self, important=None):
        """Toggle (or set) the important flag on the selected report."""
        from . import reports_analysis
        report = self.current_report()
        if report is None:
            return None
        if important is None:
            important = not reports_analysis.is_important(report)
        return self.run_operation("mark_important", important=important)

    def pin_to_project(self, project=""):
        """Pin the report to a project (asks when none is given)."""
        report = self.current_report()
        if report is None:
            return None
        if not project:
            project = str(report.project or "")
            project, accepted = QtWidgets.QInputDialog.getText(
                self, self.t("reports_op_pin_project", "Pin to project"),
                self.t("reports_project_name", "Project:"),
                QtWidgets.QLineEdit.Normal, project)
            if not accepted:
                return None
        return self.run_operation("pin_project", project=project.strip())

    def pin_to_favourite(self, favourite=None):
        """Add the report to (or remove it from) the favourites tab."""
        from . import reports_analysis
        report = self.current_report()
        if report is None:
            return None
        if favourite is None:
            favourite = not reports_analysis.is_favourite(report)
        return self.run_operation("pin_favourite", favourite=favourite)

    def add_tags(self, tags="", remove=""):
        """Add (and optionally remove) tags on the selected report."""
        report = self.current_report()
        if report is None:
            return None
        if not tags:
            tags, accepted = QtWidgets.QInputDialog.getText(
                self, self.t("reports_op_tag", "Add tags"),
                self.t("reports_tags_label", "Tags (comma separated):"))
            if not accepted:
                return None
        return self.run_operation("tag", add=tags, remove=remove)

    def compare_with(self, other=None):
        """Compare the selected report with another (opens the result)."""
        left = self.current_report()
        if left is None:
            return None
        other = other or self.pick_report(
            self.t("reports_op_compare", "Compare with another report"))
        if other is None:
            return None
        outcome = self.run_operation("compare", other=other)
        self._show_analysis(outcome)
        return outcome
    def show_versions(self, other=None):
        """Show what changed between two versions of the report."""
        left = self.current_report()
        if left is None:
            return None
        other = other or self.pick_report(
            self.t("reports_op_versions", "Show version differences"))
        if other is None:
            return None
        outcome = self.run_operation("versions", other=other)
        self._show_analysis(outcome)
        return outcome
    def _announce_report(self, event, outcome):
        """Announce a report action on the shared audit bus (best effort)."""
        notifier = getattr(self.window, "notify_audit", None)
        if callable(notifier):
            report = self.current_report()
            try:
                notifier(event, where=getattr(report, "name", ""),
                         detail=str(outcome.get("detail", "")), group="reports")
            except Exception:
                pass
    # ------------------------------------------------------- translation
    def retranslate(self, table):
        """Apply the main window's language table to this tab."""
        def label(key, fallback):
            return table.get(key, fallback)

        self.title_label.setText(label("reports", "Reports"))
        self.btn_scan.setText(label("reports_scan", "Rescan"))
        self.btn_add_dir.setText(label("reports_add_dir", "Add folder..."))
        self.btn_open_external.setText(label("reports_open_file", "Open file"))
        self.btn_send_docs.setText(label("reports_to_docs", "Store in DOCs"))
        self.btn_export.setText(label("reports_export", "Export index"))
        self.btn_delete.setText(label("reports_delete", "Delete report"))
        self.filter_edit.setPlaceholderText(
            label("reports_filter_text", "Filter reports..."))
        self.name_edit.setPlaceholderText(
            label("reports_search_name", "By name..."))
        self.content_edit.setPlaceholderText(
            label("reports_search_content", "In content..."))
        self.tag_combo.setItemText(0, label("reports_all_tags", "All tags"))
        self.problems_combo.setItemText(
            0, label("reports_problems_any", "All"))
        self.problems_combo.setItemText(
            1, label("reports_problems_errors", "Errors"))
        self.problems_combo.setItemText(
            2, label("reports_problems_warnings", "Warnings"))
        self.problems_combo.setItemText(
            3, label("reports_problems_both", "Errors + warnings"))
        self.chk_history.setText(
            label("reports_search_history", "Search history by day"))
        self.since_edit.setPlaceholderText(
            label("reports_since", "Since (YYYY-MM-DD)"))
        self.btn_clear_search.setText(
            label("reports_clear_search", "Clear search"))
        for index, (key, label_key, fallback) in enumerate(GROUPINGS):
            self.group_combo.setItemText(index, label(label_key, fallback))
        self.quick_combo.setItemText(0, label("reports_filter_all", "All"))
        for index, (key, label_key, fallback) in enumerate(QUICK_FILTERS, 1):
            self.quick_combo.setItemText(index, label(label_key, fallback))
        self.kind_combo.setItemText(0, label("reports_all_types", "All types"))
        self.btn_preview.setText(label("reports_preview_menu", "Preview..."))
        self.act_preview_run.setText(label(
            "reports_preview_run", "Refresh preview"))
        self.act_preview_ascii.setText(label(
            "reports_preview_ascii_only", "ASCII only"))
        self.chk_ascii_only.setText(label("reports_preview_ascii_only",
                                          "ASCII only"))
        self.act_preview_export.setText(label(
            "reports_preview_export", "Export preview"))
        for index, (key, label_key, fallback) in enumerate(PREVIEW_KINDS):
            self.preview_kind.setItemText(index, label(label_key, fallback))
        self.tree.setHeaderLabels([
            label("reports_col_name", "Report"),
            label("reports_col_type", "Type"),
            label("reports_col_project", "Project"),
            label("reports_col_size", "Size"),
            label("reports_col_modified", "Modified"),
        ])
        self._retranslate_tools(label)
        self.refresh_view()
        self.refresh_preview()

    def _retranslate_tools(self, label):
        """Relabel the analysis / integration / operations toolbar."""
        from . import reports_analysis
        self.btn_analysis.setText(label("reports_analysis_menu", "Analysis..."))
        for key, action in self.analysis_actions.items():
            fallback = reports_analysis.ANALYSIS_LABELS.get(key, action.text())
            action.setText(self.t("reports_an_" + key, fallback))
        self.btn_open_in.setText(label("reports_open_in_menu", "Open in..."))
        for key, action in self.open_in_actions.items():
            fallback = reports_analysis.INTEGRATION_LABELS.get(key, action.text())
            action.setText(self.t("reports_int_" + key, fallback))
        self.btn_generate.setText(label("reports_generate",
                                        "Generate from module"))
        self.btn_file_actions.setText(label("reports_file_menu", "File..."))
        self.btn_operations.setText(
            label("reports_operations_menu", "Report actions..."))
        self.btn_important.setText(label("reports_op_important",
                                         "Mark as important"))
        self.btn_favourite.setText(label("reports_op_pin_favourite",
                                         "Pin to favourites"))
        self.btn_tag.setText(label("reports_op_tag", "Add tags"))
        self.btn_compare.setText(label("reports_op_compare", "Compare..."))
        self.btn_premium.setText(label("reports_premium_menu", "Premium..."))
        self.btn_schedule.setText(label("reports_prem_run_due", "Run due reports"))
        self.btn_automate.setText(label("reports_prem_automate", "Toggle automation"))
        from . import reports_premium
        for key, action in getattr(self, "premium_actions", {}).items():
            fallback = reports_premium.PREMIUM_LABELS.get(key, action.text())
            action.setText(self.t("reports_prem_" + key, fallback))


# Report-type colours, shared across the tab.
REPORT_COLOURS = {
    "audit": "#9be9a8", "analysis": "#8ab4f8", "diff": "#c586c0",
    "test": "#4ec9b0", "error": "#ff9a9a", "search": "#f7c66d",
    "document": "#bfc7d5", "build": "#dcdcaa", "summary": "#9aa0a6",
    "lint": "#e5c07b", "deploy": "#61afef", "diagnostic": "#56b6c2",
    "performance": "#d19a66", "config": "#c678dd", "environment": "#98c379",
    "snapshot": "#abb2bf", "log": "#7f848e", "metrics": "#e06c75",
}


def _problem_badge(report):
    """A short "E:n W:n" badge for the reports that carry problems."""
    parts = []
    if report.error_count:
        parts.append("E:%d" % report.error_count)
    if report.warning_count:
        parts.append("W:%d" % report.warning_count)
    return " ".join(parts)


def _human_size(size):
    """A short human readable size (1.2 kB, 3.4 MB...)."""
    value = float(size or 0)
    for unit in ("B", "kB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return ("%d %s" % (value, unit)) if unit == "B" else \
                ("%.1f %s" % (value, unit))
        value /= 1024.0
    return "%.1f GB" % value
