# -*- coding: utf-8 -*-
"""Flow Visualizer for the Debugger tab -- eight ASCII views of a recording.

This module turns a `tabs.code_flow.CodeFlowRecording` into ASCII-friendly
pictures of the run, so the whole execution can be read at a glance without any
graphics toolkit:

    from tabs import code_flow, flow_view

    recording = code_flow.record(script)
    print(flow_view.render(recording, "timeline"))   # the full timeline
    print(flow_view.render(recording, "call_tree"))  # the call tree
    flow_view.views()                                # every view key + title

The eight views:

    timeline        every event in execution order, one line each
    call_tree       the call hierarchy as an indented tree
    call_stack      the frames of the call stack, innermost first
    block_map       every block, nested by depth, with its line
    function_map    every function: lines, calls made, calls received
    class_map       every class with its methods and their line ranges
    module_map      imports and what the module defines / uses
    exception_map   every exception, with the calls leading to it

Two shapes carry the data between views: `Node` (an entry in a hierarchy) and
`MapRow` (a row in a table). Both are plain records, so a view is testable and
a caller can lay the data out however it likes -- `render()` is only one such
layout, and `render_ascii()` is the boxed form used by the tab.

Everything here is derived from the recording: no Qt, no I/O, no re-parsing of
the script beyond what the recording already holds.
"""

# The view keys, in the order the tab lists them, with their titles.
VIEWS = (
    ("timeline", "Timeline"),
    ("call_tree", "Call tree"),
    ("call_stack", "Call stack"),
    ("block_map", "Block map"),
    ("function_map", "Function map"),
    ("class_map", "Class map"),
    ("module_map", "Module map"),
    ("exception_map", "Exception map"),
)

# The width of the widest ASCII picture before it is clipped.
MAX_WIDTH = 100
# The deepest tree the renderer will print (a pathological nesting guard).
MAX_TREE_DEPTH = 30


# ---------------------------------------------------------------------------
# Records the views hand back
# ---------------------------------------------------------------------------
class Node(object):
    """One entry in a hierarchy (a call, a frame, a block).

    ``children`` holds the nested entries, ``depth`` is the level the node sits
    at (0 for a root) and ``detail`` is the extra text a view wants to show
    beside the label (a line number, a count, an argument list...).
    """

    __slots__ = ("label", "detail", "children", "depth", "kind", "line")

    def __init__(self, label, detail="", depth=0, kind="", line=0):
        self.label = label or ""
        self.detail = detail or ""
        self.children = []
        self.depth = int(depth or 0)
        self.kind = kind or ""
        self.line = int(line or 0)

    def add(self, node):
        self.children.append(node)
        return node

    def child(self, label, detail="", kind="", line=0):
        return self.add(Node(label, detail, self.depth + 1, kind, line))

    def walk(self):
        """This node then every descendant, depth first."""
        yield self
        for child in self.children:
            for node in child.walk():
                yield node

    def count(self):
        """How many nodes the subtree holds (this node included)."""
        return len(list(self.walk()))

    def to_dict(self):
        return {
            "label": self.label,
            "detail": self.detail,
            "depth": self.depth,
            "kind": self.kind,
            "line": self.line,
            "children": [child.to_dict() for child in self.children],
        }

    def __repr__(self):
        return "Node(%r, %d child/ren)" % (self.label, len(self.children))


class MapRow(object):
    """One row of a map: a name, a line range and a handful of counters."""

    __slots__ = ("name", "kind", "start", "end", "depth", "counts", "note")

    def __init__(self, name, kind="", start=0, end=0, depth=0, counts=None,
                 note=""):
        self.name = name or ""
        self.kind = kind or ""
        self.start = int(start or 0)
        self.end = int(end or 0)
        self.depth = int(depth or 0)
        self.counts = dict(counts or {})
        self.note = note or ""

    @property
    def lines(self):
        """How many source lines the row spans (at least one)."""
        if not self.start:
            return 0
        if not self.end or self.end < self.start:
            return 1
        return self.end - self.start + 1

    def span(self):
        """The line range as text (``"42-58"``, ``"42"`` or ``""``)."""
        if not self.start:
            return ""
        if not self.end or self.end == self.start:
            return "%d" % self.start
        return "%d-%d" % (self.start, self.end)

    def to_dict(self):
        return {
            "name": self.name,
            "kind": self.kind,
            "start": self.start,
            "end": self.end,
            "depth": self.depth,
            "lines": self.lines,
            "counts": dict(self.counts),
            "note": self.note,
        }

    def __repr__(self):
        return "MapRow(%r, %s)" % (self.name, self.span() or "?")


class View(object):
    """A rendered view: its key, title, the picture and the raw data."""

    __slots__ = ("key", "title", "lines", "data")

    def __init__(self, key, title, lines, data=None):
        self.key = key
        self.title = title
        self.lines = list(lines or [])
        self.data = data

    def text(self):
        """The picture as one string."""
        return "\n".join(self.lines)

    def __len__(self):
        return len(self.lines)

    def __repr__(self):
        return "View(%r, %d line(s))" % (self.key, len(self.lines))


# ---------------------------------------------------------------------------
# Small text helpers
# ---------------------------------------------------------------------------
def _clip(text, width=MAX_WIDTH):
    """Shorten a line to `width`, marking that it was cut."""
    text = (text or "").replace("\t", "    ").rstrip()
    if width <= 0 or len(text) <= width:
        return text
    if width <= 3:
        return text[:width]
    return text[:width - 3] + "..."


def _rule(title, width=MAX_WIDTH, mark="="):
    """A titled rule: ``==== Title ========================``."""
    head = "%s %s " % (mark * 3, title)
    if len(head) >= width:
        return _clip(head, width)
    return head + mark * (width - len(head))


def _bar(value, top, width=24, mark="#"):
    """A proportional ASCII bar (used by the maps for their counters)."""
    if not top or not value:
        return ""
    filled = max(1, int(round(width * float(value) / float(top))))
    return mark * filled



# ---------------------------------------------------------------------------
# Reading a recording
# ---------------------------------------------------------------------------
def _events(recording, kind=None):
    """The recording's events, optionally only one kind."""
    if recording is None:
        return []
    return recording.for_kind(kind) if kind else recording.timeline()


def _functions(recording):
    """Every definition block: (name, line), in source order."""
    out = []
    for event in _events(recording, "block"):
        if event.block == "def" and event.name:
            out.append((event.name, event.line))
    return out


def _classes(recording):
    """Every class block: (name, line), in source order."""
    out = []
    for event in _events(recording, "block"):
        if event.block == "class" and event.name:
            out.append((event.name, event.line))
    return out




def _hierarchy_events(recording, kinds=("block", "call", "method", "log",
                                        "exception", "error")):
    """The events that build the call tree, in timeline order."""
    wanted = set(kinds)
    return [event for event in _events(recording) if event.kind in wanted]


# ---------------------------------------------------------------------------
# The eight views
# ---------------------------------------------------------------------------
def view_timeline(recording, filter_kind=""):
    """Every event in execution order, one line each.

    The header carries the script, the language and the counts, so the picture
    is self-describing when it is pasted into a report.
    """
    lines = [
        _rule("Timeline  %s  [%s]" % (recording.script or "(unnamed)",
                                      recording.language or "?")),
        "%-6s %-6s %-10s %-6s %-22s %s" % (
            "#", "line", "kind", "depth", "block/name", "source"),
        "-" * MAX_WIDTH,
    ]
    events = _events(recording, filter_kind)
    for event in events:
        symbol = event.block or event.name
        depth = ("%d" % event.depth) if event.kind == "block" else ""
        lines.append(_clip("%-6d %-6d %-10s %-6s %-22s %s" % (
            event.index, event.line, event.kind, depth, symbol,
            (event.detail or "")[:40])))
    if not events:
        lines.append("(no events)")
    lines.append("-" * MAX_WIDTH)
    lines.append(recording.summary())
    return View("timeline", "Timeline", lines, events)


def build_call_tree(recording):
    """The call tree as a `Node` root (the run) with nested entries.

    A block that opens (``def`` / ``class`` / ``if`` / ``for`` ...) becomes a
    parent, and the calls, logs and exceptions inside it become its children.
    The indentation of the picture is therefore the nesting of the run, which
    is what makes a call tree readable without a graphics toolkit.
    """
    root = Node(recording.script or "(run)", kind="run", line=0)
    # `open_nodes[level]` is the block currently open at that nesting level, so
    # an event at depth N always attaches to `open_nodes[N]` -- the block that
    # contains it. That is what makes the drawn tree mirror the run.
    open_nodes = [root]
    for event in _hierarchy_events(recording):
        depth = max(0, event.depth)
        # A block opens at its own depth: it is a child of the block one level
        # up, and it then becomes the parent for everything inside it.
        parent = open_nodes[min(depth, len(open_nodes) - 1)]
        if event.kind == "block":
            label = ("%s %s" % (event.block, event.name or "")).strip()
            node = Node(label, "line %d" % event.line, parent.depth + 1,
                        event.kind, event.line)
            parent.add(node)
            del open_nodes[depth + 1:]
            open_nodes.append(node)
        else:
            detail = "line %d" % event.line
            if event.name:
                detail = "%s  line %d" % (event.name, event.line)
            leaf = Node(event.kind, detail, parent.depth + 1, event.kind,
                        event.line)
            parent.add(leaf)
    return root


def view_call_tree(recording):
    """The call tree drawn as an indented ASCII tree."""
    root = build_call_tree(recording)
    lines = [_rule("Call tree  %s" % (recording.script or "(unnamed)")),
             root.label]
    for node in root.walk():
        if node is root:
            continue
        if node.depth > MAX_TREE_DEPTH:
            continue
        indent = "  " * (node.depth - 1)
        branch = "|- " if node.children else "|  "
        label = "%s%s%s" % (indent, branch, node.label)
        text = "%-46s %s" % (label, node.detail)
        lines.append(_clip(text))
    if len(lines) <= 2:
        lines.append("(no calls recorded)")
    return View("call_tree", "Call tree", lines, root)


def build_call_stack(recording, index=None):
    """The call stack as a list of frames, innermost first.

    `index` is a 1-based timeline position: the stack is the chain of blocks
    that were open when that event happened. With no index the deepest point of
    the run is used, which is where a debugger would have stopped.
    """
    events = _events(recording)
    if not events:
        return []
    target = index
    if target is None:
        # The deepest event is the most interesting pause point.
        target = max((event.index for event in events),
                     key=lambda i: (events[i - 1].depth, i))
    target = max(1, min(len(events), int(target)))
    # Walk back from the target, keeping the blocks still open.
    frames = []
    wanted_depth = events[target - 1].depth
    for event in reversed(events[:target]):
        if event.kind != "block":
            continue
        if event.depth <= wanted_depth and event.block != "if":
            frames.append(event)
        if len(frames) >= 8:
            break
    frames.reverse()
    return frames


def view_call_stack(recording, index=None):
    """The call stack frames, innermost first, as ASCII."""
    frames = build_call_stack(recording, index)
    lines = [_rule("Call stack  %s" % (recording.script or "(unnamed)")),
             "%-6s %-10s %-24s %s" % ("frame", "kind", "name", "line"),
             "-" * MAX_WIDTH]
    if not frames:
        lines.append("(no frames -- the run has no open blocks)")
    for position, event in enumerate(reversed(frames)):
        marker = "  <- current" if position == 0 else ""
        lines.append(_clip("#%-5d %-10s %-24s line %d%s" % (
            position, event.block, event.name or "-", event.line, marker)))
    lines.append("-" * MAX_WIDTH)
    lines.append("%d frame(s) open" % len(frames))
    return View("call_stack", "Call stack", lines, frames)


def build_block_map(recording):
    """Every block as a `MapRow`, nested by depth and in source order."""
    rows = []
    blocks = _events(recording, "block")
    # Close each block on the next block at the same or a shallower depth.
    for position, event in enumerate(blocks):
        end = 0
        for other in blocks[position + 1:]:
            if other.depth <= event.depth:
                end = max(event.line, other.line - 1)
                break
        rows.append(MapRow(event.name or event.block, event.block,
                           event.line, end, event.depth,
                           {"depth": event.depth}, note=event.detail))
    return rows


def view_block_map(recording):
    """The blocks drawn as a nested tree with their line spans."""
    rows = build_block_map(recording)
    lines = [_rule("Block map  %s" % (recording.script or "(unnamed)")),
             "%-5s %-10s %-10s %-30s %s" % (
                 "line", "span", "kind", "block", "depth"),
             "-" * MAX_WIDTH]
    if not rows:
        lines.append("(no blocks recorded)")
    for row in rows:
        indent = "  " * row.depth
        lines.append(_clip("%-5d %-10s %-10s %s%-30s %d" % (
            row.start, row.span(), row.kind, indent,
            "%s %s" % (row.kind, (row.name or "-"))[:28], row.depth)))
    lines.append("-" * MAX_WIDTH)
    counts = recording.block_counts()
    lines.append("blocks: %s" % (", ".join(
        "%s=%d" % (key, counts[key]) for key in sorted(counts)) or "none"))
    lines.append("max depth: %d" % recording.max_depth())
    return View("block_map", "Block map", lines, rows)


def build_function_map(recording):
    """Every function with its line range and its call counters.

    ``counts`` holds the calls the function *makes* and the calls it
    *receives*, which is what makes the map a dependency view and not just a
    list of names.
    """
    functions = _functions(recording)
    rows = []
    events = _events(recording)
    for position, (name, line) in enumerate(functions):
        # The function ends at the next function (or class) definition.
        later = [other_line for other_name, other_line in functions[position + 1:]
                 if other_line > line]
        later += [other_line for _n, other_line in _classes(recording)
                  if other_line > line]
        end = (min(later) - 1) if later else 0
        calls_made = sum(
            1 for event in events
            if event.kind in ("call", "method") and line <= event.line
            and (not end or event.line <= end))
        received = sum(1 for event in events
                       if event.kind in ("call", "method")
                       and event.name == name)
        rows.append(MapRow(name, "def", line, end, 0,
                           {"calls": calls_made, "callers": received}))
    return rows


def view_function_map(recording):
    """The functions with a bar for how much each one calls."""
    rows = build_function_map(recording)
    lines = [_rule("Function map  %s" % (recording.script or "(unnamed)")),
             "%-24s %-10s %-7s %-8s %-8s %s" % (
                 "function", "lines", "calls", "received", "size", "bar"),
             "-" * MAX_WIDTH]
    if not rows:
        lines.append("(no functions recorded)")
    top = max((row.counts.get("calls", 0) for row in rows), default=0)
    for row in rows:
        lines.append(_clip("%-24s %-10s %-7d %-8d %-8d %s" % (
            (row.name or "-")[:24], row.span() or "-",
            row.counts.get("calls", 0), row.counts.get("callers", 0),
            row.lines, _bar(row.counts.get("calls", 0), top, 20))))
    lines.append("-" * MAX_WIDTH)
    lines.append("%d function(s)" % len(rows))
    return View("function_map", "Function map", lines, rows)


def build_class_map(recording):
    """Every class with its methods and their line ranges.

    A class owns the functions that sit between it and the next class (or the
    end of the document), which mirrors how the language scopes them.
    """
    classes = _classes(recording)
    functions = _functions(recording)
    rows = []
    for position, (name, line) in enumerate(classes):
        later = [other for other_name, other in classes[position + 1:]
                 if other > line]
        end = (min(later) - 1) if later else 0
        methods = [(fname, fline) for fname, fline in functions
                   if fline > line and (not end or fline <= end)]
        row = MapRow(name, "class", line, end, 0,
                     {"methods": len(methods)})
        row.note = "; ".join("%s@%d" % (fname, fline)
                             for fname, fline in methods) or "(no methods)"
        rows.append(row)
    return rows


def view_class_map(recording):
    """The classes with their methods listed beneath them."""
    rows = build_class_map(recording)
    lines = [_rule("Class map  %s" % (recording.script or "(unnamed)")),
             "%-24s %-10s %-8s %s" % ("class", "span", "methods", "members"),
             "-" * MAX_WIDTH]
    if not rows:
        lines.append("(no classes recorded)")
    for row in rows:
        lines.append(_clip("%-24s %-10s %-8d %s" % (
            (row.name or "-")[:24], row.span() or "-",
            row.counts.get("methods", 0), row.note)))
    lines.append("-" * MAX_WIDTH)
    lines.append("%d class(es)" % len(rows))
    return View("class_map", "Class map", lines, rows)


def build_module_map(recording):
    """What the module imports, defines and uses, as `MapRow` entries.

    The three sections are the three things a debugger asks of a module: its
    dependencies, its own symbols and the symbols it reaches for.
    """
    rows = []
    for event in _events(recording, "import"):
        rows.append(MapRow(event.name or event.detail, "import", event.line,
                           0, 0, {}, note="line %d" % event.line))
    for name, line in _functions(recording) + _classes(recording):
        rows.append(MapRow(name, "definition", line, 0, 0, {},
                           note="line %d" % line))
    used = {}
    for event in _events(recording):
        if event.kind in ("call", "method") and event.name:
            used[event.name] = used.get(event.name, 0) + 1
    for name in sorted(used):
        rows.append(MapRow(name, "used", 0, 0, 0, {"calls": used[name]},
                           note="%d call(s)" % used[name]))
    return rows


def view_module_map(recording):
    """The module split into imports, definitions and usages."""
    rows = build_module_map(recording)
    groups = (("import", "Imports"), ("definition", "Defined here"),
              ("used", "Used here"))
    lines = [_rule("Module map  %s  [%s]" % (
        recording.script or "(unnamed)", recording.language or "?"))]
    for kind, title in groups:
        section = [row for row in rows if row.kind == kind]
        lines.append("")
        lines.append("%s (%d)" % (title, len(section)))
        lines.append("-" * max(12, len(title) + 8))
        if not section:
            lines.append("  (none)")
        for row in section:
            lines.append(_clip("  %-28s %s" % (
                (row.name or "-")[:28], row.note)))
    lines.append("")
    lines.append("%d import(s), %d definition(s), %d used symbol(s)" % (
        sum(1 for row in rows if row.kind == "import"),
        sum(1 for row in rows if row.kind == "definition"),
        sum(1 for row in rows if row.kind == "used")))
    return View("module_map", "Module map", lines, rows)


def build_exception_map(recording):
    """Every exception with the calls that led to it.

    Each row carries the call chain in the event's depth order, so an exception
    reads as "this was thrown here, from inside that".
    """
    events = _events(recording)
    rows = []
    for event in events:
        if event.kind not in ("exception", "error"):
            continue
        # The blocks still open at the throw describe the path that reached it.
        path = []
        for earlier in reversed(events[:event.index - 1]):
            if earlier.kind == "block" and earlier.depth <= event.depth:
                path.append(earlier.name or earlier.block)
            if len(path) >= 4:
                break
        path.reverse()
        row = MapRow(event.name or event.kind, event.kind, event.line, 0,
                     event.depth, {}, note=" <- ".join(path) or "(top level)")
        rows.append(row)
    return rows


def view_exception_map(recording):
    """The exceptions with the path that reached each one."""
    rows = build_exception_map(recording)
    lines = [_rule("Exception map  %s" % (recording.script or "(unnamed)")),
             "%-6s %-11s %-22s %s" % ("line", "kind", "name", "reached by"),
             "-" * MAX_WIDTH]
    if not rows:
        lines.append("(no exceptions recorded)")
    for row in rows:
        lines.append(_clip("%-6d %-11s %-22s %s" % (
            row.start, row.kind, (row.name or "-")[:22], row.note)))
    lines.append("-" * MAX_WIDTH)
    lines.append("%d exception(s), %d warning(s)" % (
        len(rows), len(_events(recording, "warning"))))
    return View("exception_map", "Exception map", lines, rows)


# ---------------------------------------------------------------------------
# The dispatcher
# ---------------------------------------------------------------------------
BUILDERS = {
    "timeline": view_timeline,
    "call_tree": view_call_tree,
    "call_stack": view_call_stack,
    "block_map": view_block_map,
    "function_map": view_function_map,
    "class_map": view_class_map,
    "module_map": view_module_map,
    "exception_map": view_exception_map,
}


def views():
    """``(key, title)`` pairs for every view, in picker order."""
    return [(key, title) for key, title in VIEWS]


def keys():
    """Every view key, in picker order."""
    return [key for key, _title in VIEWS]


def title_for(key):
    """The title of a key (or the key itself when unknown)."""
    for candidate, title in VIEWS:
        if candidate == key:
            return title
    return key


def build(recording, key="timeline", **options):
    """Build one view without drawing it, returning the `View` record.

    An unknown key falls back to the timeline rather than raising, so a caller
    can pass whatever the UI holds.
    """
    builder = BUILDERS.get(key) or view_timeline
    try:
        return builder(recording, **options)
    except TypeError:
        # A view that takes no options (e.g. function_map).
        return builder(recording)


def render(recording, key="timeline", **options):
    """The ASCII picture of one view, as a list of lines."""
    return build(recording, key, **options).lines


def render_all(recording):
    """Every view, one after another, as a single ASCII document.

    This is what a report writes to disk: all eight pictures in picker order.
    """
    lines = []
    for key in keys():
        lines.append("")
        lines.extend(render(recording, key))
    return lines


def overview(recording):
    """A one-screen summary: the headline counts plus the block breakdown."""
    counts = recording.counts()
    lines = [
        _rule("Flow overview  %s  [%s]" % (
            recording.script or "(unnamed)", recording.language or "?")),
        "events     : %d" % len(recording),
        "max depth  : %d" % recording.max_depth(),
        "steps      : %d" % counts.get("step", 0),
        "blocks     : %d  (%s)" % (
            len(_events(recording, "block")),
            ", ".join("%s=%d" % (key, value) for key, value
                      in sorted(recording.block_counts().items())) or "none"),
        "calls      : %d  (method %d)" % (
            counts.get("call", 0), counts.get("method", 0)),
        "imports    : %d" % counts.get("import", 0),
        "variables  : %d" % counts.get("variable", 0),
        "exceptions : %d" % counts.get("exception", 0),
        "errors     : %d" % counts.get("error", 0),
        "warnings   : %d" % counts.get("warning", 0),
        "logs       : %d" % counts.get("log", 0),
        "",
        recording.summary(),
    ]
    return View("overview", "Overview", lines, counts)


def flow_view_capabilities():
    """Describe the visualizer for the audit / about layer."""
    return {
        "views": keys(),
        "max_width": MAX_WIDTH,
        "ascii": True,
        "offline": True,
    }
