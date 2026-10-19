# -*- coding: utf-8 -*-
"""Analyzer tab for ScriptDB.

This module owns the "Analyzer" tab: a per-script code analysis view
that sits between the Scripts list and the Audit tab and completes them.

It is self-contained (it takes the main window in its constructor, builds its
own widgets and exposes a small, stable surface):

    ScriptAnalyzerTab(window)     -- build the tab
    tab.refresh()                 -- re-analyse the scripts
    tab.retranslate(table)        -- apply a language table (see TRANSLATIONS)

What it analyses (all standard library only, ASCII safe, sandbox friendly):

    - Overview      : line/char counts, comments, blanks, code lines, quality
    - Structure     : functions, classes, imports, symbols per script
    - Complexity    : a cyclomatic-style complexity estimate per function
    - Issues        : long lines, TODO/FIXME, bare excepts, magic numbers,
                      tabs, trailing whitespace, unused imports
    - Dependencies  : imports the scripts rely on (internal vs external)
    - Report/export : a per-script analysis report

The analyzer also announces its findings on the shared audit event bus, so the
Audit tab's timeline and automatic audits stay in sync with what is seen here.
"""

import datetime as dt
import os
import re

import app_paths

from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtGui import QFont


def _now():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# Standard library modules, so dependencies can be split into internal/external.
STDLIB_MODULES = frozenset((
    "os", "sys", "re", "json", "math", "time", "datetime", "random", "subprocess",
    "threading", "collections", "itertools", "functools", "pathlib", "shutil",
    "tempfile", "typing", "io", "csv", "sqlite3", "hashlib", "base64", "pickle",
    "logging", "unittest", "argparse", "configparser", "urllib", "http", "socket",
    "ssl", "struct", "inspect", "importlib", "string", "textwrap", "traceback",
    "uuid", "warnings", "weakref", "xml", "zipfile", "tarfile", "glob", "fnmatch",
    "difflib", "contextlib", "datetime", "decimal", "fractions", "statistics",
))

# Severity colour palette shared by the trees.
SEVERITY_COLOURS = {"low": "#9be9a8", "medium": "#f7c66d",
                    "high": "#ff7b72", "info": "#8ab4f8"}

# Analysis operations offered by the Operations menu: (key, label key, fallback).
ANALYZER_OPERATIONS = (
    ("full", "analyzer_op_full", "Full script analysis"),
    ("fragment", "analyzer_op_fragment", "Selected fragment analysis"),
    ("function", "analyzer_op_function", "Function analysis"),
    ("class", "analyzer_op_class", "Class analysis"),
    ("imports", "analyzer_op_imports", "Import analysis"),
    ("dependencies", "analyzer_op_dependencies", "Dependency analysis"),
    ("errors", "analyzer_op_errors", "Error analysis"),
    ("security", "analyzer_op_security", "Security analysis"),
    ("runtime", "analyzer_op_runtime", "Runtime analysis"),
    ("changes", "analyzer_op_changes", "Change analysis"),
    ("report", "analyzer_op_report", "Generate analysis report"),
)

# Export formats offered by the Export menu: (key, label key, fallback).
ANALYZER_EXPORTS = (
    ("txt", "analyzer_exp_txt", "TXT (ASCII)"),
    ("json", "analyzer_exp_json", "JSON (structural)"),
    ("log", "analyzer_exp_log", "LOG"),
    ("analysis", "analyzer_exp_report", "Analysis report"),
    ("security", "analyzer_exp_security", "Security report"),
    ("changes", "analyzer_exp_changes", "Changes report"),
)

# Architectural contracts this tab honours. They are declared as data so the
# Architecture sub-tab, the CLI and any plugin can read them without importing
# anything beyond this module (zero-dependency, ASCII-only, plugin-ready).
ANALYZER_ARCHITECTURE = (
    ("ascii", "analyzer_arch_ascii", "ASCII-only",
     "All output and identifiers stay within the printable ASCII range."),
    ("hot_pluggable", "analyzer_arch_hot", "Hot-pluggable",
     "The tab is built from small builders; a new page is one register call."),
    ("zero_dependency", "analyzer_arch_zero", "Zero-dependency",
     "Standard library and PyQt5 only; no external analysis package."),
    ("cross_platform", "analyzer_arch_cross", "Cross-platform",
     "Windows, macOS and Linux paths and separators are all handled."),
    ("sandbox_safe", "analyzer_arch_sandbox", "Sandbox-safe",
     "Analysis is static and read-only; nothing is executed."),
    ("plugin_ready", "analyzer_arch_plugin", "Plugin-ready",
     "Hooks and a registry let plugins observe and extend the analysis."),
    ("artefact_driven", "analyzer_arch_artefact", "Artefact-driven",
     "Every run produces exportable text/JSON/LOG artefacts."),
    ("api_cli_gui", "analyzer_arch_frontends", "API + CLI + GUI",
     "One analyzer surface reachable from code, terminal and window."),
    ("integration", "analyzer_arch_integration", "Diff + Audit integration",
     "Comparisons can be opened in Diff and findings are announced to Audit."),
)

# Premium features offered by the Premium menu: (key, label key, fallback).
ANALYZER_PREMIUM = (
    ("auto_change", "analyzer_prem_auto",
     "Automatic analysis after every file change"),
    ("realtime", "analyzer_prem_realtime",
     "Real-time analysis"),
    ("predictive", "analyzer_prem_predictive",
     "Predictive analysis (what may break)"),
    ("risk", "analyzer_prem_risk", "Risk analysis"),
    ("policy", "analyzer_prem_policy", "Policy compliance analysis"),
    ("project", "analyzer_prem_project",
     "Whole-project quality analysis"),
)

# Cross-tab integrations the analyzer talks to: (key, label, fallback).
ANALYZER_INTEGRATIONS = (
    ("diff", "analyzer_int_diff", "Diff tab"),
    ("audit", "analyzer_int_audit", "Audit tab"),
    ("runner", "analyzer_int_runner", "Script runner"),
    ("hooks", "analyzer_int_hooks", "Analysis hooks"),
    ("registry", "analyzer_int_registry", "API registry"),
    ("cli", "analyzer_int_cli", "CLI entry point"),
)

# Interval of the premium watcher (milliseconds).
PREMIUM_POLL_MS = 2000
class AnalyzerHooks:
    """Tiny pub/sub bus for plugins (sandbox-safe; errors are swallowed)."""

    def __init__(self):
        self._hooks = {"before_analysis": [], "after_analysis": [],
                       "finding": []}

    def register(self, event, callback):
        self._hooks.setdefault(event, []).append(callback)
        return callback
    def unregister(self, event, callback):
        if callback in self._hooks.get(event, []):
            self._hooks[event].remove(callback)

    def fire(self, event, **payload):
        for callback in list(self._hooks.get(event, [])):
            try:
                callback(**payload)
            except Exception:
                pass
# Process-wide hooks and plugin registry (plugin-ready surface).
HOOKS = AnalyzerHooks()
API_REGISTRY = {}


def register_api(name, func=None):
    """Expose a callable under `name` in the plugin API registry."""
    if func is None:
        def decorator(real):
            API_REGISTRY[name] = real
            return real
        return decorator
    API_REGISTRY[name] = func
    return func
class ScriptAnalyzerTab(QtWidgets.QWidget):
    """Analyse the code quality, structure and dependencies of the scripts.

    Everything is derived from the script database, so the tab works without
    any external service and updates whenever the user presses Analyse (or the
    main window asks it to).
    """

    _installed_package_cache = None

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self._state = {}
        self._build_ui()
        QtCore.QTimer.singleShot(0, self.refresh)

    # --------------------------------------------------------------- helpers
    def t(self, key, fallback=None, **kwargs):
        """Look a label up in the main window's translations."""
        return self.window.t(key, fallback, **kwargs)

    def _scripts(self):
        """All real script entries from the main window (never None)."""
        try:
            return list(self.window.scripts_only())
        except Exception:
            return []

    def _selected_script(self):
        getter = getattr(self.window, "selected_script", None)
        if callable(getter):
            try:
                return getter()
            except Exception:
                return None
        return None

    # --------------------------------------------------------------- analysis
    @staticmethod
    def _strip_strings_and_comments(code):
        """Remove string bodies and comments so heuristics don't count them.

        A light, regex-based pass (not a full parser): it blanks the contents
        of quoted strings and drops comments so line metrics and keyword
        detection only see actual code.
        """
        text = code or ""
        # Triple-quoted strings first (docstrings), then single quotes.
        text = re.sub(r'"""(?:.|\n)*?"""', '""', text)
        text = re.sub(r"'''(?:.|\n)*?'''", "''", text)
        text = re.sub(r'"(?:\\.|[^"\\])*"', '""', text)
        text = re.sub(r"'(?:\\.|[^'\\])*'", "''", text)
        text = re.sub(r"#[^\n]*", "", text)
        return text

    def _line_metrics(self, code):
        """Count total/blank/comment/code lines and character totals."""
        lines = (code or "").splitlines()
        blank = sum(1 for line in lines if not line.strip())
        comment = sum(1 for line in lines if line.lstrip().startswith("#"))
        long_lines = [i + 1 for i, line in enumerate(lines) if len(line) > 100]
        trailing = [i + 1 for i, line in enumerate(lines)
                    if line != line.rstrip()]
        tabs = [i + 1 for i, line in enumerate(lines) if "\t" in line]
        code_lines = len(lines) - blank - comment
        return {
            "total_lines": len(lines),
            "code_lines": max(0, code_lines),
            "blank_lines": blank,
            "comment_lines": comment,
            "chars": len(code or ""),
            "long_lines": long_lines,
            "trailing_ws": trailing,
            "tabs": tabs,
        }

    def _functions(self, code):
        """Top-level and nested function definitions: name + (start, end)."""
        functions = []
        for match in re.finditer(r"^([ \t]*)def\s+([A-Za-z_]\w*)\s*\(([^)]*)\)",
                                 code or "", re.MULTILINE):
            start = match.start()
            end = self._block_end(code, start)
            functions.append({
                "name": match.group(2),
                "params": match.group(3),
                "indent": len(match.group(1)),
                "start": start,
                "end": end,
                "body": (code or "")[start:end],
            })
        return functions

    def _classes(self, code):
        """Class definitions found in the code."""
        names = []
        for match in re.finditer(r"^[ \t]*class\s+([A-Za-z_]\w*)", code or "",
                                 re.MULTILINE):
            names.append(match.group(1))
        return names

    @staticmethod
    def _block_end(code, start):
        """Character offset where an indented Python block ends."""
        text = code or ""
        lines = text[start:].splitlines(keepends=True)
        if not lines:
            return len(text)
        header = lines[0]
        base_indent = len(header) - len(header.lstrip())
        offset = start + len(header)
        for line in lines[1:]:
            if not line.strip():
                offset += len(line)
                continue
            indent = len(line) - len(line.lstrip())
            if indent <= base_indent:
                break
            offset += len(line)
        return offset

    def _imports(self, code):
        """Imported module names, split into internal / external."""
        names = set()
        for line in (code or "").splitlines():
            match = re.match(r"^\s*import\s+([\w., ]+)", line)
            if match:
                for part in match.group(1).split(","):
                    base = part.strip().split(" as ")[0].strip().split(".")[0]
                    if base:
                        names.add(base)
            match = re.match(r"^\s*from\s+([\w.]+)\s+import", line)
            if match:
                names.add(match.group(1).split(".")[0])
        internal = sorted(n for n in names if n in STDLIB_MODULES)
        external = sorted(n for n in names if n not in STDLIB_MODULES)
        return internal, external, sorted(names)

    def _complexity(self, code):
        """A cyclomatic-style complexity estimate per function.

        Complexity starts at 1 and grows with each branching keyword, so a
        higher number means a function with more independent paths (harder to
        test and reason about).
        """
        branch_re = re.compile(
            r"\b(if|elif|for|while|and|or|except|with|assert)\b|"
            r"\s*[^=!<>]=[^=]", re.MULTILINE)
        rows = []
        for function in self._functions(code):
            clean = self._strip_strings_and_comments(function["body"])
            branches = len(branch_re.findall(clean))
            complexity = 1 + branches
            rows.append({
                "name": function["name"],
                "complexity": complexity,
                "lines": len(function["body"].splitlines()),
                "params": len([p for p in function["params"].split(",") if p.strip()]),
            })
        rows.sort(key=lambda r: r["complexity"], reverse=True)
        return rows

    def _issues(self, code, category=""):
        """Static issues worth a reviewer's attention."""
        metrics = self._line_metrics(code)
        clean = self._strip_strings_and_comments(code)
        issues = []
        for line_no in metrics["long_lines"][:20]:
            issues.append({"severity": "low", "line": line_no,
                           "kind": "analyzer_issue_long_line",
                           "detail": "> 100 characters"})
        for line_no in metrics["trailing_ws"][:20]:
            issues.append({"severity": "low", "line": line_no,
                           "kind": "analyzer_issue_trailing_ws", "detail": ""})
        for line_no in metrics["tabs"][:20]:
            issues.append({"severity": "low", "line": line_no,
                           "kind": "analyzer_issue_tabs", "detail": ""})
        if re.search(r"\bexcept\s*:", clean):
            issues.append({"severity": "medium", "line": 0,
                           "kind": "analyzer_issue_bare_except", "detail": ""})
        if re.search(r"\beval\s*\(", clean) or re.search(r"\bexec\s*\(", clean):
            issues.append({"severity": "high", "line": 0,
                           "kind": "analyzer_issue_eval", "detail": ""})
        for match in re.finditer(r"\b(TODO|FIXME|XXX|HACK)\b", code or ""):
            issues.append({"severity": "low", "line": 0,
                           "kind": "analyzer_issue_todo",
                           "detail": match.group(1)})
        if re.search(r"\bprint\s*\(", clean) and (category or "").lower() in (
                "python", "py"):
            issues.append({"severity": "low", "line": 0,
                           "kind": "analyzer_issue_print", "detail": ""})
        unused = self._unused_imports(code)
        for name in unused:
            issues.append({"severity": "low", "line": 0,
                           "kind": "analyzer_issue_unused_import",
                           "detail": name})
        return issues

    @staticmethod
    def _unused_imports(code):
        """Imported names that never appear again in the code body."""
        names = []
        for line in (code or "").splitlines():
            match = re.match(r"^\s*import\s+([\w., ]+)", line)
            if match:
                for part in match.group(1).split(","):
                    alias = part.strip().split(" as ")[-1].strip()
                    base = alias.split(".")[0]
                    if base:
                        names.append(base)
            match = re.match(r"^\s*from\s+[\w.]+\s+import\s+([\w., ]+)", line)
            if match:
                for part in match.group(1).split(","):
                    alias = part.strip().split(" as ")[-1].strip()
                    if alias and alias != "*":
                        names.append(alias)
        body = code or ""
        return [name for name in sorted(set(names))
                if len(re.findall(r"\b" + re.escape(name) + r"\b", body)) <= 1]

    def _quality_score(self, metrics, issues, complexity):
        """A 0-100 quality score: fewer issues and lower complexity is better."""
        score = 100
        score -= len(issues) * 2
        high = sum(1 for i in issues if i["severity"] == "high")
        medium = sum(1 for i in issues if i["severity"] == "medium")
        score -= high * 8 + medium * 4
        worst = max((r["complexity"] for r in complexity), default=1)
        if worst > 10:
            score -= (worst - 10) * 2
        if metrics["total_lines"] and metrics["comment_lines"] == 0:
            score -= 3
        return max(0, min(100, score))

    def _analyze_script(self, script):
        """Full analysis of one script entry."""
        code = script.get("code", "") or ""
        category = script.get("category", "")
        metrics = self._line_metrics(code)
        functions = self._functions(code)
        classes = self._classes(code)
        internal, external, all_imports = self._imports(code)
        complexity = self._complexity(code)
        syntax = self._syntax_analysis(code, category)
        type_findings = self._type_analysis(code, category)
        issues = self._issues(code, category)
        # Fold the syntax/type findings into the issue list so the Issues tab
        # and the quality score account for them too.
        extra_issues = list(syntax["errors"])
        extra_issues += [dict(f, severity="medium", kind=f["kind"])
                         for f in type_findings]
        extra_issues += [dict(i, severity="low") for i in syntax["indent_issues"]]
        issues = issues + extra_issues
        ast_tree = self._ast_analysis(code, category)
        style = self._style_analysis(code, category)
        runtime = self._runtime_analysis(code, category)
        dependencies = self._dependency_analysis(code, category)
        metrics_engine = self._metrics_engine(script)
        history = self._history_analysis(script)
        configuration = self._config_analysis(script)
        return {
            "name": script.get("name", "") or "(unnamed)",
            "category": category,
            "metrics": metrics,
            "functions": [f["name"] for f in functions],
            "classes": classes,
            "internal_imports": internal,
            "external_imports": external,
            "all_imports": all_imports,
            "complexity": complexity,
            "issues": issues,
            "syntax": syntax,
            "style": style,
            "runtime": runtime,
            "runtime_summary": self._runtime_summary(runtime),
            "dependencies": dependencies,
            "dependency_summary": self._dependency_summary(dependencies),
            "metrics_engine": metrics_engine,
            "ast": ast_tree,
            "ast_summary": self._ast_summary(ast_tree),
            "history": history,
            "history_summary": self._history_summary(history),
            "configuration": configuration,
            "configuration_summary": configuration.get("summary", {}),
            "score": self._quality_score(metrics, issues, complexity),
        }

    def analyze_all(self):
        """Analyse every script; returns a list of per-script results."""
        results = [self._analyze_script(script) for script in self._scripts()]
        results.sort(key=lambda r: r["score"])
        return results

    def analyze_selected(self):
        """Analyse the script currently selected on the Scripts tab."""
        script = self._selected_script()
        if script is None:
            return None
        return self._analyze_script(script)

    # ---------------------------------------------------- diff/history engine
    # Version-aware analysis. Each script is compared with its previous
    # revision (from the stored history) and, when available, with an accepted
    # snapshot, producing structured change sets: functions, classes, imports,
    # logic and refactors (rename / move / extract). The Diff tab service can be
    # focused on the same pair on demand.

    def _diff_rows(self, old_code, new_code):
        """Aligned diff rows for two code bodies (reuses the Diff tab engine)."""
        try:
            from sdb_scripts import align_lines
        except Exception:
            align_lines = None
        if align_lines is not None:
            try:
                return align_lines(old_code or "", new_code or "") or []
            except Exception:
                pass
        import difflib
        old_lines = (old_code or "").splitlines()
        new_lines = (new_code or "").splitlines()
        rows = []
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
                None, old_lines, new_lines).get_opcodes():
            if tag == "equal":
                continue
            for line in old_lines[i1:i2]:
                rows.append({"tag": "removed", "left": line, "right": ""})
            for line in new_lines[j1:j2]:
                rows.append({"tag": "added", "left": "", "right": line})
        return rows

    @staticmethod
    def _named_defs(code):
        """Map of {name: source} for top-level functions and classes."""
        import ast as _ast
        found = {}
        try:
            tree = _ast.parse(code or "")
        except (SyntaxError, ValueError):
            return found
        for node in getattr(tree, "body", []):
            if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef,
                                 _ast.ClassDef)):
                try:
                    found[node.name] = _ast.get_source_segment(
                        code or "", node) or ""
                except Exception:
                    found[node.name] = ""
        return found

    @staticmethod
    def _def_kinds(code):
        """Map of {name: 'function'|'class'} for top-level definitions."""
        import ast as _ast
        kinds = {}
        try:
            tree = _ast.parse(code or "")
        except (SyntaxError, ValueError):
            return kinds
        for node in getattr(tree, "body", []):
            if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                kinds[node.name] = "function"
            elif isinstance(node, _ast.ClassDef):
                kinds[node.name] = "class"
        return kinds

    def _history_changes(self, old_code, new_code):
        """Detect changes split by category: functions, classes, imports, logic."""
        changes = {"functions": [], "classes": [], "imports": [], "logic": []}
        old_defs = self._named_defs(old_code)
        new_defs = self._named_defs(new_code)
        old_kinds = self._def_kinds(old_code)
        new_kinds = self._def_kinds(new_code)
        for name in sorted(set(old_defs) | set(new_defs)):
            kind = new_kinds.get(name) or old_kinds.get(name) or "function"
            bucket = "classes" if kind == "class" else "functions"
            if name not in old_defs:
                changes[bucket].append({"name": name, "kind": "added",
                                        "detail": "new %s" % kind})
            elif name not in new_defs:
                changes[bucket].append({"name": name, "kind": "removed",
                                        "detail": "%s removed" % kind})
            elif old_defs[name].strip() != new_defs[name].strip():
                changes[bucket].append({"name": name, "kind": "modified",
                                        "detail": "body changed"})
        old_imports = set(self._imports(old_code)[2])
        new_imports = set(self._imports(new_code)[2])
        for name in sorted(new_imports - old_imports):
            changes["imports"].append({"name": name, "kind": "added",
                                       "detail": "import added"})
        for name in sorted(old_imports - new_imports):
            changes["imports"].append({"name": name, "kind": "removed",
                                       "detail": "import removed"})
        changes["logic"] = self._logic_changes(old_code, new_code)
        return changes

    def _logic_changes(self, old_code, new_code):
        """Detect control-flow / logic level changes between two versions."""
        import ast as _ast

        def flow_counts(code):
            counts = {"branches": 0, "loops": 0, "returns": 0,
                      "exceptions": 0, "calls": 0}
            try:
                tree = _ast.parse(code or "")
            except (SyntaxError, ValueError):
                return counts
            for node in _ast.walk(tree):
                if isinstance(node, _ast.If):
                    counts["branches"] += 1
                elif isinstance(node, (_ast.For, _ast.While)):
                    counts["loops"] += 1
                elif isinstance(node, _ast.Return):
                    counts["returns"] += 1
                elif isinstance(node, _ast.Try):
                    counts["exceptions"] += 1
                elif isinstance(node, _ast.Call):
                    counts["calls"] += 1
            return counts

        before = flow_counts(old_code)
        after = flow_counts(new_code)
        rows = []
        for key in ("branches", "loops", "returns", "exceptions", "calls"):
            if before[key] != after[key]:
                rows.append({"name": key, "kind": "modified",
                             "detail": "%s -> %s" % (before[key], after[key])})
        return rows

    def _refactor_changes(self, old_code, new_code):
        """Detect refactors: rename, move and extract across two versions."""
        import ast as _ast

        def location_map(code):
            info = {}
            try:
                tree = _ast.parse(code or "")
            except (SyntaxError, ValueError):
                return info
            for node in _ast.walk(tree):
                if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef,
                                     _ast.ClassDef)):
                    info.setdefault(node.name, []).append(
                        getattr(node, "lineno", 0))
            return info

        old_loc = location_map(old_code)
        new_loc = location_map(new_code)
        refactors = []
        for name in old_loc:
            if name not in new_loc:
                refactors.append({"kind": "removed", "name": name,
                                  "detail": "symbol removed"})
        for name in new_loc:
            if name not in old_loc:
                refactors.append({"kind": "extract", "name": name,
                                  "detail": "new symbol introduced"})
            elif old_loc.get(name) != new_loc.get(name):
                refactors.append({"kind": "move", "name": name,
                                  "detail": "lines %s -> %s" % (
                                      old_loc.get(name), new_loc.get(name))})
        for kind, name, detail in self._paired_renames(old_code, new_code):
            refactors.append({"kind": kind, "name": name, "detail": detail})
        return refactors

    @staticmethod
    def _paired_renames(old_code, new_code):
        """Heuristic rename detection: same body, different declared name."""
        old_defs = ScriptAnalyzerTab._named_defs(old_code)
        new_defs = ScriptAnalyzerTab._named_defs(new_code)
        old_remaining = dict((k, v.strip()) for k, v in old_defs.items())
        rows = []
        for new_name in list(new_defs):
            if new_name in old_remaining:
                continue
            body = new_defs[new_name].strip()
            for old_name, old_body in list(old_remaining.items()):
                if old_name in new_defs:
                    continue
                if old_body == body:
                    rows.append(("rename", "%s -> %s" % (old_name, new_name),
                                 "same body, renamed"))
                    old_remaining.pop(old_name, None)
                    break
        return rows

    def _history_analysis(self, script):
        """Compare a script with its previous revision and accepted snapshot."""
        current = script.get("code", "") or ""
        revisions = script.get("history", []) or []
        result = {"has_previous": False, "has_snapshot": False,
                  "previous": {}, "snapshot": {}, "refactors": [],
                  "changed": False, "prev_label": "", "snap_label": ""}
        previous = revisions[-1] if revisions else None
        if previous is not None:
            old_code = previous.get("code", "") or ""
            result["has_previous"] = True
            result["prev_label"] = previous.get("date", "") or "previous"
            result["previous"] = self._history_changes(old_code, current)
            result["refactors"] = self._refactor_changes(old_code, current)
            result["changed"] = bool(
                any(result["previous"][k] for k in result["previous"])
                or result["refactors"])
        snapshot = self._accepted_snapshot(script)
        if snapshot is not None:
            old_code = snapshot.get("code", "") or ""
            result["has_snapshot"] = True
            result["snap_label"] = snapshot.get("date", "") or "snapshot"
            result["snapshot"] = self._history_changes(old_code, current)
            result["changed"] = result["changed"] or bool(
                any(result["snapshot"][k] for k in result["snapshot"]))
        return result

    def _accepted_snapshot(self, script):
        """Return the accepted baseline snapshot for a script, if any.

        Snapshots are stored by the Diff tab / audit under the data folder; we
        look up the newest one that matches the script by id or name.
        """
        directory = os.path.join(self._data_dir(), "snapshots")
        if not os.path.isdir(directory):
            return None
        script_id = str(script.get("id", ""))
        name = script.get("name", "")
        best = None
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            return None
        for entry in names:
            if not entry.lower().endswith(".json"):
                continue
            path = os.path.join(directory, entry)
            try:
                import json as _json
                with open(path, "r", encoding="utf-8") as handle:
                    data = _json.load(handle)
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            if (script_id and str(data.get("id", "")) == script_id) or \
                    (name and data.get("name") == name):
                if best is None or (data.get("date", "") > best.get("date", "")):
                    best = data
        return best

    # -------------------------------------------------- configuration engine
    # Configuration-focused analysis: config files tied to the script, the
    # run-time environment, environment variables, PATH and interpreter
    # settings. Everything is read-only and degrades gracefully when a source
    # is unavailable.

    CONFIG_EXCLUDE = ("audit_db.json", "runtime_log.json", "scripts_db.json",
                      "connections_db.json", "settings_db.json",
                      "audit_timeline.json")

    def _config_files(self):
        """Candidate config files (json / ini / yaml / cfg) in the data folder."""
        found = []
        try:
            for name in sorted(os.listdir(self._data_dir())):
                lowered = name.lower()
                if lowered in self.CONFIG_EXCLUDE:
                    continue
                if lowered.endswith((".json", ".ini", ".yaml", ".yml", ".cfg")):
                    found.append(os.path.join(self._data_dir(), name))
        except OSError:
            pass
        return found

    def _config_file_rows(self):
        """Analyse config files: presence, readability and size."""
        rows = []
        for path in self._config_files():
            name = os.path.basename(path)
            try:
                size = os.path.getsize(path)
                rows.append({"name": name, "kind": "config",
                             "detail": "%s bytes" % size})
            except OSError as exc:
                rows.append({"name": name, "kind": "unreadable",
                             "detail": str(exc)})
        return rows

    def _environment_rows(self):
        """Analyse the run-time environment: platform, python, executable."""
        import platform
        import sys
        rows = [
            {"name": "platform", "kind": "runtime",
             "detail": platform.platform()},
            {"name": "python", "kind": "runtime",
             "detail": sys.version.split()[0]},
            {"name": "executable", "kind": "runtime",
             "detail": sys.executable or "(unknown)"},
            {"name": "cwd", "kind": "runtime", "detail": os.getcwd()},
        ]
        return rows

    def _env_var_rows(self, script):
        """Environment variables referenced by the script (os.environ / getenv)."""
        code = script.get("code", "") or ""
        names = set()
        pattern = (r"(?:os\.environ(?:\[|\s*\.get\()\s*[\"']([\w]+)[\"']"
                   r"|(?:os\.)?getenv\(\s*[\"']([\w]+)[\"'])")
        for match in re.finditer(pattern, code):
            name = match.group(1) or match.group(2)
            if name:
                names.add(name)
        rows = []
        for name in sorted(names):
            value = os.environ.get(name)
            rows.append({"name": name, "kind": "env",
                         "detail": "set" if value is not None else "missing"})
        return rows

    def _path_rows(self):
        """Analyse PATH: entries, existing vs missing."""
        raw = os.environ.get("PATH", "")
        entries = [part for part in raw.split(os.pathsep) if part]
        rows = []
        for entry in entries[:200]:
            rows.append({"name": entry, "kind": "path",
                         "detail": "ok" if os.path.isdir(entry) else "missing"})
        return rows

    def _interpreter_rows(self, code):
        """Interpreter settings declared by the script (shebang / requires)."""
        rows = []
        for pattern, kind in self.INTERPRETER_PATTERNS:
            for match in pattern.finditer(code or ""):
                detail = " ".join(g for g in match.groups() if g)
                rows.append({"name": kind, "kind": "interpreter",
                             "detail": detail or match.group(0).strip()})
        return rows

    def _config_analysis(self, script):
        """Full configuration analysis for one script."""
        config_files = self._config_file_rows()
        environment = self._environment_rows()
        env_vars = self._env_var_rows(script)
        path_rows = self._path_rows()
        interpreters = self._interpreter_rows(script.get("code", "") or "")
        summary = {
            "config_files": len(config_files),
            "environment": len(environment),
            "env_vars": len(env_vars),
            "env_missing": sum(1 for r in env_vars if r["detail"] == "missing"),
            "path": len(path_rows),
            "path_missing": sum(1 for r in path_rows if r["detail"] == "missing"),
            "interpreters": len(interpreters),
        }
        return {"config_files": config_files, "environment": environment,
                "env_vars": env_vars, "path": path_rows,
                "interpreters": interpreters, "summary": summary}

    def _history_summary(self, history):
        # Counts used by the diff/history summary cards and the report.
        previous = history.get("previous") or {}
        snapshot = history.get("snapshot") or {}
        categories = ("functions", "classes", "imports", "logic")
        prev_total = sum(len(previous.get(k, [])) for k in categories)
        snap_total = sum(len(snapshot.get(k, [])) for k in categories)
        return {
            "has_previous": bool(history.get("has_previous")),
            "has_snapshot": bool(history.get("has_snapshot")),
            "changed": bool(history.get("changed")),
            "previous_changes": prev_total,
            "snapshot_changes": snap_total,
            "refactors": len(history.get("refactors", [])),
            "total_changes": prev_total + snap_total + len(
                history.get("refactors", [])),
        }

    def _summary(self):
        """Aggregate the per-script results into headline numbers."""
        results = self._state.get("results", [])
        total_scripts = len(results)
        total_issues = sum(len(r["issues"]) for r in results)
        avg_score = (sum(r["score"] for r in results) / total_scripts
                     if total_scripts else 0)
        worst = results[0] if results else None
        return {
            "scripts": total_scripts,
            "issues": total_issues,
            "avg_score": round(avg_score, 1),
            "worst": worst,
        }

    # ------------------------------------------------------- report/export
    def build_report(self, fmt="txt"):
        """Build a per-script analysis report as text or JSON."""
        import json as _json
        results = self._state.get("results") or self.analyze_all()
        summary = self._summary()
        if (fmt or "txt").lower() == "json":
            return _json.dumps({"generated": _now(), "summary": summary,
                                "scripts": results}, ensure_ascii=False,
                               indent=2, default=str)
        lines = ["ANALYZER REPORT", "generated: %s" % _now(),
                 "scripts: %s  issues: %s  avg score: %s" % (
                     summary["scripts"], summary["issues"], summary["avg_score"]),
                 "=" * 60]
        for result in results:
            lines.append("")
            lines.append("%s [%s]  score=%s" % (
                result["name"], result["category"], result["score"]))
            metrics = result["metrics"]
            lines.append("  lines=%s code=%s comment=%s blank=%s funcs=%s classes=%s" % (
                metrics["total_lines"], metrics["code_lines"],
                metrics["comment_lines"], metrics["blank_lines"],
                len(result["functions"]), len(result["classes"])))
            if result["external_imports"]:
                lines.append("  external: " + ", ".join(result["external_imports"]))
            syntax = result.get("syntax") or {}
            lines.append("  syntax: %s" % (
                "valid" if syntax.get("valid", True) else "errors"))
            for error in syntax.get("errors", []):
                lines.append("    ! %s line %s: %s" % (
                    error.get("kind", ""), error.get("line", 0),
                    error.get("detail", "")))
            ast_summary = result.get("ast_summary") or {}
            if ast_summary:
                lines.append("  AST: %s funcs, %s classes, %s blocks, "
                             "%s calls, %s unused vars" % (
                                 ast_summary.get("functions", 0),
                                 ast_summary.get("classes", 0),
                                 ast_summary.get("blocks", 0),
                                 ast_summary.get("calls", 0),
                                 ast_summary.get("unused_vars", 0)))
            style = result.get("style") or {}
            lines.append("  quality: %s standard breach(es), %s duplicate(s), "
                         """%s long func(s), %s complex, %s deep-nested, """
                         "%s dead-code" % (
                             len(style.get("standards", [])),
                             len(style.get("duplicates", [])),
                             len(style.get("long_funcs", [])),
                             len(style.get("complex_blocks", [])),
                             len(style.get("deep_nesting", [])),
                             len(style.get("dead_code", []))))
            rt = result.get("runtime_summary") or {}
            if rt:
                lines.append("  runtime: %s steps, %s exception(s), "
                             "%s dangerous, %s anomaly(ies)" % (
                                 rt.get("steps", 0), rt.get("exceptions", 0),
                                 rt.get("dangerous", 0), rt.get("anomalies", 0)))
            dep = result.get("dependency_summary")
            if dep:
                lines.append("  dependencies: %s import(s), %s missing, "
                             "%s unused, %s conflict(s), %s system" % (
                                 dep.get("imports", 0), dep.get("missing", 0),
                                 dep.get("unused", 0), dep.get("conflicts", 0),
                                 dep.get("system", 0)))
                for row in (result.get("dependencies") or {}).get("missing", []):
                    lines.append("    ! missing: %s" % row["name"])
            m = result.get("metrics_engine")
            if m:
                lines.append("  metrics: %s lines, %s funcs, %s classes, "
                             """%s imports, %s errors, %s warnings, """
                             "CC=%s SC=%s LC=%s" % (
                                 m.get("lines", 0), m.get("functions", 0),
                                 m.get("classes", 0), m.get("imports", 0),
                                 m.get("errors", 0), m.get("warnings", 0),
                                 m.get("cyclomatic", 0), m.get("structural", 0),
                                 m.get("logical", 0)))
            hist = result.get("history_summary") or {}
            if hist.get("has_previous") or hist.get("has_snapshot"):
                lines.append("  history: %s change(s) vs previous, "
                             "%s vs snapshot, %s refactor(s)" % (
                                 hist.get("previous_changes", 0),
                                 hist.get("snapshot_changes", 0),
                                 hist.get("refactors", 0)))
            cfg = result.get("configuration_summary") or {}
            if cfg:
                lines.append("  config: %s file(s), %s env var(s) "
                             """(%s missing), %s PATH entries (%s missing), """
                             "%s interpreter(s)" % (
                                 cfg.get("config_files", 0),
                                 cfg.get("env_vars", 0),
                                 cfg.get("env_missing", 0),
                                 cfg.get("path", 0),
                                 cfg.get("path_missing", 0),
                                 cfg.get("interpreters", 0)))
            for issue in result["issues"]:
                lines.append("  [%s] %s %s" % (
                    issue["severity"], issue["kind"], issue.get("detail", "")))
        return "\n".join(lines)

    def export_report(self, fmt="txt"):
        """Write an analysis report to audit_exports and return the path."""
        content = self.build_report(fmt)
        directory = os.path.join(self._data_dir(), "analyzer_exports")
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError:
            return ""
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        path = os.path.join(directory, "analysis-%s.%s" % (stamp, fmt))
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(content)
        except OSError:
            return ""
        return path

    def _data_dir(self):
        base = getattr(self.window, "db_dir", None) or getattr(
            self.window, "DB_DIR", None)
        if base:
            return base
        return app_paths.ensure_db_dir()

    # ---------------------------------------------------- audit integration
    def _announce(self, detail):
        """Record an analyzer action on the shared audit timeline."""
        audit = getattr(self.window, "audit_tab", None)
        if audit is not None and hasattr(audit, "log_change"):
            try:
                audit.log_change("modules", "analyzer", detail, "analyzer")
            except Exception:
                pass

    def analyze_and_announce(self):
        """Re-analyse everything and record the run on the audit timeline."""
        self.refresh()
        summary = self._summary()
        self._announce("%s: %s script(s), %s issue(s)" % (
            self.t("analyzer_run", "analysis"), summary["scripts"],
            summary["issues"]))
        return summary

    # ------------------------------------------------------- syntax analysis
    # Full syntax pass for the supported languages. Python is checked with the
    # real parser (ast); the other languages use a brace/quote balance scanner.
    # Everything returns plain data so the UI and the report share one source.

    # Languages we can parse fully (with the standard library).
    PYTHON_CATEGORIES = ("python", "py")

    def _is_python(self, category):
        return (category or "").strip().lower() in self.PYTHON_CATEGORIES

    def _syntax_analysis(self, code, category):
        """Detect syntax errors, unclosed blocks and invalid constructs.

        Returns a dict with:
            valid          : True when no blocking syntax error was found
            errors         : list of {severity, line, col, kind, detail}
            unclosed       : list of {kind, line, detail} for unclosed blocks
            indent_issues  : list of {line, kind, detail}
        """
        if self._is_python(category):
            errors, unclosed = self._python_syntax(code)
        else:
            errors, unclosed = self._brace_syntax(code, category)
        indent_issues = self._indentation_analysis(code, category)
        valid = not any(e["severity"] == "high" for e in errors)
        return {"valid": valid, "errors": errors, "unclosed": unclosed,
                "indent_issues": indent_issues}

    def _python_syntax(self, code):
        """Parse Python with ast and classify the failure when it breaks."""
        import ast as _ast
        errors, unclosed = [], []
        text = code or ""
        try:
            _ast.parse(text)
            return errors, unclosed
        except SyntaxError as exc:
            message = (exc.msg or "").strip()
            line = exc.lineno or 0
            col = (exc.offset or 0)
            lowered = message.lower()
            if "indent" in lowered or "unindent" in lowered:
                kind = "analyzer_syntax_indent"
            elif "eof" in lowered:
                kind = "analyzer_syntax_unclosed"
                unclosed.append({"kind": self._guess_unclosed_block(text),
                                 "line": line, "detail": message})
            elif "expected" in lowered:
                kind = "analyzer_syntax_expected"
            elif "invalid syntax" in lowered:
                kind = "analyzer_syntax_invalid"
            else:
                kind = "analyzer_syntax_error"
            errors.append({"severity": "high", "line": line, "col": col,
                           "kind": kind, "detail": message})
            return errors, unclosed
        except (ValueError, MemoryError) as exc:
            errors.append({"severity": "high", "line": 0, "col": 0,
                           "kind": "analyzer_syntax_error", "detail": str(exc)})
            return errors, unclosed

    @staticmethod
    def _guess_unclosed_block(code):
        """Best-effort hint about which block is left unclosed."""
        opener = None
        for line in (code or "").splitlines():
            stripped = line.strip()
            if stripped.endswith(":") and re.match(
                    r"^(if|elif|else|for|while|try|except|finally|with|def|class)\b",
                    stripped):
                if opener is None:
                    opener = stripped
        names = {"def": "analyzer_syntax_unclosed_def",
                 "class": "analyzer_syntax_unclosed_class",
                 "if": "analyzer_syntax_unclosed_if",
                 "for": "analyzer_syntax_unclosed_for",
                 "while": "analyzer_syntax_unclosed_while",
                 "try": "analyzer_syntax_unclosed_try"}
        if opener:
            keyword = opener.split()[0].rstrip(":")
            return names.get(keyword, "analyzer_syntax_unclosed")
        return "analyzer_syntax_unclosed"

    def _brace_syntax(self, code, category):
        """Balance-check braces/brackets/quotes for non-Python languages."""
        errors, unclosed = [], []
        text = code or ""
        pairs = {")": "(", "]": "[", "}": "{"}
        openers = {"(": ")", "[": "]", "{": "}"}
        stack = []
        i = 0
        line = 1
        in_string = None
        while i < len(text):
            char = text[i]
            if char == "\n":
                line += 1
            if in_string:
                if char == "\\":
                    i += 2
                    continue
                if char == in_string:
                    in_string = None
                i += 1
                continue
            if char in ("'", '"'):
                in_string = char
            elif char in openers:
                stack.append((char, line))
            elif char in pairs:
                if stack and stack[-1][0] == pairs[char]:
                    stack.pop()
                else:
                    errors.append({"severity": "high", "line": line, "col": 0,
                                   "kind": "analyzer_syntax_unmatched",
                                   "detail": char})
            i += 1
        if in_string:
            errors.append({"severity": "high", "line": line, "col": 0,
                           "kind": "analyzer_syntax_unterminated_string",
                           "detail": in_string})
            unclosed.append({"kind": "analyzer_syntax_unterminated_string",
                             "line": line, "detail": in_string})
        for char, start_line in stack:
            errors.append({"severity": "high", "line": start_line, "col": 0,
                           "kind": "analyzer_syntax_unclosed", "detail": char})
            unclosed.append({"kind": "analyzer_syntax_unclosed",
                             "line": start_line, "detail": char})
        return errors, unclosed

    def _indentation_analysis(self, code, category):
        """Check indentation consistency (Python/YAML style languages)."""
        if self._is_python(category) or (category or "").lower() in (
                "yaml", "yml"):
            return self._python_indentation(code)
        return self._generic_indentation(code)

    @staticmethod
    def _python_indentation(code):
        """Flag mixed tabs/spaces and irregular indentation steps."""
        issues = []
        widths = []
        for number, line in enumerate((code or "").splitlines(), 1):
            if not line.strip():
                continue
            indent = line[:len(line) - len(line.lstrip())]
            if " " in indent and "\t" in indent:
                issues.append({"line": number, "kind": "analyzer_indent_mixed",
                               "detail": "tabs and spaces mixed"})
            elif "\t" in indent:
                issues.append({"line": number, "kind": "analyzer_indent_tab",
                               "detail": "tab used for indentation"})
            else:
                widths.append(len(indent))
        steps = sorted({w for w in widths if w > 0})
        if len(steps) > 3:
            issues.append({"line": 0, "kind": "analyzer_indent_irregular",
                           "detail": ", ".join(str(s) for s in steps[:8])})
        return issues

    @staticmethod
    def _generic_indentation(code):
        """Generic indentation check: tabs versus spaces consistency."""
        issues = []
        uses_tab = uses_space = False
        for line in (code or "").splitlines():
            if not line.strip():
                continue
            indent = line[:len(line) - len(line.lstrip())]
            if "\t" in indent:
                uses_tab = True
            elif indent:
                uses_space = True
        if uses_tab and uses_space:
            issues.append({"line": 0, "kind": "analyzer_indent_mixed",
                           "detail": "tabs and spaces mixed"})
        elif uses_tab:
            issues.append({"line": 0, "kind": "analyzer_indent_tab",
                           "detail": "tabs used for indentation"})
        return issues

    def _type_analysis(self, code, category):
        """Detect obvious type mismatches with a light heuristic.

        Intentionally conservative: it only flags constructs that are almost
        always wrong (e.g. adding a string literal to a number) so it does not
        produce false positives on ordinary code.
        """
        findings = []
        for number, line in enumerate((code or "").splitlines(), 1):
            stripped = line.split("#", 1)[0]
            if re.search(r"\b\d+(?:\.\d+)?\s*\+\s*[\"']", stripped):
                findings.append({"line": number,
                                 "kind": "analyzer_type_number_str",
                                 "detail": "number + string literal"})
            elif re.search(r"[\"'][^\"']*[\"']\s*\+\s*\d+\b", stripped):
                findings.append({"line": number,
                                 "kind": "analyzer_type_str_number",
                                 "detail": "string literal + number"})
        assigned = {}
        for line in (code or "").splitlines():
            match = re.match(r"^\s*(\w+)\s*=\s*(.+)$", line.strip())
            if not match:
                continue
            name, rhs = match.group(1), match.group(2).strip()
            kind = None
            if re.match(r"^[\"'].*[\"']$", rhs):
                kind = "str"
            elif re.match(r"^-?\d+(?:\.\d+)?$", rhs):
                kind = "num"
            elif rhs in ("True", "False"):
                kind = "bool"
            elif re.match(r"^\[.*\]$", rhs):
                kind = "list"
            elif re.match(r"^\{.*\}$", rhs):
                kind = "dict"
            if kind:
                assigned.setdefault(name, set()).add(kind)
        for name, kinds in assigned.items():
            if len(kinds) > 1:
                findings.append({"line": 0, "kind": "analyzer_type_reassign",
                                 "detail": "%s used as %s" % (
                                     name, ", ".join(sorted(kinds)))})
        return findings

    # ---------------------------------------------------------- AST analysis
    # A full structural representation of the code: functions, classes, control
    # blocks, imports, variables, calls, signatures and a light data-flow pass.
    # Python is walked with the real ast module; other languages fall back to a
    # regex-based extractor so the trees are populated for every script.

    def _ast_analysis(self, code, category):
        """Build the AST trees and the higher-level analyses for one script."""
        if self._is_python(category):
            tree = self._python_ast(code)
        else:
            tree = self._generic_ast(code, category)
        tree["signatures"] = self._signature_analysis(tree)
        tree["data_flow"] = self._data_flow(code, category, tree)
        return tree

    def _python_ast(self, code):
        """Walk the real Python AST and collect every requested tree."""
        import ast as _ast
        functions = []
        classes = []
        blocks = []
        imports = []
        variables = []
        calls = []
        text = code or ""
        try:
            root = _ast.parse(text)
        except SyntaxError:
            # Fall back to the regex extractor so the trees are still useful.
            return self._generic_ast(code, "Python")

        def node_line(node):
            return getattr(node, "lineno", 0)

        class Visitor(_ast.NodeVisitor):
            def visit_FunctionDef(self, node):
                functions.append({
                    "name": node.name, "line": node_line(node),
                    "args": [a.arg for a in node.args.args],
                    "kwonly": [a.arg for a in node.args.kwonlyargs],
                    "vararg": node.args.vararg.arg if node.args.vararg else "",
                    "returns": _ast.dump(node.returns) if node.returns else "",
                    "decorators": [d.id for d in node.decorator_list
                                   if isinstance(d, _ast.Name)],
                })
                self.generic_visit(node)

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_ClassDef(self, node):
                classes.append({"name": node.name, "line": node_line(node),
                                "bases": [b.id for b in node.bases
                                          if isinstance(b, _ast.Name)]})
                self.generic_visit(node)

            def visit_If(self, node):
                blocks.append({"kind": "if", "line": node_line(node)})
                self.generic_visit(node)

            def visit_For(self, node):
                blocks.append({"kind": "for", "line": node_line(node)})
                self.generic_visit(node)

            visit_AsyncFor = visit_For

            def visit_While(self, node):
                blocks.append({"kind": "while", "line": node_line(node)})
                self.generic_visit(node)

            def visit_Try(self, node):
                blocks.append({"kind": "try", "line": node_line(node)})
                for handler in node.handlers:
                    blocks.append({"kind": "except", "line": node_line(handler)})
                self.generic_visit(node)

            visit_TryStar = visit_Try

            def visit_With(self, node):
                blocks.append({"kind": "with", "line": node_line(node)})
                self.generic_visit(node)

            def visit_Import(self, node):
                for alias in node.names:
                    imports.append({"name": alias.name, "line": node_line(node),
                                    "kind": "import"})
                self.generic_visit(node)

            def visit_ImportFrom(self, node):
                imports.append({"name": node.module or "", "line": node_line(node),
                                "kind": "from"})
                for alias in node.names:
                    imports.append({"name": alias.name, "line": node_line(node),
                                    "kind": "from"})
                self.generic_visit(node)

            def visit_Name(self, node):
                if isinstance(node.ctx, _ast.Store):
                    variables.append({"name": node.id, "line": node_line(node),
                                      "kind": "store"})
                elif isinstance(node.ctx, _ast.Load):
                    variables.append({"name": node.id, "line": node_line(node),
                                      "kind": "load"})
                self.generic_visit(node)

            def visit_Call(self, node):
                func = node.func
                name = func.id if isinstance(func, _ast.Name) else (
                    func.attr if isinstance(func, _ast.Attribute) else "")
                if name:
                    calls.append({"name": name, "line": node_line(node),
                                  "args": len(node.args)})
                self.generic_visit(node)

        Visitor().visit(root)
        return {"functions": functions, "classes": classes, "blocks": blocks,
                "imports": imports, "variables": variables, "calls": calls}

    def _generic_ast(self, code, category):
        """Regex-based AST extraction for the non-Python languages."""
        functions, classes, blocks, imports, variables, calls = [], [], [], [], [], []
        text = code or ""
        for match in re.finditer(r"\b(?:function|def|sub|func)\s+(\w+)\s*\(([^)]*)\)",
                                 text, re.IGNORECASE):
            functions.append({"name": match.group(1),
                              "line": text[:match.start()].count("\n") + 1,
                              "args": [p.strip() for p in match.group(2).split(",")
                                       if p.strip()], "returns": "",
                              "decorators": []})
        for match in re.finditer(r"\b(class|interface|struct)\s+(\w+)", text):
            classes.append({"name": match.group(2),
                            "line": text[:match.start()].count("\n") + 1,
                            "bases": []})
        for match in re.finditer(r"\b(if|elif|else|for|foreach|while|try|catch|finally|switch)\b",
                                 text):
            blocks.append({"kind": match.group(1),
                           "line": text[:match.start()].count("\n") + 1})
        for match in re.finditer(r"^\s*(?:import|using|require|from)\s+([\w./-]+)",
                                 text, re.MULTILINE):
            imports.append({"name": match.group(1),
                            "line": text[:match.start()].count("\n") + 1,
                            "kind": "import"})
        for match in re.finditer(r"\b([A-Za-z_]\w*)\s*=(?!=)", text):
            variables.append({"name": match.group(1),
                              "line": text[:match.start()].count("\n") + 1,
                              "kind": "store"})
        for match in re.finditer(r"\b([A-Za-z_]\w*)\s*\(", text):
            calls.append({"name": match.group(1),
                          "line": text[:match.start()].count("\n") + 1,
                          "args": 0})
        return {"functions": functions, "classes": classes, "blocks": blocks,
                "imports": imports, "variables": variables, "calls": calls}

    @staticmethod
    def _signature_analysis(tree):
        """Describe every function signature in a comparable form."""
        signatures = []
        for function in tree.get("functions", []):
            params = function.get("args", []) or []
            signatures.append({
                "name": function.get("name", ""),
                "line": function.get("line", 0),
                "params": params,
                "arity": len(params),
                "returns": function.get("returns", "") or "",
                "decorators": function.get("decorators", []),
                "variadic": any(p.startswith("*") for p in params),
                "keyword": any(p in ("self", "cls") for p in params),
            })
        return signatures

    def _data_flow(self, code, category, tree):
        """A light data-flow view: define/use counts and unused names.

        It reports, per name, how many times it is assigned (defined) and read
        (used), and lists names that are defined but never used, which often
        reveals dead assignments or forgotten code.
        """
        defined, used = {}, {}
        for variable in tree.get("variables", []):
            name = variable.get("name", "")
            if not name:
                continue
            if variable.get("kind") == "store":
                defined[name] = defined.get(name, 0) + 1
            else:
                used[name] = used.get(name, 0) + 1
        entries = []
        for name in sorted(set(defined) | set(used)):
            entries.append({
                "name": name,
                "defined": defined.get(name, 0),
                "used": used.get(name, 0),
                "unused": defined.get(name, 0) > 0 and used.get(name, 0) == 0,
            })
        return {"entries": entries,
                "unused": [e["name"] for e in entries if e["unused"]]}

    def _ast_summary(self, tree):
        """Counts used by the summary cards and the report."""
        return {
            "functions": len(tree.get("functions", [])),
            "classes": len(tree.get("classes", [])),
            "blocks": len(tree.get("blocks", [])),
            "imports": len(tree.get("imports", [])),
            "variables": len(set(v.get("name", "")
                                 for v in tree.get("variables", []))),
            "calls": len(tree.get("calls", [])),
            "unused_vars": len(tree["data_flow"]["unused"])
            if tree.get("data_flow") else 0,
        }

    # ------------------------------------------------- code quality / linter
    # Standards compliance, duplicate code, long functions, complex blocks,
    # too many parameters, deep nesting and dead code — all static.

    MAX_FUNCTION_LINES = 50
    MAX_BLOCK_LINES = 40
    MAX_PARAMS = 5
    MAX_NESTING = 4
    MAX_COMPLEXITY = 10

    # A small set of PEP8 / general style rules, each with a fix hint.
    STYLE_RULES = (
        ("analyzer_style_long_line", "E501"),
        ("analyzer_style_trailing_ws", "W291"),
        ("analyzer_style_tabs", "W191"),
        ("analyzer_style_blank_lines", "E303"),
        ("analyzer_style_import_order", "E402"),
        ("analyzer_style_naming", "N802"),
        ("analyzer_style_comparison", "E711"),
        ("analyzer_style_lambda", "E731"),
    )

    def _style_analysis(self, code, category):
        """Run the linter/style pass and the structural smell detectors.

        Returns a dict with:
            standards     : list of {rule, line, kind, detail} standard breaches
            duplicates    : list of {block, locations} duplicated snippets
            long_funcs    : functions exceeding MAX_FUNCTION_LINES
            complex_blocks: functions exceeding MAX_COMPLEXITY
            many_params   : functions with too many parameters
            deep_nesting  : functions nesting deeper than MAX_NESTING
            dead_code     : unreachable statements after return/raise/break
        """
        standards = self._style_standards(code, category)
        duplicates = self._duplicate_blocks(code)
        long_funcs, complex_blocks, many_params, deep_nesting = \
            self._structural_smells(code)
        dead_code = self._dead_code(code)
        return {
            "standards": standards,
            "duplicates": duplicates,
            "long_funcs": long_funcs,
            "complex_blocks": complex_blocks,
            "many_params": many_params,
            "deep_nesting": deep_nesting,
            "dead_code": dead_code,
        }

    def _style_standards(self, code, category):
        """Check basic standards (PEP8-flavoured) and collect breaches."""
        breaches = []
        text = code or ""
        lines = text.splitlines()
        # --- E501 long lines -------------------------------------------
        for number, line in enumerate(lines, 1):
            if len(line) > 79:
                breaches.append({"rule": "E501", "line": number,
                                 "kind": "analyzer_style_long_line",
                                 "detail": "%s chars" % len(line)})
        # --- whitespace / tabs -----------------------------------------
        for number, line in enumerate(lines, 1):
            if line != line.rstrip():
                breaches.append({"rule": "W291", "line": number,
                                 "kind": "analyzer_style_trailing_ws",
                                 "detail": ""})
            if "\t" in line[:len(line) - len(line.lstrip())]:
                breaches.append({"rule": "W191", "line": number,
                                 "kind": "analyzer_style_tabs", "detail": ""})
        # --- too many consecutive blank lines --------------------------
        blanks = 0
        for number, line in enumerate(lines, 1):
            if not line.strip():
                blanks += 1
                if blanks == 3:
                    breaches.append({"rule": "E303", "line": number,
                                     "kind": "analyzer_style_blank_lines",
                                     "detail": ""})
            else:
                blanks = 0
        # --- comparisons to None/True/False ----------------------------
        for number, line in enumerate(lines, 1):
            if re.search(r"[=!]=\s*(None|True|False)\b", line):
                breaches.append({"rule": "E711", "line": number,
                                 "kind": "analyzer_style_comparison",
                                 "detail": ""})
        # --- assigning a lambda (E731) ---------------------------------
        for number, line in enumerate(lines, 1):
            if re.match(r"^\s*\w+\s*=\s*lambda\b", line):
                breaches.append({"rule": "E731", "line": number,
                                 "kind": "analyzer_style_lambda",
                                 "detail": ""})
        # --- import after code (E402) ----------------------------------
        seen_code = False
        for number, line in enumerate(lines, 1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if re.match(r"^(import|from)\s", stripped):
                if seen_code:
                    breaches.append({"rule": "E402", "line": number,
                                     "kind": "analyzer_style_import_order",
                                     "detail": ""})
            else:
                seen_code = True
        # --- function naming (N802: camelCase) -------------------------
        for number, line in enumerate(lines, 1):
            match = re.match(r"^\s*def\s+([A-Za-z_]\w*)", line)
            if match and re.search(r"[a-z][A-Z]", match.group(1)):
                breaches.append({"rule": "N802", "line": number,
                                 "kind": "analyzer_style_naming",
                                 "detail": match.group(1)})
        return breaches

    @staticmethod
    def _duplicate_blocks(code, min_lines=4):
        """Find duplicated runs of >= min_lines non-blank lines within a file."""
        lines = (code or "").splitlines()
        windows = {}
        for start in range(len(lines) - min_lines + 1):
            window = lines[start:start + min_lines]
            if any(not line.strip() for line in window):
                continue
            key = "\n".join(line.strip() for line in window)
            windows.setdefault(key, []).append(start + 1)
        duplicates = []
        for key, starts in windows.items():
            if len(starts) > 1:
                duplicates.append({"block": key, "locations": starts})
        duplicates.sort(key=lambda d: len(d["locations"]), reverse=True)
        return duplicates

    def _structural_smells(self, code):
        """Detect long functions, complex blocks, many params, deep nesting."""
        long_funcs = []
        complex_blocks = []
        many_params = []
        deep_nesting = []
        for function in self._functions(code):
            body = function["body"]
            lines = len(body.splitlines())
            if lines > self.MAX_FUNCTION_LINES:
                long_funcs.append({"name": function["name"], "lines": lines})
            params = [p for p in function["params"].split(",") if p.strip()]
            if len(params) > self.MAX_PARAMS:
                many_params.append({"name": function["name"],
                                    "params": len(params)})
            # --- complexity --------------------------------------------
            clean = self._strip_strings_and_comments(body)
            branches = len(re.findall(
                r"\b(if|elif|for|while|and|or|except|with|assert)\b", clean))
            complexity = 1 + branches
            if complexity > self.MAX_COMPLEXITY:
                complex_blocks.append({"name": function["name"],
                                       "complexity": complexity})
            # --- nesting depth -----------------------------------------
            depth = self._nesting_depth(body)
            if depth > self.MAX_NESTING:
                deep_nesting.append({"name": function["name"], "depth": depth})
        long_funcs.sort(key=lambda r: r["lines"], reverse=True)
        complex_blocks.sort(key=lambda r: r["complexity"], reverse=True)
        return long_funcs, complex_blocks, many_params, deep_nesting

    @staticmethod
    def _nesting_depth(code):
        """Maximum indentation depth of a code block (in levels)."""
        depths = []
        for line in (code or "").splitlines():
            if not line.strip():
                continue
            indent = len(line) - len(line.lstrip())
            if "\t" in line[:indent]:
                depths.append(indent + 4)
            else:
                depths.append(indent)
        if not depths:
            return 0
        # Normalise to nesting levels using the smallest positive step.
        steps = sorted({d for d in depths if d > 0})
        step = steps[0] if steps else 4
        return int(max(depths) / max(1, step))

    def _dead_code(self, code):
        """Find statements that can never run (after return/raise/break/etc.)."""
        findings = []
        lines = (code or "").splitlines()
        terminator_re = re.compile(r"^\s*(return|raise|break|continue)\b")
        for index, line in enumerate(lines):
            if not terminator_re.match(line):
                continue
            indent = len(line) - len(line.lstrip())
            for offset, follower in enumerate(lines[index + 1:index + 4], start=2):
                if not follower.strip():
                    continue
                follow_indent = len(follower) - len(follower.lstrip())
                stripped = follower.strip()
                # A statement at the same (or deeper) indentation as the
                # terminator, inside the same block, can never run.
                if follow_indent >= indent and not stripped.startswith("#"):
                    if not re.match(r"^(elif|else|except|finally|case|default)\b",
                                    stripped):
                        findings.append({"line": index + offset,
                                         "kind": "analyzer_dead_code",
                                         "detail": stripped[:60]})
                break
        return findings

    # ------------------------------------------------------- runtime analysis
    # Static "dynamic" analysis: we never execute the script, but we reason
    # about what would happen at run time (exceptions, dangerous paths, calls,
    # timing and anomalies).

    # Patterns that commonly raise at run time.
    EXCEPTION_PATTERNS = (
        ("analyzer_rt_zerodiv", r"/\s*0\b", "high"),
        ("analyzer_rt_index", r"\[\s*\d+\s*\]", "low"),
        ("analyzer_rt_attr", r"\.\w+\(", "low"),
        ("analyzer_rt_key", r"\[\s*[\"'][^\"']+[\"']\s*\]", "low"),
        ("analyzer_rt_int", r"\bint\s*\(", "medium"),
        ("analyzer_rt_file", r"\bopen\s*\(", "medium"),
    )
    # Patterns of dangerous execution paths.
    DANGEROUS_PATTERNS = (
        ("analyzer_rt_eval", r"\beval\s*\("),
        ("analyzer_rt_exec", r"\bexec\s*\("),
        ("analyzer_rt_os_system", r"os\.system\s*\("),
        ("analyzer_rt_subprocess", r"subprocess\.\w+"),
        ("analyzer_rt_popen", r"\bpopen\s*\("),
        ("analyzer_rt_pickle", r"pickle\.loads"),
        ("analyzer_rt_shell", r"shell\s*=\s*True"),
        ("analyzer_rt_rmtree", r"shutil\.rmtree"),
    )

    def _runtime_analysis(self, code, category):
        """Reason about the run-time behaviour of a script without running it.

        Returns a dict with:
            steps        : a plain "execution trace" of the top-level flow
            exceptions   : potential exceptions with the offending line
            dangerous    : dangerous execution paths
            stacktrace   : parsed stack traces found in the code/comments
            timing       : estimated execution time per top-level construct
            calls        : module/function calls observed
            anomalies    : run-time anomalies (hot loops, blocking calls, ...)
        """
        return {
            "steps": self._execution_trace(code),
            "exceptions": self._potential_exceptions(code),
            "dangerous": self._dangerous_paths(code),
            "stacktrace": self._stacktrace_analysis(code),
            "timing": self._timing_estimate(code),
            "calls": self._module_calls(code),
            "anomalies": self._runtime_anomalies(code),
        }

    def _execution_trace(self, code):
        """Simulate the top-level flow as an ordered list of steps."""
        steps = []
        for number, line in enumerate((code or "").splitlines(), 1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            kind = None
            if re.match(r"^(import|from)\s", stripped):
                kind = "import"
            elif re.match(r"^(def|class)\s", stripped):
                kind = "define"
            elif re.match(r"^(if|elif)\b", stripped):
                kind = "branch"
            elif re.match(r"^(for|while)\b", stripped):
                kind = "loop"
            elif re.match(r"^try\b", stripped):
                kind = "try"
            elif re.match(r"^return\b", stripped):
                kind = "return"
            elif re.match(r"^print\b", stripped):
                kind = "output"
            elif re.match(r"^\w+\s*=", stripped):
                kind = "assign"
            if kind:
                steps.append({"line": number, "kind": kind,
                              "detail": stripped[:60]})
        return steps

    def _potential_exceptions(self, code):
        """Flag constructs that frequently raise at run time."""
        findings = []
        for number, line in enumerate((code or "").splitlines(), 1):
            stripped = line.split("#", 1)[0]
            for kind, pattern, severity in self.EXCEPTION_PATTERNS:
                if re.search(pattern, stripped):
                    findings.append({"line": number, "kind": kind,
                                     "severity": severity,
                                     "detail": stripped.strip()[:60]})
        return findings

    def _dangerous_paths(self, code):
        """Detect dangerous execution paths worth a security review."""
        lowered = (code or "").lower()
        findings = []
        for kind, pattern in self.DANGEROUS_PATTERNS:
            for match in re.finditer(pattern, lowered):
                line = lowered[:match.start()].count("\n") + 1
                findings.append({"kind": kind, "line": line})
        return findings

    @staticmethod
    def _stacktrace_analysis(code):
        """Extract and interpret any stack trace embedded in the code."""
        traces = []
        current = []
        for line in (code or "").splitlines():
            if re.search(r"Traceback \(most recent call last\)", line):
                current = [line.strip()]
                continue
            if current:
                if re.match(r'^\s+File "', line) or re.match(r"^\s+\w+:", line) \
                        or line.strip().startswith("at "):
                    current.append(line.strip())
                else:
                    if current:
                        traces.append({"lines": current,
                                       "frames": sum(1 for l in current
                                                     if l.startswith("File")
                                                     or l.startswith("at "))})
                    current = []
        if current:
            traces.append({"lines": current,
                           "frames": sum(1 for l in current
                                         if l.startswith("File")
                                         or l.startswith("at "))})
        return traces

    def _timing_estimate(self, code):
        """Estimate the run-time cost of each top-level construct.

        A coarse cost model: loops cost more, nested loops far more, and I/O or
        network calls cost most. It is a ranking aid, not a stopwatch.
        """
        rows = []
        for number, line in enumerate((code or "").splitlines(), 1):
            stripped = line.strip()
            cost = 0
            if re.match(r"^(for|while)\b", stripped):
                cost = 10
                if len(line) - len(line.lstrip()) >= 4:
                    cost = 30          # nested loop
            elif re.search(r"\b(open|requests|urllib|socket|input)\b", stripped):
                cost = 50
            elif re.search(r"\b(sleep|read|write|connect)\s*\(", stripped):
                cost = 40
            elif re.match(r"^(if|elif)\b", stripped):
                cost = 2
            elif re.match(r"^\w+\s*=", stripped):
                cost = 1
            if cost:
                rows.append({"line": number, "cost": cost,
                             "detail": stripped[:60]})
        rows.sort(key=lambda r: r["cost"], reverse=True)
        return rows

    def _module_calls(self, code):
        """Trace module and function calls in the script."""
        calls = {}
        for number, line in enumerate((code or "").splitlines(), 1):
            for match in re.finditer(r"\b([A-Za-z_]\w*)\.([A-Za-z_]\w*)\s*\(", line):
                key = "%s.%s" % (match.group(1), match.group(2))
                calls.setdefault(key, []).append(number)
            for match in re.finditer(r"(?<![\w.])([A-Za-z_]\w*)\s*\(", line):
                name = match.group(1)
                if name in ("if", "for", "while", "return", "def", "class",
                            "print"):
                    continue
                calls.setdefault(name, []).append(number)
        return [{"name": name, "count": len(lines), "lines": lines}
                for name, lines in sorted(calls.items())]

    def _runtime_anomalies(self, code):
        """Flag likely run-time anomalies (hot loops, blocking calls, ...)."""
        anomalies = []
        lowered = (code or "")
        # --- loops without a break/return guard (potential infinite loop) --
        for match in re.finditer(r"^(\s*)(while\s+True|for\s+\w+\s+in\s+)", lowered,
                                 re.MULTILINE):
            line = lowered[:match.start()].count("\n") + 1
            block = lowered[match.start():match.start() + 400]
            if "break" not in block and "return" not in block:
                anomalies.append({"kind": "analyzer_rt_infinite_loop",
                                  "line": line, "detail": ""})
        # --- blocking calls --------------------------------------------
        for number, line in enumerate(lowered.splitlines(), 1):
            if re.search(r"\b(input|sleep|recv|accept)\s*\(", line):
                anomalies.append({"kind": "analyzer_rt_blocking",
                                  "line": number, "detail": line.strip()[:40]})
        # --- resource opened but never closed --------------------------
        opens = len(re.findall(r"\bopen\s*\(", lowered))
        closes = len(re.findall(r"\.close\s*\(|\bwith\s+open", lowered))
        if opens > closes:
            anomalies.append({"kind": "analyzer_rt_unclosed_resource",
                              "line": 0,
                              "detail": "%s open / %s close" % (opens, closes)})
        return anomalies

    def _runtime_summary(self, runtime):
        """Counts used by the summary cards and the report."""
        return {
            "steps": len(runtime.get("steps", [])),
            "exceptions": len(runtime.get("exceptions", [])),
            "dangerous": len(runtime.get("dangerous", [])),
            "traces": len(runtime.get("stacktrace", [])),
            "calls": len(runtime.get("calls", [])),
            "anomalies": len(runtime.get("anomalies", [])),
        }

    # --------------------------------------------------- dependency analysis
    # Detects missing, unused and conflicting dependencies, interpreter version
    # mismatches and system-level requirements. Imports / includes / requires
    # are parsed for every supported language.

    # Imports that require a package whose installed version we want to know.
    IMPORT_PATTERNS_ALL = {
        "python": (
            re.compile(r"^\s*import\s+([\w., ]+)", re.MULTILINE),
            re.compile(r"^\s*from\s+([\w.]+)\s+import", re.MULTILINE),
        ),
        "javascript": (
            re.compile(r"""(?:require|from)\s*\(?["']([^"']+)["']"""),
            None,
        ),
        "java": (re.compile(r"^\s*import\s+([\w.*]+);", re.MULTILINE), None),
        "csharp": (re.compile(r"^\s*using\s+([\w.]+);", re.MULTILINE), None),
        "cpp": (re.compile(r'^\s*#include\s*[<"]([^>"]+)[>"]', re.MULTILINE), None),
        "php": (re.compile(r"^\s*(?:use|require|include)[_\w]*\s*[\('\"]([^)'\";]+)",
                           re.MULTILINE | re.IGNORECASE), None),
        "powershell": (
            re.compile(r"^\s*(?:Import-Module|using\s+module)\s+([\w.\\/-]+)",
                       re.MULTILINE | re.IGNORECASE), None),
        "bash": (re.compile(r"^\s*(?:source|\.)\s+(\S+)", re.MULTILINE), None),
    }
    # Languages whose style/language rules we can compare against.
    STYLE_STANDARDS = {
        "python": "PEP8", "javascript": "Google Style", "java": "Google Style",
        "csharp": "Microsoft Style", "cpp": "Google Style", "php": "PSR-12",
        "powershell": "PowerShell Style", "bash": "Google Shell Style",
    }
    # Interpreter requirement lines and the version they demand.
    INTERPRETER_PATTERNS = (
        (re.compile(r"#!\s*.*?(python\d?|bash|sh|node|pwsh|perl|ruby|php)"),
         "shebang"),
        (re.compile(r"python_requires\s*[=:]\s*[\"']?([<>=!~][\w.*]+)"),
         "python_requires"),
        (re.compile(r"requires\s+(python|node|php|ruby)\s*([<>=~]+[\d.*]+)",
                    re.IGNORECASE), "requires"),
        (re.compile(r"\"engine[s]?\"\s*:\s*\{[^}]*\"(\w+)\"\s*:\s*\"([^\"]+)\""),
         "package.json"),
        (re.compile(r"REQUIRED_VERSION\s*=\s*[\"']?([\d.]+)"), "required_version"),
        (re.compile(r"^\s*#pragma\s+version\s+([\d.]+)", re.MULTILINE), "pragma"),
    )

    def _dependency_analysis(self, code, category):
        """Full dependency analysis for one script.

        Returns a dict with:
            imports      : list of {name, kind, line, internal, used}
            missing      : imported modules not installed and not stdlib
            unused       : imported modules never referenced again
            conflicts    : version conflicts between declared requirements
            interpreters : declared interpreter/runtime version requirements
            system       : system-level dependencies (exe / os modules)
            standards    : the style standard inferred for the language
        """
        imports = self._parse_imports(code, category)
        installed = self._installed_packages()
        missing, unused = [], []
        used_names = self._used_names(code)
        for entry in imports:
            base = entry["name"].split(".")[0]
            entry["used"] = base in used_names
            if entry["internal"]:
                continue
            real = installed.get(base.lower())
            if real is None:
                missing.append({"name": base, "kind": entry["kind"],
                                "line": entry["line"]})
            else:
                entry["version"] = real
            if not entry["used"]:
                unused.append({"name": base, "kind": entry["kind"],
                               "line": entry["line"]})
        interpreters = self._interpreter_requirements(code)
        conflicts = self._dependency_conflicts(code, installed)
        system = self._system_dependencies(code, category, imports)
        return {
            "imports": imports,
            "missing": self._dedupe(missing),
            "unused": self._dedupe(unused),
            "conflicts": conflicts,
            "interpreters": interpreters,
            "system": system,
            "standards": self.STYLE_STANDARDS.get(
                self._language_key(category), "generic"),
        }

    @staticmethod
    def _dedupe(rows):
        seen = set()
        result = []
        for row in rows:
            key = (row["name"], row["kind"])
            if key in seen:
                continue
            seen.add(key)
            result.append(row)
        return result

    @staticmethod
    def _language_key(category):
        key = (category or "").strip().lower()
        aliases = {"py": "python", "js": "javascript", "ts": "javascript",
                   "ps1": "powershell", "sh": "bash", "shell": "bash",
                   "cs": "csharp", "c++": "cpp", "c": "cpp"}
        return aliases.get(key, key)

    def _parse_imports(self, code, category):
        """Parse imports / includes / requires for the script's language."""
        key = self._language_key(category)
        patterns = self.IMPORT_PATTERNS_ALL.get(key)
        imports = []
        if not patterns:
            return imports
        text = code or ""
        # --- Python: import / from ----------------------------------
        if key == "python":
            for number, line in enumerate(text.splitlines(), 1):
                code_line = line.split("#", 1)[0]
                match = re.match(r"^\s*import\s+(.+)", code_line)
                if match:
                    for part in match.group(1).split(","):
                        base = part.strip().split(" as ")[0].strip().split(".")[0]
                        if base:
                            imports.append(self._import_row(base, "import",
                                                            number))
                match = re.match(r"^\s*from\s+([\w.]+)\s+import", code_line)
                if match:
                    imports.append(self._import_row(
                        match.group(1).split(".")[0], "from", number))
            return imports
        # --- other languages ----------------------------------------
        for pattern in patterns:
            if pattern is None:
                continue
            for match in pattern.finditer(text):
                names = [match.group(1)]
                for name in names:
                    if not name:
                        continue
                    base = re.split(r"[/.\\]", name)[0]
                    line = text[:match.start()].count("\n") + 1
                    imports.append(self._import_row(base or name,
                                                    "include" if key in (
                                                        "cpp", "php") else "import",
                                                    line))
        return imports

    def _import_row(self, name, kind, line):
        """Build one import row, flagging standard-library membership."""
        base = (name or "").split(".")[0].split("/")[0].strip()
        internal = base in STDLIB_MODULES or base in ("sys", "os", "re")
        return {"name": name, "kind": kind, "line": line,
                "internal": internal, "used": True, "version": ""}

    @staticmethod
    def _used_names(code):
        """Names actually referenced in the code body (to detect unused ones)."""
        used = set()
        for line in (code or "").splitlines():
            stripped = line.strip()
            if stripped.startswith(("import ", "from ", "#")):
                continue
            for match in re.finditer(r"\b([A-Za-z_]\w*)\b", stripped):
                used.add(match.group(1))
        return used

    @classmethod
    def _installed_packages(cls):
        """Installed distributions as {lower-name: version} (best effort)."""
        if cls._installed_package_cache is not None:
            return cls._installed_package_cache
        packages = {}
        try:
            from importlib import metadata as _meta
            for dist in _meta.distributions():
                try:
                    name = dist.metadata["Name"]
                except Exception:
                    name = None
                if name:
                    packages[name.lower().replace("_", "-")] = dist.version
        except Exception:
            pass
        cls._installed_package_cache = packages
        return packages

    def _interpreter_requirements(self, code):
        """Declared interpreter/runtime version requirements for the script."""
        rows = []
        text = code or ""
        for pattern, kind in self.INTERPRETER_PATTERNS:
            for match in pattern.finditer(text):
                groups = [g for g in match.groups() if g]
                rows.append({"kind": kind,
                             "detail": " ".join(groups) or match.group(0)[:40],
                             "line": text[:match.start()].count("\n") + 1})
        return rows

    def _dependency_conflicts(self, code, installed):
        """Detect version conflicts in declared requirements.

        Repeated requirements for the same package with different operators or
        versions, or a declared version that disagrees with the installed one,
        are reported as conflicts.
        """
        conflicts = []
        declared = {}
        text = code or ""
        for match in re.finditer(
                r"([A-Za-z][\w.-]+)\s*([<>=!~]=?)\s*([\d][\w.*]*)", text):
            name = match.group(1).lower()
            declared.setdefault(name, []).append(
                (match.group(2), match.group(3)))
        for name, specs in declared.items():
            versions = set(v for _op, v in specs)
            if len(versions) > 1:
                conflicts.append({"name": name,
                                  "detail": ", ".join("%s%s" % (op, v)
                                                      for op, v in specs)})
            elif name in installed and list(installed[name]) != list(versions):
                installed_version = installed[name]
                if installed_version not in versions:
                    conflicts.append({
                        "name": name,
                        "detail": "declared %s%s vs installed %s" % (
                            specs[0][0], specs[0][1], installed_version)})
        return conflicts

    def _system_dependencies(self, code, category, imports):
        """System-level requirements: executables, OS modules and services."""
        import shutil as _shutil
        rows = []
        text = code or ""
        # --- executable calls (subprocess / os.system / shell) -----------
        for match in re.finditer(
                r"(?:subprocess\.(?:run|call|Popen|check_output)|os\.system)\s*"
                r"\(\s*[\[\(]?\s*[\"']([^\"']+)[\"']", text):
            command = match.group(1).split()[0]
            rows.append({"name": command, "kind": "analyzer_dep_executable",
                         "line": text[:match.start()].count("\n") + 1,
                         "available": bool(_shutil.which(command))})
        # --- OS-specific modules ---------------------------------------
        os_modules = [i["name"] for i in imports
                      if i["name"] in ("winreg", "ctypes", "fcntl", "termios",
                                       "msvcrt", "resource")]
        for name in os_modules:
            rows.append({"name": name, "kind": "analyzer_dep_os_module",
                         "line": 0, "available": True})
        # --- services / daemons referenced ------------------------------
        for match in re.finditer(r"(systemctl|service|sc\.exe|net\s+start)\s+"
                                 r"(?:start\s+)?([\w.-]+)", text):
            rows.append({"name": match.group(2),
                         "kind": "analyzer_dep_service", "line": 0,
                         "available": True})
        return rows

    def _dependency_summary(self, dep):
        """Counts used by the summary and the report."""
        return {
            "imports": len(dep.get("imports", [])),
            "missing": len(dep.get("missing", [])),
            "unused": len(dep.get("unused", [])),
            "conflicts": len(dep.get("conflicts", [])),
            "interpreters": len(dep.get("interpreters", [])),
            "system": len(dep.get("system", [])),
        }

    # ------------------------------------------------------ metrics engine
    # A consolidated metrics pass: raw counts (lines, functions, classes,
    # imports, errors, warnings) plus three complexity measures (cyclomatic,
    # structural, logical). Everything is derived statically from the code.

    def _metrics_engine(self, script):
        """Compute the full metric set for one script.

        ``script`` is the raw entry; the already-computed sub-analyses are
        passed in through ``_analyze_script`` so nothing is done twice.
        Returns a flat dict of numbers plus a small per-function breakdown.
        """
        code = script.get("code", "") or ""
        category = script.get("category", "")
        metrics = self._line_metrics(code)
        functions = self._functions(code)
        classes = self._classes(code)
        internal, external, all_imports = self._imports(code)
        syntax = self._syntax_analysis(code, category)
        issues = self._issues(code, category)
        complexity = self._complexity(code)

        errors = sum(1 for issue in issues if issue["severity"] == "high")
        errors += len(syntax.get("errors", []))
        warnings = sum(1 for issue in issues
                       if issue["severity"] in ("medium", "low"))
        warnings += len(syntax.get("indent_issues", []))

        cyclomatic = self._cyclomatic_complexity(code)
        structural = self._structural_complexity(code, functions, classes)
        logical = self._logical_complexity(code)

        return {
            "lines": metrics["total_lines"],
            "code_lines": metrics["code_lines"],
            "comment_lines": metrics["comment_lines"],
            "blank_lines": metrics["blank_lines"],
            "functions": len(functions),
            "classes": len(classes),
            "imports": len(all_imports),
            "internal_imports": len(internal),
            "external_imports": len(external),
            "errors": errors,
            "warnings": warnings,
            "cyclomatic": cyclomatic,
            "structural": structural,
            "logical": logical,
            "complexity_total": cyclomatic + structural + logical,
            "per_function": complexity,
            "loc_per_function": round(metrics["code_lines"] /
                                      max(1, len(functions)), 1),
        }

    def _cyclomatic_complexity(self, code):
        """McCabe-style cyclomatic complexity of the whole script.

        Starts at 1 and adds one for every decision point: branches, loops,
        boolean operators, handlers and comprehensions.
        """
        clean = self._strip_strings_and_comments(code)
        decisions = len(re.findall(
            r"\b(if|elif|for|while|except|and|or|with|assert)\b", clean))
        comprehensions = len(re.findall(r"\bfor\b[^\n]*\b(in)\b[^\n]*", clean))
        return 1 + decisions + comprehensions

    def _structural_complexity(self, code, functions, classes):
        """Nesting- and size-driven structural complexity.

        Combines the maximum nesting depth, the number of functions/classes and
        the average function length into a single structural score.
        """
        depth = self._nesting_depth(code)
        function_factor = len(functions) + len(classes) * 2
        lines = len((code or "").splitlines())
        size_factor = max(0, lines // 50)
        return depth * 2 + function_factor + size_factor

    def _logical_complexity(self, code):
        """Logical complexity: the density of operators and distinct paths.

        Counts comparison/assignment/logical operators and distinct control
        keywords, then normalises by the number of code lines so a dense,
        branch-heavy file scores higher than a long but simple one.
        """
        clean = self._strip_strings_and_comments(code)
        operators = len(re.findall(r"(==|!=|<=|>=|<|>|\+|-|\*|/|%|\*\*)", clean))
        logic = len(re.findall(r"\b(and|or|not|in|is)\b", clean))
        controls = len(set(re.findall(
            r"\b(if|elif|else|for|while|try|except|finally|with|match|case)\b",
            clean)))
        code_lines = max(1, len([l for l in (code or "").splitlines()
                                 if l.strip()]))
        return round((operators + logic * 2 + controls * 3) / code_lines, 2)

    def _metrics_totals(self):
        """Sum the per-script metrics into workspace-wide totals."""
        totals = {"lines": 0, "code_lines": 0, "comment_lines": 0,
                  "blank_lines": 0, "functions": 0, "classes": 0,
                  "imports": 0, "errors": 0, "warnings": 0,
                  "cyclomatic": 0, "structural": 0, "logical": 0.0}
        results = self._state.get("results", [])
        for result in results:
            metrics = result.get("metrics_engine") or {}
            for key in totals:
                totals[key] += metrics.get(key, 0)
        totals["logical"] = round(totals["logical"], 2)
        totals["scripts"] = len(results)
        return totals

    def _metrics_summary(self, metrics):
        """Compact metrics dict used by the report and the cards."""
        keys = ("lines", "functions", "classes", "imports", "errors",
                "warnings", "cyclomatic", "structural", "logical")
        return dict((key, metrics.get(key, 0)) for key in keys)

    # -------------------------------------------------------------- UI build
    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)

        header = QtWidgets.QHBoxLayout()
        self.title_label = QtWidgets.QLabel(self.t("analyzer", "Analyzer"))
        self.title_label.setStyleSheet(
            "font-size: 14px; font-weight: bold; color: #8ab4f8;")
        header.addWidget(self.title_label)
        header.addStretch()
        # ---- Operations menu: single-script analysis actions --------------–
        self.btn_operations = QtWidgets.QToolButton()
        self.btn_operations.setText(self.t("analyzer_operations", "Operations"))
        self.btn_operations.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.operations_menu = QtWidgets.QMenu(self.btn_operations)
        self.operation_actions = {}
        for key, label_key, fallback in ANALYZER_OPERATIONS:
            action = self.operations_menu.addAction(
                self.t(label_key, fallback))
            action.triggered.connect(
                lambda _checked=False, k=key: self.run_operation(k))
            self.operation_actions[key] = action
        self.btn_operations.setMenu(self.operations_menu)
        header.addWidget(self.btn_operations)
        self.btn_analyse = QtWidgets.QPushButton(
            self.t("analyzer_analyse", "Analyse"))
        self.btn_analyse.clicked.connect(self.analyze_and_announce)
        header.addWidget(self.btn_analyse)
        # ---- Export menu: every report format ------------------------------–
        self.btn_export = QtWidgets.QToolButton()
        self.btn_export.setText(self.t("analyzer_export", "Export"))
        self.btn_export.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.export_menu = QtWidgets.QMenu(self.btn_export)
        self.export_actions = {}
        for key, label_key, fallback in ANALYZER_EXPORTS:
            action = self.export_menu.addAction(self.t(label_key, fallback))
            action.triggered.connect(
                lambda _checked=False, k=key: self.export(k))
            self.export_actions[key] = action
        self.btn_export.setMenu(self.export_menu)
        header.addWidget(self.btn_export)
        # ---- Premium menu: proactive / whole-project analyses ---------------
        self.btn_premium = QtWidgets.QToolButton()
        self.btn_premium.setText(self.t("analyzer_premium", "Premium"))
        self.btn_premium.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.premium_menu = QtWidgets.QMenu(self.btn_premium)
        self.premium_actions = {}
        for key, label_key, fallback in ANALYZER_PREMIUM:
            action = self.premium_menu.addAction(self.t(label_key, fallback))
            action.triggered.connect(
                lambda _checked=False, k=key: self.run_premium(k))
            self.premium_actions[key] = action
        self.btn_premium.setMenu(self.premium_menu)
        header.addWidget(self.btn_premium)
        # ---- Architecture summary button (opens the Architecture page) -----
        self.btn_architecture = QtWidgets.QPushButton(
            self.t("analyzer_architecture", "Architecture"))
        self.btn_architecture.clicked.connect(
            lambda: self._focus_subtab("analyzer_tab_architecture"))
        header.addWidget(self.btn_architecture)
        layout.addLayout(header)

        self.subtabs = QtWidgets.QTabWidget()
        layout.addWidget(self.subtabs, 1)
        self._subtab_keys = {}
        self._build_overview_tab()
        self._build_metrics_tab()
        self._build_syntax_tab()
        self._build_ast_tab()
        self._build_structure_tab()
        self._build_complexity_tab()
        self._build_quality_tab()
        self._build_runtime_tab()
        self._build_issues_tab()
        self._build_dependencies_tab()
        self._build_history_tab()
        self._build_config_tab()
        self._build_operations_tab()
        self._build_architecture_tab()
        self._build_premium_tab()

        self.status = QtWidgets.QLabel("")
        self.status.setStyleSheet("color: #9aa0a6;")
        layout.addWidget(self.status)

        self.subtabs.currentChanged.connect(self._on_subtab_changed)

    def _make_page(self, key, fallback):
        page = QtWidgets.QWidget()
        page_layout = QtWidgets.QVBoxLayout(page)
        index = self.subtabs.addTab(page, self.t(key, fallback))
        self._subtab_keys[index] = (key, fallback)
        return page, page_layout

    def _make_feed(self, title, columns):
        box = QtWidgets.QGroupBox(title)
        box.setStyleSheet("QGroupBox { color:#8ab4f8; }")
        inner = QtWidgets.QVBoxLayout(box)
        tree = QtWidgets.QTreeWidget()
        tree.setAlternatingRowColors(True)
        tree.setRootIsDecorated(False)
        tree.setColumnCount(len(columns))
        tree.setHeaderLabels(columns)
        tree.setFont(QFont("Consolas", 9))
        inner.addWidget(tree)
        box.tree = tree
        return box

    def _make_card(self, title, value):
        frame = QtWidgets.QFrame()
        frame.setFrameShape(QtWidgets.QFrame.StyledPanel)
        frame.setStyleSheet(
            """QFrame { background:#2d2d2d; border:1px solid #404040;"""
            " border-radius:4px; }")
        inner = QtWidgets.QVBoxLayout(frame)
        title_label = QtWidgets.QLabel(title)
        title_label.setStyleSheet("color:#9aa0a6; border:none;")
        value_label = QtWidgets.QLabel(value)
        value_label.setStyleSheet(
            "font-size: 22px; font-weight: bold; color:#dfe7f5; border:none;")
        inner.addWidget(title_label)
        inner.addWidget(value_label)
        return {"frame": frame, "title": title_label, "value": value_label}

    def _build_overview_tab(self):
        page, page_layout = self._make_page("analyzer_tab_overview", "Overview")
        cards = QtWidgets.QHBoxLayout()
        self.card_scripts = self._make_card(
            self.t("analyzer_scripts", "Scripts"), "0")
        self.card_issues = self._make_card(
            self.t("analyzer_issues", "Issues"), "0")
        self.card_score = self._make_card(
            self.t("analyzer_score", "Avg. score"), "-")
        self.card_worst = self._make_card(
            self.t("analyzer_worst", "Lowest score"), "-")
        for card in (self.card_scripts, self.card_issues, self.card_score,
                     self.card_worst):
            cards.addWidget(card["frame"], 1)
        page_layout.addLayout(cards)
        self.overview_tree = self._make_feed(
            self.t("analyzer_overview", "Per-script overview"),
            [self.t("name", "Name"), self.t("category_col", "Category"),
             self.t("analyzer_lines", "Lines"), self.t("analyzer_funcs", "Funcs"),
             self.t("analyzer_classes", "Classes"),
             self.t("analyzer_score", "Score")])
        page_layout.addWidget(self.overview_tree, 1)

    def _build_structure_tab(self):
        page, page_layout = self._make_page("analyzer_tab_structure",
                                            "Structure")
        self.structure_tree = self._make_feed(
            self.t("analyzer_structure", "Structure"),
            [self.t("analyzer_symbol", "Symbol"),
             self.t("analyzer_kind", "Kind"),
             self.t("analyzer_in", "In script")])
        page_layout.addWidget(self.structure_tree, 1)

    def _build_complexity_tab(self):
        page, page_layout = self._make_page("analyzer_tab_complexity",
                                            "Complexity")
        self.complexity_tree = self._make_feed(
            self.t("analyzer_complexity", "Complexity"),
            [self.t("analyzer_function", "Function"),
             self.t("analyzer_complexity", "Complexity"),
             self.t("analyzer_lines", "Lines"),
             self.t("analyzer_params", "Params"),
             self.t("audit_where", "Script")])
        page_layout.addWidget(self.complexity_tree, 1)

    def _build_issues_tab(self):
        page, page_layout = self._make_page("analyzer_tab_issues", "Issues")
        self.issues_tree = self._make_feed(
            self.t("analyzer_issues", "Issues"),
            [self.t("audit_where", "Script"),
             self.t("analyzer_issue_kind", "Issue"),
             self.t("audit_detail", "Detail"),
             self.t("analyzer_line", "Line"),
             self.t("audit_severity", "Severity")])
        page_layout.addWidget(self.issues_tree, 1)

    def _build_dependencies_tab(self):
        page, page_layout = self._make_page("analyzer_tab_dependencies",
                                            "Dependencies")
        cards = QtWidgets.QHBoxLayout()
        self.card_dep_missing = self._make_card(
            self.t("analyzer_dep_missing", "Missing"), "0")
        self.card_dep_unused = self._make_card(
            self.t("analyzer_dep_unused", "Unused"), "0")
        self.card_dep_conflict = self._make_card(
            self.t("analyzer_dep_conflicts", "Conflicts"), "0")
        self.card_dep_system = self._make_card(
            self.t("analyzer_dep_system", "System"), "0")
        for card in (self.card_dep_missing, self.card_dep_unused,
                     self.card_dep_conflict, self.card_dep_system):
            cards.addWidget(card["frame"], 1)
        page_layout.addLayout(cards)
        self.dependencies_tree = self._make_feed(
            self.t("analyzer_dependencies", "Dependencies"),
            [self.t("analyzer_dep_item", "Item"),
             self.t("analyzer_kind", "Kind"),
             self.t("audit_detail", "Detail"),
             self.t("analyzer_used_by", "Used by")])
        page_layout.addWidget(self.dependencies_tree, 1)

    def _build_syntax_tab(self):
        page, page_layout = self._make_page("analyzer_tab_syntax", "Syntax")
        self.syntax_tree = self._make_feed(
            self.t("analyzer_syntax", "Syntax analysis"),
            [self.t("audit_where", "Script"),
             self.t("analyzer_syntax_kind", "Kind"),
             self.t("audit_detail", "Detail"),
             self.t("analyzer_line", "Line"),
             self.t("audit_severity", "Severity")])
        page_layout.addWidget(self.syntax_tree, 1)

    def _build_ast_tab(self):
        page, page_layout = self._make_page("analyzer_tab_ast", "AST")
        self.ast_tree = QtWidgets.QTreeWidget()
        self.ast_tree.setAlternatingRowColors(True)
        self.ast_tree.setColumnCount(3)
        self.ast_tree.setHeaderLabels([
            self.t("analyzer_ast_node", "Node"),
            self.t("analyzer_kind", "Kind"),
            self.t("analyzer_line", "Line")])
        self.ast_tree.setFont(QFont("Consolas", 9))
        page_layout.addWidget(self.ast_tree, 1)

    def _fill_syntax(self):
        """Fill the syntax tree with errors, unclosed blocks and indentation."""
        tree = self.syntax_tree.tree
        tree.clear()
        for result in self._state.get("results", []):
            syntax = result.get("syntax") or {}
            rows = []
            for error in syntax.get("errors", []):
                rows.append((error.get("kind", ""), error.get("detail", ""),
                             error.get("line", 0), error.get("severity", "high")))
            for item in syntax.get("unclosed", []):
                rows.append((item.get("kind", ""), item.get("detail", ""),
                             item.get("line", 0), "high"))
            for item in syntax.get("indent_issues", []):
                rows.append((item.get("kind", ""), item.get("detail", ""),
                             item.get("line", 0), "low"))
            for kind, detail, line, severity in rows:
                item = QtWidgets.QTreeWidgetItem([
                    result["name"], self.t(kind, kind), detail,
                    str(line) if line else "",
                    severity.upper()])
                colour = SEVERITY_COLOURS.get(severity, "#dfe7f5")
                item.setForeground(1, QtGui.QColor(colour))
                item.setForeground(4, QtGui.QColor(colour))
                tree.addTopLevelItem(item)
        if tree.topLevelItemCount() == 0:
            tree.addTopLevelItem(self._placeholder(5))

    def _fill_ast(self):
        """Fill the AST tree with the full structural breakdown."""
        tree = self.ast_tree
        tree.clear()
        for result in self._state.get("results", []):
            ast = result.get("ast") or {}
            root = QtWidgets.QTreeWidgetItem(
                [result["name"], result.get("category", ""), ""])
            root.setForeground(0, QtGui.QColor("#8ab4f8"))
            tree.addTopLevelItem(root)
            groups = (
                ("analyzer_ast_functions", "functions",
                 lambda f: "%s(%s)" % (f.get("name", ""),
                                       ", ".join(f.get("args", []) or [])),
                 "#8ab4f8"),
                ("analyzer_ast_classes", "classes",
                 lambda c: c.get("name", ""), "#c586c0"),
                ("analyzer_ast_blocks", "blocks",
                 lambda b: b.get("kind", ""), "#f7c66d"),
                ("analyzer_ast_imports", "imports",
                 lambda i: i.get("name", ""), "#9be9a8"),
                ("analyzer_ast_calls", "calls",
                 lambda c: c.get("name", ""), "#ff9a9a"),
            )
            for key, field, formatter, colour in groups:
                values = ast.get(field, []) or []
                node = QtWidgets.QTreeWidgetItem(
                    [self.t(key, key), str(len(values)), ""])
                node.setForeground(0, QtGui.QColor(colour))
                for value in values:
                    node.addChild(QtWidgets.QTreeWidgetItem(
                        [formatter(value), "", str(value.get("line", ""))]))
                root.addChild(node)
            # --- variables tree ---------------------------------------
            flow = ast.get("data_flow", {}) or {}
            vnode = QtWidgets.QTreeWidgetItem(
                [self.t("analyzer_ast_variables", "variables"),
                 str(len(flow.get("entries", []))), ""])
            vnode.setForeground(0, QtGui.QColor("#b5cea8"))
            for entry in flow.get("entries", []):
                child = QtWidgets.QTreeWidgetItem([
                    entry["name"],
                    "def=%s use=%s" % (entry["defined"], entry["used"]), ""])
                if entry.get("unused"):
                    child.setForeground(1, QtGui.QColor("#ff7b72"))
                vnode.addChild(child)
            root.addChild(vnode)
            # --- signatures tree --------------------------------------
            snode = QtWidgets.QTreeWidgetItem(
                [self.t("analyzer_ast_signatures", "signatures"),
                 str(len(ast.get("signatures", []))), ""])
            snode.setForeground(0, QtGui.QColor("#8ab4f8"))
            for signature in ast.get("signatures", []):
                child = QtWidgets.QTreeWidgetItem([
                    "%s(...)" % signature["name"],
                    "arity=%s" % signature["arity"],
                    str(signature["line"])])
                snode.addChild(child)
            root.addChild(snode)
            # --- data flow tree ---------------------------------------
            dnode = QtWidgets.QTreeWidgetItem(
                [self.t("analyzer_ast_data_flow", "data flow"),
                 str(len(flow.get("unused", []))) + " unused", ""])
            dnode.setForeground(0, QtGui.QColor("#f7c66d"))
            for name in flow.get("unused", []):
                child = QtWidgets.QTreeWidgetItem(
                    [name, self.t("analyzer_ast_unused", "defined, never used"), ""])
                child.setForeground(1, QtGui.QColor("#ff7b72"))
                dnode.addChild(child)
            root.addChild(dnode)
            root.setExpanded(True)
        if tree.topLevelItemCount() == 0:
            tree.addTopLevelItem(self._placeholder(3))

    def _build_quality_tab(self):
        page, page_layout = self._make_page("analyzer_tab_quality", "Quality")
        self.quality_tree = QtWidgets.QTreeWidget()
        self.quality_tree.setAlternatingRowColors(True)
        self.quality_tree.setColumnCount(3)
        self.quality_tree.setHeaderLabels([
            self.t("analyzer_quality_item", "Item"),
            self.t("analyzer_kind", "Kind"),
            self.t("audit_detail", "Detail")])
        self.quality_tree.setFont(QFont("Consolas", 9))
        page_layout.addWidget(self.quality_tree, 1)

    def _build_runtime_tab(self):
        page, page_layout = self._make_page("analyzer_tab_runtime", "Runtime")
        cards = QtWidgets.QHBoxLayout()
        self.card_rt_steps = self._make_card(
            self.t("analyzer_rt_steps", "Trace steps"), "0")
        self.card_rt_exc = self._make_card(
            self.t("analyzer_rt_exceptions", "Exceptions"), "0")
        self.card_rt_danger = self._make_card(
            self.t("analyzer_rt_dous", "Dangerous"), "0")
        self.card_rt_anom = self._make_card(
            self.t("analyzer_rt_anomalies", "Anomalies"), "0")
        for card in (self.card_rt_steps, self.card_rt_exc,
                     self.card_rt_danger, self.card_rt_anom):
            cards.addWidget(card["frame"], 1)
        page_layout.addLayout(cards)
        self.runtime_tree = QtWidgets.QTreeWidget()
        self.runtime_tree.setAlternatingRowColors(True)
        self.runtime_tree.setColumnCount(3)
        self.runtime_tree.setHeaderLabels([
            self.t("analyzer_rt_item", "Item"),
            self.t("analyzer_kind", "Kind"),
            self.t("audit_detail", "Detail")])
        self.runtime_tree.setFont(QFont("Consolas", 9))
        page_layout.addWidget(self.runtime_tree, 1)

    def _fill_quality(self):
        """Fill the quality tree: standards, smells and dead code."""
        tree = self.quality_tree
        tree.clear()
        for result in self._state.get("results", []):
            style = result.get("style") or {}
            root = QtWidgets.QTreeWidgetItem(
                [result["name"], result.get("category", ""), ""])
            root.setForeground(0, QtGui.QColor("#8ab4f8"))
            tree.addTopLevelItem(root)
            self._quality_group(root, "analyzer_quality_standards",
                                style.get("standards", []),
                                lambda b: "%s (%s)" % (
                                    self.t(b["kind"], b["kind"]), b["rule"]),
                                lambda b: "line %s" % b["line"], "#f7c66d")
            self._quality_group(root, "analyzer_quality_duplicates",
                                style.get("duplicates", []),
                                lambda d: "%s line(s) x%s" % (
                                    len(d["block"].splitlines()),
                                    len(d["locations"])),
                                lambda d: "lines " + ", ".join(
                                    str(x) for x in d["locations"]), "#c586c0")
            self._quality_group(root, "analyzer_quality_long_funcs",
                                style.get("long_funcs", []),
                                lambda f: f["name"],
                                lambda f: "%s lines" % f["lines"], "#f7c66d")
            self._quality_group(root, "analyzer_quality_complex",
                                style.get("complex_blocks", []),
                                lambda f: f["name"],
                                lambda f: "complexity %s" % f["complexity"],
                                "#ff7b72")
            self._quality_group(root, "analyzer_quality_params",
                                style.get("many_params", []),
                                lambda f: f["name"],
                                lambda f: "%s params" % f["params"], "#f7c66d")
            self._quality_group(root, "analyzer_quality_nesting",
                                style.get("deep_nesting", []),
                                lambda f: f["name"],
                                lambda f: "depth %s" % f["depth"], "#ff7b72")
            self._quality_group(root, "analyzer_quality_dead",
                                style.get("dead_code", []),
                                lambda d: d["kind"],
                                lambda d: "line %s: %s" % (
                                    d["line"], d["detail"]), "#9be9a8")
            root.setExpanded(True)
        if tree.topLevelItemCount() == 0:
            tree.addTopLevelItem(self._placeholder(3))

    def _quality_group(self, root, key, values, name_fn, detail_fn, colour):
        """Add one labelled group node with its rows to the quality tree."""
        node = QtWidgets.QTreeWidgetItem(
            [self.t(key, key), str(len(values)), ""])
        node.setForeground(0, QtGui.QColor(colour))
        for value in values:
            child = QtWidgets.QTreeWidgetItem(
                [name_fn(value), "", detail_fn(value)])
            child.setForeground(0, QtGui.QColor(colour))
            node.addChild(child)
        root.addChild(node)

    def _fill_runtime(self):
        """Fill the runtime tree and the summary cards."""
        tree = self.runtime_tree
        tree.clear()
        agg = {"steps": 0, "exceptions": 0, "dangerous": 0, "anomalies": 0}
        for result in self._state.get("results", []):
            runtime = result.get("runtime") or {}
            summary = result.get("runtime_summary") or {}
            agg["steps"] += summary.get("steps", 0)
            agg["exceptions"] += summary.get("exceptions", 0)
            agg["dangerous"] += summary.get("dangerous", 0)
            agg["anomalies"] += summary.get("anomalies", 0)
            root = QtWidgets.QTreeWidgetItem(
                [result["name"], result.get("category", ""), ""])
            root.setForeground(0, QtGui.QColor("#8ab4f8"))
            tree.addTopLevelItem(root)
            self._runtime_group(root, "analyzer_rt_trace",
                                runtime.get("steps", []),
                                lambda s: s["kind"],
                                lambda s: "line %s: %s" % (s["line"], s["detail"]),
                                "#8ab4f8")
            self._runtime_group(root, "analyzer_rt_exceptions",
                                runtime.get("exceptions", []),
                                lambda e: e["kind"],
                                lambda e: "line %s: %s" % (e["line"], e["detail"]),
                                "#ff7b72")
            self._runtime_group(root, "analyzer_rt_dangerous",
                                runtime.get("dangerous", []),
                                lambda d: d["kind"],
                                lambda d: "line %s" % d["line"],
                                "#ff7b72")
            self._runtime_group(root, "analyzer_rt_stack",
                                runtime.get("stacktrace", []),
                                lambda t: "traceback",
                                lambda t: "%s frame(s)" % t["frames"],
                                "#c586c0")
            self._runtime_group(root, "analyzer_rt_timing",
                                runtime.get("timing", []),
                                lambda t: t["detail"],
                                lambda t: "cost %s (line %s)" % (
                                    t["cost"], t["line"]), "#f7c66d")
            self._runtime_group(root, "analyzer_rt_calls",
                                runtime.get("calls", []),
                                lambda c: c["name"],
                                lambda c: "x%s (lines %s)" % (
                                    c["count"],
                                    ", ".join(str(x) for x in c["lines"][:6])),
                                "#9be9a8")
            self._runtime_group(root, "analyzer_rt_anomalies",
                                runtime.get("anomalies", []),
                                lambda a: a["kind"],
                                lambda a: "line %s %s" % (
                                    a["line"], a["detail"]), "#ff9a9a")
            root.setExpanded(True)
        if tree.topLevelItemCount() == 0:
            tree.addTopLevelItem(self._placeholder(3))
        self.card_rt_steps["value"].setText(str(agg["steps"]))
        self.card_rt_exc["value"].setText(str(agg["exceptions"]))
        self.card_rt_exc["value"].setStyleSheet(
            """font-size: 22px; font-weight: bold; border:none; color:%s;"""
            % ("#ff9a9a" if agg["exceptions"] else "#9be9a8"))
        self.card_rt_danger["value"].setText(str(agg["dangerous"]))
        self.card_rt_danger["value"].setStyleSheet(
            """font-size: 22px; font-weight: bold; border:none; color:%s;"""
            % ("#ff7b72" if agg["dangerous"] else "#9be9a8"))
        self.card_rt_anom["value"].setText(str(agg["anomalies"]))
        self.card_rt_anom["value"].setStyleSheet(
            """font-size: 22px; font-weight: bold; border:none; color:%s;"""
            % ("#f7c66d" if agg["anomalies"] else "#9be9a8"))

    def _runtime_group(self, root, key, values, name_fn, detail_fn, colour):
        """Add one labelled group node with its rows to the runtime tree."""
        node = QtWidgets.QTreeWidgetItem(
            [self.t(key, key), str(len(values)), ""])
        node.setForeground(0, QtGui.QColor(colour))
        for value in values:
            child = QtWidgets.QTreeWidgetItem(
                [name_fn(value), "", detail_fn(value)])
            child.setForeground(0, QtGui.QColor(colour))
            node.addChild(child)
        root.addChild(node)

    def _build_metrics_tab(self):
        page, page_layout = self._make_page("analyzer_tab_metrics", "Metrics")
        cards = QtWidgets.QHBoxLayout()
        self.card_m_lines = self._make_card(
            self.t("analyzer_m_lines", "Lines of code"), "0")
        self.card_m_functions = self._make_card(
            self.t("analyzer_funcs", "Funcs"), "0")
        self.card_m_classes = self._make_card(
            self.t("analyzer_classes", "Classes"), "0")
        self.card_m_imports = self._make_card(
            self.t("analyzer_m_imports", "Imports"), "0")
        for card in (self.card_m_lines, self.card_m_functions,
                     self.card_m_classes, self.card_m_imports):
            cards.addWidget(card["frame"], 1)
        page_layout.addLayout(cards)

        cards2 = QtWidgets.QHBoxLayout()
        self.card_m_errors = self._make_card(
            self.t("analyzer_m_errors", "Errors"), "0")
        self.card_m_warnings = self._make_card(
            self.t("analyzer_m_warnings", "Warnings"), "0")
        self.card_m_cyclomatic = self._make_card(
            self.t("analyzer_m_cyclomatic", "Cyclomatic"), "0")
        self.card_m_logical = self._make_card(
            self.t("analyzer_m_logical", "Logical"), "0")
        for card in (self.card_m_errors, self.card_m_warnings,
                     self.card_m_cyclomatic, self.card_m_logical):
            cards2.addWidget(card["frame"], 1)
        page_layout.addLayout(cards2)

        self.metrics_tree = self._make_feed(
            self.t("analyzer_metrics", "Metrics"),
            [self.t("analyzer_m_metric", "Metric"),
             self.t("analyzer_m_value", "Value"),
             self.t("audit_where", "Script")])
        page_layout.addWidget(self.metrics_tree, 1)

    def _fill_metrics(self):
        """Fill the metrics tree and the summary cards."""
        tree = self.metrics_tree.tree
        tree.clear()
        totals = self._metrics_totals()
        metrics_rows = (
            ("analyzer_m_lines", "lines"),
            ("analyzer_funcs", "functions"),
            ("analyzer_classes", "classes"),
            ("analyzer_m_imports", "imports"),
            ("analyzer_m_errors", "errors"),
            ("analyzer_m_warnings", "warnings"),
            ("analyzer_m_cyclomatic", "cyclomatic"),
            ("analyzer_m_structural", "structural"),
            ("analyzer_m_logical", "logical"),
        )
        # One top-level row per metric with the total and every script as child.
        for key, field in metrics_rows:
            node = QtWidgets.QTreeWidgetItem(
                [self.t(key, key), str(totals.get(field, 0)), ""])
            node.setForeground(0, QtGui.QColor("#8ab4f8"))
            for result in self._state.get("results", []):
                value = (result.get("metrics_engine") or {}).get(field, 0)
                node.addChild(QtWidgets.QTreeWidgetItem(
                    [result["name"], str(value), result["name"]]))
            tree.addTopLevelItem(node)
        if tree.topLevelItemCount() == 0:
            tree.addTopLevelItem(self._placeholder(3))

        self.card_m_lines["value"].setText(str(totals.get("lines", 0)))
        self.card_m_functions["value"].setText(str(totals.get("functions", 0)))
        self.card_m_classes["value"].setText(str(totals.get("classes", 0)))
        self.card_m_imports["value"].setText(str(totals.get("imports", 0)))
        self.card_m_errors["value"].setText(str(totals.get("errors", 0)))
        self.card_m_errors["value"].setStyleSheet(
            """font-size: 22px; font-weight: bold; border:none; color:%s;"""
            % ("#ff7b72" if totals.get("errors") else "#9be9a8"))
        self.card_m_warnings["value"].setText(str(totals.get("warnings", 0)))
        self.card_m_warnings["value"].setStyleSheet(
            """font-size: 22px; font-weight: bold; border:none; color:%s;"""
            % ("#f7c66d" if totals.get("warnings") else "#9be9a8"))
        self.card_m_cyclomatic["value"].setText(str(totals.get("cyclomatic", 0)))
        self.card_m_logical["value"].setText(str(totals.get("logical", 0)))

    def _build_history_tab(self):
        page, page_layout = self._make_page("analyzer_tab_history", "Diff")
        cards = QtWidgets.QHBoxLayout()
        self.card_hist_prev = self._make_card(
            self.t("analyzer_hist_prev", "Changes vs previous"), "0")
        self.card_hist_snap = self._make_card(
            self.t("analyzer_hist_snapshot", "Changes vs snapshot"), "0")
        self.card_hist_ref = self._make_card(
            self.t("analyzer_hist_refactors", "Refactors"), "0")
        self.card_hist_total = self._make_card(
            self.t("analyzer_hist_total", "Total changes"), "0")
        for card in (self.card_hist_prev, self.card_hist_snap,
                     self.card_hist_ref, self.card_hist_total):
            cards.addWidget(card["frame"], 1)
        page_layout.addLayout(cards)

        actions = QtWidgets.QHBoxLayout()
        self.btn_hist_diff = QtWidgets.QPushButton(
            self.t("analyzer_hist_open_diff", "Compare in Diff tab"))
        self.btn_hist_diff.setToolTip(
            self.t("analyzer_hist_open_diff_hint",
                   "Open this script and its previous version in the Diff tab."))
        self.btn_hist_diff.clicked.connect(self._open_in_diff)
        actions.addWidget(self.btn_hist_diff)
        actions.addStretch()
        page_layout.addLayout(actions)

        self.history_tree = self._make_feed(
            self.t("analyzer_history", "Change detection"),
            [self.t("analyzer_hist_item", "Item"),
             self.t("analyzer_kind", "Kind"),
             self.t("audit_detail", "Detail"),
             self.t("audit_where", "Script")])
        page_layout.addWidget(self.history_tree, 1)

    def _build_config_tab(self):
        page, page_layout = self._make_page("analyzer_tab_config", "Config")
        cards = QtWidgets.QHBoxLayout()
        self.card_cfg_files = self._make_card(
            self.t("analyzer_cfg_files", "Config files"), "0")
        self.card_cfg_env = self._make_card(
            self.t("analyzer_cfg_env", "Env vars"), "0")
        self.card_cfg_path = self._make_card(
            self.t("analyzer_cfg_path", "PATH entries"), "0")
        self.card_cfg_interp = self._make_card(
            self.t("analyzer_cfg_interpreters", "Interpreters"), "0")
        for card in (self.card_cfg_files, self.card_cfg_env,
                     self.card_cfg_path, self.card_cfg_interp):
            cards.addWidget(card["frame"], 1)
        page_layout.addLayout(cards)

        self.config_tree = self._make_feed(
            self.t("analyzer_config", "Configuration analysis"),
            [self.t("analyzer_cfg_item", "Item"),
             self.t("analyzer_kind", "Kind"),
             self.t("audit_detail", "Detail")])
        page_layout.addWidget(self.config_tree, 1)

    def _fill_history(self):
        """Fill the diff/history tree and the summary cards."""
        tree = self.history_tree.tree
        tree.clear()
        agg_prev = agg_snap = agg_ref = 0
        for result in self._state.get("results", []):
            history = result.get("history") or {}
            summary = result.get("history_summary") or {}
            agg_prev += summary.get("previous_changes", 0)
            agg_snap += summary.get("snapshot_changes", 0)
            agg_ref += summary.get("refactors", 0)
            root = QtWidgets.QTreeWidgetItem(
                [result["name"], result.get("category", ""), "", ""])
            root.setForeground(0, QtGui.QColor("#8ab4f8"))
            tree.addTopLevelItem(root)
            self._history_source(root, "analyzer_hist_previous",
                                 history.get("previous") or {},
                                 history.get("has_previous", False),
                                 history.get("prev_label", ""), "#8ab4f8")
            self._history_source(root, "analyzer_hist_snapshot_group",
                                 history.get("snapshot") or {},
                                 history.get("has_snapshot", False),
                                 history.get("snap_label", ""), "#c586c0")
            refactors = history.get("refactors") or []
            ref_node = QtWidgets.QTreeWidgetItem(
                [self.t("analyzer_hist_refactors", "Refactors"),
                 str(len(refactors)), "", ""])
            ref_node.setForeground(0, QtGui.QColor("#f7c66d"))
            for ref in refactors:
                child = QtWidgets.QTreeWidgetItem(
                    [ref.get("name", ""), ref.get("kind", ""),
                     ref.get("detail", ""), ""])
                child.setForeground(1, QtGui.QColor("#f7c66d"))
                ref_node.addChild(child)
            root.addChild(ref_node)
            root.setExpanded(True)
        if tree.topLevelItemCount() == 0:
            tree.addTopLevelItem(self._placeholder(4))

        self.card_hist_prev["value"].setText(str(agg_prev))
        self.card_hist_snap["value"].setText(str(agg_snap))
        self.card_hist_ref["value"].setText(str(agg_ref))
        self.card_hist_total["value"].setText(str(agg_prev + agg_snap + agg_ref))
        self.card_hist_total["value"].setStyleSheet(
            """font-size: 22px; font-weight: bold; border:none; color:%s;"""
            % ("#f7c66d" if (agg_prev + agg_snap + agg_ref) else "#9be9a8"))

    def _history_source(self, root, key, changes, present, label, colour):
        """Add one change-set group (previous revision / snapshot) to the tree."""
        categories = (("functions", "analyzer_hist_functions", "Functions"),
                      ("classes", "analyzer_hist_classes", "Classes"),
                      ("imports", "analyzer_hist_imports", "Imports"),
                      ("logic", "analyzer_hist_logic", "Logic"))
        total = sum(len(changes.get(c, [])) for c, _k, _f in categories)
        node = QtWidgets.QTreeWidgetItem(
            [self.t(key, key), str(total), label if present else
             self.t("analyzer_hist_none", "no comparison"), ""])
        node.setForeground(0, QtGui.QColor(colour))
        for cat, cat_key, cat_fallback in categories:
            rows = changes.get(cat, [])
            if not rows:
                continue
            cat_node = QtWidgets.QTreeWidgetItem(
                [self.t(cat_key, cat_fallback), str(len(rows)), "", ""])
            cat_node.setForeground(0, QtGui.QColor(colour))
            for row in rows:
                child = QtWidgets.QTreeWidgetItem(
                    [row.get("name", ""), row.get("kind", ""),
                     row.get("detail", ""), ""])
                child.setForeground(1, QtGui.QColor(colour))
                cat_node.addChild(child)
            node.addChild(cat_node)
        root.addChild(node)

    def _open_in_diff(self):
        """Send the selected script's previous version to the Diff tab."""
        script = self._selected_script()
        window = self.window
        diff_tab = getattr(window, "diff_tab", None)
        if script is None or diff_tab is None:
            self._announce(self.t("analyzer_hist_diff_unavailable",
                                  "Diff tab unavailable"))
            return
        revisions = script.get("history", []) or []
        try:
            if revisions:
                diff_tab.set_sources(
                    diff_tab._entry_to_source(revisions[-1]),
                    diff_tab._entry_to_source(script))
            else:
                diff_tab._right = diff_tab._entry_to_source(script)
                diff_tab.recompute()
            window.tabs.setCurrentWidget(diff_tab)
            self._announce("%s: %s" % (
                self.t("analyzer_hist_open_diff", "Compare in Diff tab"),
                script.get("name", "")))
        except Exception:
            pass

    def _fill_config(self):
        """Fill the configuration tree and the summary cards."""
        tree = self.config_tree.tree
        tree.clear()
        files = env_vars = path_rows = interpreters = 0
        groups = (
            ("config_files", "analyzer_cfg_files", "Config files"),
            ("environment", "analyzer_cfg_runtime", "Runtime environment"),
            ("env_vars", "analyzer_cfg_env", "Env vars"),
            ("path", "analyzer_cfg_path", "PATH entries"),
            ("interpreters", "analyzer_cfg_interpreters", "Interpreters"),
        )
        for result in self._state.get("results", []):
            config = result.get("configuration") or {}
            summary = config.get("summary") or {}
            files += summary.get("config_files", 0)
            env_vars += summary.get("env_vars", 0)
            path_rows += summary.get("path", 0)
            interpreters += summary.get("interpreters", 0)
            root = QtWidgets.QTreeWidgetItem(
                [result["name"], result.get("category", ""), ""])
            root.setForeground(0, QtGui.QColor("#8ab4f8"))
            tree.addTopLevelItem(root)
            for field, key, fallback in groups:
                rows = config.get(field) or []
                node = QtWidgets.QTreeWidgetItem(
                    [self.t(key, fallback), str(len(rows)), ""])
                node.setForeground(0, QtGui.QColor("#c586c0"))
                for row in rows:
                    detail = row.get("detail", "")
                    child = QtWidgets.QTreeWidgetItem(
                        [row.get("name", ""), row.get("kind", ""), detail])
                    if detail in ("missing", "unreadable"):
                        child.setForeground(2, QtGui.QColor("#ff7b72"))
                    node.addChild(child)
                root.addChild(node)
            root.setExpanded(True)
        if tree.topLevelItemCount() == 0:
            tree.addTopLevelItem(self._placeholder(3))
        self.card_cfg_files["value"].setText(str(files))
        self.card_cfg_env["value"].setText(str(env_vars))
        self.card_cfg_path["value"].setText(str(path_rows))
        self.card_cfg_interp["value"].setText(str(interpreters))

    def _build_operations_tab(self):
        page, page_layout = self._make_page("analyzer_tab_operations",
                                            "Operations")
        cards = QtWidgets.QHBoxLayout()
        self.card_op_scope = self._make_card(
            self.t("analyzer_op_scope", "Scope"), "-")
        self.card_op_items = self._make_card(
            self.t("analyzer_op_items", "Findings"), "0")
        self.card_op_risk = self._make_card(
            self.t("analyzer_op_risk", "Risk"), "-")
        self.card_op_time = self._make_card(
            self.t("analyzer_op_time", "Generated"), "-")
        for card in (self.card_op_scope, self.card_op_items,
                     self.card_op_risk, self.card_op_time):
            cards.addWidget(card["frame"], 1)
        page_layout.addLayout(cards)

        self.operations_tree = self._make_feed(
            self.t("analyzer_operations_result", "Operation result"),
            [self.t("analyzer_op_item", "Item"),
             self.t("analyzer_kind", "Kind"),
             self.t("audit_detail", "Detail")])
        page_layout.addWidget(self.operations_tree, 1)

    def _fill_operations(self):
        """Repaint the operations tab from the last operation result."""
        tree = self.operations_tree.tree
        tree.clear()
        result = self._state.get("last_operation")
        if not result:
            tree.addTopLevelItem(self._placeholder(3))
            self.card_op_scope["value"].setText("-")
            self.card_op_items["value"].setText("0")
            self.card_op_risk["value"].setText("-")
            self.card_op_time["value"].setText("-")
            return
        root = QtWidgets.QTreeWidgetItem([
            result.get("title", ""), result.get("operation", ""),
            result.get("scope", "")])
        root.setForeground(0, QtGui.QColor("#8ab4f8"))
        tree.addTopLevelItem(root)
        for group in result.get("groups", []):
            node = QtWidgets.QTreeWidgetItem(
                [group.get("title", ""), str(len(group.get("rows", []))), ""])
            node.setForeground(0, QtGui.QColor(group.get("colour", "#c586c0")))
            for row in group.get("rows", []):
                if isinstance(row, dict):
                    name = row.get("name", "")
                    kind = row.get("kind", "")
                    detail = row.get("detail", "")
                else:
                    name, kind, detail = (list(row) + ["", "", ""])[:3]
                child = QtWidgets.QTreeWidgetItem([name, kind, detail])
                child.setForeground(0, QtGui.QColor(group.get("colour", "#dfe7f5")))
                node.addChild(child)
            root.addChild(node)
        root.setExpanded(True)
        self.card_op_scope["value"].setText(result.get("scope", "-"))
        self.card_op_items["value"].setText(str(result.get("items", 0)))
        self.card_op_risk["value"].setText(result.get("risk", "-"))
        self.card_op_time["value"].setText(result.get("generated", "-"))

    # ------------------------------------------------------- operations
    # A menu of focused analysis actions. Each builds a structured result that
    # is rendered in the Operations sub-tab, announced on the audit bus and
    # available to the export writers below.

    def _state_script(self):
        """The script the operations act on (selected, else the first one)."""
        script = self._selected_script()
        if script is not None:
            return script
        scripts = self._scripts()
        return scripts[0] if scripts else None

    def _current_result(self, script):
        """Reuse the analysed result for `script` when it is already cached."""
        script_id = script.get("id")
        for result in self._state.get("results", []):
            if result.get("id") == script_id:
                return result
        return None

    def run_operation(self, key):
        """Run one analysis operation and show its result."""
        script = self._state_script()
        if script is None:
            self.status.setText(
                self.t("analyzer_op_noselect", "Select a script first."))
            self._announce("operation skipped: no script")
            return None
        result = self._analyze_script(script)
        result["id"] = script.get("id")
        builder = getattr(self, "_op_" + key, None)
        if builder is None:
            return None
        operation = builder(script, result)
        operation["generated"] = _now()
        operation["scope"] = operation.get("scope") or script.get("name", "")
        operation["items"] = sum(len(g.get("rows", []))
                                 for g in operation.get("groups", []))
        self._state["last_operation"] = operation
        self._state["last_operation_key"] = key
        self._focus_subtab("analyzer_tab_operations")
        self._announce("%s: %s (%s item(s))" % (
            operation["title"], script.get("name", ""), operation["items"]))
        return operation

    def _focus_subtab(self, key):
        """Switch the analyzer to a sub-tab by key, if it exists."""
        for index, (sub_key, _fallback) in self._subtab_keys.items():
            if sub_key == key:
                self.subtabs.setCurrentIndex(index)
                return index
        return -1

    def _op_full(self, script, result):
        """Full script analysis: one row group per analysis dimension."""
        code = script.get("code", "") or ""
        metrics = result.get("metrics") or {}
        style = result.get("style") or {}
        groups = [
            {"title": self.t("analyzer_op_overview", "Overview"),
             "colour": "#8ab4f8", "rows": [
                 {"name": self.t("analyzer_lines", "Lines"),
                  "kind": "count", "detail": str(metrics.get("total_lines", 0))},
                 {"name": self.t("analyzer_funcs", "Funcs"),
                  "kind": "count", "detail": str(len(result.get("functions", [])))},
                 {"name": self.t("analyzer_classes", "Classes"),
                  "kind": "count", "detail": str(len(result.get("classes", [])))},
                 {"name": self.t("analyzer_score", "Score"),
                  "kind": "score", "detail": str(result.get("score", 0))}]},
            {"title": self.t("analyzer_op_structure", "Structure"),
             "colour": "#c586c0",
             "rows": [{"name": f.get("name", ""), "kind": f.get("type", "func"),
                       "detail": "line %s" % f.get("line", "")}
                      for f in (result.get("ast") or {}).get("functions", [])][:200]},
            {"title": self.t("analyzer_issues", "Issues"), "colour": "#f7c66d",
             "rows": [{"name": i.get("kind", ""), "kind": i.get("severity", ""),
                       "detail": i.get("detail", "")}
                      for i in result.get("issues", [])][:200]},
        ]
        return {"operation": "full", "title": result["name"],
                "scope": script.get("name", ""),
                "risk": self._risk_label(result), "groups": groups}

    def _op_fragment(self, script, result):
        """Analyse a selected fragment of the script (editor selection)."""
        fragment, source = self._selected_fragment(script)
        if not fragment:
            return {"operation": "fragment",
                    "title": self.t("analyzer_op_fragment", "Fragment"),
                    "scope": script.get("name", ""), "risk": "-",
                    "groups": [{"title": self.t("analyzer_op_fragment",
                                                "Fragment"), "colour": "#f7c66d",
                                "rows": [{"name": self.t("analyzer_op_no_fragment",
                                                         "no selection"),
                                          "kind": "info",
                                          "detail": self.t(
                                              "analyzer_op_no_fragment_hint",
                                              "Select code in the editor first.")}]}]}
        f_result = self._analyze_script({"code": fragment,
                                         "category": script.get("category", ""),
                                         "name": script.get("name", "")})
        f_metrics = f_result.get("metrics") or {}
        groups = [
            {"title": self.t("analyzer_op_stats", "Statistics"),
             "colour": "#8ab4f8", "rows": [
                 {"name": self.t("analyzer_lines", "Lines"),
                  "kind": "count",
                  "detail": str(f_metrics.get("total_lines", 0))},
                 {"name": self.t("analyzer_score", "Score"), "kind": "score",
                  "detail": str(f_result.get("score", 0))}]},
            {"title": self.t("analyzer_issues", "Issues"), "colour": "#f7c66d",
             "rows": [{"name": i.get("kind", ""), "kind": i.get("severity", ""),
                       "detail": i.get("detail", "")}
                      for i in f_result.get("issues", [])]},
        ]
        return {"operation": "fragment", "title": result["name"],
                "scope": source, "risk": self._risk_label(f_result),
                "groups": groups}

    def _op_function(self, script, result):
        """Function-by-function analysis: complexity, params, issues."""
        functions = (result.get("complexity") or [])
        functions = functions or (result.get("ast") or {}).get("functions", [])
        rows = []
        for f in functions:
            name = f.get("name", "")
            detail = "lines=%s params=%s complexity=%s" % (
                f.get("lines", f.get("end", 0)), f.get("params", ""),
                f.get("complexity", ""))
            kind = "high" if f.get("complexity", 0) > 10 else "function"
            rows.append({"name": name, "kind": kind, "detail": detail})
        return {"operation": "function", "title": result["name"],
                "scope": "%s function(s)" % len(rows),
                "risk": self._risk_label(result),
                "groups": [{"title": self.t("analyzer_op_functions", "Functions"),
                            "colour": "#8ab4f8", "rows": rows}]}

    def _op_class(self, script, result):
        """Class analysis: members, methods and line spans."""
        ast = result.get("ast") or {}
        rows = []
        for c in ast.get("classes", []):
            methods = [f for f in ast.get("functions", [])
                       if f.get("line", 0) >= c.get("line", 0)
                       and f.get("line", 0) <= c.get("end", 10 ** 9)]
            rows.append({"name": c.get("name", ""), "kind": "class",
                         "detail": "%s method(s), line %s" % (
                             len(methods), c.get("line", ""))})
        if not rows:
            rows = [{"name": self.t("analyzer_op_none", "none"),
                     "kind": "info", "detail": ""}]
        return {"operation": "class", "title": result["name"],
                "scope": "%s class(es)" % len(result.get("classes", [])),
                "risk": self._risk_label(result),
                "groups": [{"title": self.t("analyzer_op_classes", "Classes"),
                            "colour": "#c586c0", "rows": rows}]}

    def _op_imports(self, script, result):
        """Import analysis: internal, external and unused imports."""
        internal = result.get("internal_imports", [])
        external = result.get("external_imports", [])
        unused = []
        for issue in result.get("issues", []):
            if issue.get("kind") == "analyzer_issue_unused_import":
                unused.append(issue)
        groups = [
            {"title": self.t("analyzer_kind_internal", "internal"),
             "colour": "#9be9a8",
             "rows": [{"name": n, "kind": "internal", "detail": ""}
                      for n in internal]},
            {"title": self.t("analyzer_kind_external", "external"),
             "colour": "#8ab4f8",
             "rows": [{"name": n, "kind": "external", "detail": ""}
                      for n in external]},
            {"title": self.t("analyzer_dep_unused", "Unused"),
             "colour": "#f7c66d",
             "rows": [{"name": self.t(i.get("kind", ""), i.get("kind", "")),
                       "kind": "unused", "detail": i.get("detail", "")}
                      for i in unused]},
        ]
        return {"operation": "imports", "title": result["name"],
                "scope": "%s import(s)" % len(result.get("all_imports", [])),
                "risk": self._risk_label(result), "groups": groups}

    def _op_dependencies(self, script, result):
        """Dependency analysis: missing, unused, conflicts and system needs."""
        dep = result.get("dependencies") or {}
        groups = [
            {"title": self.t("analyzer_dep_missing", "Missing"),
             "colour": "#ff7b72",
             "rows": [{"name": r.get("name", ""), "kind": r.get("kind", ""),
                       "detail": r.get("detail", "")}
                      for r in dep.get("missing", [])]},
            {"title": self.t("analyzer_dep_unused", "Unused"),
             "colour": "#f7c66d",
             "rows": [{"name": r.get("name", ""), "kind": r.get("kind", ""),
                       "detail": r.get("detail", "")}
                      for r in dep.get("unused", [])]},
            {"title": self.t("analyzer_dep_conflicts", "Conflicts"),
             "colour": "#ff9a9a",
             "rows": [{"name": r.get("name", ""), "kind": r.get("kind", ""),
                       "detail": r.get("detail", "")}
                      for r in dep.get("conflicts", [])]},
            {"title": self.t("analyzer_dep_system", "System"),
             "colour": "#c586c0",
             "rows": [{"name": r.get("name", ""), "kind": r.get("kind", ""),
                       "detail": r.get("detail", "")}
                      for r in dep.get("system", [])]},
            {"title": self.t("analyzer_dep_interpreters", "Interpreters"),
             "colour": "#8ab4f8",
             "rows": [{"name": r.get("kind", ""), "kind": "interpreter",
                       "detail": r.get("detail", "")}
                      for r in result.get("interpreters", [])]},
        ]
        summary = result.get("dependency_summary") or {}
        return {"operation": "dependencies", "title": result["name"],
                "scope": "%s import(s), %s missing" % (
                    summary.get("imports", 0), summary.get("missing", 0)),
                "risk": self._risk_label(result), "groups": groups}

    def _op_errors(self, script, result):
        """Error analysis: syntax, type and indentation problems."""
        syntax = result.get("syntax") or {}
        rows = []
        for e in syntax.get("errors", []):
            rows.append({"name": self.t(e.get("kind", ""), e.get("kind", "")),
                         "kind": e.get("severity", "high"),
                         "detail": "line %s: %s" % (
                             e.get("line", 0), e.get("detail", ""))})
        for e in syntax.get("unclosed", []):
            rows.append({"name": self.t(e.get("kind", ""), e.get("kind", "")),
                         "kind": "high",
                         "detail": "line %s" % e.get("line", 0)})
        for e in syntax.get("indent_issues", []):
            rows.append({"name": self.t(e.get("kind", ""), e.get("kind", "")),
                         "kind": "low",
                         "detail": "line %s" % e.get("line", 0)})
        for i in result.get("issues", []):
            if i.get("severity") in ("high", "medium"):
                rows.append({"name": self.t(i.get("kind", ""), i.get("kind", "")),
                             "kind": i.get("severity", ""),
                             "detail": i.get("detail", "")})
        return {"operation": "errors", "title": result["name"],
                "scope": "%s error/finding(s)" % len(rows),
                "risk": self._risk_label(result),
                "groups": [{"title": self.t("analyzer_op_errors", "Errors"),
                            "colour": "#ff7b72", "rows": rows}]}

    def _op_security(self, script, result):
        """Security analysis: dangerous constructs flagged in the code."""
        runtime = result.get("runtime") or {}
        code = script.get("code", "") or ""
        groups = [
            {"title": self.t("analyzer_rt_dangerous", "Dangerous paths"),
             "colour": "#ff7b72",
             "rows": [{"name": self.t(d.get("kind", ""), d.get("kind", "")),
                       "kind": d.get("kind", ""),
                       "detail": "line %s" % d.get("line", 0)}
                      for d in runtime.get("dangerous", [])]},
        ]
        # Static security signatures searched directly in the source.
        signatures = (
            ("eval", "analyzer_rt_eval"), ("exec", "analyzer_rt_exec"),
            ("os.system", "analyzer_rt_os_system"),
            ("subprocess", "analyzer_rt_subprocess"),
            ("pickle", "analyzer_rt_pickle"),
            ("shell=True", "analyzer_rt_shell"),
            ("shutil.rmtree", "analyzer_rt_rmtree"),
        )
        static_rows = []
        for token, key in signatures:
            if token in code:
                static_rows.append({
                    "name": self.t(key, key), "kind": "static",
                    "detail": "%s occurrence(s)" % code.count(token)})
        groups.append({"title": self.t("analyzer_op_security_static",
                                       "Dangerous constructs"),
                       "colour": "#ff9a9a", "rows": static_rows})
        risk = self._risk_label(result, security=True)
        return {"operation": "security", "title": result["name"],
                "scope": result.get("name", ""), "risk": risk, "groups": groups}

    def _op_runtime(self, script, result):
        """Runtime analysis: traces, exceptions, timing and anomalies."""
        runtime = result.get("runtime") or {}
        groups = [
            {"title": self.t("analyzer_rt_trace", "Execution trace"),
             "colour": "#8ab4f8",
             "rows": [{"name": s.get("kind", ""), "kind": s.get("kind", ""),
                       "detail": "line %s: %s" % (
                           s.get("line", 0), s.get("detail", ""))}
                      for s in runtime.get("steps", [])][:300]},
            {"title": self.t("analyzer_rt_exceptions", "Exceptions"),
             "colour": "#ff7b72",
             "rows": [{"name": e.get("kind", ""), "kind": e.get("kind", ""),
                       "detail": "line %s: %s" % (
                           e.get("line", 0), e.get("detail", ""))}
                      for e in runtime.get("exceptions", [])]},
            {"title": self.t("analyzer_rt_timing", "Execution time"),
             "colour": "#f7c66d",
             "rows": [{"name": t.get("detail", ""), "kind": "timing",
                       "detail": "cost %s (line %s)" % (
                           t.get("cost", ""), t.get("line", ""))}
                      for t in runtime.get("timing", [])]},
            {"title": self.t("analyzer_rt_anomalies", "Anomalies"),
             "colour": "#ff9a9a",
             "rows": [{"name": a.get("kind", ""), "kind": a.get("kind", ""),
                       "detail": "line %s %s" % (
                           a.get("line", 0), a.get("detail", ""))}
                      for a in runtime.get("anomalies", [])]},
        ]
        return {"operation": "runtime", "title": result["name"],
                "scope": "%s step(s)" % len(runtime.get("steps", [])),
                "risk": self._risk_label(result), "groups": groups}

    def _op_changes(self, script, result):
        """Change analysis: differences vs the previous revision/snapshot."""
        history = result.get("history") or {}
        groups = []
        for label_key, label_fallback, changes in (
                ("analyzer_hist_previous", "Previous version",
                 history.get("previous") or {}),
                ("analyzer_hist_snapshot_group", "Snapshot",
                 history.get("snapshot") or {})):
            rows = []
            for cat, cat_fallback in (("functions", "Functions"),
                                      ("classes", "Classes"),
                                      ("imports", "Imports"),
                                      ("logic", "Logic")):
                for change in changes.get(cat, []):
                    rows.append({"name": change.get("name", ""),
                                 "kind": "%s/%s" % (
                                     self.t("analyzer_hist_" + cat,
                                            cat_fallback).lower(),
                                     change.get("kind", "")),
                                 "detail": change.get("detail", "")})
            groups.append({"title": self.t(label_key, label_fallback),
                           "colour": "#8ab4f8", "rows": rows})
        groups.append({
            "title": self.t("analyzer_hist_refactors", "Refactors"),
            "colour": "#f7c66d",
            "rows": [{"name": r.get("name", ""), "kind": r.get("kind", ""),
                      "detail": r.get("detail", "")}
                     for r in history.get("refactors", [])]})
        summary = result.get("history_summary") or {}
        return {"operation": "changes", "title": result["name"],
                "scope": "%s change(s)" % summary.get("total_changes", 0),
                "risk": self._risk_label(result), "groups": groups}

    def _op_report(self, script, result):
        """Generate a per-script analysis report and show its key sections."""
        report = self.build_report("txt")
        lines = report.splitlines()
        rows = [{"name": str(index + 1), "kind": "line", "detail": line}
                for index, line in enumerate(lines[:400])]
        return {"operation": "report", "title": result["name"],
                "scope": "%s line(s)" % len(lines), "risk": "-",
                "groups": [{"title": self.t("analyzer_op_report",
                                            "Analysis report"),
                            "colour": "#9be9a8", "rows": rows}],
                "report": report}

    def _selected_fragment(self, script):
        """Return (fragment, source-label): the editor selection or the code."""
        editor = getattr(self.window, "script_form", None)
        code_edit = getattr(editor, "code_edit", None)
        if code_edit is not None:
            try:
                cursor = code_edit.textCursor()
                if cursor.hasSelection():
                    return cursor.selectedText().replace("\u2029", "\n"), \
                        self.t("analyzer_op_selected", "editor selection")
            except Exception:
                pass
        # Fall back to the whole script when there is no live selection.
        return script.get("code", "") or "", \
            self.t("analyzer_op_whole_script", "whole script")

    @staticmethod
    def _risk_label(result, security=False):
        """A coarse risk label derived from issues and dangerous findings."""
        issues = result.get("issues", [])
        high = sum(1 for i in issues if i.get("severity") == "high")
        medium = sum(1 for i in issues if i.get("severity") == "medium")
        dangerous = len((result.get("runtime") or {}).get("dangerous", []))
        score = high * 3 + medium + dangerous * 2
        if score >= 6:
            return "high"
        if score >= 2:
            return "medium"
        return "low"

    def _build_architecture_tab(self):
        page, page_layout = self._make_page("analyzer_tab_architecture",
                                            "Architecture")
        cards = QtWidgets.QHBoxLayout()
        self.card_arch_standards = self._make_card(
            self.t("analyzer_arch_standards", "Standards"), "0")
        self.card_arch_frontends = self._make_card(
            self.t("analyzer_arch_frontends", "Front-ends"), "0")
        self.card_arch_integrations = self._make_card(
            self.t("analyzer_arch_integrations", "Integrations"), "0")
        self.card_arch_plugins = self._make_card(
            self.t("analyzer_arch_plugins", "Plugins"), "0")
        for card in (self.card_arch_standards, self.card_arch_frontends,
                     self.card_arch_integrations, self.card_arch_plugins):
            cards.addWidget(card["frame"], 1)
        page_layout.addLayout(cards)

        actions = QtWidgets.QHBoxLayout()
        self.btn_arch_api = QtWidgets.QPushButton(
            self.t("analyzer_arch_api", "API: describe()"))
        self.btn_arch_api.clicked.connect(self._show_architecture_api)
        actions.addWidget(self.btn_arch_api)
        self.btn_arch_cli = QtWidgets.QPushButton(
            self.t("analyzer_arch_cli", "CLI: capabilities"))
        self.btn_arch_cli.clicked.connect(self._show_architecture_cli)
        actions.addWidget(self.btn_arch_cli)
        actions.addStretch()
        page_layout.addLayout(actions)

        self.architecture_tree = self._make_feed(
            self.t("analyzer_architecture", "Architecture"),
            [self.t("analyzer_arch_item", "Item"),
             self.t("analyzer_kind", "Kind"),
             self.t("audit_detail", "Detail")])
        page_layout.addWidget(self.architecture_tree, 1)

    def _build_premium_tab(self):
        page, page_layout = self._make_page("analyzer_tab_premium",
                                            "Premium / Automation")
        cards = QtWidgets.QHBoxLayout()
        self.card_prem_changes = self._make_card(
            self.t("analyzer_prem_changes", "Watched changes"), "0")
        self.card_prem_predict = self._make_card(
            self.t("analyzer_prem_predict", "Predicted risks"), "0")
        self.card_prem_risk = self._make_card(
            self.t("analyzer_prem_risk_col", "Risk level"), "-")
        self.card_prem_project = self._make_card(
            self.t("analyzer_prem_project_col", "Project score"), "-")
        for card in (self.card_prem_changes, self.card_prem_predict,
                     self.card_prem_risk, self.card_prem_project):
            cards.addWidget(card["frame"], 1)
        page_layout.addLayout(cards)

        actions = QtWidgets.QHBoxLayout()
        self.premium_buttons = {}
        for key, label_key, fallback in ANALYZER_PREMIUM:
            button = QtWidgets.QPushButton(self.t(label_key, fallback))
            button.clicked.connect(
                lambda _checked=False, k=key: self.run_premium(k))
            self.premium_buttons[key] = button
            actions.addWidget(button)
        actions.addStretch()
        page_layout.addLayout(actions)

        row = QtWidgets.QHBoxLayout()
        self.chk_premium_auto = QtWidgets.QCheckBox(
            self.t("analyzer_prem_auto_toggle",
                   "Auto-analyse after every change"))
        self.chk_premium_auto.toggled.connect(self._toggle_auto_analysis)
        row.addWidget(self.chk_premium_auto)
        self.chk_premium_watch = QtWidgets.QCheckBox(
            self.t("analyzer_prem_watch", "Real-time (watcher)"))
        self.chk_premium_watch.toggled.connect(self._toggle_realtime)
        row.addWidget(self.chk_premium_watch)
        row.addStretch()
        page_layout.addLayout(row)

        self.premium_tree = self._make_feed(
            self.t("analyzer_premium", "Premium analysis"),
            [self.t("analyzer_prem_item", "Item"),
             self.t("analyzer_kind", "Kind"),
             self.t("audit_detail", "Detail")])
        page_layout.addWidget(self.premium_tree, 1)

    # ------------------------------------------------------- architecture
    def _fill_architecture(self):
        """Render the architectural contract: standards + integration."""
        tree = self.architecture_tree.tree
        tree.clear()
        root = QtWidgets.QTreeWidgetItem(
            [self.t("analyzer_arch_standards", "Standards"), "", ""])
        root.setForeground(0, QtGui.QColor("#8ab4f8"))
        tree.addTopLevelItem(root)
        for _key, label_key, fallback, detail in ANALYZER_ARCHITECTURE:
            item = QtWidgets.QTreeWidgetItem([
                self.t(label_key, fallback),
                self.t("analyzer_arch_standard", "standard"),
                self.t("analyzer_arch_detail_" + _key, detail)])
            item.setForeground(0, QtGui.QColor("#9be9a8"))
            root.addChild(item)
        # Integration section: what the tab talks to.
        integration = QtWidgets.QTreeWidgetItem(
            [self.t("analyzer_arch_integrations", "Integrations"), "", ""])
        integration.setForeground(0, QtGui.QColor("#c586c0"))
        tree.addTopLevelItem(integration)
        for key, label_key, fallback in ANALYZER_INTEGRATIONS:
            available = self._integration_available(key)
            item = QtWidgets.QTreeWidgetItem([
                self.t(label_key, fallback),
                self.t("analyzer_arch_yes" if available else "analyzer_arch_no",
                       "available" if available else "unavailable"),
                self._integration_detail(key)])
            item.setForeground(1, QtGui.QColor(
                "#9be9a8" if available else "#ff9a9a"))
            integration.addChild(item)
        # Front-ends section.
        frontends = QtWidgets.QTreeWidgetItem(
            [self.t("analyzer_arch_frontends", "Front-ends"), "", ""])
        frontends.setForeground(0, QtGui.QColor("#f7c66d"))
        tree.addTopLevelItem(frontends)
        for name, detail in (
                ("GUI", self.t("analyzer_arch_gui_detail",
                               "This tab: menus, sub-tabs, exports")),
                ("API", "analyzer_api(tab) / analyzer_describe()"),
                ("CLI", "analyzer_cli([...]) -> text/json/log")):
            frontends.addChild(QtWidgets.QTreeWidgetItem([name, "front-end", detail]))
        root.setExpanded(True)
        integration.setExpanded(True)
        frontends.setExpanded(True)
        self.card_arch_standards["value"].setText(
            str(len(ANALYZER_ARCHITECTURE)))
        self.card_arch_frontends["value"].setText("3")
        self.card_arch_integrations["value"].setText(
            str(len(ANALYZER_INTEGRATIONS)))
        self.card_arch_plugins["value"].setText(
            str(len(ANALYZER_ARCHITECTURE) + len(ANALYZER_PREMIUM) + 6))

    def describe(self):
        """Machine-readable description of the analyzer (API surface)."""
        return {
            "name": "ScriptAnalyzerTab",
            "version": 1,
            "architecture": [
                {"key": key, "name": fallback, "detail": detail}
                for key, _lk, fallback, detail in ANALYZER_ARCHITECTURE],
            "operations": [key for key, _lk, _f in ANALYZER_OPERATIONS],
            "exports": [key for key, _lk, _f in ANALYZER_EXPORTS],
            "premium": [key for key, _lk, _f in ANALYZER_PREMIUM],
            "integrations": [key for key, _lk, _f in ANALYZER_INTEGRATIONS],
            "frontends": ["gui", "api", "cli"],
        }

    def _show_architecture_api(self):
        import json as _json
        self._state["last_operation"] = {
            "operation": "api", "title": "describe()",
            "scope": "API", "risk": "-", "generated": _now(),
            "items": len(ANALYZER_ARCHITECTURE),
            "groups": [{"title": "describe()", "colour": "#9be9a8",
                        "rows": [{"name": line, "kind": "json", "detail": ""}
                                 for line in _json.dumps(
                                     self.describe(), ensure_ascii=False,
                                     indent=2).splitlines()]}]}
        self._focus_subtab("analyzer_tab_operations")

    def _show_architecture_cli(self):
        self._state["last_operation"] = {
            "operation": "cli", "title": "capabilities",
            "scope": "CLI", "risk": "-", "generated": _now(),
            "items": 0,
            "groups": [{"title": "CLI capabilities", "colour": "#f7c66d",
                        "rows": [{"name": line, "kind": "cli", "detail": ""}
                                 for line in analyzer_cli(
                                     ["capabilities"]).splitlines()]}]}
        self._focus_subtab("analyzer_tab_operations")

    @staticmethod
    def _integration_available(key):
        """Best-effort check that an integration target exists."""
        if key in ("diff", "audit"):
            return True
        if key == "runner":
            try:
                import shutil
                return bool(shutil.which("python") or shutil.which("py"))
            except Exception:
                return False
        return True

    @staticmethod
    def _integration_detail(key):
        return {
            "diff": "Diff tab: open script vs previous version",
            "audit": "Audit tab: findings on the shared event bus",
            "runner": "Runner: run a script to observe real runtime",
            "hooks": "Hooks: before_analysis / after_analysis / finding",
            "registry": "API_REGISTRY: register/analyzers",
            "cli": "analyzer_cli([...]) head-less entry point",
        }.get(key, key)

    # -------------------------------------------------------- premium engine
    def _premium_record(self, groups, title, scope, risk="-"):
        """Store and display one premium analysis result."""
        result = {"operation": "premium", "title": title, "scope": scope,
                  "risk": risk, "generated": _now(),
                  "items": sum(len(g.get("rows", [])) for g in groups),
                  "groups": groups}
        self._state["last_premium"] = result
        for group in groups:
            for row in group.get("rows", []):
                if isinstance(row, dict) and row.get("kind") in ("high", "risk"):
                    HOOKS.fire("finding", row=row, source="premium")
        self._focus_subtab("analyzer_tab_premium")
        self._announce("%s: %s item(s)" % (title, result["items"]))
        return result

    def run_premium(self, key):
        """Run one premium feature and show its result."""
        builder = getattr(self, "_premium_" + key, None)
        if builder is None:
            return None
        return builder()

    def _premium_rows_from(self, results):
        """Flatten a list of analysed results into finding rows."""
        rows = []
        for result in results:
            for issue in result.get("issues", []):
                if issue.get("severity") in ("high", "medium"):
                    rows.append({
                        "name": result["name"],
                        "kind": issue.get("severity", ""),
                        "detail": "%s: %s" % (
                            self.t(issue.get("kind", ""), issue.get("kind", "")),
                            issue.get("detail", ""))})
        return rows

    def _premium_auto_change(self):
        """Automatic analysis after every change: watch scripts for edits."""
        watched = self._state.get("watch_signatures", {})
        changed = []
        for script in self._scripts():
            signature = self._script_signature(script)
            previous = watched.get(script.get("id"))
            if previous is not None and previous != signature:
                changed.append(script)
            watched[script.get("id")] = signature
        self._state["watch_signatures"] = watched
        self._state["watch_enabled"] = True
        rows = []
        for script in changed:
            result = self._analyze_script(script)
            rows.append({"name": script.get("name", ""),
                         "kind": "changed",
                         "detail": "re-analysed, score=%s" % result.get("score", 0)})
        if not rows:
            rows = [{"name": self.t("analyzer_prem_nochange", "no change since last run"),
                     "kind": "info", "detail": "%s script(s) watched" % (
                         len(self._state.get("watch_signatures", {})))}]
        return self._premium_record(
            [{"title": self.t("analyzer_prem_auto", "Automatic analysis"),
              "colour": "#8ab4f8", "rows": rows}],
            self.t("analyzer_prem_auto", "Automatic analysis after every change"),
            "%s script(s)" % len(self._scripts()))

    @staticmethod
    def _script_signature(script):
        import hashlib
        code = (script.get("code", "") or "").encode("utf-8", "replace")
        return hashlib.sha1(code).hexdigest()

    def _premium_realtime(self):
        """Real-time analysis: report the live state of the watcher."""
        watcher = getattr(self, "premium_watch_timer", None)
        running = bool(watcher is not None and watcher.isActive())
        self._state["watch_enabled"] = running
        results = self._state.get("results", [])
        rows = []
        for result in results[:200]:
            rows.append({"name": result["name"], "kind": "live",
                         "detail": "score=%s issues=%s" % (
                             result.get("score", 0), len(result.get("issues", [])))})
        if not rows:
            rows = [{"name": self.t("analyzer_prem_nodata", "no data yet"),
                     "kind": "info", "detail": ""}]
        return self._premium_record(
            [{"title": self.t("analyzer_prem_realtime", "Real-time analysis"),
              "colour": "#9be9a8", "rows": rows}],
            self.t("analyzer_prem_realtime", "Real-time analysis"),
            "watcher %s" % ("on" if running else "off"))

    def _premium_predictive(self):
        """Predictive analysis: which parts are likely to break next."""
        predictions = []
        for result in self._state.get("results", []):
            complexity = result.get("complexity") or []
            worst = max((c.get("complexity", 0) for c in complexity), default=0)
            if worst > 10:
                predictions.append({
                    "name": result["name"], "kind": "complexity",
                    "detail": "high cyclomatic complexity (%s) may hide bugs" % worst})
            style = result.get("style") or {}
            if style.get("deep_nesting"):
                predictions.append({
                    "name": result["name"], "kind": "nesting",
                    "detail": "%s deeply nested block(s) are fragile" % len(
                        style["deep_nesting"])})
            if style.get("long_funcs"):
                predictions.append({
                    "name": result["name"], "kind": "long_function",
                    "detail": "%s long function(s) are hard to test" % len(
                        style["long_funcs"])})
            rt = result.get("runtime") or {}
            if rt.get("dangerous"):
                predictions.append({
                    "name": result["name"], "kind": "high",
                    "detail": "%s dangerous call(s) may fail at runtime" % len(
                        rt["dangerous"])})
            dep = result.get("dependency_summary") or {}
            if dep.get("missing"):
                predictions.append({
                    "name": result["name"], "kind": "dependency",
                    "detail": "%s missing dependency(ies) will break import" % (
                        dep["missing"])})
            hist = result.get("history_summary") or {}
            if hist.get("refactors"):
                predictions.append({
                    "name": result["name"], "kind": "churn",
                    "detail": "%s recent refactor(s): regression risk" % (
                        hist["refactors"])})
        if not predictions:
            predictions = [{"name": self.t("analyzer_prem_norisk", "no risk predicted"),
                            "kind": "info", "detail": ""}]
        return self._premium_record(
            [{"title": self.t("analyzer_prem_predictive", "Predictive analysis"),
              "colour": "#f7c66d", "rows": predictions}],
            self.t("analyzer_prem_predictive", "Predictive analysis"),
            "%s prediction(s)" % len(predictions),
            risk=self._project_risk())

    def _premium_risk(self):
        """Risk analysis: graded risk per script and for the project."""
        rows = []
        for result in self._state.get("results", []):
            level = self._risk_label(result, security=True)
            rows.append({"name": result["name"], "kind": level,
                         "detail": self._risk_detail(result)})
        rows.sort(key=lambda r: {"high": 0, "medium": 1, "low": 2}.get(
            r["kind"], 3))
        level = self._project_risk()
        return self._premium_record(
            [{"title": self.t("analyzer_prem_risk", "Risk analysis"),
              "colour": "#ff7b72", "rows": rows}],
            self.t("analyzer_prem_risk", "Risk analysis"),
            "%s script(s)" % len(rows), risk=level)

    def _risk_detail(self, result):
        issues = result.get("issues", [])
        high = sum(1 for i in issues if i.get("severity") == "high")
        medium = sum(1 for i in issues if i.get("severity") == "medium")
        dangerous = len((result.get("runtime") or {}).get("dangerous", []))
        return "high=%s medium=%s dangerous=%s score=%s" % (
            high, medium, dangerous, result.get("score", 0))

    def _project_risk(self):
        levels = [self._risk_label(r, security=True)
                  for r in self._state.get("results", [])]
        if "high" in levels:
            return "high"
        if "medium" in levels:
            return "medium"
        return "low" if levels else "-"

    def _premium_policy(self):
        """Policy compliance: check the project against a small rule set."""
        rules = (
            ("no_eval", "analyzer_policy_no_eval", "no eval/exec",
             lambda code, r: "eval(" not in code and "exec(" not in code),
            ("no_shell", "analyzer_policy_no_shell", "no shell=True",
             lambda code, r: "shell=True" not in code),
            ("has_headers", "analyzer_policy_headers",
             "script has a description/tag",
             lambda code, r: bool(r.get("has_metadata"))),
            ("no_todo", "analyzer_policy_no_todo", "no lingering TODO/FIXME",
             lambda code, r: "TODO" not in code and "FIXME" not in code),
            ("max_complexity", "analyzer_policy_complexity",
             "cyclomatic complexity <= 10",
             lambda code, r: max(
                 (c.get("complexity", 0) for c in r.get("complexity", [])),
                 default=0) <= 10),
            ("syntax_ok", "analyzer_policy_syntax", "syntax is valid",
             lambda code, r: (r.get("syntax") or {}).get("valid", True)),
        )
        rows = []
        violations = 0
        for script in self._scripts():
            result = self._analyze_script(script)
            result["has_metadata"] = bool(script.get("desc") or script.get("tags"))
            code = script.get("code", "") or ""
            for _key, label_key, fallback, check in rules:
                try:
                    ok = check(code, result)
                except Exception:
                    ok = False
                if not ok:
                    violations += 1
                    rows.append({"name": script.get("name", ""),
                                 "kind": "violation",
                                 "detail": self.t(label_key, fallback)})
        if not rows:
            rows = [{"name": self.t("analyzer_policy_ok", "compliant"),
                     "kind": "info", "detail": ""}]
        return self._premium_record(
            [{"title": self.t("analyzer_prem_policy", "Policy compliance"),
              "colour": "#c586c0", "rows": rows}],
            self.t("analyzer_prem_policy", "Policy compliance analysis"),
            "%s violation(s)" % violations,
            risk="high" if violations > len(self._scripts()) else "low")

    def _premium_project(self):
        """Whole-project quality: aggregated metrics across every script."""
        results = self._state.get("results", [])
        total = len(results)
        scores = [r.get("score", 0) for r in results]
        avg = round(sum(scores) / total, 1) if total else 0
        total_lines = sum(
            (r.get("metrics") or {}).get("total_lines", 0) for r in results)
        total_issues = sum(len(r.get("issues", [])) for r in results)
        worst = min(results, key=lambda r: r.get("score", 0)) if results else None
        rows = [
            {"name": self.t("analyzer_prem_total_scripts", "Scripts"),
             "kind": "count", "detail": str(total)},
            {"name": self.t("analyzer_prem_total_lines", "Total lines"),
             "kind": "count", "detail": str(total_lines)},
            {"name": self.t("analyzer_prem_total_issues", "Total issues"),
             "kind": "count", "detail": str(total_issues)},
            {"name": self.t("analyzer_score", "Avg. score"),
             "kind": "score", "detail": str(avg)},
            {"name": self.t("analyzer_worst", "Lowest score"),
             "kind": "worst",
             "detail": "%s (%s)" % (
                 worst["name"] if worst else "-",
                 worst.get("score", 0) if worst else 0)},
        ]
        # Category breakdown.
        by_category = {}
        for result in results:
            category = result.get("category", "?")
            by_category[category] = by_category.get(category, 0) + 1
        for category, count in sorted(by_category.items()):
            rows.append({"name": category, "kind": "category",
                         "detail": "%s script(s)" % count})
        return self._premium_record(
            [{"title": self.t("analyzer_prem_project", "Project quality"),
              "colour": "#9be9a8", "rows": rows}],
            self.t("analyzer_prem_project", "Whole-project quality analysis"),
            "%s script(s)" % total, risk=self._project_risk())

    def _fill_premium(self):
        """Repaint the premium tab from the last premium result."""
        tree = self.premium_tree.tree
        tree.clear()
        result = self._state.get("last_premium")
        if not result:
            tree.addTopLevelItem(self._placeholder(3))
        else:
            for group in result.get("groups", []):
                node = QtWidgets.QTreeWidgetItem(
                    [group.get("title", ""), str(len(group.get("rows", []))), ""])
                node.setForeground(0, QtGui.QColor(group.get("colour", "#8ab4f8")))
                for row in group.get("rows", []):
                    colour = {"high": "#ff7b72", "medium": "#f7c66d",
                              "violation": "#ff9a9a"}.get(
                                  row.get("kind"), group.get("colour", "#dfe7f5"))
                    child = QtWidgets.QTreeWidgetItem([
                        row.get("name", ""), row.get("kind", ""),
                        row.get("detail", "")])
                    child.setForeground(1, QtGui.QColor(colour))
                    node.addChild(child)
                node.setExpanded(True)
                tree.addTopLevelItem(node)
        summary = self._premium_summary()
        self.card_prem_predict["value"].setText(str(summary["predictions"]))
        self.card_prem_risk["value"].setText(summary["risk"])
        self.card_prem_risk["value"].setStyleSheet(
            """font-size: 22px; font-weight: bold; border:none; color:%s;"""
            % {"high": "#ff7b72", "medium": "#f7c66d", "low": "#9be9a8"}.get(
                summary["risk"], "#dfe7f5"))
        self.card_prem_project["value"].setText(str(summary["project_score"]))

    def _premium_summary(self):
        results = self._state.get("results", [])
        scores = [r.get("score", 0) for r in results]
        return {
            "predictions": self._state.get("predictions", 0),
            "risk": self._project_risk(),
            "project_score": round(sum(scores) / len(scores), 1) if scores else 0,
        }

    # ------------------------------------------------- automation toggles
    def _toggle_auto_analysis(self, enabled):
        """Enable/disable automatic re-analysis after every change."""
        self._state["watch_enabled"] = bool(enabled)
        # Seed signatures so the first poll reports nothing as changed.
        self._state["watch_signatures"] = {
            script.get("id"): self._script_signature(script)
            for script in self._scripts()}
        if enabled:
            self._start_premium_timer()
        self._announce("auto-analysis %s" % ("on" if enabled else "off"))

    def _toggle_realtime(self, enabled):
        """Enable/disable the real-time watcher (alias of the auto poller)."""
        if enabled:
            self._start_premium_timer()
        else:
            self._stop_premium_timer()

    def _start_premium_timer(self):
        if getattr(self, "premium_watch_timer", None) is None:
            timer = QtCore.QTimer(self)
            timer.setInterval(PREMIUM_POLL_MS)
            timer.timeout.connect(self._poll_changes)
            self.premium_watch_timer = timer
        if not self.premium_watch_timer.isActive():
            self.premium_watch_timer.start()

    def _stop_premium_timer(self):
        timer = getattr(self, "premium_watch_timer", None)
        if timer is not None and timer.isActive():
            timer.stop()

    def _poll_changes(self):
        """Timer callback: re-analyse scripts whose code changed on disk/list."""
        if not self._state.get("watch_enabled"):
            return
        watched = self._state.get("watch_signatures", {})
        changed = []
        for script in self._scripts():
            signature = self._script_signature(script)
            if watched.get(script.get("id")) not in (None, signature):
                changed.append(script)
            watched[script.get("id")] = signature
        if changed:
            self._state["watch_signatures"] = watched
            self.refresh()
            self._announce("auto-analysis: %s file(s) changed" % len(changed))

    def _on_subtab_changed(self, index):
        self._fill_page(index)

    def _fill_page(self, index):
        key = self._subtab_keys.get(index, ("", ""))[0]
        if key == "analyzer_tab_overview":
            self._fill_overview()
        elif key == "analyzer_tab_metrics":
            self._fill_metrics()
        elif key == "analyzer_tab_structure":
            self._fill_structure()
        elif key == "analyzer_tab_complexity":
            self._fill_complexity()
        elif key == "analyzer_tab_issues":
            self._fill_issues()
        elif key == "analyzer_tab_syntax":
            self._fill_syntax()
        elif key == "analyzer_tab_ast":
            self._fill_ast()
        elif key == "analyzer_tab_quality":
            self._fill_quality()
        elif key == "analyzer_tab_runtime":
            self._fill_runtime()
        elif key == "analyzer_tab_dependencies":
            self._fill_dependencies()
        elif key == "analyzer_tab_history":
            self._fill_history()
        elif key == "analyzer_tab_config":
            self._fill_config()
        elif key == "analyzer_tab_operations":
            self._fill_operations()
        elif key == "analyzer_tab_architecture":
            self._fill_architecture()
        elif key == "analyzer_tab_premium":
            self._fill_premium()

    def _fill_overview(self):
        summary = self._summary()
        self.card_scripts["value"].setText(str(summary["scripts"]))
        self.card_issues["value"].setText(str(summary["issues"]))
        self.card_issues["value"].setStyleSheet(
            """font-size: 22px; font-weight: bold; border:none;"""
            " color:%s;" % ("#ff9a9a" if summary["issues"] else "#9be9a8"))
        self.card_score["value"].setText(str(summary["avg_score"]))
        score_colour = ("#9be9a8" if summary["avg_score"] >= 80
                        else "#f7c66d" if summary["avg_score"] >= 50
                        else "#ff7b72")
        self.card_score["value"].setStyleSheet(
            """font-size: 22px; font-weight: bold; border:none;"""
            " color:%s;" % score_colour)
        worst = summary["worst"]
        self.card_worst["value"].setText(worst["name"] if worst else "-")
        self.card_worst["value"].setToolTip(
            str(worst["score"]) if worst else "")

        tree = self.overview_tree.tree
        tree.clear()
        for result in self._state.get("results", []):
            item = QtWidgets.QTreeWidgetItem([
                result["name"], result["category"],
                str(result["metrics"]["total_lines"]),
                str(len(result["functions"])), str(len(result["classes"])),
                str(result["score"])])
            colour = ("#9be9a8" if result["score"] >= 80
                      else "#f7c66d" if result["score"] >= 50 else "#ff7b72")
            item.setForeground(5, QtGui.QColor(colour))
            tree.addTopLevelItem(item)
        if not self._state.get("results"):
            tree.addTopLevelItem(self._placeholder(6))

    def _fill_structure(self):
        tree = self.structure_tree.tree
        tree.clear()
        for result in self._state.get("results", []):
            for name in result["classes"]:
                item = QtWidgets.QTreeWidgetItem(
                    [name, self.t("analyzer_kind_class", "class"),
                     result["name"]])
                item.setForeground(1, QtGui.QColor("#c586c0"))
                tree.addTopLevelItem(item)
            for name in result["functions"]:
                item = QtWidgets.QTreeWidgetItem(
                    [name, self.t("analyzer_kind_function", "function"),
                     result["name"]])
                item.setForeground(1, QtGui.QColor("#8ab4f8"))
                tree.addTopLevelItem(item)
        if tree.topLevelItemCount() == 0:
            tree.addTopLevelItem(self._placeholder(3))

    def _fill_complexity(self):
        tree = self.complexity_tree.tree
        tree.clear()
        for result in self._state.get("results", []):
            for row in result["complexity"]:
                item = QtWidgets.QTreeWidgetItem([
                    row["name"], str(row["complexity"]), str(row["lines"]),
                    str(row["params"]), result["name"]])
                colour = ("#9be9a8" if row["complexity"] <= 5
                          else "#f7c66d" if row["complexity"] <= 10
                          else "#ff7b72")
                item.setForeground(1, QtGui.QColor(colour))
                tree.addTopLevelItem(item)
        if tree.topLevelItemCount() == 0:
            tree.addTopLevelItem(self._placeholder(5))

    def _fill_issues(self):
        tree = self.issues_tree.tree
        tree.clear()
        for result in self._state.get("results", []):
            for issue in result["issues"]:
                item = QtWidgets.QTreeWidgetItem([
                    result["name"], self.t(issue["kind"], issue["kind"]),
                    issue.get("detail", ""),
                    str(issue.get("line", "")) if issue.get("line") else "",
                    issue["severity"].upper()])
                colour = SEVERITY_COLOURS.get(issue["severity"], "#dfe7f5")
                item.setForeground(1, QtGui.QColor(colour))
                item.setForeground(4, QtGui.QColor(colour))
                tree.addTopLevelItem(item)
        if tree.topLevelItemCount() == 0:
            tree.addTopLevelItem(self._placeholder(5))

    def _fill_dependencies(self):
        """Fill the dependency tree and the summary cards."""
        tree = self.dependencies_tree.tree
        tree.clear()
        agg = {"missing": 0, "unused": 0, "conflicts": 0, "system": 0}
        for result in self._state.get("results", []):
            dep = result.get("dependencies") or {}
            summary = result.get("dependency_summary") or {}
            for key in agg:
                agg[key] += summary.get(key, 0)
            root = QtWidgets.QTreeWidgetItem(
                [result["name"], dep.get("standards", ""), "", ""])
            root.setForeground(0, QtGui.QColor("#8ab4f8"))
            tree.addTopLevelItem(root)
            self._dep_group(root, "analyzer_dep_missing",
                            dep.get("missing", []),
                            lambda r: r["name"],
                            lambda r: "line %s" % r["line"], "#ff7b72")
            self._dep_group(root, "analyzer_dep_unused",
                            dep.get("unused", []),
                            lambda r: r["name"],
                            lambda r: "line %s" % r["line"], "#f7c66d")
            self._dep_group(root, "analyzer_dep_conflicts",
                            dep.get("conflicts", []),
                            lambda r: r["name"],
                            lambda r: r["detail"], "#ff7b72")
            self._dep_group(root, "analyzer_dep_interpreters",
                            dep.get("interpreters", []),
                            lambda r: r["kind"],
                            lambda r: "%s (line %s)" % (
                                r["detail"], r["line"]), "#c586c0")
            self._dep_group(root, "analyzer_dep_system",
                            dep.get("system", []),
                            lambda r: r["name"],
                            lambda r: ("%s %s" % (
                                self.t(r["kind"], r["kind"]),
                                self.t("analyzer_yes", "yes")
                                if r.get("available") else
                                self.t("analyzer_no", "no"))), "#9be9a8")
            self._dep_group(root, "analyzer_dep_imports",
                            dep.get("imports", []),
                            lambda r: r["name"],
                            lambda r: "%s%s" % (
                                self.t("analyzer_kind_internal", "internal")
                                if r["internal"] else
                                self.t("analyzer_kind_external", "external"),
                                " version %s" % r["version"]
                                if r.get("version") else ""), "#8ab4f8")
            root.setExpanded(True)
        if tree.topLevelItemCount() == 0:
            tree.addTopLevelItem(self._placeholder(4))
        self.card_dep_missing["value"].setText(str(agg["missing"]))
        self.card_dep_missing["value"].setStyleSheet(
            """font-size: 22px; font-weight: bold; border:none; color:%s;"""
            % ("#ff7b72" if agg["missing"] else "#9be9a8"))
        self.card_dep_unused["value"].setText(str(agg["unused"]))
        self.card_dep_conflict["value"].setText(str(agg["conflicts"]))
        self.card_dep_conflict["value"].setStyleSheet(
            """font-size: 22px; font-weight: bold; border:none; color:%s;"""
            % ("#ff7b72" if agg["conflicts"] else "#9be9a8"))
        self.card_dep_system["value"].setText(str(agg["system"]))

    def _dep_group(self, root, key, values, name_fn, detail_fn, colour):
        """Add one labelled dependency group to the tree."""
        node = QtWidgets.QTreeWidgetItem(
            [self.t(key, key), str(len(values)), "", ""])
        node.setForeground(0, QtGui.QColor(colour))
        for value in values:
            child = QtWidgets.QTreeWidgetItem(
                [name_fn(value), "", detail_fn(value), ""])
            child.setForeground(0, QtGui.QColor(colour))
            node.addChild(child)
        root.addChild(node)

    @staticmethod
    def _placeholder(columns):
        item = QtWidgets.QTreeWidgetItem(
            ["", QtWidgets.QApplication.translate("", "none")]
            + [""] * (columns - 2))
        item.setForeground(1, QtGui.QColor("#6a737d"))
        return item

    def _export(self):
        path = self.export_report("txt")
        if path:
            self.status.setText(
                self.t("analyzer_exported", "Exported: {path}").format(path=path))
            self._announce("analysis exported")

    # ------------------------------------------------------- export engine
    # Every format builds on one shared data set so the on-screen result, the
    # reports and the exports never disagree.

    def export(self, key):
        """Export the analysis in one of the formats of the Export menu."""
        fmt = {
            "log": "log", "analysis": "txt", "security": "txt",
            "changes": "txt",
        }.get(key, key)
        if key == "analysis":
            content = self.build_report("txt")
        elif key == "security":
            content = self._build_security_report()
        elif key == "changes":
            content = self._build_changes_report()
        elif key == "log":
            content = self._build_log()
        else:
            content = self.build_report(fmt)
        path = self._write_export(content, key)
        if path:
            self.status.setText(
                self.t("analyzer_exported", "Exported: {path}").format(path=path))
            self._announce("analysis exported: %s" % key)
        return path
    def _write_export(self, content, key):
        """Write `content` to the analyzer export folder and return the path."""
        directory = os.path.join(self._data_dir(), "analyzer_exports")
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError:
            return ""
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        name = {
            "txt": "analysis-%s.txt", "json": "analysis-%s.json",
            "log": "analysis-%s.log", "analysis": "report-%s.txt",
            "security": "security-%s.txt", "changes": "changes-%s.txt",
        }.get(key, "analysis-%s.txt")
        path = os.path.join(directory, name % stamp)
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(content)
        except OSError:
            return ""
        return path
    def _build_security_report(self):
        """A security-focused report built from the per-script findings."""
        lines = ["ANALYZER SECURITY REPORT", "generated: %s" % _now(),
                 "=" * 60]
        total = 0
        for result in self._state.get("results", []):
            runtime = result.get("runtime") or {}
            code = ""
            for script in self._scripts():
                if script.get("id") == result.get("id"):
                    code = script.get("code", "") or ""
                    break
            findings = list(runtime.get("dangerous", []))
            static = []
            for token in ("eval", "exec", "os.system", "subprocess",
                          "pickle", "shell=True", "shutil.rmtree"):
                if token in code:
                    static.append({"kind": token,
                                   "line": 0,
                                   "detail": "%s occurrence(s)" % (
                                       code.count(token))})
            if not findings and not static:
                continue
            lines.append("")
            lines.append("%s [%s]  risk=%s" % (
                result["name"], result.get("category", ""),
                self._risk_label(result, security=True)))
            for f in findings:
                lines.append("  [dangerous] %s line %s" % (
                    f.get("kind", ""), f.get("line", 0)))
                total += 1
            for f in static:
                lines.append("  [static] %s - %s" % (
                    f["kind"], f["detail"]))
                total += 1
        if total == 0:
            lines.append("")
            lines.append("No security findings.")
        return chr(10).join(lines)

    def _build_changes_report(self):
        """A change-focused report: diffs vs previous version/snapshot."""
        lines = ["ANALYZER CHANGES REPORT", "generated: %s" % _now(),
                 "=" * 60]
        total = 0
        for result in self._state.get("results", []):
            history = result.get("history") or {}
            summary = result.get("history_summary") or {}
            if not summary.get("total_changes"):
                continue
            lines.append("")
            lines.append("%s [%s]  %s change(s)" % (
                result["name"], result.get("category", ""),
                summary.get("total_changes", 0)))
            for label, changes in (("previous", history.get("previous") or {}),
                                   ("snapshot", history.get("snapshot") or {})):
                for cat in ("functions", "classes", "imports", "logic"):
                    for change in changes.get(cat, []):
                        lines.append("  [%s/%s] %s %s" % (
                            label, cat, change.get("name", ""),
                            change.get("detail", "")))
                        total += 1
            for ref in history.get("refactors", []):
                lines.append("  [refactor/%s] %s %s" % (
                    ref.get("kind", ""), ref.get("name", ""),
                    ref.get("detail", "")))
                total += 1
        if total == 0:
            lines.append("")
            lines.append("No changes detected.")
        return chr(10).join(lines)

    def _build_log(self):
        """A chronological LOG of the studies and their key findings."""
        lines = []
        summary = self._summary()
        lines.append("%s INFO  analyzer run started" % _now())
        lines.append("%s INFO  %s script(s), %s issue(s), avg score %s" % (
            _now(), summary["scripts"], summary["issues"],
            summary["avg_score"]))
        for result in self._state.get("results", []):
            lines.append("%s INFO  %s [%s] score=%s issues=%s" % (
                _now(), result["name"], result.get("category", ""),
                result.get("score", 0), len(result.get("issues", []))))
            syntax = result.get("syntax") or {}
            for error in syntax.get("errors", []):
                lines.append("%s ERROR %s line %s: %s" % (
                    _now(), result["name"], error.get("line", 0),
                    error.get("detail", "")))
            for issue in result.get("issues", []):
                level = "ERROR" if issue.get("severity") == "high" \
                    else "WARN" if issue.get("severity") == "medium" else "INFO"
                lines.append("%s %s %s %s" % (
                    _now(), level, result["name"],
                    issue.get("kind", "")))
        last = self._state.get("last_operation")
        if last:
            lines.append("%s INFO  last operation: %s (%s item(s))" % (
                _now(), last.get("title", ""), last.get("items", 0)))
        lines.append("%s INFO  analyzer run finished" % _now())
        return chr(10).join(lines)

    # ------------------------------------------------------------
    def refresh(self):
        """Re-analyse every script and repaint the visible sub-tab."""
        # Keep the premium state (watcher, signatures, last results) across a
        # re-analysis so the automation and the last operation stay visible.
        preserved = dict(
            (key, self._state.get(key)) for key in (
                "watch_enabled", "watch_signatures", "last_operation",
                "last_operation_key", "last_premium", "predictions")
            if key in self._state)
        HOOKS.fire("before_analysis", scripts=self._scripts())
        self._state = {"results": self.analyze_all()}
        self._state.update(preserved)
        summary = self._summary()
        self.status.setText(self.t(
            "analyzer_status",
            "{scripts} script(s), {issues} issue(s), avg score {score}").format(
            scripts=summary["scripts"], issues=summary["issues"],
            score=summary["avg_score"]))
        self._fill_page(self.subtabs.currentIndex())
        HOOKS.fire("after_analysis", summary=summary,
                   results=self._state.get("results", []))

    def retranslate(self, table):
        """Apply the main window's language table to this tab."""
        def label(key, fallback):
            return table.get(key, fallback)

        self.title_label.setText(label("analyzer", "Analyzer"))
        self.btn_analyse.setText(label("analyzer_analyse", "Analyse"))
        self.btn_export.setText(label("analyzer_export", "Export"))
        if hasattr(self, "btn_operations"):
            self.btn_operations.setText(
                label("analyzer_operations", "Operations"))
            for key, label_key, fallback in ANALYZER_OPERATIONS:
                if key in self.operation_actions:
                    self.operation_actions[key].setText(
                        label(label_key, fallback))
            for key, label_key, fallback in ANALYZER_EXPORTS:
                if key in self.export_actions:
                    self.export_actions[key].setText(
                        label(label_key, fallback))
            self.operations_tree.setTitle(
                label("analyzer_operations_result", "Operation result"))
            self.operations_tree.tree.setHeaderLabels([
                label("analyzer_op_item", "Item"),
                label("analyzer_kind", "Kind"),
                label("audit_detail", "Detail")])
            self.card_op_scope["title"].setText(
                label("analyzer_op_scope", "Scope"))
            self.card_op_items["title"].setText(
                label("analyzer_op_items", "Findings"))
            self.card_op_risk["title"].setText(
                label("analyzer_op_risk", "Risk"))
            self.card_op_time["title"].setText(
                label("analyzer_op_time", "Generated"))
            self.btn_premium.setText(label("analyzer_premium", "Premium"))
            for key, label_key, fallback in ANALYZER_PREMIUM:
                if key in self.premium_actions:
                    self.premium_actions[key].setText(
                        label(label_key, fallback))
                if key in self.premium_buttons:
                    self.premium_buttons[key].setText(
                        label(label_key, fallback))
            self.btn_architecture.setText(
                label("analyzer_architecture", "Architecture"))
            self.btn_arch_api.setText(
                label("analyzer_arch_api", "API: describe()"))
            self.btn_arch_cli.setText(
                label("analyzer_arch_cli", "CLI: capabilities"))
            self.architecture_tree.setTitle(
                label("analyzer_architecture", "Architecture"))
            self.architecture_tree.tree.setHeaderLabels([
                label("analyzer_arch_item", "Item"),
                label("analyzer_kind", "Kind"),
                label("audit_detail", "Detail")])
            self.card_arch_standards["title"].setText(
                label("analyzer_arch_standards", "Standards"))
            self.card_arch_frontends["title"].setText(
                label("analyzer_arch_frontends", "Front-ends"))
            self.card_arch_integrations["title"].setText(
                label("analyzer_arch_integrations", "Integrations"))
            self.card_arch_plugins["title"].setText(
                label("analyzer_arch_plugins", "Plugins"))
            self.premium_tree.setTitle(
                label("analyzer_premium", "Premium analysis"))
            self.premium_tree.tree.setHeaderLabels([
                label("analyzer_prem_item", "Item"),
                label("analyzer_kind", "Kind"),
                label("audit_detail", "Detail")])
            self.card_prem_changes["title"].setText(
                label("analyzer_prem_changes", "Watched changes"))
            self.card_prem_predict["title"].setText(
                label("analyzer_prem_predict", "Predicted risks"))
            self.card_prem_risk["title"].setText(
                label("analyzer_prem_risk_col", "Risk level"))
            self.card_prem_project["title"].setText(
                label("analyzer_prem_project_col", "Project score"))
            self.chk_premium_auto.setText(
                label("analyzer_prem_auto_toggle",
                      "Auto-analyse after every change"))
            self.chk_premium_watch.setText(
                label("analyzer_prem_watch", "Real-time (watcher)"))
        for index, (key, fallback) in self._subtab_keys.items():
            self.subtabs.setTabText(index, label(key, fallback))
        self.card_scripts["title"].setText(label("analyzer_scripts", "Scripts"))
        self.card_issues["title"].setText(label("analyzer_issues", "Issues"))
        self.card_score["title"].setText(label("analyzer_score", "Avg. score"))
        self.card_worst["title"].setText(label("analyzer_worst", "Lowest score"))
        self.overview_tree.setTitle(label("analyzer_overview", "Per-script overview"))
        self.overview_tree.tree.setHeaderLabels([
            label("name", "Name"), label("category_col", "Category"),
            label("analyzer_lines", "Lines"), label("analyzer_funcs", "Funcs"),
            label("analyzer_classes", "Classes"),
            label("analyzer_score", "Score")])
        self.metrics_tree.setTitle(label("analyzer_metrics", "Metrics"))
        self.metrics_tree.tree.setHeaderLabels([
            label("analyzer_m_metric", "Metric"),
            label("analyzer_m_value", "Value"),
            label("audit_where", "Script")])
        self.card_m_lines["title"].setText(
            label("analyzer_m_lines", "Lines of code"))
        self.card_m_functions["title"].setText(
            label("analyzer_funcs", "Funcs"))
        self.card_m_classes["title"].setText(
            label("analyzer_classes", "Classes"))
        self.card_m_imports["title"].setText(
            label("analyzer_m_imports", "Imports"))
        self.card_m_errors["title"].setText(
            label("analyzer_m_errors", "Errors"))
        self.card_m_warnings["title"].setText(
            label("analyzer_m_warnings", "Warnings"))
        self.card_m_cyclomatic["title"].setText(
            label("analyzer_m_cyclomatic", "Cyclomatic"))
        self.card_m_logical["title"].setText(
            label("analyzer_m_logical", "Logical"))
        self.structure_tree.setTitle(label("analyzer_structure", "Structure"))
        self.structure_tree.tree.setHeaderLabels([
            label("analyzer_symbol", "Symbol"), label("analyzer_kind", "Kind"),
            label("analyzer_in", "In script")])
        self.complexity_tree.setTitle(label("analyzer_complexity", "Complexity"))
        self.complexity_tree.tree.setHeaderLabels([
            label("analyzer_function", "Function"),
            label("analyzer_complexity", "Complexity"),
            label("analyzer_lines", "Lines"), label("analyzer_params", "Params"),
            label("audit_where", "Script")])
        self.issues_tree.setTitle(label("analyzer_issues", "Issues"))
        self.issues_tree.tree.setHeaderLabels([
            label("audit_where", "Script"),
            label("analyzer_issue_kind", "Issue"),
            label("audit_detail", "Detail"), label("analyzer_line", "Line"),
            label("audit_severity", "Severity")])
        self.dependencies_tree.setTitle(
            label("analyzer_dependencies", "Dependencies"))
        self.dependencies_tree.tree.setHeaderLabels([
            label("analyzer_dep_item", "Item"), label("analyzer_kind", "Kind"),
            label("audit_detail", "Detail"),
            label("analyzer_used_by", "Used by")])
        self.card_dep_missing["title"].setText(
            label("analyzer_dep_missing", "Missing"))
        self.card_dep_unused["title"].setText(
            label("analyzer_dep_unused", "Unused"))
        self.card_dep_conflict["title"].setText(
            label("analyzer_dep_conflicts", "Conflicts"))
        self.card_dep_system["title"].setText(
            label("analyzer_dep_system", "System"))
        self.syntax_tree.setTitle(
            label("analyzer_syntax", "Syntax analysis"))
        self.syntax_tree.tree.setHeaderLabels([
            label("audit_where", "Script"),
            label("analyzer_syntax_kind", "Kind"),
            label("audit_detail", "Detail"),
            label("analyzer_line", "Line"),
            label("audit_severity", "Severity")])
        self.ast_tree.setHeaderLabels([
            label("analyzer_ast_node", "Node"),
            label("analyzer_kind", "Kind"),
            label("analyzer_line", "Line")])
        self.quality_tree.setHeaderLabels([
            label("analyzer_quality_item", "Item"),
            label("analyzer_kind", "Kind"),
            label("audit_detail", "Detail")])
        self.runtime_tree.setHeaderLabels([
            label("analyzer_rt_item", "Item"),
            label("analyzer_kind", "Kind"),
            label("audit_detail", "Detail")])
        self.card_rt_steps["title"].setText(
            label("analyzer_rt_steps", "Trace steps"))
        self.card_rt_exc["title"].setText(
            label("analyzer_rt_exceptions", "Exceptions"))
        self.card_rt_danger["title"].setText(
            label("analyzer_rt_dous", "Dangerous"))
        self.card_rt_anom["title"].setText(
            label("analyzer_rt_anomalies", "Anomalies"))
        self.history_tree.setTitle(label("analyzer_history", "Change detection"))
        self.history_tree.tree.setHeaderLabels([
            label("analyzer_hist_item", "Item"),
            label("analyzer_kind", "Kind"),
            label("audit_detail", "Detail"),
            label("audit_where", "Script")])
        self.card_hist_prev["title"].setText(
            label("analyzer_hist_prev", "Changes vs previous"))
        self.card_hist_snap["title"].setText(
            label("analyzer_hist_snapshot", "Changes vs snapshot"))
        self.card_hist_ref["title"].setText(
            label("analyzer_hist_refactors", "Refactors"))
        self.card_hist_total["title"].setText(
            label("analyzer_hist_total", "Total changes"))
        self.btn_hist_diff.setText(
            label("analyzer_hist_open_diff", "Compare in Diff tab"))
        self.config_tree.setTitle(
            label("analyzer_config", "Configuration analysis"))
        self.config_tree.tree.setHeaderLabels([
            label("analyzer_cfg_item", "Item"),
            label("analyzer_kind", "Kind"),
            label("audit_detail", "Detail")])
        self.card_cfg_files["title"].setText(
            label("analyzer_cfg_files", "Config files"))
        self.card_cfg_env["title"].setText(
            label("analyzer_cfg_env", "Env vars"))
        self.card_cfg_path["title"].setText(
            label("analyzer_cfg_path", "PATH entries"))
        self.card_cfg_interp["title"].setText(
            label("analyzer_cfg_interpreters", "Interpreters"))
        # Analysis results are unchanged by a language switch; rebuilding the
        # visible page is enough and avoids rescanning every script.
        self._fill_page(self.subtabs.currentIndex())



# ===========================================================================
# Terminal integration: head-less usage without building the tab
# ===========================================================================


def analyzer_api(tab):
    """Machine-readable API description of a live analyzer tab (or None)."""
    if tab is None:
        return {"name": "ScriptAnalyzerTab", "available": False}
    describe = getattr(tab, "describe", None)
    if callable(describe):
        return describe()
    return {"name": "ScriptAnalyzerTab", "available": True}


def analyzer_report(tab, fmt="txt"):
    """Return the analyzer report as text/json/log for a live tab."""
    if tab is None:
        return ""
    build = getattr(tab, "build_report", None)
    if callable(build):
        return build("json" if fmt == "json" else "txt")
    return ""


def analyzer_cli(argv=None):
    """Head-less analyzer entry point (zero-dependency, ASCII output).

    Supported commands:
      capabilities          - list standards, operations, exports, premium
      describe              - JSON API description
      report [txt|json]     - analyse the script database and print a report

    The CLI never touches Qt widgets, so it can run in a terminal or a test.
    Returns a printable string (callers print it).
    """
    argv = list(argv or [])
    command = argv[0] if argv else "capabilities"
    if command in ("capabilities", "help", "-h", "--help"):
        lines = ["ScriptDB analyzer capabilities",
                 "standards: " + ", ".join(
                     name for _k, _lk, name, _d in ANALYZER_ARCHITECTURE),
                 "operations: " + ", ".join(
                     key for key, _lk, _f in ANALYZER_OPERATIONS),
                 "exports: " + ", ".join(
                     key for key, _lk, _f in ANALYZER_EXPORTS),
                 "premium: " + ", ".join(
                     key for key, _lk, _f in ANALYZER_PREMIUM),
                 "integrations: " + ", ".join(
                     key for key, _lk, _f in ANALYZER_INTEGRATIONS)]
        return chr(10).join(lines)
    if command == "describe":
        import json as _json
        return _json.dumps(analyzer_api(None), ensure_ascii=False, indent=2)
    if command == "report":
        fmt = argv[1] if len(argv) > 1 else "txt"
        # Analyse the databases on disk without building any window.
        data_path = app_paths.db_file("scripts_db.json")
        try:
            import json as _json
            with open(data_path, "r", encoding="utf-8") as handle:
                scripts = _json.load(handle)
        except (OSError, ValueError):
            scripts = []
        results = [{"name": s.get("name", ""), "category": s.get("category", ""),
                    "code": s.get("code", "")} for s in scripts
                   if isinstance(s, dict)]
        if fmt == "json":
            import json as _json
            return _json.dumps({"scripts": results}, ensure_ascii=False, indent=2)
        lines = ["ScriptDB analyzer report",
                 "scripts: %s" % len(results)]
        for row in results:
            lines.append("  %s [%s]" % (row["name"], row["category"]))
        return chr(10).join(lines)
    return "unknown command: %s" % command


ANALYZER_CLI_COMMANDS = ("capabilities", "describe", "report")
