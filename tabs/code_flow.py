# -*- coding: utf-8 -*-
"""Code Flow Recorder for the Debugger tab.

This module owns the "Code Flow Recorder": the full execution timeline of a
script. It walks a document with the multilingual engine (`debug_langs`), which
knows every block, call, method call, import, variable write, throw, error,
warning and log statement the language carries, and turns that into an ordered
sequence of timestamped events -- the timeline of how the script would run.

    from tabs import code_flow

    recording = code_flow.record(script)
    recording.counts          # {"step": 12, "block": 3, ...}
    recording.timeline()      # every event, in execution order
    recording.for_kind("call")
    code_flow.format_timeline(recording.timeline())

What it records (the ten flow kinds):

    step        one event per executable line: the actual "every step" log
    block       every if / for / while / try / catch / switch / with it enters
    call        every function call
    method      every method call (``obj.method(...)``)
    import      every import / include / using / source
    variable    every assignment, i.e. every variable change
    exception   every raise / throw / Err.Raise / exit with an error code
    error       every parse or syntax fault in the document
    warning     every TODO / FIXME / XXX / HACK marker
    log         every print / console.log / Write-Host / echo / logger call

The recorder is a *simulation* of the run, not a live process: it derives the
order from the document itself (the engine reports events per line, and the
recorder adds the step for each executable line between them). It is therefore
deterministic, needs no interpreter, and is safe to run on any script in the
database. It also consumes any real steps fed to it (`feed_step`), so a genuine
debug adapter can drive the same timeline later.

Like `debug_langs` this module imports no Qt, so it is testable from a shell.
"""

import datetime as dt

from . import debug_langs

# The recorder's own session history limit: a pathological loop cannot fill
# memory with step events. Well beyond any hand-written script timeline.
MAX_EVENTS = 20000

# How many characters of the source line an event carries.
DETAIL_LIMIT = 200

# The kinds a single line can produce, in the order they would happen on it:
# a block opens, an import binds, a variable is written, a call is made, a log
# is written, and an exception is thrown. Steps come first for the line.
LINE_ORDER = ("import", "variable", "block", "call", "method", "log",
              "warning", "exception")


def _now():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _line_key(kind):
    """The position of a flow kind within its line (see LINE_ORDER)."""
    if kind == "exception":
        return 99
    return LINE_ORDER.index(kind) if kind in LINE_ORDER else 50


class FlowEvent(object):
    """One entry in the execution timeline.

    ``index`` is the position in the timeline (1-based), ``line`` the source
    line, ``kind`` one of `debug_langs.FLOW_KINDS`, ``block`` the block kind
    for a ``block`` event, ``depth`` the nesting depth at that point, ``name``
    the symbol involved (a function name, a variable, a throw type...) and
    ``detail`` the source text. ``source`` is ``"marker"`` when the language
    marked it outright and ``"pattern"`` when the adapter inferred it.
    """

    __slots__ = ("index", "line", "kind", "block", "depth", "name", "detail",
                 "source", "at")

    def __init__(self, line, kind, name="", detail="", block="", depth=0,
                 source="marker", at=""):
        self.index = 0
        self.line = int(line or 0)
        self.kind = kind
        self.block = block or ""
        self.depth = int(depth or 0)
        self.name = name or ""
        self.detail = (detail or "")[:DETAIL_LIMIT]
        self.source = source or "marker"
        self.at = at or _now()

    def to_dict(self):
        return {
            "index": self.index,
            "line": self.line,
            "kind": self.kind,
            "block": self.block,
            "depth": self.depth,
            "name": self.name,
            "detail": self.detail,
            "source": self.source,
            "at": self.at,
        }

    def text(self):
        """One timeline line: ``#12  line  42  call       foo <source>``."""
        symbol = (" %s" % self.name) if self.name else ""
        block = (" %s" % self.block) if self.block else ""
        note = " (%s)" % self.source if self.source == "pattern" else ""
        symbol = symbol + note
        return "#%-5d line %-5d %-10s%s  %s%s" % (
            self.index, self.line, self.kind, block,
            (self.detail or "")[:70], symbol)

    def __repr__(self):
        return "FlowEvent(#%d %s line %d %r)" % (
            self.index, self.kind, self.line, self.name)


class CodeFlowRecording(object):
    """The recorded execution timeline of one script."""

    def __init__(self, script="", language="", label=""):
        self.script = script or ""
        self.language = language or ""
        self.label = label or ""
        self.events = []
        self.started = ""
        self.finished = ""
        self.truncated = False

    # ------------------------------------------------------------- writing
    def add(self, event):
        """Append one event, stamping its timeline index."""
        if len(self.events) >= MAX_EVENTS:
            self.truncated = True
            return None
        event.index = len(self.events) + 1
        self.events.append(event)
        return event

    def finish(self):
        self.finished = _now()
        return self

    # ------------------------------------------------------------- reading
    def timeline(self):
        """Every event, in execution order."""
        return list(self.events)

    def steps(self):
        """Only the line steps (the coarse execution trace)."""
        return [event for event in self.events if event.kind == "step"]

    def for_kind(self, kind):
        """Every event of one kind (or every event when `kind` is empty)."""
        if not kind:
            return list(self.events)
        return [event for event in self.events if event.kind == kind]

    def for_line(self, line):
        """Every event recorded for one source line."""
        return [event for event in self.events if event.line == line]

    def at(self, index):
        """The event at a 1-based timeline index (or None)."""
        if 1 <= index <= len(self.events):
            return self.events[index - 1]
        return None

    def counts(self):
        """How many events of each kind the recording holds."""
        counts = {kind: 0 for kind in debug_langs.FLOW_KINDS}
        for event in self.events:
            counts[event.kind] = counts.get(event.kind, 0) + 1
        return counts

    def block_counts(self):
        """How many times each block kind was entered."""
        counts = {}
        for event in self.events:
            if event.kind == "block" and event.block:
                counts[event.block] = counts.get(event.block, 0) + 1
        return counts

    def max_depth(self):
        """The deepest nesting the timeline reached."""
        return max((event.depth for event in self.events), default=0)

    def summary(self):
        """One line describing the recording (for a status bar / report)."""
        counts = self.counts()
        parts = ["%d event(s)" % len(self.events)]
        for kind in ("step", "block", "call", "method", "exception", "error",
                     "warning", "log", "import", "variable"):
            if counts.get(kind):
                parts.append("%s=%d" % (kind, counts[kind]))
        if self.truncated:
            parts.append("truncated")
        return " | ".join(parts)

    def to_dict(self):
        return {
            "script": self.script,
            "language": self.language,
            "label": self.label,
            "started": self.started,
            "finished": self.finished,
            "truncated": self.truncated,
            "counts": self.counts(),
            "events": [event.to_dict() for event in self.events],
        }

    def __len__(self):
        return len(self.events)

    def __repr__(self):
        return "CodeFlowRecording(%r, %d event(s))" % (self.script,
                                                       len(self.events))


class CodeFlowRecorder(object):
    """Turns a document into an ordered execution timeline.

    The recorder is fed by the language adapter: the adapter reports every
    flow event the document contains and, on top of that, the recorder emits a
    `step` for each executable line, so the timeline reads like a run. It also
    records the document's parse faults as `error` events and keeps the block
    nesting depth, so a block event says how deep the script was when it
    entered it.
    """

    def __init__(self, language="Python", label=""):
        self.language = language or ""
        self.label = label or ""
        self.recording = None

    # --------------------------------------------------------------- record
    def record(self, script):
        """Record the full execution timeline of a script entry.

        `script` is a database entry (``name`` / ``category`` / ``code``) or a
        plain string. The category resolves the language adapter; the code is
        what gets walked. Never raises: a document with no code records an
        empty timeline with the reason in its language field.
        """
        if isinstance(script, dict):
            code = script.get("code", "") or ""
            name = script.get("name", "") or ""
            adapter = debug_langs.adapter_for(script.get("category", ""),
                                              extension=name)
        else:
            code = script or ""
            name = ""
            adapter = debug_langs.adapter_for(self.language or "")
        adapter.text = code
        recording = CodeFlowRecording(name, adapter.key, adapter.label)
        recording.started = _now()
        self.recording = recording
        if not (code or "").strip():
            return recording.finish()
        # A data document has no control flow: it is validated, not stepped.
        if adapter.data_format:
            self._record_document(recording, adapter, code)
            return recording.finish()
        self._record_code(recording, adapter, code)
        return recording.finish()

    # ------------------------------------------------------------ internals
    def _record_code(self, recording, adapter, code):
        """Walk the code once and emit the timeline in source order."""
        lines = code.splitlines()
        # Every event the adapter can see, grouped by the line it sits on.
        by_line = {}
        for event in adapter.flow_events():
            by_line.setdefault(event["line"], []).append(event)
        # The parse faults become `error` events at their own line.
        validation = adapter.validate(code)
        if not validation.get("ok") and validation.get("error"):
            fault_line = int(validation.get("line") or 1)
            by_line.setdefault(fault_line, []).append({
                "line": fault_line,
                "kind": "error",
                "name": validation.get("error", "parse error"),
                "detail": validation.get("error", ""),
                "source": "marker",
            })
        depth = 0
        closed = self._block_closers(adapter)
        for number, raw in enumerate(lines, 1):
            stripped = raw.strip()
            # A closing brace steps out of the block before the step itself.
            if stripped in closed:
                depth = max(0, depth - 1)
            # A blank line has no event; a comment line does not *step*, but
            # it can still carry a warning (a TODO) or a log, so its events
            # are emitted below -- only the step is skipped.
            stepped = bool(stripped) and not adapter.is_comment(raw)
            if stepped:
                recording.add(FlowEvent(
                    number, "step", name="",
                    detail=stripped, depth=depth, source="marker"))
            elif not stripped:
                continue
            events = sorted(by_line.get(number, []),
                            key=lambda event: _line_key(event["kind"]))
            for event in events:
                kind = event["kind"]
                # The adapter already resolved the block depth from the
                # language's own structure (indentation or braces), so its
                # value is authoritative; the recorder's counter is only the
                # fallback for an event the adapter gave no depth for.
                if kind == "block":
                    depth = int(event.get("depth", depth))
                recording.add(FlowEvent(
                    number, kind, name=event.get("name", ""),
                    detail=event.get("detail", ""),
                    block=event.get("block", ""),
                    depth=event.get("depth", depth),
                    source=event.get("source", "marker")))
        # A file that ends inside a block leaves the depth where it was; the
        # timeline is still complete, so no adjustment is needed.

    def _record_document(self, recording, adapter, code):
        """A data document: record its validation as a single error or note."""
        validation = adapter.validate(code)
        counts = validation.get("counts") or {}
        if validation.get("ok"):
            recording.add(FlowEvent(
                0, "step", name="document",
                detail="valid %s (%s)" % (adapter.label,
                                          ", ".join(sorted(counts))),
                source="marker"))
            return
        recording.add(FlowEvent(
            int(validation.get("line") or 0), "error",
            name=validation.get("error", "invalid document"),
            detail=validation.get("summary", ""), source="marker"))

    def _block_closers(self, adapter):
        """The line texts that close a block in this language."""
        if adapter.key in ("python", "batch", "shell", "vbscript"):
            return ()
        return ("}",)

    # ---------------------------------------------------------- live steps
    def feed_step(self, line, name="", detail="", depth=0):
        """Append a real step to the current recording.

        This is the hook a genuine debug adapter uses: it hands the recorder
        the line it just stopped on, and the recorder appends it to the same
        timeline. With no recording in progress it is a no-op, so a caller can
        feed unconditionally.
        """
        if self.recording is None:
            return None
        return self.recording.add(FlowEvent(line, "step", name=name or "step",
                                            detail=detail, depth=depth))

    def feed_event(self, kind, line, **fields):
        """Append any real flow event to the current recording."""
        if self.recording is None:
            return None
        return self.recording.add(FlowEvent(line, kind, **fields))


# ---------------------------------------------------------------------------
# Module level helpers
# ---------------------------------------------------------------------------
def record(script, language="Python"):
    """Record a script's full execution timeline (never raises).

    Accepts a database entry (with ``name`` / ``category`` / ``code``) or a
    plain code string.
    """
    if isinstance(script, dict):
        recorder = CodeFlowRecorder(script.get("category", ""), "")
    else:
        recorder = CodeFlowRecorder(language, "")
    return recorder.record(script)


def format_timeline(events, language=""):
    """The timeline as ASCII lines, one per event (pane and report form)."""
    lines = []
    if language:
        lines.append("Code flow: %s" % language)
        lines.append("=" * (11 + len(language)))
    for event in events or []:
        lines.append(event.text() if hasattr(event, "text")
                     else str(event))
    return lines


def format_report(recording):
    """The recording as an ASCII report (Reports tab / file export)."""
    counts = recording.counts()
    blocks = recording.block_counts()
    lines = [
        "ScriptDB code flow recording",
        "============================",
        "Script   : %s" % recording.script,
        "Language : %s [%s]" % (recording.label, recording.language),
        "Started  : %s" % recording.started,
        "Finished : %s" % recording.finished,
        "Events   : %d" % len(recording.events),
        "Depth    : %d" % recording.max_depth(),
        "Truncated: %s" % ("yes" if recording.truncated else "no"),
        "",
        "Event counts",
        "------------",
    ]
    for kind in debug_langs.FLOW_KINDS:
        lines.append("  %-10s : %d" % (kind, counts.get(kind, 0)))
    if blocks:
        lines += ["", "Blocks entered", "--------------"]
        for block in sorted(blocks):
            lines.append("  %-10s : %d" % (block, blocks[block]))
    lines += ["", "Timeline", "--------"]
    lines += format_timeline(recording.timeline())
    return "\n".join(lines)


def flow_capabilities():
    """Describe the recorder for the audit / about layer."""
    return {
        "kinds": list(debug_langs.FLOW_KINDS),
        "block_kinds": list(debug_langs.BLOCK_KINDS),
        "max_events": MAX_EVENTS,
        "live_steps": True,
        "offline": True,
    }
def recording_from_dict(data):
    """Rebuild a recording from ``to_dict()`` output (never raises).

    This is what lets the Debugger tab keep a recording in its session file
    and show it again after a restart. A malformed payload comes back as an
    empty recording with an ``error`` event rather than raising.
    """
    if not isinstance(data, dict):
        recording = CodeFlowRecording()
        recording.add(FlowEvent(0, "error", name="malformed recording"))
        return recording.finish()
    recording = CodeFlowRecording(data.get("script", ""),
                                  data.get("language", ""),
                                  data.get("label", ""))
    recording.started = data.get("started", "")
    recording.finished = data.get("finished", "")
    recording.truncated = bool(data.get("truncated"))
    for item in data.get("events", []) or []:
        if not isinstance(item, dict):
            continue
        event = FlowEvent(item.get("line", 0), item.get("kind", "step"),
                          name=item.get("name", ""),
                          detail=item.get("detail", ""),
                          block=item.get("block", ""),
                          depth=item.get("depth", 0),
                          source=item.get("source", "marker"),
                          at=item.get("at", ""))
        recording.add(event)
    return recording
