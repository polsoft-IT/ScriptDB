# -*- coding: utf-8 -*-
"""Debugger tab for ScriptDB.

This module owns the "Debugger" tab: an offline debugging workspace that sits
right after the Audit tab. It does not spawn a real process -- it gives the
user a place to prepare and inspect a debugging session against the scripts in
the database, and it keeps a log of what was inspected. Everything is local and
uses the standard library only.

The tab follows the same one-file-per-tab pattern as the other tabs:

    DebuggerTab(window)          -- build the tab
    tab.refresh()                -- reload the script list
    tab.retranslate(table)       -- apply a language table (see TRANSLATIONS)

What it offers:

    languages      a multilingual engine: every language has its own adapter
                   (Python, JavaScript / Node, C++, Java, PowerShell, Batch /
                   CMD, VBScript, Shell (bash/sh), JSON and YAML), and an
                   adapter can be added, replaced or removed at run time (see
                   `tabs/debug_langs.py`)
    validation     a runtime check of the selected script in the language of
                   its adapter, without running it: the adapter reports a
                   syntax fault with its line and column, or validates a data
                   document (JSON / YAML) and counts what it holds
    debug overview what the adapter can see in the script: the frames, the
                   annotations (definitions, classes, variables, imports, call
                   sites, data blocks) and the breakpoints the adapter marked
    breakpoints    a list of breakpoints (script name + line + kind + condition)
                   that can be added, removed and toggled without touching the
                   scripts themselves
    watches        a watch list of expressions the user wants to follow
    stack          a call-stack view: the frames of the selected breakpoint,
                   shown as `#index  frame  (line)`
    trace log      an append-only session log (start / pause / step / continue /
                   stop / note), so a debugging session is reproducible
    run summary    a small header with the counts (breakpoints, watches,
                   frames, log entries)

All of it is offline: the tab reads the script database and keeps its own small
JSON session file next to it, and nothing is ever uploaded. The engine reads
code, it never runs it.

The main window only needs a few lines to wire the tab in::

    from tabs import DebuggerTab
    self.debugger_tab = DebuggerTab(self)
    self.tabs.addTab(self.debugger_tab, "Debugger")
"""

import datetime as dt
import json
import os
import re
from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtGui import QFont
from . import (breakpoints, code_flow, debug_langs, debug_steps,
               flow_view)
# Session file kept next to the script database.
DEBUGGER_FILE = "debugger_db.json"
DEBUGGER_FONT = "Consolas"
DEBUGGER_FONT_SIZE = 10

# The kinds of breakpoint a user can set. The model (targets, conditions,
# matching) lives in `tabs/breakpoints.py`; the list here is the UI order.
BREAKPOINT_KINDS = breakpoints.BREAKPOINT_KINDS
# The columns the breakpoint tree shows, in order.
BREAKPOINT_COLUMNS = ("script", "kind", "target", "line", "condition")
# The trace-log actions, in the order the buttons are shown.
TRACE_ACTIONS = (
    ("start", "debugger_trace_start", "Start"),
    ("pause", "debugger_trace_pause", "Pause"),
    ("step", "debugger_trace_step", "Step"),
    ("continue", "debugger_trace_continue", "Continue"),
    ("stop", "debugger_trace_stop", "Stop"),
    ("note", "debugger_trace_note", "Note"),
)

# Language-sensitive trace actions: they carry the language they were taken in,
# so the log stays readable once more than one language is in play.
LANGUAGE_TRACE_ACTIONS = ("start", "step", "stop")

# How many annotations the debug overview lists before it truncates.
DEBUGGER_MAX_ANNOTATIONS = 40
# How many timeline lines the flow pane keeps, so a long recording stays
# responsive while the full timeline is still available on the recording.
DEBUGGER_MAX_FLOW_LINES = 2000
# The kinds the flow filter offers, in the order they are listed.
FLOW_FILTER_KINDS = debug_langs.FLOW_KINDS


def _now():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _as_int(value):
    """Coerce a text line number to an int (or None)."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def adapter_for_script(script):
    """The multilingual engine's adapter for a script entry (never None).

    The script's category is the first choice, its file name the second, so a
    script stored as "Other" but named `deploy.sh` still resolves to Shell.
    """
    script = script or {}
    return debug_langs.adapter_for(script.get("category", ""),
                                  extension=script.get("name", ""))


def validate_script(script):
    """Validate one script in its own language, without running it.

    Returns the engine's validation record: `ok`, `line`, `column`, `error`,
    `summary` and the counts the adapter could establish. An empty document
    validates as not ok with "empty document" rather than raising.
    """
    script = script or {}
    adapter = adapter_for_script(script)
    return adapter.validate(script.get("code", "") or "")


def describe_script(script):
    """What the multilingual engine can see in a script (a debug overview).

    The result is JSON-friendly: the adapter identity, the frames, the block
    annotations, the marker annotations (imports, variables, breakpoints) and
    the validation record -- everything the Debugger tab prints.
    """
    script = script or {}
    adapter = adapter_for_script(script)
    code = script.get("code", "") or ""
    adapter.text = code
    blocks = adapter.blocks()
    markers = adapter.marker_annotations()
    validation = adapter.validate(code)
    counts = debug_langs.annotation_summary(blocks + markers)
    return {
        "script": script.get("name", ""),
        "language": adapter.key,
        "label": adapter.label,
        "data_format": adapter.data_format,
        "lines": len(code.splitlines()),
        "frames": adapter.frames(),
        "blocks": blocks,
        "markers": markers,
        "counts": counts,
        "validation": validation,
        "problems": ([] if validation.get("ok")
                     else [validation.get("error", "invalid document")]),
        "breakpoint_markers": [entry["line"] for entry in markers
                               if entry["kind"] == "breakpoint"],
    }


def build_debug_overview(script):
    """The debug overview as text lines (used by the pane and the report)."""
    info = describe_script(script)
    validation = info["validation"]
    lines = [
        "Debug overview",
        "--------------",
        "Script    : %s" % info["script"],
        "Language  : %s [%s]" % (info["label"], info["language"]),
        "Lines     : %d" % info["lines"],
        "Adapter   : %s" % ("document validator"
                            if info["data_format"] else "code reader"),
        "",
        "Validation",
        "----------",
        "Status    : %s" % ("ok" if validation.get("ok") else "failed"),
        "Summary   : %s" % validation.get("summary", ""),
    ]
    if not validation.get("ok"):
        lines.append("Error     : %s" % validation.get("error", ""))
        if validation.get("line"):
            lines.append("At        : line %s, column %s" % (
                validation.get("line"), validation.get("column")))
    counts = validation.get("counts") or {}
    if counts:
        lines.append("Counts    : %s" % ", ".join(
            "%s=%s" % (key, value) for key, value in sorted(counts.items())))
    lines += ["", "Frames", "------"]
    frames = info["frames"]
    if frames:
        lines += ["#%d  %s  (line %d)%s" % (
            index, frame.get("name", "?"), frame.get("line", 0),
            "  <- current" if frame.get("current") else "")
            for index, frame in enumerate(frames)]
    else:
        lines.append("(no frames)")
    lines += ["", "Annotations", "-----------"]
    annotations = (info["blocks"] + info["markers"])[:DEBUGGER_MAX_ANNOTATIONS]
    if annotations:
        lines += ["line %-5s %-12s %-22s %s" % (
            entry.get("line", 0), entry.get("kind", "?"),
            (entry.get("name", "") or "")[:22],
            (entry.get("text", "") or "")[:60]) for entry in annotations]
    else:
        lines.append("(no annotations)")
    return lines


class DebuggerSession(object):
    """The persistent part of the tab: breakpoints, watches and the trace log.

    The session is a plain dict that is read from and written to a small JSON
    file. It never touches the script database, so a debugging session can be
    prepared and kept without modifying anything.
    """

    def __init__(self, path=None):
        self.path = path
        self.breakpoints = []
        self.watches = []
        self.trace = []
        # The language adapter the user turned on, the last validation run per
        # script and the code-flow recordings, so a session can be reopened
        # exactly as it was left.
        self.engines = []
        self.validation = {}
        self.flow = []
        self.loaded = False

    # ------------------------------------------------------------------ I/O
    def to_dict(self):
        return {
            "breakpoints": list(self.breakpoints),
            "watches": list(self.watches),
            "trace": list(self.trace),
            "engines": list(self.engines),
            "validation": dict(self.validation),
            "flow": list(self.flow),
        }

    def load(self):
        """Read the session from disk (missing or broken file -> empty)."""
        if not self.path or not os.path.isfile(self.path):
            return False
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return False
        if not isinstance(data, dict):
            return False
        self.breakpoints = [bp for bp in data.get("breakpoints", [])
                            if isinstance(bp, dict)]
        self.watches = [w for w in data.get("watches", []) if isinstance(w, str)]
        self.trace = [t for t in data.get("trace", []) if isinstance(t, dict)]
        self.engines = [e for e in data.get("engines", [])
                        if isinstance(e, str)]
        stored = data.get("validation", {})
        self.validation = dict(stored) if isinstance(stored, dict) else {}
        # The code-flow timeline: a list holding the last recording's dict.
        flow = data.get("flow", [])
        self.flow = [item for item in flow if isinstance(item, dict)] \
            if isinstance(flow, list) else []
        self.loaded = True
        return True

    def save(self):
        """Write the session to disk (returns the path or "")."""
        if not self.path:
            return ""
        try:
            with open(self.path, "w", encoding="utf-8") as handle:
                json.dump(self.to_dict(), handle, ensure_ascii=False, indent=2)
        except OSError:
            return ""
        return self.path

    # ---------------------------------------------------------- breakpoints
    def add_breakpoint(self, script, line, kind="line", condition="",
                       language="", target=""):
        """Add a breakpoint (deduplicated by script + kind + target).

        The record is built by `tabs.breakpoints.make`, so it carries the
        eight-kind model (target, condition, resolved, hits) and stays
        readable by the whole breakpoint API.
        """
        # A breakpoint always needs a usable integer line: the view's line box
        # is free text, so a non-numeric entry is rejected rather than
        # silently stored as line 0.
        wanted_line = _as_int(line)
        if wanted_line is None:
            return None
        record = breakpoints.make(kind, script=script, line=wanted_line,
                                  target=target, condition=condition,
                                  language=language)
        if record is None:
            return None
        for existing in self.breakpoints:
            if (existing.get("script") == record["script"]
                    and existing.get("kind") == record["kind"]
                    and existing.get("target", "") == record.get("target", "")
                    and existing.get("line") == record["line"]):
                return existing
        self.breakpoints.append(record)
        self._sort_breakpoints()
        return record

    def _sort_breakpoints(self):
        """Keep the list ordered by script, then kind, then line."""
        order = {kind: position for position, kind in
                 enumerate(BREAKPOINT_KINDS)}
        self.breakpoints.sort(key=lambda bp: (
            bp.get("script", ""),
            bp.get("line", 0),
            order.get(bp.get("kind", "line"), 99),
            bp.get("target", "")))

    def update_breakpoint(self, record, **fields):
        """Change fields of a breakpoint in place, then re-sort."""
        if record is None:
            return None
        record.update(fields)
        self._sort_breakpoints()
        return record

    def remove_breakpoint(self, record=None, line=None, kind=None,
                          script=None, target=None):
        """Remove one record, or every record matching the fields.

        Called with a record dict it removes exactly that object. Called
        with a script name it removes everything that matches, and the
        legacy positional form remove_breakpoint(script, line, kind)
        keeps working so existing callers are unaffected.
        """
        before = len(self.breakpoints)
        if isinstance(record, dict):
            self.breakpoints = [bp for bp in self.breakpoints
                                if bp is not record]
            return before - len(self.breakpoints)
        # The first positional argument is the script name in the old API.
        if script is None:
            script = record
        wanted_line = None if line is None else _as_int(line)
        self.breakpoints = [
            bp for bp in self.breakpoints
            if not (bp.get("script") == script
                    and (wanted_line is None
                         or bp.get("line") == wanted_line)
                    and (kind is None or bp.get("kind") == kind)
                    and (target is None
                         or bp.get("target", "") == target))]
        return before - len(self.breakpoints)

    def toggle_breakpoint(self, record=None, line=None, kind=None,
                          script=None, target=None):
        """Enable / disable a record, or the matching ones.

        Accepts a record dict, or the legacy positional form
        toggle_breakpoint(script, line, kind).
        """
        state = None
        if isinstance(record, dict):
            candidates = [record]
        else:
            if script is None:
                script = record
            wanted_line = None if line is None else _as_int(line)
            candidates = [
                bp for bp in self.breakpoints
                if bp.get("script") == script
                and (wanted_line is None
                     or bp.get("line") == wanted_line)
                and (kind is None or bp.get("kind") == kind)
                and (target is None
                     or bp.get("target", "") == target)]
        for bp in candidates:
            bp["enabled"] = not bp.get("enabled", True)
            state = bp["enabled"]
        return state

    def clear_breakpoints(self):
        """Remove every breakpoint; returns how many there were."""
        count = len(self.breakpoints)
        self.breakpoints = []
        return count
    # ------------------------------------------------------------- engines
    def set_engines(self, keys):
        """Remember which language engines the user turned on."""
        self.engines = [key for key in (keys or []) if key]
        return self.engines

    def record_validation(self, script, result):
        """Store the last validation of one script (a compact record)."""
        if not script or not isinstance(result, dict):
            return None
        record = {
            "language": result.get("language", ""),
            "ok": bool(result.get("ok")),
            "error": result.get("error", ""),
            "line": result.get("line", 0),
            "column": result.get("column", 0),
            "summary": result.get("summary", ""),
            "document": bool(result.get("kind") == debug_langs.DOCUMENT_KIND),
            "at": _now(),
        }
        self.validation[script] = record
        return record

    def validated_languages(self):
        """Every language with a recorded validation, in a stable order."""
        seen = []
        for record in self.validation.values():
            language = record.get("language", "")
            if language and language not in seen:
                seen.append(language)
        return seen

    def clear_validation(self):
        count = len(self.validation)
        self.validation = {}
        return count

    # ---------------------------------------------------------- code flow
    def set_flow(self, recording):
        """Remember a recording as the session's flow timeline."""
        self.flow = ([recording.to_dict()] if recording is not None
                     else [])
        return self.flow

    def flow_counts(self):
        """The event counts of the remembered recording."""
        if not self.flow:
            return {}
        return dict(self.flow[-1].get("counts", {}) or {})

    def flow_events(self):
        """The events of the remembered recording, as dicts."""
        if not self.flow:
            return []
        return list(self.flow[-1].get("events", []) or [])

    def clear_flow(self):
        count = len(self.flow_events())
        self.flow = []
        return count

    # -------------------------------------------------------------- watches
    def add_watch(self, expression):
        expression = (expression or "").strip()
        if not expression or expression in self.watches:
            return expression or ""
        self.watches.append(expression)
        return expression

    def remove_watch(self, expression):
        before = len(self.watches)
        self.watches = [w for w in self.watches if w != expression]
        return before - len(self.watches)

    # ---------------------------------------------------------------- trace
    def log(self, action, detail=""):
        """Append one trace-log entry (action, detail, timestamp)."""
        entry = {"action": action, "detail": detail or "", "at": _now()}
        self.trace.append(entry)
        return entry

    def clear_trace(self):
        count = len(self.trace)
        self.trace = []
        return count

    # ------------------------------------------------------------ summaries
    def summary(self):
        """The headline counts for the run summary header."""
        enabled = sum(1 for bp in self.breakpoints if bp.get("enabled", True))
        failed = sum(1 for record in self.validation.values()
                     if not record.get("ok"))
        return {
            "breakpoints": len(self.breakpoints),
            "breakpoints_enabled": enabled,
            "watches": len(self.watches),
            "trace": len(self.trace),
            "engines": len(self.engines),
            "validation": len(self.validation),
            "validation_failed": failed,
        }


def build_stack(script, breakpoint_line=None, frames=6, category="",
                name=""):
    """A lightweight, offline call-stack view for a script.

    Returns a list of frame dicts, innermost first. The frames come from the
    multilingual engine, so the preview is right in every language: the adapter
    for `category` (or for the file `name`) decides what counts as a frame.

        name    `definition: foo`, `class: Bar` or `module`
        line    the 1-based line the frame starts on
        current True for the frame the breakpoint line lands in

    An empty `category` and no `name` keeps the original, language-agnostic
    behaviour (any `def` / `class` / `function` / `sub` / `proc` line is a
    frame), which is what an entry with no category at all should get.
    """
    if not category and not name:
        return build_generic_stack(script, breakpoint_line, frames)
    adapter = debug_langs.adapter_for(category, extension=name or "")
    adapter.text = script or ""
    stack = adapter.frames(breakpoint_line)
    return stack[:frames] if frames else stack


def build_generic_stack(script, breakpoint_line=None, frames=6):
    """The language-agnostic frame preview.

    Any `def` / `class` / `function` / `sub` / `proc` line counts as a frame,
    which is what a script entry with no category at all should get.
    """
    lines = (script or "").splitlines()
    if not lines:
        return []
    defs = []
    for number, raw in enumerate(lines, 1):
        match = re.match(
            r"^\s*(def|class|function|sub|proc)\s+([A-Za-z_][\w.]*)", raw,
            re.IGNORECASE)
        if match:
            kind = match.group(1).lower()
            kind = "class" if kind == "class" else "function"
            defs.append({"name": "%s: %s" % (kind, match.group(2)),
                         "line": number})
    if not defs:
        defs.append({"name": "module", "line": 1})
    # The frame the breakpoint line falls in is the last definition at or
    # before that line.
    target = breakpoint_line if breakpoint_line else 1
    current_index = 0
    for index, definition in enumerate(defs):
        if definition["line"] <= target:
            current_index = index
        else:
            break
    stack = []
    for index, definition in enumerate(defs):
        frame = dict(definition)
        frame["current"] = index == current_index
        stack.append(frame)
    # Innermost first, capped so a huge script cannot flood the view.
    stack.reverse()
    return stack[:frames]


def evaluate_watches(watches, variables=None):
    """Resolve a watch list against a variables mapping (offline).

    `variables` maps an expression to a value the user typed (or that was
    captured). A watch whose expression is unknown is reported as
    `(unresolved)` so the view still lists it. Returns a list of
    (expression, value) pairs, keeping the watch order.
    """
    variables = variables or {}
    out = []
    for expression in watches or []:
        if expression in variables:
            out.append((expression, variables[expression]))
        else:
            out.append((expression, "(unresolved)"))
    return out


def format_stack(frames):
    """The stack frames as text lines (`#i  name  (line)N  <- current`)."""
    lines = []
    for index, frame in enumerate(frames or []):
        marker = "  <- current" if frame.get("current") else ""
        lines.append("#%d  %s  (line %d)%s" % (
            index, frame.get("name", "?"), frame.get("line", 0), marker))
    return lines


def format_trace(trace):
    """The trace log as text lines (one per entry)."""
    lines = []
    for entry in trace or []:
        detail = entry.get("detail", "")
        suffix = ("  " + detail) if detail else ""
        lines.append("[%s] %s%s" % (entry.get("at", ""),
                                    entry.get("action", ""), suffix))
    return lines


def record_flow(script):
    """Record the full execution timeline of a script (never raises).

    A thin wrapper over the Code Flow Recorder, so the tab (and a report)
    can record a script with one call and without importing the recorder.
    """
    try:
        return code_flow.record(script or {})
    except Exception as exc:  # a malformed entry must not break the tab
        name = script.get("name", "") if isinstance(script, dict) else ""
        recording = code_flow.CodeFlowRecording(name)
        recording.add(code_flow.FlowEvent(0, "error", name=str(exc),
                                          detail=str(exc)))
        return recording.finish()


# ---------------------------------------------------------------------------
# The architecture standard, the premium features and the integrations
# ---------------------------------------------------------------------------
# The same three tables every other tab carries, with the same keys, so an
# "about" page can print the whole terminal's standards in one grid without a
# translation layer between them.
DEBUGGER_ARCHITECTURE = (
    ("ascii", "debugger_arch_ascii", "ASCII-only",
     "every view, report and log line stays within printable ASCII"),
    ("hot_pluggable", "debugger_arch_hot", "Hot-pluggable",
     "a language adapter is registered, replaced or removed at run time"),
    ("zero_dependency", "debugger_arch_zero", "Zero-dependency",
     "the standard library and the language adapters only; nothing is executed"),
    ("cross_platform", "debugger_arch_cross", "Cross-platform",
     "Windows, macOS and Linux path and line conventions are all handled"),
    ("plugin_ready", "debugger_arch_plugin", "Plugin-ready",
     "STEP_MODES, REPLAY_MODES and the adapter registry are the extension points"),
    ("sandbox_safe", "debugger_arch_sandbox", "Sandbox-safe",
     "the session engine never calls eval and never starts a process"),
    ("artefact_driven", "debugger_arch_artefact", "Artefact-driven",
     "a session is its JSON file: it can be snapshotted, replayed and diffed"),
    ("api_cli_gui", "debugger_arch_frontends", "API + CLI + GUI",
     "one session surface reachable from code, from a terminal and from the tab"),
    ("integration", "debugger_arch_integration", "Terminal integration",
     "the analyser's findings, the sandbox's runs, the audit bus and the "
     "reports repository are all reachable from the session"),
)

DEBUGGER_ARCHITECTURE_KEYS = tuple(key for key, _lk, _f, _d
                                   in DEBUGGER_ARCHITECTURE)
DEBUGGER_ARCHITECTURE_LABELS = {key: fallback for key, _lk, fallback, _d
                                in DEBUGGER_ARCHITECTURE}

# (key, label key, fallback, what it gives a user)
DEBUGGER_PREMIUM = (
    ("step_modes", "debugger_prem_steps", "Five step modes",
     "step over, into, out, to the next breakpoint and to a line"),
    ("conditions", "debugger_prem_conditions", "Conditional breakpoints",
     "a breakpoint stops only when its condition holds at the event"),
    ("hit_counts", "debugger_prem_hits", "Hit counts",
     "every breakpoint counts its hits and can stop on the nth one"),
    ("snapshots", "debugger_prem_snapshots", "Session snapshots",
     "freeze a session at a point, return to it, or diff two points"),
    ("replay", "debugger_prem_replay", "Replay",
     "replay a session step by step, as a transcript, or backwards"),
    ("watch_eval", "debugger_prem_watch", "Watch evaluation",
     "the watch list resolves against the state the session reached"),
    ("timeline", "debugger_prem_timeline", "Recorded timeline",
     "a full code-flow recording that stepping walks as its program order"),
    ("session_file", "debugger_prem_session", "Portable session",
     "the whole session is one JSON artefact that survives a restart"),
)

DEBUGGER_PREMIUM_KEYS = tuple(key for key, _lk, _f, _d in DEBUGGER_PREMIUM)
DEBUGGER_PREMIUM_LABELS = {key: fallback for key, _lk, fallback, _d
                           in DEBUGGER_PREMIUM}

# (key, label key, fallback, what is exchanged)
DEBUGGER_INTEGRATIONS = (
    ("analyzer", "debugger_int_analyzer", "Script analyser",
     "the analyser's findings pre-seed the session's validation state"),
    ("sandbox", "debugger_int_sandbox", "Sandbox",
     "a session can be replayed in the sandbox, and a sandbox run can be "
     "opened as a debugger session"),
    ("audit", "debugger_int_audit", "Audit",
     "breakpoints, sessions and snapshots are announced on the audit bus"),
    ("reports", "debugger_int_reports", "Reports",
     "a session, a snapshot diff or a replay is written as a report artefact"),
    ("diff", "debugger_int_diff", "Diff",
     "two snapshots of one session are compared as a diff"),
    ("search", "debugger_int_search", "Search",
     "the session artefacts are findable by content, like any other report"),
    ("runner", "debugger_int_runner", "Script runner",
     "the script a session targets can be handed to the runner"),
    ("docs", "debugger_int_docs", "Documentation",
     "the session's capabilities are generated into a documentation article"),
)

DEBUGGER_INTEGRATION_KEYS = tuple(key for key, _lk, _f, _d
                                  in DEBUGGER_INTEGRATIONS)
DEBUGGER_INTEGRATION_LABELS = {key: fallback for key, _lk, fallback, _d
                               in DEBUGGER_INTEGRATIONS}

# The step bus: a plain subscriber list for the high-frequency step events,
# kept separate from the audit bus so a step observer does not force one audit
# entry per keypress.
STEP_SUBSCRIBERS = []


def debugger_capabilities():
    """Describe the debugger for the audit / about layer."""
    engine = debug_langs.engine_info()
    flow = code_flow.flow_capabilities()
    return {
        "breakpoint_kinds": list(BREAKPOINT_KINDS),
        "trace_actions": [key for key, _lk, _f in TRACE_ACTIONS],
        "features": ["breakpoints", "watches", "stack", "trace", "summary",
                     "languages", "validation", "debug_overview",
                     "code_flow"],
        "flow_kinds": list(flow.get("kinds", [])),
        "flow_block_kinds": list(flow.get("block_kinds", [])),
        "flow_max_events": flow.get("max_events", 0),
        "languages": list(engine.get("keys", [])),
        "language_adapters": engine.get("adapter_details", []),
        "validates_documents": engine.get("validates_documents", []),
        "annotation_kinds": list(debug_langs.ANNOTATION_KINDS),
        "step_modes": list(debug_steps.STEP_MODE_KEYS),
        "replay_modes": list(debug_steps.REPLAY_MODE_KEYS),
        "session_features": list(debug_steps.SESSION_FEATURE_KEYS),
        "condition_fields": list(debug_steps.CONDITION_FIELDS),
        "condition_operators": list(debug_steps.CONDITION_OPERATORS),
        "architecture": [key for key, _lk, _f, _d in DEBUGGER_ARCHITECTURE],
        "premium": [key for key, _lk, _f, _d in DEBUGGER_PREMIUM],
        "integrations": [key for key, _lk, _f, _d in DEBUGGER_INTEGRATIONS],
        "hot_pluggable": bool(engine.get("hot_pluggable")),
        "offline": True,
    }


def debugger_session_record(script, session=None, events=None):
    """A full description of a session over one script, as plain data.

    This is the API/CLI view of a session: it records the timeline, builds a
    runner, and reports what the session can see and where it stands. It is
    head-less on purpose, so a test or the CLI can describe a session without a
    window existing.
    """
    script = script or {}
    recording = events if events is not None else record_flow(script)
    flow_events = getattr(recording, "events", recording) or []
    breakpoints = list(getattr(session, "breakpoints", []) or [])
    watches = list(getattr(session, "watches", []) or [])
    runner = debug_steps.SessionRunner(events=flow_events,
                                       breakpoints=breakpoints, watches=watches)
    return {
        "script": script.get("name", ""),
        "language": adapter_for_script(script).key,
        "events": len(flow_events),
        "step_modes": list(debug_steps.STEP_MODE_KEYS),
        "replay_modes": list(debug_steps.REPLAY_MODE_KEYS),
        "breakpoints": len(breakpoints),
        "watches": len(watches),
        "position": runner.position(),
        "track": (runner.transcript_lines()[:60]),
    }


def debugger_api(window=None, tab=None):
    """A machine-readable description of the Debugger (the API front-end).

    Describes the *debugger*, not the tab: the same document comes back with or
    without a window, so a caller can decide what to do before deciding whether
    to build one.
    """
    tab = tab or (getattr(window, "debugger_tab", None)
                  if window is not None else None)
    engine = debug_langs.engine_info()
    return {
        "name": "Debugger",
        "available": True,
        "frontends": ["api", "cli", "gui"],
        "gui_open": tab is not None,
        "languages": list(engine.get("keys", [])),
        "step_modes": list(debug_steps.STEP_MODE_KEYS),
        "replay_modes": list(debug_steps.REPLAY_MODE_KEYS),
        "session_features": list(debug_steps.SESSION_FEATURE_KEYS),
        "breakpoint_kinds": list(BREAKPOINT_KINDS),
        "condition_fields": list(debug_steps.CONDITION_FIELDS),
        "architecture": debugger_architecture(window),
        "capabilities": debugger_capabilities(),
    }


def debugger_cli(argv=None, window=None, out=None):
    """Head-less debugger entry point (zero-dependency, ASCII output).

    Supported commands::

        capabilities              - the debugger's tables, as ASCII
        api                       - the API description, as JSON
        architecture              - the architecture standard and its status
        premium                   - the premium features
        integrations              - which terminal modules are wired up
        steps                     - the five step modes
        replay                    - the replay modes
        conditions                - the condition vocabulary
        languages                 - the registered language adapters
        session <file> [n]        - record a timeline and show the first n steps

    Returns a printable string (callers print it), so it can be used from a test
    as easily as from a terminal. It never imports Qt.
    """
    argv = list(argv or [])
    command = argv[0] if argv else "capabilities"
    argument = argv[1] if len(argv) > 1 else None

    if command in ("capabilities", "help", "-h", "--help"):
        caps = debugger_capabilities()
        lines = ["ScriptDB debugger capabilities"]
        lines.append("languages: " + ", ".join(caps["languages"]))
        lines.append("breakpoint kinds: " + ", ".join(caps["breakpoint_kinds"]))
        lines.append("step modes: " + ", ".join(caps["step_modes"]))
        lines.append("replay modes: " + ", ".join(caps["replay_modes"]))
        lines.append("features: " + ", ".join(caps["features"]))
        lines.append("conditions: " + ", ".join(caps["condition_fields"]))
        return chr(10).join(lines)

    if command == "api":
        import json as _json
        return _json.dumps(debugger_api(window), indent=2, sort_keys=True,
                           default=str)

    if command == "architecture":
        return chr(10).join(format_debugger_architecture(window))

    if command == "premium":
        return chr(10).join(format_debugger_premium())

    if command == "integrations":
        lines = ["Debugger integrations", "===================="]
        for row in debugger_integration_matrix(window):
            lines.append("%-11s %-4s %s" % (
                row["key"], "wired" if row["available"] else "n/a",
                row["label"]))
            lines.append("            %s" % row["exchange"])
        return chr(10).join(lines)

    if command == "steps":
        lines = ["Debugger step modes", "==================="]
        for key, _lk, fallback, meaning in debug_steps.STEP_MODES:
            lines.append("%-11s %s" % (key, fallback))
            lines.append("            %s" % meaning)
        return chr(10).join(lines)

    if command == "replay":
        lines = ["Debugger replay modes", "====================="]
        for key, _lk, fallback, meaning in debug_steps.REPLAY_MODES:
            lines.append("%-11s %s" % (key, fallback))
            lines.append("            %s" % meaning)
        return chr(10).join(lines)

    if command == "conditions":
        lines = ["Debugger condition vocabulary", "============================"]
        lines.append("fields: " + ", ".join(debug_steps.CONDITION_FIELDS))
        lines.append("operators: "
                     + ", ".join(debug_steps.CONDITION_OPERATORS))
        lines.append("forms:")
        for example in ("line > 20", "depth == 2", "kind == call",
                        "line > 20 and depth == 1", "name"):
            ok, why = debug_steps.eval_condition(
                example, {"line": 42, "depth": 2, "kind": "call",
                          "name": "load"})
            lines.append("  %-26s -> %-5s (%s)" % (example, ok, why))
        return chr(10).join(lines)

    if command == "languages":
        engine = debug_langs.engine_info()
        lines = ["Debugger language adapters", "========================="]
        for row in engine.get("adapter_details", []):
            lines.append("%-12s %-10s %s" % (
                row.get("key", "?"), row.get("kind", ""),
                row.get("label", "")))
        return chr(10).join(lines)

    if command == "session":
        if not argument:
            return "usage: debugger session <file> [steps]"
        try:
            with open(argument, "r", encoding="utf-8",
                      errors="replace") as handle:
                code = handle.read()
        except OSError as exc:
            return "cannot read %s: %s" % (argument, exc)
        script = {"name": os.path.basename(argument),
                  "category": "Python", "code": code}
        record = debugger_session_record(script)
        lines = ["Debugger session: %s" % record["script"],
                 "===================",
                 "language : %s" % record["language"],
                 "events   : %d" % record["events"]]
        lines += record["track"]
        return chr(10).join(lines)

    return "unknown command: %s" % command


# ---------------------------------------------------------------------------
# The Qt tab
# ---------------------------------------------------------------------------


def format_debugger_architecture(window=None):
    """The architecture standard as ASCII lines, with its live status."""
    matrix = debugger_integration_matrix(window)
    available = sum(1 for row in matrix if row["available"])
    lines = ["Debugger architecture", "=====================",
             "integrations available: %d of %d" % (available, len(matrix)),
             ""]
    for key, _lk, fallback, meaning in DEBUGGER_ARCHITECTURE:
        lines.append("%-16s %s" % (key, fallback))
        lines.append("  %s" % meaning)
    return lines


def format_debugger_premium():
    """The premium features as ASCII lines."""
    lines = ["Debugger premium features", "========================="]
    for key, _lk, fallback, meaning in DEBUGGER_PREMIUM:
        lines.append("%-14s %s" % (key, fallback))
        lines.append("  %s" % meaning)
    return lines


def debugger_architecture(window=None):
    """The architecture standard as data, with the integrations resolved."""
    matrix = debugger_integration_matrix(window)
    available = sum(1 for row in matrix if row["available"])
    rows = []
    for key, label_key, fallback, meaning in DEBUGGER_ARCHITECTURE:
        ok = True
        if key == "api_cli_gui":
            ok = bool(window is not None and available > 0)
        elif key == "integration":
            ok = available > 0
        rows.append({"key": key, "label_key": label_key, "label": fallback,
                     "meaning": meaning, "ok": ok})
    return {
        "architecture": rows,
        "held": sum(1 for row in rows if row["ok"]),
        "total": len(rows),
        "integrations_available": available,
        "integrations_total": len(matrix),
        "premium": list(DEBUGGER_PREMIUM_KEYS),
        "offline": True,
    }


def _module_present(package, name):
    """True when a terminal module can be imported right now."""
    try:
        __import__("%s.%s" % (package, name))
        return True
    except Exception:
        return False


def _tab_present(window, *names):
    """True when the main window has one of the named tabs built."""
    if window is None:
        return False
    return any(getattr(window, name, None) is not None for name in names)


def debugger_integration_matrix(window=None):
    """``[{key, label, exchange, available}]`` for every integrated module."""
    probes = {
        "analyzer": lambda: _module_present("tabs", "analyzer"),
        "sandbox": lambda: _module_present("tabs", "sandbox"),
        "audit": lambda: _module_present("tabs", "audit"),
        "reports": lambda: _module_present("tabs", "reports_tab"),
        "search": lambda: _module_present("tabs", "search_engine"),
        "docs": lambda: _module_present("tabs", "docs_engine"),
        "diff": lambda: _tab_present(window, "diff_tab"),
        "runner": lambda: _tab_present(window, "runner_tab", "run_tab"),
    }
    rows = []
    for key, label_key, fallback, exchange in DEBUGGER_INTEGRATIONS:
        probe = probes.get(key)
        try:
            available = bool(probe and probe())
        except Exception:
            available = False
        rows.append({"key": key, "label_key": label_key, "label": fallback,
                     "exchange": exchange, "available": available})
    return rows


def notify_debug_step(runner, action):
    """Announce a step on the debugger's own step bus (best effort).

    The bus is a plain subscriber list rather than the audit bus because a step
    is high-frequency: a subscriber that wants every step should not force the
    audit log to record one entry per keypress.
    """
    payload = {"action": action, "position": (runner.position()
                                              if runner is not None else {})}
    reached = 0
    for callback in list(STEP_SUBSCRIBERS):
        try:
            callback(action, **payload)
            reached += 1
        except Exception:
            continue
    return reached


def subscribe_steps(callback):
    """Register a step observer. Returns an unsubscribe function."""
    STEP_SUBSCRIBERS.append(callback)

    def _unsubscribe():
        if callback in STEP_SUBSCRIBERS:
            STEP_SUBSCRIBERS.remove(callback)
    return _unsubscribe

# ---------------------------------------------------------------------------
# The Qt tab
# ---------------------------------------------------------------------------


class DebuggerTab(QtWidgets.QWidget):
    """An offline debugging workspace: breakpoints, watches, stack and trace.

    The tab never starts a real process; it prepares and inspects a debugging
    session against the scripts in the database and keeps a reproducible trace
    log. Every control is offline and reads only local files, and the
    multilingual engine annotates and validates a script in its own language
    without ever running it.
    """

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.session = DebuggerSession(self._session_path())
        self.session.load()
        self._frames = []
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

    def _session_path(self):
        return os.path.join(self._base_dir(), DEBUGGER_FILE)

    def _scripts(self):
        """All real script entries from the main window (never None)."""
        try:
            return list(self.window.scripts_only())
        except Exception:
            return list(getattr(self.window, "scripts", []) or [])

    def _script_by_name(self, name):
        for script in self._scripts():
            if script.get("name") == name:
                return script
        return None

    def _current_script(self):
        """The script selected in the picker (or the first one)."""
        name = self.script_combo.currentText() if hasattr(
            self, "script_combo") else ""
        script = self._script_by_name(name)
        if script is None:
            scripts = self._scripts()
            script = scripts[0] if scripts else None
        return script

    # ------------------------------------------------------------------- UI
    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(5)

        header = QtWidgets.QHBoxLayout()
        self.title_label = QtWidgets.QLabel(self.t("debugger", "Debugger"))
        self.title_label.setStyleSheet(
            "font-size: 14px; font-weight: bold; color: #8ab4f8;")
        header.addWidget(self.title_label)
        self.summary_label = QtWidgets.QLabel("")
        self.summary_label.setStyleSheet("color: #9aa0a6;")
        header.addWidget(self.summary_label)
        header.addStretch()
        self.btn_refresh = QtWidgets.QPushButton(
            self.t("debugger_refresh", "Refresh"))
        self.btn_refresh.clicked.connect(self.refresh)
        header.addWidget(self.btn_refresh)
        self.btn_clear = QtWidgets.QPushButton(
            self.t("debugger_clear", "Clear session"))
        self.btn_clear.clicked.connect(self.clear_session)
        header.addWidget(self.btn_clear)
        layout.addLayout(header)

        # ---- script + breakpoint controls --------------------------------
        control_row = QtWidgets.QHBoxLayout()
        control_row.addWidget(QtWidgets.QLabel(
            self.t("debugger_script", "Script:")))
        self.script_combo = QtWidgets.QComboBox()
        self.script_combo.currentIndexChanged.connect(self.on_script_changed)
        control_row.addWidget(self.script_combo, 2)

        control_row.addWidget(QtWidgets.QLabel(
            self.t("debugger_line", "Line:")))
        self.line_edit = QtWidgets.QLineEdit("1")
        self.line_edit.setMaximumWidth(70)
        control_row.addWidget(self.line_edit)

        self.kind_combo = QtWidgets.QComboBox()
        for kind in BREAKPOINT_KINDS:
            self.kind_combo.addItem(
                self.t("debugger_bp_" + kind, kind), kind)
        self.kind_combo.currentIndexChanged.connect(self.on_breakpoint_kind)
        control_row.addWidget(self.kind_combo)

        self.condition_edit = QtWidgets.QLineEdit()
        self.condition_edit.setPlaceholderText(
            self.t("debugger_condition", "Condition (optional)"))
        control_row.addWidget(self.condition_edit, 2)

        self.btn_add_bp = QtWidgets.QPushButton(
            self.t("debugger_add_bp", "Add breakpoint"))
        self.btn_add_bp.clicked.connect(self.add_breakpoint)
        control_row.addWidget(self.btn_add_bp)
        layout.addLayout(control_row)

        # ---- second breakpoint row: the target a chosen kind needs --------
        # A function / class / block / variable / exception / file breakpoint
        # names what to stop on, so the target editor and its hint sit here.
        self.target_row = QtWidgets.QHBoxLayout()
        self.target_label = QtWidgets.QLabel(
            self.t("debugger_target", "Target:"))
        self.target_row.addWidget(self.target_label)
        self.target_edit = QtWidgets.QLineEdit()
        self.target_edit.setPlaceholderText(
            self.t("debugger_target_hint", "Name to stop on..."))
        self.target_edit.returnPressed.connect(self.add_breakpoint)
        self.target_row.addWidget(self.target_edit, 2)

        # A block target is one of a known set, so it gets a picker instead of
        # free text: that is what keeps a block breakpoint valid.
        self.block_combo = QtWidgets.QComboBox()
        for block in breakpoints.BLOCK_TARGETS:
            self.block_combo.addItem(
                self.t("debugger_block_" + block, block), block)
        self.block_combo.setVisible(False)
        self.target_row.addWidget(self.block_combo)

        self.btn_resolve = QtWidgets.QPushButton(
            self.t("debugger_resolve", "Resolve"))
        self.btn_resolve.setToolTip(self.t(
            "debugger_resolve_hint",
            "Show the lines this breakpoint resolves to"))
        self.btn_resolve.clicked.connect(self.resolve_breakpoint)
        self.btn_remove_bp_row = QtWidgets.QPushButton(
            self.t("debugger_remove_bp", "Remove breakpoint"))
        self.btn_remove_bp_row.clicked.connect(self.remove_selected_breakpoint)
        self.target_row.addStretch()
        layout.addLayout(self.target_row)
        self.on_breakpoint_kind()

        self.flow_view_combo = QtWidgets.QComboBox()
        for key, view_title in flow_view.views():
            self.flow_view_combo.addItem(
                self.t("debugger_view_" + key, view_title), key)
        self.flow_view_combo.currentIndexChanged.connect(
            self.refresh_flow)
        self.btn_flow_save = QtWidgets.QPushButton(
            self.t("debugger_flow_save", "Save views"))
        self.btn_flow_save.clicked.connect(self.save_flow_views)

        self.flow_label = QtWidgets.QLabel(
            self.t("debugger_flow", "Code flow:"))
        self.btn_record = QtWidgets.QPushButton(
            self.t("debugger_record", "Record"))
        self.btn_record.clicked.connect(self.record_flow_now)
        self.flow_filter_combo = QtWidgets.QComboBox()
        self.flow_filter_combo.addItem(self.t("debugger_flow_all",
                                             "all kinds"), "")
        for kind in FLOW_FILTER_KINDS:
            self.flow_filter_combo.addItem(
                self.t("debugger_flow_" + kind, kind), kind)
        self.flow_filter_combo.currentIndexChanged.connect(
            self.refresh_flow)
        self.flow_summary_label = QtWidgets.QLabel("")
        self.flow_summary_label.setStyleSheet("color: #9aa0a6;")

        # ---- language row: the multilingual engine -----------------------
        language_row = QtWidgets.QHBoxLayout()
        language_row.addWidget(QtWidgets.QLabel(
            self.t("debugger_language", "Language:")))
        self.language_combo = QtWidgets.QComboBox()
        for key, label in debug_langs.languages():
            self.language_combo.addItem(label, key)
        self.language_combo.currentIndexChanged.connect(
            self.on_language_changed)
        language_row.addWidget(self.language_combo)

        language_row.addWidget(self.btn_resolve)
        language_row.addWidget(self.btn_remove_bp_row)

        self.btn_validate = QtWidgets.QToolButton()
        self.btn_validate.setText(self.t("debugger_validate", "Validate"))
        self.btn_validate.setPopupMode(QtWidgets.QToolButton.MenuButtonPopup)
        self.validate_menu = QtWidgets.QMenu(self.btn_validate)
        self.act_validate_selected = self.validate_menu.addAction(
            self.t("debugger_validate", "Validate"))
        self.act_validate_all = self.validate_menu.addAction(
            self.t("debugger_validate_all", "Validate all"))
        self.act_validate_selected.triggered.connect(self.validate_selected)
        self.act_validate_all.triggered.connect(self.validate_all)
        self.btn_validate.setMenu(self.validate_menu)
        self.btn_validate.clicked.connect(self.btn_validate.showMenu)
        language_row.addWidget(self.flow_label)
        language_row.addWidget(self.btn_record)
        language_row.addWidget(self.flow_filter_combo)
        language_row.addWidget(self.flow_summary_label, 1)

        self.flow_view_label = QtWidgets.QLabel(
            self.t("debugger_flow_view", "View:"))
        language_row.addWidget(self.flow_view_label)
        language_row.addWidget(self.flow_view_combo)
        language_row.addWidget(self.btn_flow_save)

        self.btn_overview = QtWidgets.QPushButton(
            self.t("debugger_overview", "Debug overview"))
        self.btn_overview.clicked.connect(self.show_overview)
        language_row.addWidget(self.btn_overview)
        language_row.addWidget(self.btn_validate)

        language_row.addStretch()
        layout.addLayout(language_row)

        # ---- watch row ---------------------------------------------------
        watch_row = QtWidgets.QHBoxLayout()
        watch_row.addWidget(QtWidgets.QLabel(
            self.t("debugger_watch", "Watch:")))
        self.watch_edit = QtWidgets.QLineEdit()
        self.watch_edit.setPlaceholderText(
            self.t("debugger_watch_hint", "Expression to watch..."))
        self.watch_edit.returnPressed.connect(self.add_watch)
        watch_row.addWidget(self.watch_edit, 3)
        self.btn_add_watch = QtWidgets.QPushButton(
            self.t("debugger_add_watch", "Add watch"))
        self.btn_add_watch.clicked.connect(self.add_watch)
        self.btn_del_watch = QtWidgets.QPushButton(
            self.t("debugger_del_watch", "Remove watch"))
        self.btn_del_watch.clicked.connect(self.remove_watch)
        watch_row.addWidget(self.btn_add_watch)
        watch_row.addWidget(self.btn_del_watch)
        layout.addLayout(watch_row)

        self.snapshots_label = QtWidgets.QLabel(
            self.t("debugger_snapshots", "Snapshots:"))
        self.btn_snapshot = QtWidgets.QPushButton(
            self.t("debugger_snapshot", "Take"))
        self.btn_snapshot.clicked.connect(self.take_snapshot)
        self.btn_restore = QtWidgets.QPushButton(
            self.t("debugger_restore", "Restore"))
        self.btn_restore.clicked.connect(self.restore_snapshot)
        self.btn_snapshot_diff = QtWidgets.QPushButton(
            self.t("debugger_snapshot_compare", "Compare"))
        self.btn_snapshot_diff.clicked.connect(self.snapshot_diff)
        self.replay_label = QtWidgets.QLabel(
            self.t("debugger_replay_label", "Replay:"))
        self.replay_combo = QtWidgets.QComboBox()
        for mode, label_key, fallback, _meaning in debug_steps.REPLAY_MODES:
            self.replay_combo.addItem(self.t(label_key, fallback), mode)
        self.btn_replay = QtWidgets.QPushButton(
            self.t("debugger_replay", "Replay"))
        self.btn_replay.clicked.connect(
            lambda _checked=False: self.replay_session(
                self.replay_combo.currentData() or "transcript"))

        # ---- split: breakpoints | stack + watches / debug overview --------
        # The detail views share a wide column. Keeping them stacked gives the
        # tree and the text panels enough width without wasting the tab height.
        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)

        bp_side = QtWidgets.QWidget()
        bp_layout = QtWidgets.QVBoxLayout(bp_side)
        bp_layout.addWidget(QtWidgets.QLabel(
            self.t("debugger_breakpoints", "Breakpoints")))
        self.bp_tree = QtWidgets.QTreeWidget()
        self.bp_tree.setColumnCount(len(BREAKPOINT_COLUMNS))
        self.bp_tree.setHeaderLabels(self._breakpoint_headers())
        self.bp_tree.setAlternatingRowColors(True)
        self.bp_tree.itemSelectionChanged.connect(self.on_breakpoint_selected)
        self.bp_tree.itemDoubleClicked.connect(
            lambda *_: self.toggle_selected_breakpoint())
        bp_layout.addWidget(self.bp_tree, 1)
        bp_actions = QtWidgets.QHBoxLayout()
        self.btn_toggle_bp = QtWidgets.QPushButton(
            self.t("debugger_toggle_bp", "Toggle"))
        self.btn_toggle_bp.clicked.connect(self.toggle_selected_breakpoint)
        self.btn_remove_bp = QtWidgets.QPushButton(
            self.t("debugger_remove_bp", "Remove breakpoint"))
        self.btn_remove_bp.clicked.connect(self.remove_selected_breakpoint)
        bp_actions.addWidget(self.btn_toggle_bp)
        bp_actions.addWidget(self.btn_remove_bp)
        bp_actions.addStretch()
        bp_layout.addLayout(bp_actions)
        splitter.addWidget(bp_side)

        stack_side = QtWidgets.QWidget()
        stack_layout = QtWidgets.QVBoxLayout(stack_side)
        stack_layout.addWidget(QtWidgets.QLabel(
            self.t("debugger_stack", "Call stack")))
        self.stack_view = QtWidgets.QPlainTextEdit()
        self.stack_view.setReadOnly(True)
        self.stack_view.setFont(QFont(DEBUGGER_FONT, DEBUGGER_FONT_SIZE))
        stack_layout.addWidget(self.stack_view, 1)

        stack_layout.addWidget(QtWidgets.QLabel(
            self.t("debugger_watches", "Watches")))
        self.watch_view = QtWidgets.QPlainTextEdit()
        self.watch_view.setReadOnly(True)
        self.watch_view.setFont(QFont(DEBUGGER_FONT, DEBUGGER_FONT_SIZE))
        self.watch_view.setMaximumHeight(120)
        stack_layout.addWidget(self.watch_view)
        splitter.addWidget(stack_side)

        overview_side = QtWidgets.QWidget()
        overview_layout = QtWidgets.QVBoxLayout(overview_side)
        overview_layout.addWidget(QtWidgets.QLabel(
            self.t("debugger_overview_title", "Debug overview")))
        self.overview_view = QtWidgets.QPlainTextEdit()
        self.overview_view.setReadOnly(True)
        self.overview_view.setFont(QFont(DEBUGGER_FONT, DEBUGGER_FONT_SIZE))
        overview_layout.addWidget(self.overview_view, 1)

        detail_splitter = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        detail_splitter.addWidget(stack_side)
        detail_splitter.addWidget(overview_side)
        detail_splitter.setStretchFactor(0, 1)
        detail_splitter.setStretchFactor(1, 2)
        splitter.addWidget(detail_splitter)

        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        layout.addWidget(splitter, 1)

        # ---- stepping ----------------------------------------------------
        # The stepping controls sit under the breakpoint row, because a session
        # is driven by the breakpoints it stops on: a user sets them, starts a
        # session and steps through what the recording holds.
        step_row = QtWidgets.QHBoxLayout()
        step_row.addWidget(QtWidgets.QLabel(
            self.t("debugger_stepping", "Session:")))
        self.btn_start_session = QtWidgets.QPushButton(
            self.t("debugger_start_session", "Start session"))
        self.btn_start_session.clicked.connect(self.start_session)
        step_row.addWidget(self.btn_start_session)
        self.step_buttons = {}
        self.btn_step_modes = QtWidgets.QToolButton()
        self.btn_step_modes.setText(self.t("debugger_step_menu", "Step..."))
        self.btn_step_modes.setPopupMode(QtWidgets.QToolButton.MenuButtonPopup)
        self.step_menu = QtWidgets.QMenu(self.btn_step_modes)
        step_modes = ("over", "into", "out")
        step_row.addWidget(self.btn_step_modes)
        for mode, label_key, fallback, _meaning in debug_steps.STEP_MODES:
            handler = (self.step_to_line if mode == "line"
                       else (self.run_to_breakpoint if mode == "breakpoint"
                             else (lambda _checked=False, m=mode: self.step(m))))
            if mode in step_modes:
                action = self.step_menu.addAction(
                    self.t(label_key, fallback))
                action.triggered.connect(handler)
                self.step_buttons[mode] = action
            else:
                button = QtWidgets.QPushButton(self.t(label_key, fallback))
                button.clicked.connect(handler)
                self.step_buttons[mode] = button
                step_row.addWidget(button)
        self.btn_step_modes.setMenu(self.step_menu)
        self.btn_step_modes.clicked.connect(lambda: self.step("over"))
        self.btn_rewind = QtWidgets.QPushButton(
            self.t("debugger_rewind", "Rewind"))
        self.btn_rewind.clicked.connect(self.rewind_session)
        step_row.addWidget(self.btn_rewind)
        step_row.addWidget(self.snapshots_label)
        step_row.addWidget(self.btn_snapshot)
        step_row.addWidget(self.btn_restore)
        step_row.addWidget(self.btn_snapshot_diff)
        step_row.addWidget(self.replay_label)
        step_row.addWidget(self.replay_combo)
        step_row.addWidget(self.btn_replay)
        step_row.addStretch()
        layout.addLayout(step_row)

        # ---- the stepping pane -------------------------------------------
        self.step_view = QtWidgets.QPlainTextEdit()
        self.step_view.setReadOnly(True)
        self.step_view.setFont(QFont(DEBUGGER_FONT, DEBUGGER_FONT_SIZE))
        self.step_view.setMaximumHeight(180)
        self.step_view.setPlainText(self.t(
            "debugger_step_hint",
            "(start a session to step through the script)"))
        layout.addWidget(self.step_view)

        # ---- trace log ---------------------------------------------------
        # ---- trace log ---------------------------------------------------
        trace_row = QtWidgets.QHBoxLayout()
        trace_row.addWidget(QtWidgets.QLabel(
            self.t("debugger_trace", "Trace log:")))
        self.trace_buttons = {}
        for action, label_key, fallback in TRACE_ACTIONS:
            button = QtWidgets.QPushButton(self.t(label_key, fallback))
            button.clicked.connect(
                lambda _checked=False, a=action: self.log_trace(a))
            self.trace_buttons[action] = button
            trace_row.addWidget(button)
        self.trace_edit = QtWidgets.QLineEdit()
        self.trace_edit.setPlaceholderText(
            self.t("debugger_trace_hint", "Note..."))
        trace_row.addWidget(self.trace_edit, 2)
        trace_row.addStretch()
        layout.addLayout(trace_row)

        self.flow_view = QtWidgets.QPlainTextEdit()
        self.flow_view.setReadOnly(True)
        self.flow_view.setFont(QFont(DEBUGGER_FONT, DEBUGGER_FONT_SIZE))
        self.flow_view.setMaximumHeight(200)
        layout.addWidget(self.flow_view)

        self.trace_view = QtWidgets.QPlainTextEdit()
        self.trace_view.setReadOnly(True)
        self.trace_view.setFont(QFont(DEBUGGER_FONT, DEBUGGER_FONT_SIZE))
        self.trace_view.setMaximumHeight(160)
        layout.addWidget(self.trace_view)

        self.status = QtWidgets.QLabel("")
        self.status.setStyleSheet("color: #9aa0a6;")
        layout.addWidget(self.status)

    # ---------------------------------------------------------- repository
    def refresh(self):
        """Reload the script list and repaint everything."""
        self._reload_scripts()
        self.refresh_languages()
        self.refresh_breakpoints()
        self.refresh_stack()
        self.refresh_watches()
        self.refresh_trace()
        self.refresh_flow()
        self.refresh_summary()
        self.status.setText(self.t("debugger_ready", "debugger ready"))

    def _reload_scripts(self):
        """Fill the script picker with the names from the database."""
        combo = getattr(self, "script_combo", None)
        if combo is None:
            return
        current = combo.currentText()
        combo.blockSignals(True)
        combo.clear()
        for script in self._scripts():
            combo.addItem(script.get("name", "(unnamed)"))
        index = combo.findText(current)
        if index >= 0:
            combo.setCurrentIndex(index)
        combo.blockSignals(False)

    def on_script_changed(self, *_):
        """Recompute the stack preview when the script changes."""
        self.refresh_stack()
        self._sync_language_to_script()

    def on_language_changed(self, *_):
        """Recompute the frames when the user picks another language."""
        self.refresh_stack()
        adapter = self.selected_language()
        self.status.setText(self.t(
            "debugger_language_set", "language: {name}").format(
            name=adapter.label))

    # -------------------------------------------------------------- engines
    def selected_language(self):
        """The adapter selected in the language picker (never None)."""
        key = self.language_combo.currentData() if hasattr(
            self, "language_combo") else ""
        return debug_langs.get(key) or debug_langs.adapter_for("")

    def refresh_languages(self):
        """Rebuild the language picker from the engine registry.

        The registry is hot-pluggable, so a language registered after the tab
        was built shows up here on the next refresh. The current choice is kept
        when it still exists, and the keys now listed are returned.
        """
        combo = getattr(self, "language_combo", None)
        if combo is None:
            return []
        current = combo.currentData()
        previous = self.session.engines
        combo.blockSignals(True)
        combo.clear()
        registered = []
        for key, label in debug_langs.languages():
            combo.addItem(label, key)
            registered.append(key)
        wanted = current or (previous[0] if previous else "")
        index = combo.findData(wanted)
        combo.setCurrentIndex(index if index >= 0 else 0)
        combo.blockSignals(False)
        self.session.set_engines([combo.currentData()])
        return registered

    def _sync_language_to_script(self):
        """Point the picker at the selected script language.

        The engine resolves the script (category first, file name second), then
        the picker follows it, so the overview and the frames agree with the
        language the script is written in without the user choosing anything.
        """
        combo = getattr(self, "language_combo", None)
        script = self._current_script()
        if combo is None or script is None:
            return None
        adapter = adapter_for_script(script)
        index = combo.findData(adapter.key)
        if index < 0:
            self.refresh_languages()
            index = combo.findData(adapter.key)
        if index >= 0 and index != combo.currentIndex():
            combo.blockSignals(True)
            combo.setCurrentIndex(index)
            combo.blockSignals(False)
            self.session.set_engines([adapter.key])
            self.session.save()
        return adapter.key

    def current_script_language(self):
        """The adapter the selected script is written in."""
        return adapter_for_script(self._current_script()).key

    # ---------------------------------------------------------- code flow
    def record_flow_now(self):
        """Record the full execution timeline of the selection.

        The recording holds every step, block, call, method call, import,
        variable change, exception, error, warning and log statement the
        language adapter can see, in the order they would happen. It is
        stored in the session, so a recording survives a restart.
        """
        script = self._current_script()
        if script is None:
            self.status.setText(self.t("debugger_no_script", """(no script selected)"""))
            return None
        recording = record_flow(script)
        self.session.set_flow(recording)
        self.session.save()
        self.refresh_flow()
        self.refresh_summary()
        self.status.setText(self.t(
            "debugger_flow_recorded",
            "recorded {count} event(s) in {language}").format(
                count=len(recording), language=recording.label))
        notifier = getattr(self.window, "notify_audit", None)
        if callable(notifier):
            try:
                notifier("debugger_record", where=recording.script,
                         detail="%s (%d event(s))" % (recording.language,
                                                       len(recording)))
            except Exception:
                pass
        return recording

    def flow_kind_filter(self):
        """The flow kind the filter shows (empty means every kind)."""
        combo = getattr(self, "flow_filter_combo", None)
        return combo.currentData() if combo is not None else ""

    def refresh_flow(self, *_):
        """Repaint the timeline pane with the current filter."""
        view = getattr(self, "flow_view", None)
        if view is None:
            return []
        recording = self.current_recording()
        if recording is None:
            view.setPlainText(self.t("debugger_flow_empty",
                                     "(no recording)"))
            self._set_flow_summary(None)
            return []
        # The view picker selects one of the eight ASCII pictures of the run;
        # the kind filter narrows the timeline view to a single event kind.
        key = self.flow_view_key()
        kind = self.flow_kind_filter()
        text = flow_view.render(recording, key, filter_kind=kind)
        lines = text[:DEBUGGER_MAX_FLOW_LINES]
        if len(text) > len(lines):
            lines.append(self.t(
                "debugger_flow_more",
                "... and {count} more line(s)").format(
                    count=len(text) - len(lines)))
        view.setPlainText(chr(10).join(lines) if lines else self.t(
            "debugger_flow_none",
            "(no events of this kind)"))
        self._set_flow_summary(recording, len(recording.for_kind(kind)))
        return lines

    def _set_flow_summary(self, recording, shown=None):
        label = getattr(self, "flow_summary_label", None)
        if label is None:
            return
        if recording is None:
            label.setText("")
            return
        counts = recording.counts()
        total = len(recording)
        parts = ["%d of %d" % (shown, total)
                 if shown is not None else "%d" % total]
        for kind in FLOW_FILTER_KINDS:
            if counts.get(kind):
                parts.append("%s=%d" % (kind, counts[kind]))
        label.setText(" | ".join(parts))

    def current_recording(self):
        """The remembered recording, rebuilt from the session."""
        if not self.session.flow:
            return None
        try:
            return code_flow.recording_from_dict(self.session.flow[-1])
        except Exception:
            return None

    def save_flow_report(self, path=None):
        """Write the recording to disk as an ASCII report."""
        recording = self.current_recording()
        if recording is None:
            self.status.setText(self.t("debugger_flow_empty", "(no recording)"))
            return ""
        path = path or os.path.join(self._base_dir(),
                                    "code_flow_report.txt")
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(code_flow.format_report(recording))
        except OSError as exc:
            self.status.setText(str(exc))
            return ""
        self.status.setText(self.t(
            "debugger_flow_saved",
            "flow report written: {path}").format(path=path))
        return path

    # ---------------------------------------------------------- visualizer
    def flow_view_key(self):
        """The view key the picker is showing."""
        combo = getattr(self, "flow_view_combo", None)
        return combo.currentData() if combo is not None else "timeline"

    def flow_views_text(self, key=None):
        """The ASCII picture of one view of the recording."""
        recording = self.current_recording()
        if recording is None:
            return [self.t("debugger_flow_empty", "(no recording)")]
        return flow_view.render(recording, key or self.flow_view_key())

    def show_flow_view(self, key=None):
        """Render the selected view into the timeline pane."""
        view = getattr(self, "flow_view", None)
        if view is None:
            return []
        lines = self.flow_views_text(key)
        view.setPlainText(chr(10).join(lines))
        return lines

    def save_flow_views(self, path=None):
        """Write every view to disk as one ASCII document."""
        recording = self.current_recording()
        if recording is None:
            self.status.setText(self.t("debugger_flow_empty",
                                       "(no recording)"))
            return ""
        path = path or os.path.join(self._base_dir(),
                                    "code_flow_views.txt")
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(chr(10).join(
                    flow_view.render_all(recording)))
        except OSError as exc:
            self.status.setText(str(exc))
            return ""
        self.status.setText(self.t("debugger_flow_views_saved",
            "views written: {path}").format(path=path))
        return path

    # ------------------------------------------------------------- stepping
    def start_session(self):
        """Record the selected script's timeline and open a session over it.

        The recording is what the stepping walks, so this is the one call that
        turns "a prepared session" into "a session that can be stepped".
        """
        script = self._current_script()
        if script is None:
            self.status.setText(self.t("debugger_no_script",
                                       "(no script selected)"))
            return None
        recording = record_flow(script)
        self.session.set_flow(recording)
        self._engine = debug_steps.SessionRunner(
            events=recording.events,
            breakpoints=self.session.breakpoints,
            watches=self.session.watches)
        self.session.log("start", "session over %s" % script.get("name", ""))
        self.session.save()
        self.refresh_stepping()
        self.refresh_watches()
        self.refresh_summary()
        self.status.setText(self.t(
            "debugger_session_started",
            "session over {name}: {events} event(s)").format(
                name=script.get("name", ""), events=len(recording.events)))
        self.notify_session("start")
        return self._engine

    def _runner(self):
        """The live session runner (or None when no session was started)."""
        return getattr(self, "_engine", None)

    def step(self, mode="over", line=None):
        """Move the session one step in `mode` and repaint the view."""
        runner = self._runner()
        if runner is None:
            runner = self.start_session()
            if runner is None:
                return None
        event = runner.step(mode, line=line)
        if event is None:
            self.status.setText(self.t(
                "debugger_step_refused",
                "cannot step {mode} from here").format(mode=mode))
            return None
        self.session.log("step", "%s -> line %s" % (mode,
                                                    runner.position()["line"]))
        self.session.save()
        self.refresh_stepping()
        self.refresh_watches()
        self.refresh_stack()
        self.status.setText(self.t(
            "debugger_stepped",
            "step {mode}: line {line}, depth {depth}").format(
                mode=mode, line=runner.position()["line"],
                depth=runner.position()["depth"]))
        notify_debug_step(runner, mode)
        return event

    def step_to_line(self):
        """Step to the line in the line box."""
        wanted = _as_int(self.line_edit.text())
        if wanted is None:
            self.status.setText(self.t("debugger_bad_line",
                                       "line must be a number"))
            return None
        return self.step("line", line=wanted)

    def run_to_breakpoint(self):
        """Advance to a breakpoint, restarting the search if need be.

        The search runs forward first, which is the honest reading of
        "continue". When there is nothing ahead but a stop does exist earlier in
        the timeline, the session is rewound and the search runs again: that is
        the case a user hits constantly (step a few times, set a breakpoint,
        press Run) and silently doing nothing would be the worst answer. The
        rewind is reported in the status line so it is never a surprise.
        """
        runner = self._runner() or self.start_session()
        if runner is None:
            return None
        # The breakpoint list is refreshed from the session first: one added
        # after the session started would otherwise be invisible to the runner.
        runner.breakpoints = list(self.session.breakpoints)
        runner.watches = list(self.session.watches)

        rewound = False
        event = runner.run_to_breakpoint(limit=10000)
        if event is None and runner.any_breakpoint_reachable():
            # Nothing ahead, but a stop exists behind: restart the search.
            event = runner.run_to_breakpoint_from_start(limit=10000)
            rewound = True
        if event is None:
            self.status.setText(self.t("debugger_no_stop",
                                       "no breakpoint to stop on"))
            return None

        self.session.log("continue", "line %s%s"
                         % (runner.position()["line"],
                            " (rewound)" if rewound else ""))
        self.session.save()
        self.refresh_stepping()
        self.refresh_watches()
        self.refresh_stack()
        if rewound:
            self.status.setText(self.t(
                "debugger_stopped_rewound",
                "rewound and stopped at line {line}").format(
                    line=runner.position()["line"]))
        else:
            self.status.setText(self.t(
                "debugger_stopped", "stopped at line {line}").format(
                    line=runner.position()["line"]))
        notify_debug_step(runner, "breakpoint")
        return event


    def rewind_session(self):
        """Return the session to the start of the recording."""
        runner = self._runner()
        if runner is None:
            return None
        runner.rewind()
        self.session.save()
        self.refresh_stepping()
        self.refresh_watches()
        self.status.setText(self.t("debugger_rewound", "session rewound"))
        return runner

    def refresh_stepping(self):
        """Repaint the stepping pane from the session runner."""
        view = getattr(self, "step_view", None)
        if view is None:
            return []
        runner = self._runner()
        lines = debug_steps.format_stepping(runner)
        if runner is not None:
            lines.append("")
            lines.append(self.t("debugger_transcript", "Transcript"))
            lines.append("-" * 10)
            lines += runner.transcript_lines()[-12:]
        view.setPlainText("\n".join(lines))
        return lines

    # ------------------------------------------------------------ snapshots
    def take_snapshot(self):
        """Freeze the session where it is."""
        runner = self._runner()
        if runner is None:
            self.status.setText(self.t("debugger_no_session",
                                       "start a session first"))
            return None
        label = self.trace_edit.text().strip() or "snap%d" % (
            len(runner.snapshots) + 1)
        item = runner.snapshot(label)
        self.session.log("snapshot", label)
        self.session.save()
        self.refresh_stepping()
        self.status.setText(self.t(
            "debugger_snapshot_taken",
            "snapshot '{label}' at line {line}").format(
                label=item.label, line=item.line))
        return item

    def restore_snapshot(self):
        """Return the session to a named snapshot (or the last one)."""
        runner = self._runner()
        if runner is None or not runner.snapshots:
            self.status.setText(self.t("debugger_no_snapshot",
                                       "no snapshot to restore"))
            return None
        label = self.trace_edit.text().strip()
        item = runner.snapshot_by_label(label) if label else runner.snapshots[-1]
        if item is None:
            item = runner.snapshots[-1]
        runner.restore(item)
        self.session.save()
        self.refresh_stepping()
        self.refresh_watches()
        self.status.setText(self.t(
            "debugger_snapshot_restored",
            "restored '{label}' (line {line})").format(
                label=item.label, line=item.line))
        return item

    def snapshot_diff(self):
        """Compare the last two snapshots and show the difference."""
        runner = self._runner()
        if runner is None or len(runner.snapshots) < 2:
            self.status.setText(self.t(
                "debugger_need_two_snapshots",
                "two snapshots are needed to compare"))
            return []
        before, after = runner.snapshots[-2], runner.snapshots[-1]
        moved, hit_changes, watch_changes = debug_steps.diff_snapshots(
            before, after)
        lines = [
            self.t("debugger_snapshot_diff", "Snapshot diff"),
            "-" * 13,
            "%s -> %s" % (before.label, after.label),
            "moved lines : %s -> %s" % (moved["from_line"], moved["line"]),
            "moved depth : %s -> %s" % (moved["from_depth"], moved["depth"]),
            "moved steps : %s" % moved["index"],
            "at kind     : %s" % (moved["kind"] or "-"),
        ]
        for key, change in sorted(hit_changes.items()):
            lines.append("hits %-20s %s -> %s" % (key, change["from"],
                                                  change["to"]))
        for key, change in sorted(watch_changes.items()):
            lines.append("watch %-19s %s -> %s" % (key, change["from"],
                                                   change["to"]))
        view = getattr(self, "step_view", None)
        if view is not None:
            view.setPlainText("\n".join(lines))
        self.status.setText(self.t("debugger_snapshot_diff_done",
                                   "snapshot diff ready"))
        return lines

    def replay_session(self, mode="transcript"):
        """Replay the session and show the frames."""
        runner = self._runner()
        if runner is None:
            self.status.setText(self.t("debugger_no_session",
                                       "start a session first"))
            return []
        frames = debug_steps.replay(runner, mode, limit=400)
        lines = debug_steps.format_replay(frames, limit=60)
        view = getattr(self, "step_view", None)
        if view is not None:
            view.setPlainText("\n".join(lines))
        self.status.setText(self.t(
            "debugger_replayed", "replayed {count} frame(s)").format(
                count=len(frames)))
        return frames

    def notify_session(self, action):
        """Announce a session event on the shared audit bus."""
        notifier = getattr(self.window, "notify_audit", None)
        if not callable(notifier):
            return 0
        try:
            return notifier("debugger_%s" % action,
                            where=(self._current_script() or {}).get("name", ""),
                            detail=action)
        except Exception:
            return 0

    # ----------------------------------------------------------- validation
    def validate_selected(self):
        """Validate the selected script in its own language.

        The result is written into the overview pane and recorded in the
        session, so a validation survives a restart and shows up in the summary
        like any other session action.
        """
        script = self._current_script()
        if script is None:
            self.status.setText(self.t("debugger_no_script",
                                       "(no script selected)"))
            return None
        result = validate_script(script)
        self.session.record_validation(script.get("name", ""), result)
        self.session.save()
        self.show_validation(script, result)
        self.refresh_summary()
        self.status.setText(self._validation_status(result))
        return result

    def validate_all(self):
        """Validate every script, each in the language of its adapter.

        A script the engine cannot validate still gets a record (with the
        reason), so the summary never silently skips a script.
        """
        results = {}
        failed = 0
        for script in self._scripts():
            name = script.get("name", "")
            result = validate_script(script)
            self.session.record_validation(name, result)
            results[name] = result
            if not result.get("ok"):
                failed += 1
        self.session.save()
        self.refresh_summary()
        self.show_validation_summary(results)
        self.status.setText(self.t(
            "debugger_validated_all",
            "validated {count} script(s): {failed} with problem(s)").format(
                count=len(results), failed=failed))
        return results

    def _validation_status(self, result):
        """A one-line status for a validation result."""
        if result.get("ok"):
            return self.t("debugger_valid_ok", "valid: {summary}").format(
                summary=result.get("summary", ""))
        return self.t("debugger_valid_failed", "invalid: {error}").format(
            error=result.get("error", ""))

    def show_validation(self, script, result):
        """Render one script's validation into the overview pane."""
        view = getattr(self, "overview_view", None)
        if view is None:
            return []
        adapter = adapter_for_script(script)
        lines = [
            self.t("debugger_overview_title", "Debug overview"),
            "-" * 14,
            "%s %s" % (self.t("debugger_col_script", "Script"),
                       script.get("name", "")),
            "%s %s [%s]" % (self.t("debugger_language", "Language:"),
                            adapter.label, adapter.key),
            "%s %s" % (self.t("debugger_validation", "Validation"),
                       "ok" if result.get("ok") else "failed"),
            "%s %s" % (self.t("debugger_summary_col", "Summary"),
                       result.get("summary", "")),
        ]
        if not result.get("ok"):
            lines.append("%s %s" % (self.t("debugger_error", "Error"),
                                    result.get("error", "")))
            if result.get("line"):
                lines.append("%s line %s, column %s" % (
                    self.t("debugger_at", "At"), result.get("line"),
                    result.get("column")))
        counts = result.get("counts") or {}
        if counts:
            lines.append("%s %s" % (
                self.t("debugger_counts", "Counts"), ", ".join(
                    "%s=%s" % (key, value)
                    for key, value in sorted(counts.items()))))
        lines += [""] + build_debug_overview(script)
        view.setPlainText("\n".join(lines))
        return lines

    def show_validation_summary(self, results):
        """Render the result of validating the whole database."""
        view = getattr(self, "overview_view", None)
        if view is None:
            return []
        failed = sum(1 for record in results.values()
                     if not record.get("ok"))
        lines = [self.t(
            "debugger_validated_all",
            "validated {count} script(s): {failed} with problem(s)").format(
                count=len(results), failed=failed),
            "-" * 14]
        for name in sorted(results):
            result = results[name]
            mark = "ok    " if result.get("ok") else "FAILED"
            lines.append("%-7s %-28s %-14s %s" % (
                mark, name, result.get("language", ""),
                result.get("summary", "") or result.get("error", "")))
        view.setPlainText("\n".join(lines))
        return lines

    def show_overview(self):
        """Recompute and show what the engine can see in the selection."""
        script = self._current_script()
        if script is None:
            self.status.setText(self.t("debugger_no_script",
                                       "(no script selected)"))
            return ""
        result = validate_script(script)
        self.show_validation(script, result)
        self.status.setText(self.t("debugger_overview_ready",
                                   "overview ready"))
        return build_debug_overview(script)

    def script_overview(self):
        """The debug overview of the selection as data."""
        return describe_script(self._current_script())

    # ------------------------------------------------------------- summary
    def refresh_summary(self):
        info = self.session.summary()
        self.summary_label.setText(self.t(
            "debugger_summary",
            "{breakpoints} breakpoint(s) ({breakpoints_enabled} on) | "
            "{watches} watch(es) | {trace} log entry(ies)").format(**info))
    # -------------------------------------------------------- breakpoints
    def _breakpoint_headers(self):
        """The breakpoint tree headers, in the model's order."""
        return [self.t("debugger_col_" + column,
                       column.title())
                for column in BREAKPOINT_COLUMNS]

    def on_breakpoint_kind(self, *_):
        """Show only the inputs the selected kind needs."""
        kind = breakpoints.normalize_kind(
            self.kind_combo.currentData())
        needs_target = kind in breakpoints.TARGETED_KINDS
        needs_line = kind in breakpoints.LINED_KINDS
        is_block = kind == "block"
        # The free-text target serves every targeted kind except
        # `block`, whose values are a closed set, so it gets the pick.
        self.target_edit.setVisible(needs_target and not is_block)
        self.block_combo.setVisible(is_block)
        self.target_label.setVisible(needs_target)
        if needs_target and not is_block:
            self.target_edit.setPlaceholderText(
                self.t("debugger_target_hint",
                       "Name to stop on..."))
        hint = "" if needs_line else self.t(
            "debugger_line_ignored",
            "this kind finds its own line")
        self.line_edit.setToolTip(hint)
        return kind

    def breakpoint_target(self):
        """The target the current controls describe."""
        kind = breakpoints.normalize_kind(
            self.kind_combo.currentData())
        if kind == "block":
            return self.block_combo.currentData() or ""
        if kind in breakpoints.TARGETED_KINDS:
            return self.target_edit.text().strip()
        return ""

    def add_breakpoint(self):
        """Add the breakpoint the controls describe."""
        script = self._current_script()
        name = script.get("name", "(current)") if script else "(current)"
        record = self.session.add_breakpoint(
            name, self.line_edit.text(),
            self.kind_combo.currentData(),
            self.condition_edit.text(),
            language=self.selected_language().key,
            target=self.breakpoint_target())
        if record is None:
            self.status.setText(self.t(
                "debugger_bad_line",
                "line must be a number"))
            return None
        ok, problems = breakpoints.validate(record)
        if script is not None:
            # Resolve it at once, so the row shows where it lands.
            self._resolve_record(record, script)
        self.session.save()
        self.refresh_breakpoints()
        self.refresh_stack()
        self.refresh_summary()
        if not ok:
            self.status.setText(self.t(
                "debugger_bp_incomplete",
                "breakpoint incomplete: {problems}").format(
                    problems="; ".join(problems)))
        else:
            self.status.setText(self.t(
                "debugger_bp_added",
                "breakpoint at {script}:{line}").format(
                    script=record["script"],
                    line=record["line"]))
        self.notify_breakpoint(record)
        return record

    def _resolve_record(self, record, script=None):
        """Resolve the lines a breakpoint lands on."""
        script = script if script is not None else self._current_script()
        if script is None:
            return []
        return breakpoints.resolve(record, script)

    def resolve_breakpoint(self):
        """Resolve the selected breakpoint, and report it."""
        record = self._selected_breakpoint()
        if record is None:
            self.status.setText(self.t(
                "debugger_bp_none",
                "(no breakpoint selected)"))
            return []
        found = self._resolve_record(record)
        self.session.save()
        self.refresh_breakpoints()
        self.refresh_summary()
        if not found:
            self.status.setText(self.t(
                "debugger_bp_unresolved",
                "resolves to nothing here"))
            return []
        self.status.setText(self.t(
            "debugger_bp_resolved",
            "resolves to {count} place(s): {lines}").format(
                count=len(found),
                lines=", ".join(
                    "line %d" % item[0]
                    for item in found[:6])))
        return found

    def notify_breakpoint(self, record):
        """Announce a breakpoint on the shared audit bus."""
        notifier = getattr(self.window, "notify_audit", None)
        if not callable(notifier):
            return 0
        try:
            return notifier("debugger_breakpoint",
                            where=record.get("script", ""),
                            detail=breakpoints.describe(record))
        except Exception:
            return 0

    def refresh_breakpoints(self):
        """Repaint the breakpoint tree from the session records.

        Every column comes from the eight-kind model, so a function or a
        variable breakpoint shows its target, and a line / conditional
        breakpoint shows its line. An incomplete record is drawn in a
        warning colour with its problem in the tooltip, rather than being
        hidden.
        """
        tree = getattr(self, "bp_tree", None)
        if tree is None:
            return
        tree.blockSignals(True)
        tree.clear()
        for bp in self.session.breakpoints:
            enabled = bp.get("enabled", True)
            kind = breakpoints.normalize_kind(bp.get("kind"))
            line = bp.get("line", 0)
            item = QtWidgets.QTreeWidgetItem([
                bp.get("script", ""),
                self.t("debugger_bp_" + kind, kind),
                bp.get("target", "") or "-",
                ("line %d" % line) if line else "-",
                bp.get("condition", "") or "-",
            ])
            item.setData(0, QtCore.Qt.UserRole, bp)
            ok, problems = breakpoints.validate(bp)
            tooltip = breakpoints.describe(bp)
            if not ok:
                tooltip = "%s\n%s" % (tooltip, "; ".join(problems))
            resolved = bp.get("resolved") or []
            if resolved:
                tooltip = "%s\n%s" % (
                    tooltip, ", ".join(
                        "line %d" % entry.get("line", 0)
                        for entry in resolved[:8]))
            for column in range(tree.columnCount()):
                item.setToolTip(column, tooltip)
            if not enabled:
                for column in range(tree.columnCount()):
                    item.setForeground(column, QtGui.QColor("#7f848e"))
            elif not ok:
                for column in range(tree.columnCount()):
                    item.setForeground(column, QtGui.QColor("#f7c66d"))
            tree.addTopLevelItem(item)
        for column in range(tree.columnCount()):
            tree.resizeColumnToContents(column)
        tree.blockSignals(False)

    def _selected_breakpoint(self):
        item = self.bp_tree.currentItem() if hasattr(self, "bp_tree") else None
        return item.data(0, QtCore.Qt.UserRole) if item is not None else None

    def toggle_selected_breakpoint(self):
        bp = self._selected_breakpoint()
        if bp is None:
            return None
        state = self.session.toggle_breakpoint(
            bp.get("script"), bp.get("line"), bp.get("kind"))
        self.session.save()
        self.refresh_breakpoints()
        self.refresh_summary()
        return state

    def remove_selected_breakpoint(self):
        bp = self._selected_breakpoint()
        if bp is None:
            return 0
        removed = self.session.remove_breakpoint(
            bp.get("script"), bp.get("line"), bp.get("kind"))
        self.session.save()
        self.refresh_breakpoints()
        self.refresh_stack()
        self.refresh_summary()
        return removed

    def on_breakpoint_selected(self):
        """Update the stack preview for the selected breakpoint."""
        bp = self._selected_breakpoint()
        if bp is not None:
            self.refresh_stack(bp.get("line"))

    # ---------------------------------------------------------------- stack
    def current_stack(self, breakpoint_line=None):
        """The stack frames for the current script (innermost first)."""
        script = self._current_script()
        code = script.get("code", "") if script else ""
        language = self.selected_language()
        line = breakpoint_line
        if line is None and hasattr(self, "line_edit"):
            try:
                line = int(self.line_edit.text())
            except (TypeError, ValueError):
                line = None
        return build_stack(code, breakpoint_line=line, category=language.key)

    def refresh_stack(self, breakpoint_line=None):
        view = getattr(self, "stack_view", None)
        if view is None:
            return
        self._frames = self.current_stack(breakpoint_line)
        lines = format_stack(self._frames)
        view.setPlainText("\n".join(lines) if lines else self.t(
            "debugger_no_frames", "(no frames)"))
        # Note the frames the adapter only guessed, so a guess is never
        # presented as a certainty.
        guessed = sum(1 for frame in self._frames
                      if frame.get("detail") == "pattern")
        if guessed and hasattr(self, "status"):
            self.status.setText(self.t(
                "debugger_frames_guessed",
                "{count} frame(s) guessed by the {language} adapter").format(
                    count=guessed,
                    language=self.selected_language().label))

    # -------------------------------------------------------------- watches
    def add_watch(self):
        expression = self.session.add_watch(self.watch_edit.text())
        if not expression:
            return ""
        self.watch_edit.clear()
        self.session.save()
        self.refresh_watches()
        self.refresh_summary()
        self.status.setText(self.t(
            "debugger_watch_added", "watching {name}").format(name=expression))
        return expression

    def remove_watch(self):
        expression = self.watch_edit.text().strip()
        if not expression:
            return 0
        removed = self.session.remove_watch(expression)
        self.watch_edit.clear()
        self.session.save()
        self.refresh_watches()
        self.refresh_summary()
        return removed

    def refresh_watches(self):
        view = getattr(self, "watch_view", None)
        if view is None:
            return
        pairs = evaluate_watches(self.session.watches)
        lines = ["%s = %s" % (expr, value) for expr, value in pairs]
        view.setPlainText("\n".join(lines) if lines else self.t(
            "debugger_no_watches", "(no watches)"))

    # ---------------------------------------------------------------- trace
    def log_trace(self, action):
        """Append one trace entry for `action` (with any note typed).

        A start / step / stop entry carries the language it was taken in, so a
        session spanning several languages stays readable.
        """
        detail = self.trace_edit.text().strip()
        if action in LANGUAGE_TRACE_ACTIONS:
            language = self.selected_language().label
            detail = ("[%s] %s" % (language, detail)) if detail \
                else "[%s]" % language
        self.session.log(action, detail)
        self.trace_edit.clear()
        self.session.save()
        self.refresh_trace()
        self.refresh_summary()
        self.status.setText(self.t(
            "debugger_logged", "logged: {action}").format(action=action))
        return action

    def refresh_trace(self):
        view = getattr(self, "trace_view", None)
        if view is None:
            return
        lines = format_trace(self.session.trace)
        view.setPlainText("\n".join(lines) if lines else self.t(
            "debugger_no_trace", "(empty trace log)"))

    # --------------------------------------------------------------- session
    def clear_session(self):
        """Clear the breakpoints, watches, trace log and validation records."""
        self.session.clear_breakpoints()
        self.session.clear_trace()
        self.session.clear_validation()
        self.session.clear_flow()
        self.session.watches = []
        self.session.save()
        self.refresh_breakpoints()
        self.refresh_stack()
        self.refresh_watches()
        self.refresh_trace()
        self.refresh_summary()
        if hasattr(self, "overview_view"):
            self.overview_view.clear()
        self.status.setText(self.t("debugger_cleared", "session cleared"))
        return True

    def save_session(self):
        """Persist the session to disk (returns the path or "")."""
        path = self.session.save()
        self.status.setText(self.t(
            "debugger_saved", "session saved: {path}").format(path=path))
        return path

    # ------------------------------------------------------- translation
    def retranslate(self, table):
        """Apply the main window's language table to this tab."""
        def label(key, fallback):
            return table.get(key, fallback)

        self.title_label.setText(label("debugger", "Debugger"))
        self.btn_refresh.setText(label("debugger_refresh", "Refresh"))
        self.btn_clear.setText(label("debugger_clear", "Clear session"))
        self.btn_add_bp.setText(label("debugger_add_bp", "Add breakpoint"))
        self.btn_add_watch.setText(label("debugger_add_watch", "Add watch"))
        self.btn_del_watch.setText(label("debugger_del_watch", "Remove watch"))
        self.btn_toggle_bp.setText(label("debugger_toggle_bp", "Toggle"))
        self.btn_remove_bp.setText(
            label("debugger_remove_bp", "Remove breakpoint"))
        self.btn_validate.setText(label("debugger_validate", "Validate"))
        self.act_validate_selected.setText(
            label("debugger_validate", "Validate"))
        self.act_validate_all.setText(
            label("debugger_validate_all", "Validate all"))
        self.btn_overview.setText(
            label("debugger_overview", "Debug overview"))
        self.flow_label.setText(label("debugger_flow", "Code flow:"))
        self.btn_record.setText(label("debugger_record", "Record"))
        self.flow_filter_combo.setItemText(
            0, label("debugger_flow_all", "all kinds"))
        for index, kind in enumerate(FLOW_FILTER_KINDS, 1):
            self.flow_filter_combo.setItemText(
                index, label("debugger_flow_" + kind, kind))
        self.condition_edit.setPlaceholderText(
            label("debugger_condition", "Condition (optional)"))
        self.watch_edit.setPlaceholderText(
            label("debugger_watch_hint", "Expression to watch..."))
        self.trace_edit.setPlaceholderText(
            label("debugger_trace_hint", "Note..."))
        for index, kind in enumerate(BREAKPOINT_KINDS):
            self.kind_combo.setItemText(
                index, label("debugger_bp_" + kind, kind))
        for index, block in enumerate(breakpoints.BLOCK_TARGETS):
            self.block_combo.setItemText(
                index, label("debugger_block_" + block, block))
        self.target_label.setText(label("debugger_target", "Target:"))
        self.target_edit.setPlaceholderText(
            label("debugger_target_hint", "Name to stop on..."))
        self.btn_resolve.setText(label("debugger_resolve", "Resolve"))
        self.btn_remove_bp_row.setText(
            label("debugger_remove_bp", "Remove breakpoint"))
        # The stepping and snapshot controls, presented in the same order the
        # row builds them so a translation table reads top-to-bottom.
        self.btn_start_session.setText(
            label("debugger_start_session", "Start session"))
        self.btn_step_modes.setText(label("debugger_step_menu", "Step..."))
        for mode, label_key, fallback, _meaning in debug_steps.STEP_MODES:
            button = self.step_buttons.get(mode)
            if button is not None:
                button.setText(table.get(label_key, fallback))
        self.btn_rewind.setText(label("debugger_rewind", "Rewind"))
        self.btn_snapshot.setText(label("debugger_snapshot", "Take"))
        self.btn_restore.setText(label("debugger_restore", "Restore"))
        self.btn_snapshot_diff.setText(
            label("debugger_snapshot_compare", "Compare"))
        self.snapshots_label.setText(
            label("debugger_snapshots", "Snapshots:"))
        self.replay_label.setText(
            label("debugger_replay_label", "Replay:"))
        for index, (_mode, label_key, fallback, _meaning) in enumerate(
                debug_steps.REPLAY_MODES):
            self.replay_combo.setItemText(index, table.get(label_key, fallback))
        self.btn_replay.setText(label("debugger_replay", "Replay"))
        self.flow_view_label.setText(
            label("debugger_flow_view", "View:"))
        self.btn_flow_save.setText(
            label("debugger_flow_save", "Save views"))
        for action, label_key, fallback in TRACE_ACTIONS:
            button = self.trace_buttons.get(action)
            if button is not None:
                button.setText(table.get(label_key, fallback))
        self.bp_tree.setHeaderLabels(self._breakpoint_headers())
        self.on_breakpoint_kind()
        self.refresh_summary()
