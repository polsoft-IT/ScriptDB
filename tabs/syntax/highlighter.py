# -*- coding: utf-8 -*-
"""The line highlighter used by every syntax module.

The highlighter is deliberately a pure function of (module, line, theme): it
returns a list of ``(start, length, colour key)`` spans and never imports Qt.
That keeps the language modules testable from a plain Python shell and lets the
editor reuse the exact same spans for the document and for exported previews.

Colouring order (first match wins, because spans are laid down in this order):

    1. block comments   (triple quotes, /* ... */, <!-- ... -->, ...)
    2. line comments    (the rest of the line is one span)
    3. strings          (quotes, plus any module specific delimiters)
    4. decorators       (@name, #name, [Section], <tag ...>)
    5. keywords
    6. built-ins
    7. numbers
"""

import re

# Colour keys every theme provides; the editor maps them onto its palette.
SPAN_KINDS = ("comment", "string", "decorator", "keyword", "builtin", "number")

# Word characters considered part of an identifier.
_IDENT = r"[A-Za-z_]\w*"

# A bare number: integer, float, hex and a few common literal forms.
_NUMBER = re.compile(
    r"0[xX][0-9a-fA-F]+|\d+(?:\.\d+)?(?:[eE][-+]?\d+)?[fFlLdD]?\b|\.\d+")

# Annotation / attribute shapes shared by most languages.
_DECORATOR = re.compile(r"^\s*(@[\w.]+|#\w+|\[[A-Za-z][\w ]*\]|<[A-Za-z][\w:-]*)")

# How far a block comment is searched for its closing marker.
_BLOCK_WINDOW = 2000


class SyntaxHighlighter(object):
    """Produce colour spans for one line of one language."""

    def __init__(self, module, theme, low_graphics=False):
        self.module = module
        self.theme = theme or {}
        self.low_graphics = bool(low_graphics)
        self.marker = module.comment_marker(getattr(module, "label", ""))
        self.block_pairs = module.block_comment_pairs()
        self.quotes = tuple(module.string_quotes)

    # ----------------------------------------------------------------- API
    def spans(self, text, category=""):
        """Colour spans for one source line as (start, length, kind) tuples."""
        if self.low_graphics or not text:
            return []
        marker = self.module.comment_marker(category)
        code, out = self._split_block_comment(text, marker)
        for start, end, kind in self._line_spans(code, marker):
            out.append((start, end - start, kind))
        return [span for span in out if span[1] > 0]

    def span_pairs(self, text, category=""):
        """The same spans as (start, length, colour) using the active theme."""
        return [(start, size, self.colour(kind))
                for start, size, kind in self.spans(text, category)]

    def colour(self, kind):
        """The themed colour for a span kind, falling back to the default."""
        return self.theme.get(kind, DEFAULT_COLOURS.get(kind, "#dfe7f5"))

    # ------------------------------------------------------------- internals
    def _split_block_comment(self, text, marker):
        """Separate a block comment from the leading code part.

        Returns ``(code_part, spans)`` where spans already contain the comment
        itself, so the remaining parsers never have to look at it.
        """
        for opener, closer in self.block_pairs:
            index = text.find(opener)
            if index < 0:
                continue
            end = text.find(closer, index + len(opener))
            tail = text[index + len(opener):index + len(opener) + _BLOCK_WINDOW]
            if end >= 0:
                end += len(closer)
            elif closer in tail:
                end = index + len(opener) + tail.index(closer) + len(closer)
            else:
                end = len(text)          # unterminated: rest of the line
            spans = [(index, end - index, "comment")]
            # A trailing line comment after the block still gets its colour.
            rest = text[end:]
            rest_start = end
            if marker and self._bare_marker(rest, marker) >= 0:
                at = rest_start + self._bare_marker(rest, marker)
                spans.append((at, len(text) - at, "comment"))
            elif self.quotes:
                for start, stop in self._delimited(rest_start, text):
                    if start >= end:
                        spans.append((start, stop - start, "string"))
            return text[:index], spans
        return text, []

    def _line_spans(self, code, marker):
        """Commment, string, decorator, keyword, built-in and number spans."""
        spans = []
        index = self._bare_marker(code, marker)
        body = code if index < 0 else code[:index]
        if index >= 0:
            spans.append((index, len(code), "comment"))
        for start, end in self._string_spans(body):
            spans.append((start, end, "string"))
        match = _DECORATOR.match(body)
        if match and match.group(1):
            spans.append((match.start(1), match.end(1), "decorator"))
        keywords = self.module.keyword_set()
        builtins = self.module.builtin_set()
        folded = self.module.case_insensitive
        for match in re.finditer(_IDENT, body):
            word = match.group(0)
            if self._covered(spans, match.start()):
                continue
            key = word.lower() if folded else word
            if key in keywords:
                spans.append((match.start(), match.end(), "keyword"))
            elif key in builtins:
                spans.append((match.start(), match.end(), "builtin"))
        for match in _NUMBER.finditer(body):
            if self._covered(spans, match.start()):
                continue
            spans.append((match.start(), match.end(), "number"))
        return spans

    def _bare_marker(self, text, marker):
        """Index of the first comment marker that is not inside a string."""
        if not marker:
            return -1
        lowered = None
        index = 0
        length = len(text)
        while index < length:
            char = text[index]
            if char in ("'", '"'):
                index = self._skip_string(text, index)
                continue
            for opener, closer in self.quotes:
                if text.startswith(opener, index):
                    index = self._skip_delimited(text, index, opener, closer)
                    break
            else:
                if marker.isalpha():
                    # Word markers such as PowerShell's "rem" must stand alone.
                    following = index + len(marker)
                    if text.lower().startswith(marker, index) and (
                            following >= length
                            or not (text[following].isalnum()
                                    or text[following] == "_")):
                        return index
                else:
                    if text.startswith(marker, index):
                        return index
                index += 1
                continue
            if lowered is None:
                lowered = text.lower()
        return -1

    @staticmethod
    def _skip_string(text, index):
        """Index just past the quoted string starting at ``index``."""
        quote = text[index]
        index += 1
        length = len(text)
        while index < length:
            if text[index] == "\\":
                index += 2
                continue
            if text[index] == quote:
                return index + 1
            index += 1
        return length

    @staticmethod
    def _skip_delimited(text, index, opener, closer):
        """Index just past the delimiter pair starting at ``index``."""
        index += len(opener)
        end = text.find(closer, index)
        return len(text) if end < 0 else end + len(closer)

    def _string_spans(self, text):
        """(start, end) for every quoted or module specific string."""
        spans = []
        index = 0
        length = len(text)
        while index < length:
            if text[index] in ("'", '"'):
                end = self._skip_string(text, index)
                spans.append((index, end))
                index = end
                continue
            for opener, closer in self.quotes:
                if text.startswith(opener, index):
                    end = self._skip_delimited(text, index, opener, closer)
                    spans.append((index, end))
                    index = end
                    break
            else:
                index += 1
        return spans

    def _delimited(self, start, text):
        """Module specific string spans in ``text[start:]`` (absolute indexes)."""
        tail = text[start:]
        return [(start + a, start + b) for a, b in
                self._string_spans_without_quotes(tail)]

    def _string_spans_without_quotes(self, text):
        spans = []
        for opener, closer in self.quotes:
            index = 0
            while True:
                found = text.find(opener, index)
                if found < 0:
                    break
                end = text.find(closer, found + len(opener))
                end = len(text) if end < 0 else end + len(closer)
                spans.append((found, end))
                index = end
        return sorted(spans)

    @staticmethod
    def _covered(spans, position):
        """True when ``position`` already belongs to one of the spans."""
        for start, end, _kind in spans:
            if start <= position < end:
                return True
        return False


# Fallback palette, used when a theme is missing a colour key.
DEFAULT_COLOURS = {
    "keyword": "#569cd6",
    "builtin": "#4ec9b0",
    "string": "#ce9178",
    "comment": "#6a9955",
    "number": "#b5cea8",
    "decorator": "#c586c0",
}
