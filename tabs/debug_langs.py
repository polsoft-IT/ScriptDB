# -*- coding: utf-8 -*-
"""Multilingual debugger engine for the Debugger tab.

One small engine that knows how ten language families are *written*, so the
Debugger tab can inspect any script in the database the same way: where the
code blocks are, which frames a breakpoint line lands in, how an expression is
validated, how a syntax error is reported, and which code-flow events the
document contains. Every language is an adapter, and an adapter is
hot-pluggable at run time:

    from tabs import debug_langs

    debug_langs.keys()                       # every registered language key
    debug_langs.languages()                  # (key, label) pairs, picker order
    debug_langs.adapter_for("Python")        # script category -> adapter
    debug_langs.register_adapter(MyAdapter)  # add / override one at run time
    debug_langs.unregister_adapter("lua")    # and take it away again
    debug_langs.load_plugin("my_debug_pack") # import + auto-register a plugin

The shipped adapters cover:

    python      Python
    javascript  JavaScript / Node
    cpp         C++
    java        Java
    powershell  PowerShell
    batch       Batch / CMD
    vbscript    VBScript
    shell       Shell (bash / sh)
    json        JSON
    yaml        YAML

`flow_events()` is the code-flow view of a document: every block, call, method
call, import, variable write, throw, error, warning and log statement the
adapter can see, each pinned to its source line. `tabs/code_flow.py` turns that
into the ordered execution timeline the Debugger records.

Annotations are deliberately conservative. The adapters are a *debugger*
description of a language, not a parser: they answer "what does the debugger
need to know about this file", so a marked annotation is always preferred over
a guess. The pattern-based adapters therefore report a warning (`pattern` in
the event's `source`) rather than claiming a certainty they cannot have.

Like the search and navigation engines this module imports no Qt, so it is
testable and usable from a shell, and it never runs any of the code it reads.
"""

import importlib
import json
import re

# ---------------------------------------------------------------------------
# Kinds of annotation an adapter reports in a debug overview.
# ---------------------------------------------------------------------------
ANNOTATION_KINDS = ("definition", "class", "variable", "import",
                    "breakpoint", "error", "warning", "unsupported")

# ---------------------------------------------------------------------------
# The kinds of event a code-flow recording is made of: an execution step per
# line, every block it enters, every call and method call, every import, every
# variable change, every exception, every error and warning, and every log
# statement. Together they form the full execution timeline.
# ---------------------------------------------------------------------------
FLOW_KINDS = ("step", "block", "call", "method", "import", "variable",
              "exception", "error", "warning", "log")

# The block kinds a flow recording reports (a block event carries one as its
# `block` field). `jump` / `call` cover the label languages, such as Batch.
BLOCK_KINDS = ("if", "loop", "with", "try", "catch", "switch", "jump", "call")

# What a validated document can be: a structure we can read (kind "document"),
# code in the adapter's language (kind "code"), or nothing usable.
DOCUMENT_KIND = "document"
CODE_KIND = "code"

# Very small marker blow-up guard: a document is never walked deeper than this.
_MAX_DEPTH = 24
# How many characters of a source line a flow event carries.
DETAIL_LIMIT = 200


# ---------------------------------------------------------------------------
# The adapter protocol
# ---------------------------------------------------------------------------
class LanguageAdapter(object):
    """How one language is written, as far as the debugger cares.

    Attributes
    ----------
    key / label
        identity of the adapter (``"python"`` / ``"Python"``).
    aliases / extensions
        lower-case strings that resolve a script category / file name to it.
    block_markers
        ``(regex, kind)`` pairs matched against a stripped line; the first
        match wins and marks a **certain** annotation.
    block_markers_soft
        the same, but a match is a pattern guess, reported with source
        ``"pattern"`` rather than presented as a certainty.
    markers
        ``(regex, kind)`` pairs for imports, global variables and breakpoint
        markers -- annotations that are not blocks.
    flow_markers
        ``(regex, kind, group, soft)`` pairs for the code-flow events that are
        not blocks, imports or variables: calls, method calls, throws, errors,
        warnings and log statements. `group` is the capture group holding the
        name (0 for the whole match).
    comment_markers
        lower-case markers that start an ignorable tail (``"#"``, ``"rem"``).
    """

    key = "python"
    label = "Python"
    aliases = ()
    extensions = ()
    block_markers = ()
    block_markers_soft = ()
    markers = ()
    flow_markers = ()
    comment_markers = ()
    max_recommended_line = 200
    # True when the language scopes a block with braces (C++, Java, JS...).
    # An indentation language (Python, Batch, Shell) sets it False and the
    # block depth is then derived from the indentation of the headers.
    brace_blocks = True
    # True when the language has no runtime the debugger can step: the adapter
    # validates the text only and says so.
    validation_only = False
    # True when the language is a data format rather than executable code.
    data_format = False
    notes = ""

    def __init__(self, text=""):
        self.text = text or ""

    def __repr__(self):
        return "%s(%d line(s))" % (type(self).__name__,
                                   len(self.text.splitlines()))

    # ------------------------------------------------------------ identity
    def describe(self):
        """A small, JSON-friendly summary (menus, reports, tests)."""
        return {
            "key": self.key,
            "label": self.label,
            "aliases": list(self.aliases),
            "extensions": list(self.extensions),
            "blocks": len(self.block_markers) + len(self.block_markers_soft),
            "flow_markers": len(self.flow_markers),
            "validates_document": self.data_format,
            "validates_code": not self.data_format,
        }

    # -------------------------------------------------------------- blocks
    def block_patterns(self):
        """Every marker as ``(regex, kind, soft)`` in priority order."""
        patterns = []
        for regex, kind in self.block_markers:
            patterns.append((regex, kind, False))
        for regex, kind in self.block_markers_soft:
            patterns.append((regex, kind, True))
        return patterns

    def blocks(self):
        """The block annotations of a document, in document order.

        A block is a line that opens a body the debugger can pause inside
        (``def``, ``class``, ``if``, ``function``, ``sub``, ...). Certain
        annotations come first; a soft (pattern-guessed) line is only reported
        for a position no certain block has already claimed.
        """
        certain = {}
        soft = {}
        stack = 0
        stack_indents = []
        patterns = self.block_patterns()
        for number, raw in enumerate((self.text or "").splitlines(), 1):
            stripped = raw.strip()
            if not stripped or self.is_comment(raw):
                continue
            hit = None
            for regex, kind, is_soft in patterns:
                match = regex.match(stripped)
                if match:
                    hit = (kind, is_soft, match)
                    break
            if hit is None:
                continue
            kind, is_soft, match = hit
            name = (match.group(1) if match.groups() else kind).strip()
            annotation = {
                "line": number,
                "kind": kind,
                "name": name,
                "text": stripped[:200],
                "depth": stack,
                "source": "pattern" if is_soft else "marker",
            }
            target = soft if is_soft else certain
            if number not in target:
                target[number] = annotation
            # Track how deeply blocks nest. A brace language opens a level on
            # a line ending with '{' and closes it on a lone '}'; an
            # indentation language (Python, Batch, Shell) opens a level on
            # every block header and closes it when the indentation returns,
            # which is read from a stack of the header indentations.
            # The annotation's own `depth` is the level it *sits at*, so it is
            # written after the block has been opened and the stack unwound.
            if self.brace_blocks:
                if raw.rstrip().endswith("{"):
                    stack += 1
                elif raw.strip() == "}":
                    stack = max(0, stack - 1)
            else:
                header_indent = len(raw) - len(raw.lstrip())
                # Unwind the blocks the new header has stepped out of: a
                # header at the same or a shallower indentation than an open
                # block closes that block (and everything inside it).
                while stack_indents and header_indent <= stack_indents[-1]:
                    stack_indents.pop()
                    stack = max(0, stack - 1)
                annotation["depth"] = stack
                # A block's *body* is indented further than its own header,
                # so the header's own level is recorded for the next header.
                stack_indents.append(header_indent)
                stack = len(stack_indents)
        merged = []
        for number in sorted(set(certain) | set(soft)):
            merged.append(certain.get(number) or soft[number])
        return merged

    def marker_annotations(self):
        """Imports, global variables and breakpoint markers, in order."""
        out = []
        for number, raw in enumerate((self.text or "").splitlines(), 1):
            stripped = raw.strip()
            if not stripped or self.is_comment(raw):
                continue
            for regex, kind in self.markers:
                match = regex.search(stripped)
                if not match:
                    continue
                name = (match.group(1) if match.groups() else stripped[:60])
                out.append({"line": number, "kind": kind, "name": name.strip(),
                            "text": stripped[:200], "source": "marker"})
                break
        return out

    def is_breakpoint_line(self, line):
        """True when the line carries a debugger stop marker (``# break``)."""
        for annotation in self.marker_annotations():
            if annotation["line"] == line \
                    and annotation["kind"] == "breakpoint":
                return True
        return False

    def breakpoint_hint(self, line):
        """What the adapter would stop on at ``line`` (``""`` when nothing)."""
        for annotation in self.blocks():
            if annotation["line"] == line:
                return annotation["name"]
        for annotation in self.marker_annotations():
            if annotation["line"] == line:
                return annotation["name"]
        return ""

    def line_comment_marker(self):
        """The marker that starts a comment (``"#"``, ``"//"``, ``"rem"``)."""
        return self.comment_markers[0] if self.comment_markers else ""

    def is_comment(self, line):
        """True when a line is (only) a comment."""
        stripped = (line or "").strip().lower()
        return bool(stripped) and any(stripped.startswith(marker)
                                      for marker in self.comment_markers)

    def is_warning(self, line):
        """True when a line is a warning marker.

        A warning lives in a comment, and a comment does not execute, so
        a warning is not a step: it is recorded as its own flow event
        instead, which puts a TODO on the timeline at the line it sits
        on. The markers are TODO, FIXME, XXX, HACK, WARNING and
        DEPRECATED, whichever comment syntax the language uses.
        """
        stripped = (line or "").strip().lower()
        marker = stripped
        for comment in self.comment_markers:
            if stripped.startswith(comment):
                marker = stripped[len(comment):].strip()
                break
        return marker.startswith(("todo", "fixme", "xxx", "hack",
                                  "warning", "deprecated"))

    # ---------------------------------------------------------------- flow
    def flow_events(self):
        """The code-flow events the adapter can see in the document.

        Each event is a dict with ``line``, ``kind`` (one of FLOW_KINDS),
        ``name``, ``detail`` and ``source``. Blocks and markers come from the
        annotation tables; ``flow_markers`` adds calls, method calls, throws,
        errors, warnings and log statements. The events are in document order,
        which the recorder then turns into an execution order.
        """
        events = []
        # A definition or class is a *frame*, not a control block, so it gets
        # its own block label rather than being reported as an `if`.
        frame_labels = {"definition": "def", "class": "class"}
        for annotation in self.blocks():
            kind = annotation.get("kind")
            if kind in frame_labels:
                block = frame_labels[kind]
            elif kind in BLOCK_KINDS:
                block = kind
            else:
                block = "if"
            events.append({
                "line": annotation["line"],
                "kind": "block",
                "block": block,
                "name": annotation.get("name", ""),
                "detail": annotation.get("text", ""),
                # The nesting level the block sits at, as resolved by the
                # adapter from the language's own structure. The recorder
                # relies on this to draw the block map and the call tree.
                "depth": annotation.get("depth", 0),
                "source": annotation.get("source", "marker"),
            })
        for annotation in self.marker_annotations():
            kind = annotation.get("kind")
            if kind not in ("import", "variable"):
                continue
            events.append({
                "line": annotation["line"],
                "kind": kind,
                "name": annotation.get("name", ""),
                "detail": annotation.get("text", ""),
                "source": "marker",
            })
        events += self._flow_marker_events()
        events += self._flow_warning_events()
        events.sort(key=lambda event: (event["line"], FLOW_KINDS.index(
            event["kind"]) if event["kind"] in FLOW_KINDS else 99))
        return events

    def _flow_warning_events(self):
        """The TODO / FIXME / XXX / HACK markers, wherever they sit.

        A warning is found in a comment, and a comment is not executable, so
        the warning is not reported by the per-line flow markers. It is still
        part of the timeline -- the debugger should show it -- so it is
        collected here, before the step events are generated.
        """
        events = []
        for number, raw in enumerate((self.text or "").splitlines(), 1):
            if self.is_warning(raw):
                events.append({
                    "line": number,
                    "kind": "warning",
                    "name": raw.strip()[:60],
                    "detail": raw.strip()[:DETAIL_LIMIT],
                    "source": "marker",
                })
        return events

    def _flow_marker_events(self):
        """The call / method / throw / error / warning / log events."""
        events = []
        # A line that declares a block is not also a call: `def f(x)`
        # is a definition, not a call of `f`, so those lines are skipped
        # here and only reported as the block they open.
        declared = set(annotation["line"] for annotation in self.blocks())
        for number, raw in enumerate((self.text or "").splitlines(), 1):
            stripped = raw.strip()
            if not stripped or self.is_comment(raw):
                continue
            if number in declared:
                continue
            for regex, kind, group, soft in self.flow_markers:
                match = regex.search(stripped)
                if not match:
                    continue
                try:
                    name = match.group(group) if group else match.group(0)
                except (IndexError, TypeError):
                    name = match.group(0)
                events.append({
                    "line": number,
                    "kind": kind,
                    "name": (name or "").strip(),
                    "detail": stripped[:200],
                    "source": "pattern" if soft else "marker",
                })
                break
        return events

    # -------------------------------------------------------------- frames
    def frames(self, breakpoint_line=None):
        """The call-stack preview for ``breakpoint_line`` (innermost first).

        Only ``class`` and ``definition`` blocks are frames; a control block
        (``if`` / ``for`` / ``try`` ...) is not a frame but still decides which
        frame the breakpoint line falls in, so a breakpoint inside a loop
        reports the function around it.
        """
        blocks = self.blocks()
        if not blocks:
            return []
        target = breakpoint_line if breakpoint_line else 1
        current = None
        for annotation in blocks:
            if annotation["line"] <= target:
                current = annotation
            else:
                break
        if current is None:
            current = blocks[0]
        frames = []
        for annotation in blocks:
            if annotation["kind"] in ("class", "definition", "module"):
                frames.append({
                    "name": "%s: %s" % (annotation["kind"],
                                        annotation["name"]),
                    "line": annotation["line"],
                    "current": annotation is current,
                    "detail": annotation.get("source", ""),
                })
        if not frames:
            frames.append({"name": "module", "line": 1, "current": True,
                           "detail": "fallback"})
        if not any(frame["current"] for frame in frames):
            frames[-1]["current"] = True
        frames.reverse()
        return frames

    # ---------------------------------------------------------- validation
    def validate(self, text=None):
        """Check the document for this adapter's language.

        Returns the same shape as :func:`validate_document`: ``ok``, ``kind``,
        ``language``, ``error``, ``line``, ``column``, ``annotations``,
        ``counts``, ``summary``. A subclass may override it to add a real
        syntax check; the base version accepts anything non-empty and reports
        what it can see.
        """
        owned = self.text if text is None else (text or "")
        annotations = self.blocks()
        counts = {"blocks": len(annotations)}
        for annotation in annotations:
            counts[annotation["kind"]] = counts.get(annotation["kind"], 0) + 1
        if not (owned or "").strip():
            return {
                "ok": False, "kind": CODE_KIND, "language": self.key,
                "error": "empty document", "line": 0, "column": 0,
                "annotations": [], "counts": {}, "summary": "0 block(s)",
            }
        unsupported = [annotation for annotation in annotations
                       if annotation["kind"] == "unsupported"]
        if unsupported:
            first = unsupported[0]
            return {
                "ok": False, "kind": CODE_KIND, "language": self.key,
                "error": first.get("name", "unsupported construct"),
                "line": first["line"], "column": 0, "annotations": annotations,
                "counts": counts, "summary": "%d block(s), %d unsupported" % (
                    len(annotations), len(unsupported)),
            }
        return {
            "ok": True, "kind": CODE_KIND, "language": self.key, "error": "",
            "line": 0, "column": 0, "annotations": annotations,
            "counts": counts,
            "summary": "%d block(s) recognised" % len(annotations),
        }


# ---------------------------------------------------------------------------
# Code adapters
# ---------------------------------------------------------------------------
class PythonAdapter(LanguageAdapter):
    key = "python"
    label = "Python"
    aliases = ("python", "py")
    extensions = (".py", ".pyw", ".pyi")
    brace_blocks = False
    block_markers = (
        (re.compile(r"^async\s+def\s+([A-Za-z_]\w*)"), "definition"),
        (re.compile(r"^def\s+([A-Za-z_]\w*)"), "definition"),
        (re.compile(r"^class\s+([A-Za-z_]\w*)"), "class"),
    )
    block_markers_soft = (
        (re.compile(r"^(?:async\s+)?(?:if|elif)\s+(.+)"), "if"),
        (re.compile(r"^(?:for|while)\s+(.+)"), "loop"),
        (re.compile(r"^(?:with|async\s+with)\s+(.+)"), "with"),
        (re.compile(r"^try\s*:"), "try"),
        (re.compile(r"^(?:except|finally)\b(.*)"), "try"),
    )
    markers = (
        (re.compile(r"^(?:import|from)\s+([\w.]+)"), "import"),
        (re.compile(r"^([A-Za-z_]\w*)\s*(?::[^=]+)?=(?!=)"), "variable"),
        (re.compile(r"^\s*#\s*(?:break|debugger|pdb)\b"), "breakpoint"),
    )
    flow_markers = (
        # Specific markers first: a call is the fallback, so anything that is
        # really an exception, a log or a warning is recognised before it.
        (re.compile(r"\braise\s+([\w.]+)"), "exception", 1, False),
        (re.compile(r"^\s*#\s*(?:TODO|FIXME|XXX|HACK)\b"), "warning", 0,
         False),
        (re.compile(r"\b(logging|logger|log)\.(\w+)\s*\("), "log", 2, False),
        (re.compile(r"\bprint\s*\("), "log", 0, False),
        (re.compile(r"\b([\w.]+)\.([\w]+)\s*\("), "method", 2, True),
        (re.compile(r"\b([A-Za-z_]\w*)\s*\("), "call", 1, True),
    )
    comment_markers = ("#",)
    max_recommended_line = 120

    def validate(self, text=None):
        """A real Python syntax check, with the line and column of a fault."""
        import ast

        owned = self.text if text is None else (text or "")
        base = LanguageAdapter.validate(self, owned)
        base["language"] = self.key
        try:
            tree = ast.parse(owned or "")
        except SyntaxError as exc:
            return {
                "ok": False, "kind": CODE_KIND, "language": self.key,
                "error": "%s: %s" % (type(exc).__name__, exc.msg),
                "line": int(exc.lineno or 0), "column": int(exc.offset or 0),
                "annotations": base["annotations"],
                "counts": base["counts"],
                "summary": "syntax error at line %s" % (exc.lineno or "?"),
            }
        except (ValueError, MemoryError) as exc:
            base["ok"] = False
            base["error"] = str(exc)
            base["summary"] = "not parseable"
            return base
        functions = sum(1 for node in ast.walk(tree)
                        if isinstance(node, (ast.FunctionDef,
                                             ast.AsyncFunctionDef)))
        classes = sum(1 for node in ast.walk(tree)
                      if isinstance(node, ast.ClassDef))
        base["counts"]["definition"] = functions
        base["counts"]["class"] = classes
        base["summary"] = "%d function(s), %d class(es), syntax ok" % (
            functions, classes)
        return base


class JavaScriptAdapter(LanguageAdapter):
    key = "javascript"
    label = "JavaScript / Node"
    aliases = ("javascript", "node", "typescript", "nodejs")
    extensions = (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx")
    block_markers = (
        (re.compile(r"^(?:export\s+)?(?:default\s+)?class\s+(\w+)"), "class"),
        (re.compile(r"^(?:export\s+)?(?:async\s+)?function\s+(\w+)"),
         "definition"),
        (re.compile(r"^(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*"
                    r"(?:async\s*)?(?:function|\()"), "definition"),
    )
    block_markers_soft = (
        (re.compile(r"^(?:export\s+)?(?:async\s+)?(\w+)\s*[:=]\s*"
                    r"(?:async\s*)?\(?[^)]*\)?\s*=>"), "definition"),
        (re.compile(r"^(?:if|else\s+if)\s*\((.+)\)"), "if"),
        (re.compile(r"^(?:for|while|do)\b\s*(.*)"), "loop"),
        (re.compile(r"^(?:try|catch|finally)\b(.*)"), "try"),
        (re.compile(r"^(?:switch)\s*\((.+)\)"), "switch"),
    )
    markers = (
        (re.compile(r"^\s*(?:import|export\s+from)\b\s*"
                    r"(?:\(?\s*['\"]?)([\w./@-]*)"), "import"),
        (re.compile(r"^\s*const\s+(\w+)\s*[:=]"), "variable"),
        (re.compile(r"^\s*//\s*(?:break|debugger)\b"), "breakpoint"),
    )
    flow_markers = (
        (re.compile(r"([\w.$]+)\.([\w$]+)\s*\("), "method", 2, True),
        (re.compile(r"\bnew\s+([\w.]+)\s*\("), "call", 1, False),
        (re.compile(r"\b([A-Za-z_$][\w$]*)\s*\("), "call", 1, True),
        (re.compile(r"\bthrow\s+(?:new\s+)?([\w.]+)"), "exception", 1, False),
        (re.compile(r"^\s*//\s*(?:TODO|FIXME)"), "warning", 0, False),
        (re.compile(r"\b(console|logger|log)\.(\w+)\s*\("), "log", 2, False),
    )
    comment_markers = ("//",)
    max_recommended_line = 120


class CppAdapter(LanguageAdapter):
    key = "cpp"
    label = "C++"
    aliases = ("c++", "cpp", "c")
    extensions = (".cpp", ".cc", ".cxx", ".c", ".h", ".hpp", ".hxx")
    block_markers = (
        (re.compile(r"^(?:template\s*<[^>]*>\s*)?"
                    r"(?:\w[\w:<>,\s*&]*?\s+)?([A-Za-z_]\w*)::([A-Za-z_]\w*)"
                    r"\s*\("), "definition"),
        (re.compile(r"^(?:class|struct)\s+(\w+)"), "class"),
    )
    block_markers_soft = (
        (re.compile(r"^#\s*include\s*[<\"]([^>\"]+)"), "import"),
        (re.compile(r"^(?:if|else\s+if)\s*\((.+)\)"), "if"),
        (re.compile(r"^(?:for|while|do)\b\s*(.*)"), "loop"),
        (re.compile(r"^(?:switch)\s*\((.+)\)"), "switch"),
        (re.compile(r"^try\b(.*)"), "try"),
        (re.compile(r"^(?:catch)\s*\((.+)\)"), "catch"),
    )
    markers = (
        (re.compile(r"^#\s*include\s*[<\"]([^>\"]+)"), "import"),
        (re.compile(r"^\s*//\s*(?:break|debug)\b"), "breakpoint"),
        (re.compile(r"^\s*(?:int|double|float|bool|auto|std::\w+)\s+(\w+)"
                    r"\s*="), "variable"),
    )
    flow_markers = (
        (re.compile(r"([\w:]+)\.([\w]+)\s*\("), "method", 2, True),
        (re.compile(r"([\w:]+)->([\w]+)\s*\("), "method", 2, True),
        (re.compile(r"\bthrow\s+([\w:]+)"), "exception", 1, False),
        (re.compile(r"^\s*//\s*(?:TODO|FIXME)"), "warning", 0, False),
        (re.compile(r"\bcatch\s*\(\s*([\w:]+)"), "catch", 1, False),
    )
    comment_markers = ("//",)
    max_recommended_line = 100


class JavaAdapter(LanguageAdapter):
    key = "java"
    label = "Java"
    aliases = ("java",)
    extensions = (".java",)
    block_markers = (
        (re.compile(r"^(?:public\s+|final\s+|abstract\s+)*"
                    r"(?:class|interface|enum|record)\s+(\w+)"), "class"),
        (re.compile(r"^(?:@\w+\s+)?(?:public|private|protected|static|final"
                    r"|synchronized|native|abstract|\s)*[\w<>\[\],.?\s]+?\s"
                    r"(\w+)\s*\([^;]*\)\s*\{"), "definition"),
    )
    block_markers_soft = (
        (re.compile(r"^(?:if|else\s+if)\s*\((.+)\)"), "if"),
        (re.compile(r"^(?:for|while|do)\b\s*(.*)"), "loop"),
        (re.compile(r"^switch\s*\((.+)\)"), "switch"),
        (re.compile(r"^try\b(.*)"), "try"),
        (re.compile(r"^catch\s*\((.+)\)"), "catch"),
    )
    markers = (
        (re.compile(r"^\s*(?:import|package)\s+([\w.*]+)"), "import"),
        (re.compile(r"^\s*//\s*(?:break|debug)\b"), "breakpoint"),
        (re.compile(r"^\s*(?:int|long|double|String|boolean|var)\s+(\w+)"
                    r"\s*="), "variable"),
    )
    flow_markers = (
        (re.compile(r"([\w.]+)\.([\w]+)\s*\("), "method", 2, True),
        (re.compile(r"\bthrow\s+new\s+([\w.]+)"), "exception", 1, False),
        (re.compile(r"^\s*//\s*(?:TODO|FIXME)"), "warning", 0, False),
        (re.compile(r"\b(Logger|logger|log)\.(\w+)\s*\("), "log", 2, False),
    )
    comment_markers = ("//",)
    max_recommended_line = 100


class PowerShellAdapter(LanguageAdapter):
    key = "powershell"
    label = "PowerShell"
    aliases = ("powershell", "ps1", "pwsh")
    extensions = (".ps1", ".psm1", ".psd1")
    block_markers = (
        (re.compile(r"^(?:function|filter)\s+([\w-]+)"), "definition"),
        (re.compile(r"^class\s+(\w+)"), "class"),
    )
    block_markers_soft = (
        (re.compile(r"^(?:if|elseif)\s*[\(\{](.+)\)?"), "if"),
        (re.compile(r"^(?:foreach|for|while|do)\b\s*(.*)"), "loop"),
        (re.compile(r"^(?:switch)\s*[\(\{](.+)\)?"), "switch"),
        (re.compile(r"^try\b(.*)"), "try"),
        (re.compile(r"^catch\b(.*)"), "catch"),
    )
    markers = (
        (re.compile(r"^\s*Import-Module\s+([\w.:-]+)"), "import"),
        (re.compile(r"^\s*\$(\w+)\s*="), "variable"),
        (re.compile(r"^\s*#\s*(?:break|debug)\b"), "breakpoint"),
    )
    flow_markers = (
        (re.compile(r"\b([\w-]+)\s+-"), "call", 1, True),
        (re.compile(r"\bthrow\s+(.+)"), "exception", 1, False),
        (re.compile(r"^\s*#\s*(?:TODO|FIXME)"), "warning", 0, False),
        (re.compile(r"\bWrite-(?:Host|Output|Log|Warning|Error)\b"),
         "log", 0, False),
    )
    comment_markers = ("#",)
    max_recommended_line = 120


class BatchAdapter(LanguageAdapter):
    key = "batch"
    label = "Batch / CMD"
    brace_blocks = False
    aliases = ("batch", "cmd", "dos")
    extensions = (".bat", ".cmd")
    block_markers = (
        (re.compile(r"^:(\w+)"), "definition"),
    )
    block_markers_soft = (
        (re.compile(r"^if\s+(?:/i\s+)?(?:not\s+)?(?:exist\s+)?(.+)", re.I),
         "if"),
        (re.compile(r"^for\s+(.+?)\s+do\b", re.I), "loop"),
        (re.compile(r"^goto\s+:?(\w+)", re.I), "jump"),
        (re.compile(r"^call\s+:?([\w.]+)", re.I), "call"),
    )
    markers = (
        (re.compile(r"^\s*call\s+:?([\w.]+)", re.I), "import"),
        (re.compile(r"^\s*set\s+(?:/(?:a|p)\s+)?(\w+)=", re.I), "variable"),
        (re.compile(r"^\s*rem\s*(?:break|debug)\b", re.I), "breakpoint"),
    )
    flow_markers = (
        (re.compile(r"^\s*call\s+:?([\w.]+)", re.I), "call", 1, False),
        (re.compile(r"\bexit\s+/b\s+(\d+)", re.I), "exception", 1, False),
        (re.compile(r"^\s*rem\s*(?:TODO|FIXME)", re.I), "warning", 0, False),
        (re.compile(r"^\s*echo\s+(.+)", re.I), "log", 1, True),
    )
    comment_markers = ("rem", "::")
    max_recommended_line = 200
    notes = ("Batch has no line-level runtime: the adapter annotates labels, "
             "jumps and calls so a breakpoint can be placed on a label.")


class VBScriptAdapter(LanguageAdapter):
    key = "vbscript"
    label = "VBScript"
    aliases = ("vbscript", "vbs", "vb")
    extensions = (".vbs", ".vbe", ".wsf")
    block_markers = (
        (re.compile(r"^(?:public\s+|private\s+)?(?:sub|function)\s+"
                    r"([\w.]+)", re.I), "definition"),
        (re.compile(r"^class\s+(\w+)", re.I), "class"),
    )
    block_markers_soft = (
        (re.compile(r"^if\s+(.+?)\s+then\b", re.I), "if"),
        (re.compile(r"^elseif\s+(.+?)\s+then\b", re.I), "if"),
        (re.compile(r"^(?:for|do)\b\s*(.*)", re.I), "loop"),
        (re.compile(r"^select\s+case\b(.*)", re.I), "switch"),
        (re.compile(r"^with\s+(.+)$", re.I), "with"),
    )
    markers = (
        (re.compile(r"^\s*(?:execute|executeglobal)\s+(.+)", re.I), "import"),
        (re.compile(r"^\s*(?:dim|const)\s+(\w+)", re.I), "variable"),
        (re.compile(r"^\s*'\s*(?:break|debug)\b"), "breakpoint"),
    )
    flow_markers = (
        (re.compile(r"^\s*(?:call\s+)?([\w.]+)\s*\(", re.I), "method", 1, True),
        (re.compile(r"\berr\.raise\s+(\d+)", re.I), "exception", 1, False),
        (re.compile(r"^\s*'\s*(?:TODO|FIXME)", re.I), "warning", 0, False),
        (re.compile(r"\bwscript\.echo\b|\bmsgbox\b", re.I), "log", 0, False),
    )
    comment_markers = ("'",)
    max_recommended_line = 200


class ShellAdapter(LanguageAdapter):
    key = "shell"
    label = "Shell (bash / sh)"
    brace_blocks = False
    aliases = ("bash", "shell", "sh", "zsh", "ksh")
    extensions = (".sh", ".bash", ".zsh", ".ksh")
    block_markers = (
        (re.compile(r"^(?:function\s+)?([\w-]+)\s*\(\s*\)\s*\{?"),
         "definition"),
        (re.compile(r"^function\s+([\w-]+)"), "definition"),
    )
    block_markers_soft = (
        (re.compile(r"^source\s+([\w./-]+)"), "import"),
        (re.compile(r"^\.\s+([\w./-]+)"), "import"),
        (re.compile(r"^if\s+\[?\s*(.+?)\s*\]?\s*;?\s*then\b"), "if"),
        (re.compile(r"^for\s+(.+?)\s*;?\s*do\b"), "loop"),
        (re.compile(r"^(?:while|until)\s+(.+?)\s*;?\s*do\b"), "loop"),
        (re.compile(r"^case\s+(.+?)\s+in\b"), "switch"),
    )
    markers = (
        (re.compile(r"^\s*(?:source|\.)\s+([\w./-]+)"), "import"),
        (re.compile(r"^\s*(?:export\s+)?([A-Za-z_]\w*)="), "variable"),
        (re.compile(r"^\s*#\s*(?:break|debug)\b"), "breakpoint"),
    )
    flow_markers = (
        (re.compile(r"^\s*(?:if|while|until|for)\s"), "call", 0, True),
        (re.compile(r"\bexit\s+(\d+)"), "exception", 1, False),
        (re.compile(r"^\s*#\s*(?:TODO|FIXME)"), "warning", 0, False),
        (re.compile(r"\b(echo|printf|logger)\b"), "log", 1, False),
    )
    comment_markers = ("#",)
    max_recommended_line = 120


class JsonAdapter(LanguageAdapter):
    key = "json"
    label = "JSON"
    aliases = ("json", "jsonc", "json5")
    extensions = (".json", ".jsonc", ".json5")
    data_format = True
    comment_markers = ()
    max_recommended_line = 400
    notes = ("A data format: there is nothing to step, so the debugger "
             "validates the document instead of showing frames.")

    def validate(self, text=None):
        """Parse the document and report a syntax fault with line / column."""
        owned = self.text if text is None else (text or "")
        stripped = (owned or "").strip()
        if not stripped:
            return _document_result(self.key, False, "empty document", 0, 0,
                                    {}, "nothing to validate")
        try:
            data = json.loads(stripped)
        except ValueError as exc:
            line, column = _json_error_position(exc, stripped)
            return _document_result(
                self.key, False, "JSONDecodeError: %s" % exc, line, column, {},
                "invalid JSON at line %s" % line)
        counts = count_document_shape(data)
        counts["top_type"] = type(data).__name__
        return _document_result(
            self.key, True, "", 0, 0, counts,
            "valid JSON (%s, %d key(s))" % (counts["top_type"],
                                            counts.get("keys", 0)))


class YamlAdapter(LanguageAdapter):
    key = "yaml"
    label = "YAML"
    aliases = ("yaml", "yml")
    extensions = (".yaml", ".yml")
    data_format = True
    comment_markers = ("#",)
    max_recommended_line = 400
    notes = ("YAML is validated with the optional PyYAML module when it is "
             "available, and with a built-in indentation check otherwise.")

    def validate(self, text=None):
        """Validate with PyYAML when importable, else with the safe checker."""
        owned = self.text if text is None else (text or "")
        stripped = (owned or "").strip()
        if not stripped:
            return _document_result(self.key, False, "empty document", 0, 0,
                                    {}, "nothing to validate")
        try:
            import yaml  # optional, never required by the application
        except ImportError:
            return self._validate_offline(stripped)
        try:
            data = yaml.safe_load(stripped)
        except Exception as exc:  # PyYAML raises several unrelated types
            marks = getattr(exc, "problem_mark", None)
            line = int(getattr(marks, "line", 0)) + 1 if marks else 0
            column = int(getattr(marks, "column", 0)) + 1 if marks else 0
            return _document_result(
                self.key, False, "%s: %s" % (type(exc).__name__, exc), line,
                column, {}, "invalid YAML at line %s" % (line or "?"))
        counts = count_document_shape(data)
        counts["top_type"] = type(data).__name__
        counts["validated_by"] = "PyYAML"
        return _document_result(
            self.key, True, "", 0, 0, counts,
            "valid YAML via PyYAML (%s)" % counts["top_type"])

    def _validate_offline(self, text):
        """The built-in checker: structure, indentation and a key/value scan.

        It cannot be a full YAML parser, so it reports only what it can prove:
        tab indentation (illegal in YAML), unmatched quotes and unbalanced flow
        collections. Suspicious-but-plausible documents still validate, with a
        warning count.
        """
        line, column, error, warnings = _yaml_structure_error(text)
        counts = {"lines": len(text.splitlines()),
                  "warnings": len(warnings),
                  "keys": 0, "validated_by": "built-in"}
        for raw in text.splitlines():
            head = raw.split("#", 1)[0]
            if re.match(r"^\s*[\w.\-'\"]+\s*:", head):
                counts["keys"] += 1
        if error:
            return _document_result(self.key, False, error, line, column,
                                    counts, "invalid YAML at line %s" % line)
        return _document_result(
            self.key, True, "", 0, 0, counts,
            "valid YAML (built-in checker, %d key(s), %d warning(s))" % (
                counts["keys"], counts["warnings"]))


BUILTIN_ADAPTERS = (
    PythonAdapter,
    JavaScriptAdapter,
    CppAdapter,
    JavaAdapter,
    PowerShellAdapter,
    BatchAdapter,
    VBScriptAdapter,
    ShellAdapter,
    JsonAdapter,
    YamlAdapter,
)

DEFAULT_KEY = "python"


# ---------------------------------------------------------------------------
# Validation helpers (pure functions, shared by the adapters)
# ---------------------------------------------------------------------------
def _document_result(language, ok, error, line, column, counts, summary):
    return {
        "ok": bool(ok),
        "kind": DOCUMENT_KIND,
        "language": language,
        "error": error or "",
        "line": int(line or 0),
        "column": int(column or 0),
        "annotations": [],
        "counts": counts or {},
        "summary": summary or "",
    }


def _json_error_position(exc, text):
    """The 1-based (line, column) of a json.JSONDecodeError."""
    line = int(getattr(exc, "lineno", 0) or 0)
    column = int(getattr(exc, "colno", 0) or 0)
    if line:
        return line, column
    offset = int(getattr(exc, "pos", 0) or 0)
    head = text[:offset]
    return head.count("\n") + 1, offset - head.rfind("\n")


def count_document_shape(data, depth=0):
    """Count what a parsed document holds (keys, values, containers, depth).

    The same walker serves JSON and YAML, so both debuggers report a document
    in the same vocabulary. Recursion is bounded, and a value deeper than the
    bound is counted as a leaf rather than descending forever.
    """
    counts = {"keys": 0, "values": 0, "containers": 0, "lists": 0,
              "maps": 0, "depth": depth, "items": 0}
    stack = [(data, depth)]
    while stack:
        value, level = stack.pop()
        counts["depth"] = max(counts["depth"], level)
        if level >= _MAX_DEPTH:
            counts["values"] += 1
            continue
        if isinstance(value, dict):
            counts["maps"] += 1
            counts["containers"] += 1
            counts["keys"] += len(value)
            counts["items"] += len(value)
            for child in value.values():
                if isinstance(child, (dict, list, tuple)):
                    stack.append((child, level + 1))
                else:
                    counts["values"] += 1
        elif isinstance(value, (list, tuple)):
            counts["lists"] += 1
            counts["containers"] += 1
            counts["items"] += len(value)
            for child in value:
                if isinstance(child, (dict, list, tuple)):
                    stack.append((child, level + 1))
                else:
                    counts["values"] += 1
        else:
            counts["values"] += 1
    return counts


def _yaml_structure_error(text):
    """The first provable YAML fault: (line, column, error, warnings)."""
    warnings = []
    flow_stack = []
    lines = text.splitlines()
    for number, raw in enumerate(lines, 1):
        if raw.strip().startswith("#") or not raw.strip():
            continue
        leading = raw[:len(raw) - len(raw.lstrip())]
        if "\t" in leading:
            return (number, 1, "tab character in indentation", warnings)
        stripped = _yaml_strip_comment(raw)
        for index, character in enumerate(stripped):
            if character in "[{":
                flow_stack.append((character, number))
            elif character in "]}":
                if not flow_stack:
                    return (number, index + 1, "unbalanced ']' or '}'",
                            warnings)
                opener = flow_stack.pop()[0]
                if (opener, character) not in (("[", "]"), ("{", "}")):
                    return (number, index + 1,
                            "mismatched '%s' after '%s'" % (character, opener),
                            warnings)
        if stripped.count("'") % 2:
            warnings.append((number, "odd number of single quotes"))
        if stripped.count('"') % 2:
            warnings.append((number, "odd number of double quotes"))
    if flow_stack:
        opener, line = flow_stack[0]
        return (line, 1, "unbalanced '%s'" % opener, warnings)
    return (0, 0, "", warnings)


def _yaml_strip_comment(line):
    """The line with any trailing comment removed (quote aware)."""
    quote = ""
    for index, character in enumerate(line):
        if quote:
            if character == quote:
                quote = ""
            continue
        if character in "\"'":
            quote = character
        elif character == "#" and index and line[index - 1] in " \t":
            return line[:index]
    return line


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------
class DebugLanguageRegistry(object):
    """A mutable table of language adapters with alias / extension lookup."""

    def __init__(self, adapter_classes=BUILTIN_ADAPTERS):
        self._adapters = {}
        self._aliases = {}
        self._extensions = {}
        for adapter_class in adapter_classes:
            self.register_adapter(adapter_class)

    # ------------------------------------------------------------- lookup
    def keys(self):
        return list(self._adapters.keys())

    def adapters(self):
        """Every registered adapter, in registration order."""
        return list(self._adapters.values())

    def languages(self):
        """(key, label) pairs for the language picker."""
        return [(adapter.key, adapter.label)
                for adapter in self._adapters.values()]

    def get(self, key, default=None):
        """The adapter registered under an exact key."""
        return self._adapters.get((key or "").lower(), default)

    def adapter_for(self, category, label="", extension=""):
        """Resolve a script category / file name / extension (never None).

        Resolution order: an exact key, a file extension, then an alias or the
        visible label as a sub-string of the lower-cased category. When nothing
        matches, the fallback adapter is returned rather than ``None``, so the
        caller never has to guard the result.
        """
        lowered = (category or "").lower().strip()
        if extension:
            found = self._adapters.get(extension_for(lowered or extension))
            if found is not None:
                return found
        if lowered and "." in lowered:
            found = self._extensions.get(lowered[lowered.rfind("."):])
            if found is not None:
                return found
        if lowered in self._adapters:
            return self._adapters[lowered]
        for alias in sorted(self._aliases, key=len, reverse=True):
            if alias and alias in lowered:
                return self._aliases[alias]
        if label:
            lowered_label = label.lower()
            for adapter in self._adapters.values():
                if adapter.label.lower() == lowered_label:
                    return adapter
        return self._adapters.get(DEFAULT_KEY) or self.adapters()[0]

    def adapter_for_extension(self, filename):
        """Resolve a file name or extension to an adapter (never None)."""
        lowered = (filename or "").lower()
        dot = lowered.rfind(".")
        extension = lowered[dot:] if dot >= 0 else ""
        found = self._extensions.get(extension)
        if found is not None:
            return found
        return self._adapters.get(DEFAULT_KEY) or self.adapters()[0]

    # ----------------------------------------------------------- mutation
    def register_adapter(self, adapter_class):
        """Add (or replace) a language adapter. Returns True when stored."""
        adapter = _instantiate(adapter_class)
        if adapter is None:
            return False
        self.unregister_adapter(adapter.key)
        self._adapters[adapter.key] = adapter
        self._aliases[adapter.key] = adapter
        self._aliases[adapter.label.lower()] = adapter
        for alias in adapter.aliases:
            self._aliases[(alias or "").lower()] = adapter
        for extension in adapter.extensions:
            self._extensions[(extension or "").lower()] = adapter
        self._prune()
        return True

    def unregister_adapter(self, key):
        """Remove an adapter by key. Returns True when something was removed."""
        key = (key or "").lower()
        adapter = self._adapters.pop(key, None)
        if adapter is None:
            return False
        for alias, owner in list(self._aliases.items()):
            if owner is adapter:
                del self._aliases[alias]
        for extension, owner in list(self._extensions.items()):
            if owner is adapter:
                del self._extensions[extension]
        return True

    def load_plugin(self, name):
        """Import a plugin module and register the adapters it provides.

        A plugin is any importable module defining ``DEBUG_ADAPTERS`` and / or
        a ``debug_adapters()`` factory. Loading never raises: a broken plugin
        comes back as a one element list holding the error message.
        """
        try:
            plugin = importlib.import_module(name)
        except Exception as exc:
            return ["error: %s" % exc]
        found = list(getattr(plugin, "DEBUG_ADAPTERS", ()) or ())
        factory = getattr(plugin, "debug_adapters", None)
        if callable(factory):
            try:
                found += list(factory() or [])
            except Exception as exc:
                return ["error: %s" % exc]
        registered = []
        for adapter_class in found:
            if self.register_adapter(adapter_class):
                registered.append(_key_of(adapter_class))
        return registered

    def reset(self):
        """Restore exactly the built-in adapters (used by the tests)."""
        self._adapters = {}
        self._aliases = {}
        self._extensions = {}
        for adapter_class in BUILTIN_ADAPTERS:
            self.register_adapter(adapter_class)

    def capabilities(self):
        """Describe the engine for the audit / about layer."""
        return {
            "languages": len(self._adapters),
            "keys": list(self._adapters.keys()),
            "flow_kinds": list(FLOW_KINDS),
            "block_kinds": list(BLOCK_KINDS),
            "validates_documents": sorted(
                adapter.key for adapter in self._adapters.values()
                if adapter.data_format),
            "hot_pluggable": True,
            "offline": True,
        }

    # ---------------------------------------------------------- internals
    def _prune(self):
        """Drop aliases and extensions owned by an adapter no longer bound."""
        live = set(id(adapter) for adapter in self._adapters.values())
        for alias, owner in list(self._aliases.items()):
            if id(owner) not in live:
                del self._aliases[alias]
        for extension, owner in list(self._extensions.items()):
            if id(owner) not in live:
                del self._extensions[extension]


def _instantiate(adapter_class):
    """Accept a class or an already built adapter instance."""
    if isinstance(adapter_class, LanguageAdapter):
        return adapter_class
    try:
        return adapter_class()
    except Exception:
        return None


def _key_of(adapter_class):
    adapter = _instantiate(adapter_class)
    return adapter.key if adapter is not None else ""


def extension_for(name):
    """The lower-case extension (with dot) of a file name (or "")."""
    lowered = (name or "").lower()
    dot = lowered.rfind(".")
    return lowered[dot:] if dot >= 0 else ""


# The process-wide registry the Debugger tab uses by default.
REGISTRY = DebugLanguageRegistry()


# ---------------------------------------------------------------- helpers
def keys():
    """Every registered language key, in picker order."""
    return REGISTRY.keys()


def adapters():
    return REGISTRY.adapters()


def languages():
    """(key, label) pairs for the language picker."""
    return REGISTRY.languages()


def get(key, default=None):
    """The adapter registered under an exact key (or `default`)."""
    return REGISTRY.get(key, default)


def adapter_for(category, label="", extension=""):
    """The adapter for a script category (never None)."""
    return REGISTRY.adapter_for(category, label, extension)


def adapter_for_extension(filename):
    return REGISTRY.adapter_for_extension(filename)


def register_adapter(adapter_class):
    return REGISTRY.register_adapter(adapter_class)


def unregister_adapter(key):
    return REGISTRY.unregister_adapter(key)


def load_plugin(name):
    return REGISTRY.load_plugin(name)


def capabilities():
    return REGISTRY.capabilities()


def analyze(code, category="Python", label="", filename=""):
    """Annotate one document without building an adapter by hand.

    Returns the adapter, its block annotations, its marker annotations, its
    flow events and the validation result, so the Debugger tab (or a report)
    can render everything from a single call.
    """
    adapter = REGISTRY.adapter_for(category or filename, label,
                                   extension_for(filename or category))
    adapter.text = code or ""
    return {
        "adapter": adapter,
        "blocks": adapter.blocks(),
        "markers": adapter.marker_annotations(),
        "flow": adapter.flow_events(),
        "validation": adapter.validate(),
    }


def validate_document(code, category="Python", label="", filename=""):
    """Validate a document against the adapter its category resolves to."""
    adapter = REGISTRY.adapter_for(category or filename, label,
                                   extension_for(filename or category))
    adapter.text = code or ""
    return adapter.validate()


def flow_events(code, category="Python", label="", filename=""):
    """The code-flow events of a document, in its own language."""
    adapter = REGISTRY.adapter_for(category or filename, label,
                                   extension_for(filename or category))
    adapter.text = code or ""
    return adapter.flow_events()


def build_frames(code, category="Python", breakpoint_line=None, limit=8):
    """The call-stack preview for a document, whichever language it is in."""
    adapter = REGISTRY.adapter_for(category or "")
    adapter.text = code or ""
    frames = adapter.frames(breakpoint_line)
    return frames[:limit] if limit else frames


def annotation_summary(annotations):
    """Per-kind counts of an annotation list (for a status line / report)."""
    counts = {}
    for annotation in annotations or []:
        kind = annotation.get("kind", "?")
        counts[kind] = counts.get(kind, 0) + 1
    return counts


def engine_info():
    """The engine description used by the Debugger header and the report."""
    info = REGISTRY.capabilities()
    info["annotation_kinds"] = list(ANNOTATION_KINDS)
    info["adapter_details"] = [adapter.describe() for adapter in adapters()]
    return info
