# -*- coding: utf-8 -*-
"""Syntax module protocol for the Editor tab.

Every supported language lives in its own module under ``tabs/syntax/`` and
exposes exactly one ``SyntaxModule`` subclass. A module describes *how* the
language is written; it never touches Qt, so a language can be added, replaced
or removed without rewriting the editor.

A module provides:

    key             the family key used by the editor ("python", "cpp", ...)
    label           human readable name shown in the language picker
    aliases         lower-case strings that map a script category to the key
    extensions      file extensions (with dot) that map to the key
    keywords        reserved words that get the keyword colour
    builtins        well known names that get the built-in colour
    line_comment    character that starts a comment in the *default* dialect
    comments        optional {dialect: marker} map, e.g. {"batch": "rem"}
    block_comments  optional ((open, close), ...) pairs
    string_quotes   optional (open, close) pairs for non-standard strings
    indent_triggers optional characters that open an indented block
    redundant_with  optional built-in names this module replaces

Everything is hot-pluggable at runtime through ``tabs/syntax/registry.py``::

    replace_module(MyPythonModule)   # override a built-in language
    register_module(MySqlModule)     # add a new language
    unregister_module("ruby")        # drop a language

Because the modules are plain data plus a few pure helpers, they are safe to
import from a sandbox (no shell, no eval, standard library only).
"""


class SyntaxModule(object):
    """Base class for one hot-pluggable language syntax module."""

    key = "python"
    label = "Python"
    aliases = ()
    extensions = ()
    keywords = ()
    builtins = ()
    # Comment marker used when the dialect has no dedicated one.
    line_comment = "#"
    # Optional {dialect: marker}; the dialect is derived from the category.
    comments = {}
    # Optional ((open, close), ...) pairs, longest marker first.
    block_comments = ()
    # Optional ((open, close), ...) string delimiters, e.g. VBScript's "".
    string_quotes = ()
    # Characters that open an indented block (used by smart indent).
    indent_triggers = (":", "{")
    # Built-in names from other modules that this module makes redundant.
    redundant_with = ()
    # True when the language is conventionally written in any case (SQL).
    case_insensitive = False

    # ------------------------------------------------------------ dialect
    def dialect(self, category):
        """The comment dialect for a script category (lower-cased)."""
        lowered = (category or "").lower()
        for name in self.comments:
            if name in lowered:
                return name
        return ""

    def comment_marker(self, category=""):
        """The single-line comment marker for a category."""
        return self.comments.get(self.dialect(category), self.line_comment)

    def line_comment_markers(self, category=""):
        """Every single-line comment marker (for cache keys and tests)."""
        markers = {self.line_comment}
        markers.update(self.comments.values())
        return tuple(sorted(m for m in markers if m))

    def block_comment_pairs(self, category=""):
        """The block comment pairs of the module, longest opener first."""
        return tuple(sorted(self.block_comments,
                            key=lambda pair: len(pair[0]), reverse=True))

    # ------------------------------------------------------------- config
    def keyword_set(self):
        words = self.keywords
        if self.case_insensitive:
            return frozenset(word.lower() for word in words)
        return frozenset(words)

    def builtin_set(self):
        words = self.builtins
        if self.case_insensitive:
            return frozenset(word.lower() for word in words)
        return frozenset(words)

    # -------------------------------------------------------------- engine
    def highlighter(self, theme, low_graphics=False):
        """A highlighter for one document (see ``highlighter.py``)."""
        from .highlighter import SyntaxHighlighter
        return SyntaxHighlighter(self, theme, low_graphics)

    def describe(self):
        """A small, JSON-friendly summary of the module (for menus/tests)."""
        return {
            "key": self.key,
            "label": self.label,
            "aliases": list(self.aliases),
            "extensions": list(self.extensions),
            "keywords": len(self.keywords),
            "builtins": len(self.builtins),
            "comments": dict(self.comments),
        }
