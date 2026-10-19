# -*- coding: utf-8 -*-
"""Analysing reports, sending them to other modules, and acting on them.

`tabs/reports_tab.py` owns the report repository: it scans directories and
describes every artifact it finds. This module is the three surfaces built on
top of that, each written as plain functions so every one is reachable from the
tab, the API, the CLI and a test without a window existing:

    ANALYSES       what a report (or a pair of them) can be read *for*:
                   errors, warnings, duplicates, changes, a diff, trends and
                   metrics -- the "Report analysis" surface
    INTEGRATIONS   where a report can be *sent*: diff, search, debugger,
                   runner, linter and the project modules, plus one-click
                   report generation from any module
    OPERATIONS     what can be done *to* a report: open, delete, mark
                   important, pin to a project, pin to favourites, tag,
                   compare with another report, show its version differences

Every function returns plain data in one shape, and none of them raises: an
unreachable module comes back as ``{ok: False, detail: "..."}`` naming what was
missing, because "the Diff tab is not open" is a fact the caller needs, not an
exception they have to read. The bridges reach a target through the window
(``getattr(window, "x_tab")``), never by importing it, for the same reason the
Docs bridges do: a closed tab cannot break the Reports tab.

Two decisions worth stating:

    * **A duplicate is found by content, not by name.** Two reports with the
      same title but different bodies are not duplicates, and two reports with
      different names but the same body are -- so the comparison is over the
      text, and a hash is what makes it cheap.
    * **A change is a line diff, a trend is a series.** "What changed between
      two reports" is answered by the same hunk diff the Diff tab uses, while
      "how has this metric moved" is answered by ordering the reports by time
      and reading one number out of each; keeping the two apart is what stops a
      trend from being reported as a change.
"""

import datetime as dt
import difflib
import hashlib
import json
import os
import re

# ---------------------------------------------------------------------------
# The analyses
# ---------------------------------------------------------------------------
# (key, label key, fallback, what it reads, what it gives back)
ANALYSES = (
    ("errors", "reports_an_errors", "Error detection",
     "the report body",
     "every line that names an error, with its line number"),
    ("warnings", "reports_an_warnings", "Warning detection",
     "the report body",
     "every line that names a warning, with its line number"),
    ("duplicates", "reports_an_duplicates", "Duplicate detection",
     "two or more reports",
     "the reports that carry identical bodies, grouped together"),
    ("changes", "reports_an_changes", "Change detection",
     "two reports",
     "whether they differ, how much, and in what way"),
    ("diff", "reports_an_diff", "Report comparison (diff)",
     "two reports",
     "the hunks that turn one report into the other, side by side"),
    ("trends", "reports_an_trends", "Trend analysis",
     "a series of reports over time",
     "how a chosen series (tests passing, errors, size) moves across them"),
    ("metrics", "reports_an_metrics", "Metric analysis",
     "one or more reports",
     "execution time, error count and code/report size, per report"),
)

ANALYSIS_KEYS = tuple(key for key, _lk, _f, _r, _g in ANALYSES)
ANALYSIS_LABELS = {key: fallback for key, _lk, fallback, _r, _g in ANALYSES}
ANALYSIS_READS = {key: reads for key, _lk, _f, reads, _g in ANALYSES}
ANALYSIS_GIVES = {key: gives for key, _lk, _f, _r, gives in ANALYSES}

# The metrics the metric analysis reports for every artifact.
METRIC_KEYS = ("execution_time", "errors", "warnings", "size", "lines",
               "characters")
METRIC_LABELS = {
    "execution_time": "Execution time",
    "errors": "Errors",
    "warnings": "Warnings",
    "size": "Size (bytes)",
    "lines": "Lines",
    "characters": "Characters",
}

# The trend series the trend analysis understands. Each is read from every
# report in the series and ordered by time; a series that a report does not
# carry is simply absent from that report's row, never invented.
TREND_SERIES = ("pass_fail", "tests", "errors", "warnings", "size", "runtime")
TREND_LABELS = {
    "pass_fail": "Tests passing / failing",
    "tests": "Test count",
    "errors": "Errors",
    "warnings": "Warnings",
    "size": "Report size",
    "runtime": "Execution time",
}

# Words that mark an error / warning line, reusing the tab's own vocabulary so
# the two never disagree about what counts as a problem.
from .reports_tab import ERROR_WORDS, WARNING_WORDS

# Words that mark a "pass" and a "fail" so the pass/fail series can be read out
# of test reports.
PASS_WORDS = ("pass", "passed", "ok", "success", "succeeded", "green")
FAIL_WORDS = ("fail", "failed", "failure", "error", "red", "broken")

# The metadata keys the operations store (bookkeeping, so a leading underscore).
IMPORTANT_KEY = "_important"
PROJECTS_KEY = "_projects"
FAVOURITE_KEY = "_favourite"

# The key a report's own metadata mapping lives under on disk (a sidecar), so
# marking, pinning and tagging survive a scan without a database.
SIDECAR_NAME = "reports_meta.json"


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------
def result(action, ok, detail="", report=None, data=None):
    """One outcome, in the shape every function here returns."""
    return {
        "ok": bool(ok),
        "action": action,
        "report": report,
        "detail": detail,
        "data": dict(data or {}),
    }


def _read(report, limit=200000):
    """A report's text (capped so a huge file cannot hang the caller)."""
    if report is None:
        return ""
    path = getattr(report, "path", report if isinstance(report, str) else "")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read(limit)
    except OSError:
        return ""


def _lines(text):
    return (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _digest(text):
    return hashlib.sha256((text or "").encode("utf-8", "replace")).hexdigest()


# ---------------------------------------------------------------------------
# Error and warning detection
# ---------------------------------------------------------------------------
def scan_lines(text, words):
    """Every line that contains one of `words`, in order.

    Returns a list of ``{line, text}`` (1-based line numbers), which is what
    both the error and the warning scan hand back: the same shape for both means
    one renderer shows either.
    """
    lowered = [word.lower() for word in words]
    found = []
    for number, line in enumerate(_lines(text), 1):
        haystack = line.lower()
        if any(word in haystack for word in lowered):
            found.append({"line": number, "text": line.strip()[:200]})
    return found


def find_errors(report, body=None):
    """Every error line in a report (with its line number)."""
    text = _read(report) if body is None else body
    return scan_lines(text, ERROR_WORDS)


def find_warnings(report, body=None):
    """Every warning line in a report (with its line number)."""
    text = _read(report) if body is None else body
    return scan_lines(text, WARNING_WORDS)


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------
def find_duplicates(reports, bodies=None):
    """Group reports whose bodies are identical.

    `bodies` maps a report path to its text when the caller already read them;
    otherwise each is read here. The comparison is over the body, so two
    reports with the same name and different contents are not duplicates and
    two with different names and the same contents are. Returns
    ``[{digest, size, names, paths}]``, largest group first.
    """
    bodies = bodies or {}
    groups = {}
    for report in reports or ():
        path = getattr(report, "path", "")
        text = bodies.get(path)
        if text is None:
            text = _read(report)
        digest = _digest(text)
        entry = groups.setdefault(digest, {"digest": digest, "size": len(text),
                                           "names": [], "paths": []})
        entry["names"].append(getattr(report, "name", path))
        entry["paths"].append(path)
    duplicates = [group for group in groups.values() if len(group["names"]) > 1]
    duplicates.sort(key=lambda group: (len(group["names"]), group["size"]),
                    reverse=True)
    return duplicates


# ---------------------------------------------------------------------------
# Change detection and report comparison (diff)
# ---------------------------------------------------------------------------
def diff_reports(left, right, left_body=None, right_body=None, context=3):
    """A textual diff of two reports: ``{changed, added, removed, hunks}``.

    `hunks` is a unified-diff style list, so the Diff tab can render it as it
    renders any other diff and a caller can count the changes without parsing
    text. The diff is over the bodies, never the file names: what changed
    *between two reports* is a statement about their content.
    """
    left_text = _read(left) if left_body is None else left_body
    right_text = _read(right) if right_body is None else right_body
    left_lines = _lines(left_text)
    right_lines = _lines(right_text)
    hunks = []
    added = removed = 0
    matcher = difflib.SequenceMatcher(a=left_lines, b=right_lines)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        block = []
        # A little context above and below, capped so a one-line change does
        # not drag a hundred lines of the report into the view.
        start = max(0, i1 - context)
        for line in left_lines[start:i1]:
            block.append((" ", line))
        for line in left_lines[i1:i2]:
            block.append(("-", line))
            removed += 1
        for line in right_lines[j1:j2]:
            block.append(("+", line))
            added += 1
        end = min(len(right_lines), j2 + context)
        for line in right_lines[j2:end]:
            block.append((" ", line))
        hunks.append({"kind": tag, "left_start": i1 + 1, "left_end": i2,
                      "right_start": j1 + 1, "right_end": j2, "lines": block})
    return {"changed": added + removed, "added": added, "removed": removed,
            "hunks": hunks}


def detect_changes(left, right, **kwargs):
    """Whether two reports differ, and how much (a thin view of the diff)."""
    comparison = diff_reports(left, right, **kwargs)
    left_name = getattr(left, "name", "")
    right_name = getattr(right, "name", "")
    if comparison["changed"] == 0:
        return {"changed": False, "added": 0, "removed": 0,
                "detail": "%s and %s are identical" % (left_name, right_name),
                "hunks": []}
    return {"changed": True, "added": comparison["added"],
            "removed": comparison["removed"],
            "detail": "%s -> %s: +%d / -%d line(s)"
                      % (left_name, right_name, comparison["added"],
                         comparison["removed"]),
            "hunks": comparison["hunks"]}


def render_diff(comparison, labels=None):
    """A diff as unified text lines (the same shape the CLI prints)."""
    lines = []
    width = 78
    for hunk in comparison.get("hunks", []):
        lines.append("@@ -%d,%d +%d,%d @@"
                     % (hunk["left_start"], hunk["left_end"] - hunk["left_start"],
                        hunk["right_start"], hunk["right_end"] - hunk["right_start"]))
        for marker, text in hunk["lines"]:
            lines.append(("%s %s" % (marker, text))[:width])
    return lines


# ---------------------------------------------------------------------------
# Trend analysis
# ---------------------------------------------------------------------------
def _number_after(text, names):
    """The first number that follows any of `names` in the text (or None)."""
    for name in names:
        match = re.search(r"%s[^0-9\-]{0,12}(-?\d+(?:\.\d+)?)" % re.escape(name),
                          text, re.IGNORECASE)
        if match:
            try:
                return float(match.group(1))
            except ValueError:
                continue
    return None


def _series_value(report, series):
    """One report's value for one trend series (or None when it has none)."""
    if series == "errors":
        return getattr(report, "error_count", 0)
    if series == "warnings":
        return getattr(report, "warning_count", 0)
    if series == "size":
        return getattr(report, "size", 0)
    body = _read(report, limit=65536)
    if series == "pass_fail":
        lowered = body.lower()
        passing = sum(1 for word in PASS_WORDS if word in lowered)
        failing = sum(1 for word in FAIL_WORDS if word in lowered)
        if not passing and not failing:
            return None
        # +1 when the report reads as a pass, -1 when it reads as a fail, and
        # 0 when the two signals are balanced -- a state the caller can see.
        return (passing > failing) - (failing > passing)
    if series == "tests":
        return _number_after(body, ("test", "tests", "total", "case", "cases"))
    if series == "runtime":
        return _number_after(body, ("time", "took", "elapsed", "duration",
                                    "seconds", "runtime"))
    return None


def analyse_trends(reports, series="pass_fail", oldest_first=True):
    """How one series moves across a set of reports over time.

    `reports` is any iterable of :class:`Report`; they are ordered by time
    (`oldest_first` decides the direction) and one value read out of each. The
    result carries the ordered points, the direction (up / down / flat) and the
    first and last values, so a caller can show a sentence and a caller can
    show a spark line, from the same data.
    """
    series = str(series or "pass_fail").strip().lower()
    if series not in TREND_SERIES:
        return {"series": series, "points": [], "direction": "flat",
                "first": None, "last": None, "error": "unknown series"}
    ordered = sorted(reports or (), key=lambda r: getattr(r, "modified_ts", 0),
                     reverse=not oldest_first)
    points = []
    for report in ordered:
        value = _series_value(report, series)
        if value is None:
            continue
        points.append({"name": getattr(report, "name", ""),
                       "modified": getattr(report, "modified", ""),
                       "value": value})
    if len(points) < 2:
        return {"series": series, "points": points, "direction": "flat",
                "first": points[0]["value"] if points else None,
                "last": points[-1]["value"] if points else None, "error": ""}
    first, last = points[0]["value"], points[-1]["value"]
    direction = "flat"
    if last > first:
        direction = "up"
    elif last < first:
        direction = "down"
    return {"series": series, "points": points, "direction": direction,
            "first": first, "last": last, "delta": last - first, "error": ""}


def render_trend(trend, label=""):
    """A trend as ASCII lines: a header, a sentence and a spark line."""
    series = trend.get("series", "pass_fail")
    lines = ["%s: %s" % (label or "Trend", TREND_LABELS.get(series, series))]
    if not trend.get("points"):
        lines.append("(no points)")
        return lines
    lines.append("%s -> %s (%s)" % (trend.get("first"), trend.get("last"),
                                    trend.get("direction", "flat")))
    lines.append(_spark([point["value"] for point in trend["points"]]))
    return lines


_SPARK = " .:-=+*#%@"


def _spark(values):
    """A short ASCII spark line for a series of numbers."""
    if not values:
        return ""
    low = min(values)
    high = max(values)
    span = high - low
    if span == 0:
        return _SPARK[-1] * len(values)
    cells = []
    for value in values:
        level = int(round((value - low) / span * (len(_SPARK) - 1)))
        cells.append(_SPARK[max(0, min(len(_SPARK) - 1, level))])
    return "".join(cells)


# ---------------------------------------------------------------------------
# Metric analysis
# ---------------------------------------------------------------------------
def report_metrics(report, body=None):
    """The headline metrics for one report (all offline).

    Execution time, errors, warnings, byte size, line count and character
    count. The execution time is read from the body when the report recorded
    one and is None when it did not -- never guessed.
    """
    text = _read(report, limit=200000) if body is None else body
    body = text or ""
    return {
        "name": getattr(report, "name", ""),
        "kind": getattr(report, "kind", ""),
        "execution_time": _number_after(body, ("time", "took", "elapsed",
                                               "duration", "seconds", "ms")),
        "errors": getattr(report, "error_count", 0),
        "warnings": getattr(report, "warning_count", 0),
        "size": getattr(report, "size", len(body)),
        "lines": len(_lines(body)),
        "characters": len(body),
    }


def analyse_metrics(reports, bodies=None):
    """The metrics for several reports, plus a per-metric summary.

    Returns ``{rows, summary}``: one row per report and, for every metric, the
    total, the average and the extremes. A report without an execution time is
    left out of that metric's summary rather than counted as zero.
    """
    bodies = bodies or {}
    rows = []
    for report in reports or ():
        path = getattr(report, "path", "")
        rows.append(report_metrics(report, bodies.get(path)))
    summary = {}
    for key in METRIC_KEYS:
        values = [row[key] for row in rows if row.get(key) is not None]
        if not values:
            summary[key] = {"count": 0, "total": None, "average": None,
                            "min": None, "max": None}
            continue
        summary[key] = {"count": len(values), "total": sum(values),
                        "average": sum(values) / len(values),
                        "min": min(values), "max": max(values)}
    return {"rows": rows, "summary": summary}


def render_metrics(analysis, labels=None):
    """A metric analysis as aligned ASCII lines."""
    rows = analysis.get("rows", [])
    if not rows:
        return ["(no reports)"]
    header = "%-28s %10s %7s %7s %9s" % ("report", "err", "warn", "lines",
                                         "bytes")
    lines = [header, "-" * len(header)]
    for row in rows:
        lines.append("%-28s %10s %7s %7s %9s" % (
            str(row["name"])[:28], row["errors"], row["warnings"],
            row["lines"], row["size"]))
    summary = analysis.get("summary", {})
    if summary:
        lines.append("")
        lines.append("Totals")
        lines.append("------")
        for key in ("errors", "warnings", "size"):
            entry = summary.get(key, {})
            if entry.get("count"):
                lines.append("%-14s total %-10s average %.1f"
                             % (key, entry["total"], entry["average"]))
    return lines


# ---------------------------------------------------------------------------
# The single analysis dispatcher
# ---------------------------------------------------------------------------
def analyse(window, key, reports=None, left=None, right=None, series=None):
    """Run one named analysis: ``{ok, action, detail, data}``.

    `key` is one of :data:`ANALYSIS_KEYS`. The dispatcher is deliberately thin:
    each analysis is reached by name so a menu, a CLI and a test all drive the
    same code, and anything the analysis needs beyond a report is passed in
    (`left`/`right` for the pair analyses, `series` for the trend).
    """
    key = str(key or "").strip().lower()
    if key not in ANALYSIS_KEYS:
        return result(key, False, "unknown analysis %r (known: %s)"
                      % (key, ", ".join(ANALYSIS_KEYS)))
    reports = list(reports or [])
    try:
        if key == "errors":
            target = left or (reports[0] if reports else None)
            if target is None:
                return result(key, False, "no report selected")
            found = find_errors(target)
            return result(key, True, "%d error line(s)" % len(found),
                          data={"lines": found})
        if key == "warnings":
            target = left or (reports[0] if reports else None)
            if target is None:
                return result(key, False, "no report selected")
            found = find_warnings(target)
            return result(key, True, "%d warning line(s)" % len(found),
                          data={"lines": found})
        if key == "duplicates":
            groups = find_duplicates(reports)
            return result(key, True, "%d duplicate group(s)" % len(groups),
                          data={"groups": groups})
        if key == "changes":
            if left is None or right is None:
                return result(key, False, "two reports are needed")
            return result(key, True, detect_changes(left, right)["detail"],
                          data=detect_changes(left, right))
        if key == "diff":
            if left is None or right is None:
                return result(key, False, "two reports are needed")
            comparison = diff_reports(left, right)
            return result(key, True,
                          "%d changed line(s)" % comparison["changed"],
                          data=comparison)
        if key == "trends":
            trend = analyse_trends(reports, series or "pass_fail")
            return result(key, True, trend.get("direction", "flat"),
                          data=trend)
        if key == "metrics":
            analysis = analyse_metrics(reports)
            return result(key, True, "%d report(s)" % len(analysis["rows"]),
                          data=analysis)
    except Exception as exc:
        return result(key, False, "analysis failed: %s" % exc)
    return result(key, False, "analysis not implemented")


# ---------------------------------------------------------------------------
# Cross-module integration
# ---------------------------------------------------------------------------
# (key, label key, fallback, what the target receives, what it gives back)
INTEGRATIONS = (
    ("diff", "reports_int_diff", "Open in Diff",
     "the report as the right-hand source, and another report chosen by the "
     "user as the left when one is given",
     "a side-by-side comparison"),
    ("search", "reports_int_search", "Open in Search",
     "the report's first meaningful line as the query",
     "every script and document that contains it"),
    ("debugger", "reports_int_debugger", "Open in Debugger",
     "the report body as the script to inspect",
     "a session over it, with the report's lines as frames"),
    ("runner", "reports_int_runner", "Open in Runner",
     "the report body as a script the runner may execute",
     "a run, with its output"),
    ("linter", "reports_int_linter", "Open in Linter",
     "the report body as the code the linter should check",
     "the linter's findings for the text"),
    ("project", "reports_int_project", "Link to project module",
     "the report's project and module, as a link the project tree can show",
     "the report attached to the module it came from"),
)

INTEGRATION_KEYS = tuple(key for key, _lk, _f, _r, _g in INTEGRATIONS)
INTEGRATION_LABELS = {key: fallback for key, _lk, fallback, _r, _g
                      in INTEGRATIONS}

# The window attribute each target lives under, in the order they are tried.
# The runner and the linter are reached through the editor, which is the tab
# that owns a live buffer to run or lint; the project link is metadata, not a
# tab, so it has no attribute here.
TARGET_ATTRS = {
    "diff": ("diff_tab", "compare_tab"),
    "search": ("search_tab",),
    "debugger": ("debugger_tab", "debug_tab"),
    "runner": ("editor_tab", "runner_tab", "run_tab"),
    "linter": ("analyzer_tab", "script_analyzer_tab"),
}


def target_tab(window, target):
    """The tab a target lives under, or None when it is not open."""
    if window is None:
        return None
    for name in TARGET_ATTRS.get(target, ()):
        tab = getattr(window, name, None)
        if tab is not None:
            return tab
    return None


def _focus(window, tab):
    try:
        tabs = getattr(window, "tabs", None)
        if tabs is not None and tab is not None:
            tabs.setCurrentWidget(tab)
        return True
    except Exception:
        return False


def _as_entry(report, name=""):
    """A report as the (name, code, category) entry the other tabs take."""
    return {
        "id": None,
        "name": name or getattr(report, "name", "") or "report",
        "category": _category_for(report),
        "tags": ", ".join(getattr(report, "tags", []) or []),
        "desc": getattr(report, "summary", "") or "",
        "code": _read(report, limit=100000),
        "date": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _category_for(report):
    """The category a report edits best as (its text type, else Text)."""
    name = (getattr(report, "name", "") or "").lower()
    if name.endswith((".py", ".pyw")):
        return "Python"
    if name.endswith((".json", ".yaml", ".yml", ".ini", ".cfg", ".conf")):
        return "JSON"
    if name.endswith((".patch", ".diff")):
        return "Diff"
    if name.endswith((".log", ".out")):
        return "Log"
    return "Text"


def open_in_diff(window, report, previous=None):
    """Open a report in the Diff tab, against another report when given."""
    tab = target_tab(window, "diff")
    if tab is None:
        return result("diff", False, "the Diff tab is not open", report)
    try:
        converter = getattr(tab, "_entry_to_source", None)
        if not callable(converter):
            return result("diff", False,
                          "the Diff tab has no source converter", report)
        right = converter(_as_entry(report))
        if previous is not None:
            left = converter(_as_entry(previous))
            tab.set_sources(left, right)
        else:
            tab._right = right
            if hasattr(tab, "recompute"):
                tab.recompute()
        _focus(window, tab)
        return result("diff", True, "opened %r in the Diff tab"
                      % getattr(report, "name", ""), report,
                      {"compared_with_previous": previous is not None})
    except Exception as exc:
        return result("diff", False, "diff failed: %s" % exc, report)


def open_in_search(window, report):
    """Search for a report's own distinctive line."""
    tab = target_tab(window, "search")
    query = query_for(report)
    if tab is None:
        return result("search", False, "the Search tab is not open", report,
                      {"query": query})
    try:
        entry = getattr(tab, "query_edit", None)
        if entry is None:
            return result("search", False,
                          "the Search tab has no query box", report,
                          {"query": query})
        entry.setText(query)
        _focus(window, tab)
        return result("search", True, "searching for %r" % query, report,
                      {"query": query})
    except Exception as exc:
        return result("search", False, "search failed: %s" % exc, report,
                      {"query": query})


def open_in_debugger(window, report):
    """Open a report in the Debugger as the script to inspect."""
    tab = target_tab(window, "debugger")
    entry = _as_entry(report)
    if tab is None:
        opener = getattr(window, "open_in_debugger", None)
        if callable(opener):
            try:
                opener(entry)
                return result("debugger", True,
                              "opened %r in the debugger"
                              % getattr(report, "name", ""), report)
            except Exception as exc:
                return result("debugger", False,
                              "debugger failed: %s" % exc, report)
        return result("debugger", False, "the Debugger tab is not open", report)
    try:
        loader = getattr(tab, "load_script", None)
        if callable(loader):
            loader(entry)
            _focus(window, tab)
            return result("debugger", True,
                          "opened %r in the debugger"
                          % getattr(report, "name", ""), report)
        return result("debugger", False,
                      "the debugger tab takes no script from here", report)
    except Exception as exc:
        return result("debugger", False, "debugger failed: %s" % exc, report)


def open_in_runner(window, report, run=False):
    """Put a report in the runner (the editor's), optionally running it."""
    tab = target_tab(window, "runner")
    entry = _as_entry(report)
    if tab is None or not hasattr(tab, "run_integration"):
        return result("runner", False, "the editor (runner) is not open",
                      report)
    try:
        # The editor owns the runner; handing it the report is what "open in
        # the runner" means, and its own run_integration decides what to run.
        loader = getattr(tab, "load_script", None)
        if callable(loader):
            loader(entry)
        tab.run_integration("runner")
        _focus(window, tab)
        return result("runner", True,
                      "put %r in the runner" % getattr(report, "name", ""),
                      report, {"ran": bool(run)})
    except Exception as exc:
        return result("runner", False, "runner failed: %s" % exc, report)


def open_in_linter(window, report):
    """Hand a report's text to the linter (the script analyser)."""
    tab = target_tab(window, "linter")
    entry = _as_entry(report)
    if tab is None:
        return result("linter", False,
                      "the Script analyser (linter) is not open", report)
    try:
        loader = getattr(tab, "load_script", None)
        if callable(loader):
            loader(entry)
            _focus(window, tab)
            return result("linter", True,
                          "linting %r" % getattr(report, "name", ""), report)
        analyse = getattr(tab, "_analyze_script", None)
        if callable(analyse):
            findings = analyse(entry)
            _focus(window, tab)
            return result("linter", True,
                          "linted %r" % getattr(report, "name", ""), report,
                          {"findings": findings})
        return result("linter", False,
                      "the analyser tab takes no script from here", report)
    except Exception as exc:
        return result("linter", False, "linter failed: %s" % exc, report)


def link_to_project(window, report, project=None, module=None):
    """Attach a report to the project / module it came from.

    The link is metadata, stored through the same sidecar the operations use,
    so the reports repository keeps listing the file and a project view can
    find the report by project without a database.
    """
    project = (project if project is not None
               else getattr(report, "project", "") or "")
    module = (module if module is not None
              else getattr(report, "module", "") or "")
    if not project and not module:
        return result("project", False, "the report names no project", report)
    outcome = set_meta(report, {PROJECTS_KEY: [p for p in (project, module)
                                               if p]}, window)
    if not outcome["ok"]:
        return result("project", False, outcome["detail"], report)
    return result("project", True,
                  "linked to %s" % (project or module), report,
                  {"project": project, "module": module})


_BRIDGES = {
    "diff": open_in_diff,
    "search": open_in_search,
    "debugger": open_in_debugger,
    "runner": open_in_runner,
    "linter": open_in_linter,
    "project": link_to_project,
}


def open_in(window, target, report, **kwargs):
    """Open a report in one named module (see :data:`INTEGRATIONS`)."""
    target = str(target or "").strip().lower()
    bridge = _BRIDGES.get(target)
    if bridge is None:
        return result(target, False, "unknown target %r (known: %s)"
                      % (target, ", ".join(INTEGRATION_KEYS)), report)
    try:
        return bridge(window, report, **kwargs)
    except Exception as exc:
        return result(target, False, "bridge failed: %s" % exc, report)


def open_in_all(window, report):
    """Try every target at once: ``{target: result}`` (honest by shape)."""
    return {target: open_in(window, target, report)
            for target in INTEGRATION_KEYS}


def query_for(report):
    """A search query that identifies a report: its first meaningful line."""
    body = _read(report, limit=4096)
    for line in _lines(body):
        text = line.strip().strip("#=*- ").strip()
        if len(text) >= 8:
            return text[:60]
    return (getattr(report, "name", "") or "report")[:60]


def generate_report(window, source="editor", fmt="md", **kwargs):
    """Ask one module to produce a report in one click.

    `source` names the producing module (`editor`, `analyzer`, `audit`,
    `diff`, `search`, `docs`, `sandbox`, `debugger`); the module is reached
    through the window and its own "generate" entry point is called, so the
    Reports tab never re-implements a report a module already knows how to
    write. Returns ``{ok, action, detail, data}``; a module that is not open,
    or that has no generator, reports which one it was.
    """
    source = str(source or "").strip().lower()
    attr = {"editor": "editor_tab", "analyzer": "analyzer_tab",
            "audit": "audit_tab", "diff": "diff_tab", "search": "search_tab",
            "docs": "docs_tab", "sandbox": "sandbox_tab",
            "debugger": "debugger_tab"}.get(source)
    if attr is None:
        return result("generate", False, "unknown module %r" % source)
    tab = getattr(window, attr, None)
    if tab is None:
        return result("generate", False, "the %s module is not open" % source)
    for name in ("generate_report", "export_report", "save_report",
                 "write_report"):
        maker = getattr(tab, name, None)
        if callable(maker):
            try:
                path = maker(fmt=fmt, **kwargs)
            except TypeError:
                try:
                    path = maker()
                except Exception as exc:
                    return result("generate", False,
                                  "%s failed: %s" % (source, exc))
            except Exception as exc:
                return result("generate", False,
                              "%s failed: %s" % (source, exc))
            return result("generate", True,
                          "report generated by %s" % source,
                          data={"path": path, "source": source})
    return result("generate", False,
                  "the %s module has no report generator" % source)


# ---------------------------------------------------------------------------
# The operations
# ---------------------------------------------------------------------------
# (key, label key, fallback, arguments, what it does)
OPERATIONS = (
    ("open", "reports_op_open", "Open",
     (), "read the report and make it the current one"),
    ("delete", "reports_op_delete", "Delete",
     ("confirm",), "remove the report file from disk"),
    ("mark_important", "reports_op_important", "Mark as important",
     ("important",), "flag the report as important, or clear the flag"),
    ("pin_project", "reports_op_pin_project", "Pin to project",
     ("project",), "attach the report to a project, or detach it"),
    ("pin_favourite", "reports_op_pin_favourite", "Pin to favourites",
     ("favourite",), "add the report to the favourites tab, or remove it"),
    ("tag", "reports_op_tag", "Add tags",
     ("add", "remove"), "add and remove tags, keeping the rest"),
    ("compare", "reports_op_compare", "Compare with another report",
     ("other",), "put two reports side by side as a diff"),
    ("versions", "reports_op_versions", "Show version differences",
     ("other",), "show what changed between two versions of the report"),
)

OPERATION_KEYS = tuple(key for key, _lk, _f, _a, _d in OPERATIONS)
OPERATION_LABELS = {key: fallback for key, _lk, fallback, _a, _d in OPERATIONS}
OPERATION_ARGUMENTS = {key: args for key, _lk, _f, args, _d in OPERATIONS}
OPERATION_MEANINGS = {key: meaning for key, _lk, _f, _a, meaning in OPERATIONS}


def _sidecar_path(report):
    directory = os.path.dirname(getattr(report, "path", "") or "")
    return os.path.join(directory, SIDECAR_NAME) if directory else ""


def read_meta(report):
    """A report's stored metadata mapping (never None).

    Read from a sidecar file next to the reports so marking, pinning and
    tagging survive a scan without a database and without writing into the
    report artifact itself -- a report is evidence, and evidence is not edited.
    """
    path = _sidecar_path(report)
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
    except (OSError, ValueError):
        return {}
    entry = loaded.get(os.path.basename(getattr(report, "path", "")))
    return dict(entry) if isinstance(entry, dict) else {}


def set_meta(report, changes, window=None):
    """Merge `changes` into a report's sidecar metadata (offline, atomic)."""
    key = os.path.basename(getattr(report, "path", "") or "")
    if not key:
        return result("meta", False, "the report has no path", report)
    path = _sidecar_path(report)
    data = {}
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            data = {}
    entry = data.get(key)
    if not isinstance(entry, dict):
        entry = {}
    entry.update(changes)
    data[key] = entry
    try:
        temporary = path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
    except OSError as exc:
        return result("meta", False, str(exc), report)
    return result("meta", True, "metadata stored", report, {"meta": entry})


def is_important(report):
    return bool(read_meta(report).get(IMPORTANT_KEY))


def is_favourite(report):
    return bool(read_meta(report).get(FAVOURITE_KEY))


def projects_of(report):
    stored = read_meta(report).get(PROJECTS_KEY)
    if stored:
        return list(stored)
    project = getattr(report, "project", "")
    return [project] if project else []


def tags_of(report):
    return list(getattr(report, "tags", []) or [])


def mark_important(report, important=True, window=None):
    """Flag a report as important, or clear the flag."""
    outcome = set_meta(report, {IMPORTANT_KEY: bool(important)}, window)
    if not outcome["ok"]:
        return result("mark_important", False, outcome["detail"], report)
    return result("mark_important", True,
                  "marked as important" if important else "unmarked", report,
                  {"important": bool(important)})


def pin_to_project(report, project=None, window=None):
    """Attach a report to a project, or detach it when `project` is empty."""
    if project:
        return result("pin_project", True, "pinned to %s" % project, report,
                      set_meta(report, {PROJECTS_KEY: [project]}, window)["data"])
    outcome = set_meta(report, {PROJECTS_KEY: []}, window)
    if not outcome["ok"]:
        return result("pin_project", False, outcome["detail"], report)
    return result("pin_project", True, "unpinned", report)


def pin_to_favourite(report, favourite=True, window=None):
    """Add a report to (or remove it from) the favourites tab."""
    outcome = set_meta(report, {FAVOURITE_KEY: bool(favourite)}, window)
    if not outcome["ok"]:
        return result("pin_favourite", False, outcome["detail"], report)
    return result("pin_favourite", True,
                  "pinned to favourites" if favourite else "unpinned", report,
                  {"favourite": bool(favourite)})


def add_tags(report, add=(), remove=(), window=None):
    """Add and remove tags on a report, keeping the rest.

    The effective tags are the sidecar's own list when it has one, otherwise
    the tags the classifier detected -- so tagging a report the first time
    starts from what it already looked like, and the classifier never silently
    overwrites a user's choice.
    """
    stored = read_meta(report).get("tags")
    current = list(stored) if stored else tags_of(report)
    if isinstance(add, str):
        add = [a.strip() for a in re.split(r"[,\s]+", add) if a.strip()]
    if isinstance(remove, str):
        remove = [r.strip() for r in re.split(r"[,\s]+", remove) if r.strip()]
    lowered_remove = {r.lower() for r in (remove or [])}
    result_tags = [t for t in current if t.lower() not in lowered_remove]
    for tag in add or []:
        if tag and tag.lower() not in {t.lower() for t in result_tags}:
            result_tags.append(tag)
    outcome = set_meta(report, {"tags": result_tags}, window)
    if not outcome["ok"]:
        return result("tag", False, outcome["detail"], report)
    return result("tag", True, "tags: %s" % ", ".join(result_tags) or "-",
                  report, {"tags": result_tags})


def compare(left, right, left_body=None, right_body=None):
    """Put two reports side by side as a diff."""
    if left is None or right is None:
        return result("compare", False, "two reports are needed", left)
    comparison = diff_reports(left, right, left_body, right_body)
    return result("compare", True,
                  "%s vs %s: %d changed line(s)"
                  % (getattr(left, "name", ""), getattr(right, "name", ""),
                     comparison["changed"]), left, comparison)


def show_versions(left, right, left_body=None, right_body=None):
    """Show what changed between two versions of a report."""
    outcome = detect_changes(left, right, left_body=left_body,
                             right_body=right_body)
    return result("versions", True, outcome["detail"], left, outcome)


def delete_report(report, confirm=None):
    """Delete a report file (the caller decides whether to ask first).

    `confirm` is an optional callable; when it returns false the file is left
    alone, so the tab can pass a confirmation dialog and a script can pass
    nothing and delete unconditionally.
    """
    if report is None:
        return result("delete", False, "no report selected")
    if callable(confirm) and not confirm(report):
        return result("delete", False, "cancelled", report)
    try:
        os.remove(getattr(report, "path", ""))
    except OSError as exc:
        return result("delete", False, str(exc), report)
    return result("delete", True, "report deleted", report)


_BRIDGE_OPS = {
    "open": lambda window, report, **kw: result(
        "open", report is not None,
        "opened" if report is not None else "no report selected", report),
    "delete": lambda window, report, **kw: delete_report(
        report, kw.get("confirm")),
    "mark_important": lambda window, report, **kw: mark_important(
        report, kw.get("important", True), window),
    "pin_project": lambda window, report, **kw: pin_to_project(
        report, kw.get("project"), window),
    "pin_favourite": lambda window, report, **kw: pin_to_favourite(
        report, kw.get("favourite", True), window),
    "tag": lambda window, report, **kw: add_tags(
        report, kw.get("add", ()), kw.get("remove", ()), window),
    "compare": lambda window, report, **kw: compare(
        report, kw.get("other")),
    "versions": lambda window, report, **kw: show_versions(
        report, kw.get("other")),
}


def run_operation(window, key, report, **kwargs):
    """Run one named operation on a report (see :data:`OPERATIONS`)."""
    key = str(key or "").strip().lower()
    handler = _BRIDGE_OPS.get(key)
    if handler is None:
        return result(key, False, "unknown operation %r (known: %s)"
                      % (key, ", ".join(OPERATION_KEYS)), report)
    try:
        return handler(window, report, **kwargs)
    except Exception as exc:
        return result(key, False, "operation failed: %s" % exc, report)


# ---------------------------------------------------------------------------
# Describing the surface
# ---------------------------------------------------------------------------
def format_analyses(window=None):
    """The seven analyses as ASCII lines."""
    lines = ["Report analyses", "==============="]
    for key, _lk, fallback, reads, gives in ANALYSES:
        lines.append("%-12s %s" % (key, fallback))
        lines.append("             reads : %s" % reads)
        lines.append("             gives : %s" % gives)
    return lines


def format_integrations(window=None):
    """The six integration targets as ASCII lines, with live availability."""
    lines = ["Report integrations", "==================="]
    for key, _lk, fallback, _receives, _gives in INTEGRATIONS:
        open_now = target_tab(window, key) is not None if window is not None \
            else False
        lines.append("%-10s %-4s %s" % (key, "open" if open_now else "n/a",
                                        fallback))
    return lines


def format_operations(window=None):
    """The eight operations as ASCII lines."""
    lines = ["Report operations", "================="]
    for key, _lk, fallback, args, meaning in OPERATIONS:
        lines.append("%-14s %s  (%s)" % (key, fallback, ", ".join(args) or "-"))
    return lines


def analysis_capabilities(window=None):
    """Describe the analysis, integration and operation surfaces."""
    return {
        "analyses": list(ANALYSIS_KEYS),
        "analysis_labels": dict(ANALYSIS_LABELS),
        "trend_series": list(TREND_SERIES),
        "trend_labels": dict(TREND_LABELS),
        "metric_keys": list(METRIC_KEYS),
        "metric_labels": dict(METRIC_LABELS),
        "integrations": list(INTEGRATION_KEYS),
        "integration_labels": dict(INTEGRATION_LABELS),
        "available": {key: target_tab(window, key) is not None
                      for key in INTEGRATION_KEYS},
        "operations": list(OPERATION_KEYS),
        "operation_labels": dict(OPERATION_LABELS),
        "operation_arguments": dict(OPERATION_ARGUMENTS),
        "offline": True,
        "ascii_only": True,
        "zero_dependency": True,
        "cross_platform": True,
    }
