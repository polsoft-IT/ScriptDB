# -*- coding: utf-8 -*-
"""Editor tab for ScriptDB.

This module owns the "Editor" tab: a full editing surface for one script at a
time, sitting right after the Scripts list. It is deliberately self-contained
and follows the same conventions as the Analyzer and Audit tabs:

    EditorTab(window)         -- build the tab
    tab.refresh()             -- reload from the window's script list
    tab.retranslate(table)    -- apply a language table (see TRANSLATIONS)
    tab.load_script(entry)    -- open a script entry in the editor

What it offers (standard library and PyQt5 only, ASCII-safe, sandbox friendly):

    - Open      : load the selected script (or any script) from the list
    - Edit      : syntax-friendly text editor with undo/redo, find & replace
    - Tools     : delete/duplicate line, move up/down, sort/trim, tabs->spaces
    - Metadata  : read, edit and validate the script's own metadata header
    - Save      : write back to the script list with a history revision
    - Artefacts : export the edited source as a plain file

Every change is announced on the shared audit event bus, so the Audit tab's
timeline stays in sync with what is edited here.
"""

import ast
import datetime as dt
import hashlib
import json
import keyword
import os
import re
import shutil
import sys
import tempfile
from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtGui import QFont


# Extension categories for file dialogs (mirrored from sdb.py to avoid circular import)
CATEGORY_EXTENSIONS = {
    "Python": ".py",
    "Bash": ".sh",
    "PowerShell": ".ps1",
    "VBScript": ".vbs",
    "JavaScript": ".js",
    "Java": ".java",
    "C++": ".cpp",
    "Batch": ".bat",
    "HTML": ".html",
    "HTA": ".hta",
    "Registry (REG)": ".reg",
    "JSON": ".json",
    "YAML": ".yaml",
    "INI": ".ini",
    "System config": ".cfg",
    "SQL": ".sql",
    "Ruby": ".rb",
    "Other": ".txt",
}

EXTENSION_CATEGORIES = {ext: cat for cat, ext in CATEGORY_EXTENSIONS.items()}

EXTENSION_ALIASES = {
    ".pyw": "Python", ".pyi": "Python", ".pyx": "Python",
    ".bash": "Bash", ".zsh": "Bash", ".ksh": "Bash", ".shell": "Bash",
    ".psm1": "PowerShell", ".psd1": "PowerShell",
    ".jsx": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript",
    ".ts": "JavaScript", ".tsx": "JavaScript",
    ".vbe": "VBScript", ".wsf": "VBScript",
    ".cmd": "Batch", ".nt": "Batch",
    ".htm": "HTML", ".xhtml": "HTML",
    ".reg": "Registry (REG)",
    ".c": "C++", ".cc": "C++", ".cxx": "C++", ".hpp": "C++", ".h": "C++",
    ".sqlite": "SQL", ".sqlite3": "SQL",
    ".jsonc": "JSON", ".json5": "JSON",
    ".yml": "YAML",
    ".toml": "INI",
    ".conf": "System config", ".inf": "System config",
    ".env": "System config",
    ".rake": "Ruby",
}
EXTENSION_ALIASES.update(EXTENSION_CATEGORIES)


def supported_extensions_filter():
    """Generate a file filter string for QFileDialog with all supported extensions."""
    category_to_exts = {}
    for ext, category in EXTENSION_ALIASES.items():
        if category not in category_to_exts:
            category_to_exts[category] = []
        category_to_exts[category].append(ext)
    
    filters = []
    for category in sorted(category_to_exts.keys()):
        exts = sorted(category_to_exts[category])
        filter_str = f"{category} files ({' '.join(exts)})"
        filters.append(filter_str)
    
    filters.append("All files (*.*)")
    return ";;".join(filters)


def get_interpreter_path(language, settings=None):
    """Get the interpreter path for a given language.
    
    First checks custom settings, then falls back to shutil.which.
    Returns None if no interpreter is found.
    """
    lang_map = {
        "python": "python",
        "py": "python",
        "bash": "bash",
        "sh": "bash",
        "shell": "bash",
        "powershell": "powershell",
        "pwsh": "powershell",
        "javascript": "node",
        "js": "node",
        "node": "node",
        "batch": "batch",
        "bat": "batch",
        "cmd": "batch",
        "vbscript": "vbscript",
        "vbs": "vbscript"
    }
    normalized = lang_map.get(language.lower(), language.lower())
    
    if settings is None:
        try:
            import sdb
            settings = sdb.load_settings(sdb.SETTINGS_FILE, None)
        except:
            settings = {}
    
    interpreters = settings.get("interpreters", {})
    custom_path = interpreters.get(normalized, "")
    if custom_path and os.path.exists(custom_path):
        return custom_path
    
    if normalized == "python":
        return shutil.which("python") or shutil.which("python3")
    elif normalized == "bash":
        return shutil.which("bash")
    elif normalized == "powershell":
        return shutil.which("pwsh") or shutil.which("powershell")
    elif normalized == "node":
        return shutil.which("node") or shutil.which("nodejs")
    elif normalized == "batch":
        return "cmd.exe" if sys.platform == "win32" else None
    elif normalized == "vbscript":
        return "cscript.exe" if sys.platform == "win32" else None
    
    return None
# Language support lives in tabs/syntax/: one hot-pluggable module per
# language, wired together by the registry (see tabs/syntax/__init__.py).
from . import code_generator
from . import code_rules
from . import metadata_core
from . import nav_engine
from . import script_profiles
from . import search_engine
from . import syntax
from . import templates
from . import workflow_engine


# Default programming font stack (cross-platform; falls back gracefully).
EDITOR_FONT = "Consolas"
EDITOR_FONT_SIZE = 10

# Editor tool actions: (key, label key, fallback).
EDITOR_TOOLS = (
    ("delete_line", "editor_delete_line", "Delete line"),
    ("move_up", "editor_move_up", "Move up"),
    ("move_down", "editor_move_down", "Move down"),
    ("sort_lines", "editor_sort_lines", "Sort lines"),
    ("trim_spaces", "editor_trim_spaces", "Trim spaces"),
    ("tabs_to_spaces", "editor_tabs_to_spaces", "Tabs to spaces"),
)

# Refactoring/cleanup actions are deliberately implemented with the standard
# library only.  They are conservative: when the Python AST cannot prove that
# an edit is safe, the action leaves the buffer unchanged and reports why.
EDITOR_REFACTOR_ACTIONS = (
    ("rename", "editor_refactor_rename", "Rename symbol"),
    ("extract_function", "editor_refactor_extract_function", "Extract function"),
    ("extract_variable", "editor_refactor_extract_variable", "Extract variable"),
    ("move_function", "editor_refactor_move_function", "Move function"),
    ("move_class", "editor_refactor_move_class", "Move class"),
)

EDITOR_CLEANUP_ACTIONS = (
    ("format_code", "editor_format_code", "Format code"),
    ("remove_unused_imports", "editor_remove_unused_imports", "Remove unused imports"),
    ("remove_dead_code", "editor_remove_dead_code", "Remove dead code"),
    ("fix_linter", "editor_fix_linter", "Fix linter errors"),
)

# Code generation: each entry is the generator key it runs, so the menu is
# built from the engine's own catalogue (see tabs/code_generator.py).
EDITOR_CODEGEN_ACTIONS = tuple(
    (key, label_key, fallback)
    for key, label_key, fallback, _reads, _gives in code_generator.CODE_GENERATOR_KINDS)

# Code rules: the automatic linter + fixer from tabs/code_rules.py, offered as
# one menu of individually-runnable rules plus the two bulk actions.
EDITOR_CODERULES_ACTIONS = tuple(
    (key, label_key, fallback)
    for key, label_key, fallback, _kind, _detail in code_rules.CODE_RULES)

# Script profiles: whole-script shapes from tabs/script_profiles.py.  The menu
# is built from the engine's own catalogue, so a profile registered at run time
# shows up here without a change to this file.
EDITOR_PROFILE_ACTIONS = tuple(
    (key, label_key, fallback)
    for key, label_key, fallback, _detail in script_profiles.SCRIPT_PROFILES)

# Script Analyzer Integration: each action analyses the live editor buffer,
# never executes it, and presents the structured analyzer result locally.
EDITOR_ANALYSIS_ACTIONS = (
    ("syntax", "editor_analysis_syntax", "Syntax analysis"),
    ("ast", "editor_analysis_ast", "AST analysis"),
    ("semantic", "editor_analysis_semantic", "Semantic analysis"),
    ("security", "editor_analysis_security", "Security analysis"),
    ("quality", "editor_analysis_quality", "Code quality analysis"),
    ("runtime", "editor_analysis_runtime", "Runtime simulation"),
    ("dependencies", "editor_analysis_dependencies", "Dependency analysis"),
    ("changes", "editor_analysis_changes", "Change analysis (diff)"),
    ("errors", "editor_analysis_errors", "Error analysis"),
    ("metrics", "editor_metrics_menu", "Code metrics"),
)

# The analysis surface: the Script Analyzer Integration, stated once as data.
# Each entry is (action key, label key, fallback, what the analysis reads,
# which analyzer result sections carry it). The action key is the one
# `run_analysis` dispatches on, and the sections are the ones
# `_populate_analysis_tree` renders, so the table cannot claim a dimension the
# editor does not actually show.
EDITOR_ANALYSIS = (
    ("syntax", "editor_analysis_syntax", "Syntax analysis",
     "parse the buffer and report the syntax tree and any syntax errors",
     ("syntax",)),
    ("ast", "editor_analysis_ast", "AST analysis",
     "walk the parsed tree: nodes, definitions and their nesting",
     ("ast",)),
    ("semantic", "editor_analysis_semantic", "Semantic analysis",
     "what the names in the buffer mean, not just where they appear",
     ("issues",)),
    ("security", "editor_analysis_security", "Security analysis",
     "risky constructs (eval, exec, shell calls) the buffer contains",
     ("runtime",)),
    ("quality", "editor_analysis_quality", "Code quality analysis",
     "metrics, style points and issues, read together",
     ("metrics", "style", "issues")),
    ("runtime", "editor_analysis_runtime", "Runtime simulation",
     "a static simulation of the run: no code is executed",
     ("runtime", "runtime_summary")),
    ("dependencies", "editor_analysis_dependencies", "Dependency analysis",
     "the imports and the modules the buffer depends on",
     ("dependencies", "dependency_summary")),
    ("changes", "editor_analysis_changes", "Change analysis (diff)",
     "what changed in the buffer against its recorded history",
     ("history", "history_summary")),
    ("errors", "editor_analysis_errors", "Error analysis",
     "errors and warnings, gathered and ranked by severity",
     ("issues", "syntax")),
    ("metrics", "editor_metrics_menu", "Code metrics",
     "the eight headline metrics for the buffer", ()),
)

EDITOR_ANALYSIS_KEYS = tuple(key for key, _lk, _f, _d, _s in EDITOR_ANALYSIS)
EDITOR_ANALYSIS_MEANINGS = {key: detail
                            for key, _lk, _f, detail, _s in EDITOR_ANALYSIS}
EDITOR_ANALYSIS_SECTIONS = {key: sections
                            for key, _lk, _f, _d, sections in EDITOR_ANALYSIS}

EDITOR_INTEGRATION_ACTIONS = (
    ("diff", "editor_integration_diff", "Open in Diff"),
    ("search", "editor_integration_search", "Open in Search"),
    ("audit", "editor_integration_audit", "Open in Audit"),
    ("runner", "editor_integration_runner", "Open in Runner"),
    ("debugger", "editor_integration_debugger", "Open in Debugger"),
    ("report", "editor_integration_report", "Generate script report"),
    ("project", "editor_integration_project", "Link to project"),
)

# The integration surface, stated once as data: where the live script can be
# sent and what each target is reached through. Each entry is
# (key, label key, fallback, what the target receives, the window attribute or
# method it is reached through, what it gives back). The key is the one
# `run_integration` dispatches on, so the table cannot name a target the editor
# does not actually reach.
EDITOR_INTEGRATIONS = (
    ("diff", "editor_integration_diff", "Open in Diff",
     "the live buffer as the right-hand source, and the stored script as the "
     "left", "window.diff_tab", "a side-by-side comparison"),
    ("search", "editor_integration_search", "Open in Search",
     "the script's name as the query", "window.search_tab",
     "every script and document that contains the name"),
    ("audit", "editor_integration_audit", "Open in Audit",
     "the stored script as a change-log entry", "window.audit_script",
     "the script recorded on the audit timeline"),
    ("runner", "editor_integration_runner", "Open in Runner",
     "the live script as something to execute", "RunScriptDialog",
     "a run dialog over it, with its output"),
    ("debugger", "editor_integration_debugger", "Open in Debugger",
     "the live script as the script to inspect",
     "window.open_in_debugger / window.debugger_tab",
     "a session over it, with the script's lines as frames"),
    ("report", "editor_integration_report", "Generate script report",
     "the live script as a report body", "a save dialog",
     "a Markdown report artifact the Reports tab indexes"),
    ("project", "editor_integration_project", "Link to project",
     "the project name the script is tied to", "window.save_data_now",
     "the script attached to a project"),
)

EDITOR_INTEGRATION_KEYS = tuple(key for key, _lk, _f, _r, _t, _g
                                in EDITOR_INTEGRATIONS)
EDITOR_INTEGRATION_MEANINGS = {key: gives for key, _lk, _f, _r, _t, gives
                               in EDITOR_INTEGRATIONS}
EDITOR_INTEGRATION_TARGETS = {key: target for key, _lk, _f, _r, target, _g
                              in EDITOR_INTEGRATIONS}

EDITOR_ARCHITECTURE = (
    ("ascii", "editor_arch_ascii", "ASCII-only", "Printable ASCII output and identifiers."),
    ("hot_pluggable", "editor_arch_hot", "Hot-pluggable", "Syntax modules and integrations can be registered independently."),
    ("zero_dependency", "editor_arch_zero", "Zero-dependency", "Python standard library and PyQt5 only."),
    ("cross_platform", "editor_arch_cross", "Cross-platform", "Windows, macOS and Linux paths are supported."),
    ("sandbox_safe", "editor_arch_sandbox", "Sandbox-safe", "Analysis is static and does not execute user code."),
    ("plugin_ready", "editor_arch_plugin", "Plugin-ready", "Extension points can observe editor operations."),
    ("artefact_driven", "editor_arch_artefact", "Artefact-driven", "Reports, snapshots, hashes and histories are exportable."),
    ("api_cli_gui", "editor_arch_frontends", "API + CLI + GUI", "The editor integrates with the application API and GUI."),
    ("events", "editor_arch_events", "Event bus integration", "Save, access and analysis actions can be audited."),
    ("snapshots", "editor_arch_snapshots", "Snapshot integration", "Project snapshots store file paths and SHA-256 hashes."),
    ("audit_reports", "editor_arch_audit_reports", "Audit and reports", "Editor activity integrates with audit history and reports."),
)

EDITOR_WORKSPACE_ACTIONS = (
    ("architecture", "editor_architecture_menu", "Architecture"),
    ("project_tree", "editor_workspace_tree", "Project tree"),
    ("open_file", "editor_workspace_open", "Open file"),
    ("recent_files", "editor_workspace_recent_files", "Recent files"),
    ("recent_projects", "editor_workspace_recent_projects", "Recent projects"),
    ("snapshot", "editor_workspace_snapshot", "Create project snapshot"),
    ("file_history", "editor_workspace_file_history", "File change history"),
    ("project_history", "editor_workspace_project_history", "Project change history"),
    ("sandbox", "editor_security_sandbox", "Run in sandbox"),
    ("readonly", "editor_security_readonly", "Toggle read-only lock"),
    ("hash", "editor_security_hash", "Show hash signature"),
    ("verify", "editor_security_verify", "Verify unauthorized changes"),
    ("access_log", "editor_security_access", "File access audit"),
)

# The workflow surface: building a script from stages (the workflow builder) or
# from blocks (the composer), both in tabs/workflow_engine.py.  Each entry is
# (key, label key, fallback); the key is the one `run_workflow_action`
# dispatches on, so the table cannot name an action the editor does not run.
EDITOR_WORKFLOW_ACTIONS = (
    ("build", "editor_workflow_build", "Build workflow..."),
    ("compose", "editor_workflow_compose", "Compose from blocks..."),
    ("blocks", "editor_workflow_blocks", "Block catalogue"),
    ("safeguards", "editor_workflow_safeguards", "Add safeguards"),
    ("measure", "editor_workflow_measure", "Measure this script"),
)

# The metadata surface: the script's own header, read and written through
# tabs/metadata_core.py.  Each entry is (key, label key, fallback); the key is
# the one `run_metadata_action` dispatches on, so the table cannot name an
# action the editor does not actually run.
EDITOR_METADATA_ACTIONS = (
    ("read", "editor_meta_read", "Show metadata"),
    ("edit", "editor_meta_edit", "Edit metadata..."),
    ("insert", "editor_meta_insert", "Insert metadata header"),
    ("validate", "editor_meta_validate", "Validate metadata"),
    ("stamp", "editor_meta_stamp", "Stamp modified"),
    ("bump", "editor_meta_bump", "Bump version"),
    ("strip", "editor_meta_strip", "Remove metadata header"),
)

# The file-system and project surface, stated once as data: what the editor
# offers for working with files and whole projects on disk. Each entry is
# (key, label key, fallback, what it reads, what it gives back); the key is the
# one `run_workspace_action` dispatches on, so the table cannot name a feature
# the editor does not actually run.
EDITOR_FILE_SYSTEM = (
    ("project_tree", "editor_workspace_tree", "Project tree",
     "a directory chosen by the user",
     "the whole tree of that project, every file openable by double-click"),
    ("open_file", "editor_workspace_open", "Quick open file",
     "a file chosen by the user",
     "the file loaded into the editor and remembered as recent"),
    ("recent_files", "editor_workspace_recent_files", "Recently used files",
     "the last 15 files opened",
     "a picker that reopens one of them"),
    ("recent_projects", "editor_workspace_recent_projects",
     "Recently used projects", "the last 10 project roots opened",
     "a picker that reopens one project's tree"),
    ("snapshot", "editor_workspace_snapshot", "Project snapshots",
     "every file below the current project root",
     "a JSON manifest of paths and SHA-256 hashes that can be verified later"),
    ("file_history", "editor_workspace_file_history", "File change history",
     "the selected script's recorded history",
     "every revision of that one file, newest readable"),
    ("project_history", "editor_workspace_project_history",
     "Project change history", "the editor's file access log",
     "every file read or written in this session, in order"),
)

EDITOR_FILE_SYSTEM_KEYS = tuple(key for key, _lk, _f, _r, _g
                                in EDITOR_FILE_SYSTEM)
EDITOR_FILE_SYSTEM_MEANINGS = {key: gives for key, _lk, _f, _r, gives
                               in EDITOR_FILE_SYSTEM}
EDITOR_FILE_SYSTEM_READS = {key: reads for key, _lk, _f, reads, _g
                            in EDITOR_FILE_SYSTEM}
# How many entries each recent list keeps before the oldest is dropped.
RECENT_FILES_LIMIT = 15
RECENT_PROJECTS_LIMIT = 10
# The editor toolbox: the refactoring and code-cleanup tools, gathered into one
# surface so an "about" page or a plugin can enumerate them without reading the
# three menus. Each entry is (group, action key, label key, fallback, meaning);
# the action key is the one `apply_tool` dispatches on, so the table cannot name
# a tool the editor does not actually run.
EDITOR_TOOLBOX = (
    # --- refactoring -------------------------------------------------------
    ("refactor", "rename", "editor_refactor_rename", "Rename symbol",
     "rename every occurrence of the symbol under the cursor"),
    ("refactor", "extract_function", "editor_refactor_extract_function",
     "Extract function",
     "turn the selected lines into a function and call it in their place"),
    ("refactor", "extract_variable", "editor_refactor_extract_variable",
     "Extract variable",
     "bind the selected expression to a new variable above the line"),
    ("refactor", "move_function", "editor_refactor_move_function",
     "Move function",
     "relocate a function definition to a chosen line in the buffer"),
    ("refactor", "move_class", "editor_refactor_move_class", "Move class",
     "relocate a class definition to a chosen line in the buffer"),
    # --- code cleanup ------------------------------------------------------
    ("cleanup", "format_code", "editor_format_code", "Format code",
     "normalise indentation, trailing spaces and blank lines"),
    ("cleanup", "remove_unused_imports", "editor_remove_unused_imports",
     "Remove unused imports",
     "drop imports whose bound names are never referenced"),
    ("cleanup", "remove_dead_code", "editor_remove_dead_code",
     "Remove dead code",
     "drop statements after an unconditional return, raise, break or continue"),
    ("cleanup", "fix_linter", "editor_fix_linter", "Fix linter errors",
     "apply the common, unambiguous lint fixes in one pass"),
)

EDITOR_TOOLBOX_GROUPS = ("refactor", "cleanup")
EDITOR_TOOLBOX_KEYS = tuple(key for _g, key, _lk, _f, _d in EDITOR_TOOLBOX)
EDITOR_TOOLBOX_LABELS = {key: fallback
                         for _g, key, _lk, fallback, _d in EDITOR_TOOLBOX}
EDITOR_TOOLBOX_MEANINGS = {key: meaning
                           for _g, key, _lk, _f, meaning in EDITOR_TOOLBOX}


def _python_ast(code):
    'Parse Python source, returning (tree, None) or (None, error).'
    try:
        return ast.parse(code or ""), None
    except (SyntaxError, ValueError) as exc:
        return None, exc

def _node_lines(node):
    start = getattr(node, "lineno", 1) - 1
    end = getattr(node, "end_lineno", getattr(node, "lineno", 1))
    return start, end

def _python_names(code):
    tree, error = _python_ast(code)
    if tree is None:
        return [], error
    names = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append((node.name, "definition", node.lineno))
        elif isinstance(node, ast.Name):
            names.append((node.id, "variable", node.lineno))
    return names, None

def format_python_code(code):
    'Apply safe, dependency-free Python formatting to a source buffer.'
    lines = (code or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    result = []
    blank = 0
    for line in lines:
        line = line.expandtabs(4).rstrip()
        if not line.strip():
            blank += 1
            if blank <= 2:
                result.append("")
            continue
        blank = 0
        result.append(line)
    while result and not result[-1].strip():
        result.pop()
    return "\n".join(result) + ("\n" if result else "")


def remove_unused_python_imports(code):
    'Remove imports whose bound names are never referenced elsewhere.'
    tree, error = _python_ast(code)
    if tree is None:
        return code, 0, error
    used = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)}
    lines = (code or "").splitlines()
    remove = set()
    for node in tree.body:
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        aliases = []
        for alias in node.names:
            bound = alias.asname or alias.name.split(".")[0]
            if bound not in used:
                aliases.append(alias)
        if aliases and len(aliases) == len(node.names):
            start, end = _node_lines(node)
            remove.update(range(start, end))
    kept = [line for index, line in enumerate(lines) if index not in remove]
    return "\n".join(kept) + ("\n" if code.endswith("\n") else ""), len(remove), None

def remove_dead_python_code(code):
    'Remove statements after an unconditional return/raise/break/continue.'
    tree, error = _python_ast(code)
    if tree is None:
        return code, 0, error
    lines = (code or "").splitlines()
    remove = set()
    for parent in ast.walk(tree):
        body = getattr(parent, "body", None)
        if not isinstance(body, list):
            continue
        for index, node in enumerate(body[:-1]):
            if isinstance(node, (ast.Return, ast.Raise, ast.Break, ast.Continue)):
                for dead in body[index + 1:]:
                    start, end = _node_lines(dead)
                    remove.update(range(start, end))
                break
    kept = [line for index, line in enumerate(lines) if index not in remove]
    return "\n".join(kept) + ("\n" if code.endswith("\n") else ""), len(remove), None

def fix_python_linter(code):
    'Fix common, unambiguous lint issues without executing user code.'
    text = format_python_code(code)
    text = re.sub(r"(?<![=!])==\s*None\b", "is None", text)
    text = re.sub(r"!=\s*None\b", "is not None", text)
    text = re.sub(r"(?<![=!])==\s*True\b", "is True", text)
    text = re.sub(r"(?<![=!])==\s*False\b", "is False", text)
    text, removed, _error = remove_unused_python_imports(text)
    return text, removed


# Search-row controls: (key, label key, fallback).
EDITOR_SEARCH_OPTIONS = (
    ("case_sensitive", "search_case", "Case"),
    ("definitions", "search_mark_definitions", "Mark definitions"),
    ("include_methods", "search_include_methods", "Method calls"),
)

# View toggles offered by the View row: (key, label key, fallback, default).
EDITOR_VIEWS = (
    ("line_numbers", "editor_view_linenumbers", "Line numbers", True),
    ("current_line", "editor_view_currentline", "Current line", True),
    ("brackets", "editor_view_brackets", "Bracket pairs", True),
    ("minimap", "editor_view_minimap", "Minimap", True),
    ("auto_close", "editor_view_autoclose", "Auto-close", True),
    ("smart_indent", "editor_view_smartindent", "Smart indent", True),
    ("suggestions", "editor_view_suggestions", "Suggestions", True),
)

# Smart suggestion kinds: (key, label key, fallback). Used by the completion
# popup so every suggestion is clearly labelled with what it offers.
EDITOR_SUGGESTION_KINDS = (
    ("function", "editor_sugg_function", "function"),
    ("class", "editor_sugg_class", "class"),
    ("method", "editor_sugg_method", "method"),
    ("variable", "editor_sugg_variable", "variable"),
    ("import", "editor_sugg_import", "import"),
    ("parameter", "editor_sugg_parameter", "parameter"),
    ("type", "editor_sugg_type", "type"),
    ("keyword", "editor_sugg_keyword", "keyword"),
    ("context", "editor_sugg_context", "context"),
)

# Two ASCII-friendly palettes. Every colour is a plain hex string so the same
# definitions work with or without a desktop theme.
EDITOR_THEMES = {
    "dark": {
        "background": "#1e1e1e", "text": "#dfe7f5",
        "gutter_bg": "#252526", "gutter_fg": "#9aa0a6",
        "gutter_fg_current": "#ffffff",
        "current_line": "#2a2d2e", "selection": "#264f78",
        "keyword": "#569cd6", "builtin": "#4ec9b0",
        "string": "#ce9178", "comment": "#6a9955",
        "number": "#b5cea8", "decorator": "#c586c0",
        "bracket": "#ffd75f", "minimap_bg": "#181818",
        "minimap_fg": "#4a4d50", "minimap_hit": "#8ab4f8",
    },
    "light": {
        "background": "#ffffff", "text": "#1f1f1f",
        "gutter_bg": "#f3f3f3", "gutter_fg": "#808080",
        "gutter_fg_current": "#000",
        "current_line": "#eef3fb", "selection": "#b5d6ff",
        "keyword": "#0000ff", "builtin": "#267f99",
        "string": "#a31515", "comment": "#008000",
        "number": "#098658", "decorator": "#af00db",
        "bracket": "#b8860b", "minimap_bg": "#f7f7f7",
        "minimap_fg": "#c8c8c8", "minimap_hit": "#2b6cb0",
    },
}

# Low-graphics palette: monochrome, ASCII-only, minimal (no colour rules).
EDITOR_LOW_GRAPHICS = {
    "background": "#000", "text": "#e6e6e6",
    "gutter_bg": "#111", "gutter_fg": "#888",
    "gutter_fg_current": "#ffffff",
    "current_line": "#222", "selection": "#333",
}

# Comment markers used when a module cannot be resolved, so the editor still
# degrades gracefully instead of losing colouring completely.
EDITOR_COMMENT_MARKERS = ("#", "//", "'", ";", "--", "rem", "<!--")


def _now():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def languages():
    """(key, label) pairs of every language the editor can edit."""
    return syntax.languages()


def language_key(category):
    """The language key a script category maps to (never empty)."""
    return syntax.key_for(category)


def syntax_module(category):
    """The hot-pluggable syntax module used for a category (never None)."""
    return syntax.module_for(category)


def _comment_marker(category):
    """The single-line comment marker for a category."""
    return syntax_module(category).comment_marker(category)


def highlight_block(text, category, theme, low_graphics=False):
    """Return a list of (start, length, colour) spans for one source line. Delegates to the syntax module registered for ``category``, so every language colours with its own rules (comments, strings, keywords, numbers). In low-graphics mode every span is dropped so the view stays minimal."""
    return syntax.highlight_spans(text, category, theme, low_graphics)


class LineNumberArea(QtWidgets.QWidget):
    """Gutter that paints the line numbers next to the code editor."""

    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor
        self.setFont(QFont(EDITOR_FONT, EDITOR_FONT_SIZE))

    def sizeHint(self):
        return QtCore.QSize(self.editor.line_number_width(), 0)

    def paintEvent(self, event):
        # Delegated so the editor owns the colour logic.
        self.editor.paint_line_numbers(event)
class CodeMinimap(QtWidgets.QWidget):
    """A narrow, ASCII-friendly overview of the whole document. Each line becomes one clickable row: the current line is marked and the viewport (the lines visible in the editor) is highlighted, so the minimap doubles as a scrollbar. It renders without images, so it works in low-graphics mode too."""

    DEFAULT_WIDTH = 90
    MIN_WIDTH = 60
    MAX_WIDTH = 200

    def __init__(self, editor, width=None):
        super().__init__(editor)
        self.editor = editor
        self._width = width if width is not None else self.DEFAULT_WIDTH
        self.setFixedWidth(self._width)
        self.setSizePolicy(QtWidgets.QSizePolicy.Fixed,
                           QtWidgets.QSizePolicy.Expanding)
        self.setToolTip("Minimap (click to jump, drag to scroll)")
        self.setCursor(QtCore.Qt.PointingHandCursor)
        self._dragging = False
        self._drag_start_y = 0
        self._drag_start_line = 0
        self._cached_lines = None
        self._cache_timestamp = 0
        self._last_line_count = 0
        self._hover_line = -1
        self.setMouseTracking(True)
        self._scroll_timer = QtCore.QTimer(self)
        self._scroll_timer.setSingleShot(True)
        self._scroll_timer.timeout.connect(self._smooth_scroll)
        self._target_line = None

    def set_width(self, width):
        """Set the minimap width (clamped to MIN_WIDTH and MAX_WIDTH)."""
        self._width = max(self.MIN_WIDTH, min(self.MAX_WIDTH, width))
        self.setFixedWidth(self._width)
        self.update()

    def width(self):
        """Return the current minimap width."""
        return self._width

    def refresh(self):
        self._invalidate_cache()
        self.update()

    def _invalidate_cache(self):
        """Invalidate the cached line data."""
        self._cached_lines = None
        self._last_line_count = 0

    def _get_lines(self):
        """Get lines with caching to avoid repeated string splits."""
        current_line_count = self.editor.blockCount()
        if (self._cached_lines is None or 
            current_line_count != self._last_line_count):
            self._cached_lines = self.editor.toPlainText().split("\n")
            self._last_line_count = current_line_count
        return self._cached_lines

    def paintEvent(self, event):
        theme = self.editor.theme()
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing, False)
        try:
            painter.fillRect(self.rect(), QtGui.QColor(theme.get(
                "minimap_bg", "#181818")))
            lines = self._get_lines()
            total = max(1, len(lines))
            height = max(1, self.height())
            row_h = max(1.0, height / total)
            fg = QtGui.QColor(theme.get("minimap_fg", "#4a4d50"))
            # The visible window in the editor, drawn as a soft band.
            first = self.editor.firstVisibleBlock().blockNumber()
            visible = self.editor.visible_block_count()
            band_top = int(first * row_h)
            band_h = max(2, int(visible * row_h))
            painter.fillRect(0, band_top, self._width, band_h,
                             QtGui.QColor(theme.get("current_line", "#2a2d2e")))
            painter.setPen(QtGui.QPen(fg, 1))
            
            # Only draw lines that are visible in the viewport for performance
            visible_start = max(0, first - 10)
            visible_end = min(total, first + visible + 10)
            
            for index in range(visible_start, visible_end):
                if index >= len(lines):
                    break
                line = lines[index]
                if not line.strip():
                    continue
                y = int(index * row_h)
                width = min(self._width - 4, max(2, int(len(line) * 0.9)))
                painter.drawLine(2, y, 2 + width, y)
            
            # Mark the current line.
            current = self.editor.textCursor().blockNumber()
            painter.fillRect(0, int(current * row_h), self._width,
                             max(1, int(row_h)),
                             QtGui.QColor(theme.get("minimap_hit", "#8ab4f8")))
            
            # Mark the hovered line with a subtle highlight.
            if self._hover_line >= 0 and self._hover_line != current:
                hover_color = QtGui.QColor(theme.get("minimap_hit", "#8ab4f8"))
                hover_color.setAlpha(80)
                painter.fillRect(0, int(self._hover_line * row_h), self._width,
                               max(1, int(row_h)), hover_color)
        finally:
            painter.end()

    def _smooth_scroll(self):
        """Perform smooth scroll to target line."""
        if self._target_line is not None:
            self.editor.goto_line(self._target_line)
            self._target_line = None

    def mousePressEvent(self, event):
        """Handle mouse press - start drag or jump to line."""
        lines = self._get_lines()
        total = max(1, len(lines))
        ratio = event.pos().y() / max(1, self.height())
        line = max(0, min(total - 1, int(ratio * total)))
        
        if event.button() == QtCore.Qt.LeftButton:
            self._dragging = True
            self._drag_start_y = event.pos().y()
            self._drag_start_line = line
            self._hover_line = -1
            self._target_line = line
            self._scroll_timer.start(50)
        else:
            self.editor.goto_line(line)

    def mouseMoveEvent(self, event):
        """Handle mouse move - scroll if dragging or update hover."""
        lines = self._get_lines()
        total = max(1, len(lines))
        ratio = event.pos().y() / max(1, self.height())
        line = max(0, min(total - 1, int(ratio * total)))
        
        if self._dragging:
            self._target_line = line
            self._scroll_timer.start(16)
        else:
            if line != self._hover_line:
                self._hover_line = line
                self.update()

    def mouseReleaseEvent(self, event):
        """Handle mouse release - stop dragging."""
        if event.button() == QtCore.Qt.LeftButton:
            self._dragging = False
            self._scroll_timer.stop()
            self._target_line = None

    def leaveEvent(self, event):
        """Handle leaving the widget - stop dragging and clear hover."""
        self._dragging = False
        self._hover_line = -1
        self.update()

    def enterEvent(self, event):
        """Handle entering the widget - reset hover state."""
        self._hover_line = -1
        super().enterEvent(event)
BUILTIN_TYPES = ("int", "float", "str", "bool", "list", "dict", "set",
                 "tuple", "bytes", "object", "None", "Any", "Optional",
                 "List", "Dict", "Set", "Tuple", "Callable")


class Suggestion:
    """One smart suggestion offered by the completion popup."""

    def __init__(self, text, kind, detail="", insert=None):
        self.text = text
        self.kind = kind
        self.detail = detail
        # What gets inserted (falls back to the visible text).
        self.insert = insert if insert is not None else text

    def label(self):
        """The single-line label shown in the popup (ASCII only)."""
        if self.detail:
            return "%s  -  %s" % (self.text, self.detail)
        return self.text


class SuggestionEngine:
    """Build smart suggestions for a code body. Uses the real Python AST when it can parse the code (context-aware), and a light regex scan otherwise, so it stays useful for every supported language. It never executes the code (sandbox-safe)."""

    MAX_SOURCE = 200000        # refuse absurd buffers (keeps the UI snappy)

    def __init__(self, code="", category="python", builtins=None):
        self.code = code or ""
        self.category = category or "Python"
        # The syntax module of the active language supplies the vocabulary.
        self.module = syntax_module(self.category)
        self.builtins = builtins if builtins is not None \
            else (self.module.builtins or ())
        self._suggestions = []
        self.parse()

    # ------------------------------------------------------------- parsing
    def parse(self):
        """(Re)build the suggestion list from the current code."""
        self._suggestions = []
        if len(self.code) > self.MAX_SOURCE:
            self.code = self.code[:self.MAX_SOURCE]
        found = {}
        if self.module.key == "python":
            try:
                self._parse_python_ast(found)
            except (SyntaxError, ValueError):
                self._parse_regex(found)
        else:
            self._parse_regex(found)
        self._add_keywords(found)
        # Order: richer kinds first so functions/classes lead the list.
        order = {"class": 0, "function": 1, "method": 2, "parameter": 3,
                 "import": 4, "variable": 5, "type": 6, "keyword": 7,
                 "context": 8}
        self._suggestions = sorted(
            found.values(),
            key=lambda s: (order.get(s.kind, 9), s.text.lower()))

    def _add(self, found, text, kind, detail="", insert=None):
        if not text:
            return
        key = (text, kind)
        if key not in found:
            found[key] = Suggestion(text, kind, detail, insert)

    # --- Python AST: context-aware symbols -------------------------------
    def _parse_python_ast(self, found):
        import ast as _ast
        tree = _ast.parse(self.code)
        # Imports first so imported names can be suggested too.
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Import):
                for alias in node.names:
                    name = alias.asname or alias.name.split(".")[0]
                    self._add(found, name, "import",
                              "import %s" % alias.name)
            elif isinstance(node, _ast.ImportFrom):
                module = node.module or ""
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    name = alias.asname or alias.name
                    self._add(found, name, "import",
                              "from %s import %s" % (module, alias.name))
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ClassDef):
                base = ""
                if node.bases:
                    try:
                        base = _ast.unparse(node.bases[0])
                    except Exception:
                        base = ""
                self._add(found, node.name, "class",
                          "class%s" % ("(%s)" % base if base else ""))
                for child in node.body:
                    if isinstance(child, (_ast.FunctionDef,
                                          _ast.AsyncFunctionDef)):
                        self._add(found, child.name, "method",
                                  "method of %s" % node.name)
            elif isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                args = self._arg_names(node.args)
                self._add(found, node.name, "function",
                          "def %s(%s)" % (node.name, ", ".join(args)))
                for arg in args:
                    if arg in ("self", "cls"):
                        continue
                    self._add(found, arg, "parameter",
                              "parameter of %s" % node.name)
            elif isinstance(node, _ast.arg):
                # Function parameters anywhere (lambdas included).
                self._add(found, node.arg, "parameter", "parameter")
            elif isinstance(node, _ast.Assign):
                for target in node.targets:
                    if isinstance(target, _ast.Name):
                        detail = "variable"
                        if node.value is not None:
                            detail = self._infer_type(node.value)
                        self._add(found, target.id, "variable", detail)
            elif isinstance(node, _ast.AnnAssign):
                if isinstance(node.target, _ast.Name):
                    try:
                        ann = _ast.unparse(node.annotation)
                    except Exception:
                        ann = "?"
                    self._add(found, node.target.id, "variable", ann)
            elif isinstance(node, (_ast.For,)):
                if isinstance(node.target, _ast.Name):
                    self._add(found, node.target.id, "variable",
                              "loop variable")
            elif isinstance(node, _ast.With):
                for item in node.items:
                    if item.optional_vars is not None and \
                            isinstance(item.optional_vars, _ast.Name):
                        self._add(found, item.optional_vars.id, "variable",
                                  "with ... as")

    @staticmethod
    def _arg_names(arguments):
        """Flatten an ast.arguments node into a list of parameter names."""
        names = [a.arg for a in getattr(arguments, "posonlyargs", [])]
        names += [a.arg for a in getattr(arguments, "args", [])]
        if getattr(arguments, "vararg", None) is not None:
            names.append(arguments.vararg.arg)
        names += [a.arg for a in getattr(arguments, "kwonlyargs", [])]
        if getattr(arguments, "kwarg", None) is not None:
            names.append(arguments.kwarg.arg)
        return names
    @staticmethod
    def _infer_type(value_node):
        """A short type hint for a variable initialiser."""
        import ast as _ast
        node = value_node
        if isinstance(node, _ast.Constant):
            return type(node.value).__name__
        if isinstance(node, _ast.List):
            return "list"
        if isinstance(node, _ast.Dict):
            return "dict"
        if isinstance(node, _ast.Set):
            return "set"
        if isinstance(node, _ast.Tuple):
            return "tuple"
        if isinstance(node, _ast.Call):
            func = node.func
            if isinstance(func, _ast.Name):
                return "%s()" % func.id
            if isinstance(func, _ast.Attribute):
                return "%s()" % func.attr
        return "variable"

    # --- Regex fallback for non-Python / malformed code ------------------
    def _parse_regex(self, found):
        text = self.code
        for match in re.finditer(r"^\s*class\s+(\w+)", text, re.MULTILINE):
            self._add(found, match.group(1), "class", "class")
        for match in re.finditer(
                r"^\s*(?:def|function|sub|func)\s+(\w+)\s*\(([^)]*)\)",
                text, re.MULTILINE | re.IGNORECASE):
            name, params = match.group(1), match.group(2)
            self._add(found, name, "function", "def %s(...)" % name)
            for param in params.split(","):
                param = param.split("=")[0].strip()
                if param and param not in ("self", "cls"):
                    self._add(found, param, "parameter",
                              "parameter of %s" % name)
        for match in re.finditer(
                r"^\s*(?:import|from|using|require)\s+([\w./\\-]+)",
                text, re.MULTILINE | re.IGNORECASE):
            name = match.group(1).split(".")[-1].strip(";")
            if name:
                self._add(found, name, "import", "import %s" % match.group(1))
        for match in re.finditer(r"^\s*(\w+)\s*=", text, re.MULTILINE):
            self._add(found, match.group(1), "variable", "variable")
        for name in BUILTIN_TYPES:
            self._add(found, name, "type", "type")

    def _add_keywords(self, found):
        for word in self.module.keywords:
            self._add(found, word, "keyword", "keyword")
        for name in sorted(self.builtins):
            self._add(found, name, "type" if name in BUILTIN_TYPES
                      else "variable", "built-in")

    # --------------------------------------------------------------- query
    def all(self):
        return list(self._suggestions)

    def suggest(self, prefix, limit=40):
        """Suggestions starting with `prefix` (case-insensitive), best first."""
        prefix = (prefix or "").lower()
        if not prefix:
            return self._suggestions[:limit]
        starts = []
        contains = []
        for suggestion in self._suggestions:
            lowered = suggestion.text.lower()
            if lowered.startswith(prefix):
                starts.append(suggestion)
            elif prefix in lowered:
                contains.append(suggestion)
        return (starts + contains)[:limit]

    def context_suggestions(self, line_text=""):
        """Context-aware hints derived from the prefix of the current line. e.g. after `import ` we offer imports, after `class ` we offer a class skeleton, after `def ` a function skeleton -- all ASCII-only."""
        hints = []
        stripped = (line_text or "").strip()
        # Note: match on the keyword itself; the trailing space is stripped.
        if stripped == "import" or stripped.startswith(("import ", "from ")):
            hints.append(Suggestion("import module", "context",
                                    "import statement"))
            for suggestion in self._suggestions:
                if suggestion.kind == "import":
                    hints.append(suggestion)
        elif stripped.startswith(("class ", "class")):
            hints.append(Suggestion("class Name:", "context",
                                    "class definition", "class Name:"))
        elif stripped.startswith(("def ", "def", "function ", "function")):
            hints.append(Suggestion("def name(args):", "context",
                                    "function definition",
                                    "def name(args):"))
        elif stripped.endswith((".", "->")):
            for suggestion in self._suggestions:
                if suggestion.kind in ("method", "variable"):
                    hints.append(suggestion)
        return hints[:40]

class CodeEditor(QtWidgets.QPlainTextEdit):
    """A code editor with the Core Editor feature set. Features: full syntax colouring, smart indentation, bracket and block auto-closing, line numbers, an ASCII minimap, current-line highlight and bracket-pair highlight, plus dark/light and low-graphics visual modes."""

    def __init__(self, parent=None):
        super().__init__(parent)
        # The visual state must exist before theme() is called, and theme()
        # is needed to build the highlighter, so the order matters here.
        self._category = "Python"
        self._theme_name = "dark"
        self._low_graphics = False
        self._module = syntax_module(self._category)
        self._highlighter = self._module.highlighter(self.theme())
        self._views = {key: default for key, _lk, _f, default in EDITOR_VIEWS}
        self.setFont(QFont(EDITOR_FONT, EDITOR_FONT_SIZE))
        self.setTabChangesFocus(False)
        self.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
        self.setTabStopDistance(4 * self.fontMetrics().width(" "))
        self._line_area = LineNumberArea(self)
        self._minimap = CodeMinimap(self)
        self._bracket_positions = (None, None)
        # Smart suggestions: the engine is rebuilt on demand (see _engine).
        self._engine = None
        self._engine_stamp = None
        self._completer = None
        self._last_suggestions = []
        # Code navigation: the tab installs a handler that turns a jump request
        # into a cursor move, so the editor stays free of tab-level state.
        self._nav_handler = None
        self.blockCountChanged.connect(self._update_margins)
        self.updateRequest.connect(self._update_line_area)
        self.cursorPositionChanged.connect(self._update_extras)
        self.textChanged.connect(self._on_text_changed)
        self._install_nav_shortcuts()
        self._apply_theme()
        self._update_margins()

    # -------------------------------------------------- code navigation
    def set_nav_handler(self, handler):
        """Install the callback that performs a navigation jump. The handler is called as ``handler(kind)`` for the structural jumps, and ``handler(kind, direction)`` when a direction is needed."""
        self._nav_handler = handler
    def _nav_request(self, kind, direction=1):
        """Ask the tab to perform a jump (safe when no handler is installed)."""
        handler = self._nav_handler
        if handler is None:
            return False
        try:
            handler(kind, direction)
        except TypeError:
            handler(kind)
        except Exception:
            return False
        return True
    def _install_nav_shortcuts(self):
        """Bind F12 / Shift+F12 / Alt+arrow and Ctrl+click to the jumps."""
        shortcuts = (
            ("F12", lambda: self._nav_request("definition")),
            ("Shift+F12", lambda: self._nav_request("reference")),
            ("Alt+Up", lambda: self._nav_request("enclosing")),
            ("Alt+Left", lambda: self._nav_request("section", -1)),
            ("Alt+Right", lambda: self._nav_request("section", 1)),
            ("Alt+Down", lambda: self._nav_request(None, 1)),
            ("Alt+E", lambda: self._nav_request("error")),
            ("Alt+W", lambda: self._nav_request("warning")),
            ("Alt+C", lambda: self._nav_request("class")),
            ("Alt+F", lambda: self._nav_request("function")),
            ("Alt+V", lambda: self._nav_request("variable")),
            ("Alt+I", lambda: self._nav_request("import")),
        )
        for keys, callback in shortcuts:
            action = QtWidgets.QShortcut(QtGui.QKeySequence(keys), self)
            action.setContext(QtCore.Qt.WidgetWithChildrenShortcut)
            action.activated.connect(callback)

    def mouseReleaseEvent(self, event):
        """Ctrl+click follows the symbol under the pointer, like an IDE."""
        if event.modifiers() & QtCore.Qt.ControlModifier \
                and event.button() == QtCore.Qt.LeftButton:
            cursor = self.cursorForPosition(event.pos())
            self.setTextCursor(cursor)
            if self._nav_request("definition"):
                return
        super().mouseReleaseEvent(event)

    # ---------------------------------------------------------- properties
    def set_category(self, category):
        """Use the syntax module of the given category for this document."""
        self._category = category or "Python"
        self._module = syntax_module(self._category)
        self._highlighter = self._module.highlighter(
            self.theme(), self._low_graphics)
        self._engine = None
        self._engine_stamp = None
        self.rehighlight()

    def module(self):
        """Return the active, hot-pluggable syntax module."""
        return self._module
    def language_key(self):
        """Key of the active language, e.g. python or cpp."""
        return self._module.key
    def uses_syntax_module(self, key):
        """True when the given syntax key names the active module."""
        return self._module.key == (key or "").lower()

    def theme(self):
        """The active palette dict."""
        if self._low_graphics:
            return EDITOR_LOW_GRAPHICS
        return EDITOR_THEMES.get(self._theme_name, EDITOR_THEMES["dark"])

    def set_view(self, key, enabled):
        self._views[key] = bool(enabled)
        self._update_margins()
        self._minimap.setVisible(self._views.get("minimap", True))
        self._update_extras()

    def view_enabled(self, key):
        return bool(self._views.get(key, False))

    def set_theme(self, name):
        """Switch between the dark and light palettes."""
        if name in EDITOR_THEMES:
            self._theme_name = name
        self._apply_theme()

    def theme_name(self):
        return self._theme_name

    def set_low_graphics(self, enabled):
        """Toggle the minimal, monochrome, ASCII-only mode."""
        self._low_graphics = bool(enabled)
        self._apply_theme()

    def low_graphics(self):
        return self._low_graphics

    def minimap(self):
        return self._minimap

    # ------------------------------------------------------------- theming
    def _apply_theme(self):
        theme = self.theme()
        self.setStyleSheet(
            "QPlainTextEdit { background:%s; color:%s;"
            " selection-background-color:%s; }"
            % (theme["background"], theme["text"], theme["selection"]))
        self._line_area.update()
        self._minimap.update()
        self.rehighlight()
        self._update_extras()

    # ------------------------------------------------------------- gutter
    def line_number_width(self):
        if not self._views.get("line_numbers", True):
            return 0
        digits = max(3, len(str(max(1, self.blockCount()))))
        return 12 + self.fontMetrics().width("9") * digits

    def _update_margins(self, *_args):
        width = self.line_number_width()
        self.setViewportMargins(width, 0, self._minimap.width(), 0)

    def _update_line_area(self, rect, dy):
        if dy:
            self._line_area.scroll(0, dy)
            self._minimap.update()
        else:
            self._line_area.update(
                0, rect.y(), self._line_area.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self._update_margins()
        self._minimap.update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        contents = self.contentsRect()
        width = self.line_number_width()
        self._line_area.setGeometry(
            QtCore.QRect(contents.left(), contents.top(), width,
                         contents.height()))
        self._minimap.setGeometry(
            QtCore.QRect(contents.right() - self._minimap.width(),
                         contents.top(), self._minimap.width(),
                         contents.height()))

    def visible_block_count(self):
        """How many editor lines are currently visible (for the minimap)."""
        block_h = max(1, self.fontMetrics().height())
        return max(1, int(self.viewport().height() / block_h))

    def goto_line(self, line_no):
        block = self.document().findBlockByNumber(max(0, line_no))
        if not block.isValid():
            return
        cursor = QtGui.QTextCursor(block)
        self.setTextCursor(cursor)
        self.centerCursor()

    def goto_location(self, location, select=False):
        """Move the cursor onto a nav-engine location and reveal it. The location carries 1-based lines and 0-based columns, so it maps straight onto a Qt cursor. With `select` the whole span the engine reported is selected, which is what a symbol jump should do."""
        if location is None:
            return False
        block = self.document().findBlockByNumber(max(0, location.line - 1))
        if not block.isValid():
            return False
        cursor = QtGui.QTextCursor(block)
        start = block.position() + max(0, location.column)
        cursor.setPosition(min(start, block.position() + len(block.text())))
        if select and location.end_line:
            end_block = self.document().findBlockByNumber(
                max(0, location.end_line - 1))
            if end_block.isValid():
                end = end_block.position() + (location.end_column or 0)
                cursor.setPosition(min(end, end_block.position()
                                       + len(end_block.text())),
                                   QtGui.QTextCursor.KeepAnchor)
        self.setTextCursor(cursor)
        self.centerCursor()
        return True
    def cursor_line_column(self):
        """The 1-based line and 0-based column of the cursor."""
        cursor = self.textCursor()
        return cursor.blockNumber() + 1, cursor.positionInBlock()

    def document_snapshot(self):
        """The open buffer as a (name, code, category) document."""
        return ("(buffer)", self.toPlainText(), self._category)

    def paint_line_numbers(self, event):
        if not self._views.get("line_numbers", True):
            return
        theme = self.theme()
        painter = QtGui.QPainter(self._line_area)
        try:
            painter.fillRect(event.rect(),
                             QtGui.QColor(theme["gutter_bg"]))
            block = self.firstVisibleBlock()
            block_no = block.blockNumber()
            top = int(self.blockBoundingGeometry(block)
                      .translated(self.contentOffset()).top())
            bottom = top + int(self.blockBoundingRect(block).height())
            current = self.textCursor().blockNumber()
            while block.isValid() and top <= event.rect().bottom():
                if block.isVisible() and bottom >= event.rect().top():
                    colour = theme["gutter_fg_current"] \
                        if block_no == current else theme["gutter_fg"]
                    painter.setPen(QtGui.QColor(colour))
                    painter.drawText(
                        0, top, self._line_area.width() - 4,
                        self.fontMetrics().height(),
                        QtCore.Qt.AlignRight, str(block_no + 1))
                block = block.next()
                top = bottom
                bottom = top + int(self.blockBoundingRect(block).height())
                block_no += 1
        finally:
            painter.end()

    def _on_text_changed(self):
        self._minimap._invalidate_cache()
        self._minimap.update()

    # ----------------------------------------------------------- highlight
    def rehighlight(self):
        """Re-apply syntax colouring to the whole document."""
        theme = self.theme()
        # The active language module decides how this document is tokenised.
        self._module = syntax_module(self._category)
        self._highlighter = self._module.highlighter(theme, self._low_graphics)
        highlighter = self._highlighter
        colours = dict((kind, None) for kind in syntax.SPAN_KINDS)
        cursor = QtGui.QTextCursor(self.document())
        cursor.beginEditBlock()
        block = self.document().begin()
        while block.isValid():
            spans = highlighter.spans(block.text(), self._category)
            iterator = block.begin()
            while not iterator.atEnd():
                fragment = iterator.fragment()
                if fragment.isValid():
                    start = fragment.position() - block.position()
                    size = fragment.length()
                    fmt = QtGui.QTextCharFormat()
                    kind = self._kind_for(start, size, spans)
                    if kind is not None:
                        if colours[kind] is None:
                            colours[kind] = QtGui.QColor(highlighter.colour(kind))
                        fmt.setForeground(colours[kind])
                    edit = QtGui.QTextCursor(self.document())
                    edit.setPosition(fragment.position())
                    edit.setPosition(fragment.position() + size,
                                     QtGui.QTextCursor.KeepAnchor)
                    edit.setCharFormat(fmt)
                iterator += 1
            block = block.next()
        cursor.endEditBlock()
        self._update_extras()

    @staticmethod
    def _kind_for(start, size, spans):
        """The span kind covering a fragment of the document, or None."""
        for span_start, span_size, kind in spans:
            if span_start < start + size and start < span_start + span_size:
                return kind
        return None

    def _update_extras(self):
        """Repaint current-line and bracket-pair highlights."""
        extras = []
        theme = self.theme()
        # Current line.
        if self._views.get("current_line", True):
            if not self.isReadOnly():
                selection = QtWidgets.QTextEdit.ExtraSelection()
                selection.format.setBackground(
                    QtGui.QColor(theme["current_line"]))
                selection.format.setProperty(
                    QtGui.QTextFormat.FullWidthSelection, True)
                selection.cursor = self.textCursor()
                selection.cursor.clearSelection()
                extras.append(selection)
        # Bracket pair.
        if self._views.get("brackets", True):
            positions = self._matching_brackets()
            self._bracket_positions = positions
            for position in positions:
                if position is None:
                    continue
                selection = QtWidgets.QTextEdit.ExtraSelection()
                fmt = QtGui.QTextCharFormat()
                fmt.setBackground(QtGui.QColor(theme.get(
                    "bracket", "#ffd75f")))
                fmt.setForeground(QtGui.QColor(theme["background"]))
                selection.format = fmt
                cursor = QtGui.QTextCursor(self.document())
                cursor.setPosition(position)
                cursor.movePosition(QtGui.QTextCursor.Right,
                                    QtGui.QTextCursor.KeepAnchor)
                selection.cursor = cursor
                extras.append(selection)
        self.setExtraSelections(extras)
        self._minimap.update()

    def _matching_brackets(self):
        """Return the document positions of the bracket pair at the cursor."""
        text = self.toPlainText()
        position = self.textCursor().position()
        pairs = {")": "(", "]": "[", "}": "{"}
        opens = {"(": ")", "[": "]", "{": "}"}
        # Look at the character before and at the cursor.
        for candidate in (position - 1, position):
            if candidate < 0 or candidate >= len(text):
                continue
            char = text[candidate]
            if char in opens:
                match = self._scan_forward(text, candidate, opens[char])
                if match is not None:
                    return (candidate, match)
            elif char in pairs:
                match = self._scan_backward(text, candidate, pairs[char])
                if match is not None:
                    return (candidate, match)
        return (None, None)

    @staticmethod
    def _scan_forward(text, start, close_char):
        depth = 0
        for index in range(start, len(text)):
            char = text[index]
            if char in "([{":
                depth += 1
            elif char in ")]}":
                depth -= 1
                if depth == 0:
                    return index if char == close_char else None
        return None

    @staticmethod
    def _scan_backward(text, start, open_char):
        depth = 0
        for index in range(start, -1, -1):
            char = text[index]
            if char in ")]}":
                depth += 1
            elif char in "([{":
                depth -= 1
                if depth == 0:
                    return index if char == open_char else None
        return None

    # ----------------------------------------------------- auto-complete
    def keyPressEvent(self, event):
        """Handle suggestions, smart indent, bracket and block auto-closing."""
        text = event.text()
        key = event.key()
        modifiers = event.modifiers()
        # Smart suggestions: an open popup consumes navigation keys first.
        if self._completer is not None and self._completer.popup().isVisible():
            if self._completer_event(event):
                return
        # Ctrl+Space: open the suggestion popup on demand.
        if key == QtCore.Qt.Key_Space and modifiers & QtCore.Qt.ControlModifier:
            self.show_suggestions(force=True)
            return
        # Ctrl+/: toggle a comment on the selected lines (like an IDE).
        if key == QtCore.Qt.Key_Slash and modifiers & QtCore.Qt.ControlModifier:
            self.toggle_comment()
            return

    # ------------------------------------------------------- comment toggling
    def toggle_comment(self):
        """Add/remove the language's comment marker on the selected lines.

        When the selection is empty the cursor's own line is toggled. When
        several lines are selected, the selection is widened to whole lines and
        all of them are toggled together: if none of them carries a marker yet
        they are all commented, otherwise the marker is removed from every
        line that has one (a standard IDE behaviour).
        """
        marker = _comment_marker(self._category) or "#"
        pad = " " if marker != "#" else " "
        document = self.document()
        cursor = self.textCursor()

        # Widen the selection to cover whole lines.
        start_block = document.findBlock(cursor.selectionStart())
        end_block = document.findBlock(max(cursor.selectionEnd(), cursor.position()))
        if cursor.hasSelection() and cursor.selectionStart() == cursor.selectionEnd():
            # A zero-length selection inside a word still means "this line".
            end_block = start_block

        blocks = []
        block = start_block
        while True:
            blocks.append(block)
            if block == end_block or not block.isValid():
                break
            block = block.next()
        lines = [(b.blockNumber(), b.text()) for b in blocks if b.isValid()]
        if not lines:
            return

        marked = [text.lstrip().startswith(marker) for _, text in lines]
        comment_out = not any(marked)

        cursor.beginEditBlock()
        try:
            for block_number, text in reversed(lines):
                block = document.findBlockByNumber(block_number)
                if not block.isValid():
                    continue
                line_cursor = QtGui.QTextCursor(block)
                if comment_out:
                    indent = len(text) - len(text.lstrip())
                    line_cursor.setPosition(block.position() + indent)
                    line_cursor.insertText(marker + pad)
                else:
                    index = text.find(marker)
                    if index < 0:
                        continue
                    start = block.position() + index
                    end = start + len(marker)
                    # Swallow one space after the marker, so un-commenting
                    # restores the original indentation.
                    if end < len(text) and text[end] == " ":
                        end += 1
                    line_cursor.setPosition(start)
                    line_cursor.setPosition(end, QtGui.QTextCursor.KeepAnchor)
                    line_cursor.removeSelectedText()
        finally:
            cursor.endEditBlock()

    def keyPressEvent_ignored(self):
        pass
        if key == QtCore.Qt.Key_Escape and self._completer is not None:
            self._completer.popup().hide()
        # Enter: keep the indentation, and indent after a block opener.
        if key in (QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter):
            if self._views.get("smart_indent", True) and self._auto_indent():
                return
        # Closing bracket: skip over an auto-inserted one.
        if text in (")", "]", "}"):
            if self._skip_closing(text):
                return
        # Opening bracket / quote: insert the pair.
        if self._views.get("auto_close", True) and text in "([{'\"":
            if self._insert_pair(text):
                return
        super().keyPressEvent(event)
        # After a normal character, refresh the popup when it helps.
        if self._views.get("suggestions", True) and text:
            if text[-1].isalnum() or text[-1] == "_":
                self.show_suggestions()
            elif text[-1] == ".":
                self.show_suggestions(force=True)

    # -------------------------------------------------------- suggestions
    def _engine_now(self):
        """Rebuild the suggestion engine only when the buffer changed."""
        code = self.toPlainText()
        stamp = (hash(code), len(code), self._category)
        if self._engine is None or self._engine_stamp != stamp:
            self._engine = SuggestionEngine(
                code, self._category)
            self._engine_stamp = stamp
        return self._engine

    def suggestions_enabled(self):
        return self._views.get("suggestions", True)

    def word_under_cursor(self):
        """The identifier prefix just before the cursor (may be empty)."""
        cursor = self.textCursor()
        block_text = cursor.block().text()
        column = cursor.positionInBlock()
        start = column
        while start > 0 and (block_text[start - 1].isalnum()
                             or block_text[start - 1] == "_"):
            start -= 1
        return block_text[start:column]

    def show_suggestions(self, force=False):
        """Open the smart-suggestion popup for the current word."""
        if not self.suggestions_enabled() and not force:
            return
        engine = self._engine_now()
        line_text = self.textCursor().block().text()
        prefix = self.word_under_cursor()
        suggestions = engine.suggest(prefix, limit=40)
        if not suggestions:
            suggestions = engine.context_suggestions(line_text)
        if not suggestions:
            return
        # Do not nag with a single untyped match (except when forced).
        if len(suggestions) <= 1 and not prefix and not force:
            return
        self._show_popup([s.label() for s in suggestions], prefix,
                         suggestions)

    def _show_popup(self, labels, prefix, suggestions):
        self._last_suggestions = suggestions
        completer = QtWidgets.QCompleter(labels, self)
        completer.setWidget(self)
        completer.setCompletionMode(QtWidgets.QCompleter.PopupCompletion)
        completer.setCaseSensitivity(QtCore.Qt.CaseInsensitive)
        completer.activated[str].connect(
            lambda chosen: self._insert_suggestion(chosen, suggestions))
        self._completer = completer
        completer.setCompletionPrefix(prefix)
        rect = self.cursorRect()
        popup = completer.popup()
        rect.setWidth(popup.sizeHintForColumn(0)
                      + popup.verticalScrollBar().sizeHint().width() + 24)
        if completer.completionModel().rowCount() > 0:
            popup.setCurrentIndex(completer.completionModel().index(0, 0))
        completer.complete(rect)

    def _completer_event(self, event):
        """Let the popup handle navigation keys before the editor does."""
        key = event.key()
        popup = self._completer.popup()
        if key in (QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter,
                   QtCore.Qt.Key_Tab):
            index = popup.currentIndex()
            if index.isValid():
                self._insert_suggestion(index.data(), self._last_suggestions)
            popup.hide()
            return True
        if key in (QtCore.Qt.Key_Up, QtCore.Qt.Key_Down,
                   QtCore.Qt.Key_PageUp, QtCore.Qt.Key_PageDown):
            return False
        if key == QtCore.Qt.Key_Escape:
            popup.hide()
            return True
        return False

    def _insert_suggestion(self, label, suggestions):
        """Insert the chosen suggestion, replacing the typed word."""
        if not label:
            return
        text = None
        for suggestion in (suggestions or []):
            if suggestion.label() == label or suggestion.text == label:
                text = suggestion.insert
                break
        if text is None:
            text = str(label).split("  -  ")[0].strip()
        cursor = self.textCursor()
        block_text = cursor.block().text()
        column = cursor.positionInBlock()
        start = column
        while start > 0 and (block_text[start - 1].isalnum()
                             or block_text[start - 1] == "_"):
            start -= 1
        prefix = block_text[start:column]
        # Insert only the part that continues what was already typed.
        insert_text = text[len(prefix):] if prefix and text.startswith(prefix) \
            else text
        cursor.insertText(insert_text)
        self.setTextCursor(cursor)

    def _current_line_indent(self):
        cursor = self.textCursor()
        line = cursor.block().text()
        return re.match(r"[ \t]*", line).group(0)

    def _auto_indent(self):
        """Insert a newline keeping the indent, adding one after a colon."""
        cursor = self.textCursor()
        line = cursor.block().text()
        indent = self._current_line_indent()
        stripped = line.rstrip()
        extra = ""
        # The language module lists the characters that open a block.
        if stripped.endswith(self._module.indent_triggers):
            extra = "    "
        # If the cursor sits between a pair, put the closer on its own line.
        text = self.toPlainText()
        position = cursor.position()
        if position < len(text) and text[position] in ")]}":
            closer = text[position]
            opener = {"(": "(", "[": "[", "{": "{"}.get(
                {")": "(", "]": "[", "}": "{"}.get(closer, ""), "")
            cursor.insertText("\n" + indent + extra + "\n" + indent)
            cursor.movePosition(QtGui.QTextCursor.Up)
            cursor.movePosition(QtGui.QTextCursor.EndOfLine)
            self.setTextCursor(cursor)
            return True
        cursor.insertText("\n" + indent + extra)
        self.setTextCursor(cursor)
        return True

    def _skip_closing(self, text):
        """Move past a closing bracket that matches the previous opener."""
        position = self.textCursor().position()
        document = self.toPlainText()
        if position >= len(document) or document[position] != text:
            return False
        if position - 1 < 0:
            return False
        return True

    def _insert_pair(self, opener):
        """Insert a matching closer (or quote) and keep the cursor inside."""
        closers = {"(": ")", "[": "]", "{": "}", "'": "'", '"': '"'}
        closer = closers[opener]
        cursor = self.textCursor()
        if cursor.hasSelection():
            selected = cursor.selectedText()
            cursor.insertText(opener + selected + closer)
            return True
        cursor.insertText(opener + closer)
        cursor.movePosition(QtGui.QTextCursor.Left)
        self.setTextCursor(cursor)
        return True

class EditorTab(QtWidgets.QWidget):
    """Edit one script at a time and save it back to the script list. The tab holds no database of its own: it reads from and writes to the script list of the main window, so the Editor, Scripts and Analyzer tabs never disagree about what a script contains."""

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.script_id = None
        self._loading = False
        self._read_only_lock = False
        self._recent_files = []
        self._recent_projects = []
        self._access_log = []
        self._file_baseline_hash = None
        self._build_ui()
        self.refresh()

    # --------------------------------------------------------------- helpers
    def t(self, key, fallback=None, **kwargs):
        """Look a label up in the main window's translations."""
        try:
            return self.window.t(key, fallback, **kwargs)
        except Exception:
            return fallback or key

    def _scripts(self):
        """All real script entries from the main window (never None)."""
        try:
            return list(self.window.scripts_only())
        except Exception:
            return []

    def _selected_script(self):
        """The script selected on the Scripts tab, if any."""
        try:
            return self.window.selected_script()
        except Exception:
            return None

    def _find_script(self, script_id):
        for script in self._scripts():
            if script.get("id") == script_id:
                return script
        return None

    # ------------------------------------------------------------------- UI
    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)

        header = QtWidgets.QHBoxLayout()
        self.title_label = QtWidgets.QLabel(self.t("editor", "Editor"))
        self.title_label.setStyleSheet(
            "font-size: 14px; font-weight: bold; color: #8ab4f8;")
        header.addWidget(self.title_label)
        header.addStretch()
        self.btn_load = QtWidgets.QPushButton(
            self.t("editor_load", "Load selected"))
        self.btn_load.clicked.connect(self.load_selected)
        header.addWidget(self.btn_load)
        self.btn_save = QtWidgets.QPushButton(self.t("save", "Save"))
        self.btn_save.clicked.connect(self.save)
        header.addWidget(self.btn_save)
        self.btn_export = QtWidgets.QPushButton(
            self.t("editor_export", "Export file"))
        self.btn_export.clicked.connect(self.export_file)
        header.addWidget(self.btn_export)
        layout.addLayout(header)

        # ---- current script row (name + category + favorite) --------------
        meta_row = QtWidgets.QHBoxLayout()
        self.name_label = QtWidgets.QLabel(self.t("editor_no_script",
                                                  "No script loaded"))
        self.name_label.setStyleSheet("color: #9aa0a6;")
        meta_row.addWidget(self.name_label)
        meta_row.addStretch()
        self.dirty_label = QtWidgets.QLabel("")
        self.dirty_label.setStyleSheet("color: #f7c66d;")
        meta_row.addWidget(self.dirty_label)
        layout.addLayout(meta_row)

        # ---- search & replace --------------------------------------------
        find_row = QtWidgets.QHBoxLayout()
        self.find_edit = QtWidgets.QLineEdit()
        self.find_edit.setPlaceholderText(self.t("editor_find_hint", "Find..."))
        self.replace_edit = QtWidgets.QLineEdit()
        self.replace_edit.setPlaceholderText(
            self.t("editor_replace_hint", "Replace with..."))
        self.btn_find = QtWidgets.QPushButton(self.t("editor_find", "Find"))
        self.btn_find.clicked.connect(self.find_next)
        # A single "Replace..." button whose menu holds every replace action,
        # mirroring the Import/Export buttons on the Scripts tab.
        self.btn_replace = QtWidgets.QToolButton()
        self.btn_replace.setText(self.t("editor_replace_menu", "Replace..."))
        self.btn_replace.setPopupMode(QtWidgets.QToolButton.MenuButtonPopup)
        self.replace_menu = QtWidgets.QMenu(self.btn_replace)
        self.act_replace_one = self.replace_menu.addAction(
            self.t("editor_replace", "Replace"))
        self.act_replace_all = self.replace_menu.addAction(
            self.t("editor_replace_all", "Replace all"))
        self.act_replace_one.triggered.connect(self.replace_one)
        self.act_replace_all.triggered.connect(self.replace_all)
        self.btn_replace.setMenu(self.replace_menu)
        # The button itself runs "Replace"; the arrow opens the menu.
        self.btn_replace.clicked.connect(self.replace_one)
        self.chk_regex = QtWidgets.QCheckBox(self.t("editor_regex", "Regex"))
        self.chk_case = QtWidgets.QCheckBox(self.t("editor_case", "Case"))
        find_row.addWidget(self.find_edit, 1)
        find_row.addWidget(self.replace_edit, 1)
        find_row.addWidget(self.btn_find)
        find_row.addWidget(self.btn_replace)
        find_row.addWidget(self.chk_regex)
        find_row.addWidget(self.chk_case)
        layout.addLayout(find_row)


        # ---- search engine ------------------------------------------------
        # The search kinds from tabs/search_engine.py: text, string, regex,
        # multiline, many files, semantic, imports, code blocks, comments, AST,
        # calls, definitions, references and TODO.
        layout.addLayout(self._build_search_row())
        layout.addWidget(self._build_search_results())

        # ---- tools row ----------------------------------------------------
        # Keep line movement and the less frequent editing transforms in
        # compact menus so the toolbar leaves more room for the editor.
        MOVED_TOOLS = ("move_up", "move_down")
        tools_row = QtWidgets.QHBoxLayout()
        self.btn_tools = QtWidgets.QToolButton()
        self.btn_tools.setText(self.t("editor_tools_menu", "Code tools..."))
        self.btn_tools.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.tools_menu = QtWidgets.QMenu(self.btn_tools)
        self.tool_actions = {}
        for key, label_key, fallback in EDITOR_TOOLS:
            if key in MOVED_TOOLS:
                continue
            action = self.tools_menu.addAction(self.t(label_key, fallback))
            action.triggered.connect(
                lambda _checked=False, k=key: self.apply_tool(k))
            self.tool_actions[key] = action
        self.btn_tools.setMenu(self.tools_menu)
        self.btn_move = QtWidgets.QToolButton()
        self.btn_move.setText(self.t("editor_move_menu", "Move..."))
        self.btn_move.setPopupMode(QtWidgets.QToolButton.MenuButtonPopup)
        self.move_menu = QtWidgets.QMenu(self.btn_move)
        self.act_move_up = self.move_menu.addAction(
            self.t("editor_move_up", "Move up"))
        self.act_move_down = self.move_menu.addAction(
            self.t("editor_move_down", "Move down"))
        self.act_move_up.triggered.connect(lambda: self.apply_tool("move_up"))
        self.act_move_down.triggered.connect(
            lambda: self.apply_tool("move_down"))
        self.btn_move.setMenu(self.move_menu)
        # The button itself moves the line up; the arrow opens the menu.
        self.btn_move.clicked.connect(lambda: self.apply_tool("move_up"))

        self.btn_refactor = QtWidgets.QToolButton()
        self.btn_refactor.setText(self.t("editor_refactor_menu", "Refactor..."))
        self.btn_refactor.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.refactor_menu = QtWidgets.QMenu(self.btn_refactor)
        self.refactor_actions = {}
        for key, label_key, fallback in EDITOR_REFACTOR_ACTIONS:
            action = self.refactor_menu.addAction(self.t(label_key, fallback))
            action.triggered.connect(lambda _checked=False, k=key: self.apply_tool(k))
            self.refactor_actions[key] = action
        self.btn_refactor.setMenu(self.refactor_menu)

        self.btn_cleanup = QtWidgets.QToolButton()
        self.btn_cleanup.setText(self.t("editor_cleanup_menu", "Code cleanup..."))
        self.btn_cleanup.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.cleanup_menu = QtWidgets.QMenu(self.btn_cleanup)
        self.cleanup_actions = {}
        for key, label_key, fallback in EDITOR_CLEANUP_ACTIONS:
            action = self.cleanup_menu.addAction(self.t(label_key, fallback))
            action.triggered.connect(lambda _checked=False, k=key: self.apply_tool(k))
            self.cleanup_actions[key] = action
        self.btn_cleanup.setMenu(self.cleanup_menu)

        self.btn_analysis = QtWidgets.QToolButton()
        self.btn_analysis.setText(self.t("editor_analysis_menu", "Code analysis..."))
        self.btn_analysis.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.analysis_menu = QtWidgets.QMenu(self.btn_analysis)
        self.analysis_actions = {}
        for key, label_key, fallback in EDITOR_ANALYSIS_ACTIONS:
            action = self.analysis_menu.addAction(self.t(label_key, fallback))
            action.triggered.connect(lambda _checked=False, k=key: self.run_analysis(k))
            self.analysis_actions[key] = action
        self.btn_analysis.setMenu(self.analysis_menu)
        tools_row.addWidget(self.btn_analysis)

        self.btn_integrations = QtWidgets.QToolButton()
        self.btn_integrations.setText(self.t("editor_integration_menu", "Integrations..."))
        self.btn_integrations.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.integration_menu = QtWidgets.QMenu(self.btn_integrations)
        self.integration_actions = {}
        for key, label_key, fallback in EDITOR_INTEGRATION_ACTIONS:
            action = self.integration_menu.addAction(self.t(label_key, fallback))
            action.triggered.connect(lambda _checked=False, k=key: self.run_integration(k))
            self.integration_actions[key] = action
        self.btn_integrations.setMenu(self.integration_menu)
        tools_row.addWidget(self.btn_integrations)

        self.btn_workspace = QtWidgets.QToolButton()
        self.btn_workspace.setText(self.t("editor_workspace_menu", "Workspace & security..."))
        self.btn_workspace.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.workspace_menu = QtWidgets.QMenu(self.btn_workspace)
        self.workspace_actions = {}
        for key, label_key, fallback in EDITOR_WORKSPACE_ACTIONS:
            action = self.workspace_menu.addAction(self.t(label_key, fallback))
            action.triggered.connect(lambda _checked=False, k=key: self.run_workspace_action(k))
            self.workspace_actions[key] = action
        self.btn_workspace.setMenu(self.workspace_menu)
        tools_row.addWidget(self.btn_workspace)

        # ---- metadata: the script's own header, read and written in place ---
        # The engine is tabs/metadata_core.py, the same one the Metadata tab
        # uses, so the Editor and that tab agree about a script's metadata.
        self.btn_metadata = QtWidgets.QToolButton()
        self.btn_metadata.setText(self.t("editor_metadata_menu", "Metadata..."))
        self.btn_metadata.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.metadata_menu = QtWidgets.QMenu(self.btn_metadata)
        self.metadata_actions = {}
        for key, label_key, fallback in EDITOR_METADATA_ACTIONS:
            action = self.metadata_menu.addAction(self.t(label_key, fallback))
            action.triggered.connect(
                lambda _checked=False, k=key: self.run_metadata_action(k))
            self.metadata_actions[key] = action
        self.btn_metadata.setMenu(self.metadata_menu)
        tools_row.addWidget(self.btn_metadata)

        # ---- workflow: build a script from stages or from blocks ------------
        # The engine is tabs/workflow_engine.py: the same blocks that make a
        # pipeline (input, process, output, log, error) also make a composed
        # script, and the safeguards and metrics are the engine's, not this
        # file's, so the two agree about what a safe script carries.
        self.btn_workflow = QtWidgets.QToolButton()
        self.btn_workflow.setText(self.t("editor_workflow_menu",
                                         "Workflow builder..."))
        self.btn_workflow.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.workflow_menu = QtWidgets.QMenu(self.btn_workflow)
        self.workflow_actions = {}
        for key, label_key, fallback in EDITOR_WORKFLOW_ACTIONS:
            action = self.workflow_menu.addAction(self.t(label_key, fallback))
            action.triggered.connect(
                lambda _checked=False, k=key: self.run_workflow_action(k))
            self.workflow_actions[key] = action
        self.btn_workflow.setMenu(self.workflow_menu)
        tools_row.addWidget(self.btn_workflow)

        self.btn_templates = QtWidgets.QToolButton()
        self.btn_templates.setText(self.t("editor_templates_menu",
                                          "Templates..."))
        self.btn_templates.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.templates_menu = QtWidgets.QMenu(self.btn_templates)
        self.template_actions = {}
        self._rebuild_templates_menu()
        self.btn_templates.setMenu(self.templates_menu)
        tools_row.addWidget(self.btn_templates)

        # ---- code generation: one action per generator kind ----------------
        # The menu is built from the generator's own catalogue, so a kind
        # registered at run time shows up here without a change to this file.
        self.btn_codegen = QtWidgets.QToolButton()
        self.btn_codegen.setText(self.t("editor_codegen_menu",
                                        "Code generation..."))
        self.btn_codegen.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.codegen_menu = QtWidgets.QMenu(self.btn_codegen)
        self.codegen_actions = {}
        for key, label_key, fallback in EDITOR_CODEGEN_ACTIONS:
            action = self.codegen_menu.addAction(self.t(label_key, fallback))
            action.triggered.connect(
                lambda _checked=False, k=key: self.run_codegen(k))
            self.codegen_actions[key] = action
        self.btn_codegen.setMenu(self.codegen_menu)
        tools_row.addWidget(self.btn_codegen)

        # ---- code rules: the automatic linter + fixer -----------------------
        self.btn_coderules = QtWidgets.QToolButton()
        self.btn_coderules.setText(self.t("editor_coderules_menu",
                                          "Code rules..."))
        self.btn_coderules.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.coderules_menu = QtWidgets.QMenu(self.btn_coderules)
        self.coderules_actions = {}
        for key, label_key, fallback in EDITOR_CODERULES_ACTIONS:
            action = self.coderules_menu.addAction(self.t(label_key, fallback))
            action.triggered.connect(
                lambda _checked=False, k=key: self.run_code_rule(k))
            self.coderules_actions[key] = action
        self.coderules_menu.addSeparator()
        self.act_rules_all = self.coderules_menu.addAction(
            self.t("coderules_fix_all", "Run every rule"))
        self.act_rules_lint = self.coderules_menu.addAction(
            self.t("coderules_lint", "Only report (change nothing)"))
        self.act_rules_report = self.coderules_menu.addAction(
            self.t("coderules_report", "Rules report"))
        self.act_rules_all.triggered.connect(self.run_all_code_rules)
        self.act_rules_lint.triggered.connect(self.lint_code_rules)
        self.act_rules_report.triggered.connect(self.show_code_rules_report)
        self.btn_coderules.setMenu(self.coderules_menu)
        tools_row.addWidget(self.btn_coderules)

        # ---- script profiles: one action per whole-script shape ------------
        # A profile writes a whole script (structure, sections, imports,
        # functions and the startup logic); the menu is built from the engine
        # catalogue, so a profile registered at run time appears here too.
        self.btn_profiles = QtWidgets.QToolButton()
        self.btn_profiles.setText(self.t("editor_profiles_menu",
                                         "Script profiles..."))
        self.btn_profiles.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.profiles_menu = QtWidgets.QMenu(self.btn_profiles)
        self.profile_actions = {}
        for key, label_key, fallback in EDITOR_PROFILE_ACTIONS:
            action = self.profiles_menu.addAction(self.t(label_key, fallback))
            action.triggered.connect(
                lambda _checked=False, k=key: self.insert_profile(k))
            self.profile_actions[key] = action
        self.btn_profiles.setMenu(self.profiles_menu)
        tools_row.addWidget(self.btn_profiles)
        tools_row.addStretch()
        self.btn_undo = QtWidgets.QPushButton(self.t("editor_undo", "Undo"))
        self.btn_undo.clicked.connect(lambda: self.editor.undo())
        self.btn_redo = QtWidgets.QPushButton(self.t("editor_redo", "Redo"))
        self.btn_redo.clicked.connect(lambda: self.editor.redo())
        tools_row.addWidget(self.btn_undo)
        tools_row.addWidget(self.btn_redo)
        layout.addLayout(tools_row)


        # ---- view row: theme, low-graphics and feature toggles ------------
        view_row = QtWidgets.QHBoxLayout()
        self.theme_label = QtWidgets.QLabel(self.t("editor_theme", "Theme:"))
        view_row.addWidget(self.theme_label)
        self.theme_combo = QtWidgets.QComboBox()
        self.theme_combo.addItem(self.t("editor_theme_dark", "Dark"), "dark")
        self.theme_combo.addItem(self.t("editor_theme_light", "Light"), "light")
        self.theme_combo.currentIndexChanged.connect(self._on_theme_changed)
        view_row.addWidget(self.theme_combo)
        self.chk_low_graphics = QtWidgets.QCheckBox(
            self.t("editor_low_graphics", "Low graphics"))
        self.chk_low_graphics.toggled.connect(self._on_low_graphics)
        view_row.addWidget(self.chk_low_graphics)
        view_row.addSpacing(10)
        self.view_checks = {}
        for key, label_key, fallback, default in EDITOR_VIEWS:
            box = QtWidgets.QCheckBox(self.t(label_key, fallback))
            box.setChecked(default)
            box.toggled.connect(
                lambda checked, k=key: self.editor.set_view(k, checked))
            self.view_checks[key] = box
            view_row.addWidget(box)
        view_row.addStretch()
        layout.addLayout(view_row)


        # ---- the editor itself (with gutter + minimap) --------------------
        self.editor = CodeEditor()

        # ---- language row: one entry per hot-pluggable syntax module ------
        # The list is read from the registry, so a newly registered module
        # shows up here without any change to this file.
        language_row = QtWidgets.QHBoxLayout()
        self.language_label = QtWidgets.QLabel(
            self.t("editor_language", "Language:"))
        language_row.addWidget(self.language_label)
        self.language_combo = QtWidgets.QComboBox()
        for key, label in languages():
            self.language_combo.addItem("%s  (hot-plug)" % label, key)
        self.language_combo.currentIndexChanged.connect(
            self._on_language_changed)
        language_row.addWidget(self.language_combo)
        self.module_label = QtWidgets.QLabel("")
        self.module_label.setStyleSheet("color: #9aa0a6;")
        language_row.addWidget(self.module_label)
        language_row.addStretch()
        language_row.addWidget(self.btn_tools)
        language_row.addWidget(self.btn_move)
        language_row.addWidget(self.btn_refactor)
        language_row.addWidget(self.btn_cleanup)
        layout.addLayout(language_row)


        # ---- navigation row: the ten code jumps ---------------------------
        # The Go to... menu is built from nav_engine.NAV_KINDS, so a new jump
        # registered in the engine shows up here without a change to this file.
        layout.addLayout(self._build_nav_row())

        # The editor's F12 / Alt+arrow shortcuts and Ctrl+click ask the tab to
        # perform the jump, so all navigation state stays in this class.
        self.editor.set_nav_handler(self.goto_kind)
        self.editor.textChanged.connect(self._on_text_changed)
        self.editor.minimap().setVisible(True)
        layout.addWidget(self.editor, 1)

        self.status = QtWidgets.QLabel("")
        self.status.setStyleSheet("color: #9aa0a6;")
        layout.addWidget(self.status)

    def _build_nav_row(self):
        """The navigation row: Go to menu, symbol box and problem jump."""
        row = QtWidgets.QHBoxLayout()
        self.nav_label = QtWidgets.QLabel(self.t("nav_go", "Navigate:"))
        row.addWidget(self.nav_label)

        # One button whose menu holds every jump the engine offers.
        self.btn_goto = QtWidgets.QToolButton()
        self.btn_goto.setText(self.t("nav_go_menu", "Go to..."))
        self.btn_goto.setPopupMode(QtWidgets.QToolButton.MenuButtonPopup)
        self.nav_menu = QtWidgets.QMenu(self.btn_goto)
        self.nav_actions = {}
        for key, label_key, fallback in nav_engine.NAV_KINDS:
            action = self.nav_menu.addAction(self.t(label_key, fallback))
            action.triggered.connect(
                lambda _checked=False, k=key: self.goto_kind(k))
            self.nav_actions[key] = action
        self.nav_menu.addSeparator()
        self.act_nav_back = self.nav_menu.addAction(
            self.t("nav_go_previous", "Go to previous"))
        self.act_nav_back.triggered.connect(lambda: self.goto_kind(None, -1))
        self.btn_goto.setMenu(self.nav_menu)
        # The button itself runs the primary jump; the arrow opens the menu.
        self.btn_goto.clicked.connect(lambda: self.goto_kind("definition"))
        row.addWidget(self.btn_goto)

        # The quick-jump box: type a symbol name and jump straight to it.
        self.nav_edit = QtWidgets.QLineEdit()
        self.nav_edit.setPlaceholderText(
            self.t("nav_symbol_hint", "Symbol to jump to..."))
        self.nav_edit.returnPressed.connect(self.goto_symbol)
        row.addWidget(self.nav_edit, 1)

        self.btn_nav_run = QtWidgets.QPushButton(
            self.t("nav_symbol_jump", "Jump"))
        self.btn_nav_run.clicked.connect(self.goto_symbol)
        row.addWidget(self.btn_nav_run)

        # Jump to the next problem (error or warning), then the previous one.
        self.btn_problem = QtWidgets.QToolButton()
        self.btn_problem.setText(self.t("nav_problem", "Problem"))
        self.btn_problem.setPopupMode(QtWidgets.QToolButton.MenuButtonPopup)
        self.problem_menu = QtWidgets.QMenu(self.btn_problem)
        self.act_problem_next = self.problem_menu.addAction(
            self.t("nav_problem_next", "Next problem"))
        self.act_problem_prev = self.problem_menu.addAction(
            self.t("nav_problem_prev", "Previous problem"))
        self.act_problem_next.triggered.connect(
            lambda: self.goto_problem(1))
        self.act_problem_prev.triggered.connect(
            lambda: self.goto_problem(-1))
        self.btn_problem.setMenu(self.problem_menu)
        self.btn_problem.clicked.connect(lambda: self.goto_problem(1))
        row.addWidget(self.btn_problem)
        row.addStretch()
        return row
    # -------------------------------------------------------- search engine
    def _build_search_row(self):
        """The search-kind row: kind, sub-kind, query, run and options."""
        row = QtWidgets.QHBoxLayout()
        self.search_label = QtWidgets.QLabel(self.t("search_kind", "Search:"))
        row.addWidget(self.search_label)

        self.search_kind = QtWidgets.QComboBox()
        # The filesystem kinds (directory / workspace) need a target path and
        # belong to the Search tab; the editor searches the open buffer and the
        # script database, both in memory.
        for key, label_key, fallback in search_engine.SEARCH_KINDS:
            if key in ("files", "workspace"):
                continue
            self.search_kind.addItem(self.t(label_key, fallback), key)
        self.search_kind.currentIndexChanged.connect(self._on_search_kind)
        row.addWidget(self.search_kind)

        # Second combo: semantic sub-kind or AST node kind, shown on demand.
        self.search_subkind = QtWidgets.QComboBox()
        self.search_subkind.setVisible(False)
        row.addWidget(self.search_subkind)

        self.search_edit = QtWidgets.QLineEdit()
        self.search_edit.setPlaceholderText(
            self.t("search_hint", "Search in code..."))
        self.search_edit.returnPressed.connect(self.run_search)
        self.search_edit.textChanged.connect(self._on_search_text_changed)
        row.addWidget(self.search_edit, 1)

        self.btn_search = QtWidgets.QPushButton(self.t("search_run", "Search"))
        self.btn_search.clicked.connect(self.run_search)
        row.addWidget(self.btn_search)

        # Clear results; the rest of the options live in one compact menu.
        self.btn_search_menu = QtWidgets.QToolButton()
        self.btn_search_menu.setText(self.t("search_options", "Options"))
        self.btn_search_menu.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.search_menu = QtWidgets.QMenu(self.btn_search_menu)
        self.search_checks = {}
        for key, label_key, fallback in EDITOR_SEARCH_OPTIONS:
            action = self.search_menu.addAction(self.t(label_key, fallback))
            action.setCheckable(True)
            action.setChecked(key == "include_methods")
            self.search_checks[key] = action
        self.search_menu.addSeparator()
        self.act_search_clear = self.search_menu.addAction(
            self.t("search_clear", "Clear results"))
        self.act_search_clear.triggered.connect(self.clear_search_results)
        self.btn_search_menu.setMenu(self.search_menu)
        row.addWidget(self.btn_search_menu)

        self.btn_search_all = QtWidgets.QPushButton(
            self.t("search_database", "Whole database"))
        self.btn_search_all.setToolTip(self.t(
            "search_database_hint", "Run this search over every script"))
        self.btn_search_all.clicked.connect(self.search_whole_database)
        row.addWidget(self.btn_search_all)
        return row
    def _build_search_results(self):
        """The results table under the search row."""
        self.search_results = QtWidgets.QTreeWidget()
        self.search_results.setAlternatingRowColors(True)
        self.search_results.setColumnCount(5)
        self.search_results.setHeaderLabels([
            self.t("search_col_doc", "Script"),
            self.t("search_col_kind", "Kind"),
            self.t("search_col_line", "Line"),
            self.t("search_col_text", "Match"),
            self.t("search_col_detail", "Detail"),
        ])
        self.search_results.setFont(QFont(EDITOR_FONT, EDITOR_FONT_SIZE))
        self.search_results.setMaximumHeight(180)
        self.search_results.setVisible(False)
        self.search_results.itemActivated.connect(self.goto_search_hit)
        self.search_results.itemDoubleClicked.connect(self.goto_search_hit)
        self._search_result = None
        return self.search_results
    def _on_search_kind(self, _index):
        """Swap the second combo between semantic kinds and AST node kinds."""
        kind = self.search_kind.currentData()
        tables = {
            "semantic": search_engine.SEMANTIC_KINDS,
            "imports": search_engine.IMPORT_KINDS,
            "blocks": search_engine.BLOCK_KINDS,
            "comments": search_engine.COMMENT_KINDS,
            "ast": search_engine.AST_KINDS,
            "string": search_engine.STRING_MODES,
            "multifile": search_engine.MULTIFILE_MODES,
        }
        table = tables.get(kind)
        self.search_subkind.blockSignals(True)
        self.search_subkind.clear()
        if table:
            for key, label_key, fallback in table:
                self.search_subkind.addItem(self.t(label_key, fallback), key)
            self.search_subkind.setVisible(True)
        else:
            self.search_subkind.setVisible(False)
        self.search_subkind.blockSignals(False)
        # A few kinds need no query at all.
        needs_query = kind not in ("semantic", "imports", "blocks",
                                   "comments", "ast", "todo")
        self.search_edit.setEnabled(needs_query)
        self.search_edit.setPlaceholderText(
            self.t("search_hint", "Search in code...") if needs_query
            else self.t("search_optional", "(optional filter)"))

    def _on_search_text_changed(self, _text):
        """Live results while typing -- only for the cheap kinds."""
        kind = self.search_kind.currentData()
        if kind in ("text", "string", "regex", "todo", "comments"):
            self.run_search(quiet=True)

    def current_search_kind(self):
        return self.search_kind.currentData() or "text"

    def search_documents(self, whole_database=False):
        # The documents a search should run over: by default the open buffer
        # plus the script it came from; whole-database swaps in every script.
        code = self.editor.toPlainText()
        if whole_database:
            documents = []
            for script in self._scripts():
                documents.append((script.get("name", ""),
                                  script.get("code", "") or "",
                                  script.get("category", "Python")))
            # Include the live buffer so unsaved edits are searchable too.
            if self.script_id is not None:
                documents.append((self.name_label.text().split("  [")[0]
                                  + " (buffer)", code,
                                  self.current_category()))
            return documents
        name = self.name_label.text().split("  [")[0].strip() or "(buffer)"
        return [(name, code, self.current_category())]

    def run_search(self, quiet=False, whole_database=False):
        """Run the selected search and fill the results table."""
        kind = self.current_search_kind()
        query = self.search_edit.text()
        options = {"case_sensitive": self.search_checks[
            "case_sensitive"].isChecked()}
        if kind == "string":
            options["match"] = self.search_subkind.currentData() or "substring"
        elif kind == "multifile":
            options["mode"] = self.search_subkind.currentData() or "text"
        elif kind == "semantic":
            options["symbol_kind"] = self.search_subkind.currentData() or "any"
        elif kind == "imports":
            options["import_kind"] = self.search_subkind.currentData() or "any"
        elif kind == "blocks":
            options["block_kind"] = self.search_subkind.currentData() or "any"
        elif kind == "comments":
            options["comment_kind"] = self.search_subkind.currentData() or "any"
        elif kind == "ast":
            options["node_kind"] = self.search_subkind.currentData() or "any"
        elif kind == "calls":
            options["include_methods"] = self.search_checks[
                "include_methods"].isChecked()
        elif kind == "references":
            options["mark_definitions"] = self.search_checks[
                "definitions"].isChecked()
        result = search_engine.search(
            self.search_documents(whole_database), query, kind, **options)
        self._search_result = result
        self.fill_search_results(result)
        if not quiet:
            self.status.setText(result.summary())
        return result
    def search_whole_database(self):
        """Run the current search across every script in the database."""
        return self.run_search(whole_database=True)

    def fill_search_results(self, result):
        """Render a SearchResult into the results tree."""
        tree = self.search_results
        tree.clear()
        colours = {
            "definition": "#9be9a8", "class": "#9be9a8",
            "function": "#8ab4f8", "method": "#8ab4f8",
            "call": "#c586c0", "method call": "#c586c0",
            "reference": "#bfc7d5", "variable": "#f7c66d",
            "TODO": "#f7c66d", "FIXME": "#ff9a9a", "HACK": "#ff9a9a",
        }
        for hit in result.hits:
            item = QtWidgets.QTreeWidgetItem([
                hit.doc, hit.kind, str(hit.line) if hit.line else "-",
                hit.text, hit.detail])
            colour = colours.get(hit.kind)
            if colour is not None:
                item.setForeground(1, QtGui.QColor(colour))
            # Keep the hit itself so activating the row can jump to it.
            item.setData(0, QtCore.Qt.UserRole, hit)
            tree.addTopLevelItem(item)
        for column in range(tree.columnCount()):
            tree.resizeColumnToContents(column)
        tree.setVisible(bool(result.hits) or bool(result.error))
        if result.error:
            self.status.setText(result.error)

    def clear_search_results(self):
        """Empty the results table and forget the last search."""
        self.search_results.clear()
        self.search_results.setVisible(False)
        self._search_result = None
        self.status.setText(self.t("search_cleared", "search results cleared"))

    def goto_search_hit(self, item, _column=0):
        """Jump to the editor line a result row points at."""
        hit = item.data(0, QtCore.Qt.UserRole) if item is not None else None
        if hit is None:
            return
        if hit.doc != self.name_label.text().split("  [")[0].strip() \
                and not hit.doc.endswith("(buffer)"):
            # A hit from another script: load it first, then jump.
            script = self._find_script_by_name(hit.doc)
            if script is not None:
                self.load_script(script)
        if hit.line:
            self.editor.goto_line(hit.line - 1)
            self._select_search_range(hit)

    def _select_search_range(self, hit):
        """Highlight the matched text inside the line."""
        block = self.editor.document().findBlockByNumber(max(0, hit.line - 1))
        if not block.isValid():
            return
        start = block.position() + max(0, hit.column)
        size = len(self.search_edit.text()) or len(hit.detail) or 1
        cursor = QtGui.QTextCursor(self.editor.document())
        cursor.setPosition(start)
        cursor.setPosition(min(start + size, block.position() + len(block.text())),
                           QtGui.QTextCursor.KeepAnchor)
        self.editor.setTextCursor(cursor)

    def _find_script_by_name(self, name):
        """Look a script up by its display name (cross-file results)."""
        for script in self._scripts():
            if script.get("name", "") == name:
                return script
        return None
    # ----------------------------------------------------- code navigation
    def navigation_document(self):
        """The open buffer as the (name, code, category) the engine reads."""
        name = self.name_label.text().split("  [")[0].strip() or "(buffer)"
        return (name, self.editor.toPlainText(), self.current_category())

    def symbol_under_cursor(self):
        """The identifier at the cursor, using the engine rules. A cursor that is not on a word (at the start of an indented line, say) falls back to the first identifier on that line, skipping the language keywords, which are never definitions."""
        text = self.editor.toPlainText()
        line, column = self.editor.cursor_line_column()
        name = nav_engine.word_at_cursor(text, line, column)[0]
        if name:
            return name
        # Fall back to the first identifier on the line that is not a keyword.
        body = self.editor.textCursor().block().text()
        keywords = getattr(self.editor.module(), "keywords", ())
        index = 0
        while index < len(body):
            name, _start, end = nav_engine.word_at(body, index)
            if not name:
                index += 1
                continue
            if name not in keywords:
                return name
            # A keyword is skipped; `end` is exclusive, so the next probe
            # starts one character past it and never re-reads the same word.
            index = end + 1
        return ""

    def goto_kind(self, kind, direction=1):
        """Run one navigation jump and report where it landed. `kind` is a nav_engine kind; when None the last chosen kind is reused, which is what the menu's 'go to previous' entry needs. Failures are reported in the status line instead of raising, so the menu stays usable on a buffer the engine cannot make sense of."""
        if kind is None:
            kind = getattr(self, "_last_nav_kind", None) or "definition"
        self._last_nav_kind = kind
        document = self.navigation_document()
        line, column = self.editor.cursor_line_column()
        word = self._nav_word_for(kind)
        try:
            target = nav_engine.locate(document, kind, line, column, word,
                                       direction)
        except Exception as exc:                  # never break the tab
            self.status.setText("navigation failed: %s" % exc)
            return None
        if target is None:
            self.status.setText(self.t(
                "nav_not_found", "nothing to go to for: {kind}").format(
                kind=self._nav_label(kind)))
            return None
        self.editor.goto_location(target, select=kind != "section")
        self.status.setText(self._nav_status(target, kind))
        return target
    def _nav_word_for(self, kind):
        """The symbol a jump follows (empty when it does not need one)."""
        if kind in ("definition", "reference", "import"):
            typed = self.nav_edit.text().strip()
            return typed or self.symbol_under_cursor()
        return ""

    def goto_symbol(self):
        """Jump to the symbol typed in the navigation box. The name is looked up as a definition first and as a reference after, so the box answers both 'where is this declared' and 'where is this used'."""
        name = self.nav_edit.text().strip()
        if not name:
            self.status.setText(self.t(
                "nav_need_symbol", "type a symbol name first"))
            return None
        document = self.navigation_document()
        line, column = self.editor.cursor_line_column()
        target = nav_engine.locate(document, "definition", line, column, name)
        if target is None:
            target = nav_engine.locate(document, "reference", line, column,
                                       name)
        if target is None:
            self.status.setText(self.t(
                "nav_not_found", "nothing to go to for: {kind}").format(
                kind=name))
            return None
        self.editor.goto_location(target, select=True)
        self.status.setText(self._nav_status(target, target.kind))
        return target
    def goto_problem(self, direction=1):
        """Jump to the next or previous error/warning in the buffer."""
        document = self.navigation_document()
        line, column = self.editor.cursor_line_column()
        found = nav_engine.problems(document, "error") \
            + nav_engine.problems(document, "warning")
        target = None
        if found:
            found.sort(key=lambda item: item.key())
            if direction >= 0:
                after = [item for item in found
                         if item.key() > (line, column)]
                target = after[0] if after else found[0]
            else:
                before = [item for item in found
                          if item.key() < (line, column)]
                target = before[-1] if before else found[-1]
        if target is None:
            self.status.setText(self.t("nav_no_problem", "no problems found"))
            return None
        self.editor.goto_location(target)
        self.status.setText("%s: %s" % (target.kind.upper(), target.detail))
        return target
    def _nav_label(self, kind):
        for key, label_key, fallback in nav_engine.NAV_KINDS:
            if key == kind:
                return self.t(label_key, fallback)
        return kind
    def _nav_status(self, target, kind):
        """A short status line for a completed jump."""
        detail = target.name or target.detail or kind
        return self.t("nav_status", "{kind}: {name} (line {line})").format(
            kind=self._nav_label(kind), name=detail, line=target.line)

    # ------------------------------------------------------------ language
    def languages(self):
        """(key, label) pairs of every registered syntax module."""
        return languages()

    def current_category(self):
        """The script category the editor is working with right now."""
        script = self._find_script(self.script_id)
        if script is not None:
            return script.get("category", "") or ""
        data = self.language_combo.currentData()
        return self.language_combo.currentText() if data is None else data
    def set_language(self, category, announce=True):
        """Point the editor at the syntax module of a script category."""
        module = syntax_module(category)
        self.editor.set_category(category)
        self._sync_language_combo(module.key)
        self.module_label.setText(self.t(
            "editor_module_active",
            "module: {key}").format(key=module.key))
        if announce:
            self.status.setText(self.t(
                "editor_language_set", "language: {name}").format(
                name=module.label))
        return module
    def _sync_language_combo(self, key):
        index = self.language_combo.findData(key)
        if index < 0:
            # A language registered while the app was running: show it too.
            self.language_combo.addItem(key, key)
            index = self.language_combo.findData(key)
        if index >= 0 and index != self.language_combo.currentIndex():
            blocked = self.language_combo.blockSignals(True)
            self.language_combo.setCurrentIndex(index)
            self.language_combo.blockSignals(blocked)

    def _on_language_changed(self, _index):
        """Re-colour the open document with the chosen syntax module."""
        key = self.language_combo.currentData() or "python"
        self.set_language(key)
        if hasattr(self, "templates_menu"):
            self._rebuild_templates_menu()

    # ------------------------------------------------------------ loading
    def refresh(self):
        """Reload the editor from the window's script list. The currently edited script is preserved when it still exists; when it is gone the editor simply keeps its buffer so no work is lost."""
        if self.script_id is not None:
            script = self._find_script(self.script_id)
            if script is not None:
                self.load_script(script)
                return
        self.status.setText(self.t(
            "editor_status_ready",
            "{count} script(s) available. Load one to start editing.").format(
            count=len(self._scripts())))

    def load_selected(self):
        """Open the script currently selected on the Scripts tab."""
        script = self._selected_script()
        if script is None:
            self.status.setText(
                self.t("editor_noselect", "Select a script on the Scripts tab."))
            return
        self.load_script(script)

    def load_script(self, script):
        """Load a script entry into the editor."""
        self._loading = True
        try:
            self.script_id = script.get("id")
            module = self.set_language(
                script.get("category", "Python"), announce=False)
            self.editor.setPlainText(script.get("code", "") or "")
            self.editor.rehighlight()
            self.editor.document().setModified(False)
            self.name_label.setText("%s  [%s / %s]" % (
                script.get("name", "") or "(unnamed)",
                script.get("category", "") or "?",
                module.key))
            self.name_label.setStyleSheet("color: #dfe7f5;")
            self.dirty_label.setText("")
        finally:
            self._loading = False
        self.status.setText(self.t(
            "editor_loaded", "Loaded {name}").format(
            name=script.get("name", "")))

    def _on_text_changed(self):
        if self._loading:
            return
        self.dirty_label.setText(self.t("editor_modified", "modified"))

    # ------------------------------------------------------------ view modes
    def _on_theme_changed(self, _index):
        """Switch the editor between the dark and light palettes."""
        name = self.theme_combo.currentData() or "dark"
        self.editor.set_low_graphics(False)
        if self.chk_low_graphics.isChecked():
            self.chk_low_graphics.setChecked(False)
        self.editor.set_theme(name)
        self.status.setText(self.t(
            "editor_theme_set", "theme: {name}").format(name=name))

    def _on_low_graphics(self, enabled):
        """Toggle the minimal, monochrome, ASCII-only rendering."""
        self.editor.set_low_graphics(enabled)
        self.status.setText(self.t(
            "editor_low_graphics_on" if enabled else "editor_low_graphics_off",
            "low graphics: on" if enabled else "low graphics: off"))

    # ------------------------------------------------------------- editing
    def apply_tool(self, key):
        """Run one editor tool against the current document."""
        if key == "delete_line":
            self._delete_line()
        elif key == "move_up":
            self._move_line(-1)
        elif key == "move_down":
            self._move_line(1)
        elif key == "sort_lines":
            self._sort_lines()
        elif key == "trim_spaces":
            self._trim_spaces()
        elif key == "tabs_to_spaces":
            self._tabs_to_spaces()
        elif key == "rename":
            self._rename_symbol()
        elif key == "extract_function":
            self._extract_function()
        elif key == "extract_variable":
            self._extract_variable()
        elif key == "move_function":
            self._move_definition("function")
        elif key == "move_class":
            self._move_definition("class")
        elif key == "format_code":
            self._format_code()
        elif key == "remove_unused_imports":
            self._remove_unused_imports()
        elif key == "remove_dead_code":
            self._remove_dead_code()
        elif key == "fix_linter":
            self._fix_linter()

    def _live_entry(self):
        script = dict(self._find_script(self.script_id) or {})
        script.setdefault("id", self.script_id)
        script.setdefault("name", self.name_label.text().split("  [")[0].strip())
        script.setdefault("category", self.current_category())
        script["code"] = self.editor.toPlainText()
        return script
    def run_integration(self, key):
        'Open the current script in another application module.'
        actions = {
            "diff": self._integration_diff,
            "search": self._integration_search,
            "audit": self._integration_audit,
            "runner": self._integration_runner,
            "debugger": self._integration_debugger,
            "report": self._integration_report,
            "project": self._integration_project,
        }
        action = actions.get(key)
        if action is None:
            return None
        return action()

    def _integration_diff(self):
        diff = getattr(self.window, "diff_tab", None)
        if diff is None:
            return self._integration_status("editor_integration_unavailable", "Diff unavailable.")
        entry = self._live_entry()
        previous = self._find_script(self.script_id) or entry
        try:
            diff._left = diff._entry_to_source(previous)
            diff._right = diff._entry_to_source(entry)
            diff.recompute()
            self.window.tabs.setCurrentWidget(diff)
            return self._integration_status("editor_integration_opened", "opened in Diff")
        except Exception as exc:
            return self._integration_status("editor_integration_failed", "Integration failed: {error}", error=exc)

    def _integration_search(self):
        search = getattr(self.window, "search_tab", None)
        if search is None:
            return self._integration_status("editor_integration_unavailable", "Search unavailable.")
        try:
            self.window.tabs.setCurrentWidget(search)
            search.query_edit.setText(self.name_label.text().split("  [")[0].strip())
            search.run_search()
            return self._integration_status("editor_integration_opened", "opened in Search")
        except Exception as exc:
            return self._integration_status("editor_integration_failed", "Integration failed: {error}", error=exc)

    def _integration_audit(self):
        entry = self._find_script(self.script_id)
        method = getattr(self.window, "audit_script", None)
        if not callable(method):
            return self._integration_status("editor_integration_unavailable", "Audit unavailable.")
        method(entry)
        return self._integration_status("editor_integration_opened", "opened in Audit")

    def _integration_runner(self):
        entry = self._live_entry()
        try:
            from sdb import RunScriptDialog
            RunScriptDialog(self, entry.get("name", "Untitled"), entry.get("code", ""),
                            entry.get("category", "Python"), self.t).exec_()
            return self._integration_status("editor_integration_opened", "opened in Runner")
        except Exception as exc:
            return self._integration_status("editor_integration_failed", "Integration failed: {error}", error=exc)

    def _integration_debugger(self):
        entry = self._live_entry()
        method = getattr(self.window, "open_in_debugger", None)
        debugger = getattr(self.window, "debugger_tab", None) or getattr(self.window, "debug_tab", None)
        try:
            if callable(method):
                method(entry)
            elif debugger is not None:
                if hasattr(debugger, "load_script"):
                    debugger.load_script(entry)
                self.window.tabs.setCurrentWidget(debugger)
            else:
                return self._integration_status("editor_integration_unavailable", "Debugger unavailable.")
            return self._integration_status("editor_integration_opened", "opened in Debugger")
        except Exception as exc:
            return self._integration_status("editor_integration_failed", "Integration failed: {error}", error=exc)

    def _integration_report(self):
        entry = self._live_entry()
        report = "# ScriptDB script report\n\n## %s\n\n- Category: %s\n- Lines: %s\n\n```%s\n%s\n```\n" % (
            entry.get("name", ""), entry.get("category", ""),
            len(entry.get("code", "").splitlines()),
            entry.get("category", "").lower(), entry.get("code", ""))
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, self.t("editor_integration_report", "Generate script report"),
            (entry.get("name", "script") or "script") + "_report.md",
            "Markdown files (*.md)")
        if not path:
            return None
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(report)
        except OSError as exc:
            return self._integration_status("editor_integration_failed", "Integration failed: {error}", error=exc)
        return self._integration_status("editor_integration_report_done", "report saved: {path}", path=path)

    def _integration_project(self):
        entry = self._find_script(self.script_id)
        if entry is None:
            return self._integration_status("editor_noselect", "Select a script first.")
        project, accepted = QtWidgets.QInputDialog.getText(
            self, self.t("editor_integration_project", "Link to project"),
            self.t("editor_project_name", "Project name:"),
            QtWidgets.QLineEdit.Normal, str(entry.get("project", "")))
        if not accepted:
            return None
        entry["project"] = project.strip()
        saver = getattr(self.window, "save_data_now", None)
        if callable(saver):
            saver()
        return self._integration_status("editor_integration_project_done", "project linked: {project}", project=project.strip() or "(none)")

    def _integration_status(self, key, fallback, **kwargs):
        if not kwargs:
            kwargs = {"value": fallback}
        message = self.t(key, fallback, **kwargs)
        self.status.setText(message)
        return message
    # ------------------------------------------------------ script profiles
    def profiles_catalogue(self):
        """The script profiles as a plugin-safe structure."""
        return script_profiles.capabilities()

    def profile_contract(self, key):
        """One profile: its sections, its functions and what it is for."""
        engine = script_profiles._DEFAULT_ENGINE
        return engine.describe(key)

    def insert_profile(self, key, values=None):
        """Write a whole script from a profile and replace the buffer.

        A profile is not a snippet: it describes the whole file, so it replaces
        the buffer rather than being pasted into it.  The existing text is not
        thrown away silently -- it goes into the document history, so Undo
        brings it straight back.
        """
        if key not in script_profiles.PROFILE_KEYS:
            self.status.setText(self.t("profiles_unknown",
                                       "Unknown profile."))
            return None
        name = self._profile_name()
        options = dict(values or {})
        options.setdefault("title", self._profile_title(name))
        options.setdefault("description", self.t(
            "profiles_description",
            "A generated script."))
        result = script_profiles.generate(key, name, **options)
        if not result["ok"]:
            self.status.setText(self.t("profiles_failed",
                                       "Generation failed: {error}").format(
                error=result.get("error", "")))
            return result
        body = script_profiles.single(result)
        # The profile decides the whole file, so the buffer is replaced in one
        # undoable step rather than appended to.
        cursor = self.editor.textCursor()
        cursor.beginEditBlock()
        cursor.select(QtGui.QTextCursor.Document)
        cursor.insertText(body)
        cursor.endEditBlock()
        self.editor.setTextCursor(cursor)
        self.editor.rehighlight()
        self.dirty_label.setText(self.t("editor_modified", "modified"))
        self._announce("script profile applied: %s" % key)
        self.status.setText(self.t(
            "profiles_applied", "{name}: {summary}").format(
            name=result.get("profile_label", key),
            summary=result.get("summary", "")))
        return result
    def _profile_name(self):
        """The module name a new script should carry."""
        script = self._find_script(self.script_id) or {}
        raw = script.get("name") or self.name_label.text().split("  [")[0]
        raw = os.path.splitext(str(raw or "script").strip())[0]
        return script_profiles.identifier(raw, "script")

    def _profile_title(self, module):
        """A title that reads well, from the script name."""
        return script_profiles.title_case(module, "Script")

    # ---------------------------------------------------- code generation
    def codegen_catalogue(self):
        """The code-generation surface as a plugin-safe structure."""
        return code_generator.capabilities()

    # ------------------------------------------------------------- workflow
    # Building a script from stages (the workflow builder) or from blocks (the
    # composer).  Both live in tabs/workflow_engine.py, as do the safeguards and
    # the metrics, so what is inserted here is the same script the engine would
    # produce on its own -- this class only chooses the input.
    def run_workflow_action(self, key):
        """Run one workflow action against the editor."""
        if key == "build":
            return self.build_workflow()
        if key == "compose":
            return self.compose_workflow()
        if key == "blocks":
            return self.show_block_catalogue()
        if key == "safeguards":
            return self.add_safeguards()
        if key == "measure":
            return self.measure_current_script()
        return None
    def _workflow_dialog(self, title, roles, preselect=()):
        """A dialog listing the blocks, grouped by role, with checkboxes.

        Returns (blocks, name, options, harden), or None when cancelled.  The
        list is built from the engine's own catalogue, so a block added to the
        engine appears here without a change to this file.
        """
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle(title)
        dialog.resize(620, 660)
        layout = QtWidgets.QVBoxLayout(dialog)

        name_row = QtWidgets.QHBoxLayout()
        name_row.addWidget(QtWidgets.QLabel(self.t("workflow_name", "Name:")))
        name_edit = QtWidgets.QLineEdit("workflow")
        name_row.addWidget(name_edit, 1)
        layout.addLayout(name_row)

        tree = QtWidgets.QTreeWidget()
        tree.setColumnCount(2)
        tree.setHeaderLabels([
            self.t("workflow_block", "Block"),
            self.t("workflow_block_emits", "What it emits")])
        checks = {}
        for role in roles:
            parent = QtWidgets.QTreeWidgetItem([
                workflow_engine.WORKFLOW_ROLE_LABELS.get(role, role),
                workflow_engine.WORKFLOW_ROLE_MEANINGS.get(role, "")])
            tree.addTopLevelItem(parent)
            for block in workflow_engine.blocks_for_role(role):
                item = QtWidgets.QTreeWidgetItem([
                    workflow_engine.BLOCK_LABELS.get(block, block),
                    workflow_engine.BLOCK_MEANINGS.get(block, "")])
                item.setFlags(item.flags() | QtCore.Qt.ItemIsUserCheckable)
                item.setCheckState(QtCore.Qt.Checked if block in preselect
                                   else QtCore.Qt.Unchecked)
                item.setData(0, QtCore.Qt.UserRole, block)
                parent.addChild(item)
                checks[block] = item
            parent.setExpanded(True)
        layout.addWidget(tree, 1)

        options_row = QtWidgets.QHBoxLayout()
        path_edit = QtWidgets.QLineEdit("input.txt")
        out_edit = QtWidgets.QLineEdit("output.txt")
        url_edit = QtWidgets.QLineEdit("https://example.invalid/api")
        options_row.addWidget(QtWidgets.QLabel("Input:"))
        options_row.addWidget(path_edit, 1)
        options_row.addWidget(QtWidgets.QLabel("Output:"))
        options_row.addWidget(out_edit, 1)
        options_row.addWidget(QtWidgets.QLabel("URL:"))
        options_row.addWidget(url_edit, 1)
        layout.addLayout(options_row)

        harden_box = QtWidgets.QCheckBox(self.t(
            "workflow_safeguards_box",
            "Add the safeguards (input, config, exceptions, log, report)"))
        harden_box.setChecked(True)
        layout.addWidget(harden_box)

        layout.addWidget(QtWidgets.QLabel(
            self.t("workflow_preview", "Preview")))
        preview = QtWidgets.QPlainTextEdit()
        preview.setReadOnly(True)
        preview.setFont(QFont(EDITOR_FONT, EDITOR_FONT_SIZE - 1))
        preview.setFixedHeight(200)
        layout.addWidget(preview)

        def chosen():
            return [block for block, item in checks.items()
                    if item.checkState() == QtCore.Qt.Checked]

        def collect_options():
            return {"path": path_edit.text().strip(),
                    "output": out_edit.text().strip(),
                    "url": url_edit.text().strip()}

        def refresh_preview():
            blocks = chosen()
            if not blocks:
                preview.setPlainText("")
                return
            builder = workflow_engine.WorkflowBuilder()
            if set(roles) == set(workflow_engine.WORKFLOW_ROLE_KEYS):
                result = builder.build(
                    stages=blocks, name=name_edit.text().strip() or "workflow",
                    options=collect_options(), harden=harden_box.isChecked())
            else:
                result = builder.compose(
                    blocks=blocks, name=name_edit.text().strip() or "composed",
                    options=collect_options(), harden=harden_box.isChecked())
            body = (result.get("files") or [("", "")])[0][1] if result.get(
                "ok") else "# " + str(result.get("error"))
            preview.setPlainText(body)

        tree.itemChanged.connect(lambda *_a: refresh_preview())
        harden_box.toggled.connect(refresh_preview)
        name_edit.textChanged.connect(refresh_preview)
        refresh_preview()

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            return None
        return (chosen(), name_edit.text().strip(), collect_options(),
                harden_box.isChecked())

    def build_workflow(self):
        """Build a script from ordered stages and insert it."""
        answer = self._workflow_dialog(
            self.t("editor_workflow_build", "Build workflow..."),
            workflow_engine.WORKFLOW_ROLE_KEYS,
            preselect=("config", "read_file", "transform", "write_file",
                       "log"))
        if answer is None:
            return None
        blocks, name, options, harden = answer
        result = workflow_engine.WorkflowBuilder().build(
            stages=blocks, name=name or "workflow", options=options,
            harden=harden)
        return self._finish_workflow(result, "workflow")

    def compose_workflow(self):
        """Compose a script from a stack of blocks and insert it."""
        answer = self._workflow_dialog(
            self.t("editor_workflow_compose", "Compose from blocks..."),
            ("input", "process", "output", "log", "error"),
            preselect=("read_file", "parse_json", "write_file"))
        if answer is None:
            return None
        blocks, name, options, harden = answer
        result = workflow_engine.WorkflowBuilder().compose(
            blocks=blocks, name=name or "composed", options=options,
            harden=harden)
        return self._finish_workflow(result, "composer")

    def _finish_workflow(self, result, label):
        """Insert a workflow or composer result, or report why it failed."""
        if not result.get("ok"):
            self.status.setText(self.t(
                "workflow_failed", "Could not build: {error}").format(
                error=result.get("error", "")))
            return result
        body = workflow_engine.WorkflowBuilder.single(result)
        self._insert_generated(body, result.get("name", "%s.py" % label))
        self._announce("editor %s: %s" % (label, result.get("summary", "")))
        metrics = result.get("metrics") or {}
        self.status.setText(self.t(
            "workflow_done", "{summary} - {detail}").format(
            summary=result.get("summary", ""),
            detail=metrics.get("detail", "")))
        return result
    def show_block_catalogue(self):
        """List every block and safeguard the engine offers, as data."""
        lines = [self.t("workflow_blocks_title", "Blocks"), ""]
        for role in workflow_engine.WORKFLOW_ROLE_KEYS:
            lines.append("%s -- %s" % (
                workflow_engine.WORKFLOW_ROLE_LABELS.get(role, role),
                workflow_engine.WORKFLOW_ROLE_MEANINGS.get(role, "")))
            for block in workflow_engine.blocks_for_role(role):
                lines.append("    %-16s %s" % (
                    block, workflow_engine.BLOCK_MEANINGS.get(block, "")))
            lines.append("")
        lines.append(self.t("workflow_safeguards_title", "Safeguards"))
        lines.append("")
        for key, _lk, _fallback, detail in workflow_engine.SAFEGUARDS:
            lines.append("    %-16s %s" % (key, detail))
        self._show_text_dialog(
            self.t("editor_workflow_blocks", "Block catalogue"),
            "\n".join(lines))
        return workflow_engine.BLOCK_KEYS
    def add_safeguards(self):
        """Wrap the buffer in the engine's safeguards, in place.

        The script's own code is left exactly as it is; the pieces that make it
        safe are added around it.  Anything already present is not added twice.
        """
        source = self.editor.toPlainText()
        if not source.strip():
            self.status.setText(self.t(
                "editor_empty", "Nothing to protect: the editor is empty."))
            return None
        result = workflow_engine.harden(source)
        self.editor.setPlainText(result["source"])
        self.editor.rehighlight()
        self.dirty_label.setText(self.t("editor_modified", "modified"))
        self._announce("editor safeguards added: %s" % result["detail"])
        self.status.setText(self.t(
            "workflow_hardened", "Safeguards added: {detail}").format(
            detail=result["detail"]))
        return result
    def measure_current_script(self):
        """Describe the buffer: size, shape, problems and complexity."""
        metrics = workflow_engine.measure(self.editor.toPlainText())
        lines = [metrics.get("detail", ""), ""]
        for key in ("lines", "code_lines", "characters", "functions",
                    "methods", "classes", "imports", "cyclomatic",
                    "logical", "errors", "warnings"):
            lines.append("%-14s %s" % (key + ":", metrics.get(key, 0)))
        if not metrics.get("parses"):
            lines.append("")
            lines.append(self.t("workflow_parse_error",
                                "does not parse: {detail}").format(
                detail=metrics.get("parse_error", "")))
        self._show_text_dialog(
            self.t("editor_workflow_measure", "Measure this script"),
            "\n".join(lines))
        self.status.setText(metrics.get("detail", ""))
        return metrics
    def workflow_contract(self):
        """The workflow surface as a plugin-safe structure."""
        return {
            "actions": {key: {"name": self.t(label_key, fallback),
                              "status": "available"}
                        for key, label_key, fallback in EDITOR_WORKFLOW_ACTIONS},
            "roles": list(workflow_engine.WORKFLOW_ROLE_KEYS),
            "blocks": {block: {"role": workflow_engine.BLOCK_ROLES[block],
                               "detail": workflow_engine.BLOCK_MEANINGS[block],
                               "status": "available"}
                       for block in workflow_engine.BLOCK_KEYS},
            "safeguards": {key: {"name": fallback, "detail": detail,
                                 "status": "available"}
                           for key, _lk, fallback, detail
                           in workflow_engine.SAFEGUARDS},
        }

    def run_codegen(self, key):
        """Generate one artifact from a source the user picks or types.

        The source is either a file on disk (a config, a JSON document, an
        OpenAPI definition) or text pasted into the dialog -- for the project
        kind it can also be a directory.  The artifact is inserted at the
        cursor, so the generator never silently replaces the buffer.
        """
        if key not in code_generator.CODE_GENERATOR_KEYS:
            self.status.setText(self.t("codegen_unknown",
                                       "Unknown generator."))
            return None
        source = self._codegen_source(key)
        if source is None:
            return None
        result = code_generator.generate(key, source)
        if not result.get("ok"):
            self.status.setText(self.t(
                "codegen_failed", "Generation failed: {error}").format(
                error=result.get("error", "")))
            return result
        body = code_generator.CodeGenerator.single(result)
        if len(result["files"]) == 1:
            self._insert_generated(body, result["name"])
        else:
            self._show_generated_files(result)
        self._announce("code generated: %s" % key)
        notes = "; ".join(result.get("notes") or [])
        self.status.setText(self.t(
            "codegen_done", "generated {name} ({count} file(s)): {notes}"
        ).format(name=result.get("name", ""), count=len(result["files"]),
                 notes=notes))
        return result
    def _codegen_source(self, key):
        """Ask for the input one generator kind needs."""
        if key == "project":
            choice = QtWidgets.QMessageBox.question(
                self, self.t("editor_codegen_menu", "Code generation"),
                self.t("codegen_project_source",
                       "Take the project from a directory on disk?"),
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.Yes)
            if choice == QtWidgets.QMessageBox.Yes:
                root = QtWidgets.QFileDialog.getExistingDirectory(
                    self, self.t("codegen_kind_project", "Project scaffold"))
                return root or None
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, self.t("codegen_source_file", "Choose a source file"), "",
            self.t("codegen_source_filter",
                   "Definitions (*.json *.yaml *.yml *.ini *.env *.txt);;"
                   "All files (*.*)"))
        if path:
            return path
        # No file chosen: let the user paste the definition instead.
        return self._codegen_paste(key)

    def _codegen_paste(self, key):
        """A small editor so a definition can be typed straight in."""
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle(self.t("codegen_paste",
                                     "Paste the input for {name}").format(
            name=code_generator.CODE_GENERATOR_LABELS.get(key, key)))
        dialog.resize(640, 420)
        layout = QtWidgets.QVBoxLayout(dialog)
        layout.addWidget(QtWidgets.QLabel(
            code_generator.CODE_GENERATOR_READS.get(key, "")))
        view = QtWidgets.QPlainTextEdit()
        view.setPlaceholderText(self._codegen_placeholder(key))
        layout.addWidget(view, 1)
        buttons = QtWidgets.QHBoxLayout()
        use_editor = QtWidgets.QPushButton(
            self.t("codegen_use_buffer", "Use the editor buffer"))
        use_editor.clicked.connect(
            lambda: (view.setPlainText(self.editor.toPlainText()),
                     dialog.accept()))
        accept = QtWidgets.QPushButton(self.t("ok", "OK"))
        accept.clicked.connect(dialog.accept)
        cancel = QtWidgets.QPushButton(
            self.t("editor_analysis_close", "Close"))
        cancel.clicked.connect(dialog.reject)
        buttons.addWidget(use_editor)
        buttons.addStretch()
        buttons.addWidget(cancel)
        buttons.addWidget(accept)
        layout.addLayout(buttons)
        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            return None
        text = view.toPlainText()
        return text or None
    @staticmethod
    def _codegen_placeholder(key):
        """A one-line example of what the generator expects."""
        return {
            "config": '{"host": "localhost", "port": 8080}',
            "metadata": '{"name": "My Project", "version": "1.0"}',
            "json": '{"id": 1, "address": {"city": "Warsaw"}}',
            "api": 'GET /users - list users\nGET /users/{id} - fetch one',
            "modules": 'core: core helpers\nnet: network layer',
            "project": 'My Project\nA short description.',
        }.get(key, "")

    def _insert_generated(self, body, name):
        """Insert a generated artifact at the cursor."""
        if not body:
            return False
        cursor = self.editor.textCursor()
        pad = "" if cursor.atBlockStart() and self.editor.toPlainText() == "" \
            else "\n\n"
        cursor.insertText(pad + body)
        self.editor.setTextCursor(cursor)
        self.editor.rehighlight()
        self.dirty_label.setText(self.t("editor_modified", "modified"))
        return True
    def _show_generated_files(self, result):
        """A multi-file artifact (a package, a scaffold) in a tree, savable."""
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("%s  |  %s" % (result.get("name", ""),
                                             result.get("summary", "")))
        dialog.resize(880, 560)
        layout = QtWidgets.QVBoxLayout(dialog)
        tree = QtWidgets.QTreeWidget()
        tree.setColumnCount(2)
        tree.setHeaderLabels([self.t("codegen_file", "File"),
                              self.t("editor_analysis_value", "Value")])
        for path, body in result["files"]:
            item = QtWidgets.QTreeWidgetItem(
                [path, "%d line(s)" % len(body.splitlines())])
            item.setData(0, QtCore.Qt.UserRole, body)
            tree.addTopLevelItem(item)
        tree.itemDoubleClicked.connect(
            lambda item, _col: self._preview_generated(
                item.text(0), item.data(0, QtCore.Qt.UserRole)))
        layout.addWidget(tree, 1)
        buttons = QtWidgets.QHBoxLayout()
        save = QtWidgets.QPushButton(
            self.t("codegen_save_all", "Save the files..."))
        save.clicked.connect(lambda: self._save_generated(result, dialog))
        insert = QtWidgets.QPushButton(
            self.t("codegen_insert_first", "Insert the first file"))
        insert.clicked.connect(lambda: (dialog.accept(), self._insert_generated(
            result["files"][0][1], result["files"][0][0])))
        close = QtWidgets.QPushButton(self.t("editor_analysis_close", "Close"))
        close.clicked.connect(dialog.close)
        buttons.addWidget(save)
        buttons.addWidget(insert)
        buttons.addStretch()
        buttons.addWidget(close)
        layout.addLayout(buttons)
        dialog.exec_()

    def _preview_generated(self, name, body):
        self._show_text_dialog(name, body or "")

    def _save_generated(self, result, parent):
        """Write every generated file below a directory the user chooses."""
        root = QtWidgets.QFileDialog.getExistingDirectory(
            parent, self.t("codegen_save_all", "Save the files..."))
        if not root:
            return None
        written = 0
        for path, body in result["files"]:
            target = os.path.join(root, *path.split("/"))
            try:
                folder = os.path.dirname(target)
                if folder and not os.path.isdir(folder):
                    os.makedirs(folder)
                with open(target, "w", encoding="utf-8") as handle:
                    handle.write(body)
                written += 1
            except OSError as exc:
                self.status.setText(str(exc))
                return None
        self._integration_status("codegen_saved", "saved {count} file(s) in {root}",
                                 count=written, root=root)
        return written
    # ---------------------------------------------------------- code rules
    def coderules_catalogue(self):
        """The code rules surface as a plugin-safe structure."""
        return code_rules.capabilities()

    def _current_language(self):
        """The language key the rules should run against."""
        return language_key(self.current_category())

    def run_code_rule(self, key):
        """Apply one code rule to the live buffer."""
        if key not in code_rules.CODE_RULE_KEYS:
            self.status.setText(self.t("coderules_unknown", "Unknown rule."))
            return None
        code = self.editor.toPlainText()
        result = code_rules.apply_rule(key, code, self._current_language())
        if result["changed"]:
            self._replace_document(result["code"], self.t(
                "coderules_applied", "{name}: {notes}").format(
                name=code_rules.CODE_RULE_TITLES.get(key, key),
                notes="; ".join(result["notes"])))
            self._announce("code rule applied: %s" % key)
        else:
            self.status.setText(self.t(
                "coderules_nothing", "{name}: nothing to fix").format(
                name=code_rules.CODE_RULE_TITLES.get(key, key)))
        return result
    def run_all_code_rules(self):
        """Run every rule that applies to the live language."""
        outcome = code_rules.fix_all(self.editor.toPlainText(),
                                     self._current_language())
        applied = outcome.get("applied") or []
        if not applied:
            self.status.setText(self.t("coderules_none_applied",
                                       "no rule changed the buffer"))
            return outcome
        self._replace_document(outcome["code"], self.t(
            "coderules_all_applied", "applied {count} rule(s): {names}").format(
            count=len(applied), names=", ".join(
                code_rules.CODE_RULE_TITLES.get(key, key) for key in applied)))
        self._announce("code rules applied: %s" % ", ".join(applied))
        return outcome
    def lint_code_rules(self):
        """Report what the rules would change, without changing anything."""
        outcome = code_rules.lint(self.editor.toPlainText(),
                                  self._current_language())
        findings = outcome.get("findings") or []
        if not findings:
            self.status.setText(self.t("coderules_clean",
                                       "every rule is satisfied"))
            self._show_text_dialog(self.t("coderules_lint",
                                          "Code rules report"),
                                   code_rules.report(self.editor.toPlainText(),
                                                     self._current_language()))
            return outcome
        lines = []
        for finding in findings:
            lines.append("* %s" % finding["name"])
            lines.append("    %s" % finding["detail"])
        self._show_text_dialog(self.t("coderules_lint",
                                      "Code rules report"),
                               "\n".join(lines))
        self.status.setText(outcome["summary"])
        return outcome
    def show_code_rules_report(self):
        """The full rules report, as text that can be copied or exported."""
        text = code_rules.report(self.editor.toPlainText(),
                                 self._current_language())
        self._show_text_dialog(self.t("coderules_report",
                                      "Rules report"), text)
        return text
    # --------------------------------------------------------- templates
    def _rebuild_templates_menu(self):
        """Fill the Templates menu from the engine, for the live language. The menu is built from `templates.templates_for`, so it can only offer a template the engine actually has, and it is rebuilt when the language changes -- which is what makes the list language-aware without a second copy of the catalogue living in this file."""
        menu = getattr(self, "templates_menu", None)
        if menu is None:
            return
        menu.clear()
        self.template_actions = {}
        language = None
        try:
            language = syntax.module_for(self.current_category()).key
        except Exception:
            language = None
        grouped = templates.templates_for(language)
        if not grouped:
            grouped = templates.templates_for(None)
        for category, items in grouped:
            submenu = menu.addMenu(self.t("template_cat_" + category,
                                          templates.CATEGORY_LABELS.get(
                                              category, category)))
            for key, label in items:
                action = submenu.addAction(self.t("template_auto_" + key,
                                                  label))
                action.triggered.connect(
                    lambda _checked=False, k=key: self.insert_template(k))
                self.template_actions[key] = action

    def template_toolbox(self):
        """The templates offered for the current language, by category."""
        try:
            language = syntax.module_for(self.current_category()).key
        except Exception:
            language = None
        return templates.templates_for(language)

    def insert_template(self, key, values=None):
        """Insert one template at the cursor, indented to where it sits.

        The block replaces the current selection when there is one and is
        otherwise inserted at the cursor. Either way it is indented to the
        leading whitespace of the line the cursor is on, so a template dropped
        into a class body or an existing function lines up on its own.
        """
        if key not in templates.TEMPLATE_KEYS:
            self.status.setText(self.t("editor_template_unknown",
                                       "Unknown template."))
            return None
        cursor = self.editor.textCursor()
        line_text = cursor.block().text()
        block = templates.apply(key, cursor_line=line_text,
                                values=values or {})
        if not block:
            return None
        cursor.insertText(block)
        self.editor.setTextCursor(cursor)
        self.editor.rehighlight()
        self._announce("template inserted: %s" % key)
        self.status.setText(self.t(
            "editor_template_inserted", "inserted template: {name}"
        ).format(name=templates.TEMPLATE_LABELS.get(key, key)))
        return block

    def add_template(self, category, key, label, body, languages=None):
        """Register a template at run time (hot-plugging).

        A plugin adds a template to the engine catalogue and calls this, and the
        menu is rebuilt for the new entry; nothing in this file knows the plugin
        exists.
        """
        outcome = templates.register(category, key, label, body,
                                     languages or templates.ANY_LANGUAGE)
        self._rebuild_templates_menu()
        return outcome

    def templates_contract(self):
        """The template engine as a plugin-safe structure."""
        try:
            language = syntax.module_for(self.current_category()).key
        except Exception:
            language = None
        return templates.capabilities(language)

    def run_workspace_action(self, key):
        actions = {
            "architecture": self._show_editor_architecture,
            "project_tree": self._workspace_project_tree,
            "open_file": self._workspace_open_file,
            "recent_files": self._workspace_recent_files,
            "recent_projects": self._workspace_recent_projects,
            "snapshot": self._workspace_snapshot,
            "file_history": self._workspace_file_history,
            "project_history": self._workspace_project_history,
            "sandbox": self._workspace_sandbox,
            "readonly": self._workspace_readonly,
            "hash": self._workspace_hash,
            "verify": self._workspace_verify,
            "access_log": self._workspace_access_log,
        }
        action = actions.get(key)
        return action() if action else None
    def architecture_contract(self):
        'Return the editor architecture contract as a plugin-safe structure.'
        return {
            key: {"name": self.t(label_key, fallback), "detail": detail,
                  "status": "available"}
            for key, label_key, fallback, detail in EDITOR_ARCHITECTURE
        }

    def toolbox_contract(self):
        'The editor toolbox as a plugin-safe structure, grouped by kind.'
        toolbox = {group: {} for group in EDITOR_TOOLBOX_GROUPS}
        for group, key, label_key, fallback, meaning in EDITOR_TOOLBOX:
            toolbox.setdefault(group, {})[key] = {
                "name": self.t(label_key, fallback),
                "detail": meaning,
                "status": "available",
            }
        return toolbox
    def analysis_contract(self):
        'The Script Analyzer Integration as a plugin-safe structure.'
        return {
            key: {"name": self.t(label_key, fallback), "detail": detail,
                  "sections": list(sections), "status": "available"}
            for key, label_key, fallback, detail, sections in EDITOR_ANALYSIS
        }

    def integrations_contract(self):
        'The cross-module integration surface as a plugin-safe structure.'
        return {
            key: {"name": self.t(label_key, fallback), "detail": gives,
                  "target": target, "status": "available"}
            for key, label_key, fallback, _receives, target, gives
            in EDITOR_INTEGRATIONS
        }

    def filesystem_contract(self):
        'The file-system and project surface as a plugin-safe structure.'
        return {
            key: {"name": self.t(label_key, fallback), "detail": gives,
                  "reads": reads, "status": "available"}
            for key, label_key, fallback, reads, gives in EDITOR_FILE_SYSTEM
        }

    def _show_editor_architecture(self):
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle(self.t("editor_architecture_menu", "Editor architecture"))
        dialog.resize(860, 560)
        layout = QtWidgets.QVBoxLayout(dialog)
        tree = QtWidgets.QTreeWidget()
        tree.setColumnCount(3)
        tree.setHeaderLabels([
            self.t("editor_arch_item", "Item"),
            self.t("editor_arch_kind", "Kind"),
            self.t("editor_arch_detail", "Detail"),
        ])
        layout.addWidget(tree, 1)
        for _key, label_key, fallback, detail in EDITOR_ARCHITECTURE:
            item = QtWidgets.QTreeWidgetItem([
                self.t(label_key, fallback),
                self.t("editor_arch_standard", "standard"),
                self.t("editor_arch_detail_" + _key, detail),
            ])
            item.setForeground(0, QtGui.QColor("#8ab4f8"))
            tree.addTopLevelItem(item)
        tree.expandAll()
        self._announce("architecture contract viewed")
        close = QtWidgets.QPushButton(self.t("editor_analysis_close", "Close"))
        close.clicked.connect(dialog.close)
        layout.addWidget(close)
        dialog.exec_()

    def _workspace_project_tree(self):
        root = QtWidgets.QFileDialog.getExistingDirectory(self, self.t("editor_workspace_tree", "Project tree"))
        if not root:
            return None
        self._recent_projects = [root] + [p for p in self._recent_projects if p != root]
        self._recent_projects = self._recent_projects[:RECENT_PROJECTS_LIMIT]
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle(root)
        dialog.resize(700, 500)
        layout = QtWidgets.QVBoxLayout(dialog)
        tree = QtWidgets.QTreeWidget()
        tree.setHeaderLabels([self.t("editor_workspace_path", "Path")])
        layout.addWidget(tree)
        root_item = QtWidgets.QTreeWidgetItem([root])
        tree.addTopLevelItem(root_item)
        for base, dirs, files in os.walk(root):
            dirs[:] = sorted(d for d in dirs if d not in {".git", "__pycache__", ".venv"})
            parent = root_item
            rel = os.path.relpath(base, root)
            if rel != ".":
                for part in rel.split(os.sep):
                    found = next((parent.child(i) for i in range(parent.childCount()) if parent.child(i).text(0) == part), None)
                    parent = found or parent
            for name in sorted(files):
                item = QtWidgets.QTreeWidgetItem([name])
                item.setData(0, QtCore.Qt.UserRole, os.path.join(base, name))
                parent.addChild(item)
        tree.itemDoubleClicked.connect(lambda item, _col: self._open_workspace_path(item.data(0, QtCore.Qt.UserRole)))
        tree.expandToDepth(1)
        dialog.exec_()

    def _open_workspace_path(self, path):
        if not path or not os.path.isfile(path):
            return
        self._recent_files = [path] + [p for p in self._recent_files if p != path]
        self._recent_files = self._recent_files[:RECENT_FILES_LIMIT]
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                code = handle.read()
        except OSError as exc:
            return self._integration_status("editor_integration_failed", "Integration failed: {error}", error=exc)
        entry = {"id": None, "name": os.path.basename(path), "category": "Python" if path.lower().endswith(".py") else "Other", "code": code, "path": path, "history": []}
        self.load_script(entry)
        self._access_log.append({"time": _now(), "path": path, "action": "read"})
        return path
    def _workspace_open_file(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, self.t("editor_workspace_open", "Open file"), "", supported_extensions_filter())
        return self._open_workspace_path(path) if path else None
    def _workspace_recent_files(self):
        return self._show_choices(self._recent_files, self._open_workspace_path, "editor_workspace_recent_files")

    def _workspace_recent_projects(self):
        return self._show_choices(self._recent_projects, lambda path: self._workspace_project_tree_at(path), "editor_workspace_recent_projects")

    def _show_choices(self, values, callback, title_key):
        if not values:
            self.status.setText(self.t("editor_workspace_empty", "No recent entries."))
            return None
        value, ok = QtWidgets.QInputDialog.getItem(self, self.t(title_key, title_key), self.t("editor_workspace_choose", "Choose:"), values, 0, False)
        return callback(value) if ok else None
    def _workspace_project_tree_at(self, root):
        if not os.path.isdir(root):
            return None
        self._recent_projects = [root] + [p for p in self._recent_projects if p != root]
        return self._show_project_tree(root)

    def _show_project_tree(self, root):
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle(root)
        dialog.resize(700, 500)
        layout = QtWidgets.QVBoxLayout(dialog)
        tree = QtWidgets.QTreeWidget()
        tree.setHeaderLabels([self.t("editor_workspace_path", "Path")])
        layout.addWidget(tree)
        root_item = QtWidgets.QTreeWidgetItem([root])
        tree.addTopLevelItem(root_item)
        for base, dirs, files in os.walk(root):
            dirs[:] = sorted(d for d in dirs if d not in {".git", "__pycache__", ".venv"})
            parent = root_item
            rel = os.path.relpath(base, root)
            if rel != ".":
                for part in rel.split(os.sep):
                    found = next((parent.child(i) for i in range(parent.childCount()) if parent.child(i).text(0) == part), None)
                    parent = found or parent
            for name in sorted(files):
                item = QtWidgets.QTreeWidgetItem([name])
                item.setData(0, QtCore.Qt.UserRole, os.path.join(base, name))
                parent.addChild(item)
        tree.itemDoubleClicked.connect(lambda item, _col: self._open_workspace_path(item.data(0, QtCore.Qt.UserRole)))
        tree.expandToDepth(1)
        dialog.exec_()
    def _workspace_snapshot(self):
        root = self._recent_projects[0] if self._recent_projects else self._data_dir()
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, self.t("editor_workspace_snapshot", "Create project snapshot"), os.path.join(root, "project_snapshot.json"), "JSON files (*.json)")
        if not path:
            return None
        files = []
        for base, _dirs, names in os.walk(root):
            for name in names:
                full = os.path.join(base, name)
                try:
                    with open(full, "rb") as handle:
                        files.append({"path": os.path.relpath(full, root), "sha256": hashlib.sha256(handle.read()).hexdigest()})
                except OSError:
                    pass
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"root": root, "created": _now(), "files": files}, handle, indent=2)
        return self._integration_status("editor_workspace_snapshot_done", "snapshot saved: {path}", path=path)

    def _workspace_file_history(self):
        script = self._find_script(self.script_id)
        if script is None:
            return self._integration_status("editor_noselect", "Select a script first.")
        history = script.get("history", []) or []
        self._show_text_dialog(self.t("editor_workspace_file_history", "File change history"), "\n".join("%s - %s" % (item.get("date", ""), item.get("name", "")) for item in history) or self.t("editor_workspace_empty", "No history."))

    def _workspace_project_history(self):
        self._show_text_dialog(self.t("editor_workspace_project_history", "Project change history"), "\n".join("%s - %s" % (item.get("time", ""), item.get("path", "")) for item in self._access_log) or self.t("editor_workspace_empty", "No history."))

    def _show_text_dialog(self, title, text):
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle(title)
        dialog.resize(700, 420)
        layout = QtWidgets.QVBoxLayout(dialog)
        view = QtWidgets.QPlainTextEdit(text)
        view.setReadOnly(True)
        layout.addWidget(view)
        close = QtWidgets.QPushButton(self.t("editor_analysis_close", "Close"))
        close.clicked.connect(dialog.close)
        layout.addWidget(close)
        dialog.exec_()

    def _workspace_sandbox(self):
        return self._integration_status("editor_security_sandbox_info", "Sandbox preview is static; execution still uses the configured runner.")

    def _workspace_readonly(self):
        self._read_only_lock = not self._read_only_lock
        self.editor.setReadOnly(self._read_only_lock)
        self._integration_status("editor_security_readonly_state", "read-only: {state}", state="on" if self._read_only_lock else "off")

    def _workspace_hash(self):
        digest = hashlib.sha256(self.editor.toPlainText().encode("utf-8")).hexdigest()
        self._file_baseline_hash = digest
        return self._integration_status("editor_security_hash_value", "SHA-256: {hash}", hash=digest)

    def _workspace_verify(self):
        digest = hashlib.sha256(self.editor.toPlainText().encode("utf-8")).hexdigest()
        baseline = self._file_baseline_hash
        state = "unchanged" if baseline is None or digest == baseline else "changed"
        return self._integration_status("editor_security_verify_state", "integrity: {state}", state=state)

    def _workspace_access_log(self):
        self._show_text_dialog(self.t("editor_security_access", "File access audit"), "\n".join("%s | %s | %s" % (item.get("time", ""), item.get("action", ""), item.get("path", "")) for item in self._access_log) or self.t("editor_workspace_empty", "No access entries."))

    # ------------------------------------------------------- metadata
    # The Editor edits a script's code; these actions edit the metadata that
    # lives in that same script's header.  Both go through tabs/metadata_core.py,
    # so the header written here is the one the Metadata tab reads back, and the
    # validation is the same rule set.
    def _editor_language_key(self):
        """The metadata-core language key for the buffer's current syntax."""
        try:
            module = self.current_module()
            return getattr(module, "key", "text") or "text"
        except Exception:
            return "text"

    def _editor_code_text(self):
        """The live buffer, as the text metadata_core parses."""
        return self.editor.toPlainText()

    def _set_editor_code_text(self, text, until_text=None):
        """Replace the buffer with ``text`` and announce it like an edit.

        Replacing the buffer is done through the editor's own document so undo,
        the dirty marker and the syntax highlighter all keep working; the cursor
        is put at the end of the metadata block, which is where a person looks
        next.
        """
        self._loading = True
        try:
            self.editor.setPlainText(text)
            self.editor.rehighlight()
        finally:
            self._loading = False
        self.dirty_label.setText(self.t("editor_modified", "modified"))
        if until_text:
            self._reveal_line(until_text)

    def _reveal_line(self, needle):
        """Scroll to, and select, the first line containing ``needle``."""
        lines = self.editor.toPlainText().split("\n")
        for index, line in enumerate(lines):
            if needle in line:
                cursor = self.editor.textCursor()
                cursor.movePosition(QtGui.QTextCursor.Start)
                cursor.movePosition(QtGui.QTextCursor.Down, n=index)
                self.editor.setTextCursor(cursor)
                self.editor.centerCursor()
                return index + 1
        return None
    def _current_metadata(self):
        """Parse the header out of the live buffer, with the field defaults.

        Uses a :class:`metadata_core.MetadataStore` pointed at the folder the
        editor's script would be written to, so the defaults (name from the
        filename, the file's own timestamps) match what the Metadata tab shows.
        """
        language = self._editor_language_key()
        parsed = metadata_core.parse_header(self._editor_code_text(), language)
        values, custom = metadata_core.header_values(parsed["fields"])
        for key, value in self._metadata_defaults().items():
            if not str(values.get(key) or "").strip():
                values[key] = value
        return parsed, values, custom
    def _metadata_defaults(self):
        """The values a metadata header falls back to when a field is empty."""
        script = self._find_script(self.script_id) or {}
        name = (script.get("name") or "").strip()
        # A script name carries its extension ("deploy.py"); the metadata name
        # field wants the bare name ("deploy"). The extension is stripped by
        # its own suffix, not by the language key -- those differ ("python" vs
        # ".py"), which is a mistake that would put the suffix in the field.
        stem = os.path.splitext(name)[0]
        return {"name": stem or name or "script"}

    def run_metadata_action(self, key):
        """Run one metadata action against the live editor buffer.

        Nothing here writes to disk: the buffer is changed, and the existing
        Save button is what files it.  That keeps one path to a saved script,
        so the history revision and the audit announcement still happen exactly
        once, however the buffer came to be changed.
        """
        if key == "read":
            return self.show_metadata()
        if key == "edit":
            return self.edit_metadata()
        if key == "insert":
            return self.insert_metadata_header()
        if key == "validate":
            return self.validate_metadata()
        if key == "stamp":
            return self.stamp_metadata()
        if key == "bump":
            return self.bump_metadata_version()
        if key == "strip":
            return self.strip_metadata_header()
        return None
    def show_metadata(self):
        """Show the metadata currently in the buffer, as a readable list."""
        parsed, values, custom = self._current_metadata()
        if not parsed["found"]:
            self.status.setText(self.t(
                "editor_meta_absent",
                "This script has no metadata header yet."))
            self._show_text_dialog(
                self.t("editor_meta_read", "Show metadata"),
                self.t("editor_meta_absent",
                       "This script has no metadata header yet."))
            return {"found": False, "values": values, "custom": custom}
        lines = []
        for field in metadata_core.METADATA_FIELD_KEYS:
            value = str(values.get(field) or "")
            if value:
                label = metadata_core.METADATA_FIELD_LABELS.get(field, field)
                lines.append("%-22s %s" % (label + ":", value))
        for field in sorted(custom):
            lines.append("%-22s %s" % (field + ":", custom[field]))
        findings = metadata_core.validate(values, custom)
        lines += ["", metadata_core.validation_summary(findings)]
        for item in findings:
            lines.append("  [%s] %s: %s" % (item["severity"].upper(),
                                            item["field"], item["detail"]))
        self._show_text_dialog(self.t("editor_meta_read", "Show metadata"),
                               "\n".join(lines))
        return {"found": True, "values": values, "custom": custom,
                "findings": findings}

    def validate_metadata(self):
        """Check the buffer's metadata against the rules and report the result."""
        _parsed, values, custom = self._current_metadata()
        findings = metadata_core.validate(values, custom)
        if not findings:
            message = self.t("editor_meta_valid", "metadata is valid")
        else:
            message = metadata_core.validation_summary(findings)
        self.status.setText(message)
        self._show_text_dialog(
            self.t("editor_meta_validate", "Validate metadata"),
            "\n".join([message, ""] + [
                "[%s] %s: %s" % (item["severity"].upper(), item["field"],
                                 item["detail"]) for item in findings]))
        return findings
    def edit_metadata(self):
        """Edit every metadata field in a dialog, then put it in the buffer.

        The dialog is built from ``metadata_core.METADATA_FIELDS``, so a field
        added to the engine appears here without a change to this file, and the
        values are rendered by the engine's own ``render_header``, so the block
        written here is byte-for-byte what the Metadata tab would write.
        """
        language = self._editor_language_key()
        _parsed, values, custom = self._current_metadata()

        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle(self.t("editor_meta_edit", "Edit metadata..."))
        dialog.resize(660, 620)
        outer = QtWidgets.QVBoxLayout(dialog)
        form = QtWidgets.QFormLayout()
        editors = {}
        for key, kind in ((k, metadata_core.METADATA_FIELD_KINDS.get(k, "text"))
                          for k in metadata_core.METADATA_FIELD_KEYS):
            if kind == "choice":
                widget = QtWidgets.QComboBox()
                widget.addItem("", "")
                for value, _lk, label in metadata_core.METADATA_CHOICES.get(key,
                                                                           ()):
                    widget.addItem(label, value)
                # Choices are stored by their own key, so match the value.
                index = widget.findData(str(values.get(key) or ""))
                widget.setCurrentIndex(index if index >= 0 else 0)
            elif kind == "multiline":
                widget = QtWidgets.QPlainTextEdit()
                widget.setFixedHeight(58)
                widget.setPlainText(str(values.get(key) or ""))
            else:
                widget = QtWidgets.QLineEdit()
                widget.setText(str(values.get(key) or ""))
            editors[key] = widget
            label = metadata_core.METADATA_FIELD_LABELS.get(key, key)
            form.addRow(label, widget)
        outer.addLayout(form)

        preview_label = QtWidgets.QLabel(
            self.t("editor_meta_preview", "Header preview"))
        outer.addWidget(preview_label)
        preview = QtWidgets.QPlainTextEdit()
        preview.setReadOnly(True)
        preview.setFont(QFont(EDITOR_FONT, EDITOR_FONT_SIZE - 1))
        preview.setFixedHeight(180)
        outer.addWidget(preview)

        def collect():
            out = {}
            for key, widget in editors.items():
                if isinstance(widget, QtWidgets.QComboBox):
                    out[key] = widget.currentData() or ""
                elif isinstance(widget, QtWidgets.QPlainTextEdit):
                    out[key] = widget.toPlainText()
                else:
                    out[key] = widget.text()
            return out
        findings_view = QtWidgets.QPlainTextEdit()
        findings_view.setReadOnly(True)
        findings_view.setFixedHeight(80)
        outer.addWidget(findings_view)

        def refresh_preview():
            current = collect()
            preview.setPlainText("\n".join(
                metadata_core.render_header(current, language, custom)))
            findings = metadata_core.validate(current, custom)
            findings_view.setPlainText("\n".join(
                [metadata_core.validation_summary(findings)] + [
                    "[%s] %s: %s" % (item["severity"].upper(), item["field"],
                                     item["detail"]) for item in findings]))

        for widget in editors.values():
            if isinstance(widget, QtWidgets.QComboBox):
                widget.currentIndexChanged.connect(refresh_preview)
            elif isinstance(widget, QtWidgets.QPlainTextEdit):
                widget.textChanged.connect(refresh_preview)
            else:
                widget.textChanged.connect(refresh_preview)
        refresh_preview()

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        outer.addWidget(buttons)

        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            return None
        values = collect()
        text, _replaced = metadata_core.apply_header(self._editor_code_text(),
                                                     values, language, custom)
        self._set_editor_code_text(text, metadata_core.HEADER_START)
        findings = metadata_core.validate(values, custom)
        self.status.setText(self.t(
            "editor_meta_updated", "metadata updated in the buffer - press Save"))
        self._announce("editor metadata edited: %s"
                       % self._metadata_defaults()["name"])
        return {"values": values, "findings": findings}

    def insert_metadata_header(self):
        """Write a header carrying the current cursor symbol's name as a start.

        A file with a header already has its header replaced rather than a
        second one added, which is the same rule ``apply_header`` applies.
        """
        language = self._editor_language_key()
        _parsed, values, custom = self._current_metadata()
        text, replaced = metadata_core.apply_header(self._editor_code_text(),
                                                    values, language, custom)
        self._set_editor_code_text(text, metadata_core.HEADER_START)
        self.status.setText(self.t(
            "editor_meta_replaced" if replaced else "editor_meta_inserted",
            "metadata header replaced in the buffer - press Save" if replaced
            else "metadata header inserted - press Save"))
        return {"replaced": replaced}

    def stamp_metadata(self):
        """Set the modified field to now and refresh the version's date."""
        language = self._editor_language_key()
        _parsed, values, custom = self._current_metadata()
        values["modified"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        text, _replaced = metadata_core.apply_header(self._editor_code_text(),
                                                     values, language, custom)
        self._set_editor_code_text(text, "modified:")
        self.status.setText(self.t(
            "editor_meta_stamped", "modified stamped in the buffer - press Save"))
        return values["modified"]

    def bump_metadata_version(self):
        """Raise the header's version by one patch step and stamp modified."""
        language = self._editor_language_key()
        _parsed, values, custom = self._current_metadata()
        before = str(values.get("version") or "")
        parts = before.split(".")
        # A version that does not read as numbers is left alone rather than
        # guessed at: inventing one would hide the real problem.
        if parts and all(piece.isdigit() for piece in parts):
            parts[-1] = str(int(parts[-1]) + 1)
            values["version"] = ".".join(parts)
        else:
            values["version"] = before or "0.0.1"
        values["modified"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        text, _replaced = metadata_core.apply_header(self._editor_code_text(),
                                                     values, language, custom)
        self._set_editor_code_text(text, "version:")
        self.status.setText(self.t(
            "editor_meta_bumped", "version {version} in the buffer - press Save",
            version=values["version"]))
        return values["version"]

    def strip_metadata_header(self):
        """Remove the managed block from the buffer, leaving the code alone."""
        language = self._editor_language_key()
        text = self._editor_code_text()
        parsed = metadata_core.parse_header(text, language)
        if not parsed["found"]:
            self.status.setText(self.t("editor_meta_absent",
                                       "This script has no metadata header yet."))
            return False
        lines = text.replace("\r\n", "\n").split("\n")
        kept = lines[:parsed["start"]] + lines[parsed["end"] + 1:]
        # The blank line the block left behind is dropped too, so removing and
        # re-inserting a header leaves no growing gap in the file.
        while (parsed["start"] < len(kept) and parsed["start"] - 1 >= 0
               and kept[parsed["start"] - 1].strip() == ""
               and kept[parsed["start"]].strip() == ""):
            del kept[parsed["start"]]
        self._set_editor_code_text("\n".join(kept))
        self.status.setText(self.t(
            "editor_meta_stripped", "metadata header removed - press Save"))
        return True
    def metadata_contract(self):
        """The metadata surface as a plugin-safe structure."""
        return {
            key: {"name": self.t(label_key, fallback), "status": "available"}
            for key, label_key, fallback in EDITOR_METADATA_ACTIONS
        }

    def run_analysis(self, key):
        'Run one static Script Analyzer operation on the live editor buffer.'
        if key == "metrics":
            return self.show_code_metrics()
        analyzer = getattr(self.window, "analyzer_tab", None)
        if analyzer is None or not hasattr(analyzer, "_analyze_script"):
            self.status.setText(self.t("editor_analysis_unavailable", "Analyzer unavailable."))
            return None
        script = self._find_script(self.script_id) or {
            "id": self.script_id, "name": self.name_label.text(),
            "category": self.current_category(), "code": self.editor.toPlainText(),
            "history": [],
        }
        live = dict(script)
        live["code"] = self.editor.toPlainText()
        try:
            result = analyzer._analyze_script(live)
        except Exception as exc:
            self.status.setText(self.t("editor_analysis_failed", "Analysis failed: {error}").format(error=exc))
            return None
        self._show_analysis_result(key, result)
        return result
    def show_code_metrics(self):
        'Display the eight requested metrics for the live editor buffer.'
        code = self.editor.toPlainText()
        analyzer = getattr(self.window, "analyzer_tab", None)
        result = None
        if analyzer is not None and hasattr(analyzer, "_analyze_script"):
            try:
                result = analyzer._analyze_script(self._live_entry())
            except Exception:
                result = None
        tree, error = _python_ast(code)
        lines = code.splitlines()
        functions = sum(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) for node in ast.walk(tree)) if tree else len(re.findall(r"^\\s*(?:async\\s+)?def\\s+\\w+", code, re.MULTILINE))
        classes = sum(isinstance(node, ast.ClassDef) for node in ast.walk(tree)) if tree else len(re.findall(r"^\\s*class\\s+\\w+", code, re.MULTILINE))
        imports = sum(isinstance(node, (ast.Import, ast.ImportFrom)) for node in ast.walk(tree)) if tree else len(re.findall(r"^\\s*(?:from|import)\\s+", code, re.MULTILINE))
        complexity_rows = (result or {}).get("complexity", [])
        cyclomatic = sum(row.get("complexity", 1) for row in complexity_rows) or (1 if code.strip() else 0)
        logical = sum(1 for _ in re.finditer(r"\\b(?:and|or|not|if|elif|else|for|while|try|except|match|case)\\b", code))
        issues = (result or {}).get("issues", [])
        errors = sum(1 for issue in issues if issue.get("severity") == "high")
        warnings = sum(1 for issue in issues if issue.get("severity") in {"medium", "low"})
        if not result:
            errors = 1 if error else 0
            warnings = sum(1 for line in lines if len(line) > 100 or line.rstrip() != line)
        values = {
            "lines": len(lines), "functions": functions, "classes": classes,
            "imports": imports, "cyclomatic": cyclomatic, "logical": logical,
            "errors": errors, "warnings": warnings,
        }
        labels = {
            "lines": self.t("editor_metric_lines", "Lines"),
            "functions": self.t("editor_metric_functions", "Functions"),
            "classes": self.t("editor_metric_classes", "Classes"),
            "imports": self.t("editor_metric_imports", "Imports"),
            "cyclomatic": self.t("editor_metric_cyclomatic", "Cyclomatic complexity"),
            "logical": self.t("editor_metric_logical", "Logical complexity"),
            "errors": self.t("editor_metric_errors", "Errors"),
            "warnings": self.t("editor_metric_warnings", "Warnings"),
        }
        self._show_text_dialog(self.t("editor_metrics_menu", "Code metrics"),
                               "\\n".join("%s: %s" % (labels[key], value) for key, value in values.items()))
        return values
    def _show_analysis_result(self, key, result):
        'Render one analyzer dimension in a compact editor-owned dialog.'
        labels = dict((item[0], self.t(item[1], item[2])) for item in EDITOR_ANALYSIS_ACTIONS)
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle(labels.get(key, key))
        dialog.resize(820, 520)
        layout = QtWidgets.QVBoxLayout(dialog)
        header = QtWidgets.QLabel("%s  |  %s" % (result.get("name", ""), labels.get(key, key)))
        header.setStyleSheet("font-weight:bold; color:#8ab4f8;")
        layout.addWidget(header)
        tree = QtWidgets.QTreeWidget()
        tree.setColumnCount(3)
        tree.setHeaderLabels([self.t("editor_analysis_item", "Item"),
                              self.t("editor_analysis_value", "Value"),
                              self.t("editor_analysis_detail", "Detail")])
        layout.addWidget(tree, 1)
        self._populate_analysis_tree(tree, key, result)
        close = QtWidgets.QPushButton(self.t("editor_analysis_close", "Close"))
        close.clicked.connect(dialog.close)
        layout.addWidget(close)
        self.status.setText(labels.get(key, key))
        dialog.exec_()

    def _populate_analysis_tree(self, tree, key, result):
        sections = {
            "syntax": ("syntax",), "ast": ("ast",),
            "semantic": ("issues",), "security": ("runtime",),
            "quality": ("metrics", "style", "issues"),
            "runtime": ("runtime", "runtime_summary"),
            "dependencies": ("dependencies", "dependency_summary"),
            "changes": ("history", "history_summary"),
            "errors": ("issues", "syntax"),
        }
        keys = sections.get(key, (key,))
        for section in keys:
            value = result.get(section)
            root = QtWidgets.QTreeWidgetItem([section, "", ""])
            root.setForeground(0, QtGui.QColor("#8ab4f8"))
            tree.addTopLevelItem(root)
            self._add_analysis_value(root, value)
        tree.expandAll()

    def _add_analysis_value(self, parent, value, name=""):
        if isinstance(value, dict):
            for key, item in value.items():
                self._add_analysis_value(parent, item, str(key))
            return
        if isinstance(value, (list, tuple, set)):
            if not value:
                parent.addChild(QtWidgets.QTreeWidgetItem([name, "0", "none"]))
            for index, item in enumerate(value):
                self._add_analysis_value(parent, item, name or str(index))
            return
        detail = "" if value is None else str(value)
        parent.addChild(QtWidgets.QTreeWidgetItem([name, detail, ""]))

    def _replace_document(self, text, message):
        if text == self.editor.toPlainText():
            self.status.setText(self.t("editor_no_changes", "no changes needed"))
            return False
        self.editor.setPlainText(text)
        self.editor.rehighlight()
        self.status.setText(message)
        return True
    def _python_only(self):
        if language_key(self.current_category()) != "python":
            self.status.setText(self.t("editor_python_only", "This tool supports Python only."))
            return False
        return True
    def _ask_text(self, title, label, value=""):
        text, accepted = QtWidgets.QInputDialog.getText(self, title, label,
                                                         QtWidgets.QLineEdit.Normal, value)
        return text.strip() if accepted else None
    def _rename_symbol(self):
        if not self._python_only():
            return
        cursor = self.editor.textCursor()
        old = cursor.selectedText().strip() or self.symbol_under_cursor()
        if not old or not old.isidentifier():
            self.status.setText(self.t("editor_need_symbol", "Select a symbol first."))
            return
        new = self._ask_text(self.t("editor_refactor_rename", "Rename symbol"),
                             self.t("editor_new_name", "New name:"), old)
        if not new or not new.isidentifier() or keyword.iskeyword(new):
            return
        pattern = re.compile(r"\b" + re.escape(old) + r"\b")
        self._replace_document(pattern.sub(new, self.editor.toPlainText()),
                                self.t("editor_refactored", "refactoring applied"))

    def _selected_lines(self):
        cursor = self.editor.textCursor()
        start = cursor.selectionStart() if cursor.hasSelection() else cursor.block().position()
        end = cursor.selectionEnd() if cursor.hasSelection() else cursor.block().position() + cursor.block().length() - 1
        first = self.editor.document().findBlock(start).blockNumber()
        last = self.editor.document().findBlock(max(start, end - 1)).blockNumber()
        return first, last
    def _extract_function(self):
        if not self._python_only():
            return
        first, last = self._selected_lines()
        lines = self.editor.toPlainText().splitlines()
        if first >= len(lines) or last >= len(lines):
            return
        name = self._ask_text(self.t("editor_refactor_extract_function", "Extract function"),
                              self.t("editor_function_name", "Function name:"), "extracted")
        if not name or not name.isidentifier():
            return
        selected = lines[first:last + 1]
        indent = re.match(r"[ \\t]*", selected[0]).group(0)
        body = "\n".join((line[len(indent):] if line.startswith(indent) else line).rstrip() for line in selected)
        function = [f"def {name}():"] + [("    " + line if line else "") for line in body.splitlines()]
        replacement = indent + name + "()"
        new_lines = lines[:first] + [replacement] + lines[last + 1:] + ["", "\n".join(function)]
        self._replace_document("\n".join(new_lines) + "\n", self.t("editor_refactored", "refactoring applied"))

    def _extract_variable(self):
        if not self._python_only():
            return
        cursor = self.editor.textCursor()
        expression = cursor.selectedText().strip()
        if not expression:
            self.status.setText(self.t("editor_select_expression", "Select an expression first."))
            return
        name = self._ask_text(self.t("editor_refactor_extract_variable", "Extract variable"),
                              self.t("editor_variable_name", "Variable name:"), "value")
        if not name or not name.isidentifier():
            return
        line_start = cursor.block().position()
        indent = re.match(r"[ \\t]*", cursor.block().text()).group(0)
        text = self.editor.toPlainText()
        selected_start = cursor.selectionStart()
        selected_end = cursor.selectionEnd()
        replacement = indent + name + " = " + expression + "\n"
        text = text[:line_start] + replacement + text[line_start:]
        shift = len(replacement)
        selected_start += shift
        selected_end += shift
        text = text[:selected_start] + name + text[selected_end:]
        self._replace_document(text, self.t("editor_refactored", "refactoring applied"))

    def _move_definition(self, kind):
        if not self._python_only():
            return
        tree, error = _python_ast(self.editor.toPlainText())
        if tree is None:
            self.status.setText(str(error))
            return
        line = self._cursor_line()
        definitions = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                       and node.lineno - 1 <= line <= getattr(node, "end_lineno", node.lineno) - 1
                       and ((kind == "function" and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)))
                            or (kind == "class" and isinstance(node, ast.ClassDef)))]
        if not definitions:
            self.status.setText(self.t("editor_need_definition", "Place the cursor inside a definition."))
            return
        node = definitions[0]
        lines = self.editor.toPlainText().splitlines()
        start, end = _node_lines(node)
        block = lines[start:end]
        remaining = lines[:start] + lines[end:]
        target = self._ask_text(self.t("editor_move_definition", "Move definition"),
                                self.t("editor_move_target", "Target line (1 = top):"), "1")
        try:
            index = max(0, min(len(remaining), int(target) - 1))
        except (TypeError, ValueError):
            return
        new_lines = remaining[:index] + block + [""] + remaining[index:]
        self._replace_document("\n".join(new_lines) + "\n", self.t("editor_refactored", "refactoring applied"))

    def _format_code(self):
        if self._python_only():
            self._replace_document(format_python_code(self.editor.toPlainText()), self.t("editor_formatted", "code formatted"))

    def _remove_unused_imports(self):
        if not self._python_only():
            return
        text, count, error = remove_unused_python_imports(self.editor.toPlainText())
        if error:
            self.status.setText(str(error))
            return
        self._replace_document(text, self.t("editor_unused_removed", "removed {count} unused import(s)").format(count=count))

    def _remove_dead_code(self):
        if not self._python_only():
            return
        text, count, error = remove_dead_python_code(self.editor.toPlainText())
        if error:
            self.status.setText(str(error))
            return
        self._replace_document(text, self.t("editor_dead_removed", "removed {count} dead line(s)").format(count=count))

    def _fix_linter(self):
        if self._python_only():
            text, count = fix_python_linter(self.editor.toPlainText())
            self._replace_document(text, self.t("editor_linter_fixed", "linter fixes applied ({count} import(s) removed)").format(count=count))

    def _cursor_line(self):
        return self.editor.textCursor().blockNumber()

    def _delete_line(self):
        cursor = self.editor.textCursor()
        cursor.select(QtGui.QTextCursor.LineUnderCursor)
        cursor.removeSelectedText()
        cursor.deleteChar()
        self.editor.setTextCursor(cursor)
        self.status.setText(self.t("editor_delete_line", "Delete line"))

    def _move_line(self, direction):
        """Swap the current line with the one above/below it."""
        cursor = self.editor.textCursor()
        line_no = cursor.blockNumber()
        lines = self.editor.toPlainText().split("\n")
        target = line_no + direction
        if target < 0:
            self.status.setText(self.t("editor_at_top", "already at the top"))
            return
        if target >= len(lines):
            self.status.setText(self.t("editor_at_bottom",
                                       "already at the bottom"))
            return
        lines[line_no], lines[target] = lines[target], lines[line_no]
        self._set_text_keep_line(lines, target)
        self.status.setText(self.t("editor_line_moved", "line moved"))

    def _sort_lines(self):
        lines = self.editor.toPlainText().split("\n")
        if len(lines) < 2:
            self.status.setText(self.t("editor_nothing_to_sort",
                                       "nothing to sort"))
            return
        ordered = sorted(lines, key=lambda s: s.lower())
        self._set_text_keep_line(ordered, 0)
        self.status.setText(self.t("editor_sorted", "lines sorted"))

    def _trim_spaces(self):
        lines = self.editor.toPlainText().split("\n")
        trimmed = 0
        result = []
        for line in lines:
            stripped = line.rstrip()
            if stripped != line:
                trimmed += 1
            result.append(stripped)
        self._set_text_keep_line(result, self._cursor_line())
        if trimmed:
            self.status.setText(self.t(
                "editor_trimmed", "trimmed {count} line(s)").format(
                count=trimmed))
        else:
            self.status.setText(self.t("editor_no_trailing",
                                       "no trailing spaces"))

    def _tabs_to_spaces(self):
        text = self.editor.toPlainText().replace("\t", "    ")
        self.editor.setPlainText(text)
        self.status.setText(self.t("editor_tabs_done",
                                   "tabs converted to spaces"))

    def _set_text_keep_line(self, lines, line_no):
        """Replace the document text and restore the cursor to `line_no`."""
        cursor = self.editor.textCursor()
        column = cursor.positionInBlock()
        self.editor.setPlainText("\n".join(lines))
        block = self.editor.document().findBlockByNumber(max(0, line_no))
        if block.isValid():
            new_cursor = QtGui.QTextCursor(block)
            new_cursor.movePosition(
                QtGui.QTextCursor.Right, QtGui.QTextCursor.MoveAnchor,
                min(column, block.length() - 1))
            self.editor.setTextCursor(new_cursor)

    # ------------------------------------------------------- find & replace
    def _pattern(self):
        needle = self.find_edit.text()
        if not needle:
            return None
        flags = 0 if self.chk_case.isChecked() else re.IGNORECASE
        if self.chk_regex.isChecked():
            try:
                return re.compile(needle, flags)
            except re.error:
                self.status.setText(self.t("search_bad_regex",
                                           "Invalid regular expression"))
                return None
        return re.compile(re.escape(needle), flags)

    def find_next(self):
        """Move the cursor to the next match of the find box."""
        pattern = self._pattern()
        if pattern is None:
            return
        text = self.editor.toPlainText()
        start = self.editor.textCursor().selectionEnd()
        match = pattern.search(text, start)
        if match is None:
            match = pattern.search(text, 0)   # wrap around
        if match is None:
            self.status.setText(self.t("editor_not_found", "not found"))
            return
        self._select_range(match.start(), match.end())
        self.status.setText(self.t("editor_found_at", "match at {pos}").format(
            pos=match.start()))

    def replace_one(self):
        """Replace the current selection (or the next match)."""
        pattern = self._pattern()
        if pattern is None:
            return
        cursor = self.editor.textCursor()
        if not cursor.hasSelection():
            self.find_next()
            cursor = self.editor.textCursor()
            if not cursor.hasSelection():
                return
        replacement = self.replace_edit.text()
        cursor.insertText(replacement)
        self.status.setText(self.t("editor_replaced", "replaced 1 occurrence"))

    def replace_all(self):
        """Replace every match in the document."""
        pattern = self._pattern()
        if pattern is None:
            return
        replacement = self.replace_edit.text()
        text = self.editor.toPlainText()
        new_text, count = pattern.subn(replacement, text)
        if count:
            self.editor.setPlainText(new_text)
        self.status.setText(self.t(
            "editor_replaced_all", "replaced {count} occurrence(s)").format(
            count=count))

    def _select_range(self, start, end):
        cursor = self.editor.textCursor()
        cursor.setPosition(start)
        cursor.setPosition(end, QtGui.QTextCursor.KeepAnchor)
        self.editor.setTextCursor(cursor)
        self.editor.ensureCursorVisible()

    # ------------------------------------------------------------ saving
    def save(self):
        """Write the buffer back to the script list (with a history entry)."""
        script = self._find_script(self.script_id)
        if script is None:
            self.status.setText(
                self.t("editor_noselect", "Select a script on the Scripts tab."))
            return
        code = self.editor.toPlainText()
        if code == (script.get("code", "") or ""):
            self.status.setText(self.t("editor_unchanged", "no changes to save"))
            return
        # Keep a revision of the previous state so History/Diff keep working.
        history = list(script.get("history", []))
        previous = self._snapshot(script)
        history.append(previous)
        script["history"] = history[-25:]
        script["code"] = code
        script["date"] = _now()
        try:
            save_data = getattr(self.window, "save_data_now", None)
            if callable(save_data):
                save_data()
        except Exception:
            pass
        self.editor.document().setModified(False)
        self.dirty_label.setText("")
        # Announce on the shared audit bus and refresh the dependent tabs.
        self._announce("editor saved: %s" % script.get("name", ""))
        for name in ("refresh_list",):
            method = getattr(self.window, name, None)
            if callable(method):
                try:
                    method()
                except Exception:
                    pass
        analyzer = getattr(self.window, "analyzer_tab", None)
        if analyzer is not None and hasattr(analyzer, "refresh"):
            try:
                analyzer.refresh()
            except Exception:
                pass
        self.status.setText(self.t("editor_saved", "saved {name}").format(
            name=script.get("name", "")))

    @staticmethod
    def _snapshot(script):
        """A history-safe copy of the current script state."""
        return {
            "id": script.get("id"),
            "name": script.get("name", ""),
            "category": script.get("category", ""),
            "tags": script.get("tags", ""),
            "desc": script.get("desc", ""),
            "code": script.get("code", ""),
            "date": script.get("date", ""),
            "meta": script.get("meta", {}),
        }

    def _announce(self, detail):
        """Record an editor action on the shared audit timeline."""
        window = self.window
        audit = getattr(window, "audit_tab", None)
        if audit is not None and hasattr(audit, "log_change"):
            try:
                audit.log_change("modules", "editor", detail, "editor")
                return
            except Exception:
                pass
        notify = getattr(window, "notify_audit", None)
        if callable(notify):
            try:
                notify("module_updated", where="editor", detail=detail)
            except Exception:
                pass

    # ------------------------------------------------------------ export
    def export_file(self):
        """Export the current buffer as a plain source file."""
        ext = ".txt"
        try:
            from sdb import extension_for_category
            script = self._find_script(self.script_id)
            if script is not None:
                ext = extension_for_category(script.get("category", ""))
        except Exception:
            pass
        suggested = (self.name_label.text().split("  [")[0].strip() or "script")
        suggested = re.sub(r'[\\/:*?"<>|]+', "_", suggested) + ext
        directory = self._data_dir()
        path = os.path.join(directory, suggested)
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(self.editor.toPlainText())
        except OSError as exc:
            self.status.setText(str(exc))
            return ""
        self.status.setText(self.t("editor_exported", "exported: {path}").format(
            path=path))
        return path

    def _data_dir(self):
        base = getattr(self.window, "base_dir", None) or getattr(
            self.window, "BASE_DIR", None)
        if base:
            return base
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # ------------------------------------------------------- translation
    def retranslate(self, table):
        """Apply the language table of the main window to this tab."""
        def label(key, fallback):
            return table.get(key, fallback)

        self.title_label.setText(label("editor", "Editor"))
        self.btn_load.setText(label("editor_load", "Load selected"))
        self.btn_save.setText(label("save", "Save"))
        self.btn_export.setText(label("editor_export", "Export file"))
        self.find_edit.setPlaceholderText(
            label("editor_find_hint", "Find..."))
        self.replace_edit.setPlaceholderText(
            label("editor_replace_hint", "Replace with..."))
        self.btn_find.setText(label("editor_find", "Find"))
        self.btn_replace.setText(label("editor_replace_menu", "Replace..."))
        self.act_replace_one.setText(label("editor_replace", "Replace"))
        self.act_replace_all.setText(label("editor_replace_all", "Replace all"))
        self.chk_regex.setText(label("editor_regex", "Regex"))
        self.chk_case.setText(label("editor_case", "Case"))
        self.btn_undo.setText(label("editor_undo", "Undo"))
        self.btn_redo.setText(label("editor_redo", "Redo"))
        self.theme_label.setText(label("editor_theme", "Theme:"))
        self.theme_combo.setItemText(0, label("editor_theme_dark", "Dark"))
        self.theme_combo.setItemText(1, label("editor_theme_light", "Light"))
        self.chk_low_graphics.setText(
            label("editor_low_graphics", "Low graphics"))
        for key, label_key, fallback, _default in EDITOR_VIEWS:
            if key in self.view_checks:
                self.view_checks[key].setText(
                    label(label_key, fallback))
        self.btn_tools.setText(label("editor_tools_menu", "Code tools..."))
        for key, label_key, fallback in EDITOR_TOOLS:
            if key in self.tool_actions:
                self.tool_actions[key].setText(
                    label(label_key, fallback))
        self.btn_move.setText(label("editor_move_menu", "Move..."))
        self.act_move_up.setText(label("editor_move_up", "Move up"))
        self.act_move_down.setText(label("editor_move_down", "Move down"))
        self.btn_refactor.setText(label("editor_refactor_menu", "Refactor..."))
        for key, label_key, fallback in EDITOR_REFACTOR_ACTIONS:
            if key in self.refactor_actions:
                self.refactor_actions[key].setText(table.get(label_key, fallback))
        self.btn_cleanup.setText(label("editor_cleanup_menu", "Code cleanup..."))
        for key, label_key, fallback in EDITOR_CLEANUP_ACTIONS:
            if key in self.cleanup_actions:
                self.cleanup_actions[key].setText(table.get(label_key, fallback))
        self.btn_analysis.setText(label("editor_analysis_menu", "Code analysis..."))
        for key, label_key, fallback in EDITOR_ANALYSIS_ACTIONS:
            if key in self.analysis_actions:
                self.analysis_actions[key].setText(table.get(label_key, fallback))
        self.btn_integrations.setText(label("editor_integration_menu", "Integrations..."))
        for key, label_key, fallback in EDITOR_INTEGRATION_ACTIONS:
            if key in self.integration_actions:
                self.integration_actions[key].setText(table.get(label_key, fallback))
        self.btn_workspace.setText(label("editor_workspace_menu", "Workspace & security..."))
        for key, label_key, fallback in EDITOR_WORKSPACE_ACTIONS:
            if key in self.workspace_actions:
                self.workspace_actions[key].setText(table.get(label_key, fallback))
        if hasattr(self, "btn_templates"):
            self.btn_templates.setText(label("editor_templates_menu",
                                             "Templates..."))
            self._rebuild_templates_menu()
        self.btn_codegen.setText(label("editor_codegen_menu",
                                       "Code generation..."))
        for key, label_key, fallback in EDITOR_CODEGEN_ACTIONS:
            if key in self.codegen_actions:
                self.codegen_actions[key].setText(table.get(label_key, fallback))
        self.btn_coderules.setText(label("editor_coderules_menu",
                                         "Code rules..."))
        for key, label_key, fallback in EDITOR_CODERULES_ACTIONS:
            if key in self.coderules_actions:
                self.coderules_actions[key].setText(table.get(label_key, fallback))
        self.act_rules_all.setText(label("coderules_fix_all",
                                         "Run every rule"))
        self.act_rules_lint.setText(label("coderules_lint",
                                          "Only report (change nothing)"))
        self.act_rules_report.setText(label("coderules_report",
                                            "Rules report"))
        self.btn_profiles.setText(label("editor_profiles_menu",
                                        "Script profiles..."))
        for key, label_key, fallback in EDITOR_PROFILE_ACTIONS:
            if key in self.profile_actions:
                self.profile_actions[key].setText(table.get(label_key, fallback))
        self.language_label.setText(label("editor_language", "Language:"))
        # The navigation row: every jump action carries its own label.
        self.nav_label.setText(label("nav_go", "Navigate:"))
        self.btn_goto.setText(label("nav_go_menu", "Go to..."))
        for key, label_key, fallback in nav_engine.NAV_KINDS:
            if key in self.nav_actions:
                self.nav_actions[key].setText(label(label_key, fallback))
        self.act_nav_back.setText(label("nav_go_previous", "Go to previous"))
        self.nav_edit.setPlaceholderText(
            label("nav_symbol_hint", "Symbol to jump to..."))
        self.btn_nav_run.setText(label("nav_symbol_jump", "Jump"))
        self.btn_problem.setText(label("nav_problem", "Problem"))
        self.act_problem_next.setText(label("nav_problem_next", "Next problem"))
        self.act_problem_prev.setText(
            label("nav_problem_prev", "Previous problem"))
        if self.script_id is None:
            self.name_label.setText(label("editor_no_script", "No script loaded"))
