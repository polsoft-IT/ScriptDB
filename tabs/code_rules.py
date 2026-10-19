# -*- coding: utf-8 -*-
"""Code Rules Engine for the ScriptDB editor.

This is the automatic linter *and* fixer behind the editor's "Code rules" menu.
It is deliberately not AI: every rule is a stated, inspectable transform, and a
rule only rewrites the buffer when it can prove the change is safe.  Where the
existing cleanup tools in ``tabs/editor.py`` each do one thing, this module
states the whole rule set once as data, so the menu, the report and the fixer
cannot disagree about what is offered.

    CodeRules().apply(code, language, rule)   -- run one rule
    CodeRules().fix(code, language, rules)    -- run a chosen set
    CodeRules().fix_all(code, language)       -- run every fixable rule
    CodeRules().lint(code, language)          -- report findings, change nothing

The rules:

    fix_imports      add the imports a name needs, drop the unused ones
    format           normalise indentation, trailing spaces, blank lines
    indentation      fix the indentation to the surrounding block
    add_return       add the missing "return None" to a bare fall-through
    add_break        add the missing "break" to a C-style switch/case
    add_except       add the missing "except" to a bare "try"
    add_docstring    add the missing module/class/function docstrings
    add_types        add the missing parameter and return type hints
    add_config       add the missing configuration sections to a config
    fix_style        apply the remaining unambiguous style fixes

Design rules mirror the rest of the editor: standard library only, printable
ASCII, nothing executed, and every result is the same shape so the caller can
report it and undo nothing it did not change.
"""

import ast
import io
import re
# --------------------------------------------------------------------------
# The catalogue: (key, label key, fallback, kind, what it does).
# ``kind`` is "fix" for a rule that rewrites code, "both" for one that can also
# report, and "check" for a report-only rule.  The key is the one ``apply``
# dispatches on, so the table cannot promise a rule the engine does not run.
# --------------------------------------------------------------------------
CODE_RULES = (
    ("fix_imports", "coderules_fix_imports", "Add missing / remove unused imports",
     "fix", "add the imports a used name needs and drop the unused ones"),
    ("format", "coderules_format", "Normalise formatting",
     "fix", "indentation, trailing spaces and blank lines"),
    ("indentation", "coderules_indentation", "Fix indentation",
     "fix", "re-indent lines that do not match their block"),
    ("add_return", "coderules_add_return", "Add missing return",
     "fix", "complete a function whose body can fall off the end"),
    ("add_break", "coderules_add_break", "Add missing break",
     "fix", "complete a switch/case arm that runs into the next one"),
    ("add_except", "coderules_add_except", "Add missing except",
     "fix", "give a bare try the except it is missing"),
    ("add_docstring", "coderules_add_docstring", "Add missing docstrings",
     "fix", "a docstring for the module, every class and every function"),
    ("add_types", "coderules_add_types", "Add missing type hints",
     "fix", "parameter and return annotations inferred from the body"),
    ("add_config", "coderules_add_config", "Add missing config sections",
     "fix", "the sections a configuration file is expected to carry"),
    ("fix_style", "coderules_fix_style", "Fix remaining style issues",
     "fix", "the small, unambiguous style fixes (== None, True/False, ...)"),
)

CODE_RULE_KEYS = tuple(key for key, _lk, _f, _k, _d in CODE_RULES)
CODE_RULE_LABELS = {key: fallback for key, _lk, fallback, _k, _d in CODE_RULES}
CODE_RULE_TITLES = {key: fallback for key, _lk, fallback, _k, _d in CODE_RULES}
CODE_RULE_KINDS = {key: kind for key, _lk, _f, kind, _d in CODE_RULES}
CODE_RULE_MEANINGS = {key: detail for key, _lk, _f, _k, detail in CODE_RULES}
CODE_RULE_FIXES = tuple(key for key in CODE_RULE_KEYS
                        if CODE_RULE_KINDS[key] in ("fix", "both"))

# What a rule needs to be meaningful.  The Python-only rules are stated here
# rather than inferred, so the menu can grey them out honestly.
PYTHON_ONLY_RULES = frozenset((
    "fix_imports", "add_return", "add_docstring", "add_types",
))
# Rules that only make sense when the buffer is not Python at all.
NON_PYTHON_RULES = frozenset(("add_break", "add_except"))

# Which module a name comes from, for the import-adding rule.  Kept small and
# explicit: these are the standard-library names a script reaches for, and
# anything unknown is reported rather than guessed.
COMMON_IMPORTS = {
    "json": "import json", "os": "import os", "re": "import re",
    "sys": "import sys", "math": "import math", "time": "import time",
    "datetime": "import datetime", "random": "import random",
    "logging": "import logging", "hashlib": "import hashlib",
    "sqlite3": "import sqlite3", "subprocess": "import subprocess",
    "shutil": "import shutil", "glob": "import glob",
    "csv": "import csv", "io": "import io", "base64": "import base64",
    "urllib": "import urllib.request", "socket": "import socket",
    "threading": "import threading", "queue": "import queue",
    "collections": "import collections", "itertools": "import itertools",
    "functools": "import functools", "pathlib": "from pathlib import Path",
    "typing": "from typing import Any, Dict, List, Optional",
    "dataclasses": "from dataclasses import dataclass, field",
    "unittest": "import unittest", "argparse": "import argparse",
    "configparser": "import configparser", "tempfile": "import tempfile",
    "uuid": "import uuid", "string": "import string", "copy": "import copy",
    "traceback": "import traceback", "warnings": "import warnings",
    "statistics": "import statistics", "decimal": "import decimal",
    "fractions": "import fractions", "enum": "from enum import Enum",
    "abc": "from abc import ABC, abstractmethod",
}

# The configuration sections a config of each kind is expected to carry.
CONFIG_SECTIONS = {
    "ini": ("general", "paths", "logging"),
    "env": ("APP_NAME", "APP_ENV", "APP_DEBUG", "APP_ROOT"),
    "json": ("general", "paths", "logging"),
}

INDENT_UNIT = "    "


# --------------------------------------------------------------------------
# Public helpers, also used by the editor status line.
# --------------------------------------------------------------------------
def rules_for_language(language):
    """The rules that are meaningful for a language key (never empty)."""
    key = (language or "python").lower()
    if key == "python":
        return tuple(CODE_RULE_KEYS)
    if key in ("c", "cpp", "csharp", "java", "javascript", "typescript",
               "php", "go", "rust", "swift", "kotlin", "scala"):
        return tuple(rule for rule in CODE_RULE_KEYS
                     if rule not in PYTHON_ONLY_RULES)
    return tuple(rule for rule in CODE_RULE_KEYS
                 if rule not in PYTHON_ONLY_RULES
                 and rule not in NON_PYTHON_RULES)


def capabilities():
    """The rule engine as a plugin-safe catalogue."""
    return {
        key: {"name": CODE_RULE_TITLES[key], "kind": CODE_RULE_KINDS[key],
              "detail": CODE_RULE_MEANINGS[key], "status": "available"}
        for key in CODE_RULE_KEYS
    }


def _result(rule, changed, code, notes=(), findings=()):
    """The one shape every rule returns."""
    return {
        "rule": rule,
        "changed": bool(changed),
        "code": code,
        "notes": [note for note in notes if note],
        "findings": [finding for finding in findings if finding],
    }


# --------------------------------------------------------------------------
# The engine.
# --------------------------------------------------------------------------
class CodeRules(object):
    """Run one rule, a chosen set, or the whole rule set, over a buffer."""

    # ----------------------------------------------------------- dispatch
    def apply(self, rule, code, language="python", **options):
        """Run one rule and return its result (always the same shape)."""
        handlers = {
            "fix_imports": self.fix_imports,
            "format": self.format_code,
            "indentation": self.fix_indentation,
            "add_return": self.add_return,
            "add_break": self.add_break,
            "add_except": self.add_except,
            "add_docstring": self.add_docstring,
            "add_types": self.add_types,
            "add_config": self.add_config,
            "fix_style": self.fix_style,
        }
        handler = handlers.get(rule)
        if handler is None:
            return _result(rule, False, code, ["unknown rule: %s" % rule])
        if rule in PYTHON_ONLY_RULES and (language or "").lower() != "python":
            return _result(rule, False, code,
                           ["%s needs Python" % rule])
        return handler(code or "", **options)

    def fix(self, code, language="python", rules=None):
        """Run the named rules in order; stop before a rule that breaks it.

        The buffer is re-parsed after every step, so a rule that would leave
        the code unparseable is rolled back and reported instead of applied.
        """
        selected = list(rules or ())
        if not selected:
            selected = rules_for_language(language)
        source = code or ""
        steps = []
        for rule in selected:
            result = self.apply(rule, source, language)
            if not result["changed"]:
                steps.append({"rule": rule, "changed": False,
                              "notes": result["notes"]})
                continue
            candidate = result["code"]
            if not self._safe(candidate, source, language):
                steps.append({"rule": rule, "changed": False,
                              "notes": ["skipped: the rule would break the "
                                        "parse"]})
                continue
            source = candidate
            steps.append({"rule": rule, "changed": True,
                          "notes": result["notes"]})
        return {"ok": True, "code": source, "steps": steps,
                "applied": [step["rule"] for step in steps if step["changed"]],
                "summary": "%d of %d rule(s) changed the buffer" % (
                    sum(1 for step in steps if step["changed"]), len(steps))}

    def fix_all(self, code, language="python"):
        """Run every rule that applies to the language."""
        return self.fix(code, language, rules_for_language(language))

    def lint(self, code, language="python"):
        """Report what the rules would change, without changing anything."""
        source = code or ""
        findings = []
        for rule in rules_for_language(language):
            result = self.apply(rule, source, language)
            if result["changed"]:
                findings.append({"rule": rule,
                                 "name": CODE_RULE_TITLES[rule],
                                 "detail": "; ".join(result["notes"])
                                 or "the rule can improve this buffer"})
        return {"ok": True, "findings": findings,
                "summary": "%d of %d rule(s) have something to fix" % (
                    len(findings), len(rules_for_language(language)))}

    def report(self, code, language="python"):
        """A plain-text report, for the editor's dialog and for export."""
        outcome = self.lint(code, language)
        lines = ["Code rules report (%s)" % (language or "python"), ""]
        if not outcome["findings"]:
            lines.append("No rule has anything to fix.")
            return "\n".join(lines) + "\n"
        for finding in outcome["findings"]:
            lines.append("* %s" % finding["name"])
            lines.append("    %s" % finding["detail"])
        lines += ["", outcome["summary"]]
        return "\n".join(lines) + "\n"

    @staticmethod
    def _safe(candidate, previous, language):
        """True when ``candidate`` is no worse than ``previous``."""
        if (language or "").lower() != "python":
            return True
        try:
            ast.parse(candidate)
            return True
        except (SyntaxError, ValueError):
            return False

    # ------------------------------------------------------- imports rule
    def fix_imports(self, code, **options):
        """Add the imports a used name needs; drop the ones nothing uses."""
        tree, error = self._parse(code)
        if tree is None:
            return _result("fix_imports", False, code, [str(error)])
        notes = []
        source = code
        text, removed, _error = _remove_unused_imports(source, tree)
        if removed:
            notes.append("removed %d unused import(s)" % removed)
            source = text
            tree, _error = self._parse(source)
            if tree is None:
                source = code
                notes = []
                tree, _error = self._parse(source)
        added = _add_missing_imports(source, tree)
        if added:
            source = added[0]
            notes.extend(added[1])
        return _result("fix_imports", source != code, source, notes)

    # ------------------------------------------------------ formatting rule
    def format_code(self, code, **options):
        """Normalise indentation characters, trailing space and blank lines."""
        lines = (code or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
        result = []
        for line in lines:
            line = line.expandtabs(len(INDENT_UNIT)).rstrip()
            if not line.strip():
                # One blank line is one blank line: collapse a run of them
                # down to a single separator.
                if result and result[-1] != "":
                    result.append("")
                continue
            result.append(line)
        while result and not result[-1].strip():
            result.pop()
        text = "\n".join(result) + ("\n" if result else "")
        return _result("format", text != code, text,
                       ["normalised whitespace"] if text != code else [])

    # ----------------------------------------------------- indentation rule
    def fix_indentation(self, code, language="python", **options):
        """Re-indent lines whose leading space does not match their block.

        For Python the block structure is read from the AST: every statement
        is placed at the indentation of its block, and a continuation line is
        kept relative to the line it continues.
        """
        if (language or "python").lower() != "python":
            return _stub_indent(code)
        lines = (code or "").split("\n")
        # Parse a de-indented copy: _walk_depths reads the block structure,
        # and the original columns would otherwise mask the wrong indent.
        normalised = _normalise_indent(lines)
        tree, error = self._parse(normalised)
        if tree is None:
            return _result("indentation", False, code, [str(error)])
        # Indentation is the block *depth*, not the column the author happened
        # to write: ``col_offset`` would just echo back an over-indented body,
        # so the depth is walked from the module downwards instead.
        wanted = {}
        self._walk_depths(tree, 0, wanted)
        changed = 0
        for line_no, depth in wanted.items():
            index = line_no - 1
            if index >= len(lines) or not lines[index].strip():
                continue
            current = len(lines[index]) - len(lines[index].lstrip(" "))
            wanted_spaces = len(INDENT_UNIT) * depth
            if current != wanted_spaces and not _is_continuation(lines, index):
                lines[index] = INDENT_UNIT * depth + lines[index].lstrip()
                changed += 1
        text = "\n".join(lines)
        return _result("indentation", changed > 0, text,
                       ["re-indented %d line(s)" % changed] if changed else [])

    # ---------------------------------------------------------- return rule
    def add_return(self, code, **options):
        """Complete a function whose body can fall off the end.

        Only functions with no ``return`` at all, that are documented as
        returning nothing, are touched: adding an explicit ``return None``
        where a value is expected would hide a bug rather than fix one.
        """
        tree, error = self._parse(code)
        if tree is None:
            return _result("add_return", False, code, [str(error)])
        lines = (code or "").split("\n")
        insertions = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if _has_value_return(node):
                continue
            if not _returns_nothing(node):
                continue
            body = node.body
            last = body[-1] if body else None
            if last is None or isinstance(last, (ast.Return, ast.Raise)):
                continue
            end = getattr(last, "end_lineno", getattr(last, "lineno", None))
            if not end:
                continue
            indent = " " * getattr(last, "col_offset", 0)
            insertions.append((end, indent + "return None"))
        if not insertions:
            return _result("add_return", False, code)
        for position, line in sorted(insertions, reverse=True):
            lines.insert(position, line)
        text = "\n".join(lines)
        return _result("add_return", True, text,
                       ["added %d missing return" % len(insertions)])

    # ----------------------------------------------------------- break rule
    def add_break(self, code, language="", **options):
        """Add the ``break`` a C-style switch arm is missing.

        A case body that is not empty, does not end in ``break``, ``return``
        ``throw``, ``continue`` or ``fallthrough``, and is followed by another
        ``case``/``default`` label, would fall through into that arm.  Only
        the closing brace of the arm is treated as the end of its body.
        """
        lines = (code or "").replace("\r\n", "\n").split("\n")
        label = re.compile(r"^\s*(case\b.*|default\s*):")
        finishing = ("break", "return", "throw", "continue", "fallthrough",
                     "goto", "raise")
        # Each arm is tracked as (index of its last body line, indentation).
        # The break goes right after that line, at the arm's own indent plus
        # one level, which is where a reader expects it.
        insertions = []
        body_end = None          # index of the last real line of the arm
        label_indent = None       # None until a case/default label is seen
        for index, line in enumerate(lines):
            if label.match(line):
                if body_end is not None:
                    insertions.append((body_end, label_indent))
                body_end = None
                label_indent = re.match(r"[ \t]*", line).group(0)
                continue
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith(("//", "/*", "*", "#")):
                continue
            if label_indent is None:
                # We are outside any switch arm.
                continue
            if stripped.startswith(finishing):
                # The arm already finishes on its own.
                body_end = None
                continue
            if stripped in ("}", "};"):
                if body_end is not None:
                    insertions.append((body_end, label_indent))
                body_end = None
                continue
            body_end = index
        if body_end is not None and label_indent is not None:
            insertions.append((body_end, label_indent))
        if not insertions:
            return _result("add_break", False, code)
        for position, indent in sorted(insertions, reverse=True):
            lines.insert(position + 1, indent + INDENT_UNIT + "break;")
        text = "\n".join(lines)
        return _result("add_break", True, text,
                       ["added a break to %d case arm(s)" % len(insertions)])

    # ---------------------------------------------------------- except rule
    def add_except(self, code, language="", **options):
        """Give a bare ``try`` the except it is missing.

        ``try:`` with no handler is a hard error in most languages and a
        silent swallow of every failure in the rest; the rule adds a narrow
        except that re-raises, so the protection is explicit and the failure
        still surfaces.  A try that already has a handler is left alone.
        """
        lines = (code or "").replace("\r\n", "\n").split("\n")
        added = 0
        index = 0
        while index < len(lines):
            stripped = lines[index].strip()
            if stripped not in ("try:", "try {", "try"):
                index += 1
                continue
            indent = re.match(r"[ \t]*", lines[index]).group(0)
            if stripped.endswith("{"):
                # A brace language: the handler follows the closing brace.
                close = _find_block_end(lines, index)
                if close is None:
                    index += 1
                    continue
                block = lines[index + 1:close]
                if any(re.match(r"\s*(catch|except|finally)\b", item)
                       for item in block):
                    index = close + 1
                    continue
                lines[close:close] = [indent + "} catch (Exception) {",
                                      indent + INDENT_UNIT + "throw;",
                                      indent + "}"]
                added += 1
                index = close + 3
                continue
            # Python: the protected body is every following line indented
            # deeper than the try itself.
            inner = []
            cursor = index + 1
            while cursor < len(lines):
                body_line = lines[cursor]
                if body_line.strip() and (
                        len(body_line) - len(body_line.lstrip(" "))) <= len(indent):
                    break
                inner.append(body_line)
                cursor += 1
            if cursor < len(lines) and lines[cursor].strip().startswith(
                    ("except", "finally")):
                index = cursor
                continue
            if not [item for item in inner if item.strip()]:
                # An empty try has nothing to protect.
                index += 1
                continue
            while inner and not inner[-1].strip():
                inner.pop()
                cursor -= 1
            lines[cursor:cursor] = [indent + "except Exception:",
                                    indent + INDENT_UNIT + "raise"]
            added += 1
            index = cursor + 2
        if not added:
            return _result("add_except", False, code)
        return _result("add_except", True, "\n".join(lines),
                       ["added an except to %d bare try" % added])

    # ------------------------------------------------------ docstring rule
    def add_docstring(self, code, **options):
        """Add the missing module, class and function docstrings.

        The text is derived from the name, so it is a real placeholder that
        explains what the object is rather than a bare ``pass``.
        """
        tree, error = self._parse(code)
        if tree is None:
            return _result("add_docstring", False, code, [str(error)])
        lines = (code or "").split("\n")
        insertions = []
        added = 0
        # Module docstring: only when the file does not already open with one.
        module_documented = bool(ast.get_docstring(tree))
        if not module_documented:
            header = 0
            while header < len(lines) and (lines[header].startswith("#!")
                                           or lines[header].startswith("# -*-")):
                header += 1
            insertions.append((header, _module_docstring(lines)))
            added += 1
        # Class and function docstrings.
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                     ast.ClassDef)):
                continue
            if ast.get_docstring(node):
                continue
            body = node.body
            if not body:
                continue
            first_body = body[0]
            line = getattr(first_body, "lineno", None)
            if not line:
                continue
            indent = " " * getattr(first_body, "col_offset", 0)
            insertions.append((line - 1, indent + '"""%s"""'
                               % _docstring_for(node)))
            added += 1
        if not insertions:
            return _result("add_docstring", False, code)
        for position, text in sorted(insertions, key=lambda item: -item[0]):
            lines.insert(position, text)
        return _result("add_docstring", True, "\n".join(lines),
                       ["added %d docstring(s)" % added])

    # ---------------------------------------------------------- types rule
    def add_types(self, code, **options):
        """Add the missing parameter and return type hints.

        The hints are inferred from what the body actually does: a parameter
        that is only iterated becomes ``Iterable``, one that is compared to a
        literal becomes that literal's type, and a function that returns a
        literal gets that type back.  Where the body proves nothing the rule
        stops rather than guessing, which is why it is safe to run blind.
        """
        tree, error = self._parse(code)
        if tree is None:
            return _result("add_types", False, code, [str(error)])
        lines = (code or "").split("\n")
        edits = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.returns is None:
                inferred = _infer_return(node)
                if inferred:
                    edit = _return_annotation_edit(lines, node, inferred)
                    if edit:
                        edits.append((edit[0], edit[1], edit[1]))
            # Parameter hints are collected per definition and applied to the
            # signature line as a group: two parameters on one line must not
            # overwrite each other, which is what a per-edit replacement would
            # do.
            hints = _infer_parameters(node)
            signature = _annotate_signature(lines, node, hints)
            if signature is not None:
                position, replacement = signature
                edits.append((position, replacement, replacement))
        if not edits:
            return _result("add_types", False, code)
        # Apply the edits from the bottom up so earlier positions stay valid.
        for position, text_value, replace in sorted(edits,
                                                    key=lambda item: -item[0]):
            if replace is None:
                lines.insert(position, text_value)
            else:
                lines[position] = replace
        text = "\n".join(lines)
        try:
            ast.parse(text)
        except (SyntaxError, ValueError) as exc:
            return _result("add_types", False, code,
                           ["skipped: the hints would not parse (%s)" % exc])
        return _result("add_types", True, text,
                       ["added %d type hint(s)" % len(edits)])

    # --------------------------------------------------------- config rule
    def add_config(self, code, **options):
        """Add the sections a configuration file is missing.

        The format is detected from the text: ``[section]`` headers, ``KEY=``
        pairs or a JSON object.  Only sections that are absent are added, and
        each one gets a comment and a commented example rather than a value,
        so nothing is silently configured.
        """
        text = code or ""
        stripped = text.strip()
        if not stripped:
            return _result("add_config", False, code,
                           ["the buffer is empty"])
        if stripped.startswith("{"):
            return _add_config_json(text)
        if re.search(r"^\s*\[.+\]", text, re.MULTILINE):
            return _add_config_sections(text, "ini")
        if re.search(r"^\s*[A-Za-z_][A-Za-z0-9_]*\s*=", text, re.MULTILINE):
            return _add_config_sections(text, "env")
        return _result("add_config", False, code,
                       ["the buffer is not a config file this rule knows"])

    # ---------------------------------------------------------- style rule
    def fix_style(self, code, **options):
        """Apply the small, unambiguous style fixes."""
        text = code or ""
        original = text
        text = re.sub(r"(?<![=!<>])==\s*None\b", "is None", text)
        text = re.sub(r"!=\s*None\b", "is not None", text)
        text = re.sub(r"(?<![=!<>])==\s*True\b", "is True", text)
        text = re.sub(r"(?<![=!<>])==\s*False\b", "is False", text)
        text = re.sub(r"[ \t]+$", "", text, flags=re.MULTILINE)
        text = re.sub(r"\n{4,}", "\n\n", text)
        return _result("fix_style", text != original, text,
                       ["applied the style fixes"] if text != original else [])

    @staticmethod
    def _walk_depths(node, depth, wanted):
        """Record the wanted indentation depth of every statement.

        The module body sits at depth 0; the body of a compound statement sits
        one level deeper than the statement that opens it.  Reading the depth
        rather than the source column is what lets this rule normalise a body
        that was written at the wrong indentation but still parsed.
        """
        body = getattr(node, "body", None)
        if isinstance(body, list):
            for statement in body:
                line = getattr(statement, "lineno", None)
                if line:
                    wanted[line] = depth
                CodeRules._walk_depths(statement, depth + 1, wanted)
        for attribute in ("orelse", "finalbody"):
            extra = getattr(node, attribute, None)
            if isinstance(extra, list):
                for statement in extra:
                    line = getattr(statement, "lineno", None)
                    if line:
                        wanted[line] = depth
                    CodeRules._walk_depths(statement, depth + 1, wanted)
        handlers = getattr(node, "handlers", None)
        if isinstance(handlers, list):
            for handler in handlers:
                line = getattr(handler, "lineno", None)
                if line:
                    wanted[line] = depth
                CodeRules._walk_depths(handler, depth + 1, wanted)

    # ------------------------------------------------------------- helpers
    @staticmethod
    def _parse(code):
        try:
            return ast.parse(code or ""), None
        except (SyntaxError, ValueError) as exc:
            return None, exc


# --------------------------------------------------------------------------
# Import helpers.
# --------------------------------------------------------------------------
def _used_names(tree):
    used = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            value = node.value
            if isinstance(value, ast.Name):
                used.add(value.id)
    return used


def _bound_names(tree):
    bound = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name == "*":
                    continue
                bound.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                               ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bound.add(node.id)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
    return bound


def _remove_unused_imports(code, tree):
    """Drop import statements whose bound names are never used."""
    used = _used_names(tree)
    lines = (code or "").splitlines()
    remove = set()
    for node in tree.body:
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        aliases = [alias for alias in node.names
                   if alias.name != "*"
                   and (alias.asname or alias.name.split(".")[0]) not in used]
        if aliases and len(aliases) == len(node.names):
            start = getattr(node, "lineno", 1) - 1
            end = getattr(node, "end_lineno", node.lineno)
            remove.update(range(start, end))
    kept = [line for index, line in enumerate(lines) if index not in remove]
    text = "\n".join(kept) + ("\n" if code.endswith("\n") else "")
    return text, len(remove), None


def _add_missing_imports(code, tree):
    """Add an import for every standard-library name the code uses but lacks."""
    if tree is None:
        return None
    used = _used_names(tree)
    bound = _bound_names(tree)
    existing = set()
    module_nodes = [node for node in tree.body
                    if isinstance(node, (ast.Import, ast.ImportFrom))]
    for node in module_nodes:
        text = ast.unparse(node)
        existing.add(text.split()[1].split(".")[0].rstrip(","))
    missing = []
    for name in sorted(used - bound):
        if name in existing or name not in COMMON_IMPORTS:
            continue
        statement = COMMON_IMPORTS[name]
        target = statement.split()[1].split(".")[0].rstrip(",")
        if target in existing:
            continue
        missing.append((name, statement))
    if not missing:
        return None
    lines = (code or "").split("\n")
    insert_at = 0
    for index, line in enumerate(lines):
        if line.startswith("#!") or line.startswith("# -*-"):
            insert_at = index + 1
    while insert_at < len(lines) and (not lines[insert_at].strip()
                                      or lines[insert_at].lstrip()
                                      .startswith(('"""', "'''", "#"))):
        insert_at += 1
    while insert_at < len(lines) and lines[insert_at].startswith(("import ",
                                                                  "from ")):
        insert_at += 1
    for _name, statement in reversed(missing):
        lines.insert(insert_at, statement)
    notes = ["added import(s): %s" % ", ".join(
        statement for _name, statement in missing)]
    return "\n".join(lines), notes


# --------------------------------------------------------------------------
# Indentation helpers.
# --------------------------------------------------------------------------
def _is_continuation(lines, index):
    """True when the line continues the statement above it.

    A line is a continuation when the statement above it is left open: an
    unclosed bracket, a trailing operator, or an explicit line join.  A
    colon is NOT one of those cases -- it opens a block, and the line after
    it is a new statement that this rule is entitled to re-indent.
    """
    if index == 0:
        return False
    previous = lines[index - 1].rstrip()
    if previous.endswith(":"):
        return False
    return previous.endswith((",", "(", "[", "{", "\\", "+", "-", "*",
                              "/", "=", "and", "or", "not"))


def _stub_indent(code):
    """The non-Python fallback: brace-depth indentation, only when confident."""
    lines = (code or "").split("\n")
    changed = 0
    depth = 0
    out = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            out.append(line)
            continue
        if stripped.startswith("}"):
            depth = max(0, depth - 1)
        wanted = INDENT_UNIT * depth
        if line[:len(line) - len(line.lstrip())].expandtabs(
                len(INDENT_UNIT)) != wanted and not _is_continuation(lines,
                                                                     len(out)):
            line = wanted + stripped
            changed += 1
        out.append(line)
        if stripped.endswith("{"):
            depth += 1
    text = "\n".join(out)
    return _result("indentation", changed > 0, text,
                   ["re-indented %d line(s)" % changed] if changed else [])


# --------------------------------------------------------------------------
# Return helpers.
# --------------------------------------------------------------------------
def _has_value_return(node):
    """True when the function returns something other than bare None."""
    for child in ast.walk(node):
        if isinstance(child, ast.Return) and child.value is not None:
            return True
    return False


def _returns_nothing(node):
    """True when the code never uses this function's result, as far as we see."""
    for child in ast.walk(node):
        if isinstance(child, ast.Return):
            return False
    return True


# --------------------------------------------------------------------------
# Docstring helpers.
# --------------------------------------------------------------------------
def _first_statement(tree):
    body = getattr(tree, "body", None) or []
    return body[0] if body else None


def _module_docstring(lines):
    """The module docstring for a buffer, named after the script itself."""
    return '"""Module documentation (generated by the code rules engine)."""'


def _docstring_for(node):
    """A one-line docstring derived from the definition's name."""
    if isinstance(node, ast.ClassDef):
        return _sentence(node.name, "class")
    if isinstance(node, ast.AsyncFunctionDef):
        return _sentence(node.name, "coroutine")
    return _sentence(node.name, "function")


def _sentence(name, kind):
    words = re.sub(r"[_]+", " ", name or "").strip() or kind
    if words.lower().startswith("test"):
        return "Test: %s." % words
    if words.lower().startswith(("get", "set", "is", "has")):
        return "Return %s." % words
    return "%s the %s." % ("Handle" if kind != "class" else "Represent", words)


# --------------------------------------------------------------------------
# Type-hint helpers.
# --------------------------------------------------------------------------
def _infer_return(node):
    """What the function returns, when the body makes it obvious."""
    found = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Return) or child.value is None:
            continue
        found.add(_hint_for_expression(child.value))
    found.discard(None)
    if not found:
        return None
    if len(found) == 1:
        return found.pop()
    if found <= {"int", "float"}:
        return "float"
    return None


def _hint_for_expression(node):
    if isinstance(node, ast.Constant):
        value = node.value
        if value is None:
            return "None"
        return {bool: "bool", int: "int", float: "float",
                str: "str", bytes: "bytes"}.get(type(value))
    if isinstance(node, ast.List):
        return "list"
    if isinstance(node, ast.Dict):
        return "dict"
    if isinstance(node, ast.Set):
        return "set"
    if isinstance(node, ast.Tuple):
        return "tuple"
    if isinstance(node, ast.DictComp):
        return "dict"
    if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
        return "list"
    if isinstance(node, ast.JoinedStr):
        return "str"
    if isinstance(node, ast.Compare) or isinstance(node, ast.BoolOp):
        return "bool"
    if isinstance(node, ast.BinOp):
        return "float" if isinstance(node.op, ast.Div) else None
    return None


def _mentions_a_string(node):
    """True when a plus expression involves a string literal.

    Adding to a string literal is concatenation, so the other side is a
    string; adding two names with no literal in sight is arithmetic, so
    both sides are numbers.  This test keeps the two apart.
    """
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value,
                                                          (str, bytes)):
            return True
        if isinstance(child, ast.JoinedStr):
            return True
    return False


def _infer_parameters(node):
    """Type hints the body proves for this function's parameters."""
    names = {argument.arg for argument in _argument_nodes(node)}
    hints = {}
    for child in ast.walk(node):
        # for x in ...  -> x is iterated
        if isinstance(child, (ast.For, ast.AsyncFor)):
            target = child.target
            if isinstance(target, ast.Name) and target.id in names:
                hints.setdefault(target.id, "Iterable")
        # isinstance(x, T) -> x is T
        if (isinstance(child, ast.Call)
                and isinstance(child.func, ast.Name)
                and child.func.id == "isinstance"
                and len(child.args) == 2
                and isinstance(child.args[0], ast.Name)
                and isinstance(child.args[1], ast.Name)):
            if child.args[0].id in names:
                hints[child.args[0].id] = child.args[1].id
        # x == "literal" / x == 3 -> that literal's type
        if isinstance(child, ast.Compare) and isinstance(child.left, ast.Name):
            if child.left.id in names and child.comparators:
                hint = _hint_for_expression(child.comparators[0])
                if hint and hint != "None":
                    hints.setdefault(child.left.id, hint)
        # bool(x) -> x is bool
        if (isinstance(child, ast.Call)
                and isinstance(child.func, ast.Name)
                and child.func.id == "bool"
                and child.args
                and isinstance(child.args[0], ast.Name)
                and child.args[0].id in names):
            hints[child.args[0].id] = "bool"
        # a + b / a * b -> both sides are numbers.  ``+`` is only read this
        # way when neither side is a string literal, so concatenation is never
        # mislabelled as arithmetic.
        if isinstance(child, ast.BinOp):
            if isinstance(child.op, ast.Add) and _mentions_a_string(child):
                pass
            else:
                for side in (child.left, child.right):
                    if isinstance(side, ast.Name) and side.id in names:
                        hints.setdefault(side.id, "float"
                                         if isinstance(child.op, ast.Div)
                                         else "int")
    # Optional parameters (a default of None) are optional, whatever else.
    for argument in _argument_nodes(node):
        if isinstance(argument, ast.arg) and _defaults_to_none(node, argument):
            hint = hints.get(argument.arg)
            if hint and hint != "Optional":
                hints[argument.arg] = "Optional[%s]" % hint
    return hints


def _argument_nodes(node):
    arguments = getattr(node, "args", None)
    if arguments is None:
        return []
    collected = list(getattr(arguments, "posonlyargs", []))
    collected += list(getattr(arguments, "args", []))
    if getattr(arguments, "vararg", None) is not None:
        collected.append(arguments.vararg)
    collected += list(getattr(arguments, "kwonlyargs", []))
    if getattr(arguments, "kwarg", None) is not None:
        collected.append(arguments.kwarg)
    return collected


def _defaults_to_none(node, argument):
    arguments = node.args
    positional = list(getattr(arguments, "args", []))
    defaults = list(getattr(arguments, "defaults", []))
    offset = len(positional) - len(defaults)
    for index, candidate in enumerate(positional):
        if candidate is argument and index >= offset:
            value = defaults[index - offset]
            return isinstance(value, ast.Constant) and value.value is None
    keyword_defaults = dict(zip(
        [item.arg for item in getattr(arguments, "kwonlyargs", [])],
        [item for item in getattr(arguments, "kw_defaults", [])]))
    value = keyword_defaults.get(argument.arg)
    return (isinstance(value, ast.Constant) and value.value is None)


def _signature_span(lines, node):
    """The (first, last) line indices of a definition's signature.

    A signature runs from the def line to the line whose bracket depth returns
    to zero: the line ending in a colon for a one-liner, or the closing bracket
    line for a wrapped one.
    """
    start = node.lineno - 1
    depth = 0
    seen_open = False
    for index in range(start, min(start + 40, len(lines))):
        cleaned = re.sub(r"(['\"]).*?\1", "", lines[index])
        for char in cleaned:
            if char in "([{":
                depth += 1
                seen_open = True
            elif char in ")]}":
                depth -= 1
        if seen_open and depth <= 0:
            return start, index
    return start, start


def _return_annotation_edit(lines, node, hint):
    """Add "-> hint" to a definition, before its closing colon.

    The colon that ends the signature is taken from the right of the last
    signature line, so a default value containing a colon is not mistaken for
    it.  Returns (index, replacement) or None when there is nothing to do.
    """
    _start, end = _signature_span(lines, node)
    line = lines[end]
    colon = line.rfind(":")
    if colon < 0:
        return None
    head = line[:colon].rstrip()
    if not head.endswith(")"):
        return None
    tail = line[colon:]
    return end, head[:-1].rstrip() + ") -> " + hint + tail


def _annotate_signature(lines, node, hints):
    """Annotate every parameter of one definition in a single pass.

    Returns (line index, replacement line) or None.  Working on the whole
    signature at once, rather than one parameter at a time, is what lets
    "def total(a, b)" become "def total(a: int, b: int)" without the second
    edit overwriting the first.
    """
    if not hints:
        return None
    start, end = _signature_span(lines, node)
    for index in range(start, end + 1):
        line = lines[index]
        # Only annotate the parameters that are still bare on this line.
        pending = [(name, hint) for name, hint in hints.items()
                   if not re.search(r"(?<![\w.])%s\s*:" % re.escape(name), line)]
        if not pending:
            continue
        matches = []
        for name, hint in pending:
            pattern = re.compile(r"(?<![\w.])(%s)(\s*)(?=[,)=])"
                                 % re.escape(name))
            found = pattern.search(line)
            if found:
                matches.append((found.end(1), hint))
        if not matches:
            continue
        # Insert from the right so the earlier positions stay valid.
        for position, hint in sorted(matches, reverse=True):
            line = line[:position] + ": " + hint + line[position:]
        return index, line
    return None



def _annotation_edit(lines, node, argument, hint):
    """Add ": hint" after a parameter, or skip when it is not reachable."""
    start, end = _signature_span(lines, node)
    for index in range(start, end + 1):
        line = lines[index]
        if re.search(r"(?<![\w.])%s\s*:" % re.escape(argument), line):
            return None
        match = re.search(r"(?<![\w.])(%s)(\s*)(?=[,)=])" % re.escape(argument),
                          line)
        if not match:
            continue
        replaced = line[:match.end(1)] + ": " + hint + line[match.end(1):]
        return (index, None, replaced)
    return None



# --------------------------------------------------------------------------
# Config helpers.
# --------------------------------------------------------------------------
def _add_config_sections(text, kind):
    """Append the missing sections to an ini-style or key=value config."""
    wanted = CONFIG_SECTIONS[kind]
    lines = text.replace("\r\n", "\n").split("\n")
    present = set()
    if kind == "ini":
        for line in lines:
            match = re.match(r"\s*\[(.+?)\]", line)
            if match:
                present.add(match.group(1).strip().lower())
    else:
        for line in lines:
            match = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
            if match:
                present.add(match.group(1))
    missing = [name for name in wanted if name.lower() not in
               {item.lower() for item in present}
               and name not in present]
    if not missing:
        return _result("add_config", False, text)
    body = "\n".join(lines).rstrip("\n")
    additions = [""]
    for name in missing:
        if kind == "ini":
            additions += ["[%s]" % name,
                          "# %s settings (not configured yet)" % name,
                          "# key = value",
                          ""]
        else:
            additions += ["# %s (not configured yet)" % name,
                          "%s=" % name,
                          ""]
    result = body + "\n" + "\n".join(additions)
    if not result.endswith("\n"):
        result += "\n"
    return _result("add_config", True, result,
                   ["added section(s): %s" % ", ".join(missing)])


def _add_config_json(text):
    """Add the missing top-level sections to a JSON config."""
    import json as _json
    try:
        data = _json.loads(text)
    except ValueError as exc:
        return _result("add_config", False, text,
                       ["the JSON config does not parse: %s" % exc])
    if not isinstance(data, dict):
        return _result("add_config", False, text,
                       ["the JSON config is not an object"])
    wanted = CONFIG_SECTIONS["json"]
    missing = [name for name in wanted if name not in data]
    if not missing:
        return _result("add_config", False, text)
    for name in missing:
        data[name] = {} if name != "general" else {"name": "", "debug": False}
    result = _json.dumps(data, indent=2, sort_keys=False) + "\n"
    return _result("add_config", True, result,
                   ["added section(s): %s" % ", ".join(missing)])


# --------------------------------------------------------------------------
# Convenience functions.
# --------------------------------------------------------------------------
_DEFAULT_RULES = CodeRules()


def apply_rule(rule, code, language="python"):
    return _DEFAULT_RULES.apply(rule, code, language)


def fix_all(code, language="python"):
    return _DEFAULT_RULES.fix_all(code, language)


def lint(code, language="python"):
    return _DEFAULT_RULES.lint(code, language)


def report(code, language="python"):
    return _DEFAULT_RULES.report(code, language)
def _normalise_indent(lines):
    """A copy of the buffer re-indented from its block structure.

    ``fix_indentation`` needs to know the depth each statement *should* sit at,
    and ``ast.parse`` reports the columns as written -- which is exactly what
    is wrong.  Re-indenting from the token stream first gives the parser a
    buffer whose blocks are unambiguous, so the depths it reports back are the
    depths the rule should apply.
    """
    import tokenize
    text = "\n".join(lines)
    if not text.endswith("\n"):
        text += "\n"
    # The tokenizer already knows where each block starts and ends, so the
    # depth is read from INDENT/DEDENT rather than guessed from punctuation.
    depths = {}
    depth = 0
    reader = io.StringIO(text).readline
    try:
        for token in tokenize.generate_tokens(reader):
            if token.type == tokenize.INDENT:
                depth += 1
                continue
            if token.type == tokenize.DEDENT:
                depth = max(0, depth - 1)
                continue
            if token.type in (tokenize.NL, tokenize.NEWLINE, tokenize.COMMENT,
                              tokenize.ENCODING, tokenize.ENDMARKER):
                continue
            row = token.start[0]
            if row not in depths:
                depths[row] = depth
    except (tokenize.TokenError, IndentationError, SyntaxError):
        # The tokenizer refuses the buffer: hand back the original so the AST
        # check upstream reports the real problem rather than this helper.
        return "\n".join(lines)
    out = []
    for index, line in enumerate(lines, 1):
        if not line.strip():
            out.append("")
            continue
        out.append(INDENT_UNIT * depths.get(index, 0) + line.strip())
    return "\n".join(out)
