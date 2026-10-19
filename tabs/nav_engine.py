# -*- coding: utf-8 -*-
"""Code navigation engine for the Editor tab.

One small pure-Python engine, no third-party dependency and no Qt import at
module level -- so every jump can be unit tested and driven from a command
line exactly the way the GUI drives it:

    definition   go to definition: where the symbol under the cursor lives
    reference    go to reference: every mention of the symbol
    import       go to import: an import line, or a name brought in by one
    class        go to class: the next (or enclosing) class
    function     go to function: the next (or enclosing) function
    variable     go to variable: the next (or enclosing) variable
    error        jump to error: the next (or previous) parse error
    warning      jump to warning: the next (or previous) linter warning
    section      jump to section: comment banners and region markers
    enclosing    the innermost section the cursor is inside

The engine knows nothing about widgets: it takes (name, code, category)
documents and returns NavLocation records, which keeps the GUI thin and the
engine reusable from the Analyzer, the Audit tab or a script.

Hot plugging: register_scanner() replaces the scanner of a language at run
time, and NAV_KINDS is the ordered menu the UI builds itself from.
"""

import ast
import io
import re
import tokenize

# --------------------------------------------------------------------------
# Result record
# --------------------------------------------------------------------------


class NavLocation(object):
    """One navigable place: a symbol, a problem or a section.

    line is 1-based and column is 0-based, so a location maps straight
    onto an editor text cursor. end_line is inclusive.
        """

    __slots__ = ("kind", "name", "line", "column", "end_line",
                 "end_column", "detail", "category")

    def __init__(self, kind, name="", line=1, column=0, end_line=None,
                 end_column=None, detail="", category=""):
        self.kind = kind          # definition / reference / error ...
        self.name = name          # the symbol name (may be empty)
        self.line = line
        self.column = column
        self.end_line = end_line if end_line is not None else line
        self.end_column = end_column
        self.detail = detail      # signature / message / context
        self.category = category

    def key(self):
        """Document order."""
        return (self.line, self.column)

    def __repr__(self):
        return "NavLocation(%r, %r, %d:%d)" % (
            self.kind, self.name, self.line, self.column)


# --------------------------------------------------------------------------
# Navigation kinds offered by the UI
# --------------------------------------------------------------------------

# (key, translation key, fallback label) for the Go to... menu.
NAV_KINDS = (
    ("definition", "nav_go_definition", "Go to definition"),
    ("reference", "nav_go_reference", "Go to reference"),
    ("import", "nav_go_import", "Go to import"),
    ("class", "nav_go_class", "Go to class"),
    ("function", "nav_go_function", "Go to function"),
    ("variable", "nav_go_variable", "Go to variable"),
    ("error", "nav_go_error", "Go to error"),
    ("warning", "nav_go_warning", "Go to warning"),
    ("section", "nav_go_section", "Go to section"),
    ("enclosing", "nav_go_enclosing", "Enclosing section"),
)

# Kinds that describe a problem rather than a structure.
PROBLEM_KINDS = ("error", "warning")

# Kinds whose results the locate panel can list ahead of the cursor.
STRUCTURE_KINDS = ("class", "function", "variable", "import", "definition")

# Kinds whose name is worth putting into the quick-jump box.
NAME_KINDS = ("definition", "reference", "import", "class", "function",
              "variable")

# The structural kinds a scanner can fill.
SCAN_KINDS = ("definition", "class", "function", "variable", "import",
              "reference", "section", "error", "warning")

# Source names used by the language rules below.
SUMMARY = "summary"
DEFINITION = "definition"
BLOCK = "block"
IMPORT = "import_line"
SECTION = "section"
BLOCK_LABEL = "block_label"


def empty_tables():
    """A fresh scan table: one list per structural kind."""
    return dict((name, []) for name in SCAN_KINDS)


# --------------------------------------------------------------------------
# Small text helpers
# --------------------------------------------------------------------------


def _collapse(pattern):
    """Compile a pattern that is matched one line at a time."""
    return re.compile(pattern, re.IGNORECASE | re.MULTILINE)


class _Lazy(object):
    """A pattern compiled on first use (keeps import time small)."""

    __slots__ = ("maker", "value")

    def __init__(self, maker):
        self.maker = maker
        self.value = None

    def __call__(self):
        if self.value is None:
            self.value = self.maker()
        return self.value


def build_rule(order, sources):
    """A navigation rule: an ordered part list plus its pattern sources."""
    return {"order": tuple(order), "sources": sources}


def _match_name(line, pattern):
    """The name a line pattern introduces (its first non-empty group)."""
    match = pattern.match(line)
    if match is not None:
        for group in match.groups():
            if group:
                return group.strip()
    return line.strip()


def _line_targets(text, pattern, kind, category=""):
    """One target per matching line, in document order."""
    out = []
    if pattern is None:
        return out
    for number, line in enumerate(text.splitlines(), 1):
        if pattern.match(line) is None:
            continue
        name = _match_name(line, pattern)
        column = line.find(name) if name else 0
        out.append(NavLocation(kind, name, number, max(0, column),
                               number, len(line), line.strip(), category))
    return out


def _is_ident_char(char):
    """True for a character that appears inside an identifier."""
    return bool(char) and (char.isalnum() or char in "_$@")


def word_at(text, position):
    """The identifier around a character offset, with its span.

    Returns (name, start, end); name is empty when the cursor does not
    sit on an identifier.
    """
    if not text:
        return ("", 0, 0)
    position = max(0, min(position, len(text)))
    start = position
    if start >= len(text) or not _is_ident_char(text[start]):
        if start > 0 and _is_ident_char(text[start - 1]):
            start -= 1
        else:
            return ("", position, position)
    end = start
    while start > 0 and _is_ident_char(text[start - 1]):
        start -= 1
    while end < len(text) and _is_ident_char(text[end]):
        end += 1
    name = text[start:end]
    if not name or name[0].isdigit():
        return ("", position, position)
    return (name, start, end)


def word_at_cursor(text, line, column):
    """The identifier at a 1-based line / 0-based column."""
    lines = text.split(chr(10))
    index = max(0, min(line - 1, len(lines) - 1)) if lines else 0
    offset = sum(len(part) + 1 for part in lines[:index]) + column
    return word_at(text, offset)


# --------------------------------------------------------------------------
# Documents and the scanner cache
# --------------------------------------------------------------------------


def normalize_document(document):
    """Coerce one document into a (name, code, category) triple."""
    if document is None:
        return ("", "", "Python")
    if isinstance(document, dict):
        return (document.get("name", ""), document.get("code", "") or "",
                document.get("category", "Python"))
    if isinstance(document, (tuple, list)):
        name = document[0] if len(document) > 0 else ""
        code = document[1] if len(document) > 1 else ""
        category = document[2] if len(document) > 2 else "Python"
        return (name or "", code or "", category or "Python")
    return (getattr(document, "name", "") or "",
            getattr(document, "code", "") or "",
            getattr(document, "category", "Python") or "Python")


def normalize_documents(documents):
    """Normalise a document or a one-item list of documents into a triple."""
    if isinstance(documents, (list, tuple)) and documents:
        first = documents[0]
        if isinstance(first, (list, tuple, dict)):
            return normalize_document(first)
    return normalize_document(documents)


def document_key(document):
    """The language rule key used for a document (never empty)."""
    category = normalize_document(document)[2]
    try:
        from . import syntax
        return syntax.module_for(category).key
    except Exception:
        return "python"


def cache_snapshot(text):
    """The identity of a buffer: length, line count and a checksum."""
    text = text or ""
    return (len(text), text.count(chr(10)), hash(text))


_CACHE = {}
_REF_CACHE = {}


def invalidate(category=None):
    """Drop the cached scan for a category, or for every category."""
    if category is None:
        _CACHE.clear()
    else:
        _CACHE.pop(document_key(("", "", category)), None)
    _REF_CACHE.clear()


# --------------------------------------------------------------------------
# Per-language patterns
# --------------------------------------------------------------------------

# The few quote-bearing patterns below are assembled from character codes: a
# quote next to a backslash in a source literal is the one combination that
# keeps getting mangled, and these constants make it impossible.
_B = chr(92)             # backslash
_D = chr(34)             # double quote
_S = chr(39)             # single quote
_QUOTES = "[" + _B + _D + _B + _S + "]"          # a class holding both quotes
_NOT_QUOTE = "([^" + _B + _D + _B + _S + "]+)"   # a group: not either quote
PY_BLOCK_RE = _Lazy(lambda: _collapse(
    r"^[ \t]*(?:class[ \t]+(\w+)"
    r"|def[ \t]+(\w+)"
    r"|([A-Z][A-Z0-9_]{2,})[ \t]*(?::[^=\n]*)?=)"))
PY_IMPORT_RE = _Lazy(lambda: _collapse(
    r"^[ \t]*import[ \t]+([\w.]+)"
    r"|^[ \t]*from[ \t]+([\w.]+)[ \t]+import"
    r"[ \t]+(\w+)"))
PY_WORD = r"(?<![\w.])%s(?![\w])"

CPP_BLOCK_RE = _Lazy(lambda: _collapse(
    r"^[ \t]*(?:class|struct|enum|union)[ \t]+(\w+)"
    r"|^[ \t]*(?:[\w:<>,*&\[\]][ \t]+)"
    r"+(\w+)[ \t]*\([^;]*\)[ \t]*[{;]"
    r"|^[ \t]*(?:const[ \t]+)?"
    r"([\w:<>*&\[\]][\w:<>*&\[\]]*)"
    r"[ \t]+(\w+)[ \t]*="))
CPP_IMPORT_RE = _Lazy(lambda: _collapse(
    "^[ " + _B + "t]*#[ " + _B + "t]*include[ " + _B + "t]*"
    + _QUOTES + "([^>" + _B + "\"]+)" + _QUOTES))
CPP_WORD = r"(?<![\w:.>])%s(?![\w])"

JAVA_BLOCK_RE = _Lazy(lambda: _collapse(
    r"^[ \t]*(?:(?:public|private|protected|static|final|"
    r"abstract|synchronized|native|transient|volatile)[ \t]+)*"
    r"(?:class|interface|enum|record)[ \t]+(\w+)"
    r"|^[ \t]*(?:(?:public|private|protected|static|final|"
    r"abstract)[ \t]+)*[\w:<>,*&\[\]?.]+"
    r"[ \t]+(\w+)[ \t]*\([^;]*\)"
    r"[ \t]*[{;]"))
JAVA_IMPORT_RE = _Lazy(lambda: _collapse(
    r"^[ \t]*import[ \t]+(?:static[ \t]+)?"
    r"([\w.*]+)"))

JS_BLOCK_RE = _Lazy(lambda: _collapse(
    r"^[ \t]*(?:export[ \t]+)?(?:default[ \t]+)?"
    r"(?:async[ \t]+)?function[ \t]+(\w+)"
    r"|^[ \t]*(?:export[ \t]+)?class[ \t]+(\w+)"
    r"|^[ \t]*(?:const|let|var)[ \t]+(\w+)[ \t]*="))
JS_IMPORT_RE = _Lazy(lambda: _collapse(
    "^[ " + _B + "t]*import[ " + _B + "t]+.*?from[ " + _B + "t]*"
    + _QUOTES + _NOT_QUOTE + _QUOTES
    + "|^[ " + _B + "t]*import[ " + _B + "t]*"
    + _QUOTES + _NOT_QUOTE + _QUOTES
    + "|^[ " + _B + "t]*require[ " + _B + "t]*" + _B + "("
    + _QUOTES + _NOT_QUOTE + _QUOTES + _B + ")"))

PS_BLOCK_RE = _Lazy(lambda: _collapse(
    r"^[ \t]*(?:function|filter|class)[ \t]+([\w-]+)"
    r"|^[ \t]*\$([\w:]+)[ \t]*="))
PS_IMPORT_RE = _Lazy(lambda: _collapse(
    r"^[ \t]*(?:using|Import-Module|\.)"
    r"[ \t]+([\w.\/-]+)"))
PS_WORD = r"(?<![\w:.$-])%s(?![\w])"

BATCH_BLOCK_RE = _Lazy(lambda: _collapse(
    r"^[ \t]*(\w+)|^[ \t]*set[ \t]+"
    r"(?:/[ap][ \t]+)?([\w ]+?)="))
BATCH_IMPORT_RE = _Lazy(lambda: _collapse(
    r"^[ \t]*call[ \t]+([\w.:\/-]+)"))

VB_BLOCK_RE = _Lazy(lambda: _collapse(
    r"^[ \t]*(?:Public|Private|Protected|Friend|Static[ \t]+)*"
    r"(?:Sub|Function|Property[ \t]+(?:Get|Let|Set)|Class|Const|Dim|"
    r"Enum)[ \t]+(\w+)"))
VB_IMPORT_RE = _Lazy(lambda: _collapse(r"^[ \t]*\."))

SQL_BLOCK_RE = _Lazy(lambda: _collapse(
    r"^[ \t]*create[ \t]+(?:or[ \t]+"
    r"replace[ \t]+)?(?:table|view|index|function|procedure|"
    r"trigger|sequence|schema)[ \t]+(?:if[ \t]+not"
    r"[ \t]+exists[ \t]+)?([\w.]+)"
    r"|^[ \t]*declare[ \t]+(@?\w+)"
    r"|^[ \t]*with[ \t]+(\w+)[ \t]+as[ \t]*\("))
SQL_IMPORT_RE = _Lazy(lambda: _collapse(
    r"^[ \t]*(?:source|include|\.import)"
    r"[ \t]+([\w./-]+)"))

BASH_BLOCK_RE = _Lazy(lambda: _collapse(
    r"^[ \t]*(?:function[ \t]+)?([\w.-]+)"
    r"[ \t]*\(\s*\)[ \t]*\{?"
    r"|^[ \t]*([A-Za-z_]\w*)[ \t]*="))
BASH_IMPORT_RE = _Lazy(lambda: _collapse(
    r"^[ \t]*(?:source|\.)[ \t]+([\w./~$-]+)"))

HTML_BLOCK_RE = _Lazy(lambda: _collapse(
    r"^[ \t]*(?:function|var|let|const)[ \t]+(\w+)"
    r"|^[ \t]*<!\-\s*(\w[\w -]*)"
    r"|^[ \t]*<([a-z][\w:-]*)[ \t]*>"))
HTML_IMPORT_RE = _Lazy(lambda: _collapse('<script[^>]*src=[\\\\"\\\\\']([^\\\\"\\\\\']+)[\\\\"\\\\\']|<link[^>]*href=[\\\\"\\\\\']([^\\\\"\\\\\']+)[\\\\"\\\\\']'))

RUBY_BLOCK_RE = _Lazy(lambda: _collapse(
    r"^[ \t]*(?:def[ \t]+([\w?!]+)"
    r"|class[ \t]+(\w+)|module[ \t]+(\w+)"
    r"|([A-Z]\w*)[ \t]*=)"))
RUBY_IMPORT_RE = _Lazy(lambda: _collapse(
    "^[ " + _B + "t]*(?:require|require_relative|load)[ " + _B
    + "t]_*" + _QUOTES + _NOT_QUOTE + _QUOTES))
DATA_BLOCK_RE = _Lazy(lambda: _collapse(
    r"^[ \t]*([\w.-]+)[ \t]*[:=]"))
DATA_IMPORT_RE = _Lazy(lambda: _collapse(
    r"^[ \t]*(?:include|using|base)[ \t]*:?"
    r"[ \t]*([\w./\-]+)"))

SYSTEM_BLOCK_RE = _Lazy(lambda: _collapse(
    r"^[ \t]*\[([^\]]+)\]"))
SYSTEM_IMPORT_RE = _Lazy(lambda: _collapse(
    r"^[ \t]*(?:include|import|load)[ \t]*"
    r"[=: ]\s*(.+)$"))


def language_patterns():
    """The {key: rule} table used by every language jump."""
    return LANGUAGE_PATTERNS


LANGUAGE_PATTERNS = {
    "python": build_rule((DEFINITION, BLOCK, IMPORT, SECTION), {
        DEFINITION: PY_BLOCK_RE, BLOCK: PY_BLOCK_RE,
        BLOCK_LABEL: ("class", "function", "variable"),
        IMPORT: PY_IMPORT_RE, SECTION: None}),
    "cpp": build_rule((DEFINITION, BLOCK, IMPORT, SECTION), {
        DEFINITION: CPP_BLOCK_RE, BLOCK: CPP_BLOCK_RE,
        BLOCK_LABEL: ("class", "function", "variable"),
        IMPORT: CPP_IMPORT_RE, SECTION: ("region", "section")}),
    "java": build_rule((DEFINITION, BLOCK, IMPORT, SECTION), {
        DEFINITION: JAVA_BLOCK_RE, BLOCK: JAVA_BLOCK_RE,
        BLOCK_LABEL: ("class", "function", "variable"),
        IMPORT: JAVA_IMPORT_RE, SECTION: ("region", "section")}),
    "javascript": build_rule((DEFINITION, BLOCK, IMPORT, SECTION), {
        DEFINITION: JS_BLOCK_RE, BLOCK: JS_BLOCK_RE,
        BLOCK_LABEL: ("class", "function", "variable"),
        IMPORT: JS_IMPORT_RE, SECTION: ("region", "section")}),
    "powershell": build_rule((DEFINITION, BLOCK, IMPORT, SECTION), {
        DEFINITION: PS_BLOCK_RE, BLOCK: PS_BLOCK_RE,
        BLOCK_LABEL: ("function", "variable"),
        IMPORT: PS_IMPORT_RE, SECTION: ("region", "section")}),
    "batch": build_rule((DEFINITION, BLOCK, IMPORT, SECTION), {
        DEFINITION: BATCH_BLOCK_RE, BLOCK: BATCH_BLOCK_RE,
        BLOCK_LABEL: ("function", "variable"),
        IMPORT: BATCH_IMPORT_RE, SECTION: ("section",)}),
    "vbscript": build_rule((DEFINITION, BLOCK, IMPORT, SECTION), {
        DEFINITION: VB_BLOCK_RE, BLOCK: VB_BLOCK_RE,
        BLOCK_LABEL: ("function", "variable"),
        IMPORT: VB_IMPORT_RE, SECTION: ("section",)}),
    "sql": build_rule((DEFINITION, BLOCK, IMPORT, SECTION), {
        DEFINITION: SQL_BLOCK_RE, BLOCK: SQL_BLOCK_RE,
        BLOCK_LABEL: ("class", "function", "variable"),
        IMPORT: SQL_IMPORT_RE, SECTION: ("section",)}),
    "bash": build_rule((DEFINITION, BLOCK, IMPORT, SECTION), {
        DEFINITION: BASH_BLOCK_RE, BLOCK: BASH_BLOCK_RE,
        BLOCK_LABEL: ("function", "variable"),
        IMPORT: BASH_IMPORT_RE, SECTION: ("section",)}),
    "html": build_rule((DEFINITION, BLOCK, IMPORT, SECTION), {
        DEFINITION: HTML_BLOCK_RE, BLOCK: HTML_BLOCK_RE,
        BLOCK_LABEL: ("function", "variable"),
        IMPORT: HTML_IMPORT_RE, SECTION: ("section",)}),
    "ruby": build_rule((DEFINITION, BLOCK, IMPORT, SECTION), {
        DEFINITION: RUBY_BLOCK_RE, BLOCK: RUBY_BLOCK_RE,
        BLOCK_LABEL: ("class", "function", "variable"),
        IMPORT: RUBY_IMPORT_RE, SECTION: ("section",)}),
    "json": build_rule((DEFINITION, BLOCK, IMPORT, SECTION), {
        DEFINITION: DATA_BLOCK_RE, BLOCK: DATA_BLOCK_RE,
        BLOCK_LABEL: ("variable",),
        IMPORT: DATA_IMPORT_RE, SECTION: ("section",)}),
    "system": build_rule((DEFINITION, BLOCK, IMPORT, SECTION), {
        DEFINITION: SYSTEM_BLOCK_RE, BLOCK: SYSTEM_BLOCK_RE,
        BLOCK_LABEL: ("class",),
        IMPORT: SYSTEM_IMPORT_RE, SECTION: ("section",)}),
}


def rule_for(key):
    """The navigation rule of a language key (never None)."""
    return LANGUAGE_PATTERNS.get(key) or LANGUAGE_PATTERNS["python"]


def language_list():
    """The keys of every language rule, in registration order."""
    return list(LANGUAGE_PATTERNS.keys())


# --------------------------------------------------------------------------
# Sections and warnings (shared by every language)
# --------------------------------------------------------------------------

_SECTION_RE = re.compile(
    r"^\s*(?:#+|//+|/\*+|--+|;+|'+|REM\s+"
    r"|<!--+|{-+)\s*(?:-{2,}|={2,}|#{2,}|\*{2,})?\s*"
    r"([A-Za-z][A-Za-z0-9 _(),./:+-]{2,60})\s*$", re.IGNORECASE)
_REGION_RE = re.compile(
    r"^\s*(?:#+|//+|/\*+|--+|;+|'+|)\s*"
    r"(?:region|endregion|pragma\s+region)\b"
    r"[ \t]*:?[ \t]*(.*)$", re.IGNORECASE)
_BANNER_RE = re.compile(
    r"^\s*(?:#|//|/\*|--+|;|')\s*"
    r"(?:-{3,}|={3,}|\*{3,})\s*$")
_DEFAULT_MARKERS = ("region", "section", "imports", "constants",
                    "classes", "functions", "helpers")

_WARNING_RE = re.compile(
    r"\b(TODO|FIXME|XXX|HACK|BUG|WARNING|DEPRECATED|REVIEW"
    r"|OPTIMIZE)\b[ \t]*[:\-]?[ \t]*(.*)",
    re.IGNORECASE)


def _markers(key):
    """The words that count as a section banner in a language."""
    rule = LANGUAGE_PATTERNS.get(key)
    if rule is not None:
        value = rule["sources"].get(SECTION)
        if isinstance(value, tuple) and value:
            return value
    return _DEFAULT_MARKERS


def _section_locations(text, markers=None):
    """Comment banners and region markers in a buffer."""
    words = tuple(word.lower() for word in (markers or _DEFAULT_MARKERS))
    out = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        region = _REGION_RE.match(line)
        if region is not None:
            title = (region.group(1) or "").strip() or "region"
            out.append(NavLocation("section", title, number, 0, number,
                                   len(line), line.strip(), "section"))
            continue
        if _BANNER_RE.match(line):
            continue
        match = _SECTION_RE.match(line)
        if match is None:
            continue
        title = match.group(1).strip(" -=")
        if not title:
            continue
        lowered = title.lower()
        if words and not any(word and word in lowered for word in words):
            continue
        out.append(NavLocation("section", title, number, 0, number,
                               len(line), line.strip(), "section"))
    return out


def _warning_locations(text, key):
    """Marker words that read as a warning worth jumping to."""
    del key
    out = []
    for number, line in enumerate(text.splitlines(), 1):
        match = _WARNING_RE.search(line)
        if match is None:
            continue
        marker = match.group(1).upper()
        out.append(NavLocation("warning", marker, number, match.start(),
                               number, len(line), line.strip(), "warning"))
    return out


# --------------------------------------------------------------------------
# Python structure from the AST
# --------------------------------------------------------------------------


def _syntax_error_location(exc, text):
    """A NavLocation for one SyntaxError."""
    line = getattr(exc, "lineno", None) or 1
    column = max(0, (getattr(exc, "offset", None) or 1) - 1)
    detail = getattr(exc, "msg", "") or "syntax error"
    body = text.splitlines()
    width = len(body[line - 1]) if 0 < line <= len(body) else 0
    return NavLocation("error", detail, line, column, line, width, detail,
                       "Python")


def _argument_names(arguments):
    """Flatten an ast.arguments container into a name list."""
    if arguments is None:
        return ""
    names = []
    for group in ("posonlyargs", "args", "kwonlyargs"):
        names += [getattr(arg, "arg", "") for arg in
                  (getattr(arguments, group, None) or [])]
    if getattr(arguments, "vararg", None) is not None:
        names.append("*" + arguments.vararg.arg)
    if getattr(arguments, "kwarg", None) is not None:
        names.append("**" + arguments.kwarg.arg)
    return ", ".join(name for name in names if name)


def _assigned_names(target):
    """Every name an assignment target binds, as (name, line, column)."""
    found = []
    if isinstance(target, ast.Name):
        found.append((target.id, getattr(target, "lineno", 1),
                      getattr(target, "col_offset", 0)))
    elif isinstance(target, (ast.Tuple, ast.List)):
        for element in target.elts:
            found.extend(_assigned_names(element))
    elif isinstance(target, ast.Starred):
        found.extend(_assigned_names(target.value))
    return found


def _python_ast_scan(tree, tables):
    """Walk the AST once, filling classes, functions, variables and imports."""
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 1)
        column = getattr(node, "col_offset", 0)
        end_line = getattr(node, "end_lineno", None) or line
        end_column = getattr(node, "end_col_offset", None)
        if isinstance(node, ast.ClassDef):
            tables["class"].append(NavLocation(
                "class", node.name, line, column, end_line, end_column,
                "class", "Python"))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = _argument_names(node.args)
            detail = "def %s(%s)" % (node.name, args)
            tables["function"].append(NavLocation(
                "function", node.name, line, column, end_line,
                end_column, detail, "Python"))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            # ast.walk visits the Assign node and then its Name targets, so the
            # targets are read here and never left behind for the next node.
            targets = list(node.targets) if isinstance(node, ast.Assign) \
                else [node.target]
            for target in targets:
                for name, tline, tcolumn in _assigned_names(target):
                    tables["variable"].append(NavLocation(
                        "variable", name, tline, tcolumn, tline, None,
                        "assigned", "Python"))
        if isinstance(node, ast.Import):
            for alias in node.names:
                tables["import"].append(NavLocation(
                    "import", alias.asname or alias.name, line, column,
                    line, None, alias.name, "Python"))
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                tables["import"].append(NavLocation(
                    "import", alias.asname or alias.name, line, column,
                    line, None,
                    "from %s import %s" % (node.module or "", alias.name),
                    "Python"))


def _python_regex_scan(text, tables):
    """Structure recovery for a Python buffer that does not parse."""
    pattern = PY_BLOCK_RE()
    for kind in ("class", "function", "variable"):
        tables[kind] = _line_targets(text, pattern, kind, "Python")
    tables["import"] = _line_targets(text, PY_IMPORT_RE(), "import",
                                     "Python")


def _python_scanner(text, key):
    """The built-in Python scanner: the AST first, line patterns second."""
    del key
    tables = empty_tables()
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        tables["error"].append(_syntax_error_location(exc, text))
        _python_regex_scan(text, tables)
    except (ValueError, TypeError):
        _python_regex_scan(text, tables)
    else:
        _python_ast_scan(tree, tables)
    tables["definition"] = list(tables["class"] + tables["function"]
                                + tables["variable"])
    tables["definition"].sort(key=lambda item: item.key())
    tables["section"] = _section_locations(text, _markers("python"))
    tables["warning"] = _warning_locations(text, "python")
    return tables


def _python_references(text, name):
    """Token-accurate mentions of a name; None when the buffer does not parse."""
    found = []
    lines = text.splitlines()
    try:
        reader = io.StringIO(text).readline
        for token in tokenize.generate_tokens(reader):
            if token.type != tokenize.NAME or token.string != name:
                continue
            line = token.start[0]
            body = lines[line - 1] if 0 < line <= len(lines) else ""
            found.append(NavLocation("reference", name, line,
                                     token.start[1], token.end[0],
                                     token.end[1], body.strip(),
                                     "Python"))
    except Exception:
        return None
    return found


# --------------------------------------------------------------------------
# Generic line-pattern scanning and the scan cache
# --------------------------------------------------------------------------


# Function pattern used by _classify: name( ... ){ or ;
_FUNCTION_RE = re.compile(
    r"\w+[ ]*\([^;]*\)[ ]*[{;][ ]*$")


def _looks_like_function(line):
    """True when a line looks like a function declaration."""
    lowered = line.strip().lower()
    if re.match(r"^[ ]*(?:def|function|sub|func|proc|method)[ ]", lowered):
        return True
    return _FUNCTION_RE.search(line) is not None


def _classify(line, labels):
    """Class, function or variable for one structural line."""
    lowered = line.strip().lower()
    if re.match(r"^[ ]*(?:class|struct|interface|enum|record|union)[ ]",
                lowered):
        return "class"
    if _looks_like_function(line):
        return "function"
    if lowered.startswith("[") or re.match(
            r"^[ ]*create[ ]+(?:table|view|index|function|procedure)",
            lowered):
        return "class"
    return "variable" if "variable" in labels else "function"


def _pattern_for(key, name):
    """The compiled pattern of one rule source, or None."""
    source = rule_for(key)["sources"].get(name)
    if source is None:
        return None
    if isinstance(source, _Lazy):
        return source()
    if hasattr(source, "match"):
        return source
    return _collapse(source)


def _fill_generic(tables, text, key):
    """Fill the tables from the line patterns of a language.

    A scanner that already found structure is trusted: nothing here is
    appended to it, or every symbol would appear twice. Only a buffer
    no scanner claimed falls back to the line patterns of its rule.
    """
    rule = rule_for(key)
    pattern = _pattern_for(key, BLOCK)
    scanned = any(tables[kind] for kind in (
        "class", "function", "variable", "import"))
    if not scanned and pattern is not None:
        labels = rule["sources"].get(BLOCK_LABEL) or ("function",)
        for number, line in enumerate(text.splitlines(), 1):
            if pattern.match(line) is None:
                continue
            name = _match_name(line, pattern)
            if not name:
                continue
            kind = _classify(line, labels)
            tables[kind].append(NavLocation(
                kind, name, number, max(0, line.find(name)), number,
                len(line), line.strip(), key))
        tables["definition"] = list(tables["class"] + tables["function"]
                                    + tables["variable"])
        tables["definition"].sort(key=lambda item: item.key())
    if not tables["import"]:
        tables["import"] = _line_targets(text, _pattern_for(key, IMPORT),
                                         "import", key)
    if not tables["section"]:
        tables["section"] = _section_locations(text, _markers(key))
    if not tables["warning"]:
        tables["warning"] = _warning_locations(text, key)


_SCANNERS = {"python": _python_scanner}


def register_scanner(key, scanner):
    """Hot-plug the scanner of a language. Returns the scanner."""
    _SCANNERS[key] = scanner
    invalidate()
    return scanner


def unregister_scanner(key):
    """Remove a hot-plugged scanner; Python keeps its built-in one."""
    removed = _SCANNERS.get(key) is not _python_scanner
    if key == "python":
        _SCANNERS["python"] = _python_scanner
    else:
        _SCANNERS.pop(key, None)
    invalidate()
    return removed


def scanners():
    """The scanners currently registered, in key order."""
    return sorted(_SCANNERS.keys())


def scan_text(text, category="Python"):
    """Scan one buffer into a cached table of location lists."""
    key = document_key(("", text, category))
    snapshot = cache_snapshot(text)
    cached = _CACHE.get(key)
    if cached is not None and cached[0] == snapshot:
        return cached[1]
    tables = _scan_uncached(text or "", key)
    _CACHE[key] = (snapshot, tables)
    return tables


def _scan_uncached(text, key):
    """Run the scanner of a key and fill in whatever it left empty."""
    tables = empty_tables()
    scanner = _SCANNERS.get(key)
    if scanner is not None:
        try:
            scanned = scanner(text, key)
        except Exception:
            scanned = None
        if isinstance(scanned, dict):
            for name in SCAN_KINDS:
                value = scanned.get(name)
                if value:
                    tables[name] = list(value)
    _fill_generic(tables, text, key)
    if key == "python" and not tables["error"]:
        tables["error"] = _parse_error(text)
    return tables


def _parse_error(text):
    """The parse error of a Python buffer, if it has one."""
    try:
        ast.parse(text)
    except SyntaxError as exc:
        return [_syntax_error_location(exc, text)]
    except (ValueError, TypeError):
        return []
    return []


# --------------------------------------------------------------------------
# Navigation queries
# --------------------------------------------------------------------------


def _targets(document, kind):
    """The scanned targets of one kind, in document order."""
    _name, code, category = normalize_document(document)
    return list(scan_text(code, category).get(kind, ()))


def all_locations(document, kind):
    """Every location of a kind, with definitions and references expanded."""
    if kind in ("definition", "reference"):
        return symbols(document, kind)
    return _targets(document, kind)


def symbols(document, kind="definition"):
    """Definitions (or references) of a document, in document order.

    A definition is every named declaration -- a class, a function and a
    variable all count, which is what a symbol list should show.
    """
    _name, code, category = normalize_document(document)
    tables = scan_text(code, category)
    if kind == "definition":
        found = list(tables.get("definition", ()))
        found.sort(key=lambda item: item.key())
        return found
    if kind in ("class", "function", "variable", "import", "section"):
        # A named structural kind answers with its own table: only the
        # "reference" kind expands into every mention of every symbol.
        found = list(tables.get(kind, ()))
        found.sort(key=lambda item: item.key())
        return found
    names = []
    for item in tables.get("definition", ()):
        if item.name and item.name not in names:
            names.append(item.name)
    found = []
    seen = set()
    for symbol in names:
        for item in references(document, symbol, 0):
            key = (item.name, item.line, item.column)
            if key not in seen:
                seen.add(key)
                found.append(item)
    return found


def enclosing(document, kind, line, column=0):
    """The innermost scanned location of a kind around the cursor.

    Returns None when the cursor is not inside anything of that kind,
    which is what the class and function buttons report in the status
    line.
    """
    best = None
    for item in _targets(document, kind):
        if item.line > line:
            continue
        if item.line == line and item.column > column:
            continue
        if item.end_line is not None and line > item.end_line:
            continue
        if best is None or item.line >= best.line:
            best = item
    return best


def _find_references(code, key, name):
    """Whole-word (Python: token-accurate) mentions of one name."""
    text = code or ""
    if key == "python":
        precise = _python_references(text, name)
        if precise is not None:
            return precise
    template = PY_WORD
    if key in ("cpp", "java", "javascript"):
        template = CPP_WORD
    elif key == "powershell":
        template = PS_WORD
    pattern = re.compile(template % re.escape(name), re.IGNORECASE)
    found = []
    for number, line in enumerate(text.splitlines(), 1):
        for match in pattern.finditer(line):
            body = match.group(0)
            offset = match.start()
            if name in body:
                offset += body.find(name)
            found.append(NavLocation("reference", name, number, offset,
                                     number, len(line), line.strip(), key))
    return found


def references(document, name, limit=0):
    """Every mention of a name, with definition sites pinned first.

    The search is token-accurate when the buffer is Python that parses,
    so strings and comments never produce a false hit, and whole-word
    text matching otherwise.
    """
    name = (name or "").strip()
    if not name:
        return []
    _doc, code, category = normalize_document(document)
    key = document_key(document)
    tables = scan_text(code, category)
    cache_key = (key, name)
    snapshot = cache_snapshot(code)
    cached = _REF_CACHE.get(cache_key)
    if cached is not None and cached[0] == snapshot:
        found = list(cached[1])
    else:
        found = _find_references(code, key, name)
        _REF_CACHE[cache_key] = (snapshot, found)
    # A definition site is pinned by line: the AST reports `def fmt` at the
    # `def` keyword while the token scan reports the name column, so matching
    # on the column alone would never line the two up.
    sites = set(item.line for item in tables.get("definition", ())
                if item.name == name)
    ordered = []
    for item in found:
        if item.line in sites:
            item.kind = "definition"
        ordered.append(item)
    ordered.sort(key=lambda item: (0 if item.kind == "definition"
                                   else 1, item.line, item.column))
    if limit and limit > 0:
        return ordered[:limit]
    return ordered


def symbol_at(document, line, column=0, word=""):
    """The definition behind the symbol under the cursor, or None."""
    name = (word or "").strip()
    if not name:
        return None
    container = enclosing(document, "class", line, column)
    candidates = [name]
    if container is not None and container.name:
        if container.name != name:
            candidates.append("%s.%s" % (container.name, name))
    for candidate in candidates:
        for item in _targets(document, "definition"):
            if item.name == candidate:
                return item
    found = references(document, name, 0)
    return found[0] if found else None


def imports(document, query=""):
    """The import targets of a document, optionally filtered by a word."""
    wanted = (query or "").strip().lower()
    found = _targets(document, "import")
    if not wanted:
        return found
    return [item for item in found
            if wanted in item.name.lower()
            or wanted in item.detail.lower()]


def problems(document, kind="error"):
    """Parse errors, marker warnings, or both when kind is any."""
    if kind == "any":
        return _targets(document, "error") + _targets(document, "warning")
    return _targets(document, kind)


def sections(document):
    """Section banners and region markers of a document."""
    return _targets(document, "section")


def _nearest(items, line, column, direction, wrap=True):
    """The first item after (or before) the cursor, wrapping on demand."""
    ordered = sorted(items, key=lambda item: item.key())
    if not ordered:
        return None
    if direction >= 0:
        after = [item for item in ordered
                 if item.key() > (line, column)]
        if after:
            return after[0]
        return ordered[0] if wrap else None
    before = [item for item in ordered
              if item.key() < (line, column)]
    if before:
        return before[-1]
    return ordered[-1] if wrap else None


def locate(document, kind, line, column=0, word="", direction=1,
           wrap=True):
    """The navigation target of a kind nearest to the cursor.

    definition, reference and import follow the symbol under the cursor;
    the structural and problem kinds move to the next (direction=1) or
    previous (direction=-1) target. enclosing walks backwards and never
    wraps, so pressing it at the top of a file reports nothing.
        """
    name = (word or "").strip()
    if kind == "definition":
        return symbol_at(document, line, column, name)
    if kind == "reference":
        if not name:
            return None
        # "Go to reference" walks the *uses* of a symbol, so the declaration
        # it is defined at is skipped while moving forward through the file.
        found = [item for item in references(document, name, 0)
                 if item.kind != "definition"]
        return _nearest(found, line, column, direction, wrap)
    if kind == "import":
        found = imports(document, name) if name else imports(document)
        return _nearest(found, line, column, 1, wrap)
    if kind in PROBLEM_KINDS:
        return _nearest(problems(document, kind), line, column,
                        direction, wrap)
    if kind == "enclosing":
        return _nearest(sections(document), line, column, -1, wrap=False)
    # The structural kinds (class, function, variable, section) honour the
    # requested direction, so the same jump can walk forwards or backwards.
    return _nearest(_targets(document, kind), line, column, direction, wrap)


def summary(document):
    """A one-line capability report used by the status line."""
    _name, code, category = normalize_document(document)
    tables = scan_text(code, category)
    counts = dict((kind, len(tables.get(kind, ())))
                  for kind in SCAN_KINDS)
    counts["key"] = document_key(document)
    counts["lines"] = (code or "").count(chr(10)) + (1 if code else 0)
    return counts


def capabilities():
    """Describe the engine for the audit and report layers."""
    return {"kinds": [key for key, _lk, _f in NAV_KINDS],
            "problem_kinds": list(PROBLEM_KINDS),
            "languages": language_list(),
            "scanners": scanners(),
            "zero_dependency": True,
            "sandbox_safe": True,
            "hot_pluggable": True}
