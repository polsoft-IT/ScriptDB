# -*- coding: utf-8 -*-
"""Breakpoint model for the Debugger tab -- eight kinds of stop point.

A breakpoint says *where* the debugger should stop and *why*. This module owns
that model: the eight kinds, the target each kind needs, the condition an
expression breakpoint carries, and the matching that resolves a breakpoint
against a script (or against a recorded timeline).

    from tabs import breakpoints

    bp = breakpoints.make("function", script="demo.py", target="render",
                          condition="row.count > 10")
    breakpoints.matches(bp, event)          # does this timeline event stop?
    breakpoints.resolve(bp, script)         # which lines would it stop on?
    breakpoints.validate(bp)                # is the record usable?

The eight kinds:

    line        a source line (the classic breakpoint)
    function    entry to a named function
    class       entry to a named class (its methods)
    block       entry to a kind of block: if / loop / try / catch / switch /
                with / jump / call
    condition   a line that only stops when an expression holds -- this is a
                *conditional line* breakpoint, so it needs a line and a
                condition
    variable    a change to a named variable (its write sites)
    exception   a thrown / raised exception, optionally one named type
    file        a change to a file -- raised when the recording names that
                file as a touched path (see `note` / `paths`)
    import      an import / include / using / source statement

A breakpoint is a plain dict so it serialises into the session file unchanged:

    {"script", "kind", "target", "line", "condition", "enabled", "language",
     "created", "hits"}

``hits`` is the number of times the breakpoint stopped during the last
resolution, which is what makes the list readable as a debugging history.

Like the other debugger engines this module imports no Qt, so it is testable
from a shell, and it never runs the code it inspects.
"""

from . import debug_langs

# The eight kinds, in the order the picker lists them.
BREAKPOINT_KINDS = ("line", "function", "class", "block", "condition",
                    "variable", "exception", "file", "import")

# The kinds that need a target name (the others use the line or nothing).
TARGETED_KINDS = ("function", "class", "block", "variable", "exception",
                  "file")

# The kinds that need a source line.
LINED_KINDS = ("line", "condition")

# The kinds that need a condition expression.
CONDITION_KINDS = ("condition",)

# The block kinds a `block` breakpoint can name (from the engine, so a new
# language block kind only has to be added there).
BLOCK_TARGETS = tuple(debug_langs.BLOCK_KINDS)

# The label of a kind, for a report or a plain-text listing.
KIND_LABELS = {
    "line": "line",
    "function": "function",
    "class": "class",
    "block": "block",
    "condition": "conditional",
    "variable": "variable",
    "exception": "exception",
    "file": "file change",
    "import": "import",
}

# Which flow event kind each breakpoint kind looks for. A `line` breakpoint
# stops on any event at its line, and a `condition` breakpoint likewise, so
# those two are handled separately in `matches()`.
KIND_EVENTS = {
    "function": ("block",),
    "class": ("block",),
    "block": ("block",),
    "variable": ("variable",),
    "exception": ("exception", "error"),
    "import": ("import",),
}


def _now():
    import datetime as dt
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _as_line(value):
    """Coerce a text line number to an int (or 0 when it is not one)."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Building and validating
# ---------------------------------------------------------------------------
def normalize_kind(kind):
    """A known kind, or ``"line"`` when the value is not one."""
    kind = (kind or "").strip().lower()
    if kind in BREAKPOINT_KINDS:
        return kind
    # Accept the older, narrower names so a stored session keeps working.
    legacy = {"break": "line", "throw": "exception", "filechange": "file"}
    return legacy.get(kind, "line")


def make(kind="line", script="", line=0, target="", condition="",
         language="", enabled=True, note="", paths=None):
    """Build a breakpoint record (a plain dict, ready for the session file).

    Nothing is rejected here: an incomplete record is still a record, and
    :func:`validate` says what it is missing. That keeps the UI able to show a
    half-typed breakpoint instead of throwing it away.
    """
    kind = normalize_kind(kind)
    record = {
        "script": script or "(current)",
        "kind": kind,
        "target": (target or "").strip(),
        "line": _as_line(line),
        "condition": (condition or "").strip(),
        "language": language or "",
        "enabled": bool(enabled),
        "note": note or "",
        "hits": 0,
        "created": _now(),
    }
    if paths:
        record["paths"] = list(paths)
    return record


def validate(record):
    """``(ok, problems)`` for a breakpoint record.

    A problem is a short sentence naming what the record still needs, so the
    tab can show it beside the row rather than silently refusing the entry.
    """
    problems = []
    if not isinstance(record, dict):
        return False, ["not a breakpoint record"]
    kind = normalize_kind(record.get("kind"))
    line = _as_line(record.get("line"))
    target = (record.get("target") or "").strip()
    if kind in LINED_KINDS and line <= 0:
        problems.append("needs a line number")
    if kind in TARGETED_KINDS and not target:
        problems.append("needs a %s name" % kind)
    if kind in CONDITION_KINDS and not (record.get("condition") or "").strip():
        problems.append("needs a condition expression")
    if kind == "block" and target and target not in BLOCK_TARGETS:
        problems.append("block must be one of: %s" % ", ".join(BLOCK_TARGETS))
    return (not problems), problems


def describe(record):
    """One line describing a breakpoint (used in the list and the report)."""
    kind = normalize_kind(record.get("kind"))
    label = KIND_LABELS.get(kind, kind)
    parts = [label]
    target = (record.get("target") or "").strip()
    if target:
        parts.append(target)
    line = _as_line(record.get("line"))
    if line:
        parts.append("line %d" % line)
    condition = (record.get("condition") or "").strip()
    if condition:
        parts.append("if %s" % condition)
    if not record.get("enabled", True):
        parts.append("(off)")
    return "  ".join(parts)


# ---------------------------------------------------------------------------
# Matching against a recorded timeline
# ---------------------------------------------------------------------------
def _target_matches(target, name):
    """True when a breakpoint target names a symbol.

    The comparison is case-insensitive and accepts a dotted tail, so a
    `function` breakpoint for ``render`` also matches ``Report.render``, which
    is how the engine reports a method.
    """
    if not target:
        return True
    target = target.lower()
    name = (name or "").lower()
    if not name:
        return False
    return (name == target or name.endswith("." + target)
            or name.split("(")[0].strip() == target)


def matches(record, event):
    """True when a timeline event would stop on this breakpoint.

    `event` is a `tabs.code_flow.FlowEvent` (or any object with ``kind``,
    ``line``, ``name`` and ``block``). A disabled breakpoint never matches.
    """
    if not record or not record.get("enabled", True):
        return False
    if event is None:
        return False
    kind = normalize_kind(record.get("kind"))
    line = _as_line(record.get("line"))
    target = (record.get("target") or "").strip()
    event_kind = getattr(event, "kind", "")
    event_line = getattr(event, "line", 0)
    event_name = getattr(event, "name", "") or ""
    event_block = getattr(event, "block", "") or ""

    # A line breakpoint stops on anything that happens at its line -- that is
    # what makes it the general-purpose kind.
    if kind == "line":
        return line > 0 and event_line == line

    # A conditional breakpoint is a line breakpoint that also carries an
    # expression; the expression is what the tab shows beside it.
    if kind == "condition":
        return line > 0 and event_line == line

    if kind in ("function", "class"):
        if event_kind != "block" or event_block != ("def" if kind == "function"
                                                    else "class"):
            return False
        return _target_matches(target, event_name)

    if kind == "block":
        if event_kind != "block":
            return False
        return not target or event_block == target

    if kind == "variable":
        if event_kind != "variable":
            return False
        return _target_matches(target, event_name)

    if kind == "exception":
        if event_kind not in ("exception", "error"):
            return False
        return _target_matches(target, event_name)

    if kind == "import":
        if event_kind != "import":
            return False
        return _target_matches(target, event_name)

    if kind == "file":
        # A file breakpoint has no per-line event: it fires when the recording
        # reports the named file among the paths it touched.
        return event_kind == "file" and _target_matches(target, event_name)

    return False


def hits(record, recording):
    """The events of a recording this breakpoint would stop on."""
    if recording is None:
        return []
    return [event for event in recording.timeline() if matches(record, event)]


def resolve(record, script):
    """The lines a breakpoint resolves to for a script, without running it.

    `script` is a database entry or a code string. The result is a list of
    ``(line, kind, name)`` triples, so the tab can show where a function or a
    variable breakpoint actually lands -- the classic "this breakpoint moved
    from line 9 to line 12" answer -- and `hits` is set on the record.
    """
    if isinstance(script, dict):
        code = script.get("code", "") or ""
        adapter = debug_langs.adapter_for(script.get("category", ""),
                                          extension=script.get("name", ""))
    else:
        code = script or ""
        adapter = debug_langs.adapter_for(record.get("language") or "")
    adapter.text = code
    kind = normalize_kind(record.get("kind"))
    line = _as_line(record.get("line"))
    target = (record.get("target") or "").strip()
    found = []

    if kind in LINED_KINDS:
        if line > 0:
            found.append((line, kind, target or "line %d" % line))
    else:
        for event in adapter.flow_events():
            probe = _EventView(event)
            if matches(record, probe):
                found.append((event["line"], event["kind"],
                              event.get("name", "") or event.get("block", "")))
    # A file breakpoint cannot be resolved from the text alone: it is about
    # another file changing, which only a run can report.
    record["resolved"] = [{"line": item[0], "kind": item[1], "name": item[2]}
                          for item in found]
    record["hits"] = len(found)
    if found and kind in LINED_KINDS:
        record["line"] = found[0][0]
    return found


class _EventView(object):
    """An adapter flow-event dict wearing the `FlowEvent` attribute names.

    `debug_langs` reports events as dicts, while the timeline holds
    `FlowEvent` objects. This adapter lets one `matches()` serve both.
    """

    __slots__ = ("kind", "line", "name", "block", "depth")

    def __init__(self, data):
        self.kind = data.get("kind", "")
        self.line = data.get("line", 0)
        self.name = data.get("name", "")
        self.block = data.get("block", "")
        self.depth = data.get("depth", 0)


# ---------------------------------------------------------------------------
# File-change breakpoints
# ---------------------------------------------------------------------------
def file_change_events(paths, label=""):
    """Synthetic `file` events for a set of touched paths.

    A file breakpoint is about the run touching *another* file, which the text
    of a script cannot show. When a run (or a report) knows which paths were
    written, it feeds them here and the breakpoint matches them like any other
    event -- so a file breakpoint is recorded and reported exactly like the
    rest, instead of living in a special case.
    """
    from .code_flow import FlowEvent
    events = []
    for index, path in enumerate(paths or [], 1):
        event = FlowEvent(0, "file", name=str(path),
                          detail=label or "file changed")
        event.index = index
        events.append(event)
    return events


# ---------------------------------------------------------------------------
# Whole-set helpers (the list the tab shows)
# ---------------------------------------------------------------------------
def validate_all(records):
    """``[(record, problems), ...]`` for every record that has a problem."""
    out = []
    for record in records or []:
        ok, problems = validate(record)
        if not ok:
            out.append((record, problems))
    return out


def filter_breakpoints(records, script="", kind="", enabled=None):
    """The records matching a script, a kind and an enabled state."""
    out = []
    for record in records or []:
        if script and record.get("script") != script:
            continue
        if kind and normalize_kind(record.get("kind")) != kind:
            continue
        if enabled is not None and bool(record.get("enabled", True)) != enabled:
            continue
        out.append(record)
    return out


def group_by_kind(records):
    """The records grouped by kind, in picker order."""
    groups = {}
    for record in records or []:
        groups.setdefault(normalize_kind(record.get("kind")), []).append(record)
    return groups


def summary(records):
    """The headline counts for a breakpoint list."""
    counts = {kind: 0 for kind in BREAKPOINT_KINDS}
    enabled = 0
    problems = 0
    for record in records or []:
        counts[normalize_kind(record.get("kind"))] += 1
        if record.get("enabled", True):
            enabled += 1
        if not validate(record)[0]:
            problems += 1
    return {
        "total": len(records or []),
        "enabled": enabled,
        "disabled": len(records or []) - enabled,
        "by_kind": counts,
        "problems": problems,
    }


def format_list(records):
    """The breakpoint list as ASCII lines (pane, report and tests)."""
    lines = ["%-4s %-11s %-20s %-9s %-18s %s" % (
        "#", "kind", "target", "line", "condition", "state")]
    lines.append("-" * 78)
    if not records:
        lines.append("(no breakpoints)")
        return lines
    for position, record in enumerate(records, 1):
        ok, problems = validate(record)
        state = "on" if record.get("enabled", True) else "off"
        if not ok:
            state = "incomplete: %s" % "; ".join(problems)
        lines.append("%-4d %-11s %-20s %-9s %-18s %s" % (
            position, KIND_LABELS.get(normalize_kind(record.get("kind")), "?"),
            (record.get("target") or "-")[:20],
            ("line %d" % _as_line(record.get("line")))
            if _as_line(record.get("line")) else "-",
            (record.get("condition") or "-")[:18], state))
    return lines


def breakpoint_capabilities():
    """Describe the breakpoint model for the audit / about layer."""
    return {
        "kinds": list(BREAKPOINT_KINDS),
        "targeted": list(TARGETED_KINDS),
        "lined": list(LINED_KINDS),
        "conditioned": list(CONDITION_KINDS),
        "block_targets": list(BLOCK_TARGETS),
        "resolves_offline": True,
        "offline": True,
    }
