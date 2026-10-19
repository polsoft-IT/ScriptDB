# -*- coding: utf-8 -*-
"""The debugger's missing half: stepping, conditions, snapshots and replay.

The Debugger tab could set breakpoints and validate a script, but it could not
*run* a session: there was no way to step, no way to make a breakpoint
conditional in a way that actually gated anything, no way to snapshot a session
and come back to it, and no way to replay one. This module is that half.

It is deliberately separate from `tabs/debugger.py`. The tab is a view over a
session; this is the session's *engine*, and it is the part a test, an API
caller or the CLI can drive without a window. Nothing here imports Qt.

    STEP_MODES       the five ways a session can move (step over / into / out,
                     next breakpoint, run to line)
    SessionRunner    a cursor over a recorded timeline: step, inspect, snapshot
    SessionSnapshot  a frozen session state that can be restored or replayed
    eval_condition   the conditional-breakpoint evaluator

The one design decision worth stating: stepping walks a *recorded* timeline
(`tabs.code_flow`) rather than a live interpreter, because the Debugger tab is
offline on purpose and the whole point of the module is that a session can be
inspected, replayed and re-entered without a process being started. A step is
therefore a move of a cursor over known events, and every step is reproducible
— which is exactly what a debugging session needs to be.
"""

import datetime as dt

# ---------------------------------------------------------------------------
# The five ways a session moves
# ---------------------------------------------------------------------------
# (key, label key, fallback, what the move does)
STEP_MODES = (
    ("over", "debug_step_over", "Step over",
     "advance to the next event at this depth or shallower, so a call is "
     "taken as one step rather than entered"),
    ("into", "debug_step_into", "Step into",
     "advance to the very next event, entering whatever depth it is at"),
    ("out", "debug_step_out", "Step out",
     "advance past the current depth: the next event shallower than here"),
    ("breakpoint", "debug_step_to_breakpoint", "Next breakpoint",
     "advance to the next event any enabled breakpoint would stop on"),
    ("line", "debug_step_to_line", "Run to line",
     "advance to the next event on the line the user names"),
)

STEP_MODE_KEYS = tuple(key for key, _lk, _f, _d in STEP_MODES)
STEP_MODE_LABELS = {key: fallback for key, _lk, fallback, _d in STEP_MODES}
STEP_MODE_MEANINGS = {key: meaning for key, _lk, _f, meaning in STEP_MODES}

# The event kinds a step treats as "a place worth stopping". A step that landed
# on every recorded annotation would be useless: a timeline notes definitions,
# imports and comments as well as the flow, and a debugger stops on the flow.
STEP_KINDS = ("call", "return", "jump", "branch", "loop", "throw", "catch",
              "assign", "read", "write", "invoke", "await", "yield",
              "exception", "enter", "exit")

# The kinds that are *entry* points rather than flow: stepping out uses them.
FRAME_ENTER_KINDS = ("enter", "call")
FRAME_LEAVE_KINDS = ("exit", "return")

# ---------------------------------------------------------------------------
# What a session records
# ---------------------------------------------------------------------------
# (key, label key, fallback, what it is)
SESSION_FEATURES = (
    ("stepping", "debug_feat_stepping", "Stepping",
     "move a session forward five ways: over, into, out, to a breakpoint or "
     "to a line"),
    ("conditions", "debug_feat_conditions", "Conditional breakpoints",
     "a condition is evaluated against the event's own state, so a breakpoint "
     "stops only when it is asked to"),
    ("hit_counts", "debug_feat_hits", "Hit counts",
     "every breakpoint counts how often it was reached, and can stop on the "
     "nth time"),
    ("snapshots", "debug_feat_snapshots", "Snapshots",
     "freeze the session at a point and return to it, or compare two points"),
    ("replay", "debug_feat_replay", "Replay",
     "replay a recorded session step by step, at once (a transcript) or "
     "backwards"),
    ("watch_eval", "debug_feat_watch", "Watch evaluation",
     "resolve the watch list against the state the session has reached"),
)

SESSION_FEATURE_KEYS = tuple(key for key, _lk, _f, _d in SESSION_FEATURES)
SESSION_FEATURE_LABELS = {key: fallback for key, _lk, fallback, _d
                          in SESSION_FEATURES}
SESSION_FEATURE_MEANINGS = {key: meaning for key, _lk, _f, meaning
                            in SESSION_FEATURES}

# How many snapshots a session keeps before the oldest is dropped. A debugging
# session is long; an unbounded list of frozen states would grow without ever
# being read.
MAX_SNAPSHOTS = 50

# The operators a condition may use, worst ambiguity first. `=` is accepted as
# equality because that is what people type, and it is normalised to `==` before
# evaluation so the stored expression stays as the user wrote it.
CONDITION_OPERATORS = ("==", "!=", ">=", "<=", ">", "<", "=", "true", "false")

# What a condition is evaluated against: the event's own fields. These names
# are the vocabulary of a condition, and they are listed in the report so the
# editor can hint them.
CONDITION_FIELDS = ("line", "depth", "kind", "name", "block", "index",
                    "detail")


# ---------------------------------------------------------------------------
# Conditional breakpoints
# ---------------------------------------------------------------------------
def _as_number(value):
    """A value as a float, or None when it is not numeric."""
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def split_condition(text):
    """A condition split into ``(field, operator, value)``, or None.

    The split is done by finding a *real* operator token, longest first, and
    never with `in`: a condition such as `kind == call` contains the word
    `call`, and a naive scan for the `=` character happily matched inside the
    value instead of at the operator. Only the tokens below are operators, and
    they are tried longest-first so `>=` is never read as `>`.
    """
    text = str(text or "").strip()
    if not text:
        return None
    for operator in (">=", "<=", "==", "!=", "=", ">", "<"):
        index = text.find(operator)
        if index < 0:
            continue
        left = text[:index].strip()
        right = text[index + len(operator):].strip()
        # Both sides have to be there: `> 5` is not a condition, and neither is
        # `line ==`. A malformed condition is reported rather than guessed at.
        if not left or not right:
            return None
        return left, operator, right
    return None

def event_state(event):
    """The event's own fields, as the mapping a condition is evaluated against.

    A `FlowEvent` or a plain dict both work: a condition should not depend on
    which of the two a caller happened to hold.
    """
    if event is None:
        return {}
    if isinstance(event, dict):
        source = event
    else:
        source = {field: getattr(event, field, "") for field in CONDITION_FIELDS}
    return {field: source.get(field, "") for field in CONDITION_FIELDS}



def eval_condition(condition, event, variables=None):
    """Evaluate one breakpoint condition against an event: ``(True/False, why)``.

    The evaluator is deliberately tiny and total: it never raises and it never
    calls `eval`. A condition that cannot be understood evaluates to True, so an
    unparseable condition **stops** rather than silently skipping -- a
    breakpoint that never fires is a bug the user cannot see, while one that
    fires too often is a bug they can.

    Supported forms::

        <field> <op> <value>      line > 20, depth == 2, kind == call
        <field>                   line   (true when non-empty/non-zero)
        true / false              a constant
        and / or chains           line > 20 and depth == 1
    """
    text = str(condition or "").strip()
    if not text:
        return True, "no condition"
    state = event_state(event)
    if variables:
        # A caller may supply extra names (a watch result, a captured value).
        for key, value in variables.items():
            state.setdefault(key, value)

    # The chains are split first, so `a or b and c` binds the way it does in
    # most languages a user knows.
    if " or " in text:
        results = [eval_condition(part, event, variables)[0]
                   for part in text.split(" or ")]
        return any(results), "or chain (%s)" % any(results)
    if " and " in text:
        results = [eval_condition(part, event, variables)[0]
                   for part in text.split(" and ")]
        return all(results), "and chain (%s)" % all(results)

    lowered = text.lower()
    if lowered == "true":
        return True, "constant true"
    if lowered == "false":
        return False, "constant false"

    parts = split_condition(text)
    if parts is None:
        # A bare field name: truthy when it holds anything.
        if lowered in state:
            value = str(state.get(lowered, "")).strip()
            truthy = bool(value) and value not in ("0", "0.0", "False", "None")
            return truthy, "%s is %s" % (lowered, "set" if truthy else "empty")
        return True, "unrecognised condition %r (stops)" % text[:40]

    field_name, operator, wanted = parts
    wanted = wanted.strip().strip('"').strip("'")
    if field_name not in state:
        return True, "unknown field %r (stops)" % field_name
    actual = state.get(field_name)
    # Numeric comparison when both sides are numeric, textual otherwise, so
    # `kind == call` and `line > 20` both behave as a reader expects.
    left_number, right_number = _as_number(actual), _as_number(wanted)
    if operator in ("=", "=="):
        if left_number is not None and right_number is not None:
            return left_number == right_number, "%s == %s" % (actual, wanted)
        return str(actual) == wanted, "%s == %s" % (actual, wanted)
    if operator == "!=":
        if left_number is not None and right_number is not None:
            return left_number != right_number, "%s != %s" % (actual, wanted)
        return str(actual) != wanted, "%s != %s" % (actual, wanted)
    if left_number is None or right_number is None:
        return True, "%s is not numeric (stops)" % operator
    pairs = {">": left_number > right_number,
             "<": left_number < right_number,
             ">=": left_number >= right_number,
             "<=": left_number <= right_number}
    return pairs[operator], "%s %s %s" % (actual, operator, wanted)

def describe_condition(condition):
    """A condition as one readable line (the report and the breakpoint list)."""
    text = str(condition or "").strip()
    if not text:
        return "(always)"
    return text if len(text) <= 60 else text[:57] + "..."


# ---------------------------------------------------------------------------
# A snapshot of a session at one point
# ---------------------------------------------------------------------------
class SessionSnapshot(object):
    """A frozen session state: where the cursor was and what it had seen.

    A snapshot is what makes "go back to how it was" possible without replaying
    from the start. It holds the cursor, the hit counts at that point and the
    watch values, so a snapshot can be restored, compared with another, or
    written into a report.
    """

    __slots__ = ("label", "index", "line", "depth", "kind", "name", "hits",
                 "watches", "at")

    def __init__(self, label="", index=0, line=0, depth=0, kind="", name="",
                 hits=None, watches=None, at=""):
        self.label = label or ""
        self.index = int(index or 0)
        self.line = int(line or 0)
        self.depth = int(depth or 0)
        self.kind = kind or ""
        self.name = name or ""
        self.hits = dict(hits or {})
        self.watches = dict(watches or {})
        self.at = at or dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def to_dict(self):
        return {
            "label": self.label,
            "index": self.index,
            "line": self.line,
            "depth": self.depth,
            "kind": self.kind,
            "name": self.name,
            "hits": dict(self.hits),
            "watches": dict(self.watches),
            "at": self.at,
        }

    @classmethod
    def from_dict(cls, data):
        """Rebuild a snapshot from its serialised form (never raises)."""
        data = data if isinstance(data, dict) else {}
        return cls(data.get("label", ""), data.get("index", 0),
                   data.get("line", 0), data.get("depth", 0),
                   data.get("kind", ""), data.get("name", ""),
                   data.get("hits"), data.get("watches"), data.get("at", ""))

    def text(self):
        """One line: ``[label] #index line N kind name``."""
        return "[%s] #%-4d line %-5d %-9s %s" % (
            self.label or "-", self.index, self.line, self.kind, self.name)

    def __repr__(self):
        return "SessionSnapshot(%r, index=%d, line=%d)" % (
            self.label, self.index, self.line)


def diff_snapshots(before, after):
    """What changed between two snapshots: ``(moved, hit_changes, watch_changes)``.

    Comparing two points of one session is how a user answers "what did that
    step actually do?", and it is the same comparison whether the two points are
    adjacent steps or hours apart.
    """
    before = before if isinstance(before, SessionSnapshot) else SessionSnapshot()
    after = after if isinstance(after, SessionSnapshot) else SessionSnapshot()
    moved = {
        "index": after.index - before.index,
        "line": after.line,
        "from_line": before.line,
        "depth": after.depth,
        "from_depth": before.depth,
        "kind": after.kind,
    }
    hit_changes = {}
    for key in set(before.hits) | set(after.hits):
        was, now = before.hits.get(key, 0), after.hits.get(key, 0)
        if was != now:
            hit_changes[key] = {"from": was, "to": now}
    watch_changes = {}
    for key in set(before.watches) | set(after.watches):
        was, now = before.watches.get(key), after.watches.get(key)
        if was != now:
            watch_changes[key] = {"from": was, "to": now}
    return moved, hit_changes, watch_changes


# ---------------------------------------------------------------------------
# The session runner
# ---------------------------------------------------------------------------
class SessionRunner(object):
    """A cursor over a recorded timeline, with stepping and snapshots.

    The runner owns no widgets and no files: it holds a recording (a list of
    events), a cursor, the breakpoints in play and the snapshots taken, and it
    answers the five step modes. Everything a view needs can be read from it,
    and everything a report needs can be serialised with `to_dict`.
    """

    def __init__(self, events=None, breakpoints=None, runner=None, watches=None):
        # `events` may be a CodeFlowRecording (it has `.events`) or a list.
        self.recording = runner
        if events is None and runner is not None:
            events = getattr(runner, "events", None)
        self.events = list(events or [])
        self.breakpoints = list(breakpoints or [])
        self.watches = list(watches or [])
        self.cursor = 0
        self.hits = {}
        self.snapshots = []
        self.transcript = []
        self.record("start")

    # --- position --------------------------------------------------------
    def current(self):
        """The event under the cursor, or None at the start / past the end."""
        if 0 <= self.cursor < len(self.events):
            return self.events[self.cursor]
        return None

    def at_start(self):
        return self.cursor <= 0

    def at_end(self):
        return self.cursor >= len(self.events)

    def position(self):
        """The cursor as a small dict (index, line, depth, kind, name).

        `index` is the *cursor*: where in the timeline the session is
        standing. It is set here rather than taken from the event, because a
        recorded event carries its own `index` field and the two are not the
        same number -- the event's is where it sits in the recording, the
        cursor's is where the session is. Returning the event's made the
        cursor un-restorable: a snapshot could not say where it was taken.

        The event's own position stays available under `event_index`, so both
        meanings are reachable and neither shadows the other.
        """
        event = self.current()
        if event is None:
            state = {"line": 0, "depth": 0, "kind": "", "name": ""}
        else:
            state = dict(event_state(event))
        state["event_index"] = state.get("index", 0)
        state["index"] = self.cursor
        state["total"] = len(self.events)
        return state

    def enabled_breakpoints(self):
        """The breakpoints that are switched on."""
        return [bp for bp in self.breakpoints if bp.get("enabled", True)]

    # --- stopping --------------------------------------------------------
    def stops_at(self, index):
        """True when any enabled breakpoint would stop on event ``index``.

        The check is what makes the conditions and the hit counts real: a
        breakpoint stops here only when its condition holds at this event, and
        its hit count is what the caller advances afterwards.
        """
        if not 0 <= index < len(self.events):
            return False
        event = self.events[index]
        for bp in self.enabled_breakpoints():
            kind = bp.get("kind", "line")
            line = bp.get("line", 0)
            state = event_state(event)
            # The line kinds land on their own line; the kinds that find their
            # own line (function, exception...) land on their target's event.
            if kind in ("line", "condition", "breakpoint"):
                if int(line or 0) != int(state.get("line", 0) or 0):
                    continue
            target = (bp.get("target") or "").strip()
            if target and kind not in ("line", "condition", "breakpoint"):
                if target.lower() not in str(state.get("name", "")).lower():
                    continue
            if not self.condition_holds(bp, event):
                continue
            # A hit-count request ("stop on the 3rd time") is honoured here.
            wanted = _as_number(bp.get("stop_after"))
            if wanted is not None and wanted > 0:
                if self.hits.get(self._bp_key(bp), 0) + 1 < wanted:
                    continue
            return True
        return False

    def condition_holds(self, bp, event):
        """Whether one breakpoint's condition holds at one event."""
        condition = (bp.get("condition") or "").strip()
        # A `condition`-kind breakpoint with no expression is a plain line
        # breakpoint; anything else is evaluated as written.
        if not condition:
            return True
        ok, _why = eval_condition(condition, event)
        return ok

    @staticmethod
    def _bp_key(bp):
        """A stable key for one breakpoint's hit count."""
        return "%s:%s:%s" % (bp.get("script", ""), bp.get("kind", "line"),
                             bp.get("line", bp.get("target", "")))

    def count_hits(self):
        """Credit a hit to every breakpoint that would stop at the cursor."""
        self.count_hits_at(self.cursor)

    def count_hits_at(self, index):
        """Credit a hit to every breakpoint matching the event at `index`.

        This is what makes a hit count real. A breakpoint's count is the number
        of times the session was *standing on* an event it matched -- not the
        number of times a step happened to pass near one, which is what a
        comparison against the post-move cursor would give.
        """
        if not 0 <= index < len(self.events):
            return 0
        event = self.events[index]
        state = event_state(event)
        credited = 0
        for bp in self.enabled_breakpoints():
            if bp.get("kind", "line") in ("line", "condition", "breakpoint"):
                if int(bp.get("line", 0) or 0) != int(state.get("line", 0) or 0):
                    continue
            else:
                target = (bp.get("target") or "").strip()
                if target and target.lower() not in str(
                        state.get("name", "")).lower():
                    continue
            if not self.condition_holds(bp, event):
                continue
            key = self._bp_key(bp)
            self.hits[key] = self.hits.get(key, 0) + 1
            credited += 1
        return credited

    def breakpoint_hits(self):
        """``{breakpoint key: hits}``, with the live counts applied.

        The breakpoints' own stored counts are folded in so a caller that only
        reads the records still sees the session's hits.
        """
        merged = {self._bp_key(bp): bp.get("hits", 0)
                  for bp in self.breakpoints}
        for key, value in self.hits.items():
            merged[key] = merged.get(key, 0) + value
        return merged

    # --- stepping --------------------------------------------------------
    def step(self, mode="over", line=None):
        """Move the cursor one step in `mode`; returns the event reached.

        Returns None when the move cannot be made (already at the end, or the
        mode needs a line it was not given), so a caller can say so rather than
        silently doing nothing.
        """
        mode = mode if mode in STEP_MODE_KEYS else "over"
        if self.at_end():
            return None
        start = self.cursor
        target = None
        if mode == "into":
            # `into` means the *next* event, whatever it is: a step-into that
            # skipped the annotations would not be a step into anything, and
            # the user asking for it wants to see every recorded line.
            target = start + 1 if start + 1 < len(self.events) else None
        elif mode == "over":
            target = self._step_over(start)
        elif mode == "out":
            target = self._step_out(start)
        elif mode == "breakpoint":
            target = self._next_stop_index(start + 1)
        elif mode == "line":
            wanted = _as_number(line)
            if wanted is None:
                return None
            target = self._next_line_index(start + 1, int(wanted))
        if target is None:
            return None
        self.cursor = target
        # The hit is credited to the event the cursor *arrived at*, which is
        # where the session is now standing. Crediting the event it left would
        # count a breakpoint the step had already gone past, which is why a
        # breakpoint that was genuinely reached reported zero hits.
        if mode in ("breakpoint", "over", "into", "out"):
            self.count_hits_at(self.cursor)
        self.record(mode)
        return self.current()

    def _next_flow_index(self, start):
        """The next index at or after `start` that is worth stopping on."""
        for index in range(start, len(self.events)):
            if self._is_flow(self.events[index]):
                return index
        return None

    def _is_flow(self, event):
        """True when an event is something a step should stop on.

        A timeline records annotations (definitions, imports) as well as flow.
        Stopping on those would make stepping useless, so only the flow kinds
        count — and when a timeline has no flow kinds at all, every event counts,
        because otherwise nothing could ever be stepped.
        """
        kind = str(event_state(event).get("kind", ""))
        if kind in STEP_KINDS:
            return True
        return not any(str(event_state(item).get("kind", "")) in STEP_KINDS
                       for item in self.events)

    def _step_over(self, start):
        """The next event at this depth or shallower."""
        depth = int(event_state(self.current()).get("depth", 0) or 0)
        for index in range(start + 1, len(self.events)):
            event = self.events[index]
            if not self._is_flow(event):
                continue
            if int(event_state(event).get("depth", 0) or 0) <= depth:
                return index
        # Nothing shallower ahead: fall back to the next flow event, so a step
        # at the end of a block still moves rather than refusing.
        return self._next_flow_index(start + 1)

    def _step_out(self, start):
        """The next event shallower than the depth the move started at.

        The comparison is against `self.cursor`, which still points at the
        event the step began from: comparing against the event under the cursor
        *after* a move would measure the wrong depth and step further in
        instead of out.
        """
        depth = int(event_state(self.events[self.cursor]).get("depth", 0) or 0)
        for index in range(start + 1, len(self.events)):
            state = event_state(self.events[index])
            # A leave-kind event ends the frame however the adapter recorded
            # its depth, so it is checked before the depth comparison.
            if str(state.get("kind", "")) in FRAME_LEAVE_KINDS:
                return index
            if int(state.get("depth", 0) or 0) < depth:
                return index
        return self._next_flow_index(start + 1)

    def _next_stop_index(self, start):
        """The next index an enabled breakpoint would stop on.

        The flow filter is *not* applied: a breakpoint on a line the timeline
        recorded as an annotation (a variable assignment, say) still has to
        stop there, because the breakpoint itself decided that event matters.
        """
        for index in range(start, len(self.events)):
            if self.stops_at(index):
                return index
        return None

    def _next_line_index(self, start, line):
        """The next index on `line` (or past it, so run-to-line always lands)."""
        for index in range(start, len(self.events)):
            state = event_state(self.events[index])
            if int(state.get("line", 0) or 0) >= line \
                    and self._is_flow(self.events[index]):
                return index
        return None

    # --- running ---------------------------------------------------------
    def run_to_end(self, limit=None):
        """Advance to the end (or `limit` steps); returns how many steps were made."""
        steps = 0
        while not self.at_end():
            if limit is not None and steps >= limit:
                break
            before = self.cursor
            self.step("into")
            if self.cursor == before:
                break
            steps += 1
        return steps

    def run_to_breakpoint(self, limit=None):
        """Advance to the next stop at or after the cursor; the event or None.

        Searches *forward only*, which is the honest reading of "continue": a
        breakpoint behind the session has already been passed and stopping on
        it again would be a rewind, not a run.
        """
        steps = 0
        while not self.at_end():
            if limit is not None and steps >= limit:
                break
            event = self.step("breakpoint")
            steps += 1
            if event is not None:
                return event
        return None

    def run_to_breakpoint_from_start(self, limit=None):
        """Restart from the beginning and advance to the first stop.

        This is the search a user expects after setting a breakpoint partway
        through a session: the breakpoint may be behind the cursor, and "run"
        should find it rather than silently report that there is nothing ahead.
        The cursor is rewound first, so the search covers the whole timeline.
        """
        self.rewind()
        return self.run_to_breakpoint(limit=limit)

    def next_breakpoint_ahead(self):
        """The first index ahead of the cursor that a breakpoint would stop on.

        Split out so the tab can tell "there is no breakpoint at all" from
        "there is one, but behind you", which are different messages.
        """
        for index in range(self.cursor + 1, len(self.events)):
            if self.stops_at(index):
                return index
        return None

    def any_breakpoint_reachable(self):
        """True when a stop exists anywhere in the timeline."""
        for index in range(len(self.events)):
            if self.stops_at(index):
                return True
        return False


    def rewind(self):
        """Return the cursor to the start (the snapshots are kept)."""
        self.cursor = 0
        self.record("rewind")
        return self.position()

    def step_back(self):
        """Move one event backwards, for a replay that goes both ways."""
        if self.cursor <= 0:
            return None
        for index in range(self.cursor - 1, -1, -1):
            if self._is_flow(self.events[index]):
                self.cursor = index
                self.record("back")
                return self.current()
        self.cursor = 0
        self.record("back")
        return self.current()

    # --- snapshots -------------------------------------------------------
    def snapshot(self, label=""):
        """Freeze the session where it is; returns the snapshot."""
        state = self.position()
        item = SessionSnapshot(
            label or "snap%d" % (len(self.snapshots) + 1),
            index=state.get("index", 0), line=state.get("line", 0),
            depth=state.get("depth", 0), kind=state.get("kind", ""),
            name=state.get("name", ""), hits=self.breakpoint_hits(),
            watches=self.watch_values())
        self.snapshots.append(item)
        # An unbounded list of frozen states would grow forever unread.
        if len(self.snapshots) > MAX_SNAPSHOTS:
            del self.snapshots[0]
        self.record("snapshot", item.label)
        return item

    def restore(self, snapshot):
        """Put the cursor back where a snapshot was taken."""
        item = (snapshot if isinstance(snapshot, SessionSnapshot)
                else SessionSnapshot.from_dict(snapshot))
        self.cursor = max(0, min(item.index, len(self.events)))
        for key, value in (item.hits or {}).items():
            self.hits[key] = value
        self.record("restore", item.label)
        return self.position()

    def snapshot_by_label(self, label):
        """The snapshot with a label (or None)."""
        for item in self.snapshots:
            if item.label == label:
                return item
        return None

    # --- watches ---------------------------------------------------------
    def watch_values(self):
        """The watch list resolved against the current event.

        A watch is resolved against the event's own fields first and against the
        variables captured in the session second, so a watch on a line number
        works without anything being captured, and a watch on a value works once
        it has been captured.
        """
        state = event_state(self.current())
        out = {}
        captured = getattr(self, "captured", None) or {}
        for expression in self.watches:
            text = str(expression or "").strip()
            if not text:
                continue
            if text in state:
                out[text] = state[text]
            elif text in captured:
                out[text] = captured[text]
            else:
                out[text] = "(unresolved)"
        return out

    # --- transcript ------------------------------------------------------
    def record(self, action, detail=""):
        """Append one step to the session transcript."""
        entry = {"action": action, "detail": detail or "",
                 "index": self.cursor,
                 "at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        self.transcript.append(entry)
        return entry

    def transcript_lines(self):
        """The transcript as ASCII lines (the pane and the report)."""
        return ["[%s] %-10s #%-4d %s" % (entry.get("at", ""),
                                         entry.get("action", ""),
                                         entry.get("index", 0),
                                         entry.get("detail", ""))
                for entry in self.transcript]

    # --- serialisation ---------------------------------------------------
    def to_dict(self):
        return {
            "cursor": self.cursor,
            "events": len(self.events),
            "hits": self.breakpoint_hits(),
            "snapshots": [item.to_dict() for item in self.snapshots],
            "transcript": list(self.transcript),
            "position": self.position(),
            "watches": self.watch_values(),
        }

    @classmethod
    def from_dict(cls, data, events=None, breakpoints=None, watches=None):
        """Rebuild a runner from ``to_dict`` output plus its timeline."""
        data = data if isinstance(data, dict) else {}
        runner = cls(events=events, breakpoints=breakpoints, watches=watches)
        runner.cursor = int(data.get("cursor", 0) or 0)
        runner.hits = dict(data.get("hits", {}) or {})
        runner.snapshots = [SessionSnapshot.from_dict(item)
                            for item in data.get("snapshots", []) or []]
        runner.transcript = list(data.get("transcript", []) or [])
        return runner

    def __repr__(self):
        return "SessionRunner(cursor=%d/%d, snapshots=%d)" % (
            self.cursor, len(self.events), len(self.snapshots))


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------
# (key, label key, fallback, what the replay does)
REPLAY_MODES = (
    ("step", "debug_replay_step", "Step by step",
     "emit one frame per step, so the session can be watched as it happened"),
    ("transcript", "debug_replay_transcript", "Transcript",
     "emit the whole session as a transcript in one pass"),
    ("backwards", "debug_replay_backwards", "Backwards",
     "walk the session from the end back to the start"),
)

REPLAY_MODE_KEYS = tuple(key for key, _lk, _f, _d in REPLAY_MODES)
REPLAY_MODE_LABELS = {key: fallback for key, _lk, fallback, _d in REPLAY_MODES}


def replay(runner, mode="transcript", limit=None):
    """Replay a session: the frames of the replay, as dicts.

    A replay is a *reconstruction* of the session from its own recording, not a
    re-execution: it reports where the cursor went and what it saw, which is
    what makes it reproducible. `limit` caps the frames so a huge recording can
    be replayed in slices.
    """
    if runner is None:
        return []
    frames = []
    if mode == "transcript":
        for entry in runner.transcript:
            frames.append({"action": entry.get("action", ""),
                           "index": entry.get("index", 0),
                           "detail": entry.get("detail", ""),
                           "at": entry.get("at", "")})
            if limit is not None and len(frames) >= limit:
                break
        return frames

    if mode == "backwards":
        # Walk from the end back, without disturbing the cursor the caller
        # holds: the replay is a reading of the session, not a move of it.
        saved = runner.cursor
        runner.cursor = len(runner.events)
        while runner.step_back() is not None:
            state = runner.position()
            frames.append({"action": "back", "index": state.get("index", 0),
                           "line": state.get("line", 0),
                           "kind": state.get("kind", "")})
            if limit is not None and len(frames) >= limit:
                break
        runner.cursor = saved
        return frames

    # Step by step: replay the cursor from the start, one step at a time.
    saved_snapshots = list(runner.snapshots)
    saved_hits = dict(runner.hits)
    saved_cursor = runner.cursor
    runner.cursor = 0
    while not runner.at_end():
        before = runner.cursor
        runner.step("into")
        if runner.cursor == before:
            break
        state = runner.position()
        frames.append({"action": "step", "index": state.get("index", 0),
                       "line": state.get("line", 0),
                       "kind": state.get("kind", "")})
        if limit is not None and len(frames) >= limit:
            break
    runner.cursor = saved_cursor
    runner.snapshots = saved_snapshots
    runner.hits = saved_hits
    return frames


def format_replay(frames, limit=60):
    """A replay as ASCII lines."""
    lines = ["Session replay", "==============",
             "frames: %d" % len(frames or [])]
    for frame in (frames or [])[:limit]:
        lines.append("#%-4d %-10s line %-5s %s" % (
            frame.get("index", 0), frame.get("action", ""),
            frame.get("line", "-"), frame.get("kind", "")))
    if frames and len(frames) > limit:
        lines.append("... and %d more frame(s)" % (len(frames) - limit))
    return lines


def format_stepping(runner):
    """A runner's state as ASCII lines (the stack pane)."""
    if runner is None:
        return ["(no session)"]
    state = runner.position()
    lines = ["Session position", "----------------",
             "cursor     : #%d of %d" % (state.get("index", 0),
                                         state.get("total", 0)),
             "line       : %s" % state.get("line", "-"),
             "depth      : %s" % state.get("depth", "-"),
             "kind       : %s" % (state.get("kind") or "-"),
             "name       : %s" % (state.get("name") or "-"),
             "snapshots  : %d" % len(runner.snapshots)]
    hits = runner.breakpoint_hits()
    if hits:
        lines.append("hits       : " + ", ".join(
            "%s=%d" % (key.split(":")[-1], value)
            for key, value in sorted(hits.items())))
    return lines


def stepping_capabilities():
    """Describe the stepping model for the audit / about layer."""
    return {
        "step_modes": list(STEP_MODE_KEYS),
        "step_labels": dict(STEP_MODE_LABELS),
        "replay_modes": list(REPLAY_MODE_KEYS),
        "session_features": list(SESSION_FEATURE_KEYS),
        "condition_fields": list(CONDITION_FIELDS),
        "condition_operators": [op for op in CONDITION_OPERATORS],
        "max_snapshots": MAX_SNAPSHOTS,
        "step_kinds": list(STEP_KINDS),
        "evaluates_without_eval": True,
        "offline": True,
        "ascii_only": True,
        "zero_dependency": True,
    }
