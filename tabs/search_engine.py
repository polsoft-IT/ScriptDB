# -*- coding: utf-8 -*-
"""Search engine for the Editor tab.

Sixteen search kinds, one small pure-Python engine, no third-party dependency
and no Qt import at module level -- so every search can be unit tested and
driven from a command line exactly the way the GUI drives it:

    text         plain substring search (optional case sensitivity)
    string       a literal string with a match mode: substring / word / line /
                 prefix / suffix (the "find a phrase" search)
    regex        one regular expression per line
    multiline    a pattern that spans lines (re.DOTALL over the whole buffer)
    multifile    the same query across several documents at once, or across an
                 explicit list of files on disk
    files        every matching file below one directory, recursively
    workspace    every matching file below one or more project/workspace roots
    semantic     symbols by kind: function / class / method / variable /
                 constant / import (the "understand the code" search)
    imports      import / include / using / require, per language
    blocks       control-flow blocks: if / for / while / try / catch / switch
    comments     every comment (inline / block / docstring / commented-out code)
    ast          Python AST walk with a node-kind filter
    calls        every call site of a name (with the argument text)
    definitions  where a name is defined, per language
    references   every mention of a name, definition sites flagged
    todo         TODO / FIXME / XXX / HACK and friends

The engine knows nothing about widgets: it takes ``(name, code, category)``
documents and returns :class:`SearchHit` records. That keeps the GUI thin and
makes the whole engine reusable from the Analyzer, the Audit tab or a script.

Hot-plugging: the ``REGISTRY`` maps a kind name to its function, and
``register_kind`` adds a new one at run time::

    from tabs import search_engine
    search_engine.register_kind("license", my_license_finder)
"""

import ast
import os
import re

# ---------------------------------------------------------------------------
# Result record
# ---------------------------------------------------------------------------


class SearchHit(object):
    """One match: where it is, what matched and what kind of thing it is."""

    __slots__ = ("doc", "line", "column", "text", "kind", "detail")

    def __init__(self, doc, line, column, text, kind, detail=""):
        self.doc = doc            # document name (script name / file name)
        self.line = line          # 1-based line number (0 = document scope)
        self.column = column      # 0-based column
        self.text = text          # the matching line (trimmed for display)
        self.kind = kind          # result kind, e.g. "definition", "call"
        self.detail = detail      # extra context (signature, argument list...)

    def key(self):
        """Sort key: document order, then position."""
        return (self.doc, self.line, self.column)

    def __repr__(self):
        return "SearchHit(%r, %d, %d, %r, %r)" % (
            self.doc, self.line, self.column, self.text, self.kind)


class SearchResult(object):
    """The outcome of one search: the hits plus a human readable summary."""

    def __init__(self, kind, query, hits=(), note="", error=""):
        self.kind = kind
        self.query = query
        self.hits = list(hits)
        self.note = note
        self.error = error

    @property
    def count(self):
        return len(self.hits)

    def documents(self):
        """Names of the documents that produced at least one hit."""
        seen = []
        for hit in self.hits:
            if hit.doc not in seen:
                seen.append(hit.doc)
        return seen

    def by_kind(self):
        """{kind: count} so the UI can show a breakdown."""
        counts = {}
        for hit in self.hits:
            counts[hit.kind] = counts.get(hit.kind, 0) + 1
        return counts

    def summary(self):
        if self.error:
            return self.error
        parts = "%d hit(s) in %d document(s)" % (self.count, len(self.documents()))
        if self.note:
            parts += " - " + self.note
        return parts

    def __repr__(self):
        return "SearchResult(%r, %r, %d hit(s))" % (
            self.kind, self.query, self.count)


# ---------------------------------------------------------------------------
# Search kinds offered by the UI
# ---------------------------------------------------------------------------

SEARCH_KINDS = (
    ("text", "search_kind_text", "Text"),
    ("string", "search_kind_string", "String"),
    ("regex", "search_kind_regex", "Regex"),
    ("multiline", "search_kind_multiline", "Multiline"),
    ("multifile", "search_kind_multifile", "Many files"),
    ("files", "search_kind_files", "Directory"),
    ("workspace", "search_kind_workspace", "Workspace"),
    ("semantic", "search_kind_semantic", "Semantic"),
    ("imports", "search_kind_imports", "Imports"),
    ("blocks", "search_kind_blocks", "Code blocks"),
    ("comments", "search_kind_comments", "Comments"),
    ("ast", "search_kind_ast", "AST"),
    ("calls", "search_kind_calls", "Calls"),
    ("definitions", "search_kind_definitions", "Definitions"),
    ("references", "search_kind_references", "References"),
    ("todo", "search_kind_todo", "TODO / FIXME"),
)

# String-match sub-kinds: shown as a second combo when kind == "string".
STRING_MODES = (
    ("substring", "search_str_substring", "Substring"),
    ("word", "search_str_word", "Whole word"),
    ("line", "search_str_line", "Whole line"),
    ("prefix", "search_str_prefix", "Starts with"),
    ("suffix", "search_str_suffix", "Ends with"),
)

# Multifile sub-modes: shown as a second combo when kind == "multifile".
MULTIFILE_MODES = (
    ("text", "search_kind_text", "Text"),
    ("regex", "search_kind_regex", "Regex"),
    ("multiline", "search_kind_multiline", "Multiline"),
)

# Semantic sub-kinds: shown as a second combo when kind == "semantic".
# This is the "understand the code" search: functions, classes, methods,
# variables and constants, resolved from how the code declares them rather
# than from raw text.
SEMANTIC_KINDS = (
    ("any", "search_sem_any", "Any symbol"),
    ("function", "search_sem_function", "Functions"),
    ("class", "search_sem_class", "Classes"),
    ("method", "search_sem_method", "Methods"),
    ("variable", "search_sem_variable", "Variables"),
    ("constant", "search_sem_constant", "Constants"),
    ("import", "search_sem_import", "Imports"),
)

# Import sub-kinds: shown as a second combo when kind == "imports".
IMPORT_KINDS = (
    ("any", "search_imp_any", "Any import"),
    ("import", "search_kind_imports", "import"),
    ("from", "search_imp_from", "from ... import"),
    ("include", "search_imp_include", "include / #include"),
    ("using", "search_imp_using", "using / ."),
    ("require", "search_imp_require", "require / source"),
)

# Code-block sub-kinds: shown as a second combo when kind == "blocks".
BLOCK_KINDS = (
    ("any", "search_blk_any", "Any block"),
    ("if", "search_blk_if", "if"),
    ("for", "search_blk_for", "for"),
    ("while", "search_blk_while", "while"),
    ("try", "search_blk_try", "try"),
    ("catch", "search_blk_catch", "catch / except"),
    ("switch", "search_blk_switch", "switch / match"),
    ("function", "search_blk_function", "function / def"),
)

# Comment sub-kinds: shown as a second combo when kind == "comments".
COMMENT_KINDS = (
    ("any", "search_cmt_any", "Any comment"),
    ("inline", "search_cmt_inline", "Inline comment"),
    ("block", "search_cmt_block", "Block / doc comment"),
    ("docstring", "search_cmt_docstring", "Docstring"),
    ("empty", "search_cmt_empty", "Commented-out code"),
)

# Marker words picked up by the TODO search.
TODO_MARKERS = ("TODO", "FIXME", "XXX", "HACK", "BUG", "NOTE", "OPTIMIZE",
                "REVIEW", "DEPRECATED", "WARNING")
_TODO_RE = re.compile(
    r"\b(" + "|".join(TODO_MARKERS) + r")\b[ \t]*[:\-]?[ \t]*(.*)", re.IGNORECASE)

# Definition / call patterns per language family. Kept deliberately small: they
# match the declaration forms a reader searches for, not a full grammar.
_MODE = re.IGNORECASE
DEFINITION_PATTERNS = {
    "python": re.compile(
        r"^[ \t]*(?:def|class)\s+(\w+)|^[ \t]*(\w+)\s*(?::[^=]+)?=", re.MULTILINE),
    "shell": re.compile(
        r"^[ \t]*(?:function\s+)?([\w-]+)\s*\(\s*\)|^[ \t]*([\w-]+)\s*=",
        re.MULTILINE),
    "shell_dash": re.compile(
        r"^[ \t]*\$\s*([\w-]+)\s*=|^[ \t]*function\s+([\w-]+)", re.MULTILINE),
    "cpp": re.compile(
        r"^[ \t]*(?:[\w:<>\*&\[\]]+\s+)+(\w+)\s*\([^;]*\)\s*[\{;]"
        r"|^[ \t]*(?:class|struct|enum)\s+(\w+)", re.MULTILINE),
    "java": re.compile(
        r"^[ \t]*(?:public|private|protected|static|final|abstract|\s)*"
        r"(?:[\w<>\[\]]+\s+)?(\w+)\s*\([^;]*\)\s*[\{;]"
        r"|^[ \t]*(?:class|interface|enum|record)\s+(\w+)", re.MULTILINE),
    "javascript": re.compile(
        r"^[ \t]*(?:export\s+)?(?:async\s+)?function\s+(\w+)"
        r"|^[ \t]*(?:const|let|var)\s+(\w+)\s*="
        r"|^[ \t]*class\s+(\w+)", re.MULTILINE),
    "powershell": re.compile(
        r"^[ \t]*function\s+([\w-]+)|^[ \t]*\$([\w:]+)\s*=", re.MULTILINE | _MODE),
    "batch": re.compile(r"^[ \t]*:(\w+)|^[ \t]*set\s+([\w\s]+)=",
                        re.MULTILINE | _MODE),
    "vbscript": re.compile(
        r"^[ \t]*(?:Sub|Function|Class|Const|Dim)\s+(\w+)",
        re.MULTILINE | _MODE),
    "sql": re.compile(
        r"^[ \t]*CREATE\s+(?:TABLE|VIEW|INDEX|FUNCTION)\s+(?:IF NOT EXISTS\s+)?"
        r"([\w.]+)", re.MULTILINE | _MODE),
    "data": re.compile(r"^[ \t]*([\w.\-]+)\s*[:=]", re.MULTILINE),
    "html": re.compile(
        r"<!\-\s*(\w+)|^[ \t]*(?:function|var|let|const)\s+(\w+)",
        re.MULTILINE | _MODE),
    "ruby": re.compile(
        r"^[ \t]*def\s+([\w?!]+)|^[ \t]*class\s+(\w+)|^[ \t]*(\w+)\s*=",
        re.MULTILINE),
    "system": re.compile(r"^[ \t]*\[?([\w.\\ ]+)\]?", re.MULTILINE),
}

# Which pattern table entry belongs to which syntax-module key.
_MODULE_PATTERNS = {
    "python": "python",
    "bash": "shell",
    "powershell": "powershell",
    "cpp": "cpp",
    "java": "java",
    "javascript": "javascript",
    "batch": "batch",
    "vbscript": "vbscript",
    "sql": "sql",
    "json": "data",
    "html": "html",
    "ruby": "ruby",
    "system": "system",
}

# --- AST node kinds exposed by the "ast" search ----------------------------
AST_KINDS = (
    ("any", "search_ast_any", "Any node"),
    ("function", "search_ast_function", "Function"),
    ("class", "search_ast_class", "Class"),
    ("call", "search_ast_call", "Call"),
    ("import", "search_ast_import", "Import"),
    ("assign", "search_ast_assign", "Assignment"),
    ("name", "search_ast_name", "Name"),
    ("constant", "search_ast_constant", "Constant"),
    ("loop", "search_ast_loop", "Loop"),
    ("conditional", "search_ast_conditional", "Conditional"),
)


# --- Import patterns per language family (import/include/using/require) -----
# One entry per language, each a regex whose first non-empty group is the
# imported name/target. Used by both the "imports" kind and the semantic
# "import" sub-kind.
IMPORT_PATTERNS = {
    "python": re.compile(
        r"^[ \t]*import\s+([\w.]+)"
        r"|^[ \t]*from\s+([\w.]+)\s+import\b", re.MULTILINE),
    "shell": re.compile(
        r"^[ \t]*(?:source|\.)\s+([\w./~$-]+)", re.MULTILINE),
    "cpp": re.compile(
        r"^[ \t]*#\s*include\s*[<\"]([^>\"]+)[>\"]", re.MULTILINE),
    "java": re.compile(
        r"^[ \t]*import\s+(?:static\s+)?([\w.*]+)", re.MULTILINE),
    "javascript": re.compile(
        r"^[ \t]*import\b.*?from\s*[\"']([^\"']+)[\"']"
        r"|^[ \t]*import\s*[\"']([^\"']+)[\"']"
        r"|(?:require\s*\(\s*[\"']([^\"']+)[\"'])|require\s+[\"']([^\"']+)[\"']",
        re.MULTILINE),
    "powershell": re.compile(
        r"^[ \t]*(?:using|Import-Module|\.)\s+([\w.\/-]+)",
        re.MULTILINE | _MODE),
    "batch": re.compile(r"^[ \t]*call\s+([\w.:\/-]+)", re.MULTILINE | _MODE),
    "vbscript": re.compile(r"^[ \t]*\.", re.MULTILINE | _MODE),
    "sql": re.compile(
        r"^[ \t]*(?:source|include|\.import)\s+([\w./-]+)",
        re.MULTILINE | _MODE),
    "ruby": re.compile(
        r"^[ \t]*(?:require|require_relative|load)\s*_*[\"']([^\"']+)[\"']",
        re.MULTILINE),
    "html": re.compile(
        r"<script[^>]*src=[\"']([^\"']+)[\"']"
        r"|<link[^>]*href=[\"']([^\"']+)[\"']", re.IGNORECASE),
    "data": re.compile(
        r"^[ \t]*(?:include|using|base)\s*:?\s*([\w./\-]+)",
        re.MULTILINE | _MODE),
    "system": re.compile(
        r"^[ \t]*(?:include|import|load)\s*[=: ]\s*(.+)$",
        re.MULTILINE | _MODE),
}

# Which import pattern belongs to which syntax-module key.
_IMPORT_LANGUAGE = {
    "python": "python",
    "bash": "shell",
    "cpp": "cpp",
    "java": "java",
    "javascript": "javascript",
    "powershell": "powershell",
    "batch": "batch",
    "vbscript": "vbscript",
    "sql": "sql",
    "ruby": "ruby",
    "html": "html",
    "json": "data",
    "system": "system",
}

# Code-block patterns per language, keyed by block sub-kind. Each pattern
# matches the line that *opens* a block, so the hit points at the block head.
BLOCK_PATTERNS = {
    "if": re.compile(r"^[ \t]*[\}\]]*\s*if\s*[\s(:]", re.IGNORECASE),
    "for": re.compile(
        r"^[ \t]*[\}\]]*\s*for\s*[\s(]|^[ \t]*foreach\b", re.IGNORECASE),
    "while": re.compile(r"^[ \t]*[\}\]]*\s*while\s*[\s(]", re.IGNORECASE),
    "try": re.compile(r"^[ \t]*[\}\]]*\s*try\s*[:{ ]?", re.IGNORECASE),
    "catch": re.compile(
        r"^[ \t]*[\}\]]*\s*(?:catch|except|rescue)\b", re.IGNORECASE),
    "switch": re.compile(
        r"^[ \t]*[\}\]]*\s*(?:switch|case|match|select\s+case)\b",
        re.IGNORECASE),
    "function": re.compile(
        r"^[ \t]*(?:async\s+)?(?:def|function|func|sub)\s+"
        r"|^[ \t]*(?:\w+\s+)*\w+\s*\([^;]*\)\s*\{"
        r"|^[ \t]*\w+\s*\(\s*\)\s*\{", re.IGNORECASE),
}
_ALL_BLOCK_KINDS = ("if", "for", "while", "try", "catch", "switch",
                    "function")

# Comment patterns per language family (one-line and block comments).
COMMENT_PATTERNS = {
    "python": re.compile(r"#(.*)$"),
    "shell": re.compile(r"#(.*)$"),
    "cpp": re.compile(r"//(.*)$|/\*(.*?)\*/"),
    "java": re.compile(r"//(.*)$|/\*(.*?)\*/"),
    "javascript": re.compile(r"//(.*)$|/\*(.*?)\*/"),
    "powershell": re.compile(r"#(.*)$|<(#[^>]*#)>"),
    "batch": re.compile(r"^[ \t]*(?:rem|::)\s?(.*)$", re.IGNORECASE),
    "vbscript": re.compile(r"'(.*)$|^[ \t]*rem\s+(.*)$", re.IGNORECASE),
    "sql": re.compile(r"--(.*)$|/\*(.*?)\*/"),
    "ruby": re.compile(r"#(.*)$"),
    "html": re.compile(r"<!--(.*?)-->", re.DOTALL),
    "data": re.compile(r"^[ \t]*[#;](.*)$"),
    "system": re.compile(r"^[ \t]*[#;](.*)$"),
}

# Which comment pattern belongs to which syntax-module key.
_COMMENT_LANGUAGE = {
    "python": "python",
    "bash": "shell",
    "cpp": "cpp",
    "java": "java",
    "javascript": "javascript",
    "powershell": "powershell",
    "batch": "batch",
    "vbscript": "vbscript",
    "sql": "sql",
    "ruby": "ruby",
    "html": "html",
    "json": "data",
    "system": "system",
}

# A comment that looks like disabled code rather than prose.
_CODE_LIKE_RE = re.compile(
    r"[;{}]\s*$|=\s*|\b(?:def|class|if|for|while|return|import|function|var|let|const)\b")


def _language_key(category):
    """The syntax-module key a category maps to (never empty)."""
    from .syntax import module_for
    return module_for(category).key or "python"


def _is_method(lines, index):
    """True when the definition on line `index` is indented inside a class.

    A language-agnostic heuristic: a function definition whose indentation is
    deeper than the nearest preceding ``class`` line is a method.
    """
    line = lines[index]
    indent = len(line) - len(line.lstrip())
    if indent <= 0:
        return False
    for previous in range(index - 1, -1, -1):
        candidate = lines[previous]
        if not candidate.strip():
            continue
        other = len(candidate) - len(candidate.lstrip())
        if other >= indent:
            continue
        if re.match(r"^\s*class\s+\w+", candidate):
            return True
        # The first shallower line is the enclosing scope; if it is not a class
        # the indented def belongs to a function or a block, not a class.
        return False
    return False

def _is_constant(name):
    """True when a symbol name looks like a constant (UPPER_SNAKE_CASE)."""
    return bool(name) and bool(re.match(r"^[A-Z][A-Z0-9_]*$", name)) \
        and any(char.isalpha() for char in name)


def _ast_classes(kind):
    """Map an AST kind name onto the ast node classes it covers."""
    if kind == "function":
        return (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
    if kind == "class":
        return (ast.ClassDef,)
    if kind == "call":
        return (ast.Call,)
    if kind == "import":
        return (ast.Import, ast.ImportFrom)
    if kind == "assign":
        return (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.NamedExpr)
    if kind == "name":
        return (ast.Name, ast.Attribute)
    if kind == "constant":
        return (ast.Constant,)
    if kind == "loop":
        return (ast.For, ast.AsyncFor, ast.While, ast.comprehension)
    if kind == "conditional":
        return (ast.If, ast.IfExp, ast.Try, ast.Match)
    return ()


# ---------------------------------------------------------------------------
# Small text helpers
# ---------------------------------------------------------------------------


def _lines(code):
    return (code or "").splitlines()


def _clean(text, limit=200):
    """One trimmed display line, never longer than `limit` characters."""
    stripped = (text or "").strip()
    if len(stripped) > limit:
        stripped = stripped[:limit - 3] + "..."
    return stripped


def _compile(pattern, flags=0):
    """Compile a pattern, returning (compiled, error_message)."""
    try:
        return re.compile(pattern, flags), ""
    except re.error as exc:
        return None, "Invalid regular expression: %s" % exc


def _pattern_for_document(category):
    """The definition pattern for a script category."""
    from .syntax import module_for          # local import: keeps import light
    key = _MODULE_PATTERNS.get(module_for(category).key, "python")
    return DEFINITION_PATTERNS.get(key, DEFINITION_PATTERNS["python"])


def _definition_kind(line, language_key):
    """Classify a definition line into function / class / variable."""
    stripped = line.strip()
    lowered = stripped.lower()
    if language_key == "python":
        if lowered.startswith("class "):
            return "class"
        if lowered.startswith("def ") or lowered.startswith("async def "):
            return "function"
    elif language_key == "javascript":
        if lowered.startswith("class "):
            return "class"
        if "function" in lowered:
            return "function"
    elif language_key in ("cpp", "java"):
        if re.match(r"^\s*(class|struct|interface|enum|record)\b", lowered):
            return "class"
        if re.search(r"\w+\s*\([^;]*\)\s*[\{;]?\s*$", stripped):
            return "function"
    elif language_key in ("vbscript", "powershell", "bash", "ruby"):
        if re.match(r"^\s*(sub|function|def|class)\b", lowered):
            return "function"
    # The name being assigned is the first non-empty group of the definition
    # pattern; an UPPER_SNAKE_CASE name is reported as a constant.
    if _is_constant(_definition_name(line, language_key)):
        return "constant"
    return "variable"


def _definition_name(line, language_key):
    """The symbol name a definition line introduces (or an empty string)."""
    stripped = line.strip()
    lowered = stripped.lower()
    if lowered.startswith("class "):
        return stripped.split("class", 1)[1].strip().split()[0].split("(")[0]
    for keyword in ("async def ", "def ", "function ", "func ", "sub "):
        if lowered.startswith(keyword):
            return stripped[len(keyword):].strip().split("(")[0].strip()
    match = re.match(r"^[ \t]*(?:const|let|var|set|\$)?\s*([\w.$:]+)\s*[=:]", line)
    if match:
        return match.group(1)
    return ""


# ---------------------------------------------------------------------------
# Document collection
# ---------------------------------------------------------------------------


def normalize_documents(documents):
    """Accept (name, code, category) triples, dicts or editor-like objects."""
    out = []
    for item in documents or ():
        if isinstance(item, dict):
            out.append((item.get("name", ""), item.get("code", "") or "",
                        item.get("category", "Python")))
        elif isinstance(item, (tuple, list)):
            name = item[0] if len(item) > 0 else ""
            code = item[1] if len(item) > 1 else ""
            category = item[2] if len(item) > 2 else "Python"
            out.append((name, code or "", category))
        else:
            out.append((getattr(item, "name", ""), getattr(item, "code", ""),
                        getattr(item, "category", "Python")))
    return out


# ---------------------------------------------------------------------------
# 1. Text
# ---------------------------------------------------------------------------


def search_text(documents, query, case_sensitive=False, **options):
    """Plain substring search, one hit per line."""
    if not query:
        return SearchResult("text", query, error="Nothing to search for.")
    flags = 0 if case_sensitive else re.IGNORECASE
    pattern = re.compile(re.escape(query), flags)
    hits = []
    for name, code, _category in normalize_documents(documents):
        for number, line in enumerate(_lines(code), 1):
            for match in pattern.finditer(line):
                hits.append(SearchHit(name, number, match.start(),
                                      _clean(line), "text"))
    return SearchResult("text", query, hits)

# ---------------------------------------------------------------------------
# 1b. String (configurable literal match)
# ---------------------------------------------------------------------------


def search_string(documents, query, case_sensitive=False,
                  match="substring", **options):
    # A literal string with a match mode (no regex metacharacters).
    #
    # `match` selects how the query has to relate to what it is found in:
    #
    #     substring  the query appears anywhere on the line (like text)
    #     word       the query appears as a whole word
    #     line       a line is exactly the query (ignoring surrounding space)
    #     prefix     the line starts with the query
    #     suffix     the line ends with the query
    #
    # Because the query is treated as plain text, this is the 'find this exact
    # phrase' search: nothing in it is a pattern, so pasting text that contains
    # a dot, a comma or a parenthesis is safe.
    if not query:
        return SearchResult("string", query, error="Nothing to search for.")
    flags = 0 if case_sensitive else re.IGNORECASE
    literal = re.escape(query)
    patterns = {
        "substring": literal,
        "word": r"(?<!\w)" + literal + r"(?!\w)",
        "prefix": r"^[ \t]*" + literal,
        "suffix": literal + r"[ \t]*$",
    }
    if match == "line":
        pattern, error = _compile(r"^[ \t]*" + literal + r"[ \t]*$", flags)
    else:
        pattern, error = _compile(patterns.get(match, literal), flags)
    if error:
        return SearchResult("string", query, error=error)
    hits = []
    for name, code, _category in normalize_documents(documents):
        for number, line in enumerate(_lines(code), 1):
            found = pattern.search(line)
            if found is None:
                continue
            hits.append(SearchHit(name, number, found.start(), _clean(line),
                                  "string", match))
    return SearchResult("string", query, hits,
                        note="match=%s" % (match or "substring"))

# ---------------------------------------------------------------------------
# 2. Regex
# ---------------------------------------------------------------------------


def search_regex(documents, query, case_sensitive=False, **options):
    """One regular expression evaluated line by line."""
    if not query:
        return SearchResult("regex", query, error="Nothing to search for.")
    flags = 0 if case_sensitive else re.IGNORECASE
    pattern, error = _compile(query, flags)
    if error:
        return SearchResult("regex", query, error=error)
    hits = []
    for name, code, _category in normalize_documents(documents):
        for number, line in enumerate(_lines(code), 1):
            match = pattern.search(line)
            if match is None:
                continue
            groups = [g for g in match.groups() if g is not None]
            detail = " -> ".join(groups) if groups else ""
            hits.append(SearchHit(name, number, match.start(), _clean(line),
                                  "regex", detail))
    return SearchResult("regex", query, hits)


# ---------------------------------------------------------------------------
# 3. Multiline
# ---------------------------------------------------------------------------


def search_multiline(documents, query, case_sensitive=False,
                     dot_all=True, max_matches=500, **options):
    """A pattern that is allowed to span line breaks.

    The buffer is searched as one string (DOTALL by default) and each match is
    mapped back onto the line it starts on, so a multi-line construct such as a
    ``def`` header followed by its body can be found in one go.
    """
    if not query:
        return SearchResult("multiline", query, error="Nothing to search for.")
    flags = re.MULTILINE | (re.DOTALL if dot_all else 0)
    if not case_sensitive:
        flags |= re.IGNORECASE
    pattern, error = _compile(query, flags)
    if error:
        return SearchResult("multiline", query, error=error)
    hits = []
    truncated = False
    for name, code, _category in normalize_documents(documents):
        text = code or ""
        for match in pattern.finditer(text):
            if len(hits) >= max_matches:
                truncated = True
                break
            start = match.start()
            line = text.count("\n", 0, start) + 1
            column = start - (text.rfind("\n", 0, start) + 1)
            body = match.group(0)
            lines = body.count("\n") + 1
            first = _clean(body.splitlines()[0] if body else "")
            hits.append(SearchHit(name, line, column, first, "multiline",
                                  "%d line(s) matched" % lines))
        if truncated:
            break
    note = "showing the first %d matches" % max_matches if truncated else ""
    return SearchResult("multiline", query, hits, note=note)


# ---------------------------------------------------------------------------
# 4. Many files
# ---------------------------------------------------------------------------


def search_multifile(documents, query, case_sensitive=False, mode="text",
                     paths=(), encoding="utf-8", max_files=5000, **options):
    """Run one of the textual searches across every document at once.

    `mode` selects text / regex / multiline; the per-document hits carry the
    document name, which is what makes the multi-file view useful.
    """
    runners = {"text": search_text, "string": search_string,
               "regex": search_regex, "multiline": search_multiline}
    runner = runners.get(mode, search_text)
    pool = list(normalize_documents(documents))
    skipped = 0
    if paths:
        loaded, skipped = load_paths(paths, encoding=encoding, max_files=max_files)
        pool.extend(loaded)
    result = runner(pool, query, case_sensitive=case_sensitive, **options)
    result.kind = "multifile"
    note = "%s over %d file(s)" % (mode, len(pool))
    if skipped:
        note += ", skipped %d unreadable" % skipped
    result.note = note + (" - " + result.note if result.note else "")
    return result


# ---------------------------------------------------------------------------
# 5. Semantic (functions / classes / methods / variables / constants / imports)
# ---------------------------------------------------------------------------


def search_semantic(documents, query, symbol_kind="any", **options):
    """Find symbols by kind, optionally filtered by a name fragment.

    This is the "I know roughly what it is called" search: it looks at how the
    code *declares* things rather than at raw text.
    """
    wanted = (query or "").strip().lower()
    hits = []
    for name, code, category in normalize_documents(documents):
        language_key = _language_key(category)
        pattern = _pattern_for_document(category)
        import_pattern = IMPORT_PATTERNS.get(_IMPORT_LANGUAGE.get(language_key))
        lines = _lines(code)
        for number, line in enumerate(lines, 1):
            stripped = line.strip()
            # Skip obvious comment lines so a commented-out definition is not
            # reported as a live symbol.
            if stripped.startswith(("#", "//", "rem ", ";", "--", "'")):
                continue
            match = pattern.match(line)
            if not match:
                continue
            groups = [g for g in match.groups() if g]
            if not groups:
                continue
            symbol = groups[0]
            kind = _definition_kind(line, language_key)
            # An import line is also matched by the assignment pattern in some
            # languages; report it as an import so the two searches agree.
            if import_pattern is not None and import_pattern.match(line):
                kind = "import"
            elif kind == "function" and _is_method(lines, number - 1):
                kind = "method"
            if symbol_kind not in ("any", None, "") and kind != symbol_kind:
                continue
            if wanted and wanted not in symbol.lower():
                continue
            hits.append(SearchHit(name, number, max(0, line.find(symbol)),
                                  _clean(stripped), kind, symbol))
    return SearchResult("semantic", query, hits,
                        note="kind=%s" % (symbol_kind or "any"))


# ---------------------------------------------------------------------------
# 5b. Imports (import / include / using / require)
# ---------------------------------------------------------------------------


def search_imports(documents, query, import_kind="any", **options):
    """Every import-like statement, per language.""

    One search covers the whole family of "bring something in" statements:
    ``import`` / ``from ... import`` (Python), ``#include`` (C/C++),
    ``import`` (Java/JS), ``using`` / ``Import-Module`` / dot-sourcing
    (PowerShell), ``require`` / ``source`` / ``load`` (Ruby/shell) and the
    ``include``/``using``/``base`` directives of the config formats.

    `import_kind` narrows the set: any / import / from / include / using /
    require. `query` filters the imported name.
    """
    wanted = (query or "").strip().lower()
    hits = []
    for name, code, category in normalize_documents(documents):
        language_key = _language_key(category)
        pattern = IMPORT_PATTERNS.get(_IMPORT_LANGUAGE.get(language_key))
        if pattern is None:
            continue
        for number, line in enumerate(_lines(code), 1):
            match = pattern.search(line)
            if match is None:
                continue
            targets = [g for g in match.groups() if g]
            target = targets[0] if targets else line.strip()
            kind = _import_subkind(line, target)
            if import_kind not in ("any", None, "") and kind != import_kind:
                continue
            if wanted and wanted not in target.lower():
                continue
            hits.append(SearchHit(name, number, max(0, line.find(target)),
                                  _clean(line.strip()), kind, target))
    return SearchResult("imports", query, hits,
                        note="kind=%s" % (import_kind or "any"))


def _import_subkind(line, target):
    """Classify an import line into import / from / include / using / require."""
    lowered = line.strip().lower()
    if lowered.startswith("from "):
        return "from"
    if "#include" in lowered or lowered.startswith("include"):
        return "include"
    if lowered.startswith(("using ", ".", "import-module")):
        return "using"
    if "require" in lowered or lowered.startswith(
            ("source", "load", ".import", "call ")):
        return "require"
    return "import"

# ---------------------------------------------------------------------------
# 5c. Code blocks (if / for / while / try / catch ...)
# ---------------------------------------------------------------------------


def search_blocks(documents, query, block_kind="any", **options):
    """Control-flow blocks: the line that opens each if / for / while / try /
    catch / switch / function block.

    `block_kind` narrows the set (`any` reports every block); `query` filters
    the block head text. This is language-agnostic: it matches the keywords a
    reader looks for rather than a full parse, so it works for every category.
    """
    wanted = (query or "").strip().lower()
    kinds = _ALL_BLOCK_KINDS if block_kind in ("any", None, "") else (block_kind,)
    hits = []
    for name, code, category in normalize_documents(documents):
        lines = _lines(code)
        for number, line in enumerate(lines, 1):
            stripped = line.strip()
            if _is_comment_line(stripped, category):
                continue
            for kind in kinds:
                pattern = BLOCK_PATTERNS.get(kind)
                if pattern is None or not pattern.match(line):
                    continue
                if wanted and wanted not in stripped.lower():
                    continue
                hits.append(SearchHit(name, number, max(0, line.find(stripped)),
                                      _clean(stripped), kind))
                break
    return SearchResult("blocks", query, hits,
                        note="block=%s" % (block_kind or "any"))

# ---------------------------------------------------------------------------
# 5d. Comments
# ---------------------------------------------------------------------------


def search_comments(documents, query, comment_kind="any", **options):
    """Every comment, optionally filtered by its text.""

    `comment_kind` narrows the set: any / inline / block / docstring / empty
    (the last being a comment that looks like disabled code rather than
    prose). `query` filters the comment text.
    """
    wanted = (query or "").strip().lower()
    hits = []
    for name, code, category in normalize_documents(documents):
        language_key = _language_key(category)
        pattern = COMMENT_PATTERNS.get(_COMMENT_LANGUAGE.get(language_key))
        docstring = _python_docstrings(code) if language_key == "python" else {}
        for number, line in enumerate(_lines(code), 1):
            stripped = line.strip()
            kind = ""
            body = ""
            if pattern is not None:
                match = pattern.search(line)
                if match is not None:
                    groups = [g for g in match.groups() if g]
                    body = groups[0].strip() if groups else line.strip()
                    kind = "block" if _looks_like_block(line) else "inline"
            if number in docstring:
                kind = "docstring"
                body = docstring[number]
            if not kind:
                continue
            if kind != "docstring" and _CODE_LIKE_RE.search(body or stripped):
                kind = "empty"
            if comment_kind not in ("any", None, "") and kind != comment_kind:
                continue
            if wanted and wanted not in (body or stripped).lower():
                continue
            hits.append(SearchHit(name, number, max(0, line.find(stripped)),
                                  _clean(stripped), kind, _clean(body, 120)))
    return SearchResult("comments", query, hits,
                        note="kind=%s" % (comment_kind or "any"))


def _looks_like_block(line):
    """True when a comment line is a whole-line (block-style) comment."""
    return bool(re.match(r"^[ \t]*(?://|/\*|\*|#|--|'|;|<!--)", line))


def _is_comment_line(stripped, category):
    """True when a stripped line is entirely a comment."""
    language_key = _language_key(category)
    markers = {
        "python": ("#",), "shell": ("#",), "cpp": ("//", "/*", "*"),
        "java": ("//", "/*", "*"), "javascript": ("//", "/*", "*"),
        "powershell": ("#",), "batch": ("rem ", "::"), "vbscript": ("'",),
        "sql": ("--",), "ruby": ("#",), "html": ("<!--",),
        "data": ("#", ";"), "system": ("#", ";"),
    }.get(language_key, ("#", "//"))
    return stripped.lower().startswith(
        tuple(m.lower() for m in markers)) if stripped else False

def _python_docstrings(code):
    """{line_number: text} for every Python docstring, via the AST."""
    out = {}
    try:
        tree = ast.parse(code or "")
    except SyntaxError:
        return out
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        value = getattr(first, "value", None)
        if isinstance(first, ast.Expr) \
                and isinstance(getattr(value, "value", None), str):
            raw = value.value.strip()
            text = raw.splitlines()[0] if raw else "(empty docstring)"
            out[first.lineno] = text
    return out
# ---------------------------------------------------------------------------
# 6. AST search (Python)
# ---------------------------------------------------------------------------


def search_ast(documents, query, node_kind="any", **options):
    """Walk the Python AST and collect nodes of one kind.

    Scripts that are not valid Python, or that use another language, are
    skipped with a note instead of failing the whole search.
    """
    wanted = (query or "").strip().lower()
    kinds = _ast_classes(node_kind)
    hits = []
    skipped = []
    for name, code, category in normalize_documents(documents):
        from .syntax import module_for
        if module_for(category).key != "python":
            skipped.append(name)
            continue
        try:
            tree = ast.parse(code or "")
        except SyntaxError:
            skipped.append(name)
            continue
        for node in ast.walk(tree):
            if kinds and not isinstance(node, kinds):
                continue
            label, detail = _describe_ast_node(node)
            if not label:
                continue
            if wanted and wanted not in label.lower():
                continue
            line = getattr(node, "lineno", 1)
            column = getattr(node, "col_offset", 0)
            hits.append(SearchHit(name, line, column, label,
                                  node_kind if node_kind != "any" else "ast",
                                  detail))
    note = "%s node(s)" % (node_kind or "any")
    if skipped:
        note += " - skipped %d non-Python/invalid: %s" % (
            len(skipped), ", ".join(skipped[:5]))
    return SearchResult("ast", query, hits, note=note)


def _argument_names(arguments):
    # Flatten an ast.arguments container into a list of parameter names.
    if arguments is None:
        return []
    names = [a.arg for a in getattr(arguments, "posonlyargs", [])]
    names += [a.arg for a in getattr(arguments, "args", [])]
    if getattr(arguments, "vararg", None) is not None:
        names.append(arguments.vararg.arg)
    names += [a.arg for a in getattr(arguments, "kwonlyargs", [])]
    if getattr(arguments, "kwarg", None) is not None:
        names.append(arguments.kwarg.arg)
    return names

def _describe_ast_node(node):
    """A display label and a short detail for one AST node."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        # node.args is an ast.arguments container, not a sequence -- the
        # parameters live in .args plus the vararg / kwarg / kwonly slots.
        args = _argument_names(node.args)
        return "%s(%s)" % (node.name, ", ".join(args)), "definition"
    if isinstance(node, ast.Lambda):
        return "lambda", "lambda"
    if isinstance(node, ast.ClassDef):
        return node.name, "class"
    if isinstance(node, ast.Call):
        try:
            callee = ast.unparse(node.func)
        except Exception:
            callee = getattr(node.func, "id", "?")
        try:
            args = ast.unparse(ast.Tuple(elts=node.args))
        except Exception:
            args = "..."
        return "%s(%s)" % (callee, args), "call"
    if isinstance(node, ast.Import):
        return ", ".join(a.name for a in node.names), "import"
    if isinstance(node, ast.ImportFrom):
        return "from %s import %s" % (
            node.module or "", ", ".join(a.name for a in node.names)), "import"
    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign,
                         ast.NamedExpr)):
        try:
            target = ast.unparse(node.target if hasattr(node, "target")
                                 else node.targets[0])
        except Exception:
            target = "?"
        return target, "assignment"
    if isinstance(node, ast.Name):
        return node.id, "name"
    if isinstance(node, ast.Attribute):
        try:
            return ast.unparse(node), "attribute"
        except Exception:
            return node.attr, "attribute"
    if isinstance(node, ast.Constant):
        value = repr(node.value)
        return _clean(value, 60), "constant"
    if isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
        return "loop", "loop"
    if isinstance(node, (ast.If, ast.IfExp, ast.Try, ast.Match)):
        return type(node).__name__, "conditional"
    return "", ""


def ast_search_available(category):
    """True when the AST search can parse this category."""
    from .syntax import module_for
    return module_for(category).key == "python"


# ---------------------------------------------------------------------------
# 7. Call sites
# ---------------------------------------------------------------------------


def search_calls(documents, query, include_methods=True, **options):
    """Every place `query` is called.

    A call is ``name(`` not preceded by ``def``/``function``/``class`` (so a
    definition is not reported as a call) and, when `include_methods` is off,
    not preceded by a dot (so ``obj.name()`` is excluded).
    """
    name = (query or "").strip()
    if not name:
        return SearchResult("calls", query, error="No function name given.")
    escaped = re.escape(name)
    call_re, error = _compile(r"(?<![\w.])" + escaped + r"\s*\(", re.IGNORECASE)
    if error:
        return SearchResult("calls", query, error=error)
    method_re, _ = _compile(r"\." + escaped + r"\s*\(", re.IGNORECASE)
    def_re, _ = _compile(r"\b(?:def|function|class|sub|func)\s+" + escaped + r"\b",
                         re.IGNORECASE)
    hits = []
    for doc, code, _category in normalize_documents(documents):
        for number, line in enumerate(_lines(code), 1):
            if def_re.search(line):
                continue                      # the definition, not a call
            for match in call_re.finditer(line):
                args = _argument_text(line, match.end() - 1)
                hits.append(SearchHit(doc, number, match.start(),
                                      _clean(line), "call", args))
            if include_methods and method_re is not None:
                for match in method_re.finditer(line):
                    args = _argument_text(line, match.end() - 1)
                    hits.append(SearchHit(doc, number, match.start(),
                                          _clean(line), "method call", args))
    return SearchResult("calls", query, hits,
                        note="arguments shown in the detail column")


def _argument_text(line, open_paren):
    """The text inside the parentheses starting at `open_paren`."""
    depth = 0
    for index in range(open_paren, len(line)):
        char = line[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return _clean(line[open_paren + 1:index], 80)
    return _clean(line[open_paren + 1:], 80)


# ---------------------------------------------------------------------------
# 8. Definitions
# ---------------------------------------------------------------------------


def search_definitions(documents, query, **options):
    """Where `query` is defined, in the language of each document."""
    name = (query or "").strip()
    if not name:
        return SearchResult("definitions", query, error="No symbol name given.")
    exact, error = _compile(r"(?<![\w])" + re.escape(name) + r"(?![\w])",
                            re.IGNORECASE)
    if error:
        return SearchResult("definitions", query, error=error)
    hits = []
    for doc, code, category in normalize_documents(documents):
        from .syntax import module_for
        language_key = module_for(category).key
        pattern = _pattern_for_document(category)
        for number, line in enumerate(_lines(code), 1):
            match = pattern.match(line)
            if not match:
                continue
            groups = [g for g in match.groups() if g]
            if not groups or not exact.search(groups[0]):
                continue
            hits.append(SearchHit(doc, number, max(0, line.find(groups[0])),
                                  _clean(line.strip()),
                                  _definition_kind(line, language_key),
                                  groups[0]))
    return SearchResult("definitions", query, hits)


# ---------------------------------------------------------------------------
# 9. References
# ---------------------------------------------------------------------------


def search_references(documents, query, mark_definitions=True, **options):
    """Every mention of `query`, with definition sites flagged as such."""
    name = (query or "").strip()
    if not name:
        return SearchResult("references", query, error="No symbol name given.")
    word_re, error = _compile(r"(?<![\w.])" + re.escape(name) + r"(?![\w])",
                              re.IGNORECASE)
    if error:
        return SearchResult("references", query, error=error)
    hits = []
    for doc, code, category in normalize_documents(documents):
        pattern = _pattern_for_document(category)
        for number, line in enumerate(_lines(code), 1):
            match = word_re.search(line)
            if match is None:
                continue
            kind = "reference"
            if mark_definitions:
                definition = pattern.match(line)
                if definition:
                    groups = [g for g in definition.groups() if g]
                    if groups and groups[0] == name:
                        kind = "definition"
            hits.append(SearchHit(doc, number, match.start(),
                                  _clean(line.strip()), kind))
    return SearchResult("references", query, hits)


# ---------------------------------------------------------------------------
# 10. TODO / FIXME
# ---------------------------------------------------------------------------


def search_todo(documents, query="", markers=None, **options):
    """TODO / FIXME / XXX and friends, optionally filtered by a word.

    `markers` narrows the marker set (e.g. ``("TODO",)``); `query` filters the
    text after the marker.
    """
    wanted = (query or "").strip().lower()
    allowed = tuple(m.upper() for m in markers) if markers else None
    hits = []
    for doc, code, _category in normalize_documents(documents):
        for number, line in enumerate(_lines(code), 1):
            match = _TODO_RE.search(line)
            if match is None:
                continue
            marker = match.group(1).upper()
            if allowed and marker not in allowed:
                continue
            note = _clean(match.group(2), 120)
            if wanted and wanted not in note.lower() and wanted not in marker.lower():
                continue
            hits.append(SearchHit(doc, number, match.start(),
                                  _clean(line.strip()), marker, note))
    return SearchResult("todo", query, hits,
                        note="markers %s" % ", ".join(
                            allowed or TODO_MARKERS))


# ---------------------------------------------------------------------------
# Filesystem search
# ---------------------------------------------------------------------------

# The textual runners a filesystem/workspace/multifile search can dispatch to.
_TEXT_RUNNERS = {
    "text": search_text,
    "string": search_string,
    "regex": search_regex,
    "multiline": search_multiline,
}


def read_file(path, encoding="utf-8"):
    # Read one text file, returning (text, error); never raises.
    try:
        with open(path, "r", encoding=encoding, errors="replace") as handle:
            return handle.read(), ""
    except OSError as exc:
        return None, "%s: %s" % (path, exc)


def load_paths(paths, encoding="utf-8", max_files=5000, extensions=()):
    # Load an explicit list of files/directories as (documents, skipped).
    #
    # A path that is a file is read directly; a path that is a directory is
    # walked recursively. `extensions` narrows which files are read. This is the
    # shared loader behind the multi-file, directory and workspace searches.
    allowed = {str(ext).lower().lstrip(".") for ext in (extensions or ()) if ext}
    documents = []
    skipped = 0
    seen = set()

    def _take(path, root):
        nonlocal skipped
        if len(documents) >= max_files:
            return False
        name = os.path.relpath(path, root) if root else os.path.basename(path)
        if name in seen:
            return True
        if allowed and os.path.splitext(path)[1].lower().lstrip(".") not in allowed:
            return True
        text, _error = read_file(path, encoding=encoding)
        if text is None:
            skipped += 1
            return True
        seen.add(name)
        documents.append((name, text, "Python"))
        return True
    for raw in (paths or ()):
        path = os.path.abspath(os.path.expanduser(str(raw or "")))
        if os.path.isdir(path):
            for directory, _names, filenames in os.walk(path):
                filenames.sort()
                for filename in filenames:
                    if not _take(os.path.join(directory, filename), path):
                        break
                if len(documents) >= max_files:
                    break
        elif os.path.isfile(path):
            _take(path, os.path.dirname(path))
        else:
            skipped += 1
        if len(documents) >= max_files:
            break
    return documents, skipped

def search_filesystem(root, query, mode="text", case_sensitive=False,
                      max_files=5000, extensions=(), encoding="utf-8",
                      **options):
    '''Search files recursively below ``root`` using the normal text engine.'''
    root = os.path.abspath(os.path.expanduser(root or ""))
    if not os.path.isdir(root):
        return SearchResult(mode, query, error="Directory not found: %s" % root)
    documents, skipped = load_paths([root], encoding=encoding,
                                    max_files=max_files, extensions=extensions)
    runner = _TEXT_RUNNERS.get(mode, search_text)
    result = runner(documents, query, case_sensitive=case_sensitive, **options)
    result.kind = mode
    note = "recursive search in %s (%d file(s))" % (root, len(documents))
    if skipped:
        note += ", skipped %d unreadable file(s)" % skipped
    if len(documents) >= max_files:
        note += ", file limit reached"
    result.note = note + (" - " + result.note if result.note else "")
    return result

def search_workspace(roots, query, mode="text", case_sensitive=False,
                     max_files=5000, extensions=(), encoding="utf-8", **options):
    '''Search every open workspace/project root recursively.'''
    roots = list(dict.fromkeys(os.path.abspath(os.path.expanduser(root))
                               for root in (roots or ()) if root))
    all_hits = []
    notes = []
    for root in roots:
        result = search_filesystem(root, query, mode=mode,
                                   case_sensitive=case_sensitive,
                                   max_files=max_files, extensions=extensions,
                                   encoding=encoding, **options)
        if result.error:
            notes.append(result.error)
            continue
        all_hits.extend(result.hits)
        notes.append(result.note)
    if not roots:
        return SearchResult(mode, query, error="No workspace directories configured.")
    return SearchResult(mode, query, all_hits, note="; ".join(notes))

# --- Registered kinds for the two filesystem searches ----------------------
# The functions above take their target (directory / roots) as an argument,
# which the UI supplies from its scope row. These thin wrappers make the same
# searches selectable as *kinds*, so they run over default locations when no
# explicit root is given.


def search_directory(documents=(), query="", root="", **options):
    # The 'directory' kind: recursive search below `root` (or cwd).
    target = root or options.pop("root", "") or os.getcwd()
    result = search_filesystem(target, query, **options)
    result.kind = "files"
    return result

def search_workspace_kind(documents=(), query="", roots=(), **options):
    # The 'workspace' kind: recursive search below every open project root.
    targets = list(roots or options.pop("roots", ()) or ())
    if not targets:
        base = options.pop("base_dir", "") or os.getcwd()
        targets = [base]
    result = search_workspace(targets, query, **options)
    result.kind = "workspace"
    return result
# ---------------------------------------------------------------------------
# Registry (hot-pluggable) and the single entry point
# ---------------------------------------------------------------------------

REGISTRY = {
    "text": search_text,
    "string": search_string,
    "regex": search_regex,
    "multiline": search_multiline,
    "multifile": search_multifile,
    "files": search_directory,
    "workspace": search_workspace_kind,
    "semantic": search_semantic,
    "imports": search_imports,
    "blocks": search_blocks,
    "comments": search_comments,
    "ast": search_ast,
    "calls": search_calls,
    "definitions": search_definitions,
    "references": search_references,
    "todo": search_todo,
}


def register_kind(name, finder, replace_existing=True):
    """Hot-plug a new search kind. Returns the function for chaining."""
    if not replace_existing and name in REGISTRY:
        return REGISTRY[name]
    REGISTRY[name] = finder
    return finder


def unregister_kind(name):
    return REGISTRY.pop(name, None) is not None


def kinds():
    """The registered kind names, in the order the UI should show them."""
    ordered = [key for key, _lk, _f in SEARCH_KINDS if key in REGISTRY]
    extra = [key for key in REGISTRY if key not in ordered]
    return ordered + extra


def search(documents, query, kind="text", **options):
    """Run one search.

    `documents` may be (name, code, category) triples, dicts or objects with
    those attributes. Unknown kinds and bad patterns come back as a
    :class:`SearchResult` carrying an ``error`` instead of raising, so the GUI
    never has to guard the call.
    """
    finder = REGISTRY.get(kind)
    if finder is None:
        return SearchResult(kind, query, error="Unknown search kind: %s" % kind)
    try:
        return finder(documents, query, **options)
    except Exception as exc:                       # never break the UI
        return SearchResult(kind, query, error="Search failed: %s" % exc)


def capabilities():
    """Describe the engine for the audit/report layer."""
    return {
        "kinds": kinds(),
        "count": len(REGISTRY),
        "text": True,
        "string": True,
        "regex": True,
        "multiline": True,
        "multifile": True,
        "directory_recursive": True,
        "workspace": True,
        "string_modes": [key for key, _lk, _f in STRING_MODES],
        "semantic_kinds": [key for key, _lk, _f in SEMANTIC_KINDS],
        "import_kinds": [key for key, _lk, _f in IMPORT_KINDS],
        "block_kinds": [key for key, _lk, _f in BLOCK_KINDS],
        "comment_kinds": [key for key, _lk, _f in COMMENT_KINDS],
        "zero_dependency": True,
        "sandbox_safe": True,
        "hot_pluggable": True,
        "ast_available": True,
    }
