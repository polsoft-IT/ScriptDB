# File: sdb_scripts.py
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ScriptDB - Scripts tab (standalone application)

The "Scripts" tab extracted from sdb.py as a self-contained program. The other
tabs (Search, Editor, Modules, Database, Metadata, Compare) are not included
here yet and will be added later.
"""

import argparse
import collections
import contextlib
import datetime as dt
import difflib
import json
import os

import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import traceback

from typing import Optional

try:
    from PyQt5 import QtCore, QtGui, QtWidgets
    from PyQt5.QtGui import QFont
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        """PyQt5 is required. Install it with: pip install PyQt5\n"""
        "Original import error: " + str(exc)
    )


BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Make the per-tab package importable both when run from source and when the
# whole project is frozen, since the tabs folder ships next to this file.
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

try:
    from tabs import theme as app_theme
    from tabs import (AuditTab, DebuggerTab, SandboxTab, DocsTab, EditorTab,
                      ReportsTab, ScriptAnalyzerTab, SearchTab, VaultTab,
                      SecurityTab, StructureTab, SqliteTab, MetadataTab, AppsTab,
                      TodoTab, TodoWindow)
    from tabs.editor import CodeEditor
except ImportError:  # pragma: no cover - tab package missing
    app_theme = None
    AuditTab = None
    DebuggerTab = None
    SandboxTab = None
    DocsTab = None
    ReportsTab = None
    ScriptAnalyzerTab = None
    EditorTab = None
    SearchTab = None
    VaultTab = None
    SecurityTab = None
    StructureTab = None
    SqliteTab = None
    MetadataTab = None
    AppsTab = None
    TodoTab = None
    TodoWindow = None
    CodeEditor = None
DATA_FILE = os.path.join(BASE_DIR, "scripts_db.json")
CONN_FILE = os.path.join(BASE_DIR, "connections_db.json")
SETTINGS_FILE = os.path.join(BASE_DIR, "settings_db.json")

# Marker stored in a script entry that represents a visual separator row.
SEPARATOR_KIND = "separator"



def _resource_base_dir():
    # Directory holding bundled resources. When frozen into a one-file exe,
    # PyInstaller unpacks everything into a temporary _MEIPASS folder.
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return sys._MEIPASS
    return BASE_DIR


def _icon_candidates(base):
    # Candidate icon paths within a single base directory.
    return [
        os.path.join(base, "sdb.ico"),
        os.path.join(base, ".dev", "sdb.ico"),
        os.path.join(base, "builder", "sdb.ico"),
    ]


def app_icon_path():
    # Locate sdb.ico both when run from source and from the frozen executable.
    # It normally lives in the '.dev' folder next to sdb.py (also accepted:
    # the old 'builder' location).
    for base in (_resource_base_dir(), BASE_DIR):
        for candidate in _icon_candidates(base):
            if os.path.isfile(candidate):
                return candidate
    return ""


def load_app_icon():
    # Build a QIcon from sdb.ico, or return an empty QIcon when not found.
    path = app_icon_path()
    if path:
        return QtGui.QIcon(path)
    return QtGui.QIcon()


def app_icon_pixmap(size=64):
    # Render the application icon into a pixmap, or None when there is no icon.
    icon = load_app_icon()
    if icon.isNull():
        return None
    return icon.pixmap(QtCore.QSize(size, size))



def load_about_icon(size=32):
    # Icon for the About button: the application icon when available, otherwise
    # an info glyph drawn at run time so the button is never blank.
    pixmap = app_icon_pixmap(size)
    if pixmap is not None and not pixmap.isNull():
        return QtGui.QIcon(pixmap)
    return QtGui.QIcon(draw_info_glyph(size))


def draw_info_glyph(size=32):
    # Paint a simple "i" badge so About has an icon even without sdb.ico.
    pixmap = QtGui.QPixmap(size, size)
    pixmap.fill(QtCore.Qt.transparent)
    painter = QtGui.QPainter(pixmap)
    try:
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
        painter.setPen(QtCore.Qt.NoPen)
        painter.setBrush(QtGui.QColor("#8ab4f8"))
        painter.drawEllipse(1, 1, size - 2, size - 2)
        painter.setPen(QtGui.QColor("#1e1e1e"))
        font = QFont("Segoe UI", int(size * 0.65))
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(pixmap.rect(), QtCore.Qt.AlignCenter, "i")
    finally:
        painter.end()
    return pixmap



TRANSLATIONS = {
    "en": {
        "title": "ScriptDB",
        "scripts": "Scripts",
        "editor": "Editor",
        "editor_load": "Load selected",
        "editor_export": "Export file",
        "editor_no_script": "No script loaded",
        "editor_find_hint": "Find...",
        "editor_replace_hint": "Replace with...",
        "editor_find": "Find",
        "editor_replace_menu": "Replace...",
        "editor_replace": "Replace",
        "editor_replace_all": "Replace all",
        "editor_regex": "Regex",
        "editor_case": "Case",
        "editor_undo": "Undo",
        "editor_redo": "Redo",
        "editor_theme": "Theme:",
        "editor_theme_dark": "Dark",
        "editor_theme_light": "Light",
        "editor_theme_set": "theme: {name}",
        "editor_low_graphics": "Low graphics",
        "editor_low_graphics_on": "low graphics: on",
        "editor_low_graphics_off": "low graphics: off",
        "editor_view_linenumbers": "Line numbers",
        "editor_view_currentline": "Current line",
        "editor_view_brackets": "Bracket pairs",
        "editor_view_minimap": "Minimap",
        "editor_view_autoclose": "Auto-close",
        "editor_view_smartindent": "Smart indent",
        "editor_view_suggestions": "Suggestions",
        "editor_language": "Language:",
        "editor_language_set": "language: {name}",
        "editor_language_source": "({module} module)",
        'nav_go': 'Navigate:',
        'nav_go_menu': 'Go to...',
        'nav_go_previous': 'Go to previous',
        'nav_go_definition': 'Go to definition',
        'nav_go_reference': 'Go to reference',
        'nav_go_import': 'Go to import',
        'nav_go_class': 'Go to class',
        'nav_go_function': 'Go to function',
        'nav_go_variable': 'Go to variable',
        'nav_go_error': 'Go to error',
        'nav_go_warning': 'Go to warning',
        'nav_go_section': 'Go to section',
        'nav_go_enclosing': 'Enclosing section',
        'nav_symbol_hint': 'Symbol to jump to...',
        'nav_symbol_jump': 'Jump',
        'nav_problem': 'Problem',
        'nav_problem_next': 'Next problem',
        'nav_problem_prev': 'Previous problem',
        'nav_need_symbol': 'type a symbol name first',
        'nav_not_found': 'nothing to go to for: {kind}',
        'nav_no_problem': 'no problems found',
        'nav_status': '{kind}: {name} (line {line})',
        "editor_language_loaded": "Loaded {name} [{language}]",
        "editor_module_active": "module: {key}",
        "search_kind": "Search:",
        "search_run": "Search",
        "search_hint": "Search in code...",
        "search_optional": "(optional filter)",
        "search_database": "Whole database",
        "search_database_hint": "Run this search over every script",
        "search_options": "Options",
        "search_case": "Case sensitive",
        "search_mark_definitions": "Mark definitions",
        "search_include_methods": "Method calls",
        "search_clear": "Clear results",
        "search_cleared": "search results cleared",
        "search_col_doc": "Script",
        "search_col_kind": "Kind",
        "search_col_line": "Line",
        "search_col_text": "Match",
        "search_col_detail": "Detail",
        "search_kind_text": "Text",
        "search_kind_string": "String",
        "search_kind_regex": "Regex",
        "search_kind_multiline": "Multiline",
        "search_kind_multifile": "Many files",
        "search_kind_files": "Directory",
        "search_kind_workspace": "Workspace",
        "search_kind_semantic": "Semantic",
        "search_kind_imports": "Imports",
        "search_kind_blocks": "Code blocks",
        "search_kind_comments": "Comments",
        "search_kind_ast": "AST",
        "search_kind_calls": "Calls",
        "search_kind_definitions": "Definitions",
        "search_kind_references": "References",
        "search_kind_todo": "TODO / FIXME",
        "docs": "DOCs",
        "docs_new_db": "New database",
        "docs_open_db": "Open...",
        "docs_save": "Save",
        "docs_save_db": "Save database as...",
        "reports": "Reports",
        "reports_scan": "Rescan",
        "reports_search": "Search:",
        "reports_search_name": "By name...",
        "reports_search_content": "In content...",
        "reports_all_tags": "All tags",
        "reports_problems_any": "All",
        "reports_problems_errors": "Errors",
        "reports_problems_warnings": "Warnings",
        "reports_problems_both": "Errors + warnings",
        "reports_search_history": "Search history by day",
        "reports_since": "Since (YYYY-MM-DD)",
        "reports_clear_search": "Clear search",
        "reports_history_day": "Generated {day}",
        "reports_add_dir": "Add folder...",
        "reports_open_file": "Open file",
        "reports_to_docs": "Store in DOCs",
        "reports_export": "Export index",
        "reports_delete": "Delete report",
        "reports_file_menu": "File...",
        "reports_delete_ask": "Delete {name}?",
        "reports_deleted": "report deleted",
        "reports_group": "Group:",
        "reports_quick": "Quick:",
        "reports_filter_all": "All",
        "reports_filter_24h": "Last 24h",
        "reports_filter_week": "Last week",
        "reports_filter_errors": "Errors",
        "reports_filter_tests": "Tests",
        "reports_filter_diff": "Diff",
        "reports_filter_analysis": "Code analysis",
        "reports_filter_text": "Filter reports...",
        "reports_all_types": "All types",
        "reports_group_project": "By project",
        "reports_group_module": "By module",
        "reports_group_type": "By report type",
        "reports_group_date": "By date",
        "reports_group_tags": "By tag",
        "reports_group_none": "Flat list",
        "reports_col_name": "Report",
        "reports_col_type": "Type",
        "reports_col_project": "Project",
        "reports_col_size": "Size",
        "reports_col_modified": "Modified",
        "reports_count": "{count} report(s)",
        "reports_scanned": "scanned {count} report(s)",
        "reports_meta": "{type} | project: {project} | tags: {tags} | {size} | {modified}",
        "reports_no_docs": "DOCs repository unavailable",
        "reports_stored_docs": "stored {name} in DOCs",
        "reports_exported": "index written: {path}",
        "reports_preview": "Preview:",
        "reports_preview_menu": "Preview...",
        "reports_preview_run": "Refresh preview",
        "reports_preview_ascii_only": "ASCII only",
        "reports_preview_export": "Export preview",
        "reports_preview_exported": "preview written: {path}",
        "reports_preview_nothing_export": "nothing to export",
        "reports_preview_ascii": "ASCII / plain text",
        "reports_preview_structure": "Structure (sections / blocks)",
        "reports_preview_metadata": "Metadata",
        "reports_preview_minimap": "Mini-map",
        "reports_analysis_menu": "Analysis...",
        "reports_an_errors": "Error detection",
        "reports_an_warnings": "Warning detection",
        "reports_an_duplicates": "Duplicate detection",
        "reports_an_changes": "Change detection",
        "reports_an_diff": "Report comparison (diff)",
        "reports_an_trends": "Trend analysis",
        "reports_an_metrics": "Metric analysis",
        "reports_open_in_menu": "Open in...",
        "reports_int_diff": "Open in Diff",
        "reports_int_search": "Open in Search",
        "reports_int_debugger": "Open in Debugger",
        "reports_int_runner": "Open in Runner",
        "reports_int_linter": "Open in Linter",
        "reports_int_project": "Link to project module",
        "reports_generate": "Generate from module",
        "reports_generate_label": "Module:",
        "reports_op_open": "Open",
        "reports_op_delete": "Delete",
        "reports_op_important": "Mark as important",
        "reports_operations_menu": "Report actions...",
        "reports_op_pin_project": "Pin to project",
        "reports_op_pin_favourite": "Pin to favourites",
        "reports_op_tag": "Add tags",
        "reports_op_compare": "Compare with another report",
        "reports_op_versions": "Show version differences",
        "reports_project_name": "Project:",
        "reports_tags_label": "Tags (comma separated):",
        "reports_pick": "Choose a report",
        "reports_pick_label": "Report:",
        "reports_pick_other": "Compare with which report?",
        "reports_need_second": "choose a second report",
        "reports_premium_menu": "Premium...",
        "reports_prem_run_due": "Run due reports",
        "reports_prem_automate": "Toggle automation",
        "reports_prem_due_done": "ran {count} scheduled report(s)",
        "reports_prem_auto": "Automatic report generation",
        "reports_prem_scheduler": "Cyclic reports (scheduler)",
        "reports_prem_comparative": "Comparative reports",
        "reports_prem_env": "Environment reports (env diff)",
        "reports_prem_security": "Security reports (permissions)",
        "reports_prem_change": "Automatic report after file change",
        "reports_prem_error": "Automatic report after an error",
        "reports_prem_deploy": "Automatic report after deploy",
        "reports_prem_build": "Automatic report after build",
        "reports_trig_build": "After a build",
        "reports_trig_run": "After a script runs",
        "reports_trig_error": "After an error",
        "reports_trig_deploy": "After a deploy",
        "reports_trig_change": "After a file change",
        "reports_cad_hourly": "Every hour",
        "reports_cad_daily": "Every day",
        "reports_cad_weekly": "Every week",
        "reports_meta_name": "Name",
        "reports_meta_type": "Type",
        "reports_meta_project": "Project",
        "reports_meta_module": "Module",
        "reports_meta_script": "Script",
        "reports_meta_process": "Process",
        "reports_meta_source": "Source",
        "reports_meta_path": "Path",
        "reports_meta_generated": "Generated",
        "reports_meta_size": "Size",
        "reports_meta_lines": "Lines",
        "reports_meta_words": "Words",
        "reports_meta_unique": "Unique words",
        "reports_meta_chars": "Characters",
        "reports_meta_errors": "Errors",
        "reports_meta_warnings": "Warnings",
        "reports_meta_tags": "Tags",
        "reports_meta_hash": "Hash",
        "docs_preview_typed": "By document type",
        "docs_group_text": "Text documents",
        "docs_group_config": "Configuration documents",
        "docs_group_log": "Log documents",
        "docs_group_report": "Report documents",
        "docs_group_audit": "Audit documents",
        "docs_group_project": "Project documents",
        "docs_group_snapshot": "Snapshot documents",
        "docs_group_script": "Script documents",
        "docs_group_system": "System documents",
        "docs_type_text": "Text",
        "docs_type_markdown": "Markdown",
        "docs_type_rst": "reStructuredText",
        "docs_type_json": "JSON",
        "docs_type_yaml": "YAML",
        "docs_type_ini": "INI / TOML",
        "docs_type_xml": "XML / HTML",
        "docs_type_log": "Log",
        "docs_type_syslog": "System log",
        "docs_type_report": "Report",
        "docs_type_table": "CSV / table",
        "docs_type_spreadsheet": "Spreadsheet",
        "docs_type_audit": "Audit trail",
        "docs_type_changelog": "Changelog",
        "docs_type_policy": "Policy / compliance",
        "docs_type_project": "Project note",
        "docs_type_readme": "README",
        "docs_type_plan": "Plan / roadmap",
        "docs_type_diagram": "Diagram (text)",
        "docs_type_snapshot": "Snapshot",
        "docs_type_backup": "Backup",
        "docs_type_script": "Script / source",
        "docs_type_sql": "SQL script",
        "docs_type_notebook": "Notebook",
        "docs_type_manifest": "Manifest",
        "docs_type_metadata": "Metadata",
        "docs_type_system": "System config",
        "docs_type_binary": "Binary / other",
        "docs_edit_page": "Edit",
        "docs_preview_page": "Preview",
        "docs_preview": "Preview:",
        "docs_preview_run": "Refresh preview",
        "docs_preview_ascii_only": "ASCII only",
        "docs_preview_export": "Export preview",
        "docs_preview_ascii": "ASCII / plain text",
        "docs_preview_structure": "Structure (outline)",
        "docs_preview_minimap": "Mini-map",
        "docs_preview_metadata": "Metadata",
        "docs_preview_links": "Links (relations)",
        "docs_preview_versions": "Changes vs snapshot",
        "docs_preview_created": "Created",
        "docs_preview_chars": "Characters",
        "docs_preview_lines": "Lines",
        "docs_preview_words": "Words",
        "docs_preview_unique": "Unique words",
        "docs_preview_checksum": "Checksum",
        "docs_preview_archived": "Archived",
        "docs_preview_encrypted": "Encrypted",
        "docs_preview_meta_keys": "Meta keys",
        "docs_preview_related": "Related documents:",
        "docs_preview_none": "(none)",
        "docs_preview_empty": "(nothing to preview)",
        "docs_preview_no_snapshot": "(choose a snapshot to compare against)",
        "docs_preview_identical": "no changes compared with the snapshot",
        "docs_preview_diff_summary": "+{added} / -{removed} line(s), {changed} changed block(s)",
        "docs_preview_nothing_export": "nothing to export",
        "docs_password": "Password...",
        "docs_export": "Export...",
        "docs_add": "Add document",
        "docs_import": "Import files...",
        "docs_save_doc": "Save document",
        "docs_archive": "Archive",
        "docs_delete": "Delete",
        "docs_edit_external": "Open in editor",
        "docs_search_tab": "Search code",
        "docs_index": "Rebuild index",
        "docs_update_local": "Update local docs",
        "docs_fulltext": "Full-text index",
        "docs_fulltext_hint": "Search the offline full-text index instead of the plain filter",
        "docs_snapshot": "Snapshot",
        "docs_snapshots": "Snapshots",
        "docs_restore": "Restore",
        "docs_snapshot_default": "snapshot",
        "docs_snapshot_taken": "snapshot stored ({count} docs)",
        "docs_snapshot_restored": "restored {count} document(s)",
        "docs_no_snapshot": "no snapshot chosen",
        "docs_reindexed": "indexed {count} document(s)",
        "docs_updated_stale": "updated {count} document(s)",
        "docs_no_updates": "everything up to date",
        "docs_engine_status": "index: {mode} - {count} document(s)",
        "docs_filter": "Filter documents...",
        "docs_all_categories": "All categories",
        "docs_show_archived": "Show archived",
        "docs_col_title": "Title",
        "docs_col_type": "Type",
        "docs_col_category": "Category",
        "docs_col_modified": "Modified",
        "docs_field_title": "Title",
        "docs_field_type": "Type",
        "docs_field_category": "Category",
        "docs_field_tags": "Tags",
        "docs_field_description": "Description",
        "docs_untitled": "untitled",
        "docs_locked": "\U0001f512 encrypted",
        "docs_locked_body": "(encrypted - enter password)",
        "docs_count": "{count} document(s)",
        "search_str_substring": "Substring",
        "search_str_word": "Whole word",
        "search_str_line": "Whole line",
        "search_str_prefix": "Starts with",
        "search_str_suffix": "Ends with",
        "search_sem_any": "Any symbol",
        "search_sem_function": "Functions",
        "search_sem_class": "Classes",
        "search_sem_method": "Methods",
        "search_sem_variable": "Variables",
        "search_sem_constant": "Constants",
        "search_sem_import": "Imports",
        "search_imp_any": "Any import",
        "search_imp_from": "from ... import",
        "search_imp_include": "include",
        "search_imp_using": "using",
        "search_imp_require": "require",
        "search_blk_any": "Any block",
        "search_blk_if": "if",
        "search_blk_for": "for",
        "search_blk_while": "while",
        "search_blk_try": "try",
        "search_blk_catch": "catch / except",
        "search_blk_switch": "switch / match",
        "search_blk_function": "function / def",
        "search_cmt_any": "Any comment",
        "search_cmt_inline": "Inline",
        "search_cmt_block": "Block",
        "search_cmt_docstring": "Docstring",
        "search_cmt_empty": "Commented-out code",
        "search_ast_any": "Any node",
        "search_ast_function": "Function",
        "search_ast_class": "Class",
        "search_ast_call": "Call",
        "search_ast_import": "Import",
        "search_ast_assign": "Assignment",
        "search_ast_name": "Name",
        "search_ast_constant": "Constant",
        "search_ast_loop": "Loop",
        "search_ast_conditional": "Conditional",
        "editor_sugg_function": "function",
        "editor_sugg_class": "class",
        "editor_sugg_method": "method",
        "editor_sugg_variable": "variable",
        "editor_sugg_import": "import",
        "editor_sugg_parameter": "parameter",
        "editor_sugg_type": "type",
        "editor_sugg_keyword": "keyword",
        "editor_sugg_context": "context",
        "editor_status_ready": "{count} script(s) available. Load one to start editing.",
        "editor_noselect": "Select a script on the Scripts tab.",
        "editor_loaded": "Loaded {name}",
        "editor_modified": "modified",
        "editor_saved": "saved {name}",
        "editor_unchanged": "no changes to save",
        "editor_exported": "exported: {path}",
        "editor_not_found": "not found",
        "editor_found_at": "match at {pos}",
        "editor_replaced": "replaced 1 occurrence",
        "editor_replaced_all": "replaced {count} occurrence(s)",
        "search": "Search",
        "apps": "Apps",
        "apps_title": "APPS",
        "apps_subtitle": "Your personal launchpad",
        "apps_search": "Search apps...",
        "apps_all": "All apps",
        "apps_new_group": "+ Group",
        "apps_add": "+ Add app",
        "apps_open_folder": "Open folder",
        "apps_sort_name": "Name",
        "apps_sort_path": "Path",
        "apps_sort_group": "Group",
        "apps_sort_menu": "Sort by",
        "apps_view_menu": "View",
        "apps_icon_small": "Small icons",
        "apps_icon_medium": "Medium icons",
        "apps_icon_large": "Large icons",
        "apps_reveal_folder": "Open location",
        "apps_copy_path": "Copy path",
        "apps_favorites": "Favorites",
        "apps_recent": "Recently used",
        "apps_pin": "Add to favorites",
        "apps_unpin": "Remove from favorites",
        "apps_icon": "Icon",
        "apps_last_used": "Last used: {when}",
        "apps_missing_path": "Path not found",
        "apps_missing_path_body": "{path}\n\nSave anyway?",
        "apps_grid_view": "Grid view",
        "apps_list_view": "List view",
        "apps_status": "{count} applications",
        "apps_launch": "Launch",
        "apps_edit": "Edit",
        "apps_remove": "Remove",
        "apps_add_title": "Add application",
        "apps_browse": "Browse...",
        "apps_name": "Name",
        "apps_path": "Application",
        "apps_args": "Arguments",
        "apps_group": "Group",
        "apps_no_group": "No group",
        "apps_group_name": "Group name:",
        "apps_edit_group": "Edit group",
        "apps_folder_question": "Open as popup folder?",
        "apps_close": "Close",
        "apps_error": "Launcher error",
        "apps_add_multi": "+ Add several...",
        "apps_sort_usage": "Most used",
        "apps_sort_recent": "Last used",
        "apps_sort_missing": "Missing path",
        "apps_sort_desc": "Descending",
        "apps_sort_dir": "Sort descending",
        "apps_kind": "Type",
        "apps_kind_auto": "Automatic",
        "apps_kind_exec": "Executable",
        "apps_kind_shell": "Shell command",
        "apps_kind_url": "URL / link",
        "apps_kind_folder": "Folder",
        "apps_kind_doc": "Document",
        "apps_kind_script": "ScriptDB script",
        "apps_tags": "Tags",
        "apps_tags_hint": "work, tools, games",
        "apps_hotkey": "Shortcut",
        "apps_hotkey_hint": "Launch this app from anywhere with this shortcut",
        "apps_move_menu": "Send to...",
        "apps_notes": "Notes...",
        "apps_notes_hint": "Where to find it, licence, account, password hint...",
        "apps_use_as": "Use as...",
        "apps_use_diff_a": "Compare its file in the Diff tab",
        "apps_use_diff_b": "Compare it against a file...",
        "apps_use_docs": "Store its content in DOCs",
        "apps_use_search": "Search its name in the Search tab",
        "apps_into_docs": "imported into DOCs: {count}",
        "apps_into_docs_failed": "DOCs is unavailable for this entry",
        "apps_tools_menu": "Launcher",
        "apps_export": "Export apps...",
        "apps_import": "Import apps...",
        "apps_exported": "exported: {path}",
        "apps_imported": "imported {count} application(s)",
        "apps_open_dir": "Open launcher folder",
        "apps_check_paths": "Check all paths",
        "apps_all_ok": "every path exists",
        "apps_danger_menu": "Remove...",
        "apps_clear_broken": "Remove broken entries",
        "apps_clear_broken_body": "Remove {count} entr(ies) whose path is missing?",
        "apps_cleared_broken": "removed {count} entr(ies)",
        "apps_clear_all": "Remove everything",
        "apps_clear_all_body": "Remove all {apps} app(s) and {groups} group(s)?",
        "apps_cleared_all": "launcher emptied ({count})",
        "apps_review": "Review all apps",
        "apps_state": "State",
        "apps_state_ok": "OK",
        "apps_usage": "Launches",
        "apps_added_many": "added {count} application(s)",
        "apps_status_missing": "{count} missing path(s)",
        "apps_none": "no applications to launch",
        "shopping": "Shopping",
        "shopping_title": "SHOPPING",
        "shopping_subtitle": "Shopping list with a running total",
        "shopping_search": "Filter items...",
        "shopping_name_hint": "What to buy...",
        "shopping_add": "+ Add item",
        "shopping_col_done": "Bought",
        "shopping_col_name": "Item",
        "shopping_col_quantity": "Qty",
        "shopping_col_price": "Price",
        "shopping_col_category": "Category",
        "shopping_col_note": "Note",
        "shopping_col_total": "Total",
        "shopping_total": "Total: {total} {currency}  |  {pending} to buy, {done} bought",
        "shopping_count": "{count} item(s)",
        "shopping_toggle_done": "Tick / untick",
        "shopping_remove": "Remove",
        "shopping_rename": "Rename item",
        "shopping_rename_prompt": "New name:",
        "shopping_clear_done": "Remove bought items",
        "shopping_clear_done_ask": "Remove {count} bought item(s)?",
        "shopping_clear_all": "Clear list",
        "shopping_clear_all_ask": "Remove all {count} item(s)?",
        "shopping_nothing_done": "nothing bought yet",
        "shopping_hide_done": "Hide bought items",
        "shopping_export": "Save to file...",
        "shopping_export_txt": "Text list (.txt)",
        "shopping_export_csv": "Spreadsheet (.csv)",
        "shopping_export_md": "Markdown checklist (.md)",
        "shopping_export_json": "JSON (.json)",
        "shopping_export_pending": "Only what is left to buy",
        "shopping_import": "Load from file...",
        "shopping_imported": "imported {count}",
        "shopping_print": "Print",
        "shopping_printed": "sent to the printer",
        "shopping_open_dir": "Open list folder",
        "shopping_saved": "saved: {path}",
        "shopping_report_title": "Shopping list",
        "shopping_report_generated": "Generated {when}",
        "apps_empty_command": "empty command",
        "apps_no_script": "no ScriptDB script is linked to this entry",
        "editor": "Editor",
        "modules": "Modules",
        "audit": "Audit",
        "audit_empty": "No audit entries yet.",
        "audit_refresh": "Refresh",
        "audit_accept": "Mark as audited",
        "debugger": "Debugger",
        "debugger_step_menu": "Step...",
        "debugger_refresh": "Refresh",
        "debugger_clear": "Clear session",
        "debugger_script": "Script:",
        "debugger_line": "Line:",
        "debugger_condition": "Condition (optional)",
        "debugger_add_bp": "Add breakpoint",
        "debugger_watch": "Watch:",
        "debugger_watch_hint": "Expression to watch...",
        "debugger_add_watch": "Add watch",
        "debugger_del_watch": "Remove watch",
        "debugger_breakpoints": "Breakpoints",
        "debugger_stack": "Call stack",
        "debugger_watches": "Watches",
        "debugger_trace": "Trace log:",
        "debugger_trace_hint": "Note...",
        "debugger_toggle_bp": "Toggle",
        "debugger_remove_bp": "Remove breakpoint",
        "debugger_col_script": "Script",
        "debugger_col_line": "Line",
        "debugger_col_kind": "Kind",
        "debugger_col_condition": "Condition",
        "debugger_bp_line": "line",
        "debugger_bp_condition": "condition",
        "debugger_bp_exception": "exception",
        "debugger_bp_function": "function",
        "debugger_language": "Language:",
        "debugger_language_set": "language: {name}",
        "debugger_validate": "Validate",
        "debugger_validate_all": "Validate all",
        "debugger_validated_all": "validated {count} script(s): {failed} "
                                  "with problem(s)",
        "debugger_validation": "Validation",
        "debugger_valid_ok": "valid: {summary}",
        "debugger_valid_failed": "invalid: {error}",
        "debugger_overview": "Debug overview",
        "debugger_overview_title": "Debug overview",
        "debugger_overview_ready": "overview ready",
        "debugger_frames_guessed": "{count} frame(s) guessed by the "
                                    "{language} adapter",
        "debugger_summary_col": "Summary",
        "debugger_error": "Error",
        "debugger_at": "At",
        "debugger_counts": "Counts",
        "debugger_no_script": "(no script selected)",
        "debugger_flow": "Code flow:",
        "debugger_record": "",
        "debugger_flow_all": "all kinds",
        "debugger_flow_empty": "(no recording)",
        "debugger_flow_none": "(no events of this kind)",
        "debugger_flow_more": "... and {count} more event(s)",
        "debugger_flow_recorded": "recorded {count} event(s) in {language}",
        "debugger_flow_saved": "flow report written: {path}",
        "debugger_flow_step": "step",
        "debugger_flow_block": "block",
        "debugger_flow_call": "call",
        "debugger_flow_method": "method",
        "debugger_flow_import": "import",
        "debugger_flow_variable": "variable",
        "debugger_flow_exception": "exception",
        "debugger_flow_error": "error",
        "debugger_flow_warning": "warning",
        "debugger_flow_log": "log",
        "debugger_flow_view": "View:",
        "debugger_flow_save": "Save views",
        "debugger_flow_views_saved": "views written: {path}",
        "debugger_view_timeline": "Timeline",
        "debugger_view_call_tree": "Call tree",
        "debugger_view_call_stack": "Call stack",
        "debugger_view_block_map": "Block map",
        "debugger_view_function_map": "Function map",
        "debugger_view_class_map": "Class map",
        "debugger_view_module_map": "Module map",
        "debugger_view_exception_map": "Exception map",
        "sandbox": "Sandbox",
        "sandbox_refresh": "Refresh",
        "sandbox_ready": "isolated and ready",
        "sandbox_preset": "Preset:",
        "sandbox_timeout": "Timeout (s):",
        "sandbox_output_cap": "Output cap:",
        "sandbox_allow_network": "Allow network",
        "sandbox_allow_writes": "Allow writes",
        "sandbox_script": "Script:",
        "sandbox_run": "Run in sandbox",
        "sandbox_scan": "Scan policy",
        "sandbox_save_report": "Save report",
        "sandbox_rules": "Policy rules",
        "sandbox_findings": "Scan findings",
        "sandbox_col_rule": "Rule",
        "sandbox_col_state": "State",
        "sandbox_col_detail": "Detail",
        "sandbox_preset_strict": "Strict",
        "sandbox_preset_standard": "Standard",
        "sandbox_preset_permissive": "Permissive",
        "sandbox_rule_network": "Network access",
        "sandbox_rule_writes": "Filesystem writes",
        "sandbox_rule_subprocess": "Child processes",
        "sandbox_rule_timeout": "Wall-clock limit",
        "sandbox_rule_output": "Output cap",
        "sandbox_rule_cwd": "Working directory",
        "sandbox_iso_group": "Process isolation",
        "sandbox_iso_process": "Separate process",
        "sandbox_iso_environment": "Separate environment",
        "sandbox_iso_path": "Separate PATH",
        "sandbox_iso_cwd": "Separate working directory",
        "sandbox_iso_runtime": "Separate runtime context",
        "sandbox_iso_terminal": "Terminal access",
        "sandbox_fs_group": "Filesystem sandbox",
        "sandbox_fs_whitelist": "Path whitelist",
        "sandbox_fs_blacklist": "Path blacklist",
        "sandbox_fs_read": "Read outside sandbox",
        "sandbox_fs_write": "Write outside sandbox",
        "sandbox_fs_destructive": "Destructive ops",
        "sandbox_fs_recursive": "Recursive ops",
        "sandbox_fs_system": "System directories",
        "sandbox_fs_pseudo": "Virtual filesystem",
        "sandbox_fs_shadow": "Shadow copy",
        "sandbox_fs_snapshot": "FS snapshot",
        "sandbox_list_fs_whitelist": "Allowed paths (one per line)",
        "sandbox_net_group": "Network sandbox",
        "sandbox_net_network": "Network access",
        "sandbox_net_whitelist": "Host whitelist",
        "sandbox_net_allowed_ports": "Allowed ports",
        "sandbox_net_ports": "Blocked ports",
        "sandbox_net_protocols": "Blocked protocols",
        "sandbox_net_raw": "Raw sockets",
        "sandbox_net_icmp": "ICMP / ping",
        "sandbox_net_dns": "DNS",
        "sandbox_net_private": "Private ranges",
        "sandbox_net_admin": "Admin ports",
        "sandbox_net_http": "HTTP",
        "sandbox_net_https": "HTTPS",
        "sandbox_net_monitor": "Traffic monitor",
        "sandbox_list_net_whitelist": "Allowed hosts (one per line)",
        "sandbox_sys_group": "Syscall filter",
        "sandbox_sys_exec": "Exec (execve)",
        "sandbox_sys_fork": "Fork",
        "sandbox_sys_spawn": "Spawn",
        "sandbox_sys_kill": "Kill",
        "sandbox_sys_mount": "Mount",
        "sandbox_sys_chmod": "Chmod",
        "sandbox_sys_chown": "Chown",
        "sandbox_sys_open": "Open",
        "sandbox_sys_write": "Write",
        "sandbox_sys_socket": "Socket",
        "sandbox_sys_bind": "Bind",
        "sandbox_sys_connect": "Connect",
        "audit_integrity": "System integrity",
        "audit_integrity_ok": "OK",
        "audit_integrity_warning": "Warning",
        "audit_integrity_critical": "Critical",
        "audit_problems": "Problems found",
        "audit_changes": "Changes since last audit",
        "audit_changes_detail": "+{added} / ~{modified} / -{removed}",
        "audit_risk": "Risk level",
        "audit_risk_low": "Low",
        "audit_risk_medium": "Medium",
        "audit_risk_high": "High",
        "audit_feed_security": "Recent security events",
        "audit_feed_modules": "Recent module changes",
        "audit_feed_config": "Recent configuration changes",
        "audit_when": "When",
        "audit_where": "Script",
        "audit_detail": "Detail",
        "audit_none": "none",
        "audit_never": "never",
        "audit_last": "Last audit: {when}",
        "audit_summary": "{total} script(s), {problems} problem(s), risk: {risk}",
        "audit_revisions": "{count} revision(s)",
        "audit_problem_empty": "Empty script body",
        "audit_problem_nometa": "No metadata",
        "audit_problem_notags": "No tags",
        "audit_problem_history": "History was truncated",
        "audit_modules_title": "Module audit",
        "audit_module": "Module",
        "audit_integrity_col": "Integrity",
        "audit_signature": "Signature",
        "audit_version": "Version",
        "audit_changed": "Changed",
        "audit_permissions": "Permissions",
        "audit_hash_ok": "hash OK",
        "audit_hash_mismatch": "mismatch",
        "audit_sig_valid": "valid",
        "audit_sig_invalid": "invalid",
        "audit_sig_none": "none",
        "audit_yes": "yes",
        "audit_no": "no",
        "audit_scripts_title": "Script audit",
        "audit_script": "Script",
        "audit_change_col": "Changes",
        "audit_change_new": "new",
        "audit_change_modified": "modified",
        "audit_change_unchanged": "unchanged",
        "audit_unused_funcs": "Unused functions",
        "audit_unused_imports": "Unused imports",
        "audit_suspicious": "Suspicious",
        "audit_lint": "Lint errors",
        "audit_permissions_title": "Permission analysis",
        "audit_target": "Target",
        "audit_files": "Files",
        "audit_network": "Network",
        "audit_environment": "Environment",
        "audit_exec": "Exec",
        "audit_findings_title": "Security findings",
        "audit_finding": "Finding",
        "audit_severity": "Severity",
        "audit_find_unauthorized": "Unauthorized change",
        "audit_find_hash_mismatch": "Hash mismatch",
        "audit_find_unsigned": "Unsigned module",
        "audit_find_sandbox": "Sandbox escape attempt",
        "audit_find_syscall": "Suspicious system call",
        "audit_cfg_changes": "Configuration changes",
        "audit_cfg_policy": "Policy compliance",
        "audit_file": "File",
        "audit_key": "Key",
        "audit_issue": "Issue",
        "audit_policy": "Policy",
        "audit_result": "Result",
        "audit_cfg_added": "New key",
        "audit_cfg_changed": "Value changed",
        "audit_cfg_removed": "Missing key",
        "audit_cfg_type": "Type mismatch",
        "audit_cfg_unused": "Unused setting",
        "audit_cfg_unreadable": "Unreadable or invalid config",
        "audit_cfg_policy_violation": "violation",
        "audit_cfg_policy_ok": "compliant",
        "audit_runtime_events": "Runtime events",
        "audit_runtime_anomalies": "Runtime anomalies",
        "audit_kind": "Kind",
        "audit_time_ms": "Time (ms)",
        "audit_anomaly": "Anomaly",
        "audit_anomaly_slow": "Slow execution",
        "audit_anomaly_repeat": "Repeated errors",
        "audit_anomaly_stacktrace": "Stacktrace detected",
        "audit_anomaly_crash": "Crash / non-zero exit",
        "audit_tab_dashboard": "Dashboard",
        "audit_tab_modules": "Modules and scripts",
        "audit_tab_security": "Security",
        "audit_tab_config": "Configuration",
        "audit_tab_runtime": "Runtime",
        "audit_tab_timeline": "Change history",
        "audit_tab_environment": "Environment",
        "audit_tab_integration": "Integration",
        "audit_tab_export": "Export",
        "audit_integration_open": "Open audit in...",
        "audit_integration_log": "Integration log",
        "audit_open_diff": "Open in Diff",
        "audit_open_search": "Open in Search",
        "audit_open_runner": "Open in Runner",
        "audit_open_debugger": "Open in Debugger",
        "audit_generate_report": "Generate audit report",
        "audit_export_artifact": "Export audit as artifact",
        "audit_operations": "Audit operations",
        "audit_op_full": "Full system audit",
        "audit_op_module": "Audit selected module",
        "audit_op_script": "Audit selected script",
        "audit_op_config": "Configuration audit",
        "audit_op_environment": "Environment audit",
        "audit_op_security": "Security audit",
        "audit_op_changes": "Change audit",
        "audit_op_dependencies": "Dependency audit",
        "audit_op_runtime": "Runtime audit",
        "audit_ok": "OK",
        "audit_failed": "FAILED",
        "audit_integration_noselect": "no script selected",
        "audit_integration_nodiff": "Diff tab unavailable",
        "audit_integration_nosearch": "search unavailable",
        "audit_integration_norunner": "runner unavailable",
        "audit_integration_nodebugger": "debugger unavailable",
        "audit_tl_how_manual": "manual",
        "audit_export_file": "audit exported",
        "audit_export_title": "Export audit",
        "audit_export_preview": "Report preview",
        "audit_export_txt": "Export TXT (ASCII)",
        "audit_export_json": "Export JSON (structural)",
        "audit_export_log": "Export LOG",
        "audit_export_full": "Export full audit set",
        "audit_export_comparative": "Export comparative audit",
        "audit_export_preview_txt": "Preview TXT",
        "audit_export_preview_json": "Preview JSON",
        "audit_export_preview_log": "Preview LOG",
        "audit_export_dir": "Open export folder",
        "audit_tab_architecture": "Architecture",
        "audit_tab_premium": "Premium / Automation",
        "audit_arch_caps": "Capabilities",
        "audit_arch_frontends": "Front-ends (API + CLI + GUI)",
        "audit_arch_frontend_text": "API: audit_api(tab) | CLI: audit_cli([...]) | GUI: this tab",
        "audit_arch_plugins": "Plugins",
        "audit_arch_plugin": "Plugin",
        "audit_arch_plugin_add": "Register demo plugin",
        "audit_arch_plugin_refresh": "Refresh plugins",
        "audit_arch_events": "Event bus",
        "audit_arch_bus_text": "events: %s",
        "audit_arch_snapshots": "Snapshots",
        "audit_arch_snapshot_name": "Snapshot",
        "audit_arch_snapshot_file": "File",
        "audit_arch_snapshot_save": "Save snapshot",
        "audit_arch_snapshot_refresh": "Refresh snapshots",
        "audit_arch_cli_help": "CLI: capabilities",
        "audit_arch_cli_integrity": "CLI: integrity",
        "audit_arch_cli_risk": "CLI: risk",
        "audit_arch_cli": "CLI command",
        "audit_unavailable": "Audit tab is not available",
        "audit_script_audited": "audited",
        "audit_exported": "Audit exported to {path}",
        "audit_this_script": "Audit this script",
        "audit_open_tab": "Open Audit tab",
        "audit_run_security": "Security audit",
        "audit_export_menu": "Export audit report",
        "audit_diff_open": "diff reviewed",
        "audit_premium_auto": "Automatic audits",
        "audit_premium_scheduler": "Cyclic audit (scheduler)",
        "audit_premium_scheduler_on": "Enabled",
        "audit_premium_interval": "Interval (s):",
        "audit_premium_run_now": "Run now",
        "audit_premium_comparative": "Comparative audit",
        "audit_premium_compare_run": "Show changes since last audit",
        "audit_premium_risk": "Security audit (risk levels)",
        "audit_premium_integrity": "Workspace integrity",
        "audit_trigger_file": "Audit after file change",
        "audit_trigger_install": "Audit after module install",
        "audit_trigger_update": "Audit after module update",
        "audit_trigger_error": "Audit after runtime error",
        "audit_trigger_deploy": "Audit after deploy",
        "audit_how_event": "event: %s",
        "audit_how_scheduled": "scheduled",
        "audit_integrity_new": "not in baseline",
        "audit_integrity_changed": "fingerprint changed",
        "audit_integrity_missing": "missing entry",
        "audit_tl_title": "Change timeline",
        "audit_tl_who": "Who",
        "audit_tl_category": "Category",
        "audit_tl_what": "What",
        "audit_tl_how": "How",
        "audit_tl_how_auto": "automatic",
        "audit_tl_added": "added",
        "audit_tl_modified": "modified",
        "audit_tl_removed": "removed",
        "audit_tl_cat_files": "Files",
        "audit_tl_cat_modules": "Modules",
        "audit_tl_cat_config": "Configuration",
        "audit_tl_cat_environment": "Environment",
        "audit_tl_cat_permissions": "Permissions",
        "audit_diff_title": "Snapshot diff",
        "audit_diff_item": "Entry",
        "audit_diff_kind": "Kind",
        "audit_diff_field": "Field",
        "audit_diff_before": "Before",
        "audit_diff_after": "After",
        "audit_diff_kind_added": "added",
        "audit_diff_kind_modified": "modified",
        "audit_diff_kind_removed": "removed",
        "audit_large_title": "Large changes",
        "audit_large_item": "Entry",
        "audit_large_before": "Before",
        "audit_large_after": "After",
        "audit_large_delta": "Delta",
        "audit_large_change_threshold": "Large change threshold",
        "audit_env_vars": "Environment variables",
        "audit_env_path": "PATH analysis",
        "audit_env_interp": "Interpreters",
        "audit_env_name": "Name",
        "audit_env_value": "Value",
        "audit_env_length": "Length",
        "audit_env_entry": "Entry",
        "audit_env_exists": "Exists",
        "audit_env_issue": "Issue",
        "audit_env_version": "Version",
        "audit_env_path_missing": "path does not exist",
        "audit_env_path_duplicate": "duplicate entry",
        "audit_env_path_relative": "relative entry",
        "audit_dep_title": "Dependencies",
        "audit_dep_name": "Package",
        "audit_dep_version": "Version",
        "audit_dep_versions": "Versions",
        "audit_dep_source": "Source",
        "audit_dep_used_by": "Used by",
        "audit_dep_conflicts": "Version conflicts",
        "audit_dep_missing": "missing",
        "analyzer": "Analyzer",
        "analyzer_analyse": "Analyse",
        "analyzer_export": "Export report",
        "analyzer_exported": "Exported: {path}",
        "analyzer_run": "analysis",
        "analyzer_status": "{scripts} script(s), {issues} issue(s), avg score {score}",
        "analyzer_tab_overview": "Overview",
        "analyzer_tab_structure": "Structure",
        "analyzer_tab_complexity": "Complexity",
        "analyzer_tab_issues": "Issues",
        "analyzer_tab_dependencies": "Dependencies",
        "analyzer_scripts": "Scripts",
        "analyzer_issues": "Issues",
        "analyzer_score": "Avg. score",
        "analyzer_worst": "Lowest score",
        "analyzer_overview": "Per-script overview",
        "analyzer_structure": "Structure",
        "analyzer_complexity": "Complexity",
        "analyzer_dependencies": "Dependencies",
        "analyzer_lines": "Lines",
        "analyzer_funcs": "Funcs",
        "analyzer_classes": "Classes",
        "analyzer_symbol": "Symbol",
        "analyzer_kind": "Kind",
        "analyzer_in": "In script",
        "analyzer_function": "Function",
        "analyzer_params": "Params",
        "analyzer_issue_kind": "Issue",
        "analyzer_line": "Line",
        "analyzer_module": "Module",
        "analyzer_used_by": "Used by",
        "analyzer_kind_class": "class",
        "analyzer_kind_function": "function",
        "analyzer_kind_internal": "internal",
        "analyzer_kind_external": "external",
        "analyzer_issue_long_line": "Long line",
        "analyzer_issue_trailing_ws": "Trailing whitespace",
        "analyzer_issue_tabs": "Tab character",
        "analyzer_issue_bare_except": "Bare except",
        "analyzer_issue_eval": "eval/exec usage",
        "analyzer_issue_todo": "TODO/FIXME",
        "analyzer_issue_print": "print() in library code",
        "analyzer_issue_unused_import": "Unused import",
        "analyzer_tab_syntax": "Syntax",
        "analyzer_tab_ast": "AST",
        "analyzer_syntax": "Syntax analysis",
        "analyzer_syntax_kind": "Kind",
        "analyzer_ast_node": "Node",
        "analyzer_ast_functions": "functions",
        "analyzer_ast_classes": "classes",
        "analyzer_ast_blocks": "blocks",
        "analyzer_ast_imports": "imports",
        "analyzer_ast_calls": "calls",
        "analyzer_ast_variables": "variables",
        "analyzer_ast_signatures": "signatures",
        "analyzer_ast_data_flow": "data flow",
        "analyzer_ast_unused": "defined, never used",
        "analyzer_syntax_error": "Syntax error",
        "analyzer_syntax_indent": "Indentation error",
        "analyzer_syntax_unclosed": "Unclosed block",
        "analyzer_syntax_expected": "Unexpected token",
        "analyzer_syntax_invalid": "Invalid syntax",
        "analyzer_syntax_unmatched": "Unmatched bracket",
        "analyzer_syntax_unterminated_string": "Unterminated string",
        "analyzer_syntax_unclosed_def": "Unclosed function",
        "analyzer_syntax_unclosed_class": "Unclosed class",
        "analyzer_syntax_unclosed_if": "Unclosed if",
        "analyzer_syntax_unclosed_for": "Unclosed for",
        "analyzer_syntax_unclosed_while": "Unclosed while",
        "analyzer_syntax_unclosed_try": "Unclosed try",
        "analyzer_indent_mixed": "Mixed tabs and spaces",
        "analyzer_indent_tab": "Tab indentation",
        "analyzer_indent_irregular": "Irregular indentation",
        "analyzer_type_number_str": "Number plus string",
        "analyzer_type_str_number": "String plus number",
        "analyzer_type_reassign": "Variable type changed",
        "analyzer_tab_quality": "Quality",
        "analyzer_tab_runtime": "Runtime",
        "analyzer_quality_item": "Item",
        "analyzer_quality_standards": "Standards",
        "analyzer_quality_duplicates": "Duplicate code",
        "analyzer_quality_long_funcs": "Long functions",
        "analyzer_quality_complex": "Complex blocks",
        "analyzer_quality_params": "Too many parameters",
        "analyzer_quality_nesting": "Deep nesting",
        "analyzer_quality_dead": "Dead code",
        "analyzer_style_long_line": "Long line",
        "analyzer_style_trailing_ws": "Trailing whitespace",
        "analyzer_style_tabs": "Tab indentation",
        "analyzer_style_blank_lines": "Too many blank lines",
        "analyzer_style_import_order": "Import after code",
        "analyzer_style_naming": "Naming convention",
        "analyzer_style_comparison": "Comparison to None/bool",
        "analyzer_style_lambda": "Lambda assignment",
        "analyzer_dead_code": "Unreachable statement",
        "analyzer_rt_steps": "Trace steps",
        "analyzer_rt_exceptions": "Exceptions",
        "analyzer_rt_dous": "Dangerous",
        "analyzer_rt_anomalies": "Anomalies",
        "analyzer_rt_item": "Item",
        "analyzer_rt_trace": "Execution trace",
        "analyzer_rt_dangerous": "Dangerous paths",
        "analyzer_rt_stack": "Stack traces",
        "analyzer_rt_timing": "Execution time",
        "analyzer_rt_calls": "Module calls",
        "analyzer_rt_zerodiv": "Possible divide by zero",
        "analyzer_rt_index": "Possible index error",
        "analyzer_rt_attr": "Possible attribute/call error",
        "analyzer_rt_key": "Possible key error",
        "analyzer_rt_int": "Possible value error",
        "analyzer_rt_file": "Possible file error",
        "analyzer_rt_eval": "eval() call",
        "analyzer_rt_exec": "exec() call",
        "analyzer_rt_os_system": "os.system() call",
        "analyzer_rt_subprocess": "subprocess usage",
        "analyzer_rt_popen": "popen() call",
        "analyzer_rt_pickle": "pickle.loads call",
        "analyzer_rt_shell": "shell=True",
        "analyzer_rt_rmtree": "shutil.rmtree call",
        "analyzer_rt_infinite_loop": "Loop without exit",
        "analyzer_rt_blocking": "Blocking call",
        "analyzer_rt_unclosed_resource": "Resource never closed",
        "analyzer_dep_missing": "Missing",
        "analyzer_dep_unused": "Unused",
        "analyzer_dep_conflicts": "Conflicts",
        "analyzer_dep_system": "System",
        "analyzer_dep_item": "Item",
        "analyzer_dep_imports": "Imports",
        "analyzer_dep_interpreters": "Interpreter versions",
        "analyzer_dep_executable": "executable",
        "analyzer_dep_os_module": "OS module",
        "analyzer_dep_service": "service",
        "analyzer_yes": "yes",
        "analyzer_no": "no",
        "analyzer_standards": "Standards",
        "analyzer_tab_metrics": "Metrics",
        "analyzer_metrics": "Metrics",
        "analyzer_m_metric": "Metric",
        "analyzer_m_value": "Value",
        "analyzer_m_lines": "Lines of code",
        "analyzer_m_imports": "Imports",
        "analyzer_m_errors": "Errors",
        "analyzer_m_warnings": "Warnings",
        "analyzer_m_cyclomatic": "Cyclomatic",
        "analyzer_m_structural": "Structural",
        "analyzer_m_logical": "Logical",
        "analyzer_tab_history": "Diff",
        "analyzer_history": "Change detection",
        "analyzer_hist_item": "Item",
        "analyzer_hist_prev": "Changes vs previous",
        "analyzer_hist_snapshot": "Changes vs snapshot",
        "analyzer_hist_refactors": "Refactors",
        "analyzer_hist_total": "Total changes",
        "analyzer_hist_previous": "Previous version",
        "analyzer_hist_snapshot_group": "Snapshot",
        "analyzer_hist_functions": "Functions",
        "analyzer_hist_classes": "Classes",
        "analyzer_hist_imports": "Imports",
        "analyzer_hist_logic": "Logic",
        "analyzer_hist_none": "no comparison",
        "analyzer_hist_open_diff": "Compare in Diff tab",
        "analyzer_hist_open_diff_hint": "Open this script and its previous version in the Diff tab.",
        "analyzer_hist_diff_unavailable": "Diff tab unavailable",
        "analyzer_tab_config": "Config",
        "analyzer_config": "Configuration analysis",
        "analyzer_cfg_item": "Item",
        "analyzer_cfg_files": "Config files",
        "analyzer_cfg_runtime": "Runtime environment",
        "analyzer_cfg_env": "Env vars",
        "analyzer_cfg_path": "PATH entries",
        "analyzer_cfg_interpreters": "Interpreters",
        "analyzer_operations": "Operations",
        "analyzer_tab_operations": "Operations",
        "analyzer_operations_result": "Operation result",
        "analyzer_op_item": "Item",
        "analyzer_op_scope": "Scope",
        "analyzer_op_items": "Findings",
        "analyzer_op_risk": "Risk",
        "analyzer_op_time": "Generated",
        "analyzer_op_full": "Full script analysis",
        "analyzer_op_fragment": "Selected fragment analysis",
        "analyzer_op_function": "Function analysis",
        "analyzer_op_class": "Class analysis",
        "analyzer_op_imports": "Import analysis",
        "analyzer_op_dependencies": "Dependency analysis",
        "analyzer_op_errors": "Error analysis",
        "analyzer_op_security": "Security analysis",
        "analyzer_op_runtime": "Runtime analysis",
        "analyzer_op_changes": "Change analysis",
        "analyzer_op_report": "Generate analysis report",
        "analyzer_op_overview": "Overview",
        "analyzer_op_structure": "Structure",
        "analyzer_op_stats": "Statistics",
        "analyzer_op_functions": "Functions",
        "analyzer_op_classes": "Classes",
        "analyzer_op_errors": "Errors",
        "analyzer_op_security_static": "Dangerous constructs",
        "analyzer_op_no_fragment": "no selection",
        "analyzer_op_no_fragment_hint": "Select code in the editor first.",
        "analyzer_op_none": "none",
        "analyzer_op_selected": "editor selection",
        "analyzer_op_whole_script": "whole script",
        "analyzer_op_noselect": "Select a script first.",
        "analyzer_exp_txt": "TXT (ASCII)",
        "analyzer_exp_json": "JSON (structural)",
        "analyzer_exp_log": "LOG",
        "analyzer_exp_report": "Analysis report",
        "analyzer_exp_security": "Security report",
        "analyzer_exp_changes": "Changes report",
        "analyzer_architecture": "Architecture",
        "analyzer_tab_architecture": "Architecture",
        "analyzer_arch_item": "Item",
        "analyzer_arch_standards": "Standards",
        "analyzer_arch_frontends": "Front-ends",
        "analyzer_arch_integrations": "Integrations",
        "analyzer_arch_plugins": "Plugin surface",
        "analyzer_arch_standard": "standard",
        "analyzer_arch_yes": "available",
        "analyzer_arch_no": "unavailable",
        "analyzer_arch_api": "API: describe()",
        "analyzer_arch_cli": "CLI: capabilities",
        "analyzer_arch_gui_detail": "This tab: menus, sub-tabs, exports",
        "analyzer_arch_ascii": "ASCII-only",
        "analyzer_arch_hot": "Hot-pluggable",
        "analyzer_arch_zero": "Zero-dependency",
        "analyzer_arch_cross": "Cross-platform",
        "analyzer_arch_sandbox": "Sandbox-safe",
        "analyzer_arch_plugin": "Plugin-ready",
        "analyzer_arch_artefact": "Artefact-driven",
        "analyzer_arch_frontends_cap": "API + CLI + GUI",
        "analyzer_arch_integration": "Diff + Audit integration",
        "analyzer_int_diff": "Diff tab",
        "analyzer_int_audit": "Audit tab",
        "analyzer_int_runner": "Script runner",
        "analyzer_int_hooks": "Analysis hooks",
        "analyzer_int_registry": "API registry",
        "analyzer_int_cli": "CLI entry point",
        "analyzer_premium": "Premium",
        "analyzer_tab_premium": "Premium / Automation",
        "analyzer_prem_item": "Item",
        "analyzer_prem_changes": "Watched changes",
        "analyzer_prem_predict": "Predicted risks",
        "analyzer_prem_risk_col": "Risk level",
        "analyzer_prem_project_col": "Project score",
        "analyzer_prem_auto": "Automatic analysis after every file change",
        "analyzer_prem_realtime": "Real-time analysis",
        "analyzer_prem_predictive": "Predictive analysis",
        "analyzer_prem_risk": "Risk analysis",
        "analyzer_prem_policy": "Policy compliance analysis",
        "analyzer_prem_project": "Whole-project quality analysis",
        "analyzer_prem_auto_toggle": "Auto-analyse after every change",
        "analyzer_prem_watch": "Real-time (watcher)",
        "analyzer_prem_nochange": "no change since last run",
        "analyzer_prem_nodata": "no data yet",
        "analyzer_prem_norisk": "no risk predicted",
        "analyzer_prem_total_scripts": "Scripts",
        "analyzer_prem_total_lines": "Total lines",
        "analyzer_prem_total_issues": "Total issues",
        "analyzer_policy_no_eval": "no eval/exec",
        "analyzer_policy_no_shell": "no shell=True",
        "analyzer_policy_headers": "script has a description/tag",
        "analyzer_policy_no_todo": "no lingering TODO/FIXME",
        "analyzer_policy_complexity": "cyclomatic complexity <= 10",
        "analyzer_policy_syntax": "syntax is valid",
        "analyzer_policy_ok": "compliant",
        "database": "Database",
        "script_name": "Script Name:",
        "category": "Category:",
        "tags": "Tags (comma separated):",
        "description": "Description:",
        "code": "Code:",
        "save": "Save",
        "clear": "Clear",
        "copy": "Copy",
        "delete": "Delete",
        "run": "Run",
        "edit": "Edit",
        "templates": "Templates",
        "export_json": "Export JSON",
        "import_json": "Import JSON",
        "import_file": "Import file...",
        "import_menu": "Import...",
        "import_menu_json": "Database JSON (.json)",
        "import_menu_file": "Source file(s)...",
        "import_menu_folder": "Whole folder...",
        "import_folder_empty": "No supported source files found.",
        "export_file": "Export file...",
        "export_menu": "Export...",
        "export_menu_json": "Database JSON (.json)",
        "export_menu_file": "Script file...",
        "export_menu_report": "Report (.md)",
        "import_menu": "Import...",
        "import_menu_json": "Database JSON (.json)",
        "import_menu_file": "Script file...",
        "all_source_files": "All files (*.*)",
        "confirm_export_file": "Export selected script to a file?",
        "exported_file": "Script saved to:",
        "delete_capped": "Delete selected script?",
        "history_capped": "This script's history is capped at 25 revisions.",
        "search_label": "Search:",
        "all_categories": "All Categories",
        "total_scripts": "Total scripts:",
        "name": "Name",
        "category_col": "Category",
        "tags_col": "Tags",
        "date": "Date",
        "select_script": "Please select a script first.",
        "fill_required": "Please fill in the script name and code.",
        "saved": "Script saved successfully!",
        "deleted": "Script deleted.",
        "copied": "Code copied to clipboard.",
        "exported": "Exported successfully.",
        "imported": "Imported successfully.",
        "skipped_duplicates": "Skipped duplicates",
        "filter_placeholder": "Filter by name, tag, code, lang:python, regex:/.../ ... (Ctrl+K)",
        "filter_history": "History",
        "filter_history_empty": "(no history yet)",
        "filter_history_clear": "Clear history",
        "confirm_delete": "Delete selected script?",
        "no_results": "No results found.",
        "template_select": "Select a template",
        "module_name": "Module name:",
        "module_check": "Check",
        "module_attrs": "Attributes",
        "module_doc": "Docstring",
        "module_test": "Run test",
        "module_clear": "Clear",
        "module_install": "pip install",
        "module_quick": "Quick check:",
        "module_test_code": "Test code:",
        "module_output": "Output:",
        "ready": "Ready",
        "db_tables": "Tables:",
        "db_snippets": "Ready snippets:",
        "db_query": "SQL Query:",
        "db_execute": "Execute",
        "db_clear": "Clear",
        "db_export_csv": "Export CSV",
        "db_connect": "Connect",
        "db_disconnect": "Disconnect",
        "db_save_conn": "Save connection",
        "db_del_conn": "Remove",
        "db_connection": "Connection:",
        "db_type": "DB Type:",
        "db_host": "Host:",
        "db_port": "Port:",
        "db_name": "Database:",
        "db_user": "User:",
        "db_password": "Password:",
        "db_file": "File (SQLite):",
        "saved_connections": "Saved connections",
        "name_connection": "Name:",
        "rows": "rows",
        "time_ms": "ms",
        "not_connected": "Not connected.",
        "module_not_found": "Module not found",
        "arguments": "Arguments",
        "favorites": "Favorites",
        "all_scripts": "All scripts",
        "favorites_only": "Favorites only",
        "toggle_favorite": "Favorite (Ctrl+F)",
        "show_all": "Show all",
        "duplicate": "Duplicate",
        "new_script": "New script",
        "history": "History",
        "version_history": "Version history",
        "preview": "Preview",
        "restore": "Restore",
        "diff": "Diff",
        "diff_title": "Changes in this revision",
        "no_history": "No history for this script yet.",
        "confirm_restore": "Restore this version into the editor?",
        "restored": "Version loaded into the editor.",
        "duplicated": "Script duplicated.",
        "copied_script": "Script copied to the clipboard as text.",
        "format_json": "Format JSON",
        "minify_json": "Minify JSON",
        "format_sql": "Format SQL",
        "sorted": "Sorted by name.",
        "download": "Download file",
        "report": "Export report",
        "statistics": "Statistics",
        "stats_title": "Database statistics",
        "shortcuts": "Shortcuts",
        "shortcuts_title": "Keyboard shortcuts",
        "shortcut": "Shortcut",
        "shortcut_action": "Action",
        "shortcut_group_general": "General",
        "shortcut_group_scripts": "Scripts",
        "shortcut_group_editor": "Editor",
        "shortcut_group_diff": "Diff tab",
        "syntax_error": "Syntax error in the code",
        "unknown_category": "Unknown category",
        "import_error": "Import error",
        "export_error": "Export error",
        "warning": "Warning",
        "information": "Information",
        "confirm": "Confirm",
        "info": "Info",
        "db_execute_selection": "Execute selection",
        "db_history": "Query history",
        "db_truncated": "Showing the first {shown} of {total} rows.",
        "db_save_selection": "Save as script",
        "load_problem": "Some data files could not be read and were skipped:",
        "load_problem_hint": "Saving now would overwrite them with the empty list. Back up the file and fix the JSON first.",
        "map": "Map",
        "map_toggle": "Minimap: on",
        "map_toggle_off": "Minimap: off",
        "runner_run": "Run",
        "runner_rerun": "Run again",
        "runner_stop": "Stop",
        "runner_send": "Send",
        "runner_stdin_placeholder": "Type input for the running script and press Enter...",
        "runner_wrap": "Wrap: off",
        "runner_status_ready": "Ready",
        "runner_status_running": "Running...",
        "runner_status_stdin": "Waiting for input...",
        "runner_status_done": "Done in {time}",
        "runner_status_error": "Runtime error",
        "runner_status_exit": "Exited with code {code}",
        "runner_status_timeout": "Timed out after {seconds}s",
        "runner_status_stopped": "Stopped by user",
        "runner_status_unavailable": "Not available",
        "runner_no_output": "(no output)",
        "runner_elapsed": "Elapsed: {time}",
        "runner_exit_label": "Exit code: {code}",
        "runner_no_python": "Python interpreter not found in PATH.",
        "runner_python_fallback": "No dedicated runner for this language; running as Python.",
        "runner_bash_unavailable": "Bash was not found. Install Git for Windows or WSL to run shell scripts.",
        "runner_powershell_unavailable": "PowerShell not found.",
        "runner_node_unavailable": "Node.js not found.",
        "runner_batch_unavailable": "Batch files are supported only on Windows.",
        "runner_exe_unavailable": "Executable not found: {name}",
        "runner_timeout_note": "[timeout after {seconds}s - process killed]",
        "runner_stopped_note": "[stopped by the user]",
        "runner_exit_note": "[exit code {code} - finished in {time}]",
        "runner_syntax_error": "Syntax error in the script.",
        "runner_no_runner_for_language": "No interpreter available for this language; the script was not run.",
        "runner_detected_language": "Detected language: {language}",
        "metadata": "Metadata",
        "meta_detect": "Auto-detect metadata",
        "meta_detected": "Auto-detected metadata for {count} script(s).",
        "meta_sync": "Sync all stats",
        "meta_load": "Load into form",
        "meta_apply": "Apply to selected",
        "meta_stamp": "Stamp modified",
        "meta_bump": "Bump version",
        "meta_export_json": "Export metadata JSON",
        "meta_export_csv": "Export metadata CSV",
        "meta_import": "Import metadata JSON",
        "meta_clear": "Clear metadata",
        "meta_filter": "Filter:",
        "meta_filter_hint": "Filter by name, author, license, URL, notes...",
        "meta_show_all": "Show scripts without metadata",
        "meta_all_status": "All statuses",
        "meta_author": "Author:",
        "meta_version": "Version:",
        "meta_license": "License:",
        "meta_language": "Language:",
        "meta_status": "Status:",
        "meta_url": "URL:",
        "meta_notes": "Notes:",
        "meta_lines": "Lines",
        "meta_chars": "Characters",
        "meta_hash": "SHA-256",
        "meta_created": "Created",
        "meta_modified": "Modified",
        "meta_select_hint": "Select a script first.",
        "meta_applied": "Metadata applied to {count} script(s).",
        "meta_synced": "Metadata synchronised for {count} script(s).",
        "meta_imported": "Metadata updated for {count} script(s).",
        "meta_confirm_clear": "Clear metadata for {count} selected script(s)?",
        "meta_summary": "Showing {shown} of {total} scripts ({with_meta} with metadata).",
        "compare": "Compare",
        "compare_left": "Left:",
        "compare_right": "Right:",
        "compare_swap": "Swap",
        "compare_run": "Compare",
        "compare_ignore_case": "Ignore case",
        "compare_ignore_ws": "Ignore whitespace",
        "compare_ignore_blank": "Ignore blank lines",
        "compare_view_side": "Side by side",
        "compare_view_unified": "Unified diff",
        "compare_view_fields": "Fields",
        "compare_copy": "Copy diff",
        "compare_export": "Export diff",
        "compare_field": "Field",
        "compare_left_value": "Left",
        "compare_right_value": "Right",
        "compare_select_hint": "Select two scripts to compare.",
        "compare_identical": "(the two scripts are identical)",
        "compare_no_diff": "No differences to export.",
        "compare_summary": "Similarity {ratio}%  |  identical: {same}  changed: {changed}  added: {added}  removed: {removed}  |  total changes: {total}  |  lines: {left_lines} -> {right_lines}",
        "compare_open_file": "File...",
        "compare_revision": "{name} - revision #{n}",
        "compare_stats": "Statistics",
        "compare_with": "Compare with...",
        "compare_ignore_comments": "Ignore comments",
        "compare_prev": "Prev diff",
        "compare_next": "Next diff",
        "compare_report": "Export report",
        "compare_report_title": "Script comparison report",
        "compare_clear_files": "Clear files",
        "editor_delete_line": "Delete line",
        "editor_tools_menu": "Code tools...",
        "editor_move_menu": "Move...",
        "editor_move_up": "Move up",
        "editor_move_down": "Move down",
        "editor_sort_lines": "Sort lines",
        "editor_trim_spaces": "Trim spaces",
        "editor_tabs_to_spaces": "Tabs to spaces",
        "editor_line_moved": "line moved",
        "editor_at_top": "already at the top",
        "editor_at_bottom": "already at the bottom",
        "editor_sorted": "lines sorted",
        "editor_nothing_to_sort": "nothing to sort",
        "editor_trimmed": "trimmed {count} line(s)",
        "editor_no_trailing": "no trailing spaces",
        "editor_tabs_done": "tabs converted to spaces",
        "editor_refactor_menu": "Refactor...",
        "editor_refactor_rename": "Rename symbol",
        "editor_refactor_extract_function": "Extract function",
        "editor_refactor_extract_variable": "Extract variable",
        "editor_refactor_move_function": "Move function",
        "editor_refactor_move_class": "Move class",
        "editor_cleanup_menu": "Code cleanup...",
        "editor_format_code": "Format code",
        "editor_remove_unused_imports": "Remove unused imports",
        "editor_remove_dead_code": "Remove dead code",
        "editor_fix_linter": "Fix linter errors",
        "editor_new_name": "New name:",
        "editor_function_name": "Function name:",
        "editor_variable_name": "Variable name:",
        "editor_move_definition": "Move definition",
        "editor_move_target": "Target line (1 = top):",
        "editor_need_symbol": "Select a symbol first.",
        "editor_select_expression": "Select an expression first.",
        "editor_need_definition": "Place the cursor inside a definition.",
        "editor_python_only": "This tool supports Python only.",
        "editor_refactored": "refactoring applied",
        "editor_formatted": "code formatted",
        "editor_unused_removed": "removed {count} unused import(s)",
        "editor_dead_removed": "removed {count} dead line(s)",
        "editor_linter_fixed": "linter fixes applied ({count} import(s) removed)",
        "editor_no_changes": "no changes needed",
        "editor_analysis_menu": "Code analysis...",
        "editor_analysis_syntax": "Syntax analysis",
        "editor_analysis_ast": "AST analysis",
        "editor_analysis_semantic": "Semantic analysis",
        "editor_analysis_security": "Security analysis",
        "editor_analysis_quality": "Code quality analysis",
        "editor_analysis_runtime": "Runtime simulation",
        "editor_analysis_dependencies": "Dependency analysis",
        "editor_analysis_changes": "Change analysis (diff)",
        "editor_analysis_errors": "Error analysis",
        "editor_analysis_item": "Item",
        "editor_analysis_value": "Value",
        "editor_analysis_detail": "Detail",
        "editor_analysis_close": "Close",
        "editor_analysis_unavailable": "Analyzer unavailable.",
        "editor_analysis_failed": "Analysis failed: {error}",
        "editor_metrics_menu": "Code metrics",
        "editor_metric_lines": "Lines",
        "editor_metric_functions": "Functions",
        "editor_metric_classes": "Classes",
        "editor_metric_imports": "Imports",
        "editor_metric_cyclomatic": "Cyclomatic complexity",
        "editor_metric_logical": "Logical complexity",
        "editor_metric_errors": "Errors",
        "editor_metric_warnings": "Warnings",
        "editor_integration_menu": "Integrations...",
        "editor_integration_diff": "Open in Diff",
        "editor_integration_search": "Open in Search",
        "editor_integration_audit": "Open in Audit",
        "editor_integration_runner": "Open in Runner",
        "editor_integration_debugger": "Open in Debugger",
        "editor_integration_report": "Generate script report",
        "editor_integration_project": "Link to project",
        "editor_integration_opened": "{value}",
        "editor_integration_unavailable": "{value}",
        "editor_integration_failed": "Integration failed: {error}",
        "editor_integration_report_done": "report saved: {path}",
        "editor_integration_project_done": "project linked: {project}",
        "editor_project_name": "Project name:",
        "editor_architecture_menu": "Editor architecture",
        "editor_arch_ascii": "ASCII-only",
        "editor_arch_hot": "Hot-pluggable",
        "editor_arch_zero": "Zero-dependency",
        "editor_arch_cross": "Cross-platform",
        "editor_arch_sandbox": "Sandbox-safe",
        "editor_arch_plugin": "Plugin-ready",
        "editor_arch_artefact": "Artefact-driven",
        "editor_arch_frontends": "API + CLI + GUI",
        "editor_arch_events": "Event bus integration",
        "editor_arch_snapshots": "Snapshot integration",
        "editor_arch_audit_reports": "Audit and reports",
        "editor_arch_standard": "standard",
        "editor_arch_item": "Item",
        "editor_arch_kind": "Kind",
        "editor_arch_detail": "Detail",
        "editor_templates_menu": "Templates...",
        "vault": "VAULT",
        "security": "SECURITY",
        "sec_title": "SECURITY",
        "sec_state_open": "UNLOCKED",
        "sec_state_locked": "LOCKED",
        "sec_count": "{0} record(s)",
        "sec_new": "New security store. Set a passphrase.",
        "sec_locked_action": "Locked. Unlock to {action}.",
        "sec_locked": "Locked.",
        "sec_create": "Set passphrase...",
        "sec_unlock": "Unlock...",
        "sec_lock": "Lock",
        "sec_policy": "Policy...",
        "sec_high": "High security",
        "sec_high_on": "High-security mode is on.",
        "sec_high_off": "High-security mode is off.",
        "sec_passphrase_hint": "New passphrase (min 8 chars):",
        "sec_passphrase_again": "Repeat the passphrase:",
        "sec_passphrase_prompt": "Passphrase:",
        "sec_token_prompt": "Token: ",
        "sec_policy_saved": "Policy updated.",
        "sec_pick": "Pick a script first.",
        "sec_fingerprint": "Fingerprint",
        "sec_fingerprinted": "Fingerprint computed.",
        "sec_sign": "Sign",
        "sec_signed": "Script signed.",
        "sec_record": "Record",
        "sec_recorded": "Integrity record written.",
        "sec_verify": "Verify",
        "sec_verified": "Verification complete.",
        "sec_seal": "Seal",
        "sec_sealed": "Script sealed.",
        "sec_unseal": "Unseal",
        "sec_unsealed": "Script unsealed.",
        "sec_nothing_sealed": "That script was never sealed.",
        "sec_backup": "Backup",
        "sec_backed_up": "Backup written: {0}",
        "sec_restore": "Restore",
        "sec_restore_pick": "Restore which backup?",
        "sec_restored": "Backup restored.",
        "sec_no_backups": "No backups yet.",
        "sec_shadow": "Shadow key",
        "sec_shadow_issued": "Shadow key issued.",
        "sec_hash_all": "Hash all",
        "sec_hashed_all": "{0} script(s) recorded.",
        "sec_reports": "Reports...",
        "sec_report_hint": "A report or a plan appears here.",
        "sec_report_shown": "Report: {0}",
        "sec_export": "Export...",
        "sec_export_report": "Report to JSON",
        "sec_export_registry": "Registry to JSON",
        "sec_export_config": "Configuration",
        "sec_exported": "Report exported: {0}",
        "sec_exported_registry": "Registry exported: {0}",
        "sec_exported_config": "Configuration exported: {0}",
        "sec_import": "Import...",
        "sec_imported_registry": "Registry imported: {0} record(s)",
        "sec_imported_config": "Configuration imported.",
        "sec_import_unknown": "That file is not a security bundle.",
        "sec_sandbox": "Sandbox plan",
        "sec_sandbox_plan": "Sandbox plan built.",
        "sec_no_scripts": "No scripts to secure.",
        "sec_rows": "{0} script(s) listed.",
        "sec_yes": "yes",
        "sec_no": "no",
        "sec_col_name": "Script",
        "sec_col_digest": "SHA-256",
        "sec_col_record": "Record",
        "sec_col_signed": "Signed",
        "sec_col_state": "State",
        "sec_col_detail": "Detail",
        "sec_group_auth": "Authorization",
        "sec_group_sandbox": "Sandbox and isolation",
        "sec_digest_sha256": "SHA-256",
        "sec_digest_sha512": "SHA-512",
        "sec_digest_sha3": "SHA3-256",
        "sec_digest_blake2b": "BLAKE2b",
        "sec_kdf_pbkdf2": "PBKDF2-HMAC-SHA256",
        "sec_kdf_scrypt": "scrypt",
        "sec_kdf_salted": "Salted SHA-256 (iterated)",
        "sec_report_integrity": "Integrity report",
        "sec_report_changes": "Change report",
        "sec_report_access": "Access report",
        "sec_report_errors": "Error report",
        "sec_report_warnings": "Warning report",
        "sec_report_risk": "Risk report",
        "sec_auth_password": "Require a passphrase",
        "sec_auth_token": "Require a token (2nd factor)",
        "sec_auth_attempts": "Failed attempts allowed",
        "sec_auth_cooldown": "Cooldown after failures (s)",
        "sec_auth_autolock": "Auto-lock (seconds)",
        "sec_auth_high": "High-security mode",
        "sec_auth_sign": "Sign every script",
        "sec_auth_tamper": "On tamper: lock the session",
        "sec_sb_fs": "Isolate the filesystem",
        "sec_sb_net": "Block the network",
        "sec_sb_syscalls": "Restrict syscalls",
        "sec_sb_env": "Scrub the environment",
        "sec_sb_process": "Run in a separate process",
        "sec_sb_logs": "Separate logs",
        "sec_sb_shadow_fs": "Shadow filesystem",
        "sec_sb_shadow_env": "Shadow environment",
        "sec_sb_shadow_net": "Shadow network",
        "structure": "STRUCTURE",
        "st_title": "STRUCTURE",
        "st_preset": "Preset:",
        "st_name": "Project:",
        "st_default_name": "project",
        "st_target": "Target:",
        "st_target_hint": "leave empty for the workspace",
        "st_browse": "Browse...",
        "st_preview": "Preview",
        "st_dry": "Dry run",
        "st_generate": "Generate",
        "st_validate": "Validate",
        "st_compile": "Compile",
        "st_reverse": "Analyze folder...",
        "st_describe": "From a sentence...",
        "st_describe_hint": "Describe the structure, e.g. \"an rpg game in godot\":",
        "st_overwrite": "Overwrite",
        "st_no_preset": "No preset selected.",
        "st_preset_count": "{0} preset(s)",
        "st_state_scope": "scope: {0}  v{1}",
        "st_previewed": "{0} file(s), {1} folder(s).",
        "st_dry_done": "Dry run: {0} action(s).",
        "st_generated": "Generated into {0}.",
        "st_generate_failed": "Generation stopped at: {0}",
        "st_validated": "Valid: {0}  errors: {1}  warnings: {2}",
        "st_compiled": "Compiled: {0}  artifacts: {1}",
        "st_reversed": "Analyzed {0}",
        "st_reversed_status": "Read {0} file(s) into preset {1}.",
        "st_reverse_sample": "Sample",
        "st_described": "Understood: {0}",
        "st_nothing": "nothing",
        "st_saved": "Preset saved: {0} v{1}",
        "st_deleted": "Preset deleted: {0}",
        "st_exported_presets": "Preset pack written: {0}",
        "st_imported_presets": "Preset pack read: {0} preset(s)",
        "st_plugins_loaded": "Built-in generators loaded.",
        "st_pick_plugin": "Pick a plugin first.",
        "st_plugin_toggled": "{0}: {1}",
        "st_enabled": "enabled",
        "st_disabled": "disabled",
        "st_cli_empty": "Type a CLI command.",
        "st_cli_done": "CLI exit code: {0}",
        "st_cli_hint": "validate python-app",
        "st_cli_run": "Run CLI",
        "st_report_saved": "Report written: {0}",
        "st_no_target": "No generated target to pack.",
        "st_zip_written": "Zip written: {0} ({1} file(s))",
        "st_no_tree": "Preview a tree first.",
        "st_docs_written": "Documentation written: {0}",
        "st_log": "Generation log",
        "st_log_empty": "nothing recorded yet",
        "st_stage": "Stage",
        "st_written": "Written",
        "st_skipped": "Skipped",
        "st_artifacts": "Artifacts",
        "st_unfilled": "unfilled token(s): ",
        "st_folder": "folder",
        "st_file": "file",
        "st_panel_tree": "Tree",
        "st_panel_edit": "Preset",
        "st_panel_plugins": "Plugins",
        "st_panel_pipeline": "Pipeline",
        "st_template": "Template:",
        "st_edit_title": "Title",
        "st_edit_kind": "Kind",
        "st_edit_language": "Language",
        "st_edit_build": "Build system",
        "st_edit_ci": "CI",
        "st_edit_ide": "IDE",
        "st_edit_extends": "Extends",
        "st_extends_hint": "extends a preset name",
        "st_save_preset": "Save preset",
        "st_delete_preset": "Delete preset",
        "st_export_presets": "Export presets",
        "st_import_presets": "Import presets",
        "st_plugin_load": "Load built-in generators",
        "st_plugin_toggle": "Enable / disable",
        "st_save_report": "Save report",
        "st_export_zip": "Export zip",
        "st_export_docs": "Export docs",
        "st_show_log": "Log",
        "st_col_path": "Path",
        "st_col_kind": "Kind",
        "st_col_size": "Size",
        "st_col_plugin": "Plugin",
        "st_col_state": "State",
        "st_report_hint": "A tree, a plan, a report or CLI output appears here.",
        "st_scope_user": "User",
        "st_scope_org": "Organization",
        "st_scope_project": "Project",
        "st_scope_global": "Global",
        "st_kind_app": "Application",
        "st_kind_module": "Module",
        "st_kind_game": "Game",
        "st_kind_library": "Library",
        "st_kind_framework": "Framework",
        "st_kind_plugin": "Plugin",
        "st_lang_python": "Python",
        "st_lang_cpp": "C++",
        "st_lang_node": "Node / JavaScript",
        "st_lang_java": "Java",
        "st_lang_rust": "Rust",
        "st_lang_csharp": "C# / Unity",
        "st_lang_unreal": "Unreal",
        "st_lang_godot": "Godot",
        "st_build_none": "None",
        "st_build_cmake": "CMake",
        "st_build_make": "Make",
        "st_build_gradle": "Gradle",
        "st_build_npm": "npm",
        "st_build_pip": "pip",
        "st_build_cargo": "Cargo",
        "st_ci_none": "None",
        "st_ci_github": "GitHub Actions",
        "st_ci_gitlab": "GitLab CI",
        "st_ci_azure": "Azure Pipelines",
        "st_ide_none": "None",
        "st_ide_vscode": "VS Code",
        "st_ide_pycharm": "PyCharm",
        "st_ide_intellij": "IntelliJ",
        "st_stage_pre": "Pre-generation",
        "st_stage_generate": "Generation",
        "st_stage_validate": "Validation",
        "st_stage_post": "Post-generation",
        "st_stage_artifacts": "Artifacts",
        "sqlite": "SQLITE",
        "sqlite_tab_offline": "Offline database",
        "sqlite_tab_online": "Online database",
        "sqlite_tab_sync": "Sync",
        "sqlite_tab_tools": "Tools",
        "sqlite_tab_logs": "Logs",
        "sqlite_tab_settings": "Settings",
        "sqlite_open": "Open database...",
        "sqlite_create": "New database...",
        "sqlite_close": "Close",
        "sqlite_save": "Save",
        "sqlite_read_only": "Read-only",
        "sqlite_browse": "Browse...",
        "sqlite_folder": "Folder to explore...",
        "sqlite_tables": "Tables",
        "sqlite_records": "Records",
        "sqlite_schema": "Schema",
        "sqlite_sql": "SQL",
        "sqlite_stats": "Statistics",
        "sqlite_load": "Load rows",
        "sqlite_search_rows": "Filter rows...",
        "sqlite_add_row": "Add",
        "sqlite_edit_row": "Edit",
        "sqlite_copy_row": "Copy",
        "sqlite_delete_row": "Delete",
        "sqlite_prev": "Previous",
        "sqlite_next": "Next",
        "sqlite_add_column": "Add column",
        "sqlite_new_table": "New table",
        "sqlite_rename_table": "Rename table",
        "sqlite_drop_table": "Drop table",
        "sqlite_run_sql": "Run SQL",
        "sqlite_explain": "Explain",
        "sqlite_sql_all": "Whole script",
        "sqlite_refresh_stats": "Refresh statistics",
        "sqlite_col_name": "Name",
        "sqlite_col_type": "Type",
        "sqlite_col_null": "Nullable",
        "sqlite_col_key": "Key",
        "sqlite_col_value": "Value",
        "sqlite_col_detail": "Detail",
        "sqlite_col_kind": "Difference",
        "sqlite_col_offline": "Offline",
        "sqlite_col_online": "Online",
        "sqlite_col_time": "Time",
        "sqlite_col_layer": "Layer",
        "sqlite_col_action": "Action",
        "sqlite_ready": "Open a database file to begin.",
        "sqlite_open_first": "Open a database first.",
        "sqlite_choose_table": "Choose a table first.",
        "sqlite_select_row": "Select a row first.",
        "sqlite_is_read_only": "The database is open read-only.",
        "sqlite_opened": "Opened {name} ({kind}).",
        "sqlite_closed": "Database closed.",
        "sqlite_loaded_rows": "Loaded {count} row(s).",
        "sqlite_row_added": "Row added.",
        "sqlite_row_updated": "Row updated.",
        "sqlite_row_copied": "Row copied.",
        "sqlite_row_deleted": "Row deleted.",
        "sqlite_row_missing": "That row is gone.",
        "sqlite_delete_confirm": "Delete the selected row? This cannot be undone.",
        "sqlite_page": "rows {first}-{last} of {total}",
        "sqlite_first_page": "Already at the first page.",
        "sqlite_state_open": "open",
        "sqlite_state_closed": "closed",
        "sqlite_state_signed_in": "signed in",
        "sqlite_state_signed_out": "signed out",
        "sqlite_readonly_state": "read-only: {state}",
        "sqlite_no_schema": "This format has no CREATE statement.",
        "sqlite_columns_hint": "One column per line: name TYPE [PRIMARY KEY] [NOT NULL]",
        "sqlite_column_spec": "name TYPE (for example: note TEXT):",
        "sqlite_need_name": "A table needs a name and at least one column.",
        "sqlite_table_created": "Table {name} created.",
        "sqlite_table_renamed": "Table renamed.",
        "sqlite_table_dropped": "Table dropped.",
        "sqlite_column_added": "Column added.",
        "sqlite_new_name": "New name:",
        "sqlite_drop_confirm": "Drop the table {name} and every row in it?",
        "sqlite_no_sql": "Write a statement first.",
        "sqlite_sql_done": "{count} statement(s) run.",
        "sqlite_cancel": "Cancel",
        "sqlite_apply": "Apply",
        "sqlite_reset": "Reset",
        "sqlite_online_user": "User",
        "sqlite_online_pass": "Password",
        "sqlite_online_login": "Sign in",
        "sqlite_online_logout": "Sign out",
        "sqlite_online_ping": "Ping",
        "sqlite_online_refresh": "Refresh session",
        "sqlite_online_tables": "Load tables",
        "sqlite_online_rows": "Load rows",
        "sqlite_online_add": "Create row",
        "sqlite_online_update": "Update row",
        "sqlite_online_delete": "Delete row",
        "sqlite_online_welcome": "Signed in as {user}.",
        "sqlite_online_bye": "Signed out.",
        "sqlite_online_tables_loaded": "{count} table(s) on the server.",
        "sqlite_online_rows_loaded": "{count} row(s) from the server.",
        "sqlite_online_done": "{action} done.",
        "sqlite_console_body": "JSON body (optional)",
        "sqlite_console_send": "Send",
        "sqlite_console_token": "Attach the token",
        "sqlite_console_sent": "Console request sent.",
        "sqlite_sync_mode": "Mode",
        "sqlite_sync_policy": "Conflicts",
        "sqlite_sync_table": "Table to reconcile",
        "sqlite_sync_compare": "Compare",
        "sqlite_sync_plan": "Plan",
        "sqlite_sync_run": "Run the sync",
        "sqlite_sync_resolve": "Resolve the selected conflict",
        "sqlite_sync_report": "Change report",
        "sqlite_sync_save_report": "Save the report",
        "sqlite_sync_result": "Sync result",
        "sqlite_sync_confirm": "Run a '{mode}' sync on {table}?",
        "sqlite_sync_select": "Select a conflict first.",
        "sqlite_sync_side": "Which side wins?",
        "sqlite_sync_resolved": "Resolved {count} conflict(s).",
        "sqlite_sync_upload": "Offline to online",
        "sqlite_sync_download": "Online to offline",
        "sqlite_sync_both": "Bidirectional",
        "sqlite_sync_manual": "Manual (decide each change)",
        "sqlite_conflict_newest": "Newest write wins",
        "sqlite_conflict_offline": "Offline wins",
        "sqlite_conflict_online": "Online wins",
        "sqlite_conflict_ask": "Ask for every change",
        "sqlite_side_offline": "Offline",
        "sqlite_side_online": "Online",
        "sqlite_side_skip": "Skip",
        "sqlite_tool_backup": "Back up now",
        "sqlite_tool_restore": "Restore a backup",
        "sqlite_tool_integrity": "Integrity check",
        "sqlite_tool_repair": "Repair (REINDEX + VACUUM)",
        "sqlite_tool_compact": "Write a compacted copy",
        "sqlite_tool_import": "Import a file",
        "sqlite_tool_export": "Export the database",
        "sqlite_tool_duplicates": "Find duplicate rows",
        "sqlite_tool_validate": "Validate the table",
        "sqlite_tool_stats": "Table statistics",
        "sqlite_backup_keep": "Backups to keep:",
        "sqlite_backup_done": "Backup written: {path} (kept {keep})",
        "sqlite_restore_confirm": "Replace the open database with this backup?\n\n{path}\n\nA safety copy of the current file is kept.",
        "sqlite_restored": "Restored. Safety copy: {path}",
        "sqlite_imported": "Imported {rows} row(s) into {table}.",
        "sqlite_exported": "Exported {count} table(s) to {path}.",
        "sqlite_report_saved": "Report saved: {path}",
        "sqlite_log_filter": "Filter the log...",
        "sqlite_log_refresh": "Refresh",
        "sqlite_log_save": "Save the log",
        "sqlite_log_count": "{count} log entry(s).",
        "sqlite_log_saved": "Log saved: {path}",
        "sqlite_set_folder": "Default folder",
        "sqlite_set_page": "Rows per page",
        "sqlite_set_backup": "Back up before a schema change",
        "sqlite_set_readonly": "Open new databases read-only",
        "sqlite_set_online_base": "Online server address",
        "sqlite_set_online_api": "API path",
        "sqlite_settings_note": "The settings decide what the tab starts with. They are kept in memory for this session; the tab never writes them anywhere without being asked.",
        "sqlite_settings_applied": "Settings applied.",
        "sqlite_settings_reset": "Settings reset.",
        "sqlite_fmt_sqlite": "SQLite database",
        "sqlite_fmt_json": "JSON document",
        "sqlite_fmt_csv": "CSV table",
        "sqlite_fmt_text": "Text lines",
        "vault_create": "Create vault...",
        "vault_unlock": "Unlock...",
        "vault_lock": "Lock",
        "vault_change_password": "Change password...",
        "vault_policy": "Security policy...",
        "vault_open_dir": "Open vault folder",
        "vault_add": "Add",
        "vault_edit": "Edit",
        "vault_duplicate": "Copy",
        "vault_move": "Move...",
        "vault_delete": "Delete",
        "vault_flags_menu": "Flags...",
        "vault_hidden_mode": "Hidden mode",
        "vault_readonly_lock": "Read-only lock",
        "vault_import": "Import...",
        "vault_export": "Export...",
        "vault_reports_menu": "Reports...",
        "vault_integrate_menu": "Send to...",
        "vault_search": "Search",
        "vault_search_clear": "Clear",
        "vault_close": "Close",
        "vault_copy": "Copy",
        "vault_select_first": "Select a resource first.",
        "vault_add_title": "Add a resource",
        "vault_edit_title": "Edit resource",
        "vault_move_to": "Move to section:",
        "vault_flag_pinned": "Pinned",
        "vault_flag_critical": "Critical",
        "vault_flag_hidden": "Hidden",
        "vault_flag_readonly": "Read-only",
        "vault_sec_scripts": "Scripts",
        "vault_sec_documents": "Documents",
        "vault_sec_files": "Files",
        "vault_sec_passwords": "Passwords",
        "vault_sec_snapshots": "Snapshots",
        "vault_search_name": "By name",
        "vault_search_content": "By content",
        "vault_search_tags": "By tags",
        "vault_search_type": "By type",
        "vault_search_date": "By date",
        "vault_search_meta": "By metadata",
        "vault_search_all": "Every aspect",
        "vault_search_hint": "Search the vault...",
        "vault_search_empty": "Showing everything.",
        "vault_search_found": "Found {count} resource(s).",
        "vault_col_name": "Name",
        "vault_col_flags": "Flags",
        "vault_col_tags": "Tags",
        "vault_col_size": "Size",
        "vault_col_modified": "Modified",
        "vault_counts": "{shown} shown / {total} stored",
        "vault_state_locked": "LOCKED",
        "vault_state_unlocked": "UNLOCKED",
        "vault_state_hidden": "HIDDEN MODE",
        "vault_state_readonly": "READ-ONLY",
        "vault_idle": "idle {time}",
        "vault_new": "New vault. Set a master password.",
        "vault_created": "Vault created.",
        "vault_rekey_warning": "The vault already has a master password. Replacing it re-encrypts everything. Continue?",
        "vault_unlocked": "Vault unlocked.",
        "vault_locked": "locked",
        "vault_locked_msg": "Vault locked.",
        "vault_password_changed": "Master password changed.",
        "vault_no_password": "No master password is set yet.",
        "vault_bad_password": "Wrong password.",
        "vault_store_mismatch": "Store and vault passwords differ.",
        "vault_locked_action": "Locked. Unlock the vault to {action}.",
        "vault_autolocked": "Vault locked itself after inactivity.",
        "vault_new_password": "Choose the new master password",
        "vault_policy_note": "Auto-lock drops the key from memory after inactivity. Auto-destroy removes the key material after the lockout limit.",
        "vault_policy_save": "Apply",
        "vault_policy_saved": "Policy applied ({count} change(s)).",
        "vault_policy_autolock": "Auto-lock (seconds)",
        "vault_policy_attempts": "Failed attempts allowed",
        "vault_policy_lockout": "Lockout (seconds)",
        "vault_policy_2fa": "Require 2FA",
        "vault_policy_double": "Double-layer encryption",
        "vault_policy_otk": "One-time keys",
        "vault_policy_destroy": "Auto-destroy after lockout",
        "vault_policy_intrusion": "Intrusion detection",
        "vault_policy_manipulation": "Manipulation detection",
        "vault_hidden_on": "Hidden mode on",
        "vault_hidden_off": "Hidden mode off",
        "vault_readonly_on": "Read-only lock on",
        "vault_readonly_off": "Read-only lock off",
        "vault_readonly_blocked": "The read-only lock is on. Turn it off first.",
        "vault_item_readonly": "That resource is marked read-only.",
        "vault_added": "Added {name}.",
        "vault_updated": "Updated {name}.",
        "vault_moved": "Moved {count} resource(s).",
        "vault_delete_confirm": "Delete {count} resource(s)? This cannot be undone.\n\n{names}",
        "vault_deleted": "Deleted {count} resource(s).",
        "vault_flags_set": "{flag}: {state} on {count} resource(s)",
        "vault_analysis": "Analysis",
        "vault_analysis_structure": "Document structure",
        "vault_analysis_integrity": "Script integrity",
        "vault_analysis_risk": "Security risk",
        "vault_analysis_changes": "Change history",
        "vault_report_security": "Security report",
        "vault_report_integrity": "Integrity report",
        "vault_report_access": "Access report",
        "vault_report_changes": "Change report",
        "vault_report_risk": "Risk report",
        "vault_report_saved": "Report saved: {path}",
        "vault_int_editor": "Open in Editor",
        "vault_int_debugger": "Open in Debugger",
        "vault_int_runner": "Run in Sandbox",
        "vault_int_audit": "Open in Audit",
        "vault_int_reports": "Generate a report",
        "vault_int_diff": "Compare in Diff",
        "vault_int_snapshots": "Take a snapshot",
        "vault_sent": "Sent {name} to {target}.",
        "vault_target_unavailable": "{target} is not available.",
        "vault_import_filter": "Vault exports and text (*.json *.txt *.md *.py *.cfg *.ini);;All files (*.*)",
        "vault_imported": "Imported {count} resource(s), encrypted.",
        "vault_export_vault": "Export the vault (re-authenticate)",
        "vault_export_section": "Export the open section (inside the vault)",
        "vault_export_snapshots": "Export the snapshots",
        "vault_export_config": "Export the vault configuration",
        "vault_export_confirm": "Confirm the master password to export outside the vault",
        "vault_exported": "Exported {count} resource(s) to {path}.",
        "vault_snapshots_exported": "Exported {count} snapshot(s).",
        "vault_config_exported": "Configuration written to {path}.",
        "vault_snapshot": "Snapshot the vault",
        "vault_snapshot_take": "Take snapshot",
        "vault_snapshot_note": "Every section is included by default. Unchecking any of them requires the master password again.",
        "vault_snapshot_confirm": "A partial snapshot: confirm the master password",
        "vault_snapshot_empty": "Select at least one section.",
        "vault_snapshot_taken": "Snapshot written: {path}",
        "vault_restore_refused": "That snapshot cannot be restored.",
        "vault_restored": "Restored {count} resource(s).",
        "vault_folder_opened": "Opened {path}.",
        "editor_codegen_menu": "Code generation...",
        "editor_profiles_menu": "Script profiles...",
        "profile_cli": "Command-line script (CLI)",
        "profile_service": "Service script",
        "profile_automation": "Automation script",
        "profile_test": "Test script",
        "profile_utility": "Utility script",
        "profile_module": "Module script",
        "profile_api": "API script",
        "profiles_description": "A generated script.",
        "profiles_applied": "{name}: {summary}",
        "profiles_failed": "Generation failed: {error}",
        "profiles_unknown": "Unknown profile.",
        "codegen_kind_config": "Config -> initialisation",
        "codegen_kind_metadata": "Metadata -> project module",
        "codegen_kind_json": "JSON/YAML -> data classes",
        "codegen_kind_api": "API -> client",
        "codegen_kind_modules": "Modules -> package",
        "codegen_kind_project": "Project -> scaffold",
        "codegen_source_file": "Choose a source file",
        "codegen_source_filter": "Definitions (*.json *.yaml *.yml *.ini *.env *.txt);;All files (*.*)",
        "codegen_paste": "Paste the input for {name}",
        "codegen_use_buffer": "Use the editor buffer",
        "codegen_project_source": "Take the project from a directory on disk?",
        "codegen_file": "File",
        "codegen_save_all": "Save the files...",
        "codegen_insert_first": "Insert the first file",
        "codegen_saved": "saved {count} file(s) in {root}",
        "codegen_done": "generated {name} ({count} file(s)): {notes}",
        "codegen_failed": "Generation failed: {error}",
        "codegen_unknown": "Unknown generator.",
        "editor_coderules_menu": "Code rules...",
        "coderules_fix_imports": "Add missing / remove unused imports",
        "coderules_format": "Normalise formatting",
        "coderules_indentation": "Fix indentation",
        "coderules_add_return": "Add missing return",
        "coderules_add_break": "Add missing break",
        "coderules_add_except": "Add missing except",
        "coderules_add_docstring": "Add missing docstrings",
        "coderules_add_types": "Add missing type hints",
        "coderules_add_config": "Add missing config sections",
        "coderules_fix_style": "Fix remaining style issues",
        "coderules_fix_all": "Run every rule",
        "coderules_lint": "Only report (change nothing)",
        "coderules_report": "Rules report",
        "coderules_applied": "{name}: {notes}",
        "coderules_nothing": "{name}: nothing to fix",
        "coderules_all_applied": "applied {count} rule(s): {names}",
        "coderules_none_applied": "no rule changed the buffer",
        "coderules_clean": "every rule satisfied",
        "coderules_unknown": "Unknown rule.",
        "template_cat_function": "Function templates",
        "template_cat_class": "Class templates",
        "template_cat_module": "Module templates",
        "template_cat_project": "Project templates",
        "template_cat_config": "Configuration templates",
        "template_cat_test": "Test templates",
        "template_cat_documentation": "Documentation templates",
        "template_cat_starter": "Startup script templates",
        "template_cat_block": "Repeatable blocks",
        "editor_template_inserted": "inserted template: {name}",
        "editor_template_unknown": "Unknown template.",
        "editor_workspace_menu": "Workspace & security...",
        "editor_workspace_tree": "Project tree",
        "editor_workspace_open": "Open file",
        "editor_workspace_recent_files": "Recent files",
        "editor_workspace_recent_projects": "Recent projects",
        "editor_workspace_snapshot": "Create project snapshot",
        "editor_workspace_file_history": "File change history",
        "editor_workspace_project_history": "Project change history",
        "editor_workspace_path": "Path",
        "editor_workspace_choose": "Choose:",
        "editor_workspace_empty": "No recent entries.",
        "editor_workspace_snapshot_done": "snapshot saved: {path}",
        "editor_security_sandbox": "Run in sandbox",
        "editor_security_readonly": "Toggle read-only lock",
        "editor_security_hash": "Show hash signature",
        "editor_security_verify": "Verify unauthorized changes",
        "editor_security_access": "File access audit",
        "editor_security_sandbox_info": "Sandbox preview is static; execution still uses the configured runner.",
        "editor_security_readonly_state": "read-only: {state}",
        "editor_security_hash_value": "SHA-256: {hash}",
        "editor_security_verify_state": "integrity: {state}",
        "search_options": "Options",
        "search_scope": "Scope:",
        "search_in_all": "All fields",
        "search_ignore_case": "Ignore case",
        "search_regex": "Regex",
        "search_whole_word": "Whole word",
        "search_favorites_only": "Favorites only",
        "search_sort": "Sort:",
        "sort_relevance": "Relevance",
        "sort_name": "Name",
        "sort_date": "Newest first",
        "sort_category": "Category",
        "search_clear": "Clear",
        "search_found": "Found {count} matching script(s).",
        "search_bad_regex": "Invalid regular expression",
        "search_score": "Relevance score: {score}",
        "search_copy_results": "Copy result list",
        "search_export": "Export results to JSON",
        'search_refresh': 'Refresh',
        'search_count': '{count} script(s) searchable',
        'search_ready': 'Pick a kind and run a search.',
        'search_jumped': 'opened {doc} at line {line}',
        'search_detail': 'Detail',
        'search_nothing_export': 'run a search first',
        'search_scope_database': 'Whole database',
        'search_scope_selection': 'Selected script',
        'search_scope_editor': 'Open in editor',
        'search_scope_set': 'scope: {name}',
        "todo_calendar": "Calendar",
        "todo_tasks_tab": "Tasks",
        "todo_editor_tab": "Editor",
        "todo_plan_tab": "Plan",
        "todo_notes_tab": "Notes",
        "todo_expenses_tab": "Expenses",
        "todo_calculator": "Calculator",
        "todo_close_window": "Close window",
        "todo_window_hint": "This is a separate window: drag it anywhere, or press Ctrl+T to hide it.",
        "todo_notes_search": "Search notes...",
        "todo_notes_new": "+ Note",
        "todo_notes_delete": "Delete",
        "todo_notes_title": "Note title",
        "todo_notes_body": "Write a note...",
        "todo_notes_save": "Save note",
        "todo_notes_untitled": "Untitled",
        "todo_expense_description": "Description",
        "todo_expense_add": "+ Add",
        "todo_expense_remove": "Remove selected",
        "about": "About",
        "about_title": "About ScriptDB",
        "about_version": "2.7.0",
        "about_author": "Author",
        "about_company": "Company",
        "about_email": "E-mail",
        "favorite_col": "Favorite",
        "menu_add_script": "Add script",
        "menu_separators": "Separators",
        "menu_columns": "Columns",
        "sep_add": "Add separator...",
        "sep_add_top": "Add at the top",
        "sep_add_bottom": "Add at the bottom",
        "sep_add_before_first": "Add before first script",
        "sep_edit": "Edit separator",
        "sep_rename": "Rename separator...",
        "sep_remove": "Remove separator",
        "sep_prompt": "Separator label (leave empty for a plain line):",
        "sep_unnamed": "(unnamed separator)",
        "sep_hide_all": "Hide separators",
        "sep_clear_all": "Remove all separators",
        "sep_confirm_clear": "Remove all {count} separator(s)?",
        "sep_default_label": "Default label for new separators...",
        "sep_move_up": "Move separator up",
        "sep_move_down": "Move separator down",
        "configure_interpreters": "Configure Interpreters...",
        "interpreters_title": "Configure Script Interpreters",
        "interpreters_hint": "Set custom paths for script interpreters. Leave empty to use system default.",
        "interpreter_path": "Interpreter path",
        "browse": "Browse...",
        "interpreters_saved": "Interpreter configuration saved.",
        "sep_hidden_note": "Separators are hidden. Use the empty-area menu to show them.",
        "col_favorite": "Show favorite column",
        "col_row_numbers": "Show row numbers",
        "col_reset": "Reset columns",
        "group_by_category": "Group by category",
        "zebra_stripes": "Alternating row colors",
        "bold_separators": "Bold separator labels",
        "list_expand_all": "Expand all",
        "list_collapse_all": "Collapse all",
        "list_reset_view": "Reset list view",
        "tree_view": "Tree view (grouped)",
        "list_view": "List view (flat)",
        "diff": "Diff",
        "about_github": "GitHub",
        "diff_identical": "(the two sides are identical)",
        "diff_open_file": "Open file",
        "diff_left_dir": "Select the left folder",
        "diff_right_dir": "Select the right folder",
        "diff_empty_stdin": "No data on stdin.",
        "diff_recent": "Recent comparisons",
        "diff_need_two_files": "Drop two files to compare them.",
        "diff_changes": "Changes",
        "diff_show": "Show:",
        "diff_filter_added": "Added",
        "diff_filter_removed": "Removed",
        "diff_filter_changed": "Changed",
        "diff_filter_symbols": "In functions",
        "diff_collapse": "Collapse unchanged",
        "diff_low_graphics": "Low graphics",
        "diff_search": "Search in diff...",
        "diff_prev_hit": "Prev hit",
        "diff_next_hit": "Next hit",
        "diff_view_analysis": "Analysis",
        "diff_stats_title": "Diff statistics",
        "diff_export": "Export report",
        "diff_diagnostics": "Diagnostics (.json)",
        "diff_report_name": "diff_report",
        "diff_export_menu": "Export...",
        "diff_export_patch": "Patch (.patch)",
        "diff_export_json": "JSON (.json)",
        "diff_export_txt": "TXT report (.txt)",
        "diff_new_session": "New session",
        "diff_close_session": "Close session",
        "diff_modes_menu": "\U0001f9ea Modes...",
        "diff_mode_merge3": "Three-way diff (merge)",
        "diff_mode_merge4": "Four-way diff (advanced merge)",
        "diff_mode_live": "Live diff (file vs in-memory)",
        "diff_mode_sysrest": "System configuration diff",
        "diff_mode_env": "Environment diff (env1 vs env2)",
        "diff_mode_hex": "Binary diff (hex view)",
        "diff_merge_base": "Base (ancestor):",
        "diff_merge_left": "Left:",
        "diff_merge_right": "Right:",
        "diff_merge_ours": "Ours:",
        "diff_merge_theirs": "Theirs:",
        "diff_merge_result": "Merged result:",
        "diff_merge_run": "Merge",
        "diff_merge_conflict": "{count} conflict(s) need a decision.",
        "diff_merge_clean": "Merge is clean (no conflicts).",
        "diff_merge_take_left": "Take left",
        "diff_merge_take_right": "Take right",
        "diff_merge_take_both": "Take both",
        "diff_live_refresh": "Refresh",
        "diff_live_auto": "Auto (1s)",
        "diff_live_watch": "Watch file",
        "diff_live_no_file": "No file loaded for the live diff.",
        "diff_live_memory": "In-memory buffer (left)",
        "diff_live_file": "File: {name}",
        "diff_sysrest_run": "Collect system configuration",
        "diff_sysrest_hint": "Collects OS, environment, hardware and network settings for comparison.",
        "diff_env_run": "Collect environment variables",
        "diff_env_hint": "Snapshots the current environment. Compare env1 (left) with env2 (right).",
        "diff_hex_offset": "Offset",
        "diff_hex_bytes": "Bytes",
        "diff_hex_ascii": "ASCII",
        "diff_hex_side": "Side",
        "diff_hex_hint": "Byte-level diff of the two files with a hex + ASCII view.",
        "diff_hex_no_file": "No file loaded for the hex diff.",
                "todo": "ToDo",
        "todo_title": "TO DO",
        "todo_subtitle": "Tasks, calendar and reminders",
        "todo_unavailable": "The ToDo tab is not available",
        "todo_badge_hint": "{due} due today, {late} late (Ctrl+T)",
        "todo_search": "Search tasks...",
        "todo_view_today": "Today",
        "todo_view_day": "Day",
        "todo_view_week": "Week",
        "todo_view_month": "Month",
        "todo_view_year": "Year",
        "todo_prev": "Previous",
        "todo_next": "Next",
        "todo_today": "Today",
        "todo_add_task": "+ Task",
        "todo_tools": "Tools",
        "todo_new": "New task",
        "todo_new_on_day": "New task on the shown day...",
        "todo_templates": "From a template",
        "todo_add": "Add",
        "todo_save": "Save task",
        "todo_duplicate": "Duplicate",
        "todo_delete": "Delete",
        "todo_close": "Close",
        "todo_mark_done": "Mark as done",
        "todo_done": "Done",
        "todo_reopen": "Reopen",
        "todo_edit": "Edit",
        "todo_list": "Tasks",
        "todo_untitled": "untitled",
        "todo_warning": "Warning",
        "todo_need_title": "A task needs a title.",
        "todo_editing": "Editing: {title}",
        "todo_free_day": "nothing scheduled",
        "todo_now_line": "now {when}",
        "todo_untimed": "No set time",
        "todo_nothing_today": "Nothing is due today.",
        "todo_section_late": "Late",
        "todo_section_today": "Due today",
        "todo_section_doing": "In progress",
        "todo_section_soon": "Next seven days",
        "todo_field_title": "Title",
        "todo_field_project": "Project",
        "todo_field_kind": "Kind",
        "todo_field_due": "Due",
        "todo_field_start": "Start",
        "todo_field_remind": "Remind",
        "todo_field_repeat": "Repeat",
        "todo_field_tags": "Tags",
        "todo_field_colour": "Colour",
        "todo_field_notes": "Notes",
        "todo_field_links": "Links",
        "todo_field_checklist": "Checklist",
        "todo_tags_hint": "bug, ui, urgent",
        "todo_link_hint": "path, URL, script name or ticket",
        "todo_check_hint": "one step",
        "todo_quick_due": "Quick due time",
        "todo_quick_today": "Today 17:00",
        "todo_quick_tomorrow": "Tomorrow 09:00",
        "todo_quick_week": "In a week 09:00",
        "todo_quick_monday": "Next Monday 09:00",
        "todo_auto": "automatic",
        "todo_every": "every",
        "todo_unit_day": "day(s)",
        "todo_unit_week": "week(s)",
        "todo_unit_month": "month(s)",
        "todo_unit_year": "year(s)",
        "todo_remind_at": "at the time",
        "todo_remind_min": "{count} min before",
        "todo_remind_hour": "{count} h before",
        "todo_remind_day": "{count} d before",
        "todo_kind_task": "Task",
        "todo_kind_code": "Code",
        "todo_kind_bug": "Bug",
        "todo_kind_review": "Review",
        "todo_kind_research": "Research",
        "todo_kind_meeting": "Meeting",
        "todo_kind_release": "Release",
        "todo_kind_admin": "Admin",
        "todo_kind_idea": "Idea",
        "todo_priority_critical": "Critical",
        "todo_priority_high": "High",
        "todo_priority_normal": "Normal",
        "todo_priority_low": "Low",
        "todo_status_todo": "To do",
        "todo_status_doing": "Doing",
        "todo_status_blocked": "Blocked",
        "todo_status_done": "Done",
        "todo_status_cancelled": "Cancelled",
        "todo_repeat_none": "Never",
        "todo_repeat_daily": "Every day",
        "todo_repeat_weekdays": "Every weekday",
        "todo_repeat_weekly": "Every week",
        "todo_repeat_biweekly": "Every two weeks",
        "todo_repeat_monthly": "Every month",
        "todo_repeat_quarterly": "Every quarter",
        "todo_repeat_yearly": "Every year",
        "todo_repeat_custom": "Custom...",
        "todo_filter_all": "All open",
        "todo_filter_status_doing": "In progress",
        "todo_filter_status_blocked": "Blocked",
        "todo_filter_priority_critical": "Critical",
        "todo_filter_priority_high": "High",
        "todo_filter_kind_bug": "Bugs",
        "todo_filter_kind_code": "Code",
        "todo_filter_kind_review": "Reviews",
        "todo_filter_kind_meeting": "Meetings",
        "todo_filter_done": "Completed",
        "todo_all_projects": "All projects",
        "todo_all_tags": "All tags",
        "todo_set_status": "Set status",
        "todo_set_priority": "Set priority",
        "todo_mark_todo": "Mark as to do",
        "todo_snooze": "Snooze",
        "todo_snooze_for": "Snooze {what}",
        "todo_snooze_10": "Snooze 10 min",
        "todo_start_working": "Start working on it",
        "todo_move": "Move to day",
        "todo_move_0": "Today",
        "todo_move_1": "Tomorrow",
        "todo_move_7": "+1 week",
        "todo_move_30": "+1 month",
        "todo_copy": "Copy as text",
        "todo_selected": "{count} tasks selected",
        "todo_send_docs": "Send to DOCs",
        "todo_send_editor": "Open in the Editor",
        "todo_saved": "saved",
        "todo_completed": "completed",
        "todo_reopened": "reopened",
        "todo_completed_n": "completed {count} task(s)",
        "todo_deleted_n": "deleted {count}",
        "todo_deleted": "deleted",
        "todo_delete_ask": "Delete this task?",
        "todo_snoozed": "snoozed until {when}",
        "todo_moved": "moved to {when}",
        "todo_copied": "copied {count} task(s)",
        "todo_clear_done": "Clear completed tasks",
        "todo_clear_done_ask": "Remove {count} completed task(s)?",
        "todo_nothing_completed": "no completed tasks to clear",
        "todo_cleared": "removed {count} task(s)",
        "todo_forget": "no completed tasks",
        "todo_clear_all": "Remove all tasks",
        "todo_clear_all_ask": "Remove all {count} task(s)? This cannot be undone.",
        "todo_nothing_to_clear": "no tasks to remove",
        "todo_holidays": "Holidays",
        "todo_country_pl": "Poland",
        "todo_country_uk": "United Kingdom",
        "todo_show_holidays": "Show holidays in the grid",
        "todo_display": "Display",
        "todo_show_weekends": "Show weekends",
        "todo_show_week_numbers": "Week numbers",
        "todo_show_done": "Show completed tasks",
        "todo_start_monday": "Week starts on Monday",
        "todo_work_hours": "Working hours",
        "todo_work_start": "Day starts at",
        "todo_work_end": "Day ends at",
        "todo_holiday_in": "{days} day(s) to {name}",
        "todo_review": "Review and statistics...",
        "todo_review_head": "ToDo review",
        "todo_review_by": "Group",
        "todo_review_count": "Count",
        "todo_review_project": "By project",
        "todo_review_kind": "By kind",
        "todo_review_priority": "By priority",
        "todo_review_day": "Day",
        "todo_review_load": "Load",
        "todo_stat_total": "Tasks",
        "todo_stat_open": "Open",
        "todo_stat_done": "Done",
        "todo_stat_late": "Late",
        "todo_stat_week": "Done this week",
        "todo_worklog": "Recently completed",
        "todo_no_log": "nothing completed yet",
        "todo_organise": "Sort and organise...",
        "todo_org_overdue": "Push all overdue tasks to today",
        "todo_org_untimed": "Give every task without a time a due hour",
        "todo_org_project": "Assign the current project to every untagged task",
        "todo_org_dedupe": "Remove duplicate tasks (same title and day)",
        "todo_org_empty": "Remove tasks with no title",
        "todo_org_archive": "Archive every completed task",
        "todo_org_note": "Every action here is applied to the whole list.",
        "todo_org_m": "moved {count} task(s)",
        "todo_org_touched": "changed {count}",
        "todo_org_removed": "removed {count}",
        "todo_org_need_project": "pick a project filter first",
        "todo_export": "Export",
        "todo_export_ics": "Calendar (.ics)",
        "todo_export_md": "Markdown report (.md)",
        "todo_export_json": "All tasks (.json)",
        "todo_export_csv": "Tasks as CSV (.csv)",
        "todo_import": "Import",
        "todo_import_ics": "Calendar (.ics)",
        "todo_imported": "imported {count}",
        "todo_exported": "written: {path}",
        "todo_report_title": "ToDo report",
        "todo_report_generated": "Generated {when}",
        "todo_report_summary": "{count} task(s)",
        "todo_notifications": "Reminders and notifications",
        "todo_notify_on": "Show reminder popups",
        "todo_notify_test": "Show a test popup",
        "todo_notify_history": "Notification history...",
        "todo_notify_when": "When",
        "todo_notify_kind": "Kind",
        "todo_notify_what": "What",
        "todo_notify_enabled": "reminder popups: {state}",
        "todo_clear_log": "Clear",
        "todo_on": "on",
        "todo_off": "off",
        "todo_reminder_title": "Reminder: {title}",
        "todo_test_title": "This is a test reminder",
        "todo_test_body": "Reminders look like this and appear over every tab.",
        "todo_sum_open": "{count} open",
        "todo_sum_done": "{count} done",
        "todo_sum_late": "{count} late",
        "todo_sum_estimate": "{count} min estimated",
        "todo_from_script": "Work on {name}",
        "todo_scratch": "a ToDo task",
        "todo_docs_category": "ToDo",
        "todo_needs_docs": "the DOCs tab has no open repository",
        "todo_sent_docs": "stored in DOCs",
        "todo_needs_editor": "the Editor tab is unavailable",
        "todo_link_unresolved": "cannot open: {what}",
        "todo_tpl_code_feature": "Implement a feature",
        "todo_tpl_code_bug": "Fix a bug",
        "todo_tpl_code_review": "Review a change",
        "todo_tpl_code_refactor": "Refactor",
        "todo_tpl_code_test": "Write tests",
        "todo_tpl_release": "Cut a release",
        "todo_tpl_deploy": "Deploy",
        "todo_tpl_standup": "Daily standup",
        "todo_tpl_review_week": "Weekly review",
        "todo_tpl_research": "Research",
        "todo_tpl_idea": "Idea",
        "todo_calendar": "Calendar",
        "todo_tasks": "Tasks",
        "todo_editor": "Editor",
        "todo_plan": "Plan",
        "todo_notes": "Notes",
        "todo_expenses": "Expenses",
        "todo_calculator": "Calculator",
        "todo_close_window": "Close window",
        "todo_window_hint": "This is a separate window: drag it anywhere, or press Ctrl+T to hide it.",
        "todo_notes_search": "Search notes...",
        "todo_notes_new": "+ Note",
        "todo_notes_delete": "Delete",
        "todo_notes_title": "Note title",
        "todo_notes_body": "Write a note...",
        "todo_notes_save": "Save note",
        "todo_notes_untitled": "Untitled",
        "todo_expense_description": "Description",
        "todo_expense_add": "+ Add",
        "todo_expense_remove": "Remove selected",
        "about_text": (
            """Author:  Sebastian Januchowski\n"""
            """Company: polsoft.ITS\u2122\n"""
            """E-mail:  polsoft.its@mail.com\n"""
            """GitHub:  https://github.com/polsoft-it\n"""
            """ScriptDB {version}\n\n"""
            """A local database of code snippets with a syntax-highlighting editor, """
            """script runner, module inspector, SQL browser, metadata tools and a """
            """three-way comparison view.\n\n"""
            """Scripts: {scripts}\n"""
            """Saved connections: {connections}\n\n"""
            """Script database:\n{data}\n\n"""
            """Connections file:\n{conn}\n\n"""
            """Built with Python and PyQt5."""
        ),
    },
    "pl": {
        "title": "ScriptDB",
        'search_case': 'Wielkosc liter',
        'search_cleared': 'wyniki wyszukiwania wyczyszczone',
        'search_col_detail': 'Szczegoly',
        'search_col_doc': 'Skrypt',
        'search_col_kind': 'Rodzaj',
        'search_col_line': 'Linia',
        'search_col_text': 'Dopasowanie',
        'search_database': 'Cala baza',
        'search_database_hint': 'Uruchom to wyszukiwanie na wszystkich skryptach',
        'search_hint': 'Szukaj w kodzie...',
        'search_include_methods': 'Wywolania metod',
        'search_kind': 'Szukaj:',
        'search_kind_multiline': 'Wielolinijkowe',
        'search_kind_regex': 'Regex',
        'search_kind_text': 'Tekst',
        'search_mark_definitions': 'Zaznacz definicje',
        'search_optional': '(opcjonalny filtr)',
        'search_run': 'Szukaj',
        "scripts": "Skrypty",
        "editor": "Edytor",
        "editor_load": "Wczytaj wybrany",
        "editor_export": "Eksportuj plik",
        "editor_no_script": "Nie wczytano skryptu",
        "editor_find_hint": "Szukaj...",
        "editor_replace_hint": "ZamieĹ„ na...",
        "editor_find": "Szukaj",
        "editor_replace_menu": "ZamieĹ„...",
        "editor_replace": "ZamieĹ„",
        'nav_go': 'Navigate:',
        'nav_go_menu': 'Go to...',
        'nav_go_previous': 'Go to previous',
        'nav_go_definition': 'Go to definition',
        'nav_go_reference': 'Go to reference',
        'nav_go_import': 'Go to import',
        'nav_go_class': 'Go to class',
        'nav_go_function': 'Go to function',
        'nav_go_variable': 'Go to variable',
        'nav_go_error': 'Go to error',
        'nav_go_warning': 'Go to warning',
        'nav_go_section': 'Go to section',
        'nav_go_enclosing': 'Enclosing section',
        'nav_symbol_hint': 'Symbol to jump to...',
        'nav_symbol_jump': 'Jump',
        'nav_problem': 'Problem',
        'nav_problem_next': 'Next problem',
        'nav_problem_prev': 'Previous problem',
        'nav_need_symbol': 'type a symbol name first',
        'nav_not_found': 'nothing to go to for: {kind}',
        'nav_no_problem': 'no problems found',
        'nav_status': '{kind}: {name} (line {line})',
        "editor_replace_all": "ZamieĹ„ wszystko",
        "editor_regex": "Regex",
        "editor_case": "WielkoĹ›Ä‡ liter",
        "editor_undo": "Cofnij",
        "editor_redo": "PonĂłw",
        "editor_theme": "Motyw:",
        "editor_theme_dark": "Ciemny",
        "editor_theme_light": "Jasny",
        "editor_theme_set": "motyw: {name}",
        "editor_low_graphics": "Tryb minimalny",
        "editor_low_graphics_on": "tryb minimalny: wĹ‚.",
        "editor_low_graphics_off": "tryb minimalny: wyĹ‚.",
        "editor_view_linenumbers": "Numery linii",
        "editor_view_currentline": "BieĹĽÄ…ca linia",
        "editor_view_brackets": "Pary nawiasĂłw",
        "editor_view_minimap": "Minimapa",
        "editor_view_autoclose": "Auto-zamykanie",
        "editor_view_smartindent": "Inteligentne wciÄ™cia",
        "editor_view_suggestions": "Podpowiedzi",
        "editor_language": "JÄ™zyk:",
        "editor_language_set": "jÄ™zyk: {name}",
        "editor_language_source": "(moduĹ‚ {module})",
        'nav_go': 'Nawigacja:',
        'nav_go_menu': 'IdĹş do...',
        'nav_go_previous': 'IdĹş do poprzedniego',
        'nav_go_definition': 'IdĹş do definicji',
        'nav_go_reference': 'IdĹş do odwoĹ‚ania',
        'nav_go_import': 'IdĹş do importu',
        'nav_go_class': 'IdĹş do klasy',
        'nav_go_function': 'IdĹş do funkcji',
        'nav_go_variable': 'IdĹş do zmiennej',
        'nav_go_error': 'IdĹş do bĹ‚Ä™du',
        'nav_go_warning': 'IdĹş do ostrzeĹĽenia',
        'nav_go_section': 'IdĹş do sekcji',
        'nav_go_enclosing': 'OtaczajÄ…ca sekcja',
        'nav_symbol_hint': 'Symbol, do ktĂłrego skoczyÄ‡...',
        'nav_symbol_jump': 'Skocz',
        'nav_problem': 'Problem',
        'nav_problem_next': 'NastÄ™pny problem',
        'nav_problem_prev': 'Poprzedni problem',
        'nav_need_symbol': 'najpierw wpisz nazwÄ™ symbolu',
        'nav_not_found': 'brak celu dla: {kind}',
        'nav_no_problem': 'nie znaleziono problemĂłw',
        'nav_status': '{kind}: {name} (linia {line})',
        "search_kind_multifile": "Wiele plikĂłw",
        "search_kind_string": "CiÄ…g znakĂłw",
        "search_kind_files": "Katalog (rekursywnie)",
        "search_kind_workspace": "Projekt / workspace",
        "search_str_substring": "Fragment",
        "search_str_word": "CaĹ‚e sĹ‚owo",
        "search_str_line": "CaĹ‚a linia",
        "search_str_prefix": "Zaczyna siÄ™ od",
        "search_str_suffix": "KoĹ„czy siÄ™ na",
        "search_kind_semantic": "Semantyczne",
        "search_kind_imports": "Importy",
        "search_kind_blocks": "Bloki kodu",
        "search_kind_comments": "Komentarze",
        "search_kind_ast": "AST",
        "search_kind_calls": "WywoĹ‚ania",
        "search_kind_definitions": "Definicje",
        "search_kind_references": "Referencje",
        "search_kind_todo": "TODO / FIXME",
        "docs": "DOCs",
        "docs_new_db": "Nowa baza",
        "docs_open_db": "OtwĂłrz...",
        "docs_save": "Zapisz",
        "docs_save_db": "Zapisz bazÄ™ jako...",
        "reports": "Raporty",
        "reports_scan": "OdĹ›wieĹĽ",
        "reports_search": "Szukaj:",
        "reports_search_name": "Po nazwie...",
        "reports_search_content": "W treĹ›ci...",
        "reports_all_tags": "Wszystkie tagi",
        "reports_problems_any": "Wszystkie",
        "reports_problems_errors": "BĹ‚Ä™dy",
        "reports_problems_warnings": "OstrzeĹĽenia",
        "reports_problems_both": "BĹ‚Ä™dy + ostrzeĹĽenia",
        "reports_search_history": "Szukaj w historii wg dnia",
        "reports_since": "Od (RR-MM-DD)",
        "reports_clear_search": "WyczyĹ›Ä‡ wyszukiwanie",
        "reports_history_day": "Wygenerowano {day}",
        "reports_add_dir": "Dodaj folder...",
        "reports_open_file": "OtwĂłrz plik",
        "reports_to_docs": "Zapisz w DOCs",
        "reports_export": "Eksportuj indeks",
        "reports_delete": "UsuĹ„ raport",
        "reports_file_menu": "Plik...",
        "reports_delete_ask": "UsunÄ…Ä‡ {name}?",
        "reports_deleted": "usuniÄ™to raport",
        "reports_group": "Grupuj:",
        "reports_quick": "Szybkie:",
        "reports_filter_all": "Wszystkie",
        "reports_filter_24h": "Ostatnie 24h",
        "reports_filter_week": "Ostatni tydzieĹ„",
        "reports_filter_errors": "BĹ‚Ä™dy",
        "reports_filter_tests": "Testy",
        "reports_filter_diff": "Diff",
        "reports_filter_analysis": "Analiza kodu",
        "reports_filter_text": "Filtruj raporty...",
        "reports_all_types": "Wszystkie typy",
        "reports_group_project": "Wg projektu",
        "reports_group_module": "Wg moduĹ‚u",
        "reports_group_type": "Wg typu raportu",
        "reports_group_date": "Wg daty",
        "reports_group_tags": "Wg tagu",
        "reports_group_none": "PĹ‚aska lista",
        "reports_col_name": "Raport",
        "reports_col_type": "Typ",
        "reports_col_project": "Projekt",
        "reports_col_size": "Rozmiar",
        "reports_col_modified": "Zmieniono",
        "reports_count": "Raporty: {count}",
        "reports_scanned": "zeskanowano {count} raport(Ăłw)",
        "reports_meta": "{type} | projekt: {project} | tagi: {tags} | {size} | {modified}",
        "reports_no_docs": "Repozytorium DOCs niedostÄ™pne",
        "reports_stored_docs": "zapisano {name} w DOCs",
        "reports_exported": "zapisano indeks: {path}",
        "reports_preview": "PodglÄ…d:",
        "reports_preview_menu": "PodglÄ…d...",
        "reports_preview_run": "OdĹ›wieĹĽ podglÄ…d",
        "reports_preview_ascii_only": "Tylko ASCII",
        "reports_preview_export": "Eksportuj podglÄ…d",
        "reports_preview_exported": "zapisano podglÄ…d: {path}",
        "reports_preview_nothing_export": "nie ma czego eksportowaÄ‡",
        "reports_preview_ascii": "ASCII / zwykĹ‚y tekst",
        "reports_preview_structure": "Struktura (sekcje / bloki)",
        "reports_preview_metadata": "Metadane",
        "reports_preview_minimap": "Mini-mapa",
        "reports_analysis_menu": "Analiza...",
        "reports_an_errors": "Wykrywanie bĹ‚Ä™dĂłw",
        "reports_an_warnings": "Wykrywanie ostrzeĹĽeĹ„",
        "reports_an_duplicates": "Wykrywanie duplikatĂłw",
        "reports_an_changes": "Wykrywanie zmian",
        "reports_an_diff": "PorĂłwnywanie raportĂłw (diff)",
        "reports_an_trends": "Analiza trendĂłw",
        "reports_an_metrics": "Analiza metryk",
        "reports_open_in_menu": "OtwĂłrz w...",
        "reports_int_diff": "OtwĂłrz w Diff",
        "reports_int_search": "OtwĂłrz w Wyszukiwaniu",
        "reports_int_debugger": "OtwĂłrz w Debuggerze",
        "reports_int_runner": "OtwĂłrz w Runnerze",
        "reports_int_linter": "OtwĂłrz w Linterze",
        "reports_int_project": "PodĹ‚Ä…cz do moduĹ‚u projektu",
        "reports_generate": "Generuj z moduĹ‚u",
        "reports_generate_label": "ModuĹ‚:",
        "reports_op_open": "OtwĂłrz",
        "reports_op_delete": "UsuĹ„",
        "reports_op_important": "Oznacz jako waĹĽny",
        "reports_operations_menu": "Akcje raportu...",
        "reports_op_pin_project": "Przypnij do projektu",
        "reports_op_pin_favourite": "Przypnij do ulubionych",
        "reports_op_tag": "Dodaj tagi",
        "reports_op_compare": "PorĂłwnaj z innym raportem",
        "reports_op_versions": "PokaĹĽ rĂłĹĽnice miÄ™dzy wersjami",
        "reports_project_name": "Projekt:",
        "reports_tags_label": "Tagi (oddzielone przecinkiem):",
        "reports_pick": "Wybierz raport",
        "reports_pick_label": "Raport:",
        "reports_pick_other": "Z ktĂłrym raportem porĂłwnaÄ‡?",
        "reports_need_second": "wybierz drugi raport",
        "reports_premium_menu": "Premium...",
        "reports_prem_run_due": "Uruchom zalegĹ‚e raporty",
        "reports_prem_automate": "Automatyzacja wĹ‚Ä…cz/wyĹ‚Ä…cz",
        "reports_prem_due_done": "uruchomiono {count} raport(Ăłw) z harmonogramu",
        "reports_prem_auto": "Automatyczne generowanie raportĂłw",
        "reports_prem_scheduler": "Raporty cykliczne (scheduler)",
        "reports_prem_comparative": "Raporty porĂłwnawcze",
        "reports_prem_env": "Raporty Ĺ›rodowisk (env diff)",
        "reports_prem_security": "Raporty bezpieczeĹ„stwa (uprawnienia)",
        "reports_prem_change": "Automatyczny raport po zmianie pliku",
        "reports_prem_error": "Automatyczny raport po bĹ‚Ä™dzie",
        "reports_prem_deploy": "Automatyczny raport po deployu",
        "reports_prem_build": "Automatyczny raport po kompilacji",
        "reports_trig_build": "Po kompilacji",
        "reports_trig_run": "Po uruchomieniu skryptu",
        "reports_trig_error": "Po bĹ‚Ä™dzie",
        "reports_trig_deploy": "Po deployu",
        "reports_trig_change": "Po zmianie pliku",
        "reports_cad_hourly": "Co godzinÄ™",
        "reports_cad_daily": "Codziennie",
        "reports_cad_weekly": "Co tydzieĹ„",
        "reports_meta_name": "Nazwa",
        "reports_meta_type": "Typ",
        "reports_meta_project": "Projekt",
        "reports_meta_module": "ModuĹ‚",
        "reports_meta_script": "Skrypt",
        "reports_meta_process": "Proces",
        "reports_meta_source": "ĹąrĂłdĹ‚o",
        "reports_meta_path": "ĹšcieĹĽka",
        "reports_meta_generated": "Czas generowania",
        "reports_meta_size": "Rozmiar",
        "reports_meta_lines": "Linie",
        "reports_meta_words": "SĹ‚owa",
        "reports_meta_unique": "Unikalne sĹ‚owa",
        "reports_meta_chars": "Znaki",
        "reports_meta_errors": "BĹ‚Ä™dy",
        "reports_meta_warnings": "OstrzeĹĽenia",
        "reports_meta_tags": "Tagi",
        "reports_meta_hash": "Hash",
        "docs_preview_typed": "WedĹ‚ug typu dokumentu",
        "docs_group_text": "Dokumenty tekstowe",
        "docs_group_config": "Dokumenty konfiguracyjne",
        "docs_group_log": "Dokumenty logĂłw",
        "docs_group_report": "Dokumenty raportĂłw",
        "docs_group_audit": "Dokumenty audytowe",
        "docs_group_project": "Dokumenty projektowe",
        "docs_group_snapshot": "Dokumenty snapshotĂłw",
        "docs_group_script": "Dokumenty skryptĂłw",
        "docs_group_system": "Dokumenty systemowe",
        "docs_type_text": "Tekst",
        "docs_type_markdown": "Markdown",
        "docs_type_rst": "reStructuredText",
        "docs_type_json": "JSON",
        "docs_type_yaml": "YAML",
        "docs_type_ini": "INI / TOML",
        "docs_type_xml": "XML / HTML",
        "docs_type_log": "Log",
        "docs_type_syslog": "Log systemowy",
        "docs_type_report": "Raport",
        "docs_type_table": "CSV / tabela",
        "docs_type_spreadsheet": "Arkusz",
        "docs_type_audit": "ĹšcieĹĽka audytu",
        "docs_type_changelog": "Historia zmian",
        "docs_type_policy": "Polityka / zgodnoĹ›Ä‡",
        "docs_type_project": "Notatka projektowa",
        "docs_type_readme": "README",
        "docs_type_plan": "Plan / roadmapa",
        "docs_type_diagram": "Diagram (tekst)",
        "docs_type_snapshot": "Snapshot",
        "docs_type_backup": "Kopia zapasowa",
        "docs_type_script": "Skrypt / ĹşrĂłdĹ‚o",
        "docs_type_sql": "Skrypt SQL",
        "docs_type_notebook": "Notatnik",
        "docs_type_manifest": "Manifest",
        "docs_type_metadata": "Metadane",
        "docs_type_system": "Konfiguracja systemu",
        "docs_type_binary": "Binarne / inne",
        "docs_edit_page": "Edycja",
        "docs_preview_page": "PodglÄ…d",
        "docs_preview": "PodglÄ…d:",
        "docs_preview_run": "OdĹ›wieĹĽ podglÄ…d",
        "docs_preview_ascii_only": "Tylko ASCII",
        "docs_preview_export": "Eksportuj podglÄ…d",
        "docs_preview_ascii": "ASCII / zwykĹ‚y tekst",
        "docs_preview_structure": "Struktura (zarys)",
        "docs_preview_minimap": "Mini-mapa",
        "docs_preview_metadata": "Metadane",
        "docs_preview_links": "PowiÄ…zania",
        "docs_preview_versions": "Zmiany wzglÄ™dem migawki",
        "docs_preview_created": "Utworzono",
        "docs_preview_chars": "Znaki",
        "docs_preview_lines": "Linie",
        "docs_preview_words": "SĹ‚owa",
        "docs_preview_unique": "Unikalne sĹ‚owa",
        "docs_preview_checksum": "Suma kontrolna",
        "docs_preview_archived": "Zarchiwizowany",
        "docs_preview_encrypted": "Zaszyfrowany",
        "docs_preview_meta_keys": "Klucze meta",
        "docs_preview_related": "PowiÄ…zane dokumenty:",
        "docs_preview_none": "(brak)",
        "docs_preview_empty": "(brak treĹ›ci do podglÄ…du)",
        "docs_preview_no_snapshot": "(wybierz migawkÄ™ do porĂłwnania)",
        "docs_preview_identical": "brak zmian wzglÄ™dem migawki",
        "docs_preview_diff_summary": "+{added} / -{removed} linii, zmienione bloki: {changed}",
        "docs_preview_nothing_export": "brak danych do eksportu",
        "docs_password": "HasĹ‚o...",
        "docs_export": "Eksport...",
        "docs_add": "Dodaj dokument",
        "docs_import": "Importuj pliki...",
        "docs_save_doc": "Zapisz dokument",
        "docs_archive": "Archiwizuj",
        "docs_delete": "UsuĹ„",
        "docs_edit_external": "OtwĂłrz w edytorze",
        "docs_search_tab": "Szukaj w kodzie",
        "docs_index": "Przebuduj indeks",
        "docs_update_local": "Aktualizuj lokalne",
        "docs_fulltext": "Indeks peĹ‚notekstowy",
        "docs_fulltext_hint": "Szukaj w offline indeksie peĹ‚notekstowym zamiast zwykĹ‚ym filtrze",
        "docs_snapshot": "Migawka",
        "docs_snapshots": "Migawki",
        "docs_restore": "PrzywrĂłÄ‡",
        "docs_snapshot_default": "migawka",
        "docs_snapshot_taken": "zapisano migawkÄ™ ({count} dok.)",
        "docs_snapshot_restored": "przywrĂłcono {count} dokument(Ăłw)",
        "docs_no_snapshot": "nie wybrano migawki",
        "docs_reindexed": "zaindeksowano {count} dokument(Ăłw)",
        "docs_updated_stale": "zaktualizowano {count} dokument(Ăłw)",
        "docs_no_updates": "wszystko aktualne",
        "docs_engine_status": "indeks: {mode} - dokumenty: {count}",
        "docs_filter": "Filtruj dokumenty...",
        "docs_all_categories": "Wszystkie kategorie",
        "docs_show_archived": "PokaĹĽ zarchiwizowane",
        "docs_col_title": "TytuĹ‚",
        "docs_col_type": "Typ",
        "docs_col_category": "Kategoria",
        "docs_col_modified": "Zmieniono",
        "docs_field_title": "TytuĹ‚",
        "docs_field_type": "Typ",
        "docs_field_category": "Kategoria",
        "docs_field_tags": "Tagi",
        "docs_field_description": "Opis",
        "docs_untitled": "bez tytuĹ‚u",
        "docs_locked": "\U0001f512 zaszyfrowana",
        "docs_locked_body": "(zaszyfrowane - podaj hasĹ‚o)",
        "docs_count": "Dokumenty: {count}",
        "search_sem_any": "Dowolny symbol",
        "search_sem_function": "Funkcje",
        "search_sem_class": "Klasy",
        "search_sem_method": "Metody",
        "search_sem_variable": "Zmienne",
        "search_sem_constant": "StaĹ‚e",
        "search_sem_import": "Importy",
        "search_imp_any": "Dowolny import",
        "search_imp_from": "from ... import",
        "search_imp_include": "include",
        "search_imp_using": "using",
        "search_imp_require": "require",
        "search_blk_any": "Dowolny blok",
        "search_blk_if": "if",
        "search_blk_for": "for",
        "search_blk_while": "while",
        "search_blk_try": "try",
        "search_blk_catch": "catch / except",
        "search_blk_switch": "switch / match",
        "search_blk_function": "funkcja / def",
        "search_cmt_any": "Dowolny komentarz",
        "search_cmt_inline": "W linii",
        "search_cmt_block": "Blokowy",
        "search_cmt_docstring": "Docstring",
        "search_cmt_empty": "Zakomentowany kod",
        "search_ast_any": "Dowolny wÄ™zeĹ‚",
        "search_ast_function": "Funkcja",
        "search_ast_class": "Klasa",
        "search_ast_call": "WywoĹ‚anie",
        "search_ast_import": "Import",
        "search_ast_assign": "Przypisanie",
        "search_ast_name": "Nazwa",
        "search_ast_constant": "StaĹ‚a",
        "search_ast_loop": "PÄ™tla",
        "search_ast_conditional": "Warunek",
        "editor_sugg_function": "funkcja",
        "editor_sugg_class": "klasa",
        "editor_sugg_method": "metoda",
        "editor_sugg_variable": "zmienna",
        "editor_sugg_import": "import",
        "editor_sugg_parameter": "parametr",
        "editor_sugg_type": "typ",
        "editor_sugg_keyword": "sĹ‚owo kluczowe",
        "editor_sugg_context": "kontekst",
        "editor_status_ready": "DostÄ™pne skrypty: {count}. Wczytaj jeden, aby rozpoczÄ…Ä‡ edycjÄ™.",
        "editor_noselect": "Wybierz skrypt w zakĹ‚adce Skrypty.",
        "editor_loaded": "Wczytano {name}",
        "editor_modified": "zmodyfikowano",
        "editor_saved": "zapisano {name}",
        "editor_unchanged": "brak zmian do zapisania",
        "editor_exported": "wyeksportowano: {path}",
        "editor_not_found": "nie znaleziono",
        "editor_found_at": "trafienie na pozycji {pos}",
        "editor_replaced": "zamieniono 1 wystÄ…pienie",
        "editor_replaced_all": "zamieniono {count} wystÄ…pieĹ„",
        "search": "Szukaj",
        "apps": "Aplikacje",
        "apps_title": "APLIKACJE",
        "apps_subtitle": "Twoj osobisty launcher",
        "apps_search": "Szukaj aplikacji...",
        "apps_all": "Wszystkie aplikacje",
        "apps_new_group": "+ Grupa",
        "apps_add": "+ Dodaj aplikacje",
        "apps_open_folder": "Otworz folder",
        "apps_sort_name": "Nazwa",
        "apps_sort_path": "Sciezka",
        "apps_sort_group": "Grupa",
        "apps_sort_menu": "Sortuj wedlug",
        "apps_view_menu": "Widok",
        "apps_icon_small": "Male ikony",
        "apps_icon_medium": "Srednie ikony",
        "apps_icon_large": "Duze ikony",
        "apps_reveal_folder": "Otwórz lokalizację",
        "apps_copy_path": "Kopiuj ścieżkę",
        "apps_favorites": "Ulubione",
        "apps_recent": "Ostatnio używane",
        "apps_pin": "Dodaj do ulubionych",
        "apps_unpin": "Usuń z ulubionych",
        "apps_icon": "Ikona",
        "apps_last_used": "Ostatnio użyto: {when}",
        "apps_missing_path": "Nie znaleziono ścieżki",
        "apps_missing_path_body": "{path}\n\nZapisać mimo to?",
        "apps_grid_view": "Widok siatki",
        "apps_list_view": "Widok listy",
        "apps_status": "{count} aplikacji",
        "apps_launch": "Uruchom",
        "apps_edit": "Edytuj",
        "apps_remove": "Usun",
        "apps_add_title": "Dodaj aplikacje",
        "apps_browse": "Przegladaj...",
        "apps_name": "Nazwa",
        "apps_path": "Aplikacja",
        "apps_args": "Argumenty",
        "apps_group": "Grupa",
        "apps_no_group": "Bez grupy",
        "apps_group_name": "Nazwa grupy:",
        "apps_edit_group": "Edytuj grupe",
        "apps_folder_question": "Otwierac jako folder popup?",
        "apps_close": "Zamknij",
        "apps_error": "Blad launchera",
        "apps_add_multi": "+ Dodaj kilka...",
        "apps_sort_usage": "Najczesciej uzywane",
        "apps_sort_recent": "Ostatnio uzywane",
        "apps_sort_missing": "Brakujaca sciezka",
        "apps_sort_desc": "Malejaco",
        "apps_sort_dir": "Sortuj malejaco",
        "apps_kind": "Typ",
        "apps_kind_auto": "Automatycznie",
        "apps_kind_exec": "Plik wykonywalny",
        "apps_kind_shell": "Polecenie powloki",
        "apps_kind_url": "URL / link",
        "apps_kind_folder": "Folder",
        "apps_kind_doc": "Dokument",
        "apps_kind_script": "Skrypt ScriptDB",
        "apps_tags": "Tagi",
        "apps_tags_hint": "praca, narzedzia, gry",
        "apps_hotkey": "Skrot",
        "apps_hotkey_hint": "Uruchom te aplikacje z dowolnego miejsca tym skrotem",
        "apps_move_menu": "Wyslij do...",
        "apps_notes": "Notatki...",
        "apps_notes_hint": "Gdzie ja znalezc, licencja, konto, podpowiedz hasla...",
        "apps_use_as": "Uzyj jako...",
        "apps_use_diff_a": "Porownaj jej plik w zakladce Diff",
        "apps_use_diff_b": "Porownaj ja z plikiem...",
        "apps_use_docs": "Zapisz jej tresc w DOCs",
        "apps_use_search": "Szukaj jej nazwy w zakladce Szukaj",
        "apps_into_docs": "zaimportowano do DOCs: {count}",
        "apps_into_docs_failed": "DOCs jest niedostepne dla tego wpisu",
        "apps_tools_menu": "Launcher",
        "apps_export": "Eksportuj aplikacje...",
        "apps_import": "Importuj aplikacje...",
        "apps_exported": "wyeksportowano: {path}",
        "apps_imported": "zaimportowano {count} aplikacji",
        "apps_open_dir": "Otworz katalog launchera",
        "apps_check_paths": "Sprawdz wszystkie sciezki",
        "apps_all_ok": "wszystkie sciezki istnieja",
        "apps_danger_menu": "Usun...",
        "apps_clear_broken": "Usun niedzialajace wpisy",
        "apps_clear_broken_body": "Usunac {count} wpis(ow) z brakujaca sciezka?",
        "apps_cleared_broken": "usunieto {count} wpis(ow)",
        "apps_clear_all": "Usun wszystko",
        "apps_clear_all_body": "Usunac wszystkie aplikacje ({apps}) i grupy ({groups})?",
        "apps_cleared_all": "launcher wyczyszczony ({count})",
        "apps_review": "Przeglad wszystkich aplikacji",
        "apps_state": "Stan",
        "apps_state_ok": "OK",
        "apps_usage": "Uruchomienia",
        "apps_added_many": "dodano {count} aplikacji",
        "apps_status_missing": "brak sciezki w {count} wpis(ach)",
        "apps_none": "brak aplikacji do uruchomienia",
        "shopping": "Zakupy",
        "shopping_title": "ZAKUPY",
        "shopping_subtitle": "Lista zakupow z suma",
        "shopping_search": "Filtruj pozycje...",
        "shopping_name_hint": "Co kupic...",
        "shopping_add": "+ Dodaj pozycje",
        "shopping_col_done": "Kupione",
        "shopping_col_name": "Pozycja",
        "shopping_col_quantity": "Ilosc",
        "shopping_col_price": "Cena",
        "shopping_col_category": "Kategoria",
        "shopping_col_note": "Uwagi",
        "shopping_col_total": "Suma",
        "shopping_total": "Suma: {total} {currency}  |  do kupienia: {pending}, kupione: {done}",
        "shopping_count": "{count} pozycji",
        "shopping_toggle_done": "Odhacz / przywroc",
        "shopping_remove": "Usun",
        "shopping_rename": "Zmien nazwe pozycji",
        "shopping_rename_prompt": "Nowa nazwa:",
        "shopping_clear_done": "Usun kupione pozycje",
        "shopping_clear_done_ask": "Usunac {count} kupionych pozycji?",
        "shopping_clear_all": "Wyczysc liste",
        "shopping_clear_all_ask": "Usunac wszystkie pozycje ({count})?",
        "shopping_nothing_done": "nic jeszcze nie kupiono",
        "shopping_hide_done": "Ukryj kupione pozycje",
        "shopping_export": "Zapisz do pliku...",
        "shopping_export_txt": "Lista tekstowa (.txt)",
        "shopping_export_csv": "Arkusz (.csv)",
        "shopping_export_md": "Checklista Markdown (.md)",
        "shopping_export_json": "JSON (.json)",
        "shopping_export_pending": "Tylko to, co zostalo do kupienia",
        "shopping_import": "Wczytaj z pliku...",
        "shopping_imported": "zaimportowano {count}",
        "shopping_print": "Drukuj",
        "shopping_printed": "wyslano do drukarki",
        "shopping_open_dir": "Otworz katalog listy",
        "shopping_saved": "zapisano: {path}",
        "shopping_report_title": "Lista zakupow",
        "shopping_report_generated": "Wygenerowano {when}",
        "apps_empty_command": "puste polecenie",
        "apps_no_script": "do tego wpisu nie jest podlaczony zaden skrypt ScriptDB",
        "editor": "Edytor",
        "modules": "Modu\u0142y",
        "audit": "Audyt",
        "audit_empty": "Brak wpis\u00f3w audytu.",
        "audit_refresh": "Od\u015bwie\u017c",
        "audit_accept": "Oznacz jako sprawdzone",
        "debugger": "Debugger",
        "debugger_step_menu": "Krok...",
        "debugger_refresh": "Od\u015bwie\u017c",
        "debugger_clear": "Wyczy\u015b\u0107 sesj\u0119",
        "debugger_script": "Skrypt:",
        "debugger_line": "Linia:",
        "debugger_condition": "Warunek (opcjonalny)",
        "debugger_add_bp": "Dodaj punkt zatrzymania",
        "debugger_watch": "Obserwuj:",
        "debugger_watch_hint": "Wyra\u017cenie do obserwacji...",
        "debugger_add_watch": "Dodaj obserwacj\u0119",
        "debugger_del_watch": "Usu\u0144 obserwacj\u0119",
        "debugger_breakpoints": "Punkty zatrzymania",
        "debugger_stack": "Stos wywo\u0142a\u0144",
        "debugger_watches": "Obserwacje",
        "debugger_trace": "Dziennik \u015bledzenia:",
        "debugger_trace_hint": "Notatka...",
        "debugger_toggle_bp": "Prze\u0142\u0105cz",
        "debugger_remove_bp": "Usu\u0144 punkt zatrzymania",
        "debugger_col_script": "Skrypt",
        "debugger_col_line": "Linia",
        "debugger_col_kind": "Rodzaj",
        "debugger_col_condition": "Warunek",
        "debugger_bp_line": "linia",
        "debugger_bp_condition": "warunek",
        "debugger_bp_exception": "wyj\u0105tek",
        "debugger_bp_function": "funkcja",
        "debugger_language": "J\u0119zyk:",
        "debugger_language_set": "j\u0119zyk: {name}",
        "debugger_validate": "Sprawd\u017a",
        "debugger_validate_all": "Sprawd\u017a wszystkie",
        "debugger_validated_all": "sprawdzono {count} skrypt(\u00f3w): "
                                   "{failed} z problemem",
        "debugger_validation": "Walidacja",
        "debugger_valid_ok": "poprawny: {summary}",
        "debugger_valid_failed": "b\u0142\u0119dny: {error}",
        "debugger_overview": "Przegl\u0105d debugowania",
        "debugger_overview_title": "Przegl\u0105d debugowania",
        "debugger_overview_ready": "przegl\u0105d gotowy",
        "debugger_frames_guessed": "{count} ramka(i) rozpoznana(e) "
                                   "wzorcowo przez adapter {language}",
        "debugger_summary_col": "Podsumowanie",
        "debugger_error": "B\u0142\u0105d",
        "debugger_at": "W",
        "debugger_counts": "Zliczenia",
        "debugger_no_script": "(nie wybrano skryptu)",
        "debugger_flow": "Przep\u0142yw kodu:",
        "debugger_record": "Nagraj",
        "debugger_flow_all": "wszystkie rodzaje",
        "debugger_flow_empty": "(brak nagrania)",
        "debugger_flow_none": "(brak zdarze\u0144 tego rodzaju)",
        "debugger_flow_more": "... i {count} wi\u0119cej zdarze\u0144",
        "debugger_flow_recorded": "nagrano {count} zdarze\u0144 w {language}",
        "debugger_flow_saved": "zapisano raport przep\u0142ywu: {path}",
        "debugger_flow_step": "krok",
        "debugger_flow_block": "blok",
        "debugger_flow_call": "wywo\u0142anie",
        "debugger_flow_method": "metoda",
        "debugger_flow_import": "import",
        "debugger_flow_variable": "zmienna",
        "debugger_flow_exception": "wyj\u0105tek",
        "debugger_flow_error": "b\u0142\u0105d",
        "debugger_flow_warning": "ostrze\u017cenie",
        "debugger_flow_log": "log",
        "debugger_flow_view": "Widok:",
        "debugger_flow_save": "Zapisz widoki",
        "debugger_flow_views_saved": "zapisano widoki: {path}",
        "debugger_view_timeline": "Os czasu",
        "debugger_view_call_tree": "Drzewo wywo\u0142a\u0144",
        "debugger_view_call_stack": "Stos wywo\u0142a\u0144",
        "debugger_view_block_map": "Mapa blok\u00f3w",
        "debugger_view_function_map": "Mapa funkcji",
        "debugger_view_class_map": "Mapa klas",
        "debugger_view_module_map": "Mapa modu\u0142\u00f3w",
        "debugger_view_exception_map": "Mapa wyj\u0105tk\u00f3w",
        "sandbox": "Sandbox",
        "sandbox_refresh": "Od\u015bwie\u017c",
        "sandbox_ready": "odizolowany i gotowy",
        "sandbox_preset": "Preset:",
        "sandbox_timeout": "Limit czasu (s):",
        "sandbox_output_cap": "Limit wyj\u015bcia:",
        "sandbox_allow_network": "Zezw\u00f3l na sie\u0107",
        "sandbox_allow_writes": "Zezw\u00f3l na zapis",
        "sandbox_script": "Skrypt:",
        "sandbox_run": "Uruchom w sandboxie",
        "sandbox_scan": "Skanuj polityk\u0119",
        "sandbox_save_report": "Zapisz raport",
        "sandbox_rules": "Regu\u0142y polityki",
        "sandbox_findings": "Wyniki skanowania",
        "sandbox_col_rule": "Regu\u0142a",
        "sandbox_col_state": "Stan",
        "sandbox_col_detail": "Szczeg\u00f3\u0142y",
        "sandbox_preset_strict": "\u015acis\u0142y",
        "sandbox_preset_standard": "Standardowy",
        "sandbox_preset_permissive": "Zezwalaj\u0105cy",
        "sandbox_rule_network": "Dost\u0119p do sieci",
        "sandbox_rule_writes": "Zapis do plik\u00f3w",
        "sandbox_rule_subprocess": "Procesy potomne",
        "sandbox_rule_timeout": "Limit czasu",
        "sandbox_rule_output": "Limit wyj\u015bcia",
        "sandbox_rule_cwd": "Katalog roboczy",
        "sandbox_iso_group": "Izolacja procesu",
        "sandbox_iso_process": "Osobny proces",
        "sandbox_iso_environment": "Osobne \u015brodowisko",
        "sandbox_iso_path": "Osobny PATH",
        "sandbox_iso_cwd": "Osobny katalog roboczy",
        "sandbox_iso_runtime": "Osobny kontekst runtime",
        "sandbox_iso_terminal": "Dost\u0119p do terminala",
        "sandbox_fs_group": "Sandbox systemu plik\u00f3w",
        "sandbox_fs_whitelist": "Bia\u0142a lista \u015bcie\u017cek",
        "sandbox_fs_blacklist": "Czarna lista \u015bcie\u017cek",
        "sandbox_fs_read": "Odczyt poza sandboxem",
        "sandbox_fs_write": "Zapis poza sandboxem",
        "sandbox_fs_destructive": "Operacje destrukcyjne",
        "sandbox_fs_recursive": "Operacje rekurencyjne",
        "sandbox_fs_system": "Katalogi systemowe",
        "sandbox_fs_pseudo": "Wirtualny system plik\u00f3w",
        "sandbox_fs_shadow": "Kopia shadow",
        "sandbox_fs_snapshot": "Migawka FS",
        "sandbox_list_fs_whitelist": "Dozwolone \u015bcie\u017cki (jedna na lini\u0119)",
        "sandbox_net_group": "Sandbox sieci",
        "sandbox_net_network": "Dost\u0119p do sieci",
        "sandbox_net_whitelist": "Bia\u0142a lista host\u00f3w",
        "sandbox_net_allowed_ports": "Dozwolone porty",
        "sandbox_net_ports": "Zablokowane porty",
        "sandbox_net_protocols": "Zablokowane protoko\u0142y",
        "sandbox_net_raw": "Gniazda raw",
        "sandbox_net_icmp": "ICMP / ping",
        "sandbox_net_dns": "DNS",
        "sandbox_net_private": "Adresy prywatne",
        "sandbox_net_admin": "Porty administracyjne",
        "sandbox_net_http": "HTTP",
        "sandbox_net_https": "HTTPS",
        "sandbox_net_monitor": "Monitor ruchu",
        "sandbox_list_net_whitelist": "Dozwolone hosty (jeden na lini\u0119)",
        "sandbox_sys_group": "Filtr wywo\u0142a\u0144 systemowych",
        "sandbox_sys_exec": "Exec (execve)",
        "sandbox_sys_fork": "Fork",
        "sandbox_sys_spawn": "Spawn",
        "sandbox_sys_kill": "Kill",
        "sandbox_sys_mount": "Mount",
        "sandbox_sys_chmod": "Chmod",
        "sandbox_sys_chown": "Chown",
        "sandbox_sys_open": "Open",
        "sandbox_sys_write": "Write",
        "sandbox_sys_socket": "Socket",
        "sandbox_sys_bind": "Bind",
        "sandbox_sys_connect": "Connect",
        "audit_integrity": "Integralno\u015b\u0107 systemu",
        "audit_integrity_ok": "OK",
        "audit_integrity_warning": "Uwaga",
        "audit_integrity_critical": "Krytyczny",
        "audit_problems": "Wykryte problemy",
        "audit_changes": "Zmiany od ostatniego audytu",
        "audit_changes_detail": "+{added} / ~{modified} / -{removed}",
        "audit_risk": "Poziom ryzyka",
        "audit_risk_low": "Niski",
        "audit_risk_medium": "\u015aredni",
        "audit_risk_high": "Wysoki",
        "audit_feed_security": "Ostatnie zdarzenia bezpiecze\u0144stwa",
        "audit_feed_modules": "Ostatnie zmiany w modu\u0142ach",
        "audit_feed_config": "Ostatnie zmiany w konfiguracji",
        "audit_when": "Kiedy",
        "audit_where": "Skrypt",
        "audit_detail": "Szczeg\u00f3\u0142y",
        "audit_none": "brak",
        "audit_never": "nigdy",
        "audit_last": "Ostatni audyt: {when}",
        "audit_summary": "{total} skrypt(\u00f3w), {problems} problem(\u00f3w), ryzyko: {risk}",
        "audit_revisions": "{count} wersji",
        "audit_problem_empty": "Puste cia\u0142o skryptu",
        "audit_problem_nometa": "Brak metadanych",
        "audit_problem_notags": "Brak tag\u00f3w",
        "audit_problem_history": "Historia zosta\u0142a obci\u0119ta",
        "audit_modules_title": "Audyt modu\u0142\u00f3w",
        "audit_module": "Modu\u0142",
        "audit_integrity_col": "Integralno\u015b\u0107",
        "audit_signature": "Podpis",
        "audit_version": "Wersja",
        "audit_changed": "Zmiany",
        "audit_permissions": "Uprawnienia",
        "audit_hash_ok": "hash OK",
        "audit_hash_mismatch": "niezgodno\u015b\u0107",
        "audit_sig_valid": "wa\u017cny",
        "audit_sig_invalid": "niewa\u017cny",
        "audit_sig_none": "brak",
        "audit_yes": "tak",
        "audit_no": "nie",
        "audit_scripts_title": "Audyt skrypt\u00f3w",
        "audit_script": "Skrypt",
        "audit_change_col": "Zmiany",
        "audit_change_new": "nowy",
        "audit_change_modified": "zmodyfikowany",
        "audit_change_unchanged": "bez zmian",
        "audit_unused_funcs": "Nieu\u017cywane funkcje",
        "audit_unused_imports": "Nieu\u017cywane importy",
        "audit_suspicious": "Podejrzane konstrukcje",
        "audit_lint": "B\u0142\u0119dy lintera",
        "audit_permissions_title": "Analiza uprawnie\u0144",
        "audit_target": "Cel",
        "audit_files": "Pliki",
        "audit_network": "Sie\u0107",
        "audit_environment": "\u015arodowisko",
        "audit_exec": "Wykonanie",
        "audit_findings_title": "Wykryte zagro\u017cenia",
        "audit_finding": "Zagro\u017cenie",
        "audit_severity": "Waga",
        "audit_find_unauthorized": "Nieautoryzowana zmiana",
        "audit_find_hash_mismatch": "Niezgodno\u015b\u0107 hash",
        "audit_find_unsigned": "Niepodpisany modu\u0142",
        "audit_find_sandbox": "Pr\u00f3ba obej\u015bcia sandboxa",
        "audit_find_syscall": "Podejrzane wywo\u0142anie systemowe",
        "audit_cfg_changes": "Zmiany konfiguracji",
        "audit_cfg_policy": "Zgodno\u015b\u0107 z politykami",
        "audit_file": "Plik",
        "audit_key": "Klucz",
        "audit_issue": "Problem",
        "audit_policy": "Polityka",
        "audit_result": "Wynik",
        "audit_cfg_added": "Nowy klucz",
        "audit_cfg_changed": "Zmieniona warto\u015b\u0107",
        "audit_cfg_removed": "Brakuj\u0105cy klucz",
        "audit_cfg_type": "Niezgodny typ",
        "audit_cfg_unused": "Nieu\u017cywane ustawienie",
        "audit_cfg_unreadable": "Nieczytelny lub b\u0142\u0119dny config",
        "audit_cfg_policy_violation": "naruszenie",
        "audit_cfg_policy_ok": "zgodne",
        "audit_runtime_events": "Zdarzenia runtime",
        "audit_runtime_anomalies": "Anomalie runtime",
        "audit_kind": "Typ",
        "audit_time_ms": "Czas (ms)",
        "audit_anomaly": "Anomalia",
        "audit_anomaly_slow": "Wolne wykonanie",
        "audit_anomaly_repeat": "Powtarzalne b\u0142\u0119dy",
        "audit_anomaly_stacktrace": "Wykryto stacktrace",
        "audit_anomaly_crash": "Awaria / kod wyj\u015bcia",
        "audit_tab_dashboard": "Dashboard audytu",
        "audit_tab_modules": "Audyt modu\u0142\u00f3w i skrypt\u00f3w",
        "audit_tab_security": "Audyt bezpiecze\u0144stwa",
        "audit_tab_config": "Audyt konfiguracji",
        "audit_tab_runtime": "Audyt runtime",
        "audit_tab_timeline": "Audyt zmian",
        "audit_tab_environment": "Audyt \u015brodowisk",
        "audit_tab_integration": "Integracja",
        "audit_tab_export": "Eksport",
        "audit_integration_open": "Otw\u00f3rz audyt w...",
        "audit_integration_log": "Dziennik integracji",
        "audit_open_diff": "Otw\u00f3rz w Diff",
        "audit_open_search": "Otw\u00f3rz wyszukiwaniu",
        "audit_open_runner": "Otw\u00f3rz w runnerze",
        "audit_open_debugger": "Otw\u00f3rz w debuggerze",
        "audit_generate_report": "Generuj raport audytu",
        "audit_export_artifact": "Eksportuj audyt jako artefakt",
        "audit_operations": "Operacje audytowe",
        "audit_op_full": "Pe\u0142ny audyt systemu",
        "audit_op_module": "Audyt wybranego modu\u0142u",
        "audit_op_script": "Audyt wybranego skryptu",
        "audit_op_config": "Audyt konfiguracji",
        "audit_op_environment": "Audyt \u015brodowiska",
        "audit_op_security": "Audyt bezpiecze\u0144stwa",
        "audit_op_changes": "Audyt zmian",
        "audit_op_dependencies": "Audyt zale\u017cno\u015bci",
        "audit_op_runtime": "Audyt runtime",
        "audit_ok": "OK",
        "audit_failed": "B\u0141\u0104D",
        "audit_integration_noselect": "nie wybrano skryptu",
        "audit_integration_nodiff": "zak\u0142adka Diff niedost\u0119pna",
        "audit_integration_nosearch": "wyszukiwanie niedost\u0119pne",
        "audit_integration_norunner": "runner niedost\u0119pny",
        "audit_integration_nodebugger": "debugger niedost\u0119pny",
        "audit_tl_how_manual": "r\u0119cznie",
        "audit_export_file": "wyeksportowano audyt",
        "audit_export_title": "Eksport audytu",
        "audit_export_preview": "Podgl\u0105d raportu",
        "audit_export_txt": "Eksport TXT (ASCII)",
        "audit_export_json": "Eksport JSON (strukturalny)",
        "audit_export_log": "Eksport LOG",
        "audit_export_full": "Eksport pe\u0142nego zestawu",
        "audit_export_comparative": "Eksport audytu por\u00f3wnawczego",
        "audit_export_preview_txt": "Podgl\u0105d TXT",
        "audit_export_preview_json": "Podgl\u0105d JSON",
        "audit_export_preview_log": "Podgl\u0105d LOG",
        "audit_export_dir": "Otw\u00f3rz katalog eksportu",
        "audit_tab_architecture": "Architektura",
        "audit_tab_premium": "Premium / Automatyzacja",
        "audit_arch_caps": "Mo\u017cliwo\u015bci",
        "audit_arch_frontends": "Interfejsy (API + CLI + GUI)",
        "audit_arch_frontend_text": "API: audit_api(tab) | CLI: audit_cli([...]) | GUI: ta zak\u0142adka",
        "audit_arch_plugins": "Wtyczki",
        "audit_arch_plugin": "Wtyczka",
        "audit_arch_plugin_add": "Zarejestruj demo wtyczk\u0119",
        "audit_arch_plugin_refresh": "Od\u015bwie\u017c wtyczki",
        "audit_arch_events": "Magistrala zdarze\u0144",
        "audit_arch_bus_text": "zdarzenia: %s",
        "audit_arch_snapshots": "Snapshoty",
        "audit_arch_snapshot_name": "Snapshot",
        "audit_arch_snapshot_file": "Plik",
        "audit_arch_snapshot_save": "Zapisz snapshot",
        "audit_arch_snapshot_refresh": "Od\u015bwie\u017c snapshoty",
        "audit_arch_cli_help": "CLI: mo\u017cliwo\u015bci",
        "audit_arch_cli_integrity": "CLI: integralno\u015b\u0107",
        "audit_arch_cli_risk": "CLI: ryzyko",
        "audit_arch_cli": "Polecenie CLI",
        "audit_unavailable": "Zak\u0142adka audytu niedost\u0119pna",
        "audit_script_audited": "zaudytowano",
        "audit_exported": "Wyeksportowano audyt do {path}",
        "audit_this_script": "Audyt tego skryptu",
        "audit_open_tab": "Otw\u00f3rz zak\u0142adk\u0119 Audyt",
        "audit_run_security": "Audyt bezpiecze\u0144stwa",
        "audit_export_menu": "Eksportuj raport audytu",
        "audit_diff_open": "por\u00f3wnanie przejrzane",
        "audit_premium_auto": "Automatyczne audyty",
        "audit_premium_scheduler": "Audyt cykliczny (scheduler)",
        "audit_premium_scheduler_on": "W\u0142\u0105czony",
        "audit_premium_interval": "Interwa\u0142 (s):",
        "audit_premium_run_now": "Uruchom teraz",
        "audit_premium_comparative": "Audyt por\u00f3wnawczy",
        "audit_premium_compare_run": "Poka\u017c zmiany od ostatniego audytu",
        "audit_premium_risk": "Audyt bezpiecze\u0144stwa (poziomy ryzyka)",
        "audit_premium_integrity": "Integralno\u015b\u0107 workspace",
        "audit_trigger_file": "Audyt po zmianie pliku",
        "audit_trigger_install": "Audyt po instalacji modu\u0142u",
        "audit_trigger_update": "Audyt po aktualizacji modu\u0142u",
        "audit_trigger_error": "Audyt po b\u0142\u0119dzie runtime",
        "audit_trigger_deploy": "Audyt po deployu",
        "audit_how_event": "zdarzenie: %s",
        "audit_how_scheduled": "cyklicznie",
        "audit_integrity_new": "brak w baseline",
        "audit_integrity_changed": "zmieniony odcisk",
        "audit_integrity_missing": "brakuj\u0105cy wpis",
        "audit_tl_title": "Os czasu zmian",
        "audit_tl_who": "Kto",
        "audit_tl_category": "Kategoria",
        "audit_tl_what": "Co",
        "audit_tl_how": "Jak",
        "audit_tl_how_auto": "automatycznie",
        "audit_tl_added": "dodano",
        "audit_tl_modified": "zmodyfikowano",
        "audit_tl_removed": "usuni\u0119to",
        "audit_tl_cat_files": "Pliki",
        "audit_tl_cat_modules": "Modu\u0142y",
        "audit_tl_cat_config": "Konfiguracja",
        "audit_tl_cat_environment": "\u015arodowisko",
        "audit_tl_cat_permissions": "Uprawnienia",
        "audit_diff_title": "Por\u00f3wnanie snapshot\u00f3w",
        "audit_diff_item": "Pozycja",
        "audit_diff_kind": "Typ",
        "audit_diff_field": "Pole",
        "audit_diff_before": "Przed",
        "audit_diff_after": "Po",
        "audit_diff_kind_added": "dodano",
        "audit_diff_kind_modified": "zmieniono",
        "audit_diff_kind_removed": "usuni\u0119to",
        "audit_large_title": "Du\u017ce zmiany",
        "audit_large_item": "Pozycja",
        "audit_large_before": "Przed",
        "audit_large_after": "Po",
        "audit_large_delta": "R\u00f3\u017cnica",
        "audit_large_change_threshold": "Pr\u00f3g du\u017cej zmiany",
        "audit_env_vars": "Zmienne \u015brodowiskowe",
        "audit_env_path": "Analiza PATH",
        "audit_env_interp": "Interpretery",
        "audit_env_name": "Nazwa",
        "audit_env_value": "Warto\u015b\u0107",
        "audit_env_length": "D\u0142ugo\u015b\u0107",
        "audit_env_entry": "Wpis",
        "audit_env_exists": "Istnieje",
        "audit_env_issue": "Problem",
        "audit_env_version": "Wersja",
        "audit_env_path_missing": "\u015bcie\u017cka nie istnieje",
        "audit_env_path_duplicate": "zduplikowany wpis",
        "audit_env_path_relative": "wpis relatywny",
        "audit_dep_title": "Zale\u017cno\u015bci",
        "audit_dep_name": "Pakiet",
        "audit_dep_version": "Wersja",
        "audit_dep_versions": "Wersje",
        "audit_dep_source": "\u0179r\u00f3d\u0142o",
        "audit_dep_used_by": "U\u017cywane przez",
        "audit_dep_conflicts": "Konflikty wersji",
        "audit_dep_missing": "brak",
        "analyzer": "Analizator",
        "analyzer_analyse": "Analizuj",
        "analyzer_export": "Eksportuj raport",
        "analyzer_exported": "Wyeksportowano: {path}",
        "analyzer_run": "analiza",
        "analyzer_status": "{scripts} skrypt(\u00f3w), {issues} problem(\u00f3w), \u015brednia ocena {score}",
        "analyzer_tab_overview": "Przegl\u0105d",
        "analyzer_tab_structure": "Struktura",
        "analyzer_tab_complexity": "Z\u0142o\u017cono\u015b\u0107",
        "analyzer_tab_issues": "Problemy",
        "analyzer_tab_dependencies": "Zale\u017cno\u015bci",
        "analyzer_scripts": "Skrypty",
        "analyzer_issues": "Problemy",
        "analyzer_score": "\u015arednia ocena",
        "analyzer_worst": "Najni\u017csza ocena",
        "analyzer_overview": "Przegl\u0105d skrypt\u00f3w",
        "analyzer_structure": "Struktura",
        "analyzer_complexity": "Z\u0142o\u017cono\u015b\u0107",
        "analyzer_dependencies": "Zale\u017cno\u015bci",
        "analyzer_lines": "Linie",
        "analyzer_funcs": "Funkcje",
        "analyzer_classes": "Klasy",
        "analyzer_symbol": "Symbol",
        "analyzer_kind": "Rodzaj",
        "analyzer_in": "W skrypcie",
        "analyzer_function": "Funkcja",
        "analyzer_params": "Parametry",
        "analyzer_issue_kind": "Problem",
        "analyzer_line": "Linia",
        "analyzer_module": "Modu\u0142",
        "analyzer_used_by": "U\u017cywane przez",
        "analyzer_kind_class": "klasa",
        "analyzer_kind_function": "funkcja",
        "analyzer_kind_internal": "wewn\u0119trzny",
        "analyzer_kind_external": "zewn\u0119trzny",
        "analyzer_issue_long_line": "D\u0142uga linia",
        "analyzer_issue_trailing_ws": "Bia\u0142e znaki na ko\u0144cu",
        "analyzer_issue_tabs": "Znak tabulacji",
        "analyzer_issue_bare_except": "Pusty except",
        "analyzer_issue_eval": "U\u017cycie eval/exec",
        "analyzer_issue_todo": "TODO/FIXME",
        "analyzer_issue_print": "print() w kodzie bibliotecznym",
        "analyzer_issue_unused_import": "Nieu\u017cywany import",
        "analyzer_tab_syntax": "Sk\u0142adnia",
        "analyzer_tab_ast": "AST",
        "analyzer_syntax": "Analiza sk\u0142adni",
        "analyzer_syntax_kind": "Rodzaj",
        "analyzer_ast_node": "W\u0119ze\u0142",
        "analyzer_ast_functions": "funkcje",
        "analyzer_ast_classes": "klasy",
        "analyzer_ast_blocks": "bloki",
        "analyzer_ast_imports": "importy",
        "analyzer_ast_calls": "wywo\u0142ania",
        "analyzer_ast_variables": "zmienne",
        "analyzer_ast_signatures": "sygnatury",
        "analyzer_ast_data_flow": "przep\u0142yw danych",
        "analyzer_ast_unused": "zdefiniowane, nieu\u017cywane",
        "analyzer_syntax_error": "B\u0142\u0105d sk\u0142adni",
        "analyzer_syntax_indent": "B\u0142\u0105d wci\u0119cia",
        "analyzer_syntax_unclosed": "Niezamkni\u0119ty blok",
        "analyzer_syntax_expected": "Nieoczekiwany token",
        "analyzer_syntax_invalid": "Niepoprawna sk\u0142adnia",
        "analyzer_syntax_unmatched": "Niedopasowany nawias",
        "analyzer_syntax_unterminated_string": "Niezamkni\u0119ty \u0142a\u0144cuch",
        "analyzer_syntax_unclosed_def": "Niezamkni\u0119ta funkcja",
        "analyzer_syntax_unclosed_class": "Niezamkni\u0119ta klasa",
        "analyzer_syntax_unclosed_if": "Niezamkni\u0119ty if",
        "analyzer_syntax_unclosed_for": "Niezamkni\u0119ta p\u0119tla for",
        "analyzer_syntax_unclosed_while": "Niezamkni\u0119ta p\u0119tla while",
        "analyzer_syntax_unclosed_try": "Niezamkni\u0119ty try",
        "analyzer_indent_mixed": "Mieszane taby i spacje",
        "analyzer_indent_tab": "Wci\u0119cie tabulatorem",
        "analyzer_indent_irregular": "Nieregularne wci\u0119cia",
        "analyzer_type_number_str": "Liczba plus \u0142a\u0144cuch",
        "analyzer_type_str_number": "\u0141a\u0144cuch plus liczba",
        "analyzer_type_reassign": "Zmiana typu zmiennej",
        "analyzer_tab_quality": "Jako\u015b\u0107",
        "analyzer_tab_runtime": "Runtime",
        "analyzer_quality_item": "Pozycja",
        "analyzer_quality_standards": "Standardy",
        "analyzer_quality_duplicates": "Duplikaty kodu",
        "analyzer_quality_long_funcs": "Zbyt d\u0142ugie funkcje",
        "analyzer_quality_complex": "Zbyt z\u0142o\u017cone bloki",
        "analyzer_quality_params": "Zbyt wiele parametr\u00f3w",
        "analyzer_quality_nesting": "Zbyt g\u0142\u0119bokie zagnie\u017cd\u017cenie",
        "analyzer_quality_dead": "Martwy kod",
        "analyzer_style_long_line": "D\u0142uga linia",
        "analyzer_style_trailing_ws": "Bia\u0142e znaki na ko\u0144cu",
        "analyzer_style_tabs": "Wci\u0119cie tabulatorem",
        "analyzer_style_blank_lines": "Za du\u017co pustych linii",
        "analyzer_style_import_order": "Import po kodzie",
        "analyzer_style_naming": "Konwencja nazewnicza",
        "analyzer_style_comparison": "Por\u00f3wnanie z None/bool",
        "analyzer_style_lambda": "Przypisanie lambda",
        "analyzer_dead_code": "Nieosi\u0105galna instrukcja",
        "analyzer_rt_steps": "Kroki \u015bledzenia",
        "analyzer_rt_exceptions": "Wyj\u0105tki",
        "analyzer_rt_dous": "Niebezpieczne",
        "analyzer_rt_anomalies": "Anomalie",
        "analyzer_rt_item": "Pozycja",
        "analyzer_rt_trace": "\u015aledzenie wykonania",
        "analyzer_rt_dangerous": "Niebezpieczne \u015bcie\u017cki",
        "analyzer_rt_stack": "Stacktrace",
        "analyzer_rt_timing": "Czas wykonania",
        "analyzer_rt_calls": "Wywo\u0142ania modu\u0142\u00f3w",
        "analyzer_rt_zerodiv": "Mo\u017cliwe dzielenie przez zero",
        "analyzer_rt_index": "Mo\u017cliwy b\u0142\u0105d indeksu",
        "analyzer_rt_attr": "Mo\u017cliwy b\u0142\u0105d atrybutu",
        "analyzer_rt_key": "Mo\u017cliwy b\u0142\u0105d klucza",
        "analyzer_rt_int": "Mo\u017cliwy b\u0142\u0105d warto\u015bci",
        "analyzer_rt_file": "Mo\u017cliwy b\u0142\u0105d pliku",
        "analyzer_rt_eval": "Wywo\u0142anie eval()",
        "analyzer_rt_exec": "Wywo\u0142anie exec()",
        "analyzer_rt_os_system": "Wywo\u0142anie os.system()",
        "analyzer_rt_subprocess": "U\u017cycie subprocess",
        "analyzer_rt_popen": "Wywo\u0142anie popen()",
        "analyzer_rt_pickle": "Wywo\u0142anie pickle.loads",
        "analyzer_rt_shell": "shell=True",
        "analyzer_rt_rmtree": "Wywo\u0142anie shutil.rmtree",
        "analyzer_rt_infinite_loop": "P\u0119tla bez wyj\u015bcia",
        "analyzer_rt_blocking": "Wywo\u0142anie blokuj\u0105ce",
        "analyzer_rt_unclosed_resource": "Zas\u00f3b niezamkni\u0119ty",
        "analyzer_dep_missing": "Brakuj\u0105ce",
        "analyzer_dep_unused": "Nieu\u017cywane",
        "analyzer_dep_conflicts": "Konflikty",
        "analyzer_dep_system": "Systemowe",
        "analyzer_dep_item": "Pozycja",
        "analyzer_dep_imports": "Importy",
        "analyzer_dep_interpreters": "Wersje interpreter\u00f3w",
        "analyzer_dep_executable": "program",
        "analyzer_dep_os_module": "modu\u0142 OS",
        "analyzer_dep_service": "us\u0142uga",
        "analyzer_yes": "tak",
        "analyzer_no": "nie",
        "analyzer_standards": "Standardy",
        "analyzer_tab_metrics": "Metryki",
        "analyzer_metrics": "Metryki",
        "analyzer_m_metric": "Metryka",
        "analyzer_m_value": "Warto\u015b\u0107",
        "analyzer_m_lines": "Liczba linii kodu",
        "analyzer_m_imports": "Importy",
        "analyzer_m_errors": "B\u0142\u0119dy",
        "analyzer_m_warnings": "Ostrze\u017cenia",
        "analyzer_m_cyclomatic": "Cyklomatyczna",
        "analyzer_m_structural": "Strukturalna",
        "analyzer_m_logical": "Logiczna",
        "analyzer_tab_history": "Diff",
        "analyzer_history": "Wykrywanie zmian",
        "analyzer_hist_item": "Pozycja",
        "analyzer_hist_prev": "Zmiany vs poprzednia",
        "analyzer_hist_snapshot": "Zmiany vs snapshot",
        "analyzer_hist_refactors": "Refaktory",
        "analyzer_hist_total": "Zmiany razem",
        "analyzer_hist_previous": "Poprzednia wersja",
        "analyzer_hist_snapshot_group": "Snapshot",
        "analyzer_hist_functions": "Funkcje",
        "analyzer_hist_classes": "Klasy",
        "analyzer_hist_imports": "Importy",
        "analyzer_hist_logic": "Logika",
        "analyzer_hist_none": "brak porĂłwnania",
        "analyzer_hist_open_diff": "PorĂłwnaj w zakĹ‚adce Diff",
        "analyzer_hist_open_diff_hint": "OtwĂłrz ten skrypt i jego poprzedniÄ… wersjÄ™ w zakĹ‚adce Diff.",
        "analyzer_hist_diff_unavailable": "ZakĹ‚adka Diff niedostÄ™pna",
        "analyzer_tab_config": "Konfiguracja",
        "analyzer_config": "Analiza konfiguracji",
        "analyzer_cfg_item": "Pozycja",
        "analyzer_cfg_files": "Pliki konfiguracyjne",
        "analyzer_cfg_runtime": "Ĺšrodowisko wykonawcze",
        "analyzer_cfg_env": "Zmienne Ĺ›rodowiskowe",
        "analyzer_cfg_path": "Wpisy PATH",
        "analyzer_cfg_interpreters": "Interpretery",
        "analyzer_operations": "Operacje",
        "analyzer_tab_operations": "Operacje",
        "analyzer_operations_result": "Wynik operacji",
        "analyzer_op_item": "Pozycja",
        "analyzer_op_scope": "Zakres",
        "analyzer_op_items": "Ustalenia",
        "analyzer_op_risk": "Ryzyko",
        "analyzer_op_time": "Wygenerowano",
        "analyzer_op_full": "PeĹ‚na analiza skryptu",
        "analyzer_op_fragment": "Analiza wybranego fragmentu",
        "analyzer_op_function": "Analiza funkcji",
        "analyzer_op_class": "Analiza klasy",
        "analyzer_op_imports": "Analiza importĂłw",
        "analyzer_op_dependencies": "Analiza zaleĹĽnoĹ›ci",
        "analyzer_op_errors": "Analiza bĹ‚Ä™dĂłw",
        "analyzer_op_security": "Analiza bezpieczeĹ„stwa",
        "analyzer_op_runtime": "Analiza runtime",
        "analyzer_op_changes": "Analiza zmian",
        "analyzer_op_report": "Generowanie raportu analizy",
        "analyzer_op_overview": "PrzeglÄ…d",
        "analyzer_op_structure": "Struktura",
        "analyzer_op_stats": "Statystyki",
        "analyzer_op_functions": "Funkcje",
        "analyzer_op_classes": "Klasy",
        "analyzer_op_errors": "BĹ‚Ä™dy",
        "analyzer_op_security_static": "Niebezpieczne konstrukcje",
        "analyzer_op_no_fragment": "brak zaznaczenia",
        "analyzer_op_no_fragment_hint": "Najpierw zaznacz kod w edytorze.",
        "analyzer_op_none": "brak",
        "analyzer_op_selected": "zaznaczenie w edytorze",
        "analyzer_op_whole_script": "caĹ‚y skrypt",
        "analyzer_op_noselect": "Najpierw wybierz skrypt.",
        "analyzer_exp_txt": "TXT (ASCII)",
        "analyzer_exp_json": "JSON (strukturalny)",
        "analyzer_exp_log": "LOG",
        "analyzer_exp_report": "Raport analizy",
        "analyzer_exp_security": "Raport bezpieczeĹ„stwa",
        "analyzer_exp_changes": "Raport zmian",
        "analyzer_architecture": "Architektura",
        "analyzer_tab_architecture": "Architektura",
        "analyzer_arch_item": "Pozycja",
        "analyzer_arch_standards": "Standardy",
        "analyzer_arch_frontends": "Interfejsy",
        "analyzer_arch_integrations": "Integracje",
        "analyzer_arch_plugins": "Powierzchnia wtyczek",
        "analyzer_arch_standard": "standard",
        "analyzer_arch_yes": "dostÄ™pne",
        "analyzer_arch_no": "niedostÄ™pne",
        "analyzer_arch_api": "API: describe()",
        "analyzer_arch_cli": "CLI: moĹĽliwoĹ›ci",
        "analyzer_arch_gui_detail": "Ta zakĹ‚adka: menu, podzakĹ‚adki, eksport",
        "analyzer_arch_ascii": "Tylko ASCII",
        "analyzer_arch_hot": "Hot-pluggable",
        "analyzer_arch_zero": "Zero zaleĹĽnoĹ›ci",
        "analyzer_arch_cross": "Wieloplatformowy",
        "analyzer_arch_sandbox": "Bezpieczny w sandboxie",
        "analyzer_arch_plugin": "Gotowy na wtyczki",
        "analyzer_arch_artefact": "Oparta na artefaktach",
        "analyzer_arch_frontends_cap": "API + CLI + GUI",
        "analyzer_arch_integration": "Integracja Diff + Audyt",
        "analyzer_int_diff": "ZakĹ‚adka Diff",
        "analyzer_int_audit": "ZakĹ‚adka Audyt",
        "analyzer_int_runner": "Runner skryptĂłw",
        "analyzer_int_hooks": "Hooki analizy",
        "analyzer_int_registry": "Rejestr API",
        "analyzer_int_cli": "Punkt wejĹ›cia CLI",
        "analyzer_premium": "Premium",
        "analyzer_tab_premium": "Premium / Automatyzacja",
        "analyzer_prem_item": "Pozycja",
        "analyzer_prem_changes": "Obserwowane zmiany",
        "analyzer_prem_predict": "Przewidywane ryzyka",
        "analyzer_prem_risk_col": "Poziom ryzyka",
        "analyzer_prem_project_col": "Ocena projektu",
        "analyzer_prem_auto": "Automatyczna analiza po kaĹĽdej zmianie pliku",
        "analyzer_prem_realtime": "Analiza w czasie rzeczywistym",
        "analyzer_prem_predictive": "Analiza predykcyjna",
        "analyzer_prem_risk": "Analiza ryzyka",
        "analyzer_prem_policy": "Analiza zgodnoĹ›ci z politykami",
        "analyzer_prem_project": "Analiza jakoĹ›ci projektu jako caĹ‚oĹ›ci",
        "analyzer_prem_auto_toggle": "Autoanaliza po kaĹĽdej zmianie",
        "analyzer_prem_watch": "Czas rzeczywisty (watcher)",
        "analyzer_prem_nochange": "brak zmian od ostatniego uruchomienia",
        "analyzer_prem_nodata": "brak danych",
        "analyzer_prem_norisk": "nie przewidziano ryzyka",
        "analyzer_prem_total_scripts": "Skrypty",
        "analyzer_prem_total_lines": "Linie razem",
        "analyzer_prem_total_issues": "Problemy razem",
        "analyzer_policy_no_eval": "bez eval/exec",
        "analyzer_policy_no_shell": "bez shell=True",
        "analyzer_policy_headers": "skrypt ma opis/tag",
        "analyzer_policy_no_todo": "bez zalegĹ‚ych TODO/FIXME",
        "analyzer_policy_complexity": "zĹ‚oĹĽonoĹ›Ä‡ cyklomatyczna <= 10",
        "analyzer_policy_syntax": "skĹ‚adnia poprawna",
        "analyzer_policy_ok": "zgodne",
        "database": "Baza danych",
        "script_name": "Nazwa skryptu:",
        "category": "Kategoria:",
        "tags": "Tagi (oddzielone przecinkami):",
        "description": "Opis:",
        "code": "Kod:",
        "save": "Zapisz",
        "clear": "Wyczy\u015b\u0107",
        "copy": "Kopiuj",
        "delete": "Usu\u0144",
        "run": "Uruchom",
        "edit": "Edytuj",
        "templates": "Szablony",
        "export_json": "Eksportuj JSON",
        "import_json": "Importuj JSON",
        "import_file": "Importuj plik...",
        "import_menu": "Import...",
        "import_menu_json": "Baza JSON (.json)",
        "import_menu_file": "Plik(i) zrĂłdĹ‚owe...",
        "import_menu_folder": "CaĹ‚y folder...",
        "import_folder_empty": "Nie znaleziono obsĹ‚ugiwanych plikĂłw ĹşrĂłdĹ‚owych.",
        "export_file": "Eksportuj plik...",
        "export_menu": "Eksport...",
        "export_menu_json": "Baza JSON (.json)",
        "export_menu_file": "Plik skryptu...",
        "export_menu_report": "Raport (.md)",
        "all_source_files": "Wszystkie pliki (*.*)",
        "confirm_export_file": "Wyeksportowa\u0107 wybrany skrypt do pliku?",
        "exported_file": "Skrypt zapisany w:",
        "history_capped": "Historia tego skryptu jest ograniczona do 25 wersji.",
        "search_label": "Szukaj:",
        "all_categories": "Wszystkie kategorie",
        "total_scripts": "\u0141\u0105cznie skrypt\u00f3w:",
        "name": "Nazwa",
        "category_col": "Kategoria",
        "tags_col": "Tagi",
        "date": "Data",
        "select_script": "Najpierw wybierz skrypt.",
        "fill_required": "Uzupe\u0142nij nazw\u0119 i kod skryptu.",
        "saved": "Skrypt zapisany pomy\u015blnie!",
        "deleted": "Skrypt usuni\u0119ty.",
        "copied": "Kod skopiowany do schowka.",
        "exported": "Eksport zako\u0144czony pomy\u015blnie.",
        "imported": "Import zako\u0144czony pomy\u015blnie.",
        "skipped_duplicates": "Pomini\u0119te duplikaty",
        "filter_placeholder": "Filtruj po nazwie, tagu, kodzie, lang:python, regex:/.../ ... (Ctrl+K)",
        "filter_history": "Historia",
        "filter_history_empty": "(brak historii)",
        "filter_history_clear": "Wyczy\u015b\u0107 histori\u0119",
        "confirm_delete": "Usun\u0105\u0107 wybrany skrypt?",
        "no_results": "Brak wynik\u00f3w.",
        "template_select": "Wybierz szablon",
        "module_name": "Nazwa modu\u0142u:",
        "module_check": "Sprawd\u017a",
        "module_attrs": "Atrybuty",
        "module_doc": "Docstring",
        "module_test": "Uruchom test",
        "module_clear": "Wyczy\u015b\u0107",
        "module_install": "pip install",
        "module_quick": "Szybkie sprawdzenie:",
        "module_test_code": "Kod testowy:",
        "module_output": "Wyj\u015bcie:",
        "ready": "Gotowe",
        "db_tables": "Tabele:",
        "db_snippets": "Gotowe fragmenty:",
        "db_query": "Zapytanie SQL:",
        "db_execute": "Wykonaj",
        "db_clear": "Wyczy\u015b\u0107",
        "db_export_csv": "Eksportuj CSV",
        "db_connect": "Po\u0142\u0105cz",
        "db_disconnect": "Roz\u0142\u0105cz",
        "db_save_conn": "Zapisz po\u0142\u0105czenie",
        "db_del_conn": "Usu\u0144",
        "db_connection": "Po\u0142\u0105czenie:",
        "db_type": "Typ bazy:",
        "db_host": "Host:",
        "db_port": "Port:",
        "db_name": "Baza danych:",
        "db_user": "U\u017cytkownik:",
        "db_password": "Has\u0142o:",
        "db_file": "Plik (SQLite):",
        "saved_connections": "Zapisane po\u0142\u0105czenia",
        "name_connection": "Nazwa:",
        "rows": "wierszy",
        "time_ms": "ms",
        "not_connected": "Brak po\u0142\u0105czenia.",
        "module_not_found": "Nie znaleziono modu\u0142u",
        "arguments": "Argumenty",
        "favorites": "Ulubione",
        "all_scripts": "Wszystkie skrypty",
        "favorites_only": "Tylko ulubione",
        "toggle_favorite": "Ulubiony (Ctrl+F)",
        "show_all": "Poka\u017c wszystkie",
        "duplicate": "Duplikuj",
        "new_script": "Nowy skrypt",
        "history": "Historia",
        "version_history": "Historia wersji",
        "preview": "Podgl\u0105d",
        "restore": "Przywr\u00f3\u0107",
        "diff": "Por\u00f3wnaj",
        "diff_title": "Zmiany w tej wersji",
        "no_history": "Ten skrypt nie ma jeszcze historii.",
        "confirm_restore": "Wczyta\u0107 t\u0119 wersj\u0119 do edytora?",
        "restored": "Wersja wczytana do edytora.",
        "duplicated": "Skrypt zduplikowany.",
        "copied_script": "Skrypt skopiowany do schowka jako tekst.",
        "format_json": "Formatuj JSON",
        "minify_json": "Minifikuj JSON",
        "format_sql": "Formatuj SQL",
        "sorted": "Posortowano po nazwie.",
        "download": "Pobierz plik",
        "report": "Eksportuj raport",
        "statistics": "Statystyki",
        "stats_title": "Statystyki bazy",
        "shortcuts": "Skróty",
        "shortcuts_title": "Skróty klawiszowe",
        "shortcut": "Skrót",
        "shortcut_action": "Akcja",
        "shortcut_group_general": "Ogólne",
        "shortcut_group_scripts": "Skrypty",
        "shortcut_group_editor": "Edytor",
        "shortcut_group_diff": "Zakładka Diff",
        "syntax_error": "B\u0142\u0105d sk\u0142adni w kodzie",
        "unknown_category": "Nieznana kategoria",
        "import_error": "B\u0142\u0105d importu",
        "export_error": "B\u0142\u0105d eksportu",
        "warning": "Uwaga",
        "information": "Informacja",
        "confirm": "Potwierd\u017a",
        "info": "Info",
        "db_execute_selection": "Wykonaj zaznaczenie",
        "db_history": "Historia zapyta\u0144",
        "db_truncated": "Pokazano pierwsze {shown} z {total} wierszy.",
        "db_save_selection": "Zapisz jako skrypt",
        "load_problem": "Nie uda\u0142o si\u0119 odczyta\u0107 cz\u0119\u015bci plik\u00f3w danych i zosta\u0142y pomini\u0119te:",
        "load_problem_hint": "Zapisanie teraz nadpisa\u0142oby je pust\u0105 list\u0105. Zr\u00f3b kopi\u0119 pliku i najpierw napraw JSON.",
        "map": "Mapa",
        "map_toggle": "Minimapa: w\u0142.",
        "map_toggle_off": "Minimapa: wy\u0142.",
        "runner_run": "Uruchom",
        "runner_rerun": "Uruchom ponownie",
        "runner_stop": "Zatrzymaj",
        "runner_send": "Wy\u015blij",
        "runner_stdin_placeholder": "Wpisz dane wej\u015bciowe dla dzia\u0142aj\u0105cego skryptu i naci\u015bnij Enter...",
        "runner_wrap": "Zawijanie: wy\u0142.",
        "runner_status_ready": "Gotowe",
        "runner_status_running": "Uruchamianie...",
        "runner_status_stdin": "Oczekiwanie na dane...",
        "runner_status_done": "Gotowe w {time}",
        "runner_status_error": "B\u0142\u0105d wykonania",
        "runner_status_exit": "Zako\u0144czono z kodem {code}",
        "runner_status_timeout": "Przekroczono limit {seconds}s",
        "runner_status_stopped": "Zatrzymano przez u\u017cytkownika",
        "runner_status_unavailable": "Niedost\u0119pne",
        "runner_no_output": "(brak wyj\u015bcia)",
        "runner_elapsed": "Czas: {time}",
        "runner_exit_label": "Kod wyj\u015bcia: {code}",
        "runner_no_python": "Nie znaleziono interpretera Pythona w PATH.",
        "runner_python_fallback": "Brak dedykowanego runnera dla tego j\u0119zyka; uruchamiam jako Python.",
        "runner_bash_unavailable": "Nie znaleziono Bash. Zainstaluj Git for Windows lub WSL, aby uruchamia\u0107 skrypty pow\u0142oki.",
        "runner_powershell_unavailable": "Nie znaleziono PowerShell.",
        "runner_node_unavailable": "Nie znaleziono Node.js.",
        "runner_batch_unavailable": "Pliki wsadowe s\u0105 obs\u0142ugiwane tylko w systemie Windows.",
        "runner_exe_unavailable": "Nie znaleziono pliku wykonywalnego: {name}",
        "runner_timeout_note": "[przekroczono limit {seconds}s - proces zabity]",
        "runner_stopped_note": "[zatrzymano przez u\u017cytkownika]",
        "runner_exit_note": "[kod wyj\u015bcia {code} - czas {time}]",
        "runner_syntax_error": "B\u0142\u0105d sk\u0142adni w skrypcie.",
        "metadata": "Metadane",
        "meta_detect": "Autowykryj metadane",
        "meta_detected": "Autowykryto metadane dla {count} skryptu(\u00f3w).",
        "meta_sync": "Synchronizuj statystyki",
        "meta_load": "Wczytaj do formularza",
        "meta_apply": "Zastosuj do wybranych",
        "meta_stamp": "Oznacz dat\u0119 zmiany",
        "meta_bump": "Zwi\u0119ksz wersj\u0119",
        "meta_export_json": "Eksportuj metadane JSON",
        "meta_export_csv": "Eksportuj metadane CSV",
        "meta_import": "Importuj metadane JSON",
        "meta_clear": "Wyczy\u015b\u0107 metadane",
        "meta_filter": "Filtr:",
        "meta_filter_hint": "Filtruj po nazwie, autorze, licencji, URL, notatkach...",
        "meta_show_all": "Poka\u017c skrypty bez metadanych",
        "meta_all_status": "Wszystkie statusy",
        "meta_author": "Autor:",
        "meta_version": "Wersja:",
        "meta_license": "Licencja:",
        "meta_language": "J\u0119zyk:",
        "meta_status": "Status:",
        "meta_url": "URL:",
        "meta_notes": "Notatki:",
        "meta_lines": "Linie",
        "meta_chars": "Znaki",
        "meta_hash": "SHA-256",
        "meta_created": "Utworzono",
        "meta_modified": "Zmodyfikowano",
        "meta_select_hint": "Najpierw wybierz skrypt.",
        "meta_applied": "Zastosowano metadane do {count} skryptu(\u00f3w).",
        "meta_synced": "Zsynchronizowano metadane dla {count} skryptu(\u00f3w).",
        "meta_imported": "Zaktualizowano metadane dla {count} skryptu(\u00f3w).",
        "meta_confirm_clear": "Wyczy\u015bci\u0107 metadane dla {count} wybranych skrypt\u00f3w?",
        "meta_summary": "Pokazano {shown} z {total} skrypt\u00f3w ({with_meta} z metadanymi).",
        "compare": "Por\u00f3wnanie",
        "compare_left": "Lewy:",
        "compare_right": "Prawy:",
        "compare_swap": "Zamie\u0144",
        "compare_run": "Por\u00f3wnaj",
        "compare_ignore_case": "Ignoruj wielko\u015b\u0107 liter",
        "compare_ignore_ws": "Ignoruj bia\u0142e znaki",
        "compare_ignore_blank": "Ignoruj puste linie",
        "compare_view_side": "Obok siebie",
        "compare_view_unified": "Diff jednolity",
        "compare_view_fields": "Pola",
        "compare_copy": "Kopiuj diff",
        "compare_export": "Eksportuj diff",
        "compare_field": "Pole",
        "compare_left_value": "Lewy",
        "compare_right_value": "Prawy",
        "compare_select_hint": "Wybierz dwa skrypty do por\u00f3wnania.",
        "compare_identical": "(oba skrypty s\u0105 identyczne)",
        "compare_no_diff": "Brak r\u00f3\u017cnic do wyeksportowania.",
        "compare_summary": "Podobie\u0144stwo {ratio}%  |  identyczne: {same}  zmienione: {changed}  dodane: {added}  usuni\u0119te: {removed}  |  zmiany razem: {total}  |  linie: {left_lines} -> {right_lines}",
        "compare_open_file": "Plik...",
        "compare_revision": "{name} - wersja #{n}",
        "compare_stats": "Statystyki",
        "compare_with": "Por\u00f3wnaj z...",
        "compare_ignore_comments": "Ignoruj komentarze",
        "compare_prev": "Poprzednia zmiana",
        "compare_next": "Nast\u0119pna zmiana",
        "compare_report": "Eksportuj raport",
        "compare_report_title": "Raport por\u00f3wnania skrypt\u00f3w",
        "compare_clear_files": "Wyczy\u015b\u0107 pliki",
        "editor_delete_line": "Usu\u0144 lini\u0119",
        "editor_tools_menu": "Narz\u0119dzia kodu...",
        "editor_move_menu": "Przenie\u015b...",
        "editor_move_up": "W g\u00f3r\u0119",
        "editor_move_down": "W d\u00f3\u0142",
        "editor_sort_lines": "Sortuj linie",
        "editor_trim_spaces": "Usu\u0144 spacje",
        "editor_tabs_to_spaces": "Taby na spacje",
        "editor_line_moved": "linia przeniesiona",
        "editor_at_top": "ju\u017c na g\u00f3rze",
        "editor_at_bottom": "ju\u017c na dole",
        "editor_sorted": "linie posortowane",
        "editor_nothing_to_sort": "brak linii do sortowania",
        "editor_trimmed": "usuni\u0119to spacje w {count} linii",
        "editor_no_trailing": "brak spacji na ko\u0144cu",
        "editor_tabs_done": "taby zamienione na spacje",
        "editor_refactor_menu": "Refaktoryzacja...",
        "editor_refactor_rename": "ZmieĹ„ nazwÄ™ symbolu",
        "editor_refactor_extract_function": "WyodrÄ™bnij funkcjÄ™",
        "editor_refactor_extract_variable": "WyodrÄ™bnij zmiennÄ…",
        "editor_refactor_move_function": "PrzenieĹ› funkcjÄ™",
        "editor_refactor_move_class": "PrzenieĹ› klasÄ™",
        "editor_cleanup_menu": "Czyszczenie kodu...",
        "editor_format_code": "Formatuj kod",
        "editor_remove_unused_imports": "UsuĹ„ nieuĹĽywane importy",
        "editor_remove_dead_code": "UsuĹ„ martwy kod",
        "editor_fix_linter": "Napraw bĹ‚Ä™dy lintera",
        "editor_new_name": "Nowa nazwa:",
        "editor_function_name": "Nazwa funkcji:",
        "editor_variable_name": "Nazwa zmiennej:",
        "editor_move_definition": "PrzenieĹ› definicjÄ™",
        "editor_move_target": "Linia docelowa (1 = gĂłra):",
        "editor_need_symbol": "Najpierw zaznacz symbol.",
        "editor_select_expression": "Najpierw zaznacz wyraĹĽenie.",
        "editor_need_definition": "UmieĹ›Ä‡ kursor wewnÄ…trz definicji.",
        "editor_python_only": "To narzÄ™dzie obsĹ‚uguje tylko Python.",
        "editor_refactored": "zastosowano refaktoryzacjÄ™",
        "editor_formatted": "sformatowano kod",
        "editor_unused_removed": "usuniÄ™to {count} nieuĹĽywanych importĂłw",
        "editor_dead_removed": "usuniÄ™to {count} linii martwego kodu",
        "editor_linter_fixed": "naprawiono linter ({count} importĂłw usuniÄ™to)",
        "editor_no_changes": "brak zmian do zastosowania",
        "editor_analysis_menu": "Analiza kodu...",
        "editor_analysis_syntax": "Analiza skĹ‚adni",
        "editor_analysis_ast": "Analiza AST",
        "editor_analysis_semantic": "Analiza semantyczna",
        "editor_analysis_security": "Analiza bezpieczeĹ„stwa",
        "editor_analysis_quality": "Analiza jakoĹ›ci kodu",
        "editor_analysis_runtime": "Analiza runtime (symulacja)",
        "editor_analysis_dependencies": "Analiza zaleĹĽnoĹ›ci",
        "editor_analysis_changes": "Analiza zmian (diff)",
        "editor_analysis_errors": "Analiza bĹ‚Ä™dĂłw",
        "editor_analysis_item": "Element",
        "editor_analysis_value": "WartoĹ›Ä‡",
        "editor_analysis_detail": "SzczegĂłĹ‚y",
        "editor_analysis_close": "Zamknij",
        "editor_analysis_unavailable": "Analizator jest niedostÄ™pny.",
        "editor_analysis_failed": "Analiza nie powiodĹ‚a siÄ™: {error}",
        "editor_metrics_menu": "Metryki kodu",
        "editor_metric_lines": "Liczba linii",
        "editor_metric_functions": "Liczba funkcji",
        "editor_metric_classes": "Liczba klas",
        "editor_metric_imports": "Liczba importĂłw",
        "editor_metric_cyclomatic": "ZĹ‚oĹĽonoĹ›Ä‡ cyklomatyczna",
        "editor_metric_logical": "ZĹ‚oĹĽonoĹ›Ä‡ logiczna",
        "editor_metric_errors": "Liczba bĹ‚Ä™dĂłw",
        "editor_metric_warnings": "Liczba ostrzeĹĽeĹ„",
        "editor_integration_menu": "Integracje...",
        "editor_integration_diff": "OtwĂłrz w Diff",
        "editor_integration_search": "OtwĂłrz w wyszukiwaniu",
        "editor_integration_audit": "OtwĂłrz w Audycie",
        "editor_integration_runner": "OtwĂłrz w Runnerze",
        "editor_integration_debugger": "OtwĂłrz w Debuggerze",
        "editor_integration_report": "Generuj raport skryptu",
        "editor_integration_project": "PowiÄ…ĹĽ z projektem",
        "editor_integration_opened": "{value}",
        "editor_integration_unavailable": "{value}",
        "editor_integration_failed": "Integracja nie powiodĹ‚a siÄ™: {error}",
        "editor_integration_report_done": "zapisano raport: {path}",
        "editor_integration_project_done": "powiÄ…zano projekt: {project}",
        "editor_project_name": "Nazwa projektu:",
        "editor_architecture_menu": "Architektura edytora",
        "editor_arch_ascii": "Tylko ASCII",
        "editor_arch_hot": "Hot-pluggable",
        "editor_arch_zero": "Zero zaleĹĽnoĹ›ci",
        "editor_arch_cross": "Wieloplatformowy",
        "editor_arch_sandbox": "Bezpieczny w sandboxie",
        "editor_arch_plugin": "Gotowy na wtyczki",
        "editor_arch_artefact": "Oparty na artefaktach",
        "editor_arch_frontends": "API + CLI + GUI",
        "editor_arch_events": "Integracja z event bus",
        "editor_arch_snapshots": "Integracja z snapshotami",
        "editor_arch_audit_reports": "Audyt i raporty",
        "editor_arch_standard": "standard",
        "editor_arch_item": "Element",
        "editor_arch_kind": "Rodzaj",
        "editor_arch_detail": "SzczegĂłĹ‚y",
        "editor_templates_menu": "Szablony...",
        "vault": "SEJF",
        "security": "ZABEZPIECZENIA",
        "sec_title": "ZABEZPIECZENIA",
        "sec_state_open": "ODBLOKOWANE",
        "sec_state_locked": "ZABLOKOWANE",
        "sec_count": "{0} wpis(ów)",
        "sec_new": "Nowy magazyn zabezpieczeń. Ustaw hasło.",
        "sec_locked_action": "Zablokowane. Odblokuj, aby {action}.",
        "sec_locked": "Zablokowane.",
        "sec_create": "Ustaw hasło...",
        "sec_unlock": "Odblokuj...",
        "sec_lock": "Zablokuj",
        "sec_policy": "Polityka...",
        "sec_high": "Wysokie bezpieczeństwo",
        "sec_high_on": "Tryb wysokiego bezpieczeństwa włączony.",
        "sec_high_off": "Tryb wysokiego bezpieczeństwa wyłączony.",
        "sec_passphrase_hint": "Nowe hasło (min. 8 znaków):",
        "sec_passphrase_again": "Powtórz hasło:",
        "sec_passphrase_prompt": "Hasło:",
        "sec_token_prompt": "Token: ",
        "sec_policy_saved": "Zaktualizowano politykę.",
        "sec_pick": "Najpierw wybierz skrypt.",
        "sec_fingerprint": "Odcisk",
        "sec_fingerprinted": "Obliczono odcisk.",
        "sec_sign": "Podpisz",
        "sec_signed": "Skrypt podpisany.",
        "sec_record": "Zapisz wpis",
        "sec_recorded": "Zapisano wpis integralności.",
        "sec_verify": "Weryfikuj",
        "sec_verified": "Weryfikacja zakończona.",
        "sec_seal": "Zaszyfruj",
        "sec_sealed": "Skrypt zaszyfrowany.",
        "sec_unseal": "Odszyfruj",
        "sec_unsealed": "Skrypt odszyfrowany.",
        "sec_nothing_sealed": "Ten skrypt nie był zaszyfrowany.",
        "sec_backup": "Kopia",
        "sec_backed_up": "Zapisano kopię: {0}",
        "sec_restore": "Przywróć",
        "sec_restore_pick": "Którą kopię przywrócić?",
        "sec_restored": "Przywrócono kopię.",
        "sec_no_backups": "Brak kopii.",
        "sec_shadow": "Klucz cienia",
        "sec_shadow_issued": "Wydano klucz cienia.",
        "sec_hash_all": "Odcisk wszystkich",
        "sec_hashed_all": "Zapisano {0} skrypt(ów).",
        "sec_reports": "Raporty...",
        "sec_report_hint": "Tutaj pojawi się raport lub plan.",
        "sec_report_shown": "Raport: {0}",
        "sec_export": "Eksport...",
        "sec_export_report": "Raport do JSON",
        "sec_export_registry": "Rejestr do JSON",
        "sec_export_config": "Konfiguracja",
        "sec_exported": "Wyeksportowano raport: {0}",
        "sec_exported_registry": "Wyeksportowano rejestr: {0}",
        "sec_exported_config": "Wyeksportowano konfigurację: {0}",
        "sec_import": "Import...",
        "sec_imported_registry": "Zaimportowano rejestr: {0} wpis(ów)",
        "sec_imported_config": "Zaimportowano konfigurację.",
        "sec_import_unknown": "To nie jest pakiet zabezpieczeń.",
        "sec_sandbox": "Plan sandboxa",
        "sec_sandbox_plan": "Zbudowano plan sandboxa.",
        "sec_no_scripts": "Brak skryptów do zabezpieczenia.",
        "sec_rows": "Wymieniono {0} skrypt(ów).",
        "sec_yes": "tak",
        "sec_no": "nie",
        "sec_col_name": "Skrypt",
        "sec_col_digest": "SHA-256",
        "sec_col_record": "Wpis",
        "sec_col_signed": "Podpis",
        "sec_col_state": "Stan",
        "sec_col_detail": "Szczegóły",
        "sec_group_auth": "Autoryzacja",
        "sec_group_sandbox": "Sandbox i izolacja",
        "sec_digest_sha256": "SHA-256",
        "sec_digest_sha512": "SHA-512",
        "sec_digest_sha3": "SHA3-256",
        "sec_digest_blake2b": "BLAKE2b",
        "sec_kdf_pbkdf2": "PBKDF2-HMAC-SHA256",
        "sec_kdf_scrypt": "scrypt",
        "sec_kdf_salted": "SHA-256 z solą (iterowane)",
        "sec_report_integrity": "Raport integralności",
        "sec_report_changes": "Raport zmian",
        "sec_report_access": "Raport dostępu",
        "sec_report_errors": "Raport błędów",
        "sec_report_warnings": "Raport ostrzeżeń",
        "sec_report_risk": "Raport ryzyka",
        "sec_auth_password": "Wymagaj hasła",
        "sec_auth_token": "Wymagaj tokenu (2. czynnik)",
        "sec_auth_attempts": "Dozwolone nieudane próby",
        "sec_auth_cooldown": "Blokada po błędach (s)",
        "sec_auth_autolock": "Autoblokada (sekundy)",
        "sec_auth_high": "Tryb wysokiego bezpieczeństwa",
        "sec_auth_sign": "Podpisuj każdy skrypt",
        "sec_auth_tamper": "Przy manipulacji: zablokuj sesję",
        "sec_sb_fs": "Izoluj system plików",
        "sec_sb_net": "Blokuj sieć",
        "sec_sb_syscalls": "Ogranicz wywołania systemowe",
        "sec_sb_env": "Wyczyść środowisko",
        "sec_sb_process": "Uruchom w osobnym procesie",
        "sec_sb_logs": "Osobne logi",
        "sec_sb_shadow_fs": "Cienisty system plików",
        "sec_sb_shadow_env": "Cieniste środowisko",
        "sec_sb_shadow_net": "Cienista sieć",
        "structure": "STRUKTURY",
        "st_title": "STRUKTURY",
        "st_preset": "Preset:",
        "st_name": "Projekt:",
        "st_default_name": "projekt",
        "st_target": "Cel:",
        "st_target_hint": "pozostaw puste, aby użyć obszaru roboczego",
        "st_browse": "Przeglądaj...",
        "st_preview": "Podgląd",
        "st_dry": "Próba",
        "st_generate": "Generuj",
        "st_validate": "Waliduj",
        "st_compile": "Kompiluj",
        "st_reverse": "Analizuj folder...",
        "st_describe": "Z opisu...",
        "st_describe_hint": "Opisz strukturę, np. \"gra rpg w godot\":",
        "st_overwrite": "Nadpisz",
        "st_no_preset": "Nie wybrano presetu.",
        "st_preset_count": "{0} preset(ów)",
        "st_state_scope": "zakres: {0}  v{1}",
        "st_previewed": "{0} plik(ów), {1} folder(ów).",
        "st_dry_done": "Próba: {0} akcji.",
        "st_generated": "Wygenerowano w {0}.",
        "st_generate_failed": "Generowanie zatrzymane na: {0}",
        "st_validated": "Poprawny: {0}  błędy: {1}  ostrzeżenia: {2}",
        "st_compiled": "Skompilowano: {0}  artefakty: {1}",
        "st_reversed": "Przeanalizowano {0}",
        "st_reversed_status": "Wczytano {0} plik(ów) do presetu {1}.",
        "st_reverse_sample": "Próbka",
        "st_described": "Rozpoznano: {0}",
        "st_nothing": "nic",
        "st_saved": "Zapisano preset: {0} v{1}",
        "st_deleted": "Usunięto preset: {0}",
        "st_exported_presets": "Zapisano paczkę presetów: {0}",
        "st_imported_presets": "Wczytano paczkę presetów: {0} preset(ów)",
        "st_plugins_loaded": "Załadowano wbudowane generatory.",
        "st_pick_plugin": "Najpierw wybierz plugin.",
        "st_plugin_toggled": "{0}: {1}",
        "st_enabled": "włączony",
        "st_disabled": "wyłączony",
        "st_cli_empty": "Wpisz polecenie CLI.",
        "st_cli_done": "Kod wyjścia CLI: {0}",
        "st_cli_hint": "validate python-app",
        "st_cli_run": "Uruchom CLI",
        "st_report_saved": "Zapisano raport: {0}",
        "st_no_target": "Brak wygenerowanego celu do spakowania.",
        "st_zip_written": "Zapisano zip: {0} ({1} plik(ów))",
        "st_no_tree": "Najpierw pokaż podgląd struktury.",
        "st_docs_written": "Zapisano dokumentację: {0}",
        "st_log": "Log generowania",
        "st_log_empty": "nic jeszcze nie zapisano",
        "st_stage": "Etap",
        "st_written": "Zapisano",
        "st_skipped": "Pominięto",
        "st_artifacts": "Artefakty",
        "st_unfilled": "nieuzupełnione tokeny: ",
        "st_folder": "folder",
        "st_file": "plik",
        "st_panel_tree": "Drzewo",
        "st_panel_edit": "Preset",
        "st_panel_plugins": "Pluginy",
        "st_panel_pipeline": "Pipeline",
        "st_template": "Szablon:",
        "st_edit_title": "Tytuł",
        "st_edit_kind": "Rodzaj",
        "st_edit_language": "Język",
        "st_edit_build": "System budowania",
        "st_edit_ci": "CI",
        "st_edit_ide": "IDE",
        "st_edit_extends": "Dziedziczy",
        "st_extends_hint": "nazwa presetu do rozszerzenia",
        "st_save_preset": "Zapisz preset",
        "st_delete_preset": "Usuń preset",
        "st_export_presets": "Eksportuj presety",
        "st_import_presets": "Importuj presety",
        "st_plugin_load": "Załaduj wbudowane generatory",
        "st_plugin_toggle": "Włącz / wyłącz",
        "st_save_report": "Zapisz raport",
        "st_export_zip": "Eksportuj zip",
        "st_export_docs": "Eksportuj dokumentację",
        "st_show_log": "Log",
        "st_col_path": "Ścieżka",
        "st_col_kind": "Rodzaj",
        "st_col_size": "Rozmiar",
        "st_col_plugin": "Plugin",
        "st_col_state": "Stan",
        "st_report_hint": "Tutaj pojawi się drzewo, plan, raport lub wynik CLI.",
        "st_scope_user": "Użytkownik",
        "st_scope_org": "Organizacja",
        "st_scope_project": "Projekt",
        "st_scope_global": "Globalny",
        "st_kind_app": "Aplikacja",
        "st_kind_module": "Moduł",
        "st_kind_game": "Gra",
        "st_kind_library": "Biblioteka",
        "st_kind_framework": "Framework",
        "st_kind_plugin": "Plugin",
        "st_lang_python": "Python",
        "st_lang_cpp": "C++",
        "st_lang_node": "Node / JavaScript",
        "st_lang_java": "Java",
        "st_lang_rust": "Rust",
        "st_lang_csharp": "C# / Unity",
        "st_lang_unreal": "Unreal",
        "st_lang_godot": "Godot",
        "st_build_none": "Brak",
        "st_build_cmake": "CMake",
        "st_build_make": "Make",
        "st_build_gradle": "Gradle",
        "st_build_npm": "npm",
        "st_build_pip": "pip",
        "st_build_cargo": "Cargo",
        "st_ci_none": "Brak",
        "st_ci_github": "GitHub Actions",
        "st_ci_gitlab": "GitLab CI",
        "st_ci_azure": "Azure Pipelines",
        "st_ide_none": "Brak",
        "st_ide_vscode": "VS Code",
        "st_ide_pycharm": "PyCharm",
        "st_ide_intellij": "IntelliJ",
        "st_stage_pre": "Przed generowaniem",
        "st_stage_generate": "Generowanie",
        "st_stage_validate": "Walidacja",
        "st_stage_post": "Po generowaniu",
        "st_stage_artifacts": "Artefakty",
        "sqlite": "SQLITE",
        "sqlite_tab_offline": "Baza offline",
        "sqlite_tab_online": "Baza online",
        "sqlite_tab_sync": "Synchronizacja",
        "sqlite_tab_tools": "NarzÄ™dzia",
        "sqlite_tab_logs": "Logi",
        "sqlite_tab_settings": "Ustawienia",
        "sqlite_open": "OtwĂłrz bazÄ™...",
        "sqlite_create": "Nowa baza...",
        "sqlite_close": "Zamknij",
        "sqlite_save": "Zapisz",
        "sqlite_read_only": "Tylko odczyt",
        "sqlite_browse": "PrzeglÄ…daj...",
        "sqlite_folder": "Katalog do przeglÄ…dania...",
        "sqlite_tables": "Tabele",
        "sqlite_records": "Rekordy",
        "sqlite_schema": "Schemat",
        "sqlite_sql": "SQL",
        "sqlite_stats": "Statystyki",
        "sqlite_load": "Wczytaj rekordy",
        "sqlite_search_rows": "Filtruj rekordy...",
        "sqlite_add_row": "Dodaj",
        "sqlite_edit_row": "Edytuj",
        "sqlite_copy_row": "Kopiuj",
        "sqlite_delete_row": "UsuĹ„",
        "sqlite_prev": "Poprzednia",
        "sqlite_next": "NastÄ™pna",
        "sqlite_add_column": "Dodaj kolumnÄ™",
        "sqlite_new_table": "Nowa tabela",
        "sqlite_rename_table": "ZmieĹ„ nazwÄ™ tabeli",
        "sqlite_drop_table": "UsuĹ„ tabelÄ™",
        "sqlite_run_sql": "Wykonaj SQL",
        "sqlite_explain": "Plan zapytania",
        "sqlite_sql_all": "CaĹ‚y skrypt",
        "sqlite_refresh_stats": "OdĹ›wieĹĽ statystyki",
        "sqlite_col_name": "Nazwa",
        "sqlite_col_type": "Typ",
        "sqlite_col_null": "Dopuszcza NULL",
        "sqlite_col_key": "Klucz",
        "sqlite_col_value": "WartoĹ›Ä‡",
        "sqlite_col_detail": "SzczegĂłĹ‚y",
        "sqlite_col_kind": "RĂłĹĽnica",
        "sqlite_col_offline": "Offline",
        "sqlite_col_online": "Online",
        "sqlite_col_time": "Czas",
        "sqlite_col_layer": "Warstwa",
        "sqlite_col_action": "Akcja",
        "sqlite_ready": "OtwĂłrz plik bazy, aby zaczÄ…Ä‡.",
        "sqlite_open_first": "Najpierw otwĂłrz bazÄ™.",
        "sqlite_choose_table": "Najpierw wybierz tabelÄ™.",
        "sqlite_select_row": "Najpierw wybierz rekord.",
        "sqlite_is_read_only": "Baza jest otwarta tylko do odczytu.",
        "sqlite_opened": "Otwarto {name} ({kind}).",
        "sqlite_closed": "Baza zamkniÄ™ta.",
        "sqlite_loaded_rows": "Wczytano {count} rekordĂłw.",
        "sqlite_row_added": "Rekord dodany.",
        "sqlite_row_updated": "Rekord zaktualizowany.",
        "sqlite_row_copied": "Rekord skopiowany.",
        "sqlite_row_deleted": "Rekord usuniÄ™ty.",
        "sqlite_row_missing": "Ten rekord juĹĽ nie istnieje.",
        "sqlite_delete_confirm": "UsunÄ…Ä‡ wybrany rekord? Tej operacji nie moĹĽna cofnÄ…Ä‡.",
        "sqlite_page": "rekordy {first}-{last} z {total}",
        "sqlite_first_page": "To juĹĽ pierwsza strona.",
        "sqlite_state_open": "otwarta",
        "sqlite_state_closed": "zamkniÄ™ta",
        "sqlite_state_signed_in": "zalogowany",
        "sqlite_state_signed_out": "wylogowany",
        "sqlite_readonly_state": "tylko odczyt: {state}",
        "sqlite_no_schema": "Ten format nie ma instrukcji CREATE.",
        "sqlite_columns_hint": "Jedna kolumna w linii: nazwa TYP [PRIMARY KEY] [NOT NULL]",
        "sqlite_column_spec": "nazwa TYP (na przykĹ‚ad: note TEXT):",
        "sqlite_need_name": "Tabela wymaga nazwy i co najmniej jednej kolumny.",
        "sqlite_table_created": "Utworzono tabelÄ™ {name}.",
        "sqlite_table_renamed": "Nazwa tabeli zmieniona.",
        "sqlite_table_dropped": "Tabela usuniÄ™ta.",
        "sqlite_column_added": "Kolumna dodana.",
        "sqlite_new_name": "Nowa nazwa:",
        "sqlite_drop_confirm": "UsunÄ…Ä‡ tabelÄ™ {name} i wszystkie jej rekordy?",
        "sqlite_no_sql": "Najpierw wpisz instrukcjÄ™.",
        "sqlite_sql_done": "Wykonano {count} instrukcji.",
        "sqlite_cancel": "Anuluj",
        "sqlite_apply": "Zastosuj",
        "sqlite_reset": "PrzywrĂłÄ‡",
        "sqlite_online_user": "UĹĽytkownik",
        "sqlite_online_pass": "HasĹ‚o",
        "sqlite_online_login": "Zaloguj",
        "sqlite_online_logout": "Wyloguj",
        "sqlite_online_ping": "Ping",
        "sqlite_online_refresh": "OdĹ›wieĹĽ sesjÄ™",
        "sqlite_online_tables": "Wczytaj tabele",
        "sqlite_online_rows": "Wczytaj rekordy",
        "sqlite_online_add": "Dodaj rekord",
        "sqlite_online_update": "Zaktualizuj rekord",
        "sqlite_online_delete": "UsuĹ„ rekord",
        "sqlite_online_welcome": "Zalogowano jako {user}.",
        "sqlite_online_bye": "Wylogowano.",
        "sqlite_online_tables_loaded": "{count} tabel na serwerze.",
        "sqlite_online_rows_loaded": "{count} rekordĂłw z serwera.",
        "sqlite_online_done": "Wykonano: {action}.",
        "sqlite_console_body": "TreĹ›Ä‡ JSON (opcjonalnie)",
        "sqlite_console_send": "WyĹ›lij",
        "sqlite_console_token": "DoĹ‚Ä…cz token",
        "sqlite_console_sent": "Ĺ»Ä…danie konsoli wysĹ‚ane.",
        "sqlite_sync_mode": "Tryb",
        "sqlite_sync_policy": "Konflikty",
        "sqlite_sync_table": "Tabela do uzgodnienia",
        "sqlite_sync_compare": "PorĂłwnaj",
        "sqlite_sync_plan": "Plan",
        "sqlite_sync_run": "Uruchom synchronizacjÄ™",
        "sqlite_sync_resolve": "Rozstrzygnij wybrany konflikt",
        "sqlite_sync_report": "Raport zmian",
        "sqlite_sync_save_report": "Zapisz raport",
        "sqlite_sync_result": "Wynik synchronizacji",
        "sqlite_sync_confirm": "UruchomiÄ‡ synchronizacjÄ™ '{mode}' na {table}?",
        "sqlite_sync_select": "Najpierw wybierz konflikt.",
        "sqlite_sync_side": "KtĂłra strona wygrywa?",
        "sqlite_sync_resolved": "RozstrzygniÄ™to {count} konfliktĂłw.",
        "sqlite_sync_upload": "Offline do online",
        "sqlite_sync_download": "Online do offline",
        "sqlite_sync_both": "Dwukierunkowa",
        "sqlite_sync_manual": "RÄ™czna (decyduj o kaĹĽdej zmianie)",
        "sqlite_conflict_newest": "Wygrywa nowszy zapis",
        "sqlite_conflict_offline": "Wygrywa offline",
        "sqlite_conflict_online": "Wygrywa online",
        "sqlite_conflict_ask": "Pytaj o kaĹĽdÄ… zmianÄ™",
        "sqlite_side_offline": "Offline",
        "sqlite_side_online": "Online",
        "sqlite_side_skip": "PomiĹ„",
        "sqlite_tool_backup": "Wykonaj kopiÄ™",
        "sqlite_tool_restore": "PrzywrĂłÄ‡ kopiÄ™",
        "sqlite_tool_integrity": "Test integralnoĹ›ci",
        "sqlite_tool_repair": "Napraw (REINDEX + VACUUM)",
        "sqlite_tool_compact": "Zapisz zagÄ™szczonÄ… kopiÄ™",
        "sqlite_tool_import": "Importuj plik",
        "sqlite_tool_export": "Eksportuj bazÄ™",
        "sqlite_tool_duplicates": "ZnajdĹş duplikaty",
        "sqlite_tool_validate": "SprawdĹş tabelÄ™",
        "sqlite_tool_stats": "Statystyki tabeli",
        "sqlite_backup_keep": "Kopii do zachowania:",
        "sqlite_backup_done": "Zapisano kopiÄ™: {path} (zachowano {keep})",
        "sqlite_restore_confirm": "ZastÄ…piÄ‡ otwartÄ… bazÄ™ tÄ… kopiÄ…?\n\n{path}\n\nKopia bezpieczeĹ„stwa bieĹĽÄ…cego pliku zostanie zachowana.",
        "sqlite_restored": "PrzywrĂłcono. Kopia bezpieczeĹ„stwa: {path}",
        "sqlite_imported": "Zaimportowano {rows} rekordĂłw do {table}.",
        "sqlite_exported": "Wyeksportowano {count} tabel do {path}.",
        "sqlite_report_saved": "Zapisano raport: {path}",
        "sqlite_log_filter": "Filtruj log...",
        "sqlite_log_refresh": "OdĹ›wieĹĽ",
        "sqlite_log_save": "Zapisz log",
        "sqlite_log_count": "{count} wpisĂłw w logu.",
        "sqlite_log_saved": "Zapisano log: {path}",
        "sqlite_set_folder": "DomyĹ›lny katalog",
        "sqlite_set_page": "RekordĂłw na stronÄ™",
        "sqlite_set_backup": "Kopia przed zmianÄ… schematu",
        "sqlite_set_readonly": "Otwieraj nowe bazy tylko do odczytu",
        "sqlite_set_online_base": "Adres serwera online",
        "sqlite_set_online_api": "ĹšcieĹĽka API",
        "sqlite_settings_note": "Ustawienia decydujÄ…, z czym startuje zakĹ‚adka. SÄ… trzymane w pamiÄ™ci tej sesji; zakĹ‚adka nie zapisuje ich nigdzie bez pytania.",
        "sqlite_settings_applied": "Ustawienia zastosowane.",
        "sqlite_settings_reset": "Ustawienia przywrĂłcone.",
        "sqlite_fmt_sqlite": "Baza SQLite",
        "sqlite_fmt_json": "Dokument JSON",
        "sqlite_fmt_csv": "Tabela CSV",
        "sqlite_fmt_text": "Linie tekstu",
        "vault_create": "UtwĂłrz sejf...",
        "vault_unlock": "Odblokuj...",
        "vault_lock": "Zablokuj",
        "vault_change_password": "ZmieĹ„ hasĹ‚o...",
        "vault_policy": "Polityka bezpieczeĹ„stwa...",
        "vault_open_dir": "OtwĂłrz katalog sejfu",
        "vault_add": "Dodaj",
        "vault_edit": "Edytuj",
        "vault_duplicate": "Kopiuj",
        "vault_move": "PrzenieĹ›...",
        "vault_delete": "UsuĹ„",
        "vault_flags_menu": "Oznaczenia...",
        "vault_hidden_mode": "Tryb ukryty",
        "vault_readonly_lock": "Blokada tylko do odczytu",
        "vault_import": "Importuj...",
        "vault_export": "Eksportuj...",
        "vault_reports_menu": "Raporty...",
        "vault_integrate_menu": "WyĹ›lij do...",
        "vault_search": "Szukaj",
        "vault_search_clear": "WyczyĹ›Ä‡",
        "vault_close": "Zamknij",
        "vault_copy": "Kopiuj",
        "vault_select_first": "Najpierw wybierz zasĂłb.",
        "vault_add_title": "Dodaj zasĂłb",
        "vault_edit_title": "Edytuj zasĂłb",
        "vault_move_to": "PrzenieĹ› do sekcji:",
        "vault_flag_pinned": "PrzypiÄ™ty",
        "vault_flag_critical": "Krytyczny",
        "vault_flag_hidden": "Ukryty",
        "vault_flag_readonly": "Tylko odczyt",
        "vault_sec_scripts": "Skrypty",
        "vault_sec_documents": "Dokumenty",
        "vault_sec_files": "Pliki",
        "vault_sec_passwords": "HasĹ‚a",
        "vault_sec_snapshots": "Snapshoty",
        "vault_search_name": "Po nazwie",
        "vault_search_content": "Po treĹ›ci",
        "vault_search_tags": "Po tagach",
        "vault_search_type": "Po typie",
        "vault_search_date": "Po dacie",
        "vault_search_meta": "Po metadanych",
        "vault_search_all": "Wszystkie aspekty",
        "vault_search_hint": "Szukaj w sejfie...",
        "vault_search_empty": "Pokazano wszystko.",
        "vault_search_found": "Znaleziono {count} zasobĂłw.",
        "vault_col_name": "Nazwa",
        "vault_col_flags": "Oznaczenia",
        "vault_col_tags": "Tagi",
        "vault_col_size": "Rozmiar",
        "vault_col_modified": "Zmieniono",
        "vault_counts": "{shown} widocznych / {total} zapisanych",
        "vault_state_locked": "ZABLOKOWANY",
        "vault_state_unlocked": "ODBLOKOWANY",
        "vault_state_hidden": "TRYB UKRYTY",
        "vault_state_readonly": "TYLKO ODCZYT",
        "vault_idle": "bezczynny {time}",
        "vault_new": "Nowy sejf. Ustaw hasĹ‚o gĹ‚Ăłwne.",
        "vault_created": "Sejf utworzony.",
        "vault_rekey_warning": "Sejf ma juĹĽ hasĹ‚o gĹ‚Ăłwne. Zmiana spowoduje ponowne zaszyfrowanie wszystkiego. KontynuowaÄ‡?",
        "vault_unlocked": "Sejf odblokowany.",
        "vault_locked": "zablokowany",
        "vault_locked_msg": "Sejf zablokowany.",
        "vault_password_changed": "HasĹ‚o gĹ‚Ăłwne zmienione.",
        "vault_no_password": "HasĹ‚o gĹ‚Ăłwne nie jest jeszcze ustawione.",
        "vault_bad_password": "NieprawidĹ‚owe hasĹ‚o.",
        "vault_store_mismatch": "HasĹ‚a sejfu i magazynu rĂłĹĽniÄ… siÄ™.",
        "vault_locked_action": "Zablokowany. Odblokuj sejf, aby {action}.",
        "vault_autolocked": "Sejf zablokowaĹ‚ siÄ™ po bezczynnoĹ›ci.",
        "vault_new_password": "Wybierz nowe hasĹ‚o gĹ‚Ăłwne",
        "vault_policy_note": "Auto-blokada usuwa klucz z pamiÄ™ci po bezczynnoĹ›ci. Auto-zniszczenie usuwa materiaĹ‚ klucza po przekroczeniu limitu blokady.",
        "vault_policy_save": "Zastosuj",
        "vault_policy_saved": "Polityka zastosowana ({count} zmian).",
        "vault_policy_autolock": "Auto-blokada (sekundy)",
        "vault_policy_attempts": "Dozwolone bĹ‚Ä™dne prĂłby",
        "vault_policy_lockout": "Blokada (sekundy)",
        "vault_policy_2fa": "Wymagaj 2FA",
        "vault_policy_double": "PodwĂłjne szyfrowanie",
        "vault_policy_otk": "Klucze jednorazowe",
        "vault_policy_destroy": "Auto-zniszczenie po blokadzie",
        "vault_policy_intrusion": "Wykrywanie wĹ‚amaĹ„",
        "vault_policy_manipulation": "Wykrywanie manipulacji",
        "vault_hidden_on": "Tryb ukryty wĹ‚Ä…czony",
        "vault_hidden_off": "Tryb ukryty wyĹ‚Ä…czony",
        "vault_readonly_on": "Blokada tylko do odczytu wĹ‚Ä…czona",
        "vault_readonly_off": "Blokada tylko do odczytu wyĹ‚Ä…czona",
        "vault_readonly_blocked": "Blokada tylko do odczytu jest wĹ‚Ä…czona. WyĹ‚Ä…cz jÄ… najpierw.",
        "vault_item_readonly": "Ten zasĂłb jest oznaczony jako tylko do odczytu.",
        "vault_added": "Dodano {name}.",
        "vault_updated": "Zaktualizowano {name}.",
        "vault_moved": "Przeniesiono {count} zasobĂłw.",
        "vault_delete_confirm": "UsunÄ…Ä‡ {count} zasobĂłw? Tej operacji nie moĹĽna cofnÄ…Ä‡.\n\n{names}",
        "vault_deleted": "UsuniÄ™to {count} zasobĂłw.",
        "vault_flags_set": "{flag}: {state} dla {count} zasobĂłw",
        "vault_analysis": "Analiza",
        "vault_analysis_structure": "Struktura dokumentu",
        "vault_analysis_integrity": "IntegralnoĹ›Ä‡ skryptu",
        "vault_analysis_risk": "Ryzyko bezpieczeĹ„stwa",
        "vault_analysis_changes": "Historia zmian",
        "vault_report_security": "Raport bezpieczeĹ„stwa",
        "vault_report_integrity": "Raport integralnoĹ›ci",
        "vault_report_access": "Raport dostÄ™pu",
        "vault_report_changes": "Raport zmian",
        "vault_report_risk": "Raport ryzyka",
        "vault_report_saved": "Zapisano raport: {path}",
        "vault_int_editor": "OtwĂłrz w edytorze",
        "vault_int_debugger": "OtwĂłrz w debuggerze",
        "vault_int_runner": "Uruchom w sandboxie",
        "vault_int_audit": "OtwĂłrz w audycie",
        "vault_int_reports": "Wygeneruj raport",
        "vault_int_diff": "PorĂłwnaj w Diff",
        "vault_int_snapshots": "UtwĂłrz snapshot",
        "vault_sent": "WysĹ‚ano {name} do {target}.",
        "vault_target_unavailable": "{target} jest niedostÄ™pny.",
        "vault_import_filter": "Eksporty sejfu i tekst (*.json *.txt *.md *.py *.cfg *.ini);;Wszystkie pliki (*.*)",
        "vault_imported": "Zaimportowano {count} zasobĂłw, zaszyfrowane.",
        "vault_export_vault": "Eksportuj sejf (ponowna autoryzacja)",
        "vault_export_section": "Eksportuj otwartÄ… sekcjÄ™ (wewnÄ…trz sejfu)",
        "vault_export_snapshots": "Eksportuj snapshoty",
        "vault_export_config": "Eksportuj konfiguracjÄ™ sejfu",
        "vault_export_confirm": "PotwierdĹş hasĹ‚o gĹ‚Ăłwne, aby eksportowaÄ‡ poza sejf",
        "vault_exported": "Wyeksportowano {count} zasobĂłw do {path}.",
        "vault_snapshots_exported": "Wyeksportowano {count} snapshotĂłw.",
        "vault_config_exported": "Konfiguracja zapisana w {path}.",
        "vault_snapshot": "Snapshot sejfu",
        "vault_snapshot_take": "UtwĂłrz snapshot",
        "vault_snapshot_note": "DomyĹ›lnie wszystkie sekcje sÄ… uwzglÄ™dnione. Odznaczenie ktĂłrejkolwiek wymaga ponownego podania hasĹ‚a gĹ‚Ăłwnego.",
        "vault_snapshot_confirm": "CzÄ™Ĺ›ciowy snapshot: potwierdĹş hasĹ‚o gĹ‚Ăłwne",
        "vault_snapshot_empty": "Wybierz co najmniej jednÄ… sekcjÄ™.",
        "vault_snapshot_taken": "Zapisano snapshot: {path}",
        "vault_restore_refused": "Tego snapshotu nie moĹĽna przywrĂłciÄ‡.",
        "vault_restored": "PrzywrĂłcono {count} zasobĂłw.",
        "vault_folder_opened": "Otwarto {path}.",
        "editor_codegen_menu": "Generator kodu...",
        "editor_profiles_menu": "Profile skryptĂłw...",
        "profile_cli": "Skrypt CLI (wiersz poleceĹ„)",
        "profile_service": "Skrypt serwisowy",
        "profile_automation": "Skrypt automatyzacji",
        "profile_test": "Skrypt testowy",
        "profile_utility": "Skrypt narzÄ™dziowy",
        "profile_module": "Skrypt moduĹ‚owy",
        "profile_api": "Skrypt API",
        "profiles_description": "Wygenerowany skrypt.",
        "profiles_applied": "{name}: {summary}",
        "profiles_failed": "Generowanie nie powiodĹ‚o siÄ™: {error}",
        "profiles_unknown": "Nieznany profil.",
        "codegen_kind_config": "Konfiguracja -> inicjalizacja",
        "codegen_kind_metadata": "Metadane -> moduĹ‚ projektu",
        "codegen_kind_json": "JSON/YAML -> klasy danych",
        "codegen_kind_api": "API -> klient",
        "codegen_kind_modules": "ModuĹ‚y -> pakiet",
        "codegen_kind_project": "Projekt -> szkielet",
        "codegen_source_file": "Wybierz plik ĹşrĂłdĹ‚owy",
        "codegen_source_filter": "Definicje (*.json *.yaml *.yml *.ini *.env *.txt);;Wszystkie pliki (*.*)",
        "codegen_paste": "Wklej dane wejĹ›ciowe dla {name}",
        "codegen_use_buffer": "UĹĽyj zawartoĹ›ci edytora",
        "codegen_project_source": "WziÄ…Ä‡ projekt z katalogu na dysku?",
        "codegen_file": "Plik",
        "codegen_save_all": "Zapisz pliki...",
        "codegen_insert_first": "Wstaw pierwszy plik",
        "codegen_saved": "zapisano {count} plik(Ăłw) w {root}",
        "codegen_done": "wygenerowano {name} ({count} plik(Ăłw)): {notes}",
        "codegen_failed": "Generowanie nie powiodĹ‚o siÄ™: {error}",
        "codegen_unknown": "Nieznany generator.",
        "editor_coderules_menu": "ReguĹ‚y kodu...",
        "coderules_fix_imports": "Dodaj brakujÄ…ce / usuĹ„ nieuĹĽywane importy",
        "coderules_format": "Normalizuj formatowanie",
        "coderules_indentation": "Popraw wciÄ™cia",
        "coderules_add_return": "Dodaj brakujÄ…ce return",
        "coderules_add_break": "Dodaj brakujÄ…ce break",
        "coderules_add_except": "Dodaj brakujÄ…ce except",
        "coderules_add_docstring": "Dodaj brakujÄ…ce docstringi",
        "coderules_add_types": "Dodaj brakujÄ…ce typy",
        "coderules_add_config": "Dodaj brakujÄ…ce sekcje konfiguracji",
        "coderules_fix_style": "Popraw pozostaĹ‚e kwestie stylu",
        "coderules_fix_all": "Uruchom wszystkie reguĹ‚y",
        "coderules_lint": "Tylko raport (bez zmian)",
        "coderules_report": "Raport reguĹ‚",
        "coderules_applied": "{name}: {notes}",
        "coderules_nothing": "{name}: brak zmian do zrobienia",
        "coderules_all_applied": "zastosowano {count} reguĹ‚: {names}",
        "coderules_none_applied": "ĹĽadna reguĹ‚a nie zmieniĹ‚a bufora",
        "coderules_clean": "wszystkie reguĹ‚y sÄ… speĹ‚nione",
        "coderules_unknown": "Nieznana reguĹ‚a.",
        "template_cat_function": "Szablony funkcji",
        "template_cat_class": "Szablony klas",
        "template_cat_module": "Szablony moduĹ‚Ăłw",
        "template_cat_project": "Szablony projektĂłw",
        "template_cat_config": "Szablony konfiguracji",
        "template_cat_test": "Szablony testĂłw",
        "template_cat_documentation": "Szablony dokumentacji",
        "template_cat_starter": "Szablony skryptĂłw startowych",
        "template_cat_block": "Powtarzalne bloki",
        "editor_template_inserted": "wstawiono szablon: {name}",
        "editor_template_unknown": "Nieznany szablon.",
        "editor_workspace_menu": "System plikĂłw i bezpieczeĹ„stwo...",
        "editor_workspace_tree": "Drzewo projektu",
        "editor_workspace_open": "OtwĂłrz plik",
        "editor_workspace_recent_files": "Ostatnio uĹĽywane pliki",
        "editor_workspace_recent_projects": "Ostatnio uĹĽywane projekty",
        "editor_workspace_snapshot": "UtwĂłrz snapshot projektu",
        "editor_workspace_file_history": "Historia zmian pliku",
        "editor_workspace_project_history": "Historia zmian projektu",
        "editor_workspace_path": "ĹšcieĹĽka",
        "editor_workspace_choose": "Wybierz:",
        "editor_workspace_empty": "Brak ostatnich wpisĂłw.",
        "editor_workspace_snapshot_done": "zapisano snapshot: {path}",
        "editor_security_sandbox": "Uruchom w sandboxie",
        "editor_security_readonly": "PrzeĹ‚Ä…cz blokadÄ™ tylko do odczytu",
        "editor_security_hash": "PokaĹĽ podpis hash",
        "editor_security_verify": "Zweryfikuj nieautoryzowane zmiany",
        "editor_security_access": "Audyt dostÄ™pu do pliku",
        "editor_security_sandbox_info": "PodglÄ…d sandboxa jest statyczny; wykonanie nadal uĹĽywa skonfigurowanego runnera.",
        "editor_security_readonly_state": "tylko do odczytu: {state}",
        "editor_security_hash_value": "SHA-256: {hash}",
        "editor_security_verify_state": "integralnoĹ›Ä‡: {state}",
        "search_options": "Opcje",
        "search_scope": "Zakres:",
        "search_in_all": "Wszystkie pola",
        "search_ignore_case": "Ignoruj wielko\u015b\u0107 liter",
        "search_regex": "Regex",
        "search_whole_word": "Ca\u0142e s\u0142owa",
        "search_favorites_only": "Tylko ulubione",
        "search_sort": "Sortuj:",
        "sort_relevance": "Trafno\u015b\u0107",
        "sort_name": "Nazwa",
        "sort_date": "Najnowsze",
        "sort_category": "Kategoria",
        "search_clear": "Wyczy\u015b\u0107",
        "search_found": "Znaleziono {count} pasuj\u0105cych skrypt\u00f3w.",
        "search_bad_regex": "Nieprawid\u0142owe wyra\u017cenie regularne",
        "search_score": "Ocena trafno\u015bci: {score}",
        "search_copy_results": "Kopiuj list\u0119 wynik\u00f3w",
        "search_export": "Eksportuj wyniki do JSON",
        'search_refresh': 'OdĹ›wieĹĽ',
        'search_count': '{count} skrypt(Ăłw) do przeszukania',
        'search_ready': 'Wybierz rodzaj i uruchom wyszukiwanie.',
        'search_jumped': 'otwarto {doc} w linii {line}',
        'search_detail': 'SzczegĂłĹ‚y',
        'search_nothing_export': 'najpierw uruchom wyszukiwanie',
        'search_scope_database': 'CaĹ‚a baza',
        'search_scope_selection': 'Wybrany skrypt',
        'search_scope_editor': 'Otwarte w edytorze',
        'search_scope_set': 'zakres: {name}',
        "todo_calendar": "Kalendarz",
        "todo_tasks_tab": "Zadania",
        "todo_editor_tab": "Edytor",
        "todo_plan_tab": "Plan",
        "todo_notes_tab": "Notatki",
        "todo_expenses_tab": "Wydatki",
        "todo_calculator": "Kalkulator",
        "todo_close_window": "Zamknij okno",
        "todo_window_hint": "To osobne okno: przeciagnij je gdziekolwiek lub wcisnij Ctrl+T, aby je ukryc.",
        "todo_notes_search": "Szukaj notatek...",
        "todo_notes_new": "+ Notatka",
        "todo_notes_delete": "Usun",
        "todo_notes_title": "Tytul notatki",
        "todo_notes_body": "Napisz notatke...",
        "todo_notes_save": "Zapisz notatke",
        "todo_notes_untitled": "Bez tytulu",
        "todo_expense_description": "Opis",
        "todo_expense_add": "+ Dodaj",
        "todo_expense_remove": "Usun wybrane",
        "about": "O programie",
        "about_title": "O programie ScriptDB",
        "about_version": "2.7.0",
        "about_author": "Autor",
        "about_company": "Firma",
        "about_email": "E-mail",
        "favorite_col": "Ulubiony",
        "menu_add_script": "Dodaj skrypt",
        "menu_separators": "Separatory",
        "menu_columns": "Kolumny",
        "sep_add": "Dodaj separator...",
        "sep_add_top": "Dodaj na g\u00f3rze",
        "sep_add_bottom": "Dodaj na dole",
        "sep_add_before_first": "Dodaj przed pierwszym skryptem",
        "sep_edit": "Edytuj separator",
        "sep_rename": "Zmie\u0144 nazw\u0119 separatora...",
        "sep_remove": "Usu\u0144 separator",
        "sep_prompt": "Etykieta separatora (puste = zwyk\u0142a linia):",
        "sep_unnamed": "(separator bez nazwy)",
        "sep_hide_all": "Ukryj separatory",
        "sep_clear_all": "Usu\u0144 wszystkie separatory",
        "sep_confirm_clear": "Usun\u0105\u0107 wszystkie separatory ({count})?",
        "sep_default_label": "Domy\u015blna etykieta nowych separator\u00f3w...",
        "sep_move_up": "Przenie\u015b separator w g\u00f3r\u0119",
        "sep_move_down": "Przenie\u015b separator w d\u00f3\u0142",
        "configure_interpreters": "Konfiguruj interpretery...",
        "interpreters_title": "Konfiguracja interpreter\u00f3w skrypt\u00f3w",
        "interpreters_hint": "Ustaw niestandardowe \u015bcie\u017cki do interpreter\u00f3w skrypt\u00f3w. Pozostaw puste, aby u\u017cy\u0107 domy\u015blnych z systemu.",
        "interpreter_path": "\u015acie\u017cka interpretera",
        "browse": "Przegl\u0105daj...",
        "interpreters_saved": "Konfiguracja interpreter\u00f3w zapisana.",
        "sep_hidden_note": "Separatory s\u0105 ukryte. W\u0142\u0105cz je w menu pustego obszaru.",
        "col_favorite": "Poka\u017c kolumn\u0119 ulubionych",
        "col_row_numbers": "Poka\u017c numery wierszy",
        "col_reset": "Przywr\u00f3\u0107 kolumny",
        "group_by_category": "Grupuj po kategorii",
        "zebra_stripes": "Naprzemienne kolory wierszy",
        "bold_separators": "Pogrubione etykiety separator\u00f3w",
        "list_expand_all": "Rozwi\u0144 wszystko",
        "list_collapse_all": "Zwi\u0144 wszystko",
        "list_reset_view": "Przywr\u00f3\u0107 widok listy",
        "tree_view": "Widok drzewa (zgrupowane)",
        "list_view": "Widok listy (płaski)",
        "diff": "Por\u00f3wnanie",
        "about_github": "GitHub",
        "diff_identical": "(obie strony s\u0105 identyczne)",
        "diff_open_file": "Otw\u00f3rz plik",
        "diff_left_dir": "Wybierz folder lewy",
        "diff_right_dir": "Wybierz folder prawy",
        "diff_empty_stdin": "Brak danych na stdin.",
        "diff_recent": "Ostatnie por\u00f3wnania",
        "diff_need_two_files": "Upu\u015b\u0107 dwa pliki, aby je por\u00f3wna\u0107.",
        "diff_changes": "Zmiany",
        "diff_show": "Poka\u017c:",
        "diff_filter_added": "Dodane",
        "diff_filter_removed": "Usuni\u0119te",
        "diff_filter_changed": "Zmienione",
        "diff_filter_symbols": "W funkcjach",
        "diff_collapse": "Zwi\u0144 bez zmian",
        "diff_low_graphics": "Tryb ASCII",
        "diff_search": "Szukaj w r\u00f3\u017cnicach...",
        "diff_prev_hit": "Poprzednie",
        "diff_next_hit": "Nast\u0119pne",
        "diff_view_analysis": "Analiza",
        "diff_stats_title": "Statystyki porĂłwnania",
        "diff_export": "Eksportuj raport",
        "diff_diagnostics": "Diagnostyka (.json)",
        "diff_report_name": "raport_diff",
        "diff_export_menu": "Eksport...",
        "diff_export_patch": "Patch (.patch)",
        "diff_export_json": "JSON (.json)",
        "diff_export_txt": "Raport TXT (.txt)",
        "diff_new_session": "Nowa sesja",
        "diff_close_session": "Zamknij sesj\u0119",
        "diff_modes_menu": "\U0001f9ea Tryby...",
        "diff_mode_merge3": "Diff tr\u00f3jstronny (merge)",
        "diff_mode_merge4": "Diff czterostronny (merge zaawansowany)",
        "diff_mode_live": "Diff live (plik vs wersja w pami\u0119ci)",
        "diff_mode_sysrest": "Diff konfiguracji systemu",
        "diff_mode_env": "Diff \u015brodowisk (env1 vs env2)",
        "diff_mode_hex": "Diff binarny z widokiem hex",
        "diff_merge_base": "Baza (przodek):",
        "diff_merge_left": "Lewy:",
        "diff_merge_right": "Prawy:",
        "diff_merge_ours": "Nasze:",
        "diff_merge_theirs": "Ich:",
        "diff_merge_result": "Wynik scalenia:",
        "diff_merge_run": "Scal",
        "diff_merge_conflict": "Konflikty wymagaj\u0105ce decyzji: {count}.",
        "diff_merge_clean": "Scalenie czyste (brak konflikt\u00f3w).",
        "diff_merge_take_left": "We\u017a lewy",
        "diff_merge_take_right": "We\u017a prawy",
        "diff_merge_take_both": "We\u017a oba",
        "diff_live_refresh": "Od\u015bwie\u017c",
        "diff_live_auto": "Auto (1s)",
        "diff_live_watch": "Obserwuj plik",
        "diff_live_no_file": "Nie wczytano pliku do diffu live.",
        "diff_live_memory": "Bufor w pami\u0119ci (lewy)",
        "diff_live_file": "Plik: {name}",
        "diff_sysrest_run": "Zbierz konfiguracj\u0119 systemu",
        "diff_sysrest_hint": "Zbiera ustawienia systemu, \u015brodowiska, sprz\u0119tu i sieci do por\u00f3wnania.",
        "diff_env_run": "Zbierz zmienne \u015brodowiska",
        "diff_env_hint": "Zapisuje bie\u017c\u0105ce \u015brodowisko. Por\u00f3wnaj env1 (lewy) z env2 (prawy).",
        "diff_hex_offset": "Offset",
        "diff_hex_bytes": "Bajty",
        "diff_hex_ascii": "ASCII",
        "diff_hex_side": "Strona",
        "diff_hex_hint": "Por\u00f3wnanie bajtowe obu plik\u00f3w z widokiem hex + ASCII.",
        "diff_hex_no_file": "Nie wczytano pliku do diffu hex.",
                "todo": "Zadania",
        "todo_title": "ZADANIA",
        "todo_subtitle": "Zadania, kalendarz i przypomnienia",
        "todo_unavailable": "Zakladka Zadania jest niedostepna",
        "todo_badge_hint": "{due} na dzis, {late} zaleglych (Ctrl+T)",
        "todo_search": "Szukaj zadan...",
        "todo_view_today": "Dzis",
        "todo_view_day": "Dzien",
        "todo_view_week": "Tydzien",
        "todo_view_month": "Miesiac",
        "todo_view_year": "Rok",
        "todo_prev": "Poprzedni",
        "todo_next": "Nastepny",
        "todo_today": "Dzis",
        "todo_add_task": "+ Zadanie",
        "todo_tools": "Narzedzia",
        "todo_new": "Nowe zadanie",
        "todo_new_on_day": "Nowe zadanie w pokazanym dniu...",
        "todo_templates": "Z szablonu",
        "todo_add": "Dodaj",
        "todo_save": "Zapisz zadanie",
        "todo_duplicate": "Duplikuj",
        "todo_delete": "Usun",
        "todo_close": "Zamknij",
        "todo_mark_done": "Oznacz jako zrobione",
        "todo_done": "Zrobione",
        "todo_reopen": "Wznow",
        "todo_edit": "Edytuj",
        "todo_list": "Zadania",
        "todo_untitled": "bez tytulu",
        "todo_warning": "Uwaga",
        "todo_need_title": "Zadanie wymaga tytulu.",
        "todo_editing": "Edytujesz: {title}",
        "todo_free_day": "nic zaplanowane",
        "todo_now_line": "teraz {when}",
        "todo_untimed": "Bez godziny",
        "todo_nothing_today": "Na dzis nic nie jest zaplanowane.",
        "todo_section_late": "Zalegle",
        "todo_section_today": "Na dzis",
        "todo_section_doing": "W toku",
        "todo_section_soon": "Najblizsze siedem dni",
        "todo_field_title": "Tytul",
        "todo_field_project": "Projekt",
        "todo_field_kind": "Rodzaj",
        "todo_field_due": "Termin",
        "todo_field_start": "Start",
        "todo_field_remind": "Przypomnij",
        "todo_field_repeat": "Powtarzaj",
        "todo_field_tags": "Tagi",
        "todo_field_colour": "Kolor",
        "todo_field_notes": "Notatki",
        "todo_field_links": "Odwolania",
        "todo_field_checklist": "Lista krokow",
        "todo_tags_hint": "blad, ui, pilne",
        "todo_link_hint": "sciezka, URL, nazwa skryptu lub zgloszenie",
        "todo_check_hint": "jeden krok",
        "todo_quick_due": "Szybki termin",
        "todo_quick_today": "Dzis 17:00",
        "todo_quick_tomorrow": "Jutro 09:00",
        "todo_quick_week": "Za tydzien 09:00",
        "todo_quick_monday": "W poniedzialek 09:00",
        "todo_auto": "automatyczny",
        "todo_every": "co",
        "todo_unit_day": "dzien",
        "todo_unit_week": "tydzien",
        "todo_unit_month": "miesiac",
        "todo_unit_year": "rok",
        "todo_remind_at": "o czasie",
        "todo_remind_min": "{count} min przed",
        "todo_remind_hour": "{count} godz. przed",
        "todo_remind_day": "{count} dni przed",
        "todo_kind_task": "Zadanie",
        "todo_kind_code": "Kod",
        "todo_kind_bug": "Blad",
        "todo_kind_review": "Przeglad",
        "todo_kind_research": "Analiza",
        "todo_kind_meeting": "Spotkanie",
        "todo_kind_release": "Wydanie",
        "todo_kind_admin": "Administracja",
        "todo_kind_idea": "Pomysl",
        "todo_priority_critical": "Krytyczny",
        "todo_priority_high": "Wysoki",
        "todo_priority_normal": "Normalny",
        "todo_priority_low": "Niski",
        "todo_status_todo": "Do zrobienia",
        "todo_status_doing": "W toku",
        "todo_status_blocked": "Zablokowane",
        "todo_status_done": "Zrobione",
        "todo_status_cancelled": "Anulowane",
        "todo_repeat_none": "Nigdy",
        "todo_repeat_daily": "Codziennie",
        "todo_repeat_weekdays": "Dni robocze",
        "todo_repeat_weekly": "Co tydzien",
        "todo_repeat_biweekly": "Co dwa tygodnie",
        "todo_repeat_monthly": "Co miesiac",
        "todo_repeat_quarterly": "Co kwartal",
        "todo_repeat_yearly": "Co rok",
        "todo_repeat_custom": "Wlasny...",
        "todo_filter_all": "Wszystkie otwarte",
        "todo_filter_status_doing": "W toku",
        "todo_filter_status_blocked": "Zablokowane",
        "todo_filter_priority_critical": "Krytyczne",
        "todo_filter_priority_high": "Wysokie",
        "todo_filter_kind_bug": "Bledy",
        "todo_filter_kind_code": "Kod",
        "todo_filter_kind_review": "Przeglady",
        "todo_filter_kind_meeting": "Spotkania",
        "todo_filter_done": "Zakonczone",
        "todo_all_projects": "Wszystkie projekty",
        "todo_all_tags": "Wszystkie tagi",
        "todo_set_status": "Ustaw status",
        "todo_set_priority": "Ustaw priorytet",
        "todo_mark_todo": "Oznacz jako do zrobienia",
        "todo_snooze": "Odloz",
        "todo_snooze_for": "Odloz o {what}",
        "todo_snooze_10": "Odloz o 10 min",
        "todo_start_working": "Zacznij nad tym pracowac",
        "todo_move": "Przenies na dzien",
        "todo_move_0": "Dzis",
        "todo_move_1": "Jutro",
        "todo_move_7": "+1 tydzien",
        "todo_move_30": "+1 miesiac",
        "todo_copy": "Kopiuj jako tekst",
        "todo_selected": "wybrano {count} zadan",
        "todo_send_docs": "Wyslij do DOCs",
        "todo_send_editor": "Otworz w Edytorze",
        "todo_saved": "zapisano",
        "todo_completed": "zakonczone",
        "todo_reopened": "wznowione",
        "todo_completed_n": "zakonczono {count} zadan",
        "todo_deleted_n": "usunieto {count}",
        "todo_deleted": "usunieto",
        "todo_delete_ask": "Usunac to zadanie?",
        "todo_snoozed": "odlozone do {when}",
        "todo_moved": "przeniesione na {when}",
        "todo_copied": "skopiowano {count} zadan",
        "todo_clear_done": "Usun zakonczone zadania",
        "todo_clear_done_ask": "Usunac {count} zakonczonych zadan?",
        "todo_nothing_completed": "brak zakonczonych zadan",
        "todo_cleared": "usunieto {count} zadan",
        "todo_forget": "brak zakonczonych zadan",
        "todo_clear_all": "Usun wszystkie zadania",
        "todo_clear_all_ask": "Usunac wszystkie {count} zadan? Operacji nie mozna cofnac.",
        "todo_nothing_to_clear": "brak zadan do usuniecia",
        "todo_holidays": "Swieta",
        "todo_country_pl": "Polska",
        "todo_country_uk": "Wielka Brytania",
        "todo_show_holidays": "Pokaz swieta w siatce",
        "todo_display": "Wyswietlanie",
        "todo_show_weekends": "Pokaz weekendy",
        "todo_show_week_numbers": "Numery tygodni",
        "todo_show_done": "Pokaz zakonczone zadania",
        "todo_start_monday": "Tydzien zaczyna sie w poniedzialek",
        "todo_work_hours": "Godziny pracy",
        "todo_work_start": "Dzien zaczyna sie o",
        "todo_work_end": "Dzien konczy sie o",
        "todo_holiday_in": "{days} dni do: {name}",
        "todo_review": "Przeglad i statystyki...",
        "todo_review_head": "Przeglad zadan",
        "todo_review_by": "Grupa",
        "todo_review_count": "Liczba",
        "todo_review_project": "Wg projektu",
        "todo_review_kind": "Wg rodzaju",
        "todo_review_priority": "Wg priorytetu",
        "todo_review_day": "Dzien",
        "todo_review_load": "Obciazenie",
        "todo_stat_total": "Zadania",
        "todo_stat_open": "Otwarte",
        "todo_stat_done": "Zrobione",
        "todo_stat_late": "Zalegle",
        "todo_stat_week": "Zrobione w tym tygodniu",
        "todo_worklog": "Ostatnio zakonczone",
        "todo_no_log": "nic jeszcze nie zakonczono",
        "todo_organise": "Sortuj i porzadkuj...",
        "todo_org_overdue": "Przenies wszystkie zalegle na dzis",
        "todo_org_untimed": "Nadaj godzine zadaniom bez godziny",
        "todo_org_project": "Przypisz biezacy projekt do zadan bez projektu",
        "todo_org_dedupe": "Usun duplikaty (ten sam tytul i dzien)",
        "todo_org_empty": "Usun zadania bez tytulu",
        "todo_org_archive": "Zarchiwizuj wszystkie zakonczone",
        "todo_org_note": "Kazda akcja dotyczy calej listy.",
        "todo_org_moved": "przeniesiono {count} zadan",
        "todo_org_touched": "zmieniono {count}",
        "todo_org_removed": "usunieto {count}",
        "todo_org_need_project": "najpierw wybierz filtr projektu",
        "todo_export": "Eksport",
        "todo_export_ics": "Kalendarz (.ics)",
        "todo_export_md": "Raport Markdown (.md)",
        "todo_export_json": "Wszystkie zadania (.json)",
        "todo_export_csv": "Zadania jako CSV (.csv)",
        "todo_import": "Import",
        "todo_import_ics": "Kalendarz (.ics)",
        "todo_imported": "zaimportowano {count}",
        "todo_exported": "zapisano: {path}",
        "todo_report_title": "Raport zadan",
        "todo_report_generated": "Wygenerowano {when}",
        "todo_report_summary": "{count} zadan",
        "todo_notifications": "Przypomnienia i powiadomienia",
        "todo_notify_on": "Pokazuj wyskakujace przypomnienia",
        "todo_notify_test": "Pokaz testowe powiadomienie",
        "todo_notify_history": "Historia powiadomien...",
        "todo_notify_when": "Kiedy",
        "todo_notify_kind": "Rodzaj",
        "todo_notify_what": "Co",
        "todo_notify_enabled": "przypomnienia: {state}",
        "todo_clear_log": "Wyczysc",
        "todo_on": "wlaczone",
        "todo_off": "wylaczone",
        "todo_reminder_title": "Przypomnienie: {title}",
        "todo_test_title": "To jest testowe przypomnienie",
        "todo_test_body": "Tak wygladaja przypomnienia; pokazuja sie nad kazda zakladka.",
        "todo_sum_open": "{count} otwartych",
        "todo_sum_done": "{count} zrobionych",
        "todo_sum_late": "{count} zaleglych",
        "todo_sum_estimate": "{count} min szacunku",
        "todo_from_script": "Praca nad {name}",
        "todo_scratch": "zadanie z listy",
        "todo_docs_category": "Zadania",
        "todo_needs_docs": "zakladka DOCs nie ma otwartej bazy",
        "todo_sent_docs": "zapisano w DOCs",
        "todo_needs_editor": "zakladka Edytor jest niedostepna",
        "todo_link_unresolved": "nie mozna otworzyc: {what}",
        "todo_tpl_code_feature": "Zaimplementuj funkcje",
        "todo_tpl_code_bug": "Napraw blad",
        "todo_tpl_code_review": "Przejrzyj zmiane",
        "todo_tpl_code_refactor": "Refaktoryzuj",
        "todo_tpl_code_test": "Napisz testy",
        "todo_tpl_release": "Wydaj wersje",
        "todo_tpl_deploy": "Wdrozenie",
        "todo_tpl_standup": "Codzienny standup",
        "todo_tpl_review_week": "Przeglad tygodnia",
        "todo_tpl_research": "Analiza",
        "todo_tpl_idea": "Pomysl",
        "todo_calendar": "Kalendarz",
        "todo_tasks": "Zadania",
        "todo_editor": "Edytor",
        "todo_plan": "Plan",
        "todo_notes": "Notatki",
        "todo_expenses": "Wydatki",
        "todo_calculator": "Kalkulator",
        "todo_close_window": "Zamknij okno",
        "todo_window_hint": "To osobne okno: przeciagnij je gdziekolwiek lub wcisnij Ctrl+T, aby je ukryc.",
        "todo_notes_search": "Szukaj notatek...",
        "todo_notes_new": "+ Notatka",
        "todo_notes_delete": "Usun",
        "todo_notes_title": "Tytul notatki",
        "todo_notes_body": "Napisz notatke...",
        "todo_notes_save": "Zapisz notatke",
        "todo_notes_untitled": "Bez tytulu",
        "todo_expense_description": "Opis",
        "todo_expense_add": "+ Dodaj",
        "todo_expense_remove": "Usun wybrane",
        "about_text": (
            """Autor:   Sebastian Januchowski\n"""
            """Firma:   polsoft.ITS\u2122\n"""
            """E-mail:  polsoft.its@mail.com\n"""
            """GitHub:  https://github.com/polsoft-it\n"""
            """ScriptDB {version}\n\n"""
            """Lokalna baza fragment\u00f3w kodu z edytorem z pod\u015bwietlaniem sk\u0142adni, """
            """runnerem skrypt\u00f3w, przegl\u0105dark\u0105 modu\u0142\u00f3w, klientem SQL, narz\u0119dziami """
            """metadanych oraz widokiem por\u00f3wnania.\n\n"""
            """Skrypty: {scripts}\n"""
            """Zapisane po\u0142\u0105czenia: {connections}\n\n"""
            """Baza skrypt\u00f3w:\n{data}\n\n"""
            """Plik po\u0142\u0105cze\u0144:\n{conn}\n\n"""
            """Zbudowano w Pythonie z u\u017cyciem PyQt5."""
        ),
    },
}


CATEGORIES = [
    "Python",
    "Bash",
    "PowerShell",
    "VBScript",
    "JavaScript",
    "Java",
    "C++",
    "Batch",
    "HTML",
    "HTA",
    "Registry (REG)",
    "JSON",
    "YAML",
    "INI",
    "System config",
    "SQL",
    "Ruby",
    "Other",
]

# Maps a category to the file extension used when a script is saved/loaded as a
# plain source file (import/export in its natural form).
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
# Reverse lookup: file extension (lower-case, with dot) -> category.
EXTENSION_CATEGORIES = {ext: cat for cat, ext in CATEGORY_EXTENSIONS.items()}

# Extra extension aliases that map onto an existing category.
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
    """Generate a file filter string for QFileDialog with all supported extensions.
    
    Returns a string like: "Python files (*.py *.pyw);;Bash files (*.sh);;All files (*.*)"
    """
    # Group extensions by category
    category_to_exts = {}
    for ext, category in EXTENSION_ALIASES.items():
        if category not in category_to_exts:
            category_to_exts[category] = []
        category_to_exts[category].append(ext)
    
    # Build filter strings
    filters = []
    for category in sorted(category_to_exts.keys()):
        exts = sorted(category_to_exts[category])
        # Format: "Category files (*.ext1 *.ext2)"
        filter_str = f"{category} files ({' '.join(exts)})"
        filters.append(filter_str)
    
    # Add "All files" option at the end
    filters.append("All files (*.*)")
    
    # Join with ;;
    return ";;".join(filters)


def get_interpreter_path(language, settings=None):
    """Get the interpreter path for a given language.
    
    First checks custom settings, then falls back to shutil.which.
    Returns None if no interpreter is found.
    
    Args:
        language: Language key (e.g., "python", "bash", "powershell")
        settings: Settings dict (optional, if None will try to load from SETTINGS_FILE)
    
    Returns:
        Path to interpreter executable or None
    """
    # Normalize language key
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
    
    # Check custom settings first
    if settings is None:
        try:
            settings = load_settings(SETTINGS_FILE, None)
        except:
            settings = {}
    
    interpreters = settings.get("interpreters", {})
    custom_path = interpreters.get(normalized, "")
    if custom_path and os.path.exists(custom_path):
        return custom_path
    
    # Fall back to system PATH
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


TEMPLATES = {
    "Python": (
        "Python - Hello World",
        "#!/usr/bin/env python3\n# -*- coding: utf-8 -*-\n\ndef main():\n    print('Hello, World!')\n\nif __name__ == '__main__':\n    main()\n",
    ),
    "Bash": (
        "Bash - Hello World",
        "#!/usr/bin/env bash\nset -euo pipefail\n\necho \"Hello, World!\"\n",
    ),
    "PowerShell": (
        "PowerShell - Hello World",
        "Write-Host 'Hello, World!'\n",
    ),
    "JavaScript": (
        "JavaScript - Hello World",
        "console.log('Hello, World!');\n",
    ),
    "Java": (
        "Java - Hello World",
        "public class Main {\n    public static void main(String[] args) {\n        System.out.println(\"Hello, World!\");\n    }\n}\n",
    ),
    "C++": (
        "C++ - Hello World",
        "#include <iostream>\n\nint main() {\n    std::cout << \"Hello, World!\" << std::endl;\n    return 0;\n}\n",
    ),
    "Batch": (
        "Batch - Hello World",
        "@echo off\nsetlocal\n\necho Hello, World!\n",
    ),
    "HTML": (
        "HTML - Starter Page",
        "<!DOCTYPE html>\n<html>\n<head>\n <meta charset=\"UTF-8\">\n <title>Hello</title>\n</head>\n<body>\n <h1>Hello, World!</h1>\n</body>\n</html>\n",
    ),
    "JSON": (
        "JSON - Example",
        '{\n  "app": "ScriptDB",\n  "version": "1.0.0",\n  "debug": false\n}\n',
    ),
    "SQL": (
        "SQL - Example",
        "CREATE TABLE IF NOT EXISTS users (\n  id INTEGER PRIMARY KEY AUTOINCREMENT,\n  name TEXT NOT NULL\n);\nINSERT INTO users (name) VALUES ('Alice');\nSELECT * FROM users;\n",
    ),
    "Other": (
        "Other - Note",
        "# Note\n# Created: " + dt.date.today().isoformat() + "\n",
    ),
}


def _load_json_list(path, warnings, kind):
    """Read a JSON file expected to contain a list (kind = noun in warnings)."""
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        if warnings is not None:
            warnings.append(f"Could not read {os.path.basename(path)}: {exc}")
        return []
    if isinstance(data, list):
        return data
    if warnings is not None:
        warnings.append(
            f"{os.path.basename(path)} does not contain a list of {kind} "
            f"(found {type(data).__name__})."
        )
    return []


def _save_json_list(path, data, atomic=True):
    # Atomic write so a crash or a second instance cannot corrupt the database.
    if not atomic:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        return
    directory = os.path.dirname(path) or "."
    fd, temp_path = tempfile.mkstemp(dir=directory, prefix=".sdb-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except Exception:
        try:
            os.remove(temp_path)
        except OSError:
            pass
        raise
class _PathStore:
    # JSON-list store bound to a path, a warning noun and a write mode.
    def __init__(self, path, kind, mode=None):
        self.path = path
        self.kind = kind
        self.mode = mode

    def load(self, warnings=None):
        return _load_json_list(self.path, warnings, self.kind)

    def save(self, data):
        _save_json_list(self.path, data, atomic=True)
        if self.mode is not None:
            try:
                os.chmod(self.path, self.mode)
            except OSError:
                pass


def load_connections(path, warnings=None):
    """Read the saved connections, reporting unreadable files via `warnings`."""
    return _PathStore(path, "connections", mode=0o600).load(warnings)


def save_connections(path, data):
    _PathStore(path, "connections", mode=0o600).save(data)


def load_data(path, warnings=None):
    """Read the script database, reporting unreadable files via `warnings`."""
    return _PathStore(path, "scripts").load(warnings)


def save_data(path, data):
    _PathStore(path, "scripts").save(data)


def is_separator(entry):
    """True when a list entry is a visual separator row, not a script."""
    return bool(isinstance(entry, dict) and entry.get("kind") == SEPARATOR_KIND)


# Monotonic counter guaranteeing unique separator ids even when several
# separators are created inside the same clock tick.
_SEPARATOR_SEQUENCE = [0]


def next_separator_serial():
    _SEPARATOR_SEQUENCE[0] += 1
    return _SEPARATOR_SEQUENCE[0]


def make_separator(label=""):
    """Build a new separator row with a unique id."""
    now = dt.datetime.now()
    return {
        "kind": SEPARATOR_KIND,
        "id": f"sep-{now.timestamp()}-{next_separator_serial()}",
        "name": (label or "").strip(),
        "category": "",
        "tags": "",
        "desc": "",
        "code": "",
        "date": now.strftime("%Y-%m-%d %H:%M:%S"),
    }
def load_settings(path, warnings=None):
    """Read the UI settings dict, falling back to an empty dict."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        if warnings is not None:
            warnings.append(f"Could not read {os.path.basename(path)}: {exc}")
        return {}
    return data if isinstance(data, dict) else {}


def save_settings(path, data):
    _save_json_list(path, data, atomic=True)


# Column keys of the Scripts list, in display order.
LIST_COLUMNS = ("name", "category", "tags", "date", "favorite")

# Default user-interface preferences; merged with the saved values on load.
DEFAULT_SETTINGS = {
    "columns": {key: True for key in LIST_COLUMNS},
    "group_by_category": False,  # Default to list view (flat)
    "separators_enabled": True,
    "show_separators": True,
    "zebra_stripes": True,
    "show_row_numbers": False,
    "bold_separators": True,
    "empty_separator_label": "",
    "interpreters": {
        "python": "",
        "bash": "",
        "powershell": "",
        "node": "",
        "batch": "",
        "vbscript": ""
    }
}


def script_to_dict(script):
    """Return only the meaningful fields of a script entry."""
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


def unified_diff(old_entry, new_entry):
    """Human readable unified diff between two script versions."""
    old_lines = json.dumps(script_to_dict(old_entry), ensure_ascii=False, indent=2, sort_keys=True).splitlines()
    new_lines = json.dumps(script_to_dict(new_entry), ensure_ascii=False, indent=2, sort_keys=True).splitlines()
    return "\n".join(
        difflib.unified_diff(old_lines, new_lines, fromfile="previous", tofile="current", lineterm="")
    )
def _clean_meta(raw):
    # Sanitise an imported metadata dict into known string fields.
    result = {}
    if not isinstance(raw, dict):
        return result
    for key, value in raw.items():
        if value is None:
            continue
        result[str(key)] = value if isinstance(value, (int, float, bool)) else str(value)
    return result


def guess_category(code):
    """Very lightweight language guess used only to preselect a category."""
    text = code.strip()
    if not text:
        return None
    if text.startswith("#!") and ("bash" in text.splitlines()[0] or "sh" in text.splitlines()[0]):
        return "Bash"
    if re.search(r"^\s*<(\?xml|!DOCTYPE html|html)", text, re.IGNORECASE | re.MULTILINE):
        return "HTML"
    if re.search(r"<\s*scriptlanguage|<\s*hta:", text, re.IGNORECASE):
        return "HTA"
    if re.search(r"(Windows Registry Editor|^\s*\[HKEY_)", text, re.IGNORECASE | re.MULTILINE):
        return "Registry (REG)"
    if re.search(r"^\s*(SELECT|INSERT|UPDATE|DELETE|CREATE TABLE|PRAGMA|ALTER TABLE)\b", text, re.IGNORECASE | re.MULTILINE):
        return "SQL"
    if re.search(r"\b(Write-Host|Get-ChildItem|param\s*\(|\$env:)", text):
        return "PowerShell"
    if re.match(r"^\s*@echo\s+off|\b(setlocal|goto\s+:\w+)", text, re.IGNORECASE):
        return "Batch"
    if re.search(r"\b(console\.log|document\.(getElementById|querySelector)|function\s+\w+\s*\()", text):
        return "JavaScript"
    if re.search(r"\b(System\.out\.println|public\s+static\s+void\s+main)\b", text):
        return "Java"
    if re.search(r"#include\s*<|std::", text):
        return "C++"
    if re.match(r"^\s*[\{\[]", text) and text.rstrip().endswith(("}", "]")):
        try:
            json.loads(text)
            return "JSON"
        except Exception:
            pass
    if re.search(r"\b(Option Explicit|Sub\s+\w+|End Sub|WScript\.)", text, re.IGNORECASE):
        return "VBScript"
    if re.search(r"^(import|from|def|class|if __name__)\b", text, re.MULTILINE):
        return "Python"
    return None
def highlight_text(text, category):
    """Return HTML with a very small, safe syntax highlight for a code block."""
    placeholders = []

    def stash(value, css):
        token = f"\x00{len(placeholders)}\x00"
        placeholders.append((token, f'<span style="color:{css}">{escape_html(value)}</span>'))
        return token

    escaped = escape_html(text)
    # Comments first so keywords inside them are not highlighted.
    escaped = re.sub(r"#[^\n]*", lambda m: stash(m.group(0), "#6a9955"), escaped)
    escaped = re.sub(r"//[^\n]*", lambda m: stash(m.group(0), "#6a9955"), escaped)
    escaped = re.sub(r"&quot;.*?&quot;|&#x27;.*?&#x27;", lambda m: stash(m.group(0), "#ce9178"), escaped)
    for pattern in (r"\b\d+(?:\.\d+)?\b",):
        escaped = re.sub(pattern, lambda m: stash(m.group(0), "#b5cea8"), escaped)
    for pattern in (
        r"\b(def|class|import|from|return|if|elif|else|for|while|try|except|finally|with|as|lambda|None|True|False|self)\b",
        r"\b(function|const|let|var|new|typeof|async|await|SELECT|INSERT|UPDATE|DELETE|FROM|WHERE|TABLE|CREATE)\b",
    ):
        escaped = re.sub(pattern, lambda m: stash(m.group(0), "#569cd6"), escaped)
    for token, html in reversed(placeholders):
        escaped = escaped.replace(token, html)
    return f'<pre style="white-space:pre-wrap; margin:0; font-family:Consolas, monospace;">{escaped}</pre>'


def escape_html(text):
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
    )
def normalize_entry(raw):
    """Coerce an imported dict into a valid script entry (or None)."""
    if not isinstance(raw, dict):
        return None
    # Separators belong to the user's own list layout, so keep them as they
    # are instead of running them through the script normalization below.
    if is_separator(raw):
        separator = make_separator(str(raw.get("name", "")))
        separator["id"] = str(raw.get("id") or separator["id"])
        separator["date"] = raw.get("date") or separator["date"]
        return separator
    code = raw.get("code") or ""
    name = (raw.get("name") or "").strip()
    if not name and not code:
        return None
    if not name:
        name = f"Imported script {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    category = raw.get("category") or ""
    if category not in CATEGORIES:
        guessed = guess_category(code)
        if guessed:
            category = guessed
        elif category:
            CATEGORIES.append(category)
        else:
            category = "Other"
    tag_value = raw.get("tags") or ""
    if isinstance(tag_value, (list, tuple)):
        tag_value = ", ".join(str(t) for t in tag_value)
    return {
        "id": str(raw.get("id") or dt.datetime.now().timestamp()),
        "name": name,
        "category": category,
        "tags": str(tag_value).strip(),
        "desc": (raw.get("desc") or "").strip(),
        "code": code,
        "date": raw.get("date") or dt.datetime.now().strftime("%Y-%m-%d"),
        "meta": _clean_meta(raw.get("meta")),
    }


def extension_for_category(category):
    """Return the natural file extension (with dot) for a script category."""
    return CATEGORY_EXTENSIONS.get(category, ".txt")


def category_for_extension(filename):
    """Guess the script category from a file name's extension (or None).

    The canonical table is checked first, then the alias table, so
    secondary spellings such as .yml, .toml or .rake still resolve.
    """
    extension = os.path.splitext(filename)[1].lower()
    category = EXTENSION_CATEGORIES.get(extension)
    if category is not None:
        return category
    return EXTENSION_ALIASES.get(extension)


def safe_file_name(name, default="script"):
    """Turn a script name into a file-name stem safe on every platform."""
    stem = re.sub('[\\/:*?"<>|]+', "_", (name or "").strip()).strip(". ")
    stem = re.sub(r"\s+", "_", stem).strip(".")
    return stem or default


def script_export_path(script, directory):
    """Build a destination path that keeps the script's original extension."""
    ext = extension_for_category(script.get("category", ""))
    return os.path.join(directory, safe_file_name(script.get("name", "")) + ext)
def build_script_from_file(path):
    """Read a plain source file and return a script entry (or None).

    The category is derived from the file extension first (so the file keeps
    its natural form), falling back to the content heuristic.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            code = handle.read()
    except OSError as exc:
        raise OSError(str(exc))
    filename = os.path.basename(path)
    category = category_for_extension(filename) or guess_category(code) or "Other"
    now = dt.datetime.now()
    return {
        "id": str(now.timestamp()),
        "name": os.path.splitext(filename)[0] or filename,
        "category": category,
        "tags": "",
        "desc": f"Imported from {filename}",
        "code": code,
        "date": now.strftime("%Y-%m-%d %H:%M:%S"),
        "date_created": now.strftime("%Y-%m-%d %H:%M:%S"),
        "favorite": False,
        "history": [],
    }


class RunScriptDialog(QtWidgets.QDialog):
    """Modal runner for a script: executes it and shows live output.

    Workers run in a background thread and never touch Qt widgets directly;
    every UI update is marshalled through a thread-safe queue that is drained
    on the GUI thread by a timer. The dialog can stop a running process, shows
    elapsed time and the exit code, and lets the user feed stdin to a script
    that is waiting for input.
    """

    RUN_TIMEOUT_SECONDS = 300
    PUMP_INTERVAL_MS = 40
    ANSI_RESET = "\x1b[0m"
    ANSI_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
    ANSI_COLOURS = {
        30: "#1f1f1f", 31: "#ff7b72", 32: "#9be9a8", 33: "#f7c66d",
        34: "#8ab4f8", 35: "#d2a8ff", 36: "#79c0ff", 37: "#dfe7f5",
        90: "#6e7681", 91: "#ffa198", 92: "#56d364", 93: "#e3b341",
        94: "#79c0ff", 95: "#d2a8ff", 96: "#a5d6ff", 97: "#ffffff",
    }

    def __init__(self, parent, title, script_text, language, translator=None):
        super().__init__(parent)
        self._t = translator or (lambda key, **kw: key)
        self.script_text = script_text
        self.language = (language or "").lower().strip()
        self.title_text = title
        # Saved interpreter settings, so a configured interpreter path wins
        # over the venv/system default in python_executable().
        self.settings = load_settings(SETTINGS_FILE, None)
        # Arguments text captured on the GUI thread at start_run time; the
        # worker thread reads this copy instead of touching args_edit.
        self._args_snapshot = ""
        # Worker -> GUI marshalling.
        self._output_queue = collections.deque()
        self._queue_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._process_lock = threading.Lock()
        self._process = None          # Popen of the running child, if any
        self._stdin_handle = None     # file object wrapping the child's stdin
        self._running = False
        self._started_at = None
        self._ansi_colour = None
        self._ansi_bold = False
        self._ansi_pending = ""
        self.setWindowTitle(f"{self._t('run')} - {title}")
        self.resize(820, 560)
        self.setWindowFlag(QtCore.Qt.WindowContextHelpButtonHint, False)
        layout = QtWidgets.QVBoxLayout(self)

        header = QtWidgets.QHBoxLayout()
        lang_badge = QtWidgets.QLabel(self.language.title() or "Script")
        lang_badge.setStyleSheet(
            """background:#2d2d2d; border:1px solid #404040; border-radius:3px;"""
            """padding:2px 6px; color:#8ab4f8; font-weight:bold;"""
        )
        title_label = QtWidgets.QLabel(f"\u25b6 {title}")
        title_label.setStyleSheet("font-weight: bold; color: #8ab4f8;")
        header.addWidget(lang_badge)
        header.addWidget(title_label)
        header.addStretch()
        self.elapsed_label = QtWidgets.QLabel("")
        self.elapsed_label.setStyleSheet("color: #9aa0a6;")
        header.addWidget(self.elapsed_label)
        self.status_label = QtWidgets.QLabel("")
        self.status_label.setStyleSheet("color: #f7c66d; font-weight: bold;")
        header.addWidget(self.status_label)
        layout.addLayout(header)

        self.output = QtWidgets.QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setFont(QFont("Consolas", 10))
        self.output.setStyleSheet("background-color: #1f1f1f; color: #dfe7f5;")
        self.output.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
        layout.addWidget(self.output, 1)

        # stdin row - lets a long-running/waiting script receive input.
        stdin_row = QtWidgets.QHBoxLayout()
        self.stdin_label = QtWidgets.QLabel("stdin")
        self.stdin_label.setStyleSheet("color:#9aa0a6;")
        self.stdin_edit = QtWidgets.QLineEdit()
        self.stdin_edit.setPlaceholderText(self._t("runner_stdin_placeholder"))
        self.stdin_edit.setFont(QFont("Consolas", 10))
        self.stdin_edit.returnPressed.connect(self.send_stdin)
        self.btn_send = QtWidgets.QPushButton(self._t("runner_send"))
        self.btn_send.clicked.connect(self.send_stdin)
        stdin_row.addWidget(self.stdin_label)
        stdin_row.addWidget(self.stdin_edit, 1)
        stdin_row.addWidget(self.btn_send)
        layout.addLayout(stdin_row)

        # Command-line arguments row - passed to the script on (re)run.
        args_row = QtWidgets.QHBoxLayout()
        self.args_label = QtWidgets.QLabel("args")
        self.args_label.setStyleSheet("color:#9aa0a6;")
        self.args_edit = QtWidgets.QLineEdit()
        self.args_edit.setPlaceholderText(self._t("runner_args_placeholder",
                                                   "Command-line arguments for the script..."))
        self.args_edit.setFont(QFont("Consolas", 10))
        args_row.addWidget(self.args_label)
        args_row.addWidget(self.args_edit, 1)
        layout.addLayout(args_row)

        # Environment row - auto-detect a virtualenv next to the script and
        # run Python scripts with its interpreter instead of sys.executable.
        env_row = QtWidgets.QHBoxLayout()
        self.venv_check = QtWidgets.QCheckBox(
            self._t("runner_use_venv", "Use detected virtual environment"))
        self.venv_check.setChecked(True)
        self.venv_path = detect_virtualenv(BASE_DIR)
        if self.venv_path:
            shown = os.path.basename(os.path.dirname(self.venv_path))
            self.venv_check.setText(self._t("runner_venv_found",
                                            "Use venv: {path}", path=shown))
            self.venv_check.setToolTip(self.venv_path)
        else:
            self.venv_check.setEnabled(False)
            self.venv_check.setToolTip(
                self._t("runner_venv_none", "No virtual environment detected"))
        env_row.addWidget(self.venv_check)
        env_row.addStretch()
        self.btn_history = QtWidgets.QPushButton(
            self._t("runner_history", "Run history..."))
        self.btn_history.clicked.connect(self.show_run_history)
        env_row.addWidget(self.btn_history)
        layout.addLayout(env_row)

        footer = QtWidgets.QHBoxLayout()
        self.btn_rerun = QtWidgets.QPushButton(self._t("runner_rerun"))
        self.btn_rerun.clicked.connect(self.start_run)
        self.btn_stop = QtWidgets.QPushButton(self._t("runner_stop"))
        self.btn_stop.clicked.connect(self.stop_run)
        self.btn_stop.setEnabled(False)
        self.btn_copy = QtWidgets.QPushButton(self._t("copy"))
        self.btn_copy.clicked.connect(self.copy_output)
        self.btn_clear = QtWidgets.QPushButton(self._t("clear"))
        self.btn_clear.clicked.connect(self.output.clear)
        self.btn_wrap = QtWidgets.QPushButton(self._t("runner_wrap"))
        self.btn_wrap.setCheckable(True)
        self.btn_wrap.toggled.connect(self._toggle_wrap)
        self.btn_close = QtWidgets.QPushButton("Close")
        self.btn_close.clicked.connect(self.close)
        footer.addWidget(self.btn_rerun)
        footer.addWidget(self.btn_stop)
        footer.addStretch()
        footer.addWidget(self.btn_copy)
        footer.addWidget(self.btn_clear)
        footer.addWidget(self.btn_wrap)
        footer.addWidget(self.btn_close)
        layout.addLayout(footer)

        self._pump_timer = QtCore.QTimer(self)
        self._pump_timer.setInterval(self.PUMP_INTERVAL_MS)
        self._pump_timer.timeout.connect(self._drain_output_queue)
        self._pump_timer.start()

        self._elapsed_timer = QtCore.QTimer(self)
        self._elapsed_timer.setInterval(200)
        self._elapsed_timer.timeout.connect(self._update_elapsed)

        QtCore.QTimer.singleShot(50, self.start_run)

    # ----------------------------------------------------------- queueing API
    def append_text(self, text, color=None):
        """Queue output text for display (safe from worker threads)."""
        with self._queue_lock:
            self._output_queue.append(("text", text, color))

    def set_status(self, text, color=None):
        """Queue a status-label update (safe from worker threads)."""
        with self._queue_lock:
            self._output_queue.append(("status", text, color))

    def set_running(self, running):
        """Queue a running/idle transition (safe from worker threads)."""
        with self._queue_lock:
            self._output_queue.append(("running", running, None))

    @QtCore.pyqtSlot()
    def _drain_output_queue(self):
        items = []
        with self._queue_lock:
            while self._output_queue:
                items.append(self._output_queue.popleft())
        for kind, value, color in items:
            if kind == "status":
                self.status_label.setText(value)
                if color:
                    self.status_label.setStyleSheet(f"color: {color}; font-weight: bold;")
            elif kind == "running":
                self._apply_running(bool(value))
            elif kind == "history":
                # History records are materialised here on the GUI thread,
                # where reading output.toPlainText() is safe.
                self._write_history(value)
            else:
                self._apply_text(value, color)

    def _write_history(self, spec):
        """Build and store one history entry (GUI thread only)."""
        status, exit_code, elapsed = spec
        entry = {
            "title": self.title_text,
            "language": self.language or "python",
            "args": self._args_snapshot,
            "status": status,
            "exit_code": exit_code,
            "elapsed": round(elapsed, 2),
            "timestamp": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "output": self.output.toPlainText()[-4000:],
        }
        append_run_history(entry)

    def _apply_text(self, text, color=None):
        # QPlainTextEdit has no setTextColor(); colour must be applied via a
        # QTextCharFormat on the cursor (or by setting the widget default).
        text = self._ansi_pending + text
        self._ansi_pending = ""
        last_escape = text.rfind("\x1b")
        if last_escape >= 0:
            last_match = None
            for last_match in self.ANSI_CSI_RE.finditer(text):
                pass
            if last_match is None or last_match.end() <= last_escape:
                self._ansi_pending = text[last_escape:]
                text = text[:last_escape]
        cursor = self.output.textCursor()
        cursor.movePosition(QtGui.QTextCursor.End)
        position = 0
        for match in self.ANSI_CSI_RE.finditer(text):
            self._insert_output_segment(cursor, text[position:match.start()], color)
            sequence = match.group(0)
            if sequence.endswith("m"):
                self._apply_ansi_codes(sequence[2:-1])
            position = match.end()
        self._insert_output_segment(cursor, text[position:], color)
        self.output.setTextCursor(cursor)
        self.output.ensureCursorVisible()

    def _insert_output_segment(self, cursor, text, fallback_color):
        if not text:
            return
        fmt = QtGui.QTextCharFormat()
        foreground = self._ansi_colour or fallback_color or "#ffffff"
        fmt.setForeground(QtGui.QColor(foreground))
        if self._ansi_bold:
            fmt.setFontWeight(QtGui.QFont.Bold)
        cursor.setCharFormat(fmt)
        cursor.insertText(text)

    def _apply_ansi_codes(self, codes):
        """Apply ANSI SGR state without exposing escape sequences in output."""
        values = [int(value or 0) for value in codes.split(";")]
        for value in values:
            if value == 0:
                self._ansi_colour = None
                self._ansi_bold = False
            elif value == 1:
                self._ansi_bold = True
            elif value == 22:
                self._ansi_bold = False
            elif value == 39:
                self._ansi_colour = None
            elif value in self.ANSI_COLOURS:
                self._ansi_colour = self.ANSI_COLOURS[value]

    def _apply_running(self, running):
        self._running = running
        self.btn_stop.setEnabled(running)
        self.btn_rerun.setEnabled(not running)
        if running:
            self._started_at = dt.datetime.now()
            self._elapsed_timer.start()
        else:
            self._elapsed_timer.stop()

    # ------------------------------------------------------------- utilities
    def _update_elapsed(self):
        if not self._running or self._started_at is None:
            return
        seconds = (dt.datetime.now() - self._started_at).total_seconds()
        self.elapsed_label.setText(self._t("runner_elapsed", time=self._fmt_duration(seconds)))

    @staticmethod
    def _fmt_duration(seconds):
        if seconds < 60:
            return f"{seconds:.2f}s"
        minutes, rest = divmod(seconds, 60)
        return f"{int(minutes)}m {rest:.1f}s"

    def copy_output(self):
        QtWidgets.QApplication.clipboard().setText(self.output.toPlainText())

    def _toggle_wrap(self, checked):
        self.output.setLineWrapMode(
            QtWidgets.QPlainTextEdit.WidgetWidth if checked
            else QtWidgets.QPlainTextEdit.NoWrap
        )
        self.btn_wrap.setText("Wrap: on" if checked else self._t("runner_wrap"))

    def closeEvent(self, event):
        """Make sure we never leave a child process running behind us."""
        self._pump_timer.stop()
        self._elapsed_timer.stop()
        self.stop_run(silent=True)
        super().closeEvent(event)
    def _report_unavailable(self, message):
        self.append_text(message + "\n", "#ff7b72")
        self.set_status(self._t("runner_status_unavailable"), "#ff7b72")
        self.set_running(False)

    def _run_in_thread(self, target, *args):
        self.set_running(True)
        threading.Thread(target=target, args=args, daemon=True).start()

    def _finish(self, status_key, status_color, **kwargs):
        """Common end-of-run bookkeeping for every runner."""
        self.set_status(self._t(status_key, **kwargs), status_color)
        self.set_running(False)

    # --------------------------------------------------------------- stdin
    def send_stdin(self):
        """Forward a line of text to a running child process' stdin."""
        with self._process_lock:
            handle = self._stdin_handle
        if handle is None:
            return
        text = self.stdin_edit.text()
        self.stdin_edit.clear()
        try:
            handle.write(text + "\n")
            handle.flush()
            self.append_text(f"{text}\n", "#c8c8c8")
        except Exception as exc:
            self.append_text(f"stdin error: {exc}\n", "#ff7b72")

    # ------------------------------------------------------- process control
    def stop_run(self, silent=False):
        """Request the running process to stop (or interrupt in-process code)."""
        self._stop_event.set()
        with self._process_lock:
            process = self._process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
            except Exception:
                pass
        if not silent:
            self.set_status(self._t("runner_status_stopped"), "#ff7b72")
        self.set_running(False)

    # ------------------------------------------------------- language routing
    def start_run(self):
        """Clear previous output and dispatch to the right runner."""
        if self._running:
            return
        self._stop_event.clear()
        self.output.clear()
        self._ansi_colour = None
        self._ansi_bold = False
        self._ansi_pending = ""
        # Snapshot the args line now, on the GUI thread, so worker threads
        # never have to read the widget when they record history later.
        self._args_snapshot = self.args_edit.text().strip()
        self.elapsed_label.setText("")
        self.status_label.setText(self._t("runner_status_running"))
        self.status_label.setStyleSheet("color: #f7c66d; font-weight: bold;")
        try:
            runner = self._runner_for_language(self.language)
        except Exception as exc:  # dispatch itself should never kill the dialog
            self.append_text(f"{self._t('runner_status_error')}: {exc}\n", "#ff7b72")
            self._finish("runner_status_error", "#ff7b72")
            return
        try:
            runner()
        except Exception as exc:
            # Any setup failure (temp file, missing interpreter path, ...) is
            # reported instead of silently leaving the dialog in 'running' state.
            self.append_text(
                self._t("runner_start_error", "Failed to start: {error}",
                        error=exc) + "\n", "#ff7b72")
            self.record_history("error", None, 0.0)
            self._finish("runner_status_error", "#ff7b72")
    # ------------------------------------------------- arguments / venv / history
    def script_args(self):
        """Split the args line into a list using shlex-style quoting."""
        text = self.args_edit.text().strip()
        if not text:
            return []
        try:
            import shlex
            return shlex.split(text, posix=(os.name != "nt"))
        except ValueError:
            return text.split()

    def python_executable(self):
        """The interpreter to use for Python scripts (venv-aware)."""
        # Check custom interpreter setting first
        custom_path = get_interpreter_path("python", self.settings)
        if custom_path and os.path.exists(custom_path):
            return custom_path

        # Fall back to venv if enabled
        if self.venv_check.isChecked() and self.venv_path:
            if os.path.isdir(self.venv_path):
                scripts = os.path.join(
                    self.venv_path, "Scripts" if os.name == "nt" else "bin")
                exe = os.path.join(
                    scripts, "python.exe" if os.name == "nt" else "python")
                if os.path.isfile(exe):
                    return exe
            elif os.path.isfile(self.venv_path):
                return self.venv_path
        return sys.executable

    def record_history(self, status, exit_code, elapsed):
        """Queue one history record (safe from worker threads).

        The actual entry is built on the GUI thread in _drain_output_queue,
        because reading args_edit / output widgets from the worker thread
        would be an unsafe cross-thread Qt access.
        """
        with self._queue_lock:
            self._output_queue.append(("history", (status, exit_code, elapsed), None))

    def show_run_history(self):
        """Show the recent run history in a simple read-only dialog."""
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle(self._t("runner_history_title", "Run history"))
        dialog.resize(680, 460)
        layout = QtWidgets.QVBoxLayout(dialog)
        view = QtWidgets.QPlainTextEdit()
        view.setReadOnly(True)
        view.setFont(QFont("Consolas", 9))
        history = load_run_history()
        if not history:
            view.setPlainText(self._t("runner_history_empty", "No runs recorded yet."))
        else:
            lines = []
            for entry in history[:100]:
                lines.append(
                    "[{ts}] {title} ({lang}) args: {args}\n"
                    "    {status}  exit={code}  {elapsed}s".format(
                        ts=entry.get("timestamp", ""),
                        title=entry.get("title", ""),
                        lang=entry.get("language", ""),
                        args=entry.get("args") or "-",
                        status=entry.get("status", ""),
                        code=entry.get("exit_code", ""),
                        elapsed=entry.get("elapsed", "")))
            view.setPlainText("\n".join(lines))
        layout.addWidget(view, 1)
        close_btn = QtWidgets.QPushButton(self._t("close", "Close"))
        close_btn.clicked.connect(dialog.close)
        row = QtWidgets.QHBoxLayout()
        row.addStretch()
        row.addWidget(close_btn)
        layout.addLayout(row)
        dialog.exec_()

    def _runner_for_language(self, language):
        if language in {"bash", "sh", "shell"}:
            return self._run_bash
        if language in {"powershell", "pwsh"}:
            return self._run_powershell
        if language in {"javascript", "js", "node"}:
            return self._run_node
        if language in {"batch", "bat", "cmd"}:
            return self._run_batch
        if language in {"vbscript", "vbs"}:
            return self._run_windows_script
        if language == "json":
            return self._run_json
        if language == "sql":
            return self._run_sql_threaded
        if language in {"python", "py", ""}:
            return self._run_python_threaded
        return self._run_as_python_fallback

    def write_temp_file(self, suffix):
        fd, path = tempfile.mkstemp(suffix=suffix)
        os.close(fd)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(self.script_text)
        return path

    # ------------------------------------------------------------ interpreters
    def _run_bash(self):
        exe = get_interpreter_path("bash", self.settings)
        if not exe:
            self._report_unavailable(self._t("runner_bash_unavailable"))
            return
        self._run_in_thread(
            self.run_command, [exe, "-c", self.script_text] + self.script_args())

    def _run_powershell(self):
        exe = get_interpreter_path("powershell", self.settings)
        if not exe:
            self._report_unavailable(self._t("runner_powershell_unavailable"))
            return
        cmd = [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
               "-Command", self.script_text] + self.script_args()
        self._run_in_thread(self.run_command, cmd)

    def _run_node(self):
        exe = get_interpreter_path("node", self.settings)
        if not exe:
            self._report_unavailable(self._t("runner_node_unavailable"))
            return
        tmp = self.write_temp_file(".js")
        # Keep async failures visible and make stack traces point back to the
        # original source locations when Node supports source maps.
        command = [exe, "--enable-source-maps",
                   "--unhandled-rejections=strict", "--trace-uncaught",
                   "--trace-warnings", tmp] + self.script_args()
        self._run_in_thread(self.run_command, command, tmp)

    def _run_batch(self):
        if os.name != "nt":
            self._report_unavailable(self._t("runner_batch_unavailable"))
            return
        exe = get_interpreter_path("batch", self.settings) or "cmd.exe"
        tmp = self.write_temp_file(".bat")
        self._run_in_thread(
            self.run_command, [exe, "/c", tmp] + self.script_args(), tmp)

    def _run_windows_script(self):
        """Run a VBScript/HTA file through the Windows Script Host."""
        if os.name != "nt":
            self._report_unavailable(self._t("runner_batch_unavailable"))
            return
        exe = get_interpreter_path("vbscript", self.settings) or "cscript.exe"
        suffix = ".vbs" if self.language in {"vbscript", "vbs"} else ".hta"
        tmp = self.write_temp_file(suffix)
        self._run_in_thread(
            self.run_command, [exe, "//nologo", tmp] + self.script_args(), tmp)
    # ---------------------------------------------------------------- Python
    def _run_python_threaded(self):
        # Run Python outside the GUI process so input(), sys.exit(), crashes
        # and infinite loops cannot block or corrupt ScriptDB itself.
        tmp = self.write_temp_file(".py")
        self._run_in_thread(
            self.run_command,
            [self.python_executable(), "-u", "-X", "faulthandler", tmp]
            + self.script_args(),
            tmp,
        )

    def run_python(self):
        """Execute the script in-process, capturing stdout/stderr live."""
        start = dt.datetime.now()
        stream = _QueueStream(self.append_text)
        namespace = {"__name__": "__main__", "__file__": "<script>"}
        try:
            code = compile(self.script_text, "<script>", "exec")
        except SyntaxError:
            self.append_text(traceback.format_exc(), "#ff7b72")
            self._finish("runner_status_error", "#ff7b72")
            return
        try:
            with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
                exec(code, namespace)
            stream.flush()
            elapsed = (dt.datetime.now() - start).total_seconds()
            if not stream.wrote_anything:
                self.append_text(self._t("runner_no_output") + "\n", "#9aa0a6")
            self.set_status(
                self._t("runner_status_done", time=self._fmt_duration(elapsed)),
                "#9be9a8",
            )
            self.set_running(False)
        except KeyboardInterrupt:
            self.append_text(self._t("runner_stopped_note") + "\n", "#ff7b72")
            self._finish("runner_status_stopped", "#ff7b72")
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
            stream.flush()
            self.append_text(self._t("runner_exit_note", code=code,
                                     time=self._fmt_duration((dt.datetime.now() - start).total_seconds())) + "\n",
                             "#8ab4f8" if code == 0 else "#ff7b72")
            self._finish("runner_status_exit", "#9be9a8" if code == 0 else "#ff7b72", code=code)
        except Exception:
            stream.flush()
            self.append_text(traceback.format_exc(), "#ff7b72")
            self._finish("runner_status_error", "#ff7b72")
    # ---------------------------------------------------------------- SQLite
    def _run_sql_threaded(self):
        self._run_in_thread(self.run_sqlite)

    def run_sqlite(self):
        """Run the script against an in-memory SQLite database and print rows."""
        try:
            connection = sqlite3.connect(":memory:")
            connection.isolation_level = None  # autocommit, like the sqlite3 CLI
            cursor = connection.cursor()
            statements = [s for s in _split_sql_statements(self.script_text)]
            if not statements:
                self.append_text(self._t("runner_no_output") + "\n", "#9aa0a6")
            for statement in statements:
                cursor.execute(statement)
                if cursor.description:
                    self._print_result_table(cursor)
                else:
                    affected = getattr(cursor, "rowcount", -1)
                    if affected is not None and affected >= 0:
                        self.append_text(f"-- {affected} row(s) affected\n", "#8ab4f8")
            connection.close()
            self._finish("runner_status_done", "#9be9a8", time="0.00s")
        except Exception:
            self.append_text(traceback.format_exc(), "#ff7b72")
            self._finish("runner_status_error", "#ff7b72")

    def _print_result_table(self, cursor):
        headers = [d[0] for d in cursor.description]
        rows = cursor.fetchall()
        table = "\t".join(headers)
        self.append_text(table + "\n", "#8ab4f8")
        self.append_text("-" * max(4, len(table)) + "\n", "#8ab4f8")
        for row in rows:
            self.append_text("\t".join(str(v) for v in row) + "\n", "#dfe7f5")
        self.append_text(f"-- {len(rows)} row(s)\n", "#9aa0a6")

    # ------------------------------------------------------------------ JSON
    def _run_json(self):
        try:
            parsed = json.loads(self.script_text)
            pretty = json.dumps(parsed, ensure_ascii=False, indent=2)
            self.append_text(pretty + "\n", "#8ab4f8")
            self._finish("runner_status_done", "#9be9a8", time="0.00s")
        except Exception as exc:
            self.append_text(f"JSON error: {exc}\n", "#ff7b72")
            self._finish("runner_status_error", "#ff7b72")

    # ----------------------------------------------------- external process
    def _run_as_python_fallback(self):
        """Handle a script whose category has no dedicated runner.

        Rather than blindly executing the text as Python (which produced a
        confusing syntax error for HTML or other markup), first re-detect the
        language from the code itself.  When that finds a language with a
        dedicated runner (bash, PowerShell, Node...), use it; otherwise do not
        try to interpret the file as Python at all and report the problem.
        """
        detected = detect_language_kind(self.script_text, hint=self.language)[0]
        if detected in {"python", "text"}:
            if detected == "python":
                self.append_text(self._t("runner_python_fallback") + "\n",
                                 "#f7c66d")
                self._run_python_threaded()
            else:
                # Plain text / markup with no interpreter: refuse instead of
                # throwing a Python syntax error at the user.
                self.append_text(
                    self._t("runner_no_runner_for_language",
                            "No interpreter available for this language; "
                            "the script was not run.") + "\n", "#ff7b72")
                self.record_history("unavailable", None, 0.0)
                self._finish("runner_status_unavailable", "#ff7b72")
            return
        # A concrete language was detected in the content; route to it.
        self.append_text(
            self._t("runner_detected_language",
                    "Detected language: {language}",
                    language=detected) + "\n", "#f7c66d")
        runner = self._runner_for_language(detected)
        runner()
    def run_command(self, command, temp_path=None):
        """Run an external command with consistent runner safeguards."""
        process = None
        start = dt.datetime.now()
        environment = os.environ.copy()
        environment.setdefault("PYTHONUTF8", "1")
        environment.setdefault("PYTHONIOENCODING", "utf-8")
        launch_options = {
            "cwd": BASE_DIR,
            "env": environment,
        }
        if os.name == "nt":
            launch_options["creationflags"] = (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            launch_options["start_new_session"] = True
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                shell=False,
                **launch_options,
            )
        except FileNotFoundError:
            self.append_text(
                self._t("runner_exe_unavailable", name=command[0]) + "\n", "#ff7b72",
            )
            self._finish("runner_status_unavailable", "#ff7b72")
            if temp_path:
                _safe_remove(temp_path)
            return
        except Exception as exc:
            self.append_text(str(exc) + "\n", "#ff7b72")
            self._finish("runner_status_error", "#ff7b72")
            if temp_path:
                _safe_remove(temp_path)
            return
        with self._process_lock:
            self._process = process
            self._stdin_handle = process.stdin
        reader = threading.Thread(
            target=self._stream_process_output, args=(process,), daemon=True
        )
        reader.start()

        timed_out = False
        try:
            while True:
                try:
                    process.wait(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    if self._stop_event.is_set():
                        break
                    if (dt.datetime.now() - start).total_seconds() > self.RUN_TIMEOUT_SECONDS:
                        timed_out = True
                        break
            reader.join(timeout=2)
            elapsed = (dt.datetime.now() - start).total_seconds()
            if timed_out:
                self._kill(process)
                self.append_text(
                    "\n" + self._t("runner_timeout_note", seconds=self.RUN_TIMEOUT_SECONDS) + "\n",
                    "#ff7b72",
                )
                self.record_history("timeout", None, elapsed)
                self._finish("runner_status_timeout", "#ff7b72", seconds=self.RUN_TIMEOUT_SECONDS)
            elif self._stop_event.is_set():
                self._kill(process)
                self.append_text("\n" + self._t("runner_stopped_note") + "\n", "#ff7b72")
                self.record_history("stopped", None, elapsed)
                self._finish("runner_status_stopped", "#ff7b72")
            else:
                rc = process.returncode
                self.append_text(
                    "\n" + self._t("runner_exit_note", code=rc,
                                   time=self._fmt_duration(elapsed)) + "\n",
                    "#8ab4f8" if rc == 0 else "#ff7b72",
                )
                self.record_history(
                    "ok" if rc == 0 else "error", rc, elapsed)
                self._finish(
                    "runner_status_exit",
                    "#9be9a8" if rc == 0 else "#ff7b72",
                    code=rc,
                )
        finally:
            self._kill(process)
            with self._process_lock:
                self._process = None
                self._stdin_handle = None
            if temp_path:
                _safe_remove(temp_path)

    def _stream_process_output(self, process):
        """Read the child's merged stdout/stderr and queue each line."""
        try:
            for line in iter(process.stdout.readline, ""):
                self.append_text(line)
        except (ValueError, OSError):
            pass
        finally:
            try:
                process.stdout.close()
            except Exception:
                pass

    @staticmethod
    def _kill(process):
        if process is None or process.poll() is not None:
            return
        try:
            if os.name == "nt":
                # A script can spawn children; kill the complete process tree
                # so a stopped runner cannot leave work running in the background.
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            else:
                os.killpg(process.pid, 9)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass
RUN_HISTORY_FILE = os.path.join(BASE_DIR, "run_history.json")
RUN_HISTORY_LIMIT = 200


def load_run_history():
    """Read the run-history file, returning [] when missing or corrupted."""
    try:
        with open(RUN_HISTORY_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def append_run_history(entry):
    """Prepend one run record to the history file (best effort, capped)."""
    history = load_run_history()
    history.insert(0, entry)
    del history[RUN_HISTORY_LIMIT:]
    try:
        with open(RUN_HISTORY_FILE, "w", encoding="utf-8") as handle:
            json.dump(history, handle, ensure_ascii=False, indent=1)
    except OSError:
        pass


def detect_virtualenv(directory):
    """Find a virtualenv next to `directory` (or above it, up to 2 levels).

    Returns the path of the python interpreter inside the venv, or None.
    Looks for venv/.venv/env folders containing a python executable.
    """
    names = ("venv", ".venv", "env", ".env")
    exe_name = "python.exe" if os.name == "nt" else "python"
    current = os.path.abspath(directory or BASE_DIR)
    for _level in range(3):
        for name in names:
            candidate = os.path.join(current, name)
            scripts = os.path.join(candidate, "Scripts" if os.name == "nt" else "bin")
            exe = os.path.join(scripts, exe_name)
            # Accept a folder that holds an interpreter (python or pythonw).
            if os.path.isdir(scripts) and any(
                f.lower().startswith("python") and f.lower().endswith(
                    ".exe" if os.name == "nt" else "")
                for f in os.listdir(scripts)
            ):
                return exe if os.path.isfile(exe) else candidate
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return None


def _safe_remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _split_sql_statements(script):
    """Split a SQL script into individual statements, honouring string literals."""
    statements = []
    current = []
    quote = None
    i = 0
    text = script or ""
    while i < len(text):
        char = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if quote:
            current.append(char)
            if char == quote:
                if nxt == quote:          # doubled quote = escaped quote
                    current.append(nxt)
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if char in ("'", '"', "`"):
            quote = char
            current.append(char)
        elif char == "-" and nxt == "-":      # line comment
            while i < len(text) and text[i] not in "\r\n":
                i += 1
            continue
        elif char == ";":
            statement = "".join(current).strip()
            if statement:
                statements.append(statement)
            current = []
        else:
            current.append(char)
        i += 1
    tail = "".join(current).strip()
    if tail:
        statements.append(tail)
    return statements


class _QueueStream:
    """File-like object that pushes written text onto the runner's display queue."""

    def __init__(self, emit):
        self._emit = emit
        self._buffer = ""
        self.wrote_anything = False

    def write(self, text):
        if not text:
            return 0
        self.wrote_anything = True
        self._buffer += text
        self._flush_lines()
        return len(text)

    def _flush_lines(self):
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self._emit(line + "\n")

    def flush(self):
        if self._buffer:
            self._emit(self._buffer)
            self._buffer = ""

    def isatty(self):
        return False

    def writable(self):
        return True

    def readable(self):
        return False
class TemplateDialog(QtWidgets.QDialog):
    def __init__(self, parent, existing_form):
        super().__init__(parent)
        self.setWindowTitle("Templates")
        self.resize(640, 440)
        self.existing_form = existing_form
        self.template_names = list(TEMPLATES.keys())

        layout = QtWidgets.QVBoxLayout(self)

        list_widget = QtWidgets.QListWidget()
        for name in self.template_names:
            list_widget.addItem(name)
        self.list_widget = list_widget
        self.preview = QtWidgets.QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setFont(QFont("Consolas", 10))

        list_widget.currentRowChanged.connect(self.on_select)
        list_widget.setCurrentRow(0)

        buttons = QtWidgets.QHBoxLayout()
        use_btn = QtWidgets.QPushButton("Use")
        use_btn.clicked.connect(self.use_template)
        copy_btn = QtWidgets.QPushButton("Copy")
        copy_btn.clicked.connect(self.copy_template)
        close_btn = QtWidgets.QPushButton("Close")
        close_btn.clicked.connect(self.close)
        buttons.addWidget(use_btn)
        buttons.addWidget(copy_btn)
        buttons.addStretch()
        buttons.addWidget(close_btn)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        splitter.addWidget(list_widget)
        splitter.addWidget(self.preview)
        splitter.setSizes([180, 460])

        layout.addWidget(splitter)
        layout.addLayout(buttons)

    def on_select(self, index):
        if index < 0:
            return
        name = self.template_names[index]
        _, code = TEMPLATES[name]
        self.preview.setPlainText(code)

    def use_template(self):
        current = self.list_widget.currentItem()
        if not current:
            return
        name = current.text()
        title, code = TEMPLATES[name]
        self.existing_form.script_name.setText(title)
        combo = self.existing_form.category_combo
        combo.setCurrentText(name if combo.findText(name) >= 0 else "Python")
        self.existing_form.code_edit.setPlainText(code)
        self.close()

    def copy_template(self):
        current = self.list_widget.currentItem()
        if not current:
            return
        _, code = TEMPLATES[current.text()]
        QtWidgets.QApplication.clipboard().setText(code)
class CodePreviewDialog(QtWidgets.QDialog):
    """Read-only, syntax highlighted preview of a script."""

    def __init__(self, parent, title, code, category="Python"):
        super().__init__(parent)
        self.setWindowTitle(f"Preview - {title}")
        self.resize(760, 560)
        self.setWindowFlag(QtCore.Qt.WindowContextHelpButtonHint, False)

        layout = QtWidgets.QVBoxLayout(self)
        header = QtWidgets.QLabel(f"{category} - {title}")
        header.setStyleSheet("font-weight:bold; color:#8ab4f8;")
        layout.addWidget(header)

        self.browser = QtWidgets.QTextBrowser()
        self.browser.setOpenExternalLinks(False)
        self.browser.setFont(QFont("Consolas", 10))
        self.browser.setStyleSheet("background:#1f1f1f; color:#dfe7f5;")
        body = [highlight_text(line, category) for line in code.splitlines()]
        self.browser.setHtml("<br>".join(body) or "(empty)")
        layout.addWidget(self.browser)

        footer = QtWidgets.QHBoxLayout()
        copy_btn = QtWidgets.QPushButton("Copy")
        copy_btn.clicked.connect(lambda: QtWidgets.QApplication.clipboard().setText(code))
        close_btn = QtWidgets.QPushButton("Close")
        close_btn.clicked.connect(self.close)
        footer.addStretch()
        footer.addWidget(copy_btn)
        footer.addWidget(close_btn)
        layout.addLayout(footer)


class DiffDialog(QtWidgets.QDialog):
    """Show a unified diff between two revisions of the same script."""

    def __init__(self, parent, title, diff_text):
        super().__init__(parent)
        self.setWindowTitle(f"Diff - {title}")
        self.resize(820, 560)
        self.setWindowFlag(QtCore.Qt.WindowContextHelpButtonHint, False)

        layout = QtWidgets.QVBoxLayout(self)
        self.view = QtWidgets.QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setFont(QFont("Consolas", 10))
        self.view.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
        self.view.setPlainText(diff_text or "(no changes)")
        layout.addWidget(self.view)

        close_btn = QtWidgets.QPushButton("Close")
        close_btn.clicked.connect(self.close)
        footer = QtWidgets.QHBoxLayout()
        footer.addStretch()
        footer.addWidget(close_btn)
        layout.addLayout(footer)
class HistoryDialog(QtWidgets.QDialog):
    """Browse saved revisions of a script and restore / diff any of them."""

    def __init__(self, parent, window, script):
        super().__init__(parent)
        self.window = window
        self.script = script
        self.revisions = [dict(item) for item in script.get("history", [])]
        self.setWindowTitle(f"{window.t('version_history')} - {script.get('name', '')}")
        self.resize(760, 520)
        self.setWindowFlag(QtCore.Qt.WindowContextHelpButtonHint, False)

        layout = QtWidgets.QVBoxLayout(self)
        self.list_widget = QtWidgets.QListWidget()
        current = script_to_dict(script)
        self.list_widget.addItem(self._row_text(current, "current"))
        for index, entry in enumerate(reversed(self.revisions)):
            revision = len(self.revisions) - index
            self.list_widget.addItem(self._row_text(entry, f"#{revision}"))
        self.list_widget.currentRowChanged.connect(self.on_select)
        layout.addWidget(self.list_widget)

        self.preview = QtWidgets.QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setFont(QFont("Consolas", 10))
        layout.addWidget(self.preview)

        buttons = QtWidgets.QHBoxLayout()
        self.btn_diff = QtWidgets.QPushButton(window.t("diff"))
        self.btn_restore = QtWidgets.QPushButton(window.t("restore"))
        close_btn = QtWidgets.QPushButton("Close")
        self.btn_diff.clicked.connect(self.show_diff)
        self.btn_restore.clicked.connect(self.restore_version)
        close_btn.clicked.connect(self.close)
        buttons.addWidget(self.btn_diff)
        buttons.addWidget(self.btn_restore)
        buttons.addStretch()
        buttons.addWidget(close_btn)
        layout.addLayout(buttons)

        self.list_widget.setCurrentRow(0)
        if not self.revisions:
            self.preview.setPlainText(window.t("no_history"))

    @staticmethod
    def _row_text(entry, label):
        return f"[{label}] {entry.get('date', '')} - {entry.get('name', '')} ({entry.get('category', '')})"

    def current_entry(self):
        row = self.list_widget.currentRow()
        if row <= 0:
            return dict(self.script)
        index = len(self.revisions) - row
        if 0 <= index < len(self.revisions):
            return dict(self.revisions[index])
        return dict(self.script)

    def on_select(self, row):
        if row < 0:
            return
        entry = self.current_entry()
        self.preview.setPlainText(entry.get("code", ""))
    def show_diff(self):
        row = self.list_widget.currentRow()
        if row <= 0:
            self.window.show_message(self.window.t("info"), self.window.t("no_history"))
            return
        selected = self.current_entry()
        index = len(self.revisions) - row
        if index - 1 >= 0:
            previous = self.revisions[index - 1]
        else:
            previous = self.script
        DiffDialog(self, self.script.get("name", ""), unified_diff(previous, selected)).exec_()

    def restore_version(self):
        row = self.list_widget.currentRow()
        if row <= 0:
            return
        if QtWidgets.QMessageBox.question(
            self, self.window.t("confirm"), self.window.t("confirm_restore"),
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
        ) != QtWidgets.QMessageBox.Yes:
            return
        entry = self.current_entry()
        self.window._open_script_editor_dialog(entry)
        self.window.show_message(self.window.t("info"), self.window.t("restored"))
        self.close()


class WheelScrollFilter(QtCore.QObject):
    """Application-wide wheel handler so scrolling works everywhere.

    Widgets that scroll themselves (text views, trees, lists, combo popups,
    scroll areas) keep their normal behaviour. When the wheel turns over a
    widget that cannot scroll on its own (labels, plain widgets, layout
    background, toolbars), the event is redirected to the nearest scrollable
    ancestor so the whole program responds to the mouse wheel.
    """

    # Widgets that handle the wheel themselves and must never be hijacked.
    SCROLLABLE = (QtWidgets.QAbstractScrollArea,)
    # Popups (menus, combo dropdowns) must keep the wheel for their own list.
    POPUP_CLASSES = (QtWidgets.QMenu, QtWidgets.QComboBox)

    def eventFilter(self, obj, event):
        if event.type() != QtCore.QEvent.Wheel:
            return False
        # Never touch an object Qt has already deleted, and never let an
        # exception out of a Qt callback: both end in an access violation
        # inside the (CPython) event loop instead of a Python traceback.
        try:
            if not isinstance(obj, QtWidgets.QWidget) or not _is_alive(obj):
                return False
            # Let anything that scrolls itself (and popups) behave normally.
            if isinstance(obj, self.SCROLLABLE):
                return False
            if isinstance(obj, self.POPUP_CLASSES):
                return False
            delta = event.pixelDelta().y() or event.angleDelta().y()
            if not delta:
                return False
            # Start at the nearest container, but let the wheel bubble outward
            # when that container is already at the relevant edge.
            for target in self._scroll_areas(obj):
                if not _is_alive(target):
                    continue
                bar = target.verticalScrollBar()
                if not bar.isVisible() or bar.minimum() == bar.maximum():
                    continue
                next_value = max(bar.minimum(), min(bar.maximum(),
                                                    bar.value() - delta))
                if next_value == bar.value():
                    continue
                bar.setValue(next_value)
                event.accept()
                return True
        except Exception:
            return False
        return False

    @staticmethod
    def _scroll_areas(widget):
        # Walk up the parent chain so nested panels scroll naturally.
        parent = widget.parentWidget()
        while parent is not None:
            yield parent
            if not _is_alive(parent):
                return
            parent = parent.parentWidget()


def _is_alive(widget):
    """True when a wrapper still points at a live C++ object.

    The application-wide event filter is called for every wheel event, on
    objects that may already have been deleted by Qt in the meantime;
    calling into such a wrapper is what crashed the process.
    """
    if widget is None:
        return False
    try:
        import sip  # noqa: F401 - PyQt5 ships sip; the check is optional.
    except ImportError:
        return True
    try:
        return not sip.isdeleted(widget)
    except Exception:
        return True



class AboutDialog(QtWidgets.QDialog):
    """A compact, visual About panel that remains readable at small sizes."""

    def __init__(self, window):
        super().__init__(window)
        self.setWindowTitle(window.t("about_title", "About ScriptDB"))
        self.setWindowIcon(load_app_icon())
        self.setMinimumSize(620, 470)
        self.resize(700, 540)

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(22, 22, 22, 18)
        root.setSpacing(14)

        header = QtWidgets.QFrame()
        header.setObjectName("aboutHeader")
        header_layout = QtWidgets.QHBoxLayout(header)
        header_layout.setContentsMargins(18, 16, 18, 16)
        icon = QtWidgets.QLabel()
        icon.setPixmap(app_icon_pixmap(76) or draw_info_glyph(76))
        icon.setFixedSize(76, 76)
        icon.setScaledContents(True)
        header_layout.addWidget(icon)
        heading = QtWidgets.QVBoxLayout()
        title = QtWidgets.QLabel(window.t("about_title", "About ScriptDB"))
        title.setObjectName("aboutTitle")
        version = QtWidgets.QLabel(window.t("about_version", "2.7.0"))
        version.setObjectName("aboutVersion")
        subtitle = QtWidgets.QLabel("Local code workspace and script database")
        subtitle.setObjectName("aboutSubtitle")
        heading.addWidget(title)
        heading.addWidget(version)
        heading.addWidget(subtitle)
        heading.addStretch()
        header_layout.addLayout(heading, 1)
        root.addWidget(header)

        stats = QtWidgets.QHBoxLayout()
        stats.setSpacing(10)
        for label, value in (
                ("Scripts", len(window.scripts)),
                ("Connections", len(window.connections)),
                ("Engine", "PyQt5")):
            tile = QtWidgets.QFrame()
            tile.setObjectName("aboutTile")
            tile_layout = QtWidgets.QVBoxLayout(tile)
            tile_layout.setContentsMargins(12, 9, 12, 9)
            number = QtWidgets.QLabel(str(value))
            number.setObjectName("aboutMetric")
            caption = QtWidgets.QLabel(label)
            caption.setObjectName("aboutCaption")
            tile_layout.addWidget(number)
            tile_layout.addWidget(caption)
            stats.addWidget(tile, 1)
        root.addLayout(stats)

        details = QtWidgets.QTextBrowser()
        details.setOpenExternalLinks(True)
        details.setPlaceholderText("About ScriptDB")
        details.setPlainText(window.t(
            "about_text",
            version=window.t("about_version", "2.7.0"),
            scripts=len(window.scripts),
            connections=len(window.connections),
            data=DATA_FILE,
            conn=CONN_FILE,
        ))
        root.addWidget(details, 1)

        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self.setStyleSheet("""
            QFrame#aboutHeader {
                background: palette(midlight);
                border: 1px solid palette(mid);
                border-radius: 8px;
            }
            QLabel#aboutTitle { font-size: 24px; font-weight: bold; }
            QLabel#aboutVersion { color: palette(highlight); font-weight: bold; }
            QLabel#aboutSubtitle, QLabel#aboutCaption { color: palette(mid); }
            QFrame#aboutTile {
                background: palette(base);
                border: 1px solid palette(mid);
                border-radius: 6px;
            }
            QLabel#aboutMetric { font-size: 18px; font-weight: bold; }
            QTextBrowser { padding: 10px; }
        """)


def install_wheel_scrolling(app):
    """Install the global wheel filter so scrolling works in every tab.

    Returns the filter instance so the caller can keep a reference (Qt does
    not own the Python object, so dropping it would disable the filter).
    """
    filt = WheelScrollFilter(app)
    app.installEventFilter(filt)
    app._wheel_scroll_filter = filt
    return filt

def apply_application_theme(app, theme=None, compact=False,
                            low_graphics=False):
    # Give the whole application one look, once, before any window is built.
    #
    # This is the single place the theme is installed.  It has to happen on the
    # QApplication rather than on the window, because Qt draws several things --
    # tooltips, the item-view drop indicator, the file dialog's own widgets --
    # from the application palette, and a window-level stylesheet cannot reach
    # them.
    if app_theme is None:                       # the tabs package is missing
        return False
    chosen = theme or getattr(app, "_sdb_theme", None) or app_theme.DEFAULT_THEME
    app._sdb_theme = chosen
    app._sdb_compact = bool(compact)
    palette = app_theme.build_palette(chosen, compact)
    if palette is not None:
        app.setPalette(palette)
    sheet = (app_theme.build_low_graphics_stylesheet(chosen) if low_graphics
             else app_theme.build_stylesheet(chosen, compact))
    app.setStyleSheet(sheet)
        # The fonts the tabs ask for by name (Consolas) may be missing on a bare
        # Linux box; asking here means every widget gets a monospaced fallback
        # instead of each tab guessing.  Nothing is set when the tool already has a
        # monospace family, so a properly configured machine is left alone.
    try:
        database = QtGui.QFontDatabase()
        families = set(database.families())
        if not ({"Consolas", "Cascadia Mono", "DejaVu Sans Mono",
                 "Liberation Mono", "Courier New"} & families):
            for candidate in ("Menlo", "Monaco", "Ubuntu Mono", "Courier"):
                if candidate in families:
                    font = app.font()
                    font.setFamily(candidate)
                    app.setFont(font)
                    break
    except Exception:
        pass
    return True


class ScriptForm(QtWidgets.QWidget):
    def __init__(self, parent, on_save, on_clear, on_copy):
        super().__init__(parent)
        self.on_save = on_save
        self.on_clear = on_clear
        self.on_copy = on_copy

        self.setLayout(QtWidgets.QVBoxLayout())
        form = QtWidgets.QFormLayout()
        self.script_name = QtWidgets.QLineEdit()
        self.category_combo = QtWidgets.QComboBox()
        self.category_combo.addItems(CATEGORIES)
        self.favorite_btn = QtWidgets.QPushButton("\u2606 Favorite")
        self.favorite_btn.setCheckable(True)
        self.favorite_btn.setToolTip("Mark this script as a favorite (Ctrl+F)")
        self.tags_edit = QtWidgets.QLineEdit()
        self.description_edit = QtWidgets.QTextEdit()
        self.code_edit = QtWidgets.QPlainTextEdit()
        self.code_edit.setFont(QFont("Consolas", 10))
        self.code_edit.setTabChangesFocus(False)

        name_row = QtWidgets.QHBoxLayout()
        name_row.addWidget(self.script_name, 1)
        name_row.addWidget(self.favorite_btn)

        form.addRow("Script Name:", name_row)
        form.addRow("Category:", self.category_combo)
        form.addRow("Tags (comma separated):", self.tags_edit)
        form.addRow("Description:", self.description_edit)
        form.addRow("Code:", self.code_edit)
        self.layout().addLayout(form)
        buttons = QtWidgets.QHBoxLayout()
        save_btn = QtWidgets.QPushButton("Save")
        clear_btn = QtWidgets.QPushButton("Clear")
        copy_btn = QtWidgets.QPushButton("Copy")
        preview_btn = QtWidgets.QPushButton("Preview")
        buttons.addWidget(save_btn)
        buttons.addWidget(clear_btn)
        buttons.addWidget(preview_btn)
        buttons.addStretch()
        buttons.addWidget(copy_btn)
        self.layout().addLayout(buttons)

        save_btn.clicked.connect(self.on_save)
        clear_btn.clicked.connect(self.on_clear)
        copy_btn.clicked.connect(self.on_copy)
        preview_btn.clicked.connect(self.preview_code)

    def preview_code(self):
        """Show the current code in a read-only, syntax highlighting window."""
        dialog = CodePreviewDialog(self, self.script_name.text().strip() or "Preview",
                                   self.code_edit.toPlainText(), self.category_combo.currentText())
        dialog.exec_()

    def clear_fields(self):
        self.script_name.clear()
        self.tags_edit.clear()
        self.description_edit.clear()
        self.code_edit.clear()
        self.favorite_btn.setChecked(False)
        if self.category_combo.count():
            self.category_combo.setCurrentIndex(0)

    def load_entry(self, entry):
        self.script_name.setText(entry.get("name", ""))
        category = entry.get("category", "Python")
        if category in CATEGORIES:
            self.category_combo.setCurrentText(category)
        else:
            self.category_combo.setCurrentIndex(0)
        self.tags_edit.setText(entry.get("tags", ""))
        self.description_edit.setPlainText(entry.get("desc", ""))
        self.code_edit.setPlainText(entry.get("code", ""))
        self.favorite_btn.setChecked(bool(entry.get("favorite")))

    def get_entry_data(self):
        return {
            "name": self.script_name.text().strip(),
            "category": self.category_combo.currentText(),
            "tags": self.tags_edit.text().strip(),
            "desc": self.description_edit.toPlainText().strip(),
            "code": self.code_edit.toPlainText().strip(),
        }


class ScriptPreview(QtWidgets.QWidget):
    """Read-only details and source preview for the selected script."""

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        self.title = QtWidgets.QLabel(window.t("preview", "Preview"))
        self.title.setProperty("role", "title")
        layout.addWidget(self.title)
        self.meta = QtWidgets.QLabel("")
        self.meta.setProperty("role", "hint")
        self.meta.setWordWrap(True)
        layout.addWidget(self.meta)
        self.description = QtWidgets.QPlainTextEdit()
        self.description.setReadOnly(True)
        self.description.setMaximumHeight(90)
        layout.addWidget(self.description)
        self.code = CodeEditor(self) if CodeEditor is not None \
            else QtWidgets.QPlainTextEdit(self)
        self.code.setReadOnly(True)
        self.code.setFont(QFont("Consolas", 10))
        layout.addWidget(self.code, 1)
        self.show_empty()

    def show_empty(self):
        self.title.setText(self.window.t("preview", "Preview"))
        self.meta.clear()
        self.description.clear()
        self.code.setPlainText(self.window.t("select_script", "Select a script"))
        if CodeEditor is not None:
            self.code.set_category("Python")

    def show_script(self, script):
        self.title.setText(script.get("name", "Untitled"))
        self.meta.setText("%s  |  %s  |  %s" % (
            script.get("category", ""), script.get("tags", ""),
            script.get("date", "")))
        self.description.setPlainText(script.get("desc", ""))
        self.code.setPlainText(script.get("code", ""))
        if CodeEditor is not None:
            self.code.set_category(script.get("category", "Python"))
# ---------------------------------------------------------------------------
# Mojibake repair: some older language tables were saved with their UTF-8
# bytes re-interpreted as CP1252 ("zakoÅ, czony" instead of "zakoÅ„czony").
# Rather than hand-fixing thousands of strings, every translation is passed
# through this repair: if re-encoding the bytes as CP1252 and decoding them
# as UTF-8 yields a cleaner string, use that instead.
# The tell-tale sign of CP1252 mojibake is a run of Latin-1 supplement
# characters (\u0080-\u00ff) or CP1252 specials in the \u2013-\u201f window
# (e.g. \u201a for a comma, \u2122 for ™) that only appear after misdecoding.
_MOJIBAKE_RE = re.compile(
    "[\u0080-\u00ff]{2,}|[\u00c0-\u00ff][\u2013-\u201f\u2026\u2030\u2039"
    "\u203a\u201a\u201e\u201c\u201d\u2018\u2019\u2122\u2022\u2020\u2021\u02c6"
    "\u2032\u2033\u0152\u0153\u0160\u0161\u0178\u017d\u017e\u0192\u02dc\u20ac\u0141\u0142]")


def _fix_mojibake(text):
    """Repair CP1252-misdecoded UTF-8 text (idempotent, best effort)."""
    if not isinstance(text, str) or not _MOJIBAKE_RE.search(text):
        return text
    try:
        repaired = text.encode("cp1252").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        # Try latin-1 fallback for bytes outside cp1252.
        try:
            repaired = text.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return text
    # Only accept the repair when it actually removed mojibake sequences.
    if len(repaired) >= len(text) or _MOJIBAKE_RE.search(repaired):
        return text
    return repaired


class ScriptDatabaseWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.lang = "en"
        # Collect load warnings so the user learns about a damaged data file
        # instead of silently starting empty and overwriting it on the next save.
        self.load_warnings = []
        self.scripts = [normalize_entry(s) or s for s in load_data(DATA_FILE, self.load_warnings)]
        self.connections = load_connections(CONN_FILE, self.load_warnings)
        # User interface preferences (list layout, separators, column visibility).
        self.settings = dict(DEFAULT_SETTINGS)
        self.settings.update(load_settings(SETTINGS_FILE, self.load_warnings))
        if not isinstance(self.settings.get("columns"), dict):
            self.settings["columns"] = dict(DEFAULT_SETTINGS["columns"])
        for column in LIST_COLUMNS:
            self.settings["columns"].setdefault(column, True)
        # Apply the remembered theme before any widget is built, so the whole
        # window -- including the tabs that read a colour while constructing
        # themselves -- is drawn with the right palette the first time rather
        # than repainting after a visible flash of the default one.
        if app_theme is not None:
            saved_theme = self.settings.get("theme", app_theme.DEFAULT_THEME)
            if saved_theme in app_theme.THEMES:
                application = QtWidgets.QApplication.instance()
                if application is not None:
                    apply_application_theme(
                        application, saved_theme,
                        compact=bool(self.settings.get("compact_ui", False)))
        self.editing_id = None
        self.filter_text = ""
        self.filter_favorites = False
        self.sort_ascending = True
        self.setWindowTitle("ScriptDB")
        self.setWindowIcon(load_app_icon())
        # Restore window geometry if saved, otherwise use default size
        saved_geometry = self.settings.get("window_geometry")
        if saved_geometry:
            try:
                parts = [int(value) for value in saved_geometry]
                if len(parts) == 4:
                    self.setGeometry(QtCore.QRect(*parts))
                else:
                    self.resize(1400, 900)
            except (TypeError, ValueError):
                self.resize(1400, 900)
        else:
            self.resize(1400, 900)
        # Never let the window become smaller than the widest toolbar it has to
        # show: below this size Qt simply clips the buttons on the right edge.
        self.setMinimumSize(1200, 900)

        self.central_widget = QtWidgets.QWidget()
        self.setCentralWidget(self.central_widget)
        self.central_layout = QtWidgets.QVBoxLayout(self.central_widget)

        self.top_bar = QtWidgets.QHBoxLayout()
        self.title_label = QtWidgets.QLabel("ScriptDB")
        self.title_label.setProperty("role", "title")
        self.top_bar.addWidget(self.title_label)
        self.top_bar.addStretch()
        self.count_label = QtWidgets.QLabel("Total scripts: 0")
        self.top_bar.addWidget(self.count_label)
        self.stats_btn = QtWidgets.QPushButton("Statistics")
        self.stats_btn.clicked.connect(self.show_statistics)
        self.top_bar.addWidget(self.stats_btn)
        # A Shortcuts button right after Statistics: it opens a popup window
        # listing every keyboard shortcut available in the program, so they can
        # be discovered without reading the source.
        self.shortcuts_btn = QtWidgets.QPushButton("\u2328 Shortcuts")
        self.shortcuts_btn.setToolTip(self.t("shortcuts_title", "Keyboard shortcuts"))
        self.shortcuts_btn.clicked.connect(self.show_shortcuts)
        self.top_bar.addWidget(self.shortcuts_btn)
        # The ToDo button sits right next to Statistics, as asked: it opens the
        # task workspace and, when there is something waiting, says so on the
        # button itself so the count is visible from any tab.
        self.todo_btn = QtWidgets.QPushButton("\u2611 ToDo")
        self.todo_btn.setToolTip("Tasks, calendar and reminders (Ctrl+T)")
        self.todo_btn.setShortcut(QtCore.Qt.CTRL + QtCore.Qt.Key_T)
        # The button toggles the popup window: it opens it, and closes it when
        # the list is already the window in front, so the same key gets you in
        # and out without reaching for the mouse.
        self.todo_btn.clicked.connect(self.toggle_todo_window)
        self.top_bar.addWidget(self.todo_btn)
        # The theme button cycles Dark -> Light -> Monochrome.  It is a menu
        # rather than a toggle so a third palette did not need a second button,
        # and so the current choice is visible without clicking.
        self.theme_btn = QtWidgets.QToolButton()
        self.theme_btn.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.theme_btn.setToolTip("Colour theme (Ctrl+Shift+T)")
        self.theme_menu = QtWidgets.QMenu(self.theme_btn)
        self._theme_actions = {}
        if app_theme is not None:
            for name in app_theme.theme_names():
                action = self.theme_menu.addAction(app_theme.theme_label(name))
                action.setCheckable(True)
                action.triggered.connect(
                    lambda _checked=False, key=name: self.set_theme(key))
                self._theme_actions[name] = action
        self.theme_btn.setMenu(self.theme_menu)
        self.theme_btn.setShortcut(QtCore.Qt.CTRL + QtCore.Qt.SHIFT + QtCore.Qt.Key_T)
        self.top_bar.addWidget(self.theme_btn)
        self.lang_btn = QtWidgets.QPushButton("\U0001f310 PL")
        self.lang_btn.clicked.connect(self.toggle_language)
        self.top_bar.addWidget(self.lang_btn)
        self.lang_btn.setShortcut(QtCore.Qt.CTRL + QtCore.Qt.Key_L)
        self.about_btn = QtWidgets.QPushButton("\u24d8")
        self.about_btn.setIcon(load_about_icon())
        self.about_btn.setToolTip("About ScriptDB")
        self.about_btn.setShortcut(QtCore.Qt.CTRL + QtCore.Qt.Key_F1)
        self.about_btn.clicked.connect(self.show_about)
        self.top_bar.addWidget(self.about_btn)
        self.shortcut_new = QtWidgets.QShortcut(QtCore.Qt.CTRL + QtCore.Qt.Key_N, self)
        self.shortcut_new.activated.connect(self.add_new_script)
        self.shortcut_save = QtWidgets.QShortcut(QtCore.Qt.CTRL + QtCore.Qt.Key_S, self)
        self.shortcut_save.activated.connect(self.save_script)
        self.shortcut_fav = QtWidgets.QShortcut(QtCore.Qt.CTRL + QtCore.Qt.Key_F, self)
        self.shortcut_fav.activated.connect(self.toggle_favorite_selected)
        self.shortcut_find = QtWidgets.QShortcut(QtCore.Qt.CTRL + QtCore.Qt.Key_K, self)
        self.shortcut_find.activated.connect(self.focus_filter)
        self._install_global_shortcuts()
        self.central_layout.addLayout(self.top_bar)

        # Only the Scripts tab is present for now; the other tabs will be added
        # later as separate widgets on this same QTabWidget.
        self.tabs = QtWidgets.QTabWidget()
        self.central_layout.addWidget(self.tabs, 1)

        self.list_tab = QtWidgets.QWidget()
        self.tabs.addTab(self.list_tab, "Scripts")

        # The Editor tab sits right after the Scripts list: it edits one script
        # at a time and writes changes back to the list.
        if EditorTab is not None:
            self.editor_tab = EditorTab(self)
        else:
            self.editor_tab = QtWidgets.QWidget()
        self.tabs.addTab(self.editor_tab, "Editor")

        # The Analyzer tab sits between the Scripts list and the Audit
        # tab: it analyses the scripts the Audit tab later verifies.
        if ScriptAnalyzerTab is not None:
            self.analyzer_tab = ScriptAnalyzerTab(self)
        else:
            self.analyzer_tab = QtWidgets.QWidget()
        self.tabs.addTab(self.analyzer_tab, "Analyzer")

        # The Audit tab lives in its own module (tabs/audit.py) so this file
        # stays small; every tab follows the same one-file-per-tab pattern.
        if AuditTab is not None:
            self.audit_tab = AuditTab(self)
        else:
            self.audit_tab = QtWidgets.QWidget()
        self.tabs.addTab(self.audit_tab, "Audit")

        # The Debugger tab sits right after Audit: it prepares and inspects an
        # offline debugging session (breakpoints, watches, stack, trace log)
        # against the scripts Audit verifies.
        if DebuggerTab is not None:
            self.debugger_tab = DebuggerTab(self)
        else:
            self.debugger_tab = QtWidgets.QWidget()
        self.tabs.addTab(self.debugger_tab, "Debugger")

        # The Sandbox tab sits right after Debugger: it runs a script in an
        # isolated, throw-away workspace under an explicit policy (no network
        # unless allowed, writes only inside the sandbox, a hard timeout and a
        # capped output buffer).
        if SandboxTab is not None:
            self.sandbox_tab = SandboxTab(self)
        else:
            self.sandbox_tab = QtWidgets.QWidget()
        self.tabs.addTab(self.sandbox_tab, "Sandbox")

        self.diff_tab = DiffTab(self)
        self.tabs.addTab(self.diff_tab, "Diff")

        # The Vault tab is the encrypted, isolated private safe. It reuses the
        # DOCs crypto and repository rather than growing its own, and nothing
        # inside it is reachable without the master password.
        if VaultTab is not None:
            self.vault_tab = VaultTab(self)
        else:
            self.vault_tab = QtWidgets.QWidget()
        self.tabs.addTab(self.vault_tab, "Vault")

        # The Security tab hardens the scripts themselves: hashing, HMAC,
        # sealing, signatures, the integrity registry, backups and reports. Its
        # engine is in tabs/secure_core.py, so nothing here touches key material.
        if SecurityTab is not None:
            self.security_tab = SecurityTab(self)
        else:
            self.security_tab = QtWidgets.QWidget()
        self.tabs.addTab(self.security_tab, "Security")

        # The Structure tab builds file trees from presets: the generator,
        # template engine, validator, pipeline and integrations are all in
        # tabs/structure_core.py, so this file only wires the tab in.
        if StructureTab is not None:
            self.structure_tab = StructureTab(self)
        else:
            self.structure_tab = QtWidgets.QWidget()
        self.tabs.addTab(self.structure_tab, "Structure")

        # The DOCs tab is a document repository: it organises, stores, archives
        # and edits documents of every common type in one portable SQLite file
        # that other database programs can open, optionally password protected.
        if DocsTab is not None:
            self.docs_tab = DocsTab(self)
        else:
            self.docs_tab = QtWidgets.QWidget()
        self.tabs.addTab(self.docs_tab, "DOCs")

        # The SQLite tab is the database workbench: an offline data layer over
        # files, an online data layer over an API, and the sync layer that
        # joins them (tabs/sqlite_offline.py, sqlite_online.py, sqlite_sync.py).
        if SqliteTab is not None:
            self.sqlite_tab = SqliteTab(self)
        else:
            self.sqlite_tab = QtWidgets.QWidget()
        self.tabs.addTab(self.sqlite_tab, "SQLite")

        # The Reports tab collects every generated report artifact (audit,
        # analyzer, diff, search exports...) so they can be browsed, grouped
        # and filtered without re-running anything.
        if ReportsTab is not None:
            self.reports_tab = ReportsTab(self)
        else:
            self.reports_tab = QtWidgets.QWidget()
        self.tabs.addTab(self.reports_tab, "Reports")

        # The Metadata tab edits the header metadata that lives inside the
        # script files themselves: it reads, validates and writes that block,
        # and it is the only place a bulk metadata change is made.  The engine
        # is in tabs/metadata_core.py, so this is only its interface.
        if MetadataTab is not None:
            self.metadata_tab = MetadataTab(self)
        else:
            self.metadata_tab = QtWidgets.QWidget()
        self.tabs.addTab(self.metadata_tab, "Metadata")

        # The Search tab runs the search-engine kinds over the whole database
        # and is placed last: it is the catch-all lookup reached from the other
        # tabs, so it sits at the far end of the tab strip.
        if SearchTab is not None:
            self.search_tab = SearchTab(self)
        else:
            self.search_tab = QtWidgets.QWidget()
        self.tabs.addTab(self.search_tab, "Search")

        # The Apps tab is the personal launcher. It is deliberately placed at
        # the very end of the strip: it is the one tab that is not part of the
        # script workflow, so it sits after everything else.
        if AppsTab is not None:
            self.apps_tab = AppsTab(self)
        else:
            self.apps_tab = QtWidgets.QWidget()
        self.tabs.addTab(self.apps_tab, "Apps")

        # The ToDo workspace is NOT a tab: it is a popup window opened from the
        # ToDo button beside Statistics (and Ctrl+T). A task list is consulted
        # while working on something else, so it has to float and be closable
        # without losing the tab you were on. Nothing is added to the strip
        # here; the window is created below, after the tabs are in place.
        # Todo is created on demand. Its calendar, reminders and shopping
        # view are not needed until the user opens the workspace.
        self.todo_window = None
        self.todo_tab = None

        self.build_list_tab()
        self.apply_language()
        self.refresh_list()
        self.diff_tab.refresh_pickers()
        self.install_context_menus()

        # Announce a damaged data file once the window is on screen.
        if self.load_warnings:
            QtCore.QTimer.singleShot(0, self.report_load_warnings)

    def t(self, key, fallback=None, **kwargs):
        """Look up a translation, optionally formatting {placeholders}."""
        table = TRANSLATIONS.get(self.lang, TRANSLATIONS["en"])
        if key in table:
            text = _fix_mojibake(table[key])
        elif fallback is not None:
            text = _fix_mojibake(fallback)
        else:
            text = key
        if kwargs:
            try:
                return text.format(**kwargs)
            except (KeyError, IndexError, ValueError):
                return text
        return text
    # ------------------------------------------------- audit integration
    # The audit tab and the rest of the application form one program: every
    # data change here is announced on the shared audit event bus, which both
    # records it in the timeline and drives the automatic audits.
    def audit_available(self):
        """True when the real AuditTab (not the placeholder) is loaded."""
        return AuditTab is not None and hasattr(self.audit_tab, "emit_audit_event")

    def notify_audit(self, event, **payload):
        """Announce a change to the audit subsystem (timeline + auto audits).

        This is the single entry point every mutation in the application uses,
        so the change history and the automatic audits stay complete without
        each action having to know about the audit internals.
        """
        if not self.audit_available():
            return 0
        try:
            # Record the change in the timeline first, then let the event bus
            # run whatever automatic audit the event implies.
            recorder = getattr(self.audit_tab, "_recording_hook", None)
            if callable(recorder):
                recorder()
            return self.audit_tab.emit_audit_event(event, **payload)
        except Exception:
            return 0

    def _announce_script_save(self, was_edit, entry):
        """Pick the right audit event for a just-saved script."""
        event = "module_updated" if was_edit else "module_installed"
        self.notify_audit(event, where=entry.get("name", ""),
                          detail=entry.get("category", ""))

    def open_audit_tab(self, key=None):
        """Bring the Audit tab to the front, optionally selecting a sub-tab."""
        if not self.audit_available():
            return
        self.tabs.setCurrentWidget(self.audit_tab)
        if key is not None:
            index = self.audit_tab._index_of_key(key)
            if index is not None:
                self.audit_tab.subtabs.setCurrentIndex(index)
        self.audit_tab.refresh()

    def audit_script(self, script=None, focus="audit_tab_modules"):
        """Audit a specific script (or the current selection) and show it."""
        if not self.audit_available():
            self.show_message(self.t("info"), self.t("audit_unavailable"))
            return
        script = script or self.selected_script()
        if script is not None:
            # Remember the selection so the report's "selection" section and
            # the audit operations act on exactly this script.
            self.audit_tab._audit_target_id = script.get("id")
            self.audit_tab.log_change(
                "modules", script.get("name", ""),
                self.t("audit_script_audited", "audited"),
                self.t("audit_tl_how_manual", "manual"))
        self.open_audit_tab(focus)

    def run_security_audit(self):
        """Jump straight to the graded security audit page."""
        self.open_audit_tab("audit_tab_premium")

    def export_audit(self, fmt="txt"):
        """Export the audit from the main window (used by menus/shortcuts)."""
        if not self.audit_available():
            return ""
        path = self.audit_tab.export_report(fmt)
        if path:
            self.show_message(self.t("info"),
                              self.t("audit_exported").format(path=path))
        return path

    def report_load_warnings(self):
        """Tell the user which data files could not be read, and why."""
        if not self.load_warnings:
            return
        detail = "\n".join(f"\u2022 {item}" for item in self.load_warnings)
        QtWidgets.QMessageBox.warning(
            self,
            self.t("warning"),
            f"{self.t('load_problem')}\n\n{detail}\n\n{self.t('load_problem_hint')}",
        )

    def show_message(self, title, message):
        """Single funnel for user notifications (easy to silence in tests).

        The main window has no editor widget of its own, so this only shows
        the message box; it must never touch self.code or any other pane.
        """
        QtWidgets.QMessageBox.information(self, title, message)

    # ------------------------------------------------------- todo integration
    def todo_available(self):
        # True when the real ToDo workspace (not the placeholder) is loaded.
        return TodoTab is not None and hasattr(self.todo_tab, "store")

    def open_todo_tab(self, view=None):
        # Open the ToDo popup window, optionally on a specific view. A reminder
        # that came due while the list was closed is fired on the way in,
        # because that is when the user is looking for it.
        if self.todo_window is None and TodoWindow is not None:
            self.todo_window = TodoWindow(self)
            self.todo_tab = self.todo_window.todo
            self.todo_window.restore_geometry(self.settings)
        if not self.todo_available():
            self.show_message(self.t("info"),
                              self.t("todo_unavailable",
                                     "The ToDo workspace is not available"))
            return
        if self.todo_window is not None:
            self.todo_window.open_view(view)
        else:
            # No window class (an older build): fall back to the tab, if any.
            index = self.tabs.indexOf(self.todo_tab)
            if index >= 0:
                self.tabs.setCurrentIndex(index)
            if view is not None and hasattr(self.todo_tab, "set_view"):
                self.todo_tab.set_view(view)
        try:
            self.todo_tab.check_reminders()
            self.todo_tab.refresh()
        except Exception:
            pass
        self.refresh_todo_badge()

    def toggle_todo_window(self, view=None):
        # The ToDo button: show the popup, or hide it when it is already up.
        self.refresh_todo_badge()
        if self.todo_window is None:
            self.open_todo_tab(view)
            return
        self.todo_window.toggle(view)

    def refresh_todo_badge(self):
        # Say how much is waiting on the ToDo button itself. The badge counts
        # what is due today and what is late, because those are what a person
        # acts on; a quiet day leaves the button plain rather than showing 0.
        if not hasattr(self, "todo_btn"):
            return 0
        label = "\u2611 " + self.t("todo", "ToDo")
        if not self.todo_available():
            self.todo_btn.setText(label)
            return 0
        try:
            buckets = self.todo_tab.store.today()
        except Exception:
            self.todo_btn.setText(label)
            return 0
        due_today = len(buckets.get("today", []))
        late = len(buckets.get("overdue", []))
        waiting = due_today + late
        if not waiting:
            self.todo_btn.setText(label)
            self.todo_btn.setStyleSheet("")
            return 0
        self.todo_btn.setText("%s  %d" % (label, waiting))
        colour = "#f7768e" if late else "#f7c66d"
        self.todo_btn.setStyleSheet("color: %s; font-weight: bold;" % colour)
        self.todo_btn.setToolTip(self.t(
            "todo_badge_hint",
            "{due} due today, {late} late (Ctrl+T)",
            due=due_today, late=late))
        return waiting

    def show_about(self):
        """Show the About dialog: version, data paths and storage counts."""
        AboutDialog(self).exec_()

    def closeEvent(self, event):
        """Save window geometry before closing."""
        # Save current window geometry
        rect = self.geometry()
        self.settings["window_geometry"] = [rect.x(), rect.y(), rect.width(), rect.height()]
        self.save_settings()
        super().closeEvent(event)

    def install_context_menus(self):
        """Add consistent menus to tabs and views without a custom menu."""
        self.tabs.tabBar().setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.tabs.tabBar().customContextMenuRequested.connect(
            self.show_tab_context_menu)
        for view in self.findChildren(QtWidgets.QAbstractItemView):
            if view.contextMenuPolicy() == QtCore.Qt.CustomContextMenu:
                continue
            view.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
            view.customContextMenuRequested.connect(
                lambda point, view=view: self.show_view_context_menu(view, point))

    def _copy_view_selection(self, view):
        indexes = view.selectionModel().selectedIndexes()
        if not indexes:
            return
        rows = {}
        for index in indexes:
            rows.setdefault(index.row(), {})[index.column()] = str(
                index.data() or "")
        lines = []
        for row in sorted(rows):
            columns = rows[row]
            lines.append("\t".join(columns.get(column, "")
                                    for column in range(max(columns) + 1)))
        QtWidgets.QApplication.clipboard().setText("\n".join(lines))

    def _refresh_current_tab(self, widget=None):
        widget = widget or self.tabs.currentWidget()
        refresh = getattr(widget, "refresh", None)
        if callable(refresh):
            try:
                refresh()
                return
            except TypeError:
                pass
        refresh_list = getattr(widget, "refresh_list", None)
        if callable(refresh_list):
            refresh_list()

    def show_view_context_menu(self, view, point):
        """Show standard actions for a list, table or tree view."""
        menu = QtWidgets.QMenu(view)
        copy_action = menu.addAction(self.t("context_copy", "Copy selection"),
                                     lambda: self._copy_view_selection(view))
        copy_action.setEnabled(bool(view.selectionModel().selectedIndexes()))
        menu.addAction(self.t("context_select_all", "Select all"),
                       view.selectAll)
        clear_action = menu.addAction(
            self.t("context_clear_selection", "Clear selection"),
            view.clearSelection)
        clear_action.setEnabled(bool(view.selectionModel().selectedIndexes()))
        menu.addSeparator()
        menu.addAction(self.t("context_refresh", "Refresh"),
                       lambda: self._refresh_current_tab())
        menu.exec_(view.viewport().mapToGlobal(point))

    def show_tab_context_menu(self, point):
        """Navigate and refresh tabs from the tab strip."""
        bar = self.tabs.tabBar()
        index = bar.tabAt(point)
        if index < 0:
            return
        self.tabs.setCurrentIndex(index)
        menu = QtWidgets.QMenu(bar)
        menu.addAction(self.t("context_refresh", "Refresh"),
                       lambda: self._refresh_current_tab(self.tabs.widget(index)))
        menu.addAction(self.t("context_copy_tab", "Copy tab name"),
                       lambda: QtWidgets.QApplication.clipboard().setText(
                           self.tabs.tabText(index)))
        menu.addSeparator()
        previous = menu.addAction(self.t("context_previous_tab", "Previous tab"),
                                  lambda: self.tabs.setCurrentIndex(
                                      max(0, index - 1)))
        previous.setEnabled(index > 0)
        following = menu.addAction(self.t("context_next_tab", "Next tab"),
                                   lambda: self.tabs.setCurrentIndex(
                                       min(self.tabs.count() - 1, index + 1)))
        following.setEnabled(index < self.tabs.count() - 1)
        menu.addAction(self.t("about", "About ScriptDB"), self.show_about)
        menu.exec_(bar.mapToGlobal(point))

    def apply_language(self):
        T = TRANSLATIONS[self.lang]
        self.setWindowTitle(T["title"])
        self.title_label.setText(T["title"])
        self.lang_btn.setText("\U0001f310 EN" if self.lang == "pl" else "\U0001f310 PL")
        # The tab labels are addressed by the widget they belong to, not by a
        # position: a hard-coded index silently mislabels every tab after the
        # one that was inserted, which is exactly what happened when the Vault
        # was added between Diff and DOCs.
        for widget, fallback, label_key in (
                (self.list_tab, "Scripts", "scripts"),
                (self.apps_tab, "Apps", "apps"),
                (self.editor_tab, "Editor", "editor"),
                (self.analyzer_tab, "Analyzer", "analyzer"),
                (self.audit_tab, "Audit", "audit"),
                (self.debugger_tab, "Debugger", "debugger"),
                (self.sandbox_tab, "Sandbox", "sandbox"),
                (self.diff_tab, "Diff", "diff"),
                (self.vault_tab, "Vault", "vault"),
                (self.security_tab, "Security", "security"),
                (self.structure_tab, "Structure", "structure"),
                (self.sqlite_tab, "SQLite", "sqlite"),
                (self.docs_tab, "DOCs", "docs"),
                (self.reports_tab, "Reports", "reports"),
                (self.search_tab, "Search", "search")):
            index = self.tabs.indexOf(widget)
            if index >= 0:
                self.tabs.setTabText(index, T.get(label_key, fallback))
        self.stats_btn.setText(T["statistics"])
        self.shortcuts_btn.setText("\u2328 " + T.get("shortcuts", "Shortcuts"))
        self.shortcuts_btn.setToolTip(T.get("shortcuts_title", "Keyboard shortcuts"))
        self.theme_btn.setText(app_theme.theme_label(self.current_theme())
                               if app_theme is not None else "")
        for key, action in getattr(self, "_theme_actions", {}).items():
            action.setText(app_theme.theme_label(key))
            action.setChecked(key == self.current_theme())
        self.todo_btn.setText("\u2611 " + T.get("todo", "ToDo"))
        # Every tab that can retranslate itself is handled by one generic loop:
        # an attribute name list instead of a repeated hasattr/getattr block
        # per tab, so a new tab only needs one more entry here.
        for attribute in (
                "editor_tab", "apps_tab", "vault_tab", "security_tab",
                "structure_tab", "sqlite_tab", "search_tab", "diff_tab",
                "analyzer_tab", "audit_tab", "debugger_tab", "sandbox_tab",
                "docs_tab", "reports_tab", "todo_tab"):
            tab = getattr(self, attribute, None)
            if tab is not None and hasattr(tab, "retranslate"):
                tab.retranslate(T)
        # The ToDo workspace is a window of its own, so its chrome is renamed
        # here rather than through the tab strip.
        if getattr(self, "todo_window", None) is not None:
            self.todo_window.retranslate()
            self.todo_window.setWindowTitle("ScriptDB - " + T.get("todo", "ToDo"))
        self.count_label.setText(f"{T['total_scripts']} {len(self.scripts)}")
        self.btn_preview_script.setText(T["preview"])
        self.btn_duplicate_script.setText(T["duplicate"])
        self.btn_history_script.setText(T["history"])
        self.btn_favorite_script.setText("\u2605 " + T["favorites"])
        self.btn_import.setText(T.get("import_menu", "Import..."))
        self.act_import_json.setText(T.get("import_menu_json", "Database JSON (.json)"))
        self.act_import_file.setText(T.get("import_menu_file", "Source file(s)..."))
        self.act_import_folder.setText(T.get("import_menu_folder", "Whole folder..."))
        self.btn_export.setText(T.get("export_menu", "Export..."))
        self.act_export_json.setText(T.get("export_menu_json", "Database JSON (.json)"))
        self.act_export_file.setText(T.get("export_menu_file", "Script file..."))
        self.act_export_report.setText(T.get("export_menu_report", "Report (.md)"))
        self.filter_favorites_btn.setText(T["favorites_only"] if self.filter_favorites else "\u2606 " + T["favorites_only"])
        self.btn_sort_scripts.setText("Sort Z-A" if not self.sort_ascending else "Sort A-Z")
        if hasattr(self, 'btn_view_mode'):
            is_tree = bool(self.setting("group_by_category", False))
            self.btn_view_mode.setText(T.get("list_view", "List View") if is_tree else T.get("tree_view", "Tree View"))
        self.update_list_columns()

    def toggle_language(self):
        # Save current geometry before language change
        current_geometry = self.geometry()
        self.lang = "pl" if self.lang == "en" else "en"
        self.apply_language()
        # Restore geometry to prevent window resize
        self.setGeometry(current_geometry)

    # --------------------------------------------------------------- theme
    def current_theme(self):
        # The theme in force, falling back to the default.
        if app_theme is None:
            return ""
        return self.setting("theme", app_theme.DEFAULT_THEME)

    def set_theme(self, name, save=True):
        # Switch the whole window to another palette and remember it.
        #
        # The sheet is re-applied to the *application*, not to this window: the
        # dialogs, tooltips and the file explorer are separate top-level windows
        # and would keep the old palette otherwise.  The view then has to be
        # rebuilt because several tabs cache a colour they read from the palette
        # when they were first built.
        if app_theme is None or name not in app_theme.THEMES:
            return False
        app = QtWidgets.QApplication.instance()
        compact = bool(self.setting("compact_ui", False))
        if app is not None:
            apply_application_theme(app, name, compact=compact)
        if save:
            self.set_setting("theme", name, refresh=False)
        for key, action in getattr(self, "_theme_actions", {}).items():
            action.setChecked(key == name)
        self.apply_language()
        self.refresh_list()
        return True

    def update_list_columns(self):
        tree = self.script_tree
        headers = [self.t("name"), self.t("category_col"), self.t("tags_col"), self.t("date")]
        tree.setHeaderLabels(headers)
    def build_list_tab(self):
        layout = QtWidgets.QVBoxLayout(self.list_tab)

        toolbar = QtWidgets.QHBoxLayout()
        self.btn_add_script = QtWidgets.QPushButton("Add Script")
        self.btn_templates = QtWidgets.QPushButton("Templates")
        self.btn_run_script = QtWidgets.QPushButton("Run")
        self.btn_preview_script = QtWidgets.QPushButton("Preview")
        self.btn_duplicate_script = QtWidgets.QPushButton("Duplicate")
        self.btn_history_script = QtWidgets.QPushButton("History")
        self.btn_favorite_script = QtWidgets.QPushButton("\u2605 Favorite")
        self.btn_sort_scripts = QtWidgets.QPushButton("Sort A-Z")
        self.btn_delete_script = QtWidgets.QPushButton("Delete")
        # A single "Import..." button whose menu holds every import source.
        self.btn_import = QtWidgets.QToolButton()
        self.btn_import.setText("Import...")
        self.btn_import.setPopupMode(QtWidgets.QToolButton.MenuButtonPopup)
        self.import_menu = QtWidgets.QMenu(self.btn_import)
        self.act_import_json = self.import_menu.addAction("Database JSON (.json)")
        self.act_import_file = self.import_menu.addAction("Source file(s)...")
        self.act_import_folder = self.import_menu.addAction("Whole folder...")
        self.act_import_json.triggered.connect(self.import_json)
        self.act_import_file.triggered.connect(self.import_script_file)
        self.act_import_folder.triggered.connect(self.import_script_folder)
        self.btn_import.setMenu(self.import_menu)
        # The button itself opens the menu (the arrow toggles it as well).
        self.btn_import.clicked.connect(self.show_import_menu)

        # A single "Export..." button whose menu holds every export target.
        self.btn_export = QtWidgets.QToolButton()
        self.btn_export.setText("Export...")
        self.btn_export.setPopupMode(QtWidgets.QToolButton.MenuButtonPopup)
        self.export_menu = QtWidgets.QMenu(self.btn_export)
        self.act_export_json = self.export_menu.addAction("Database JSON (.json)")
        self.act_export_file = self.export_menu.addAction("Script file...")
        self.act_export_report = self.export_menu.addAction("Report (.md)")
        self.act_export_json.triggered.connect(self.export_json)
        self.act_export_file.triggered.connect(self.export_script_file)
        self.act_export_report.triggered.connect(self.export_report)
        self.btn_export.setMenu(self.export_menu)
        # The button itself opens the menu (the arrow toggles it as well).
        self.btn_export.clicked.connect(self.show_export_menu)

        self.btn_add_script.clicked.connect(self.add_new_script)
        self.btn_templates.clicked.connect(self.show_templates)
        self.btn_run_script.clicked.connect(self.run_selected_script)
        self.btn_preview_script.clicked.connect(self.preview_selected_script)
        self.btn_duplicate_script.clicked.connect(self.duplicate_selected_script)
        self.btn_history_script.clicked.connect(self.show_script_history)
        self.btn_favorite_script.clicked.connect(self.toggle_favorite_selected)
        self.btn_sort_scripts.clicked.connect(self.sort_scripts)
        self.btn_delete_script.clicked.connect(self.delete_selected_script)
        toolbar.addWidget(self.btn_add_script)
        toolbar.addWidget(self.btn_templates)
        toolbar.addWidget(self.btn_run_script)
        toolbar.addWidget(self.btn_preview_script)
        toolbar.addWidget(self.btn_duplicate_script)
        toolbar.addWidget(self.btn_history_script)
        toolbar.addWidget(self.btn_favorite_script)
        toolbar.addWidget(self.btn_sort_scripts)
        toolbar.addWidget(self.btn_delete_script)
        toolbar.addStretch()
        
        # Add view mode toggle button
        self.btn_view_mode = QtWidgets.QPushButton("Tree View")
        self.btn_view_mode.setCheckable(True)
        self.btn_view_mode.setToolTip("Toggle between tree and list view")
        self.btn_view_mode.clicked.connect(self.toggle_view_mode)
        # Set initial state based on current setting
        is_tree = bool(self.setting("group_by_category", False))
        self.btn_view_mode.setChecked(is_tree)
        self.btn_view_mode.setText("List View" if is_tree else "Tree View")
        toolbar.addWidget(self.btn_view_mode)
        
        toolbar.addWidget(self.btn_import)
        toolbar.addWidget(self.btn_export)
        layout.addLayout(toolbar)
        filter_row = QtWidgets.QHBoxLayout()
        self.filter_input = QtWidgets.QLineEdit()
        self.filter_input.setPlaceholderText(
            self.t("filter_placeholder",
                   "Filter by name, tag, code, lang:python, regex:/.../ ... (Ctrl+K)"))
        self.filter_input.textChanged.connect(self.on_filter_changed)
        # Search history: a drop-down of the last filters, saved with settings.
        self.filter_history_btn = QtWidgets.QToolButton()
        self.filter_history_btn.setText("\u2934 " + self.t("filter_history", "History"))
        self.filter_history_btn.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.filter_history_menu = QtWidgets.QMenu(self.filter_history_btn)
        self.filter_history_btn.setMenu(self.filter_history_menu)
        self.filter_history_menu.aboutToShow.connect(self._rebuild_filter_history)
        self.filter_favorites_btn = QtWidgets.QPushButton("\u2606 Favorites only")
        self.filter_favorites_btn.setCheckable(True)
        self.filter_favorites_btn.toggled.connect(self.on_favorites_filter_toggled)
        filter_row.addWidget(self.filter_input)
        filter_row.addWidget(self.filter_history_btn)
        filter_row.addWidget(self.filter_favorites_btn)
        layout.addLayout(filter_row)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        form_widget = QtWidgets.QWidget()
        form_layout = QtWidgets.QVBoxLayout(form_widget)

        self.script_tree = QtWidgets.QTreeWidget()
        self.script_tree.setAlternatingRowColors(True)
        self.script_tree.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.script_tree.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.script_tree.setColumnCount(4)
        self.script_tree.setHeaderLabels(["Name", "Category", "Tags", "Date"])
        self.script_tree.itemDoubleClicked.connect(self.edit_selected_script)
        self.script_tree.itemSelectionChanged.connect(self.on_list_selection_changed)
        self.script_tree.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.script_tree.customContextMenuRequested.connect(self.show_context_menu)
        # Enable context menu on header
        self.script_tree.header().setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.script_tree.header().customContextMenuRequested.connect(self.show_header_context_menu)

        form_layout.addWidget(self.script_tree)
        splitter.addWidget(form_widget)

        self.script_form = ScriptForm(self, self.save_script, self.clear_form, self.copy_form_code)
        self.script_form.hide()
        self.script_preview = ScriptPreview(self)
        splitter.addWidget(self.script_preview)
        splitter.setSizes([430, 670])
        layout.addWidget(splitter)

    # ---------------------------------------------------------------- filtering
    def _compile_filter(self, query):
        """Compile the quick filter; supports regex:/.../ for power users."""
        text = query.strip()
        flags = re.IGNORECASE
        # An explicit regex:/.../ prefix searches the raw pattern in the code.
        if text.lower().startswith("regex:"):
            pattern = text[6:].strip()
            # Support the /pattern/flags shorthand for the regex mode.
            m = re.match(r"^/(.*)/([iIsSmMx]*)$", pattern, re.DOTALL)
            if m:
                pattern, flagchars = m.group(1), m.group(2)
                flags = 0
                if "i" in flagchars.lower():
                    flags |= re.IGNORECASE
                if "s" in flagchars.lower():
                    flags |= re.DOTALL
                if "m" in flagchars.lower():
                    flags |= re.MULTILINE
                if "x" in flagchars.lower():
                    flags |= re.VERBOSE
                if "I" in flagchars:               # case sensitive override
                    flags &= ~re.IGNORECASE
            try:
                return re.compile(pattern, flags), "regex"
            except re.error:
                return None, "regex"
        return re.compile(re.escape(text), flags), "text"

    def filter_matches_code(self, script, query):
        """True when the query matches the script's code (text or regex)."""
        pattern, mode = self._compile_filter(query)
        if pattern is None:
            return False
        code = script.get("code", "") or ""
        if mode == "regex":
            return pattern.search(code) is not None
        return pattern.search(code) is not None

    def visible_scripts(self):
        """Apply the quick text filter and the favorites filter to the list.

        The filter searches name, category, tags and description, and — when
        nothing else matched — also the script's code, so a phrase typed from
        memory of the implementation finds its script.  A `regex:...` prefix
        switches to a regular-expression search over everything, including the
        code.  A `lang:xxx` or `cat:xxx` prefix narrows to one language.
        """
        raw = self.filter_text.strip()
        query = raw.lower()
        category_filter = None
        # A `lang:python` / `cat:sql` prefix narrows the search to one language.
        m = re.match(r"^(?:lang|cat|category):(\S+)\s*", raw, re.IGNORECASE)
        if m:
            category_filter = m.group(1).lower()
            raw = raw[m.end():]
            query = raw.lower()
        regex_mode = query.lower().startswith("regex:")
        code_pattern = None
        if regex_mode:
            code_pattern, _mode = self._compile_filter(raw)
        result = []
        for script in self.scripts:
            if is_separator(script):
                continue
            if self.filter_favorites and not script.get("favorite"):
                continue
            if category_filter is not None and \
                    category_filter not in script.get("category", "").lower():
                continue
            if query:
                haystack = " ".join([
                    script.get("name", ""), script.get("category", ""),
                    script.get("tags", ""), script.get("desc", ""),
                ]).lower()
                matched = query in haystack
                if not matched:
                    # Fall through to the code search: the phrase was not in
                    # the metadata, so try the implementation itself.
                    if regex_mode:
                        matched = (code_pattern is not None and
                                   code_pattern.search(script.get("code", "") or "")
                                   is not None)
                    else:
                        matched = query in (script.get("code", "") or "").lower()
                if not matched:
                    continue
            result.append(script)
        return result

    def highlight_matches(self, item, query):
        """Bold+highlight the parts of the name cell that match the filter."""
        if not query:
            return
        pattern, mode = self._compile_filter(query)
        if pattern is None:
            return
        column = 1 if self.show_row_numbers() else 0
        text = item.text(column)
        font = item.font(column)
        ranges = []
        for match in pattern.finditer(text):
            if match.start() != match.end():
                ranges.append((match.start(), match.end() - match.start()))
        if not ranges:
            return
        font.setBold(True)
        item.setFont(column, font)
        item.setData(column, QtCore.Qt.UserRole + 2, (text, ranges))
        item.setForeground(column, QtGui.QColor("#ffd766"))
    def focus_filter(self):
        self.tabs.setCurrentIndex(0)
        self.filter_input.setFocus()
        self.filter_input.selectAll()

    def _install_global_shortcuts(self):
        """Bind the application-wide keyboard shortcuts.

        These use Qt.WindowShortcut context so they fire wherever the main
        window has focus — including inside the editor, the search tab and the
        diff viewer — without colliding with the editor's own widget-level
        shortcuts (which use WidgetWithChildrenShortcut and win first for keys
        they both claim).

        Shortcut map:
          Ctrl+Shift+F — jump to the Search tab (advanced search)
          Ctrl+D       — duplicate the selected script(s)
          Ctrl+H       — show the version history of the selected script
          F2           — rename the selected script (inline prompt)
          Delete       — delete the selected script(s)
          Ctrl+/       — toggle comment in the editor (delegated to the tab)
        """
        bindings = (
            # (key sequence, handler, text-safe)
            # The third element marks shortcuts that must NOT fire while a
            # text-input widget has focus (otherwise pressing Delete or F2
            # inside an editing field would destroy a script or pop a dialog).
            ("Ctrl+Shift+F", self.goto_search_tab, False),
            ("Ctrl+D", self.duplicate_selected_script, True),
            ("Ctrl+H", self.show_script_history, True),
            ("F2", self.rename_selected_script, True),
            ("Delete", self.delete_selected_script, True),
            ("Ctrl+/", self.toggle_comment_in_editor, True),
        )
        self._global_shortcuts = []
        for keys, callback, guard_text in bindings:
            shortcut = QtWidgets.QShortcut(QtGui.QKeySequence(keys), self)
            if guard_text:
                # Intercept: only run the callback when no text field has focus.
                shortcut.activated.connect(
                    lambda cb=callback: self._run_when_not_editing(cb))
            else:
                shortcut.activated.connect(callback)
            self._global_shortcuts.append(shortcut)

    def _run_when_not_editing(self, callback):
        """Run a shortcut callback only when a text widget is not focused.

        This is what keeps Delete/F2/Ctrl+D from destroying a script while the
        user is typing in a rename dialog, the filter box or the editor: a
        QLineEdit/QTextEdit/CodeEditor consumes those keys for text editing,
        and the shortcut is skipped.
        """
        focus = QtWidgets.QApplication.focusWidget()
        if isinstance(focus, (QtWidgets.QLineEdit, QtWidgets.QTextEdit,
                              QtWidgets.QPlainTextEdit)):
            return
        callback()

    def goto_search_tab(self):
        """Focus the Search tab (Ctrl+Shift+F) and put the cursor in its box."""
        tab = getattr(self, "search_tab", None)
        if tab is not None:
            self.tabs.setCurrentWidget(tab)
            # If the tab exposes a search input, focus it so typing starts a
            # new search right away.
            for attr in ("search_edit", "query_edit", "input_search",
                         "search_input", "find_edit"):
                widget = getattr(tab, attr, None)
                if isinstance(widget, QtWidgets.QLineEdit):
                    widget.setFocus()
                    widget.selectAll()
                    return
            for line_edit in tab.findChildren(QtWidgets.QLineEdit):
                line_edit.setFocus()
                line_edit.selectAll()
                return
            return
        # Fallback: the quick filter on the Scripts tab.
        self.focus_filter()

    def rename_selected_script(self):
        """F2: rename the selected script with a small input dialog."""
        script = self.selected_script()
        if not script:
            self.show_message(self.t("info"), self.t("select_script"))
            return
        current = script.get("name", "")
        new_name, ok = QtWidgets.QInputDialog.getText(
            self, self.t("rename_title", "Rename script"),
            self.t("rename_prompt", "New name:"),
            QtWidgets.QLineEdit.Normal, current,
        )
        if not ok:
            return
        new_name = new_name.strip()
        if not new_name or new_name == current:
            return
        script["name"] = new_name
        script["date"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.save_data_now()
        self.refresh_list()
        self.notify_audit("module_updated",
                          where=current,
                          detail=self.t("renamed_to", "renamed to: {name}",
                                        name=new_name))

    def toggle_comment_in_editor(self):
        """Ctrl+/: toggle a comment on the current line(s) in the editor.

        The editor widget owns the text buffer, so this only forwards the key
        there; when the editor is not focused the quick filter is opened
        instead, so the shortcut always does something sensible.
        """
        editor = getattr(self, "editor_tab", None)
        handler = getattr(editor, "toggle_comment", None) if editor else None
        if callable(handler):
            handler()
            return
        # Editor does not implement it: fall back to focusing the filter.
        self.focus_filter()

    def on_filter_changed(self, text):
        self.filter_text = text
        if text.strip():
            self.remember_search(text.strip())
        self.refresh_list()

    # ------------------------------------------------------------ search history
    def search_history(self):
        """The saved search queries, newest first (persisted in settings)."""
        history = self.settings.get("search_history", [])
        if isinstance(history, list):
            return [str(item) for item in history if str(item).strip()]
        return []

    def remember_search(self, query):
        """Put a query at the top of the search history (dedup, max 25)."""
        try:
            history = [str(item) for item in self.settings.get("search_history", [])
                       if str(item).strip()]
        except Exception:
            history = []
        if query in history:
            history.remove(query)
        history.insert(0, query)
        self.settings["search_history"] = history[:25]
        self.save_settings()

    def _rebuild_filter_history(self):
        """Fill the history drop-down with the saved searches."""
        menu = self.filter_history_menu
        menu.clear()
        history = self.search_history()
        if not history:
            empty = menu.addAction(self.t("filter_history_empty", "(no history yet)"))
            empty.setEnabled(False)
            return
        clear = menu.addAction(self.t("filter_history_clear", "Clear history"))
        clear.triggered.connect(self._clear_search_history)
        menu.addSeparator()
        for query in history:
            label = query if len(query) <= 48 else query[:45] + "..."
            action = menu.addAction(label)
            action.triggered.connect(
                lambda _checked=False, value=query: self._apply_history_query(value))

    def _apply_history_query(self, query):
        self.filter_input.setText(query)
        self.refresh_list()

    def _clear_search_history(self):
        self.settings["search_history"] = []
        self.save_settings()

    def on_favorites_filter_toggled(self, checked):
        self.filter_favorites = bool(checked)
        self.filter_favorites_btn.setText(self.t("favorites_only") if checked else f"\u2606 {self.t('favorites_only')}")
        self.refresh_list()

    def toggle_favorite_selected(self):
        scripts = self.selected_scripts()
        if not scripts:
            QtWidgets.QMessageBox.information(self, self.t("info"), self.t("select_script"))
            return
        # If every selected script is already a favorite, clear them all;
        # otherwise mark them all as favorites.
        make_favorite = not all(s.get("favorite") for s in scripts)
        for script in scripts:
            script["favorite"] = make_favorite
        save_data(DATA_FILE, self.scripts)
        self.refresh_list()

    def sort_scripts(self):
        self.sort_ascending = not self.sort_ascending
        reverse = not self.sort_ascending
        # Sort only the scripts; separators keep their place so the manual
        # layout the user built is not shuffled along with the names.
        separators = self.separator_rows()
        ordered = sorted(self.scripts_only(),
                         key=lambda s: s.get("name", "").lower(),
                         reverse=reverse)
        self.scripts = ordered + separators
        self.save_data_now()
        self.refresh_list()
        self.btn_sort_scripts.setText("Sort Z-A" if reverse else "Sort A-Z")

    def selected_scripts(self):
        """Return every selected script entry, in list order (deduplicated)."""
        ids = []
        for item in self.script_tree.selectedItems():
            script_id = item.data(0, QtCore.Qt.UserRole)
            if script_id is not None:
                ids.append(script_id)
        by_id = {s.get("id"): s for s in self.scripts if not is_separator(s)}
        return [by_id[i] for i in ids if i in by_id]

    def selected_script(self):
        """Return the primary selected script entry (first selection), or None."""
        selected = self.script_tree.selectedItems()
        if not selected:
            return None
        script_id = selected[0].data(0, QtCore.Qt.UserRole)
        return self.find_script(script_id)

    def find_script(self, script_id):
        """Look a script up by id (separators excluded)."""
        return next((s for s in self.scripts
                     if s.get("id") == script_id and not is_separator(s)), None)

    def open_in_diff(self):
        """Send the selected script to the Diff tab and show it there."""
        script = self.selected_script()
        if script is None:
            self.show_message(self.t("info"), self.t("select_script"))
            return
        self.diff_tab._right = self.diff_tab._entry_to_source(script)
        self.diff_tab.recompute()
        self.tabs.setCurrentWidget(self.diff_tab)

    def show_context_menu(self, point):
        index = self.script_tree.indexAt(point)
        if not index.isValid():
            # Right-click on empty space: offer the list customization actions.
            self.show_empty_area_menu(point)
            return
        item = self.script_tree.itemAt(point)
        if item is not None and item.data(0, QtCore.Qt.UserRole + 1) == SEPARATOR_KIND:
            self.show_separator_menu(point, item)
            return
        menu = QtWidgets.QMenu(self)
        menu.addAction(self.t("edit"), self.edit_selected_script)
        menu.addAction(self.t("run"), self.run_selected_script)
        menu.addAction(self.t("preview"), self.preview_selected_script)
        menu.addAction(self.t("toggle_favorite"), self.toggle_favorite_selected)
        menu.addAction(self.t("duplicate"), self.duplicate_selected_script)
        menu.addAction(self.t("history"), self.show_script_history)
        menu.addAction(self.t("diff"), self.open_in_diff)
        # --- audit integration: audit the script / jump to the audit tab ---
        menu.addSeparator()
        menu.addAction(self.t("audit_this_script", "Audit this script"),
                       lambda: self.audit_script(self.selected_script()))
        menu.addAction(self.t("audit_open_tab", "Open Audit tab"),
                       lambda: self.open_audit_tab())
        menu.addAction(self.t("audit_run_security", "Security audit"),
                       self.run_security_audit)
        menu.addAction(self.t("audit_export_menu", "Export audit report"),
                       lambda: self.export_audit("txt"))
        menu.addAction(self.t("export_file"), self.export_script_file)
        menu.addSeparator()
        menu.addAction(self.t("copy"), self.copy_form_code)
        menu.addAction(self.t("copy") + " (" + self.t("name") + " + " + self.t("code") + ")", self.copy_script_as_text)
        menu.addSeparator()
        menu.addAction(self.t("delete"), self.delete_selected_script)
        menu.exec_(self.script_tree.mapToGlobal(point))

    # --------------------------------------------------- list customization
    def show_empty_area_menu(self, point):
        """Context menu for the empty area below the last row."""
        menu = QtWidgets.QMenu(self)
        menu.addAction(self.t("menu_add_script"), self.add_new_script)
        menu.addSeparator()

        separators = menu.addMenu(self.t("menu_separators"))
        separators.addAction(self.t("sep_add_top"), lambda: self.add_separator(0))
        separators.addAction(self.t("sep_add_bottom"), lambda: self.add_separator(len(self.scripts_only())))
        separators.addAction(self.t("sep_add_before_first"), lambda: self.add_separator(0))
        edit_separator = separators.addMenu(self.t("sep_edit"))
        rows = self.separator_rows()
        if rows:
            for separator in rows:
                label = separator.get("name", "") or self.t("sep_unnamed")
                edit_separator.addAction(label, lambda sid=separator.get("id"): self.edit_separator(sid))
        else:
            edit_separator.setEnabled(False)
        remove_separator = separators.addMenu(self.t("sep_remove"))
        if rows:
            for separator in rows:
                label = separator.get("name", "") or self.t("sep_unnamed")
                remove_separator.addAction(label, lambda sid=separator.get("id"): self.remove_separator(sid))
        else:
            remove_separator.setEnabled(False)
        separators.addSeparator()
        toggle = separators.addAction(self.t("sep_hide_all"), self.toggle_separators)
        toggle.setCheckable(True)
        toggle.setChecked(not self.setting("separators_enabled", True))
        clear = separators.addAction(self.t("sep_clear_all"), self.clear_separators)
        clear.setEnabled(bool(rows))
        menu.addSeparator()
        columns = menu.addMenu(self.t("menu_columns"))
        for key, title in (("favorite", "favorite_col"), ("date", "date"),
                           ("tags", "tags_col"), ("category", "category_col"),
                           ("name", "name")):
            action = columns.addAction(self.t(title), lambda k=key: self.toggle_column(k))
            action.setCheckable(True)
            action.setChecked(bool((self.setting("columns", {}) or {}).get(key, True)))
            if key == "name":
                action.setEnabled(False)
        columns.addSeparator()
        row_numbers = columns.addAction(self.t("col_row_numbers"), self.toggle_row_numbers)
        row_numbers.setCheckable(True)
        row_numbers.setChecked(self.show_row_numbers())
        columns.addAction(self.t("col_reset"), self.reset_columns)
        menu.addSeparator()

        group = menu.addAction(self.t("group_by_category"), self.toggle_grouping)
        group.setCheckable(True)
        group.setChecked(bool(self.setting("group_by_category", False)))
        zebra = menu.addAction(self.t("zebra_stripes"), self.toggle_zebra)
        zebra.setCheckable(True)
        zebra.setChecked(bool(self.setting("zebra_stripes", True)))
        bold = menu.addAction(self.t("bold_separators"), self.toggle_bold_separators)
        bold.setCheckable(True)
        bold.setChecked(bool(self.setting("bold_separators", True)))
        menu.addAction(self.t("sep_default_label"), self.set_default_separator_label)
        menu.addSeparator()
        menu.addAction(self.t("configure_interpreters", "Configure Interpreters..."), self.configure_interpreters)
        menu.addSeparator()

        # Add view mode toggle
        is_tree = bool(self.setting("group_by_category", False))
        view_mode_label = self.t("list_view") if is_tree else self.t("tree_view")
        view_mode = menu.addAction(view_mode_label, self.toggle_view_mode)
        menu.addSeparator()

        expand = menu.addAction(self.t("list_expand_all"), self.script_tree.expandAll)
        collapse = menu.addAction(self.t("list_collapse_all"), self.script_tree.collapseAll)
        # Enable expand/collapse only in tree view mode
        expand.setEnabled(is_tree)
        collapse.setEnabled(is_tree)
        menu.addAction(self.t("list_reset_view"), self.reset_list_customization)
        menu.addSeparator()
        menu.addAction(self.t("statistics"), self.show_statistics)
        menu.addAction(self.t("report"), self.export_report)
        menu.exec_(self.script_tree.viewport().mapToGlobal(point))

    def show_separator_menu(self, point, item):
        """Context menu shown when a separator row itself is right-clicked."""
        separator_id = item.data(0, QtCore.Qt.UserRole)
        menu = QtWidgets.QMenu(self)
        menu.addAction(self.t("sep_rename"), lambda: self.edit_separator(separator_id))
        menu.addAction(self.t("sep_move_up"), lambda: self.move_separator(separator_id, -1))
        menu.addAction(self.t("sep_move_down"), lambda: self.move_separator(separator_id, 1))
        menu.addSeparator()
        menu.addAction(self.t("sep_remove"), lambda: self.remove_separator(separator_id))
        menu.exec_(self.script_tree.viewport().mapToGlobal(point))

    def show_header_context_menu(self, point):
        """Context menu shown when the header is right-clicked."""
        menu = QtWidgets.QMenu(self)

        # View mode toggle
        is_tree = bool(self.setting("group_by_category", False))
        view_mode_label = self.t("list_view") if is_tree else self.t("tree_view")
        view_mode = menu.addAction(view_mode_label, self.toggle_view_mode)

        menu.addSeparator()

        # Expand/Collapse options (only enabled in tree view)
        expand = menu.addAction(self.t("list_expand_all"), self.script_tree.expandAll)
        collapse = menu.addAction(self.t("list_collapse_all"), self.script_tree.collapseAll)
        expand.setEnabled(is_tree)
        collapse.setEnabled(is_tree)

        menu.addSeparator()

        # Other common options
        columns = menu.addMenu(self.t("col_reset", "Columns"))
        for key, title in (("favorite", "favorite_col"), ("date", "date"),
                           ("tags", "tags_col"), ("category", "category_col"),
                           ("name", "name")):
            action = columns.addAction(self.t(title), lambda k=key: self.toggle_column(k))
            action.setCheckable(True)
            action.setChecked(bool((self.setting("columns", {}) or {}).get(key, True)))
            if key == "name":
                action.setEnabled(False)

        menu.exec_(self.script_tree.header().mapToGlobal(point))
    def add_separator(self, position):
        """Insert a separator at the given block position, asking for its label."""
        label, accepted = QtWidgets.QInputDialog.getText(
            self, self.t("sep_add"), self.t("sep_prompt"),
            QtWidgets.QLineEdit.Normal, self.setting("empty_separator_label", ""),
        )
        if not accepted:
            return
        separator = self.insert_separator_at(position, label)
        if not self.setting("separators_enabled", True):
            self.set_setting("separators_enabled", True, refresh=False)
        self.save_data_now()
        self.refresh_list()
        self.script_tree.scrollToItem(
            next((self.script_tree.topLevelItem(i)
                  for i in range(self.script_tree.topLevelItemCount())
                  if self.script_tree.topLevelItem(i).data(0, QtCore.Qt.UserRole) == separator["id"]),
                 None) or self.script_tree.topLevelItem(0)
        )

    def edit_separator(self, separator_id):
        separator = next((s for s in self.scripts if is_separator(s) and s.get("id") == separator_id), None)
        if separator is None:
            return
        label, accepted = QtWidgets.QInputDialog.getText(
            self, self.t("sep_rename"), self.t("sep_prompt"),
            QtWidgets.QLineEdit.Normal, separator.get("name", ""),
        )
        if not accepted:
            return
        separator["name"] = label.strip()
        self.save_data_now()
        self.refresh_list()

    def remove_separator(self, separator_id):
        self.scripts = [s for s in self.scripts
                        if not (is_separator(s) and s.get("id") == separator_id)]
        self.save_data_now()
        self.refresh_list()

    def move_separator(self, separator_id, direction):
        """Move a separator one script-block boundary up or down."""
        separators = {s.get('id'): s for s in self.separator_rows()}
        if separator_id not in separators:
            return

        labels = {}          # boundary number -> [(id, name), ...]
        scripts = []
        for entry in self.scripts:
            if is_separator(entry):
                labels.setdefault(len(scripts), []).append(entry)
            else:
                scripts.append(entry)

        # Find the boundary this label currently sits on.
        current = None
        for boundary, group in labels.items():
            for entry in group:
                if entry.get('id') == separator_id:
                    current = boundary
        if current is None:
            return
        moving = next(e for e in labels[current] if e.get('id') == separator_id)
        target = current + (-1 if direction < 0 else 1)
        if target < 0 or target > len(scripts):
            return

        labels[current].remove(moving)
        if not labels[current]:
            del labels[current]
        labels.setdefault(target, []).append(moving)

        rebuilt = []
        for boundary in range(len(scripts) + 1):
            rebuilt.extend(labels.get(boundary, []))
            if boundary < len(scripts):
                rebuilt.append(scripts[boundary])
        self.scripts = rebuilt
        self.save_data_now()
        self.refresh_list()
    def toggle_separators(self):
        self.set_setting("separators_enabled", not self.setting("separators_enabled", True))

    def clear_separators(self):
        rows = self.separator_rows()
        if not rows:
            return
        if QtWidgets.QMessageBox.question(
            self, self.t("confirm"),
            self.t("sep_confirm_clear", count=len(rows)),
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
        ) != QtWidgets.QMessageBox.Yes:
            return
        self.scripts = [s for s in self.scripts if not is_separator(s)]
        self.save_data_now()
        self.refresh_list()

    def toggle_column(self, key):
        columns = dict(self.setting("columns", {}) or {})
        columns[key] = not columns.get(key, True)
        self.set_setting("columns", columns)

    def toggle_row_numbers(self):
        self.set_setting("show_row_numbers", not self.show_row_numbers())

    def reset_columns(self):
        self.settings["columns"] = dict(DEFAULT_SETTINGS["columns"])
        self.set_setting("show_row_numbers", False, refresh=False)
        self.refresh_list()

    def toggle_grouping(self):
        self.set_setting("group_by_category", not self.setting("group_by_category", False))
        # Update view mode button if it exists
        if hasattr(self, 'btn_view_mode'):
            is_tree = bool(self.setting("group_by_category", False))
            self.btn_view_mode.setChecked(is_tree)
            self.btn_view_mode.setText("List View" if is_tree else "Tree View")

    def toggle_view_mode(self):
        """Toggle between tree view (grouped) and list view (flat)."""
        current = self.setting("group_by_category", False)
        new_value = not current
        self.set_setting("group_by_category", new_value)
        # Update button text and state
        if hasattr(self, 'btn_view_mode'):
            self.btn_view_mode.setChecked(new_value)
            self.btn_view_mode.setText("Tree View" if not new_value else "List View")

    def toggle_zebra(self):
        self.set_setting("zebra_stripes", not self.setting("zebra_stripes", True))

    def toggle_bold_separators(self):
        self.set_setting("bold_separators", not self.setting("bold_separators", True))

    def set_default_separator_label(self):
        label, accepted = QtWidgets.QInputDialog.getText(
            self, self.t("sep_default_label"), self.t("sep_prompt"),
            QtWidgets.QLineEdit.Normal, self.setting("empty_separator_label", ""),
        )
        if accepted:
            self.set_setting("empty_separator_label", label.strip(), refresh=False)

    def configure_interpreters(self):
        """Open a dialog to configure custom interpreter paths."""
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle(self.t("interpreters_title"))
        dialog.resize(600, 400)
        layout = QtWidgets.QVBoxLayout(dialog)

        # Hint label
        hint = QtWidgets.QLabel(self.t("interpreters_hint"))
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #9aa0a6; font-style: italic;")
        layout.addWidget(hint)

        # Form layout for interpreter paths
        form = QtWidgets.QFormLayout()
        interpreters = self.settings.get("interpreters", {})

        # Language labels and input fields
        self.interpreter_edits = {}
        languages = [
            ("python", "Python"),
            ("bash", "Bash"),
            ("powershell", "PowerShell"),
            ("node", "Node.js"),
            ("batch", "Batch"),
            ("vbscript", "VBScript")
        ]

        for lang_key, lang_name in languages:
            row = QtWidgets.QHBoxLayout()
            edit = QtWidgets.QLineEdit(interpreters.get(lang_key, ""))
            edit.setPlaceholderText(f"System default")
            self.interpreter_edits[lang_key] = edit

            browse_btn = QtWidgets.QPushButton(self.t("browse"))
            browse_btn.clicked.connect(lambda checked, lk=lang_key, le=edit: self._browse_interpreter(lk, le))

            row.addWidget(edit, 1)
            row.addWidget(browse_btn)
            form.addRow(f"{lang_name}:", row)

        layout.addLayout(form)

        # Buttons
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        if dialog.exec_() == QtWidgets.QDialog.Accepted:
            # Save interpreter paths
            new_interpreters = {}
            for lang_key, _ in languages:
                path = self.interpreter_edits[lang_key].text().strip()
                new_interpreters[lang_key] = path
            self.settings["interpreters"] = new_interpreters
            self.save_settings()
            QtWidgets.QMessageBox.information(
                self, self.t("information"), self.t("interpreters_saved")
            )

    def _browse_interpreter(self, language, line_edit):
        """Browse for an interpreter executable."""
        if language == "python":
            pattern = "Python executable (python.exe python3 python);;All files (*.*)"
        elif language == "bash":
            pattern = "Bash executable (bash.exe bash);;All files (*.*)"
        elif language == "powershell":
            pattern = "PowerShell (pwsh.exe powershell.exe);;All files (*.*)"
        elif language == "node":
            pattern = "Node.js (node.exe node);;All files (*.*)"
        elif language == "batch":
            pattern = "Command (cmd.exe);;All files (*.*)"
        elif language == "vbscript":
            pattern = "Windows Script Host (cscript.exe wscript.exe);;All files (*.*)"
        else:
            pattern = "All files (*.*)"

        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, self.t("interpreter_path"), "", pattern
        )
        if path:
            line_edit.setText(path)

    def reset_list_customization(self):
        """Restore the default view, leaving the user's separators in place."""
        self.settings["columns"] = dict(DEFAULT_SETTINGS["columns"])
        for key in ("group_by_category", "separators_enabled", "zebra_stripes",
                    "show_row_numbers", "bold_separators"):
            self.settings[key] = DEFAULT_SETTINGS[key]
        self.save_settings()
        self.refresh_list()

    def save_data_now(self):
        """Persist the script list, reporting a failure instead of crashing."""
        try:
            save_data(DATA_FILE, self.scripts)
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, self.t("export_error"), str(exc))
    def copy_script_as_text(self, item=None):
        """Copy a script (header + code) to the clipboard, not just the code."""
        if isinstance(item, QtWidgets.QTreeWidgetItem):
            script_id = item.data(0, QtCore.Qt.UserRole)
        else:
            script = self.selected_script()
            script_id = script.get("id") if script else None
        script = next((s for s in self.scripts if s.get("id") == script_id), None)
        if not script:
            return
        block = (
            f"# {script.get('name', '')}\n"
            f"# Category: {script.get('category', '')}\n"
            f"# Tags: {script.get('tags', '')}\n"
            f"# {script.get('desc', '')}\n\n"
            f"{script.get('code', '')}\n"
        )
        QtWidgets.QApplication.clipboard().setText(block)
        self.show_message(self.t("info"), self.t("copied_script"))

    def add_new_script(self):
        """Open a blank script editor dialog."""
        self.clear_form()
        self._open_script_editor_dialog()

    def _open_script_editor_dialog(self, entry=None):
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle(self.t("edit", "Edit script"))
        dialog.resize(720, 620)
        form = ScriptForm(dialog, self.save_script, self.clear_form,
                          self.copy_form_code)
        self.script_form = form
        self._script_editor_dialog = dialog
        if entry is not None:
            self.editing_id = entry.get("id")
            form.load_entry(entry)
        form.setEnabled(True)
        layout = QtWidgets.QVBoxLayout(dialog)
        layout.addWidget(form)
        form.script_name.setFocus()
        dialog.finished.connect(lambda: setattr(self, "_script_editor_dialog", None))
        dialog.show()

    def clear_form(self):
        self.editing_id = None
        self.script_form.clear_fields()
        self.script_form.setEnabled(True)

    def save_script(self):
        data = self.script_form.get_entry_data()
        name = data["name"]
        code = data["code"]
        if not name or not code:
            QtWidgets.QMessageBox.warning(self, self.t("warning"), self.t("fill_required"))
            return

        entry = {
            "id": self.editing_id or str(dt.datetime.now().timestamp()),
            "name": name,
            "category": data["category"],
            "tags": data["tags"],
            "desc": data["desc"],
            "code": code,
            "date": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "favorite": bool(self.script_form.favorite_btn.isChecked()),
            "history": [],
        }

        was_edit = bool(self.editing_id)
        if self.editing_id:
            for index, existing in enumerate(self.scripts):
                if existing.get("id") != self.editing_id:
                    continue
                history = list(existing.get("history", []))
                # Only keep a revision when something actually changed.
                if script_to_dict(existing) != script_to_dict(entry):
                    history.append(script_to_dict(existing))
                entry["history"] = history[-25:]
                if len(history) > 25:
                    entry["history_truncated"] = True
                entry["favorite"] = entry["favorite"] or bool(existing.get("favorite"))
                entry["date_created"] = existing.get("date_created", existing.get("date", entry["date"]))
                self.scripts[index] = entry
                break
        else:
            entry["date_created"] = entry["date"]
            self.scripts.append(entry)
        try:
            save_data(DATA_FILE, self.scripts)
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, self.t("export_error"), str(exc))
            return
        self.refresh_list()
        # Announce the save on the shared audit bus so the timeline records
        # it and the matching automatic audit runs.
        self._announce_script_save(was_edit, entry)
        self.clear_form()
        self.show_message(self.t("information"), self.t("saved"))
        dialog = getattr(self, "_script_editor_dialog", None)
        if dialog is not None:
            dialog.close()
    # ------------------------------------------------- list customization
    def setting(self, key, default=None):
        """Read one saved preference, falling back to its default."""
        return self.settings.get(key, DEFAULT_SETTINGS.get(key, default))

    def save_settings(self):
        """Persist the UI preferences; failures must not break the session."""
        try:
            save_settings(SETTINGS_FILE, self.settings)
        except Exception as exc:
            self.show_message(self.t("export_error"), str(exc))

    def set_setting(self, key, value, refresh=True):
        """Write one preference, persist it and rebuild the list."""
        self.settings[key] = value
        self.save_settings()
        if refresh:
            self.refresh_list()

    def separator_rows(self):
        """Every separator entry currently stored in the list, in order."""
        return [entry for entry in self.scripts if is_separator(entry)]

    def scripts_only(self):
        """Only the real script entries (separators filtered out)."""
        return [entry for entry in self.scripts if not is_separator(entry)]

    def show_row_numbers(self):
        return bool(self.setting("show_row_numbers", False))

    def list_row_values(self, script):
        """Build the cell texts for one script, in the fixed column order."""
        return {
            "name": script.get("name", ""),
            "category": script.get("category", ""),
            "tags": script.get("tags", ""),
            "date": script.get("date", ""),
            "favorite": "\u2605" if script.get("favorite") else "",
        }

    def visible_columns(self):
        """Column keys the user left enabled, always keeping the name column."""
        columns = self.setting("columns", {}) or {}
        active = [key for key in LIST_COLUMNS if columns.get(key, True)]
        if "name" not in active:
            active.insert(0, "name")
        return active

    def _make_list_item(self, script):
        """One tree row for a script, honouring the visible columns."""
        values = self.list_row_values(script)
        cells = [values[key] for key in self.visible_columns()]
        if self.show_row_numbers():
            cells.insert(0, str(self.script_tree.topLevelItemCount() + 1))
        item = QtWidgets.QTreeWidgetItem(cells)
        item.setData(0, QtCore.Qt.UserRole, script.get("id"))
        if script.get("favorite"):
            item.setForeground(0, QtGui.QColor("#f7c66d"))
        return item
    def _make_separator_item(self, separator):
        """A full-width, non-selectable row carrying a separator label."""
        label = separator.get("name", "") or self.t("sep_unnamed")
        cells = [label] + [""] * (len(self.visible_columns()) - 1)
        if self.show_row_numbers():
            cells.insert(0, "")
        item = QtWidgets.QTreeWidgetItem(cells)
        item.setData(0, QtCore.Qt.UserRole, separator.get("id"))
        item.setData(0, QtCore.Qt.UserRole + 1, SEPARATOR_KIND)
        font = item.font(0)
        font.setBold(bool(self.setting("bold_separators", True)))
        item.setFont(0, font)
        item.setForeground(0, QtGui.QColor("#8ab4f8"))
        item.setBackground(0, QtGui.QColor("#2a2d2e"))
        # Separators are labels, not scripts: keep them out of the selection.
        item.setFlags(QtCore.Qt.ItemIsEnabled)
        return item

    def apply_list_headers(self):
        """Column headers matching the visible columns and language."""
        labels = {
            "name": self.t("name"),
            "category": self.t("category_col"),
            "tags": self.t("tags_col"),
            "date": self.t("date"),
            "favorite": self.t("favorite_col"),
        }
        headers = [labels[key] for key in self.visible_columns()]
        if self.show_row_numbers():
            headers.insert(0, "#")
        self.script_tree.setColumnCount(len(headers))
        self.script_tree.setHeaderLabels(headers)

    def separator_block_index(self, separator_id):
        """Position of a separator's block inside the ordered script list."""
        seen = 0
        for entry in self.scripts:
            if is_separator(entry):
                if entry.get("id") == separator_id:
                    return seen
            else:
                seen += 1
        return seen

    def insert_separator_at(self, position, label=""):
        """Insert a separator at the given script-block boundary."""
        separator = make_separator(label or self.setting("empty_separator_label", ""))
        boundary = max(0, position)
        scripts_seen = 0
        index = len(self.scripts)          # default: after everything
        for cursor, entry in enumerate(self.scripts):
            if is_separator(entry):
                continue
            if scripts_seen == boundary:
                index = cursor
                while index < len(self.scripts) and is_separator(self.scripts[index]):
                    index += 1
                break
            scripts_seen += 1
            if scripts_seen == boundary:
                index = cursor + 1
                while index < len(self.scripts) and is_separator(self.scripts[index]):
                    index += 1
                break
        self.scripts.insert(index, separator)
        return separator
    def refresh_list(self):
        self.apply_list_headers()
        self.script_tree.clear()
        self.script_tree.setAlternatingRowColors(bool(self.setting("zebra_stripes", True)))
        shown = self.visible_scripts()
        query = self.filter_text.strip()
        shown_ids = {script.get("id") for script in shown}
        separators_enabled = bool(self.setting("separators_enabled", True))
        if self.setting("group_by_category", False):
            self._populate_grouped(shown, separators_enabled)
        else:
            self._populate_flat(shown_ids, separators_enabled, query)
        total = len(self.scripts_only())
        if len(shown) != total:
            self.count_label.setText(f"{self.t('total_scripts')} {len(shown)} / {total}")
        else:
            self.count_label.setText(f"{self.t('total_scripts')} {total}")

    def _populate_flat(self, shown_ids, separators_enabled, query=""):
        """Render the list in the user's own order, separators included."""
        for entry in self.scripts:
            if is_separator(entry):
                if separators_enabled:
                    self.script_tree.addTopLevelItem(self._make_separator_item(entry))
                continue
            if entry.get("id") in shown_ids:
                item = self._make_list_item(entry)
                self.highlight_matches(item, query)
                self.script_tree.addTopLevelItem(item)

    def _populate_grouped(self, shown, separators_enabled):
        """Fill the tree grouped by category with a true tree structure."""
        by_category = collections.OrderedDict()
        for script in shown:
            by_category.setdefault(script.get("category", "") or "?", []).append(script)
        for category in sorted(by_category, key=lambda name: name.lower()):
            # Create parent node for category
            if separators_enabled:
                # Use separator as parent node
                parent = self._make_separator_item(make_separator(category))
            else:
                # Use simple header as parent node
                parent = QtWidgets.QTreeWidgetItem([category])
                parent.setForeground(0, QtGui.QColor("#8ab4f8"))
                parent.setFlags(QtCore.Qt.ItemIsEnabled)
            self.script_tree.addTopLevelItem(parent)
            # Add scripts as children of the category node
            for script in by_category[category]:
                child = self._make_list_item(script)
                parent.addChild(child)
            # Expand the category node to show children
            parent.setExpanded(True)

    def edit_selected_script(self, item=None):
        if item is None:
            selected = self.script_tree.selectedItems()
            if not selected:
                QtWidgets.QMessageBox.information(self, "Info", self.t("select_script"))
                return
            item = selected[0]
        script_id = item.data(0, QtCore.Qt.UserRole)
        if script_id is None:
            return
        entry = next((s for s in self.scripts if s.get("id") == script_id), None)
        if entry is None:
            return
        self._open_script_editor_dialog(entry)
    def on_list_selection_changed(self):
        selected = self.script_tree.selectedItems()
        if not selected:
            self.script_preview.show_empty()
            return
        if len(selected) > 1:
            self.script_preview.show_empty()
            self.count_label.setText(self.t("multi_selected").format(len(selected)))
            return
        script_id = selected[0].data(0, QtCore.Qt.UserRole)
        entry = next((s for s in self.scripts if s.get("id") == script_id), None)
        if entry is not None:
            self.script_preview.show_script(entry)

    def run_selected_script(self):
        scripts = self.selected_scripts()
        if not scripts:
            self.show_message(self.t("info"), self.t("select_script"))
            return
        if len(scripts) > 1:
            self.show_message(self.t("info"), self.t("multi_run_hint"))
            return
        entry = scripts[0]
        RunScriptDialog(self, entry.get("name", "Untitled"), entry.get("code", ""),
                        entry.get("category", "Python"), self.t).exec_()

    def delete_selected_script(self):
        scripts = self.selected_scripts()
        if not scripts:
            self.show_message(self.t("info"), self.t("select_script"))
            return
        if len(scripts) > 1:
            question = self.t("confirm_delete_multi").format(len(scripts))
        else:
            question = self.t("confirm_delete")
        if not QtWidgets.QMessageBox.question(self, self.t("confirm"), question, QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No):
            return
        ids = {s.get("id") for s in scripts}
        removed_names = [s.get("name", "") for s in scripts]
        self.scripts = [s for s in self.scripts if s.get("id") not in ids]
        save_data(DATA_FILE, self.scripts)
        self.refresh_list()
        # Tell the audit subsystem that modules were removed.
        self.notify_audit("module_updated",
                          where=", ".join(removed_names),
                          detail=self.t("audit_changed", "changed"))
        if len(ids) > 1:
            self.show_message(self.t("info"), self.t("deleted_multi").format(len(ids)))
        else:
            self.show_message(self.t("info"), self.t("deleted"))

    def copy_form_code(self):
        form = getattr(self, "script_form", None)
        code = form.code_edit.toPlainText().strip() if form is not None else ""
        if not code:
            selected = self.selected_script()
            code = selected.get("code", "").strip() if selected else ""
        if not code:
            return
        QtWidgets.QApplication.clipboard().setText(code)
        self.show_message(self.t("info"), self.t("copied"))

    def preview_selected_script(self):
        script = self.selected_script()
        if not script:
            QtWidgets.QMessageBox.information(self, self.t("info"), self.t("select_script"))
            return
        CodePreviewDialog(self, script.get("name", ""), script.get("code", ""),
                          script.get("category", "Python")).exec_()
    def duplicate_selected_script(self):
        scripts = self.selected_scripts()
        if not scripts:
            QtWidgets.QMessageBox.information(self, self.t("info"), self.t("select_script"))
            return
        now = dt.datetime.now()
        for offset, script in enumerate(scripts):
            clone = normalize_entry(dict(script))
            clone["id"] = str(now.timestamp() + offset)
            clone["name"] = f"{script.get('name', '')} (copy)"
            clone["date"] = now.strftime("%Y-%m-%d %H:%M:%S")
            clone["date_created"] = clone["date"]
            clone["history"] = []
            self.scripts.append(clone)
        save_data(DATA_FILE, self.scripts)
        self.refresh_list()
        self.notify_audit("module_installed",
                          where=", ".join(s.get("name", "") for s in scripts),
                          detail=self.t("duplicated", "duplicated"))
        if len(scripts) > 1:
            QtWidgets.QMessageBox.information(self, self.t("info"), self.t("duplicated_multi").format(len(scripts)))
        else:
            QtWidgets.QMessageBox.information(self, self.t("info"), self.t("duplicated"))

    def show_script_history(self):
        script = self.selected_script()
        if not script:
            QtWidgets.QMessageBox.information(self, self.t("info"), self.t("select_script"))
            return
        HistoryDialog(self, self, script).exec_()

    def show_import_menu(self):
        """Drop the import menu down from the Import... button."""
        self.import_menu.exec_(
            self.btn_import.mapToGlobal(
                QtCore.QPoint(0, self.btn_import.height())))

    def show_export_menu(self):
        """Drop the export menu down from the Export... button."""
        self.export_menu.exec_(
            self.btn_export.mapToGlobal(
                QtCore.QPoint(0, self.btn_export.height())))

    def export_report(self):
        """Save a Markdown report with all scripts, tags and descriptions."""
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, self.t("report"), "scripts_report.md", "Markdown files (*.md)")
        if not path:
            return
        lines = ["# ScriptDB report", "", f"Generated: {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", "", f"Total scripts: {len(self.scripts)}", ""]
        for script in self.scripts:
            lines.append(f"## {script.get('name', '')}")
            lines.append(f"- Category: {script.get('category', '')}")
            lines.append(f"- Tags: {script.get('tags', '') or '-'}")
            lines.append(f"- Favorite: {'yes' if script.get('favorite') else 'no'}")
            lines.append(f"- Updated: {script.get('date', '')}")
            lines.append("")
            lines.append(script.get("desc", "").strip() or "(no description)")
            lines.append("")
            lines.append("```" + (script.get("category", "").lower() or ""))
            lines.append(script.get("code", ""))
            lines.append("```")
            lines.append("")
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("\n".join(lines))
            QtWidgets.QMessageBox.information(self, self.t("info"), self.t("exported"))
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, self.t("export_error"), str(exc))
    @staticmethod
    def _palette_colors(dialog):
        """Palette-derived colors shared by the styled popup dialogs."""
        palette = dialog.palette()
        return {
            "text": palette.color(QtGui.QPalette.WindowText).darker(115).name(),
            "muted": palette.color(QtGui.QPalette.Mid).darker(115).name(),
            "panel": palette.color(QtGui.QPalette.Base).name(),
            "header": palette.color(QtGui.QPalette.Midlight).name(),
            "border": palette.color(QtGui.QPalette.Mid).name(),
            "accent": palette.color(QtGui.QPalette.Highlight).name(),
        }

    def _styled_dialog(self, title, extra_css=None, minimum=None, resize=None):
        """Create a popup dialog with the palette colors computed once.

        The extra stylesheet is appended to the shared color substitutions,
        so each dialog only writes the rules it actually needs.
        """
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle(title)
        if minimum:
            dialog.setMinimumSize(*minimum)
        if resize:
            dialog.resize(*resize)
        colors = self._palette_colors(dialog)
        if extra_css:
            dialog.setStyleSheet(extra_css % colors)
        return dialog, colors

    def show_shortcuts(self):
        """Show a popup window listing every keyboard shortcut."""
        groups = [
            ("shortcut_group_general", [
                ("Ctrl+N", "new_script"),
                ("Ctrl+S", "save"),
                ("Ctrl+F", "toggle_favorite"),
                ("Ctrl+K", "filter_placeholder"),
                ("Ctrl+T", "todo"),
                ("Ctrl+Shift+T", "editor_theme"),
                ("Ctrl+L", "language"),
                ("Ctrl+F1", "about"),
                ("Ctrl+Shift+F", "search"),
                ("F1", "help"),
            ]),
            ("shortcut_group_scripts", [
                ("Ctrl+D", "duplicate"),
                ("Ctrl+H", "history"),
                ("F2", "edit"),
                ("Delete", "delete"),
                ("Ctrl+R", "run"),
                ("Ctrl+E", "editor_export"),
                ("Ctrl+P", "preview"),
                ("Ctrl+O", "open"),
            ]),
            ("shortcut_group_editor", [
                ("Ctrl+/", "toggle_comment"),
                ("Ctrl+D", "duplicate_line"),
                ("Ctrl+Shift+D", "delete_line"),
                ("Ctrl+Shift+Up", "move_up"),
                ("Ctrl+Shift+Down", "move_down"),
                ("Ctrl+F", "editor_find"),
                ("Ctrl+H", "editor_replace_menu"),
                ("F3", "editor_find_next"),
                ("Shift+F3", "editor_find_prev"),
                ("F12", "editor_go_definition"),
            ]),
            ("shortcut_group_diff", [
                ("Alt+Down", "shortcut_next_change"),
                ("Alt+Up", "shortcut_prev_change"),
            ]),
        ]
        total_shortcuts = sum(len(items) for _k, items in groups)
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle(self.t("shortcuts_title", "Keyboard shortcuts"))
        dialog.setMinimumSize(620, 540)
        dialog.resize(720, 620)
        layout = QtWidgets.QVBoxLayout(dialog)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        header = QtWidgets.QFrame()
        header.setObjectName("shortcutsHeader")
        header_layout = QtWidgets.QHBoxLayout(header)
        header_layout.setContentsMargins(20, 16, 20, 14)
        header_layout.setSpacing(14)
        icon_label = QtWidgets.QLabel()
        icon_label.setObjectName("shortcutsIcon")
        pixmap = app_icon_pixmap(40)
        if pixmap is not None and not pixmap.isNull():
            icon_label.setPixmap(pixmap)
        else:
            icon_label.setText("⌨")
            icon_label.setStyleSheet("font-size: 30px;")
        text_column = QtWidgets.QVBoxLayout()
        text_column.setSpacing(2)
        title = QtWidgets.QLabel(self.t("shortcuts_title", "Keyboard shortcuts"))
        title.setObjectName("shortcutsTitle")
        subtitle = QtWidgets.QLabel(
            self.t("shortcuts_subtitle", "Press a key anywhere in the program"))
        subtitle.setObjectName("shortcutsSubtitle")
        text_column.addWidget(title)
        text_column.addWidget(subtitle)
        header_layout.addWidget(icon_label)
        header_layout.addLayout(text_column)
        header_layout.addStretch()
        badge = QtWidgets.QLabel(str(total_shortcuts))
        badge.setObjectName("shortcutsBadge")
        badge.setAlignment(QtCore.Qt.AlignCenter)
        badge.setToolTip(self.t("shortcuts_count", "{count} shortcut(s)").format(
            count=total_shortcuts) if total_shortcuts else "")
        badge.setMinimumWidth(46)
        badge.setMinimumHeight(46)
        header_layout.addWidget(badge, 0, QtCore.Qt.AlignTop)
        layout.addWidget(header)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        content = QtWidgets.QWidget()
        content_layout = QtWidgets.QVBoxLayout(content)
        content_layout.setContentsMargins(10, 12, 10, 12)
        content_layout.setSpacing(14)
        for group_key, items in groups:
            box = QtWidgets.QGroupBox(self.t(group_key, group_key))
            box.setObjectName("shortcutsGroup")
            box_layout = QtWidgets.QGridLayout(box)
            box_layout.setVerticalSpacing(6)
            box_layout.setHorizontalSpacing(12)
            # Two side-by-side columns of (key, action) pairs per group.
            half = (len(items) + 1) // 2
            columns = [items[:half], items[half:]]
            for column_index, column_items in enumerate(columns):
                for row, (keys, action_key) in enumerate(column_items):
                    base_row = row * 2
                    key_label = QtWidgets.QLabel(keys)
                    key_label.setObjectName("shortcutKey")
                    key_label.setProperty("shortcutRow", row % 2)
                    action_label = QtWidgets.QLabel(self.t(action_key, action_key))
                    action_label.setObjectName("shortcutAction")
                    action_label.setProperty("shortcutRow", row % 2)
                    box_layout.addWidget(key_label, base_row, column_index * 2)
                    box_layout.addWidget(action_label, base_row, column_index * 2 + 1)
            box_layout.setColumnStretch(0, 0)
            box_layout.setColumnStretch(1, 1)
            box_layout.setColumnStretch(2, 0)
            box_layout.setColumnStretch(3, 1)
            content_layout.addWidget(box)
        content_layout.addStretch()
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)

        close_btn = QtWidgets.QPushButton(self.t("close", "Close"))
        close_btn.setObjectName("shortcutsClose")
        close_btn.setMinimumWidth(110)
        close_btn.setMinimumHeight(32)
        close_btn.clicked.connect(dialog.close)
        footer = QtWidgets.QHBoxLayout()
        footer.addStretch()
        footer.addWidget(close_btn)
        layout.addLayout(footer)

        palette_colors = self._palette_colors(dialog)
        text_color = palette_colors["text"]
        muted_color = palette_colors["muted"]
        panel_color = palette_colors["panel"]
        header_color = palette_colors["header"]
        border_color = palette_colors["border"]
        accent_color = palette_colors["accent"]
        # The key-cap labels sit on a light background, so the accent
        # (often a pale highlight blue) is unreadable there; darken the
        # window text instead so the shortcut text is clearly visible.
        key_text_color = dialog.palette().color(
            QtGui.QPalette.WindowText).darker(200).name()
        dialog.setStyleSheet("""
            QFrame#shortcutsHeader {
                background: %(header)s;
                border: 1px solid %(border)s;
                border-radius: 10px;
            }
            QLabel#shortcutsTitle {
                color: %(text)s;
                font-size: 23px;
                font-weight: bold;
            }
            QLabel#shortcutsSubtitle {
                color: %(muted)s;
                font-size: 13px;
            }
            QLabel#shortcutsBadge {
                color: %(badge_text)s;
                background: %(accent)s;
                border-radius: 23px;
                font-size: 17px;
                font-weight: bold;
            }
            QGroupBox#shortcutsGroup {
                color: %(text)s;
                font-size: 14px;
                font-weight: bold;
                border: 1px solid %(border)s;
                border-radius: 8px;
                margin-top: 12px;
                padding: 8px 10px 8px 10px;
            }
            QGroupBox#shortcutsGroup::title {
                subcontrol-origin: margin;
                left: 12px;
                padding: 0 6px;
                background: %(panel)s;
            }
            QLabel#shortcutKey {
                color: %(key_text)s;
                background: %(key_bg)s;
                border: 1px solid %(key_border)s;
                border-radius: 5px;
                padding: 4px 12px;
                font-family: Consolas, monospace;
                font-size: 13px;
                font-weight: bold;
            }
            QLabel#shortcutKey[shortcutRow="1"] { background: %(key_bg_alt)s; }
            QLabel#shortcutAction {
                color: %(text)s;
                padding: 3px 4px;
                font-size: 13px;
                font-weight: 600;
                font-family: "Segoe UI Black", "Segoe UI", Arial, sans-serif;
                letter-spacing: 0.3px;
            }
            QScrollArea { background: %(panel)s; border: none; }
            QPushButton#shortcutsClose {
                padding: 6px 18px;
                font-size: 13px;
                border-radius: 6px;
                border: 1px solid %(border)s;
                background: %(header)s;
                color: %(text)s;
                font-weight: bold;
            }
            QPushButton#shortcutsClose:hover {
                border-color: %(accent)s;
                color: %(accent)s;
            }
        """ % {"text": text_color, "muted": muted_color,
               "panel": panel_color, "header": header_color,
               "border": border_color, "accent": accent_color,
               "badge_text": panel_color,
               "key_text": key_text_color,
               "key_bg": panel_color, "key_bg_alt": header_color,
               "key_border": border_color})
        dialog.exec_()

    def show_statistics(self):
        """Summarise the database: totals, categories, tags and favorites."""
        per_category = {}
        per_tag = {}
        for script in self.scripts_only():
            per_category[script.get("category", "?")] = per_category.get(script.get("category", "?"), 0) + 1
            for tag in [t.strip() for t in script.get("tags", "").split(",") if t.strip()]:
                per_tag[tag] = per_tag.get(tag, 0) + 1
        total_scripts = len(self.scripts_only())
        favorite_count = sum(1 for s in self.scripts if s.get("favorite"))
        total_lines = sum(len(s.get('code', '').splitlines()) for s in self.scripts)
        history_count = sum(1 for s in self.scripts if s.get("history"))
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle(self.t("stats_title"))
        dialog.setMinimumSize(680, 520)
        dialog.resize(760, 600)
        layout = QtWidgets.QVBoxLayout(dialog)

        header = QtWidgets.QFrame()
        header.setObjectName("statsHeader")
        header_layout = QtWidgets.QVBoxLayout(header)
        header_layout.setContentsMargins(20, 16, 20, 16)
        title = QtWidgets.QLabel(self.t("stats_title", "Database statistics"))
        title.setObjectName("statsTitle")
        subtitle = QtWidgets.QLabel(
            "A quick overview of your local script collection")
        subtitle.setObjectName("statsSubtitle")
        header_layout.addWidget(title)
        header_layout.addWidget(subtitle)
        layout.addWidget(header)

        metrics = QtWidgets.QHBoxLayout()
        metrics.setSpacing(10)
        for value, label in (
                (total_scripts, "Scripts"),
                (total_lines, "Code lines"),
                (favorite_count, "Favorites")):
            card = QtWidgets.QFrame()
            card.setObjectName("statsCard")
            card_layout = QtWidgets.QVBoxLayout(card)
            card_layout.setContentsMargins(14, 11, 14, 11)
            number = QtWidgets.QLabel(f"{value:,}")
            number.setObjectName("statsNumber")
            caption = QtWidgets.QLabel(label)
            caption.setObjectName("statsCaption")
            card_layout.addWidget(number)
            card_layout.addWidget(caption)
            metrics.addWidget(card, 1)
        layout.addLayout(metrics)

        content = QtWidgets.QHBoxLayout()
        content.setSpacing(12)

        category_box = QtWidgets.QGroupBox("By category")
        category_layout = QtWidgets.QVBoxLayout(category_box)
        category_layout.setSpacing(8)
        max_category = max(per_category.values(), default=1)
        for category, count in sorted(per_category.items(),
                                      key=lambda item: (-item[1], item[0].lower())):
            row = QtWidgets.QHBoxLayout()
            name = QtWidgets.QLabel(category)
            name.setMinimumWidth(92)
            bar = QtWidgets.QProgressBar()
            bar.setRange(0, max_category)
            bar.setValue(count)
            bar.setFormat(f"{count}  ({count / max(total_scripts, 1):.0%})")
            bar.setTextVisible(True)
            row.addWidget(name)
            row.addWidget(bar, 1)
            category_layout.addLayout(row)
        if not per_category:
            category_layout.addWidget(QtWidgets.QLabel("No categories yet."))
        category_layout.addStretch()
        content.addWidget(category_box, 3)

        details_box = QtWidgets.QGroupBox("Collection details")
        details_layout = QtWidgets.QVBoxLayout(details_box)
        details_layout.addWidget(QtWidgets.QLabel(f"With history: {history_count}"))
        details_layout.addWidget(QtWidgets.QLabel(f"Unique tags: {len(per_tag)}"))
        details_layout.addSpacing(8)
        details_layout.addWidget(QtWidgets.QLabel("Top tags"))
        tag_view = QtWidgets.QListWidget()
        tag_view.setMinimumWidth(190)
        for tag, count in sorted(per_tag.items(), key=lambda item: (-item[1], item[0].lower()))[:10]:
            tag_view.addItem(f"{tag}  ·  {count}")
        if not per_tag:
            tag_view.addItem("No tags yet.")
        details_layout.addWidget(tag_view, 1)
        content.addWidget(details_box, 2)
        layout.addLayout(content, 1)

        close_btn = QtWidgets.QPushButton("Close")
        close_btn.clicked.connect(dialog.close)
        footer = QtWidgets.QHBoxLayout()
        footer.addStretch()
        footer.addWidget(close_btn)
        layout.addLayout(footer)

        palette_colors = self._palette_colors(dialog)
        text_color = palette_colors["text"]
        muted_color = palette_colors["muted"]
        panel_color = palette_colors["panel"]
        header_color = palette_colors["header"]
        border_color = palette_colors["border"]
        accent_color = palette_colors["accent"]
        dialog.setStyleSheet("""
            QFrame#statsHeader {
                background: %(header)s;
                border: 1px solid %(border)s;
                border-radius: 8px;
            }
            QLabel#statsTitle {
                color: %(text)s;
                font-size: 22px;
                font-weight: bold;
            }
            QLabel#statsSubtitle, QLabel#statsCaption { color: %(muted)s; }
            QFrame#statsCard {
                background: %(panel)s;
                border: 1px solid %(border)s;
                border-radius: 6px;
            }
            QLabel#statsNumber { color: %(text)s; font-size: 20px; font-weight: bold; }
            QGroupBox { color: %(text)s; }
            QListWidget { color: %(text)s; background: %(panel)s; }
            QProgressBar {
                color: %(text)s;
                min-height: 20px;
                text-align: right;
            }
            QProgressBar::chunk { background: %(accent)s; }
        """ % {"text": text_color, "muted": muted_color,
               "panel": panel_color, "header": header_color,
               "border": border_color, "accent": accent_color})
        dialog.exec_()

    def show_templates(self):
        self.clear_form()
        self._open_script_editor_dialog()
        TemplateDialog(self, self.script_form).exec_()
    def _import_entries(self, entries, describe_failure=None):
        """Shared import path: dedupe, append, save and report.

        `entries` yields either a script dict, or (dict, extra_note) where the
        extra note is appended to the entry's description. Deduplication uses
        both the id and the (name, code) fingerprint, so a re-import of the
        same JSON or files never creates duplicates.

        Returns (added, skipped, failed).
        """
        existing = {s.get("id") for s in self.scripts}
        # Fingerprints (name + code) catch re-imported entries whose id was
        # regenerated on import, so the same source never duplicates.
        fingerprints = {(s.get("name"), s.get("code"))
                        for s in self.scripts if not is_separator(s)}
        added = 0
        skipped = 0
        failed = []
        for item in entries:
            if isinstance(item, tuple):
                entry, note = item
            else:
                entry, note = item, None
            if entry is None:
                continue
            if (entry.get("name"), entry.get("code")) in fingerprints:
                skipped += 1
                continue
            if entry.get("id") in existing:
                if note is None:
                    skipped += 1
                    continue
                # Timestamp ids can collide between files imported in the same
                # run; nudge them forward until the id is unique.
                while entry["id"] in existing:
                    entry["id"] = str(float(entry["id"]) + 1e-6)
            if note:
                entry["desc"] = (entry.get("desc", "") + "\n" + note).strip()
            existing.add(entry.get("id"))
            fingerprints.add((entry.get("name"), entry.get("code")))
            self.scripts.append(entry)
            added += 1
        if added:
            save_data(DATA_FILE, self.scripts)
            self.refresh_list()
        if failed and describe_failure is not None:
            failed.extend(describe_failure())
        return added, skipped, failed

    @staticmethod
    def _report_import_result(parent, added, skipped, failed):
        """Show the shared import summary / error dialogs."""
        if failed:
            QtWidgets.QMessageBox.warning(
                parent, parent.t("warning"),
                f"{parent.t('import_error')}\n" + "\n".join(failed),
            )
        QtWidgets.QMessageBox.information(
            parent, parent.t("info"),
            f"{parent.t('imported')} ({added})" +
            (f"\n{parent.t('skipped_duplicates', 'Skipped duplicates')}: {skipped}"
             if skipped else ""))

    def import_json(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Import JSON", "", "JSON files (*.json)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if not isinstance(data, list):
                QtWidgets.QMessageBox.warning(self, self.t("warning"), "JSON must be a list of script entries")
                return
            def entries():
                for entry in data:
                    if isinstance(entry, dict):
                        yield normalize_entry(entry)
            added, skipped, _failed = self._import_entries(entries())
            self._report_import_result(self, added, skipped, [])
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, self.t("import_error"), str(exc))

    def export_json(self):
        """Export the whole database or only the scripts currently visible."""
        answer = QtWidgets.QMessageBox.question(
            self, self.t("confirm"),
            f"{self.t('export_json')}: {self.t('all_scripts')} (Yes) / {self.t('favorites_only')} -> visible (No)?",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No | QtWidgets.QMessageBox.Cancel,
        )
        if answer == QtWidgets.QMessageBox.Cancel:
            return
        payload = self.scripts if answer == QtWidgets.QMessageBox.Yes else self.visible_scripts()
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, self.t("export_json"), "scripts_export.json", "JSON files (*.json)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            QtWidgets.QMessageBox.information(self, self.t("info"), f"{self.t('exported')} ({len(payload)})")
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, self.t("export_error"), str(exc))
    def import_script_file(self):
        """Import one or more plain source files in their natural form."""
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self, self.t("import_file"), "", supported_extensions_filter()
        )
        if not paths:
            return
        failed = []
        def entries():
            for path in paths:
                try:
                    yield build_script_from_file(path)
                except OSError as exc:
                    failed.append(f"{os.path.basename(path)}: {exc}")
        added, skipped, _failed = self._import_entries(entries())
        self._report_import_result(self, added, skipped, failed)

    def import_script_folder(self):
        """Import recognized source files from a folder and its subfolders."""
        folder = QtWidgets.QFileDialog.getExistingDirectory(
            self, self.t("import_menu_folder", "Whole folder..."))
        if not folder:
            return
        supported = set(EXTENSION_ALIASES) | {".txt"}
        paths = []
        for root, _directories, filenames in os.walk(folder):
            for filename in filenames:
                if os.path.splitext(filename)[1].lower() in supported:
                    paths.append(os.path.join(root, filename))
        paths.sort(key=lambda path: os.path.relpath(path, folder).lower())
        if not paths:
            QtWidgets.QMessageBox.information(
                self, self.t("info"), self.t("import_folder_empty",
                                               "No supported source files found."))
            return
        failed = []
        def entries():
            for path in paths:
                try:
                    entry = build_script_from_file(path)
                except OSError as exc:
                    failed.append(f"{os.path.relpath(path, folder)}: {exc}")
                    continue
                if entry is not None:
                    yield entry, "Imported from: " + os.path.relpath(path, folder)
        added, skipped, _failed = self._import_entries(entries())
        self._report_import_result(self, added, skipped, failed)

    def export_script_file(self):
        """Export the selected script(s) as plain files keeping the extension."""
        scripts = self.selected_scripts()
        if not scripts:
            self.show_message(self.t("info"), self.t("select_script"))
            return
        if len(scripts) == 1:
            script = scripts[0]
            ext = extension_for_category(script.get("category", ""))
            suggested = safe_file_name(script.get("name", ""), "script") + ext
            path, _ = QtWidgets.QFileDialog.getSaveFileName(
                self, self.t("export_file"), suggested, f"*{ext};;{self.t('all_source_files')}"
            )
            if not path:
                return
            targets = [(path, script)]
        else:
            directory = QtWidgets.QFileDialog.getExistingDirectory(self, self.t("export_file"))
            if not directory:
                return
            targets = [(script_export_path(script, directory), script) for script in scripts]
        try:
            for path, script in targets:
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(script.get("code", ""))
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, self.t("export_error"), str(exc))
            return
        if len(targets) == 1:
            self.show_message(self.t("info"), f"{self.t('exported_file')}\n{targets[0][0]}")
        else:
            self.show_message(self.t("info"), f"{self.t('exported')} ({len(targets)})")


# ===========================================================================
# Diff engine - algorithms (Myers / Patience / Histogram), normalization,
# token/byte/AST comparison and directory walking.
# ===========================================================================

DIFF_VIEWS = ("side", "unified", "inline", "blocks", "ast", "analysis",
              "files", "merge3", "merge4", "live", "sysrest", "env", "hex")
DIFF_ALGORITHMS = ("myers", "patience", "histogram")
DIFF_MODES = ("text", "token", "bytes")
# The "special" modes reachable from the Modes... menu (view key -> label key).
DIFF_SPECIAL_MODES = (
    ("merge3", "diff_mode_merge3"),
    ("merge4", "diff_mode_merge4"),
    ("live", "diff_mode_live"),
    ("sysrest", "diff_mode_sysrest"),
    ("env", "diff_mode_env"),
    ("hex", "diff_mode_hex"),
)

COMMENT_MARK_SETS = {
    "Python": ("#",), "Bash": ("#",), "PowerShell": ("#",),
    "VBScript": ("'",), "JavaScript": ("//",), "Java": ("//",),
    "C++": ("//",), "Batch": ("rem",), "SQL": ("--",),
    "HTML": ("<!--", "//"), "HTA": ("//", "<!--"),
}
# A neutral superset of comment markers, used when the language is unknown.
COMMENT_MARKS = ("#", "//", "--", ";", "'", "rem", "/*", "*", "<!--")


def _comment_key(line, language):
    # Lower-cased comparison key used to decide whether a line is a comment.
    stripped = (line or "").lstrip()
    if not stripped:
        return None
    markers = COMMENT_MARK_SETS.get(language or "", ())
    lowered = stripped.lower()
    for mark in markers:
        if mark in ("//", "/*", "<!--"):
            if lowered.startswith(mark):
                return mark
        elif lowered.startswith(mark):
            return mark
    return None


def is_comment_line(line, language=""):
    """True when a line is (mostly) a comment in the given language."""
    marker = _comment_key(line, language)
    if marker:
        return True
    stripped = (line or "").strip().lower()
    return any(stripped.startswith(m) for m in COMMENT_MARKS)


def prepare_diff_lines(text, ignore_case=False, ignore_ws=False,
                       ignore_blank=False, ignore_comments=False, language=""):
    """Normalise a code body into the list of lines the differ will compare."""
    lines = (text or "").splitlines()
    if ignore_comments:
        lines = [line for line in lines if not is_comment_line(line, language)]
    if ignore_blank:
        lines = [line for line in lines if line.strip()]
    if ignore_ws:
        lines = [re.sub(r"\s+", " ", line).strip() for line in lines]
    if ignore_case:
        lines = [line.lower() for line in lines]
    return lines


def _myers_ops(left, right):
    """Line-level opcodes using difflib's Myers-like matcher."""
    matcher = difflib.SequenceMatcher(a=left, b=right, autojunk=False)
    return matcher.get_opcodes()


def _patience_ops(left, right):
    """Patience-style diff: anchor on unique common lines, recurse on gaps.

    A recursion-depth guard and an explicit progress check stop the walk from
    looping forever when a gap has no usable anchors (which would otherwise
    recurse on the very same range and overflow the stack).
    """
    def anchors(lo1, hi1, lo2, hi2):
        count_left = collections.Counter(left[lo1:hi1])
        count_right = collections.Counter(right[lo2:hi2])
        found = []
        for line in dict.fromkeys(left[lo1:hi1]):
            if count_left[line] == 1 and count_right.get(line) == 1:
                found.append((line, right.index(line, lo2, hi2)))
        found.sort(key=lambda kv: kv[1])
        return found

    def longest_increasing(values):
        # Indices forming a longest strictly increasing subsequence of `values`.
        tails = []
        prev = [-1] * len(values)
        for index, value in enumerate(values):
            lo, hi = 0, len(tails)
            while lo < hi:
                mid = (lo + hi) // 2
                if values[tails[mid]] < value:
                    lo = mid + 1
                else:
                    hi = mid
            prev[index] = tails[lo - 1] if lo > 0 else -1
            if lo == len(tails):
                tails.append(index)
            else:
                tails[lo] = index
        result = []
        node = tails[-1] if tails else -1
        while node >= 0:
            result.append(node)
            node = prev[node]
        result.reverse()
        return result

    ops = []

    def walk(lo1, hi1, lo2, hi2, depth=0):
        if lo1 >= hi1 and lo2 >= hi2:
            return
        common = anchors(lo1, hi1, lo2, hi2)
        if not common or depth > 40 or lo1 >= hi1 or lo2 >= hi2:
            ops.append(("replace", lo1, hi1, lo2, hi2))
            return
        keep = [common[k] for k in longest_increasing([p for _l, p in common])]
        if not keep:
            ops.append(("replace", lo1, hi1, lo2, hi2))
            return
        prev1, prev2 = lo1, lo2
        for line, _position in keep:
            j = right.index(line, prev2, hi2)
            i = left.index(line, prev1, hi1)
            walk(prev1, i, prev2, j, depth + 1)
            ops.append(("equal", i, i + 1, j, j + 1))
            prev1, prev2 = i + 1, j + 1
        walk(prev1, hi1, prev2, hi2, depth + 1)

    walk(0, len(left), 0, len(right))
    merged = []
    for op in ops:
        if (merged and op[0] == "equal" and merged[-1][0] == "equal"
                and merged[-1][2] == op[1] and merged[-1][4] == op[3]):
            previous = merged[-1]
            merged[-1] = ("equal", previous[1], op[2], previous[3], op[4])
        else:
            merged.append(op)
    return merged


def _histogram_ops(left, right):
    """Histogram-style diff: difflib with autojunk, a good histogram stand-in."""
    matcher = difflib.SequenceMatcher(a=left, b=right, autojunk=False)
    return matcher.get_opcodes()


DIFF_ALGORITHM_HANDLERS = {
    "myers": _myers_ops,
    "patience": _patience_ops,
    "histogram": _histogram_ops,
}


def word_diff_segments(left_text, right_text):
    """Split two lines into tokens and mark which pieces differ.

    Returns (left_segments, right_segments) where each segment is a
    (text, changed) pair, so the inline view can highlight the exact changes.
    """
    token_re = re.compile(r"\w+|\s+|[^\w\s]")
    left_tokens = [m.group(0) for m in token_re.finditer(left_text)]
    right_tokens = [m.group(0) for m in token_re.finditer(right_text)]
    matcher = difflib.SequenceMatcher(a=left_tokens, b=right_tokens, autojunk=False)
    left_segments = []
    right_segments = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        left_piece = "".join(left_tokens[i1:i2])
        right_piece = "".join(right_tokens[j1:j2])
        if tag == "equal":
            left_segments.append((left_piece, False))
            right_segments.append((right_piece, False))
        else:
            if left_piece:
                left_segments.append((left_piece, True))
            if right_piece:
                right_segments.append((right_piece, True))
    return left_segments, right_segments


def align_lines(left, right, algorithm="myers"):
    """Return diff rows describing how two line lists relate.

    Rows carry tag ('equal'/'replace'/'delete'/'insert'), line numbers on both
    sides and word-level segments for 'replace' rows so the views can highlight
    the exact changes inside a line.
    """
    handler = DIFF_ALGORITHM_HANDLERS.get(algorithm, _myers_ops)
    rows = []
    for tag, i1, i2, j1, j2 in handler(left, right):
        if tag == "equal":
            for offset in range(i2 - i1):
                rows.append({
                    "tag": "equal",
                    "left_no": i1 + offset + 1, "left": left[i1 + offset],
                    "right_no": j1 + offset + 1, "right": right[j1 + offset],
                    "left_segments": None, "right_segments": None,
                })
        elif tag == "replace":
            overlap = min(i2 - i1, j2 - j1)
            for offset in range(overlap):
                lseg, rseg = word_diff_segments(left[i1 + offset], right[j1 + offset])
                rows.append({
                    "tag": "replace",
                    "left_no": i1 + offset + 1, "left": left[i1 + offset],
                    "right_no": j1 + offset + 1, "right": right[j1 + offset],
                    "left_segments": lseg, "right_segments": rseg,
                })
            for li in range(i1 + overlap, i2):
                rows.append({
                    "tag": "delete", "left_no": li + 1, "left": left[li],
                    "right_no": None, "right": "",
                    "left_segments": None, "right_segments": None,
                })
            for rj in range(j1 + overlap, j2):
                rows.append({
                    "tag": "insert", "left_no": None, "left": "",
                    "right_no": rj + 1, "right": right[rj],
                    "left_segments": None, "right_segments": None,
                })
        elif tag == "delete":
            for li in range(i1, i2):
                rows.append({
                    "tag": "delete", "left_no": li + 1, "left": left[li],
                    "right_no": None, "right": "",
                    "left_segments": None, "right_segments": None,
                })
        elif tag == "insert":
            for rj in range(j1, j2):
                rows.append({
                    "tag": "insert", "left_no": None, "left": "",
                    "right_no": rj + 1, "right": right[rj],
                    "left_segments": None, "right_segments": None,
                })
    return rows
# A token of the "token" comparison mode: identifiers, numbers, strings,
# operators and whitespace, so only the meaningful pieces are diffed.
TOKEN_PATTERN = re.compile(r"[A-Za-z_]\w*|\d+(?:\.\d+)?|\s+|.", re.UNICODE)


def tokenize(text):
    """Split text into diffable tokens (word / number / space / punctuation)."""
    return TOKEN_PATTERN.findall(text or "")


def bytes_diff_lines(data, other):
    """Compare two byte blobs and return human readable difference lines."""
    if data == other:
        return []
    result = []
    limit = max(len(data), len(other))
    step = 16
    for base in range(0, limit, step):
        chunk_a = data[base:base + step]
        chunk_b = other[base:base + step]
        if chunk_a == chunk_b:
            continue
        result.append(f"@{base:08x}")
        result.append("- " + " ".join(f"{b:02x}" for b in chunk_a))
        result.append("+ " + " ".join(f"{b:02x}" for b in chunk_b))
    return result


def token_diff_lines(left, right, algorithm="myers"):
    """Diff two texts at token level; returns display rows."""
    left_tokens = tokenize(left)
    right_tokens = tokenize(right)
    rows = align_lines(left_tokens, right_tokens, algorithm)
    return rows, left_tokens, right_tokens


def ast_symbols(text, language):
    """Extract top-level symbols (functions/classes) with a light parser.

    This is not a full parser: it recognises the common definition forms of
    Python, JavaScript/Java/C++ and shell so the AST view can group the diff by
    function or class without pulling in a language grammar.
    """
    language = (language or "").lower()
    symbols = []
    if language in ("python", "py", ""):
        # `\s*` before the keyword would swallow the preceding blank lines, so
        # only horizontal whitespace is allowed; the group limit keeps the
        # reported offset on the `def`/`class` keyword itself.
        pattern = re.compile(r"^[ \t]*(def|class)\s+(\w+)\s*(\([^)]*\))?\s*:", re.MULTILINE)
        for match in pattern.finditer(text or ""):
            keyword_at = match.start(1)
            symbols.append((match.group(2), "class" if match.group(1) == "class" else "func",
                            keyword_at, _python_block_end(text, keyword_at)))
    else:
        pattern = re.compile(
            r"^\s*(?:function\s+(\w+)|(?:public|private|protected|static|final|\s)*"
            r"(?:class|interface|struct)\s+(\w+)|(\w+)\s*\([^)]*\)\s*\{)",
            re.MULTILINE)
        for match in pattern.finditer(text or ""):
            name = match.group(1) or match.group(2) or match.group(3)
            kind = "class" if match.group(2) else "func"
            symbols.append((name, kind, match.start(), _brace_block_end(text, match.start())))
    return symbols


def _python_block_end(text, start):
    # End offset of an indented Python block that begins at `start`.
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


def _brace_block_end(text, start):
    # End offset of a brace-delimited block beginning at `start`.
    depth = 0
    seen = False
    for index in range(start, len(text or "")):
        char = text[index]
        if char == "{":
            depth += 1
            seen = True
        elif char == "}":
            depth -= 1
            if seen and depth <= 0:
                return index + 1
    return len(text)
CHANGE_KINDS = ("added", "removed", "changed")


def change_kind(row):
    """Map a diff row tag onto a user facing change category."""
    tag = row.get("tag")
    if tag == "insert":
        return "added"
    if tag == "delete":
        return "removed"
    if tag == "replace":
        return "changed"
    return "equal"


def iter_change_blocks(rows):
    """Group consecutive non-equal rows into change blocks.

    Returns (start_index, rows) tuples so the sidebar, the filters and the
    progress bar all work from the same notion of "one change".
    """
    blocks = []
    current = []
    start = None
    for index, row in enumerate(rows):
        if row.get("tag") == "equal":
            if current:
                blocks.append((start, current))
                current = []
                start = None
            continue
        if start is None:
            start = index
        current.append(row)
    if current:
        blocks.append((start, current))
    return blocks


def symbols_in_range(symbols, start, end):
    """Names of the symbols overlapping the document range [start, end)."""
    return [
        name for name, _kind, sym_start, sym_end in symbols
        if sym_start < end and sym_end > start
    ]


def _structural_name_change(left_line, right_line):
    """True when a definition line changed its declared name only."""
    pattern = re.compile(
        r"\b(?:def|class|function|interface|struct)\s+([A-Za-z_]\w*)")
    left_match = pattern.search(left_line or "")
    right_match = pattern.search(right_line or "")
    if left_match and right_match:
        return left_match.group(1) != right_match.group(1)
    return False


def renamed_definitions(left_code, right_code):
    """Pairs of symbol names that were renamed between the two sides."""
    left_names = {name for name, kind, _a, _b in ast_symbols(left_code, "Python")
                  if kind == "func"}
    right_names = {name for name, kind, _a, _b in ast_symbols(right_code, "Python")
                   if kind == "func"}
    return sorted(left_names - right_names), sorted(right_names - left_names)


# ---------------------------------------------------------------------------
# Code analysis: language detection, AST-level comparison, refactor and
# duplicate detection.
# ---------------------------------------------------------------------------

LANGUAGE_SIGNATURES = [
    # (kind, [regex patterns], [file extensions])
    ("python", [r"^\s*def\s+\w+\s*\(", r"^\s*class\s+\w+", r"^\s*import\s+\w+",
                r"if\s+__name__\s*=="], [".py", ".pyw", ".pyi"]),
    ("csharp", [r"\busing\s+[\w.]+;", r"\bnamespace\s+[\w.]+", r"\bpublic\s+(?:class|void|int)\b"],
               [".cs"]),
    ("cpp", [r"#include\s*<", r"\bstd::", r"\bint\s+main\s*\("], [".cpp", ".cc", ".cxx", ".h", ".hpp", ".c"]),
    ("powershell", [r"\bWrite-Host\b", r"\bGet-ChildItem\b", r"\$env:", r"^\s*param\s*\("],
                   [".ps1", ".psm1", ".psd1"]),
    ("bash", [r"^#!.*\b(?:bash|sh)\b", r"\becho\s+", r"\bfi\b", r"\bdone\b"],
             [".sh", ".bash", ".zsh", ".ksh"]),
    ("batch", [r"^\s*@echo\s+off", r"\bsetlocal\b", r"\bgoto\s+:\w+"], [".bat", ".cmd", ".nt"]),
    ("vbscript", [r"\bOption\s+Explicit\b", r"\bSub\s+\w+", r"\bEnd\s+Sub\b", r"\bWScript\."],
                 [".vbs", ".vbe", ".wsf"]),
    ("javascript", [r"\bconsole\.log\b", r"\bfunction\s+\w+\s*\(",
                    r"\bconst\s+\w+\s*=", r"=>"], [".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"]),
    ("java", [r"\bpublic\s+static\s+void\s+main\b", r"\bSystem\.out\.println\b",
              r"\bpackage\s+[\w.]+;"], [".java"]),
]

LANGUAGE_LABELS = {
    "python": "Python", "csharp": "C#", "cpp": "C++", "java": "Java",
    "javascript": "JavaScript", "powershell": "PowerShell", "bash": "Bash",
    "batch": "Batch", "vbscript": "VBScript", "text": "Plain text",
}


def detect_language_kind(code, filename="", hint=""):
    """Identify the language of a code block (kind + human label).

    The file extension wins when it is known; otherwise the content is scored
    against the language signatures and the best match is returned.
    """
    ext = os.path.splitext(filename or "")[1].lower()
    for kind, _patterns, extensions in LANGUAGE_SIGNATURES:
        if ext and ext in extensions:
            return kind, LANGUAGE_LABELS[kind]
    text = (code or "").lower()
    if not text.strip():
        return "text", LANGUAGE_LABELS["text"]
    best_kind, best_score = "text", 0
    for kind, patterns, _extensions in LANGUAGE_SIGNATURES:
        score = sum(1 for pattern in patterns if re.search(pattern, code, re.IGNORECASE | re.MULTILINE))
        if score > best_score:
            best_kind, best_score = kind, score
    if best_score == 0 and hint:
        kind = (hint or "").lower()
        if kind in LANGUAGE_LABELS:
            return kind, LANGUAGE_LABELS[kind]
    return best_kind, LANGUAGE_LABELS[best_kind]


IMPORT_PATTERNS = {
    "python": re.compile(r"^\s*(?:from\s+([\w.]+)\s+import\s+(.+)|import\s+(\S+)([^\n]*))", re.MULTILINE),
    "javascript": re.compile(r"^\s*import\s+(?:.*?from\s+)?[\"\']([^\"\']+)[\"\']|^\s*const\s+\w+\s*=\s*require\([\"\']([^\"\']+)[\"\']\)", re.MULTILINE),
    "java": re.compile(r"^\s*import\s+([\w.*]+);", re.MULTILINE),
    "csharp": re.compile(r"^\s*using\s+([\w.]+);", re.MULTILINE),
    "cpp": re.compile(r'^\s*#include\s*[<"]([^>"]+)[>"]', re.MULTILINE),
    "powershell": re.compile(r"^\s*(?:Import-Module|using\s+module)\s+([\w.\\/-]+)", re.MULTILINE | re.IGNORECASE),
    "bash": re.compile(r"^\s*(?:source|\.)\s+(\S+)", re.MULTILINE),
}


def extract_imports(code, kind):
    """Return the set of imported module names for the given language."""
    pattern = IMPORT_PATTERNS.get(kind)
    if pattern is None:
        return set()
    names = set()
    for match in pattern.finditer(code or ""):
        if kind == "python":
            if match.group(1):                       # from X import a, b
                names.add(match.group(1).split(".")[0])
                for part in (match.group(2) or "").split(","):
                    cleaned = part.strip().split(" as ")[0].strip()
                    if cleaned:
                        names.add(cleaned)
            else:                                    # import a, b  /  import a.b
                for part in (match.group(3) + (match.group(4) or "")).split(","):
                    cleaned = part.strip().split(" as ")[0].strip()
                    if cleaned:
                        names.add(cleaned.split(".")[0])
            continue
        for group in match.groups():
            if group:
                names.add(group.strip())
    return {name for name in names if name}


DEFINITION_PATTERNS = {
    "python": re.compile(r"^(\s*)(?:def)\s+(\w+)\s*(\([^)]*\))?", re.MULTILINE),
    "javascript": re.compile(r"^(\s*)(?:function\s+(\w+)|(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?(?:function|\())", re.MULTILINE),
    "java": re.compile(r"^(\s*)(?:public|private|protected|static|final|\s)*([\w<>\[\]]+)\s+(\w+)\s*(\([^)]*\))", re.MULTILINE),
    "csharp": re.compile(r"^(\s*)(?:public|private|protected|static|internal|\s)*(?:class|void|[\w<>\[\]]+)\s+(\w+)\s*(\([^)]*\))?", re.MULTILINE),
    "cpp": re.compile(r"^(\s*)(?:[\w:<>\*&]+\s+)+(\w+)\s*(\([^)]*\))", re.MULTILINE),
    "powershell": re.compile(r"^(\s*)function\s+([\w-]+)\s*(\([^)]*\))?", re.MULTILINE | re.IGNORECASE),
    "bash": re.compile(r"^(\s*)(?:function\s+)?([\w-]+)\s*\(\s*\)", re.MULTILINE),
    "vbscript": re.compile(r"^(\s*)(?:Sub|Function)\s+(\w+)\s*(\([^)]*\))?", re.MULTILINE | re.IGNORECASE),
}


def extract_functions(code, kind):
    """Return function definitions as (name, params) for the given language."""
    pattern = DEFINITION_PATTERNS.get(kind)
    if pattern is None:
        return []
    functions = []
    for match in pattern.finditer(code or ""):
        groups = [g for g in match.groups() if g is not None]
        # Last named group is the function name, an earlier ()-group the params.
        name = None
        params = ""
        for group in reversed(groups):
            if group.startswith("("):
                params = group
                continue
            if re.match(r"^[\w-]+$", group):
                name = group
                break
        if name:
            functions.append((name, params))
    return functions


def extract_classes(code, kind):
    """Return class names found in the code (language aware)."""
    if kind == "python":
        return [m.group(1) for m in re.finditer(r"^\s*class\s+(\w+)", code or "", re.MULTILINE)]
    if kind in ("javascript",):
        return [m.group(1) for m in re.finditer(r"^\s*class\s+(\w+)", code or "", re.MULTILINE)]
    if kind in ("java", "csharp", "cpp"):
        return [m.group(1) for m in re.finditer(r"\b(?:class|interface|struct|enum)\s+(\w+)", code or "")]
    return []


def _params_of(params_text):
    inner = (params_text or "").strip().strip("()")
    if not inner:
        return []
    parts = [p.strip() for p in inner.split(",")]
    names = []
    for part in parts:
        token = part.split("=")[0].strip().split()[-1] if part.strip() else ""
        if token:
            names.append(token)
    return names


def compare_code_ast(left_code, right_code, left_name="", right_name=""):
    """Structured AST-level comparison of two code bodies.

    Returns a dict describing what changed at the level of functions, classes,
    imports and parameters, which the Analysis view renders as a report.
    """
    left_kind, _ = detect_language_kind(left_code, left_name)
    right_kind, _ = detect_language_kind(right_code, right_name)

    left_funcs = dict(extract_functions(left_code, left_kind))
    right_funcs = dict(extract_functions(right_code, right_kind))
    left_classes = set(extract_classes(left_code, left_kind))
    right_classes = set(extract_classes(right_code, right_kind))
    left_imports = extract_imports(left_code, left_kind)
    right_imports = extract_imports(right_code, right_kind)

    functions_added = sorted(set(right_funcs) - set(left_funcs))
    functions_removed = sorted(set(left_funcs) - set(right_funcs))
    functions_common = sorted(set(left_funcs) & set(right_funcs))
    functions_changed = sorted(
        name for name in functions_common
        if _params_of(left_funcs[name]) != _params_of(right_funcs[name])
    )

    parameter_deltas = []
    for name in functions_common:
        before = _params_of(left_funcs[name])
        after = _params_of(right_funcs[name])
        if before != after:
            parameter_deltas.append((name, before, after))

    return {
        "left_kind": left_kind,
        "right_kind": right_kind,
        "imports_added": sorted(right_imports - left_imports),
        "imports_removed": sorted(left_imports - right_imports),
        "classes_added": sorted(right_classes - left_classes),
        "classes_removed": sorted(left_classes - right_classes),
        "functions_added": functions_added,
        "functions_removed": functions_removed,
        "functions_changed": functions_changed,
        "parameter_deltas": parameter_deltas,
        "renames": renamed_definitions(left_code, right_code),
    }


def detect_refactorings(left_code, right_code, left_name="", right_name=""):
    """Detect rename / move / extract refactorings between two code bodies.

    - rename: same body, different name.
    - move:   the same block appears at a different line on the other side.
    - extract: a block that exists inline on one side appears as a new
      function on the other.
    """
    events = []
    left_kind, _ = detect_language_kind(left_code, left_name)
    right_kind, _ = detect_language_kind(right_code, right_name)
    left_funcs = dict(extract_functions(left_code, left_kind))
    right_funcs = dict(extract_functions(right_code, right_kind))
    left_lines = (left_code or "").splitlines()
    right_lines = (right_code or "").splitlines()

    # --- rename: identical bodies under different names -------------------
    left_bodies = {name: _function_body_text(left_code, left_kind, name) for name in left_funcs}
    right_bodies = {name: _function_body_text(right_code, right_kind, name) for name in right_funcs}
    for old_name, body in left_bodies.items():
        if not body.strip():
            continue
        for new_name, new_body in right_bodies.items():
            if new_name == old_name:
                continue
            if body == new_body:
                events.append({"kind": "rename", "detail": f"{old_name} -> {new_name}"})
                break

    # --- move: an unchanged line block relocated --------------------------
    common = [line for line in left_lines if line.strip() and line in right_lines]
    moved = 0
    for line in common:
        if left_lines.index(line) != right_lines.index(line):
            moved += 1
    if moved >= 3:
        events.append({"kind": "move",
                       "detail": f"{moved} unchanged line(s) at a different position"})

    # --- extract: a new function whose body already existed inline --------
    for new_name in sorted(set(right_funcs) - set(left_funcs)):
        body = right_bodies.get(new_name, "")
        if body and body in (left_code or ""):
            events.append({"kind": "extract",
                           "detail": f"inline block extracted into {new_name}()"})
    return events


def _function_body_text(code, kind, name):
    """Indented (or braced) body of a named function, without its signature."""
    for match in re.finditer(
            r"(?:def|function|Sub|Function)\s+" + re.escape(name) + r"\b", code or "", re.IGNORECASE):
        end = _python_block_end(code, match.start())
        block = code[match.start():end]
        lines = block.splitlines()
        return "\n".join(line.strip() for line in lines[1:] if line.strip())
    return ""


def detect_duplicate_blocks(left_code, right_code, min_lines=4):
    """Find blocks that appear more than once across both sides.

    Returns (block_text, [locations]) for every block of at least `min_lines`
    consecutive non-blank lines that shows up in more than one place.
    """
    def blocks_of(lines, side):
        # A sliding window over every run of `min_lines` non-blank lines, so a
        # duplicated snippet is found wherever it sits, not only between blanks.
        found = []
        stripped = [("" if not line.strip() else line.strip()) for line in lines]
        for start in range(len(stripped) - min_lines + 1):
            window = stripped[start:start + min_lines]
            if "" in window:
                continue
            found.append(("\n".join(window), (side, start + 1)))
        return found
    seen = {}
    for block, location in blocks_of((left_code or "").splitlines(), "left"):
        seen.setdefault(block, []).append(location)
    for block, location in blocks_of((right_code or "").splitlines(), "right"):
        seen.setdefault(block, []).append(location)
    duplicates = []
    for block, locations in seen.items():
        if len(locations) > 1:
            duplicates.append((block, locations))
    duplicates.sort(key=lambda item: len(item[0].splitlines()), reverse=True)
    # Keep only the widest block for each location set, so the report does not
    # list every shorter sub-window of the same repeated run.
    longest = []
    covered = set()
    for block, locations in duplicates:
        key = frozenset(locations)
        if key in covered:
            continue
        covered.add(key)
        longest.append((block, locations))
    return longest


def search_in_rows(rows, needle, ignore_case=True):
    """Return the row indices whose text contains `needle`."""
    if not needle:
        return []
    query = needle.lower() if ignore_case else needle
    hits = []
    for index, row in enumerate(rows):
        for text in (row.get("left"), row.get("right")):
            haystack = (text or "").lower() if ignore_case else (text or "")
            if haystack and query in haystack:
                hits.append(index)
                break
    return hits


def directory_tree(root):
    """Return a sorted relative-path -> absolute-path map for a directory."""
    mapping = {}
    for base, _dirs, files in os.walk(root):
        for name in files:
            full = os.path.join(base, name)
            mapping[os.path.relpath(full, root).replace("\\", "/")] = full
    return collections.OrderedDict(sorted(mapping.items()))


def compare_directories(left_root, right_root):
    """Compare two directory trees; returns per-file status rows."""
    left = directory_tree(left_root)
    right = directory_tree(right_root)
    rows = []
    for path in sorted(set(left) | set(right)):
        in_left = path in left
        in_right = path in right
        if in_left and not in_right:
            status = "only-left"
        elif in_right and not in_left:
            status = "only-right"
        else:
            try:
                with open(left[path], "rb") as handle:
                    a = handle.read()
                with open(right[path], "rb") as handle:
                    b = handle.read()
                status = "same" if a == b else "different"
            except OSError:
                status = "error"
        # Every path produces exactly one row, whatever its status, and the
        # loop must run to the end: returning inside it made the Folders view
        # show at most one file.
        rows.append({
            "path": path,
            "status": status,
            "left": left.get(path, ""),
            "right": right.get(path, ""),
        })
    return rows
# ---------------------------------------------------------------------------
# Special diff modes: three/four-way merge, live, system configuration,
# environment comparison and a binary hex view.
# ---------------------------------------------------------------------------


def merge_three_way(base, ours, theirs):
    """Line-level three-way merge of `ours` and `theirs` against `base`.

    Returns a list of result rows, one per output line, each carrying:
      - ``tag``      : "ours", "theirs", "both" or "conflict" (both sides
                       changed the same region).
      - ``text``     : the resulting text for non-conflict rows.
      - ``ours`` / ``theirs`` : the competing versions for a conflict row.
    """
    base = (base or "").splitlines()
    ours = (ours or "").splitlines()
    theirs = (theirs or "").splitlines()
    rows = []

    ours_ops = difflib.SequenceMatcher(a=base, b=ours, autojunk=False).get_opcodes()
    theirs_ops = difflib.SequenceMatcher(a=base, b=theirs, autojunk=False).get_opcodes()

    ours_regions = [(i1, i2, ours[j1:j2])
                    for op, i1, i2, j1, j2 in ours_ops if op != "equal"]
    theirs_regions = [(i1, i2, theirs[j1:j2])
                      for op, i1, i2, j1, j2 in theirs_ops if op != "equal"]

    def next_event(index_a, index_b):
        # Which side's region starts first (or overlaps) from the cursor on.
        a_start = ours_regions[index_a][0] if index_a < len(ours_regions) else None
        b_start = theirs_regions[index_b][0] if index_b < len(theirs_regions) else None
        if a_start is None and b_start is None:
            return None
        if b_start is None or (a_start is not None and a_start <= b_start):
            return "ours"
        return "theirs"

    ai = bi = 0
    while ai < len(ours_regions) or bi < len(theirs_regions):
        side = next_event(ai, bi)
        if side == "ours":
            a_start, a_end, a_lines = ours_regions[ai]
            # Overlapping theirs region -> potential conflict.
            if bi < len(theirs_regions) and theirs_regions[bi][0] < a_end:
                b_start, b_end, b_lines = theirs_regions[bi]
                if a_lines == b_lines:
                    rows.append({"tag": "both", "text": "\n".join(a_lines),
                                 "ours": a_lines, "theirs": b_lines})
                else:
                    rows.append({"tag": "conflict", "text": "",
                                 "ours": a_lines, "theirs": b_lines,
                                 "ours_range": (a_start, a_end),
                                 "theirs_range": (b_start, b_end)})
                ai += 1
                bi += 1
                continue
            rows.append({"tag": "ours", "text": "\n".join(a_lines),
                         "ours": a_lines, "theirs": a_lines})
            ai += 1
        else:
            b_start, b_end, b_lines = theirs_regions[bi]
            rows.append({"tag": "theirs", "text": "\n".join(b_lines),
                         "ours": b_lines, "theirs": b_lines})
            bi += 1
    return rows


def merge_four_way(base, ours, theirs, extra):
    """Four-way merge: base vs ours vs theirs vs an extra (fourth) side.

    Returns (rows, conflicts). `rows` is the three-way merge of ours/theirs
    against base, then any line where the extra side also diverged is flagged
    as an additional conflict against the merged result.
    """
    rows = merge_three_way(base, ours, theirs)
    extra_lines = (extra or "").splitlines()
    merged_text = "\n".join(row.get("text", "") for row in rows)
    conflicts = [i for i, row in enumerate(rows) if row["tag"] == "conflict"]
    # A fourth side conflicts when it differs from the merged result too; it is
    # reported as an extra conflict row so the reviewer can decide.
    if extra_lines:
        merged_lines = merged_text.splitlines()
        for op, i1, i2, j1, j2 in difflib.SequenceMatcher(
                a=merged_lines, b=extra_lines, autojunk=False).get_opcodes():
            if op == "equal":
                continue
            conflicts.append(len(rows))
            rows.append({
                "tag": "conflict",
                "text": "",
                "ours": merged_lines[i1:i2],
                "theirs": extra_lines[j1:j2],
                "fourth": True,
            })
    return rows, conflicts


def environment_snapshot():
    """Return the current process environment as a sorted list of K=V lines."""
    return [f"{key}={value}" for key, value in sorted(os.environ.items(),
                                                      key=lambda kv: kv[0].lower())]


def system_configuration():
    """Collect a comparable snapshot of the system configuration as text lines.

    Everything here is best-effort and read-only, so it works on any platform
    without extra dependencies.
    """
    lines = ["# ScriptDB system configuration",
             f"# captured: {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
             "",
             "[platform]",
             f"system={os.name}",
             f"platform={sys.platform}",
             f"python={sys.version.split()[0]}",
             f"executable={sys.executable}",
             f"cwd={os.getcwd()}",
             "",
             "[environment]"]
    for key, value in sorted(os.environ.items(), key=lambda kv: kv[0].lower()):
        lines.append(f"{key}={value}")
    return lines


def hex_dump_rows(data, other, base_offset=0, width=16, limit=512):
    """Build a side-by-side hex+ASCII diff view of two byte blobs.

    Returns rows with the offset, each side's hex bytes, each side's ASCII and
    a ``changed`` flag, so the hex view can highlight differing bytes.
    """
    data = data or b""
    other = other or b""
    rows = []
    span = max(len(data), len(other))
    for base in range(0, span, width):
        if len(rows) >= limit:
            break
        chunk_a = data[base:base + width]
        chunk_b = other[base:base + width]
        rows.append({
            "offset": base + base_offset,
            "left_hex": " ".join(f"{b:02x}" for b in chunk_a),
            "right_hex": " ".join(f"{b:02x}" for b in chunk_b),
            "left_ascii": "".join(chr(b) if 32 <= b < 127 else "." for b in chunk_a),
            "right_ascii": "".join(chr(b) if 32 <= b < 127 else "." for b in chunk_b),
            "changed": chunk_a != chunk_b,
        })
    return rows


class DiffMinimap(QtWidgets.QWidget):
    """A narrow strip that shows where the changed blocks sit in a document."""

    WIDTH = 14

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(self.WIDTH)
        self.setSizePolicy(QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Expanding)
        self._rows = []
        self._view_row = 0
        self.setToolTip("Changed blocks overview")

    def set_rows(self, rows):
        self._rows = rows or []
        self.update()

    def set_view_row(self, row):
        self._view_row = max(0, int(row))
        self.update()

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        try:
            painter.fillRect(self.rect(), QtGui.QColor("#1b1b1b"))
            total = max(1, len(self._rows))
            height = max(1, self.height())
            colors = {
                "insert": "#2ea043",
                "delete": "#f85149",
                "replace": "#d29922",
            }
            for index, row in enumerate(self._rows):
                color = colors.get(row.get("tag"))
                if not color:
                    continue
                y = int(index * height / total)
                h = max(2, int(height / total))
                painter.fillRect(1, y, self.WIDTH - 2, h, QtGui.QColor(color))
            marker_y = int(self._view_row * height / total)
            painter.setPen(QtGui.QPen(QtGui.QColor("#8ab4f8"), 1))
            painter.drawLine(0, marker_y, self.WIDTH, marker_y)
        finally:
            painter.end()

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton and self._rows:
            ratio = event.pos().y() / max(1, self.height())
            row = int(ratio * len(self._rows))
            parent = self.parent()
            if hasattr(parent, "scroll_to_row"):
                parent.scroll_to_row(row)
            event.accept()
class DiffTab(QtWidgets.QWidget):
    """Professional diff viewer with several views and a rich control panel.

    Views: side-by-side, unified, inline, change blocks, AST tree and a
    directory-tree comparison. It also supports drag & drop of files, a
    history of recent comparisons and snapshots taken from the script list.
    """

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.setAcceptDrops(True)
        self._left = self._blank_source("(left)")
        self._right = self._blank_source("(right)")
        self._rows = []
        self._diff_positions = []
        self._current_pos = -1
        self._recent = []
        self._syncing_scroll = False
        self._build_ui()

    # --------------------------------------------------------------- helpers
    @staticmethod
    def _blank_source(name):
        return {"name": name, "path": "", "code": "", "category": "Other"}

    def t(self, key, fallback=None, **kwargs):
        return self.window.t(key, fallback, **kwargs)

    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)

        # ---- file panel: drop area, pickers and history ---------------
        file_row = QtWidgets.QHBoxLayout()
        self.left_picker = QtWidgets.QComboBox()
        self.left_picker.setMinimumWidth(230)
        self.right_picker = QtWidgets.QComboBox()
        self.right_picker.setMinimumWidth(230)
        self.left_picker.currentIndexChanged.connect(self.pick_left)
        self.right_picker.currentIndexChanged.connect(self.pick_right)
        self.btn_swap = QtWidgets.QPushButton("Swap")
        self.btn_swap.clicked.connect(self.swap_sides)
        file_row.addWidget(QtWidgets.QLabel("Left:"))
        file_row.addWidget(self.left_picker, 1)
        file_row.addSpacing(8)
        file_row.addWidget(QtWidgets.QLabel("Right:"))
        file_row.addWidget(self.right_picker, 1)
        file_row.addWidget(self.btn_swap)
        layout.addLayout(file_row)

        files_row = QtWidgets.QHBoxLayout()
        self.btn_open_left = QtWidgets.QPushButton("Open left file...")
        self.btn_open_right = QtWidgets.QPushButton("Open right file...")
        self.btn_open_dirs = QtWidgets.QPushButton("Compare folders...")
        self.btn_stdin = QtWidgets.QPushButton("From stdin...")
        self.btn_snapshot = QtWidgets.QPushButton("Snapshot ->")
        # Cross-tab: send this comparison to the Audit tab for review.
        self.btn_audit = QtWidgets.QPushButton("Audit comparison")
        self.recent_combo = QtWidgets.QComboBox()
        self.recent_combo.setMinimumWidth(200)
        self.recent_combo.addItem("Recent comparisons")
        self.recent_combo.activated.connect(self.load_recent)
        self.drop_hint = QtWidgets.QLabel("Drop files here (drag & drop)")
        self.drop_hint.setStyleSheet("color:#9aa0a6;")
        self.btn_open_left.clicked.connect(lambda: self.open_file("left"))
        self.btn_open_right.clicked.connect(lambda: self.open_file("right"))
        self.btn_open_dirs.clicked.connect(self.open_directories)
        self.btn_stdin.clicked.connect(self.load_from_stdin)
        self.btn_snapshot.clicked.connect(self.load_snapshot)
        self.btn_audit.clicked.connect(self.audit_comparison)
        files_row.addWidget(self.btn_open_left)
        files_row.addWidget(self.btn_open_right)
        files_row.addWidget(self.btn_open_dirs)
        files_row.addWidget(self.btn_stdin)
        files_row.addWidget(self.btn_snapshot)
        files_row.addWidget(self.btn_audit)
        files_row.addStretch()
        files_row.addWidget(self.drop_hint)
        files_row.addWidget(self.recent_combo)
        layout.addLayout(files_row)

        # ---- sessions: several diffs open at once ----------------------
        sessions_row = QtWidgets.QHBoxLayout()
        self.session_combo = QtWidgets.QComboBox()
        self.session_combo.setMinimumWidth(180)
        self.session_combo.activated.connect(self.switch_session)
        self.btn_new_session = QtWidgets.QPushButton("New session")
        self.btn_close_session = QtWidgets.QPushButton("Close session")
        self.btn_new_session.clicked.connect(lambda: self.new_session())
        self.btn_close_session.clicked.connect(self.close_session)
        sessions_row.addWidget(QtWidgets.QLabel("Sessions:"))
        sessions_row.addWidget(self.session_combo, 1)
        sessions_row.addWidget(self.btn_new_session)
        sessions_row.addWidget(self.btn_close_session)
        self.btn_stats = QtWidgets.QPushButton("Statistics")

        # A single "Export..." button whose menu holds every export target, so
        # the toolbar stays tidy as more formats are added.
        self.btn_export = QtWidgets.QToolButton()
        self.btn_export.setText("Export...")
        self.btn_export.setPopupMode(QtWidgets.QToolButton.MenuButtonPopup)
        self.export_menu = QtWidgets.QMenu(self.btn_export)
        self.act_export_patch = self.export_menu.addAction("Patch (.patch)")
        self.act_export_json = self.export_menu.addAction("JSON (.json)")
        self.act_export_txt = self.export_menu.addAction("TXT report (.txt)")
        self.export_menu.addSeparator()
        self.act_export_diag = self.export_menu.addAction("Diagnostics (.json)")
        self.act_export_patch.triggered.connect(lambda: self.export_report(".patch"))
        self.act_export_json.triggered.connect(lambda: self.export_report(".json"))
        self.act_export_txt.triggered.connect(lambda: self.export_report(".txt"))
        self.act_export_diag.triggered.connect(self.export_diagnostics)
        self.btn_export.setMenu(self.export_menu)
        # The button itself opens the menu (the arrow toggles it as well).
        self.btn_export.clicked.connect(self.show_export_menu)

        # A single "Modes..." button exposing the special diff modes.
        self.btn_modes = QtWidgets.QToolButton()
        self.btn_modes.setText("\U0001f9ea Modes...")
        self.btn_modes.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.modes_menu = QtWidgets.QMenu(self.btn_modes)
        self.mode_actions = {}
        for key, label_key in DIFF_SPECIAL_MODES:
            action = self.modes_menu.addAction(label_key)
            action.triggered.connect(lambda _checked=False, k=key: self.activate_mode(k))
            self.mode_actions[key] = action
        self.btn_modes.setMenu(self.modes_menu)

        self.btn_stats.clicked.connect(self.show_statistics)

        # "Shortcuts" button next to Statistics: opens a popup window listing
        # every keyboard shortcut available in the program.
        self.btn_shortcuts = QtWidgets.QPushButton(
            self.t("shortcuts_btn", "Shortcuts"))
        self.btn_shortcuts.setToolTip(
            self.t("shortcuts_btn_tip", "Show keyboard shortcuts"))
        # The shortcut list lives on the main window, not on this tab.
        self.btn_shortcuts.clicked.connect(self.window.show_shortcuts)

        sessions_row.addSpacing(12)
        sessions_row.addWidget(self.btn_modes)
        sessions_row.addWidget(self.btn_stats)
        sessions_row.addWidget(self.btn_shortcuts)
        sessions_row.addWidget(self.btn_export)
        layout.addLayout(sessions_row)
        # ---- control panel: algorithm, options, modes, views ----------
        control = QtWidgets.QHBoxLayout()
        self.algorithm_combo = QtWidgets.QComboBox()
        for key, label in (("myers", "Myers"), ("patience", "Patience"), ("histogram", "Histogram")):
            self.algorithm_combo.addItem(label, key)
        self.algorithm_combo.currentIndexChanged.connect(self.recompute)

        self.mode_combo = QtWidgets.QComboBox()
        for key, label in (("text", "Text"), ("token", "Token"), ("bytes", "Bytes")):
            self.mode_combo.addItem(label, key)
        self.mode_combo.currentIndexChanged.connect(self.recompute)

        self.chk_ws = QtWidgets.QCheckBox("Ignore whitespace")
        self.chk_comments = QtWidgets.QCheckBox("Ignore comments")
        self.chk_blank = QtWidgets.QCheckBox("Ignore blank lines")
        self.chk_case = QtWidgets.QCheckBox("Ignore case")
        for chk in (self.chk_ws, self.chk_comments, self.chk_blank, self.chk_case):
            chk.toggled.connect(self.recompute)

        self.chk_fast = QtWidgets.QCheckBox("Fast mode")
        self.chk_fast.toggled.connect(self.recompute)

        control.addWidget(QtWidgets.QLabel("Algorithm:"))
        control.addWidget(self.algorithm_combo)
        control.addWidget(QtWidgets.QLabel("Compare:"))
        control.addWidget(self.mode_combo)
        control.addSpacing(10)
        control.addWidget(self.chk_ws)
        control.addWidget(self.chk_comments)
        control.addWidget(self.chk_blank)
        control.addWidget(self.chk_case)
        control.addSpacing(10)
        control.addWidget(self.chk_fast)
        layout.addLayout(control)

        # ---- view selector + navigation --------------------------------
        views = QtWidgets.QHBoxLayout()
        self.view_buttons = {}
        view_group = QtWidgets.QButtonGroup(self)
        for key, label in (("side", "Side-by-side"), ("unified", "Unified"),
                           ("inline", "Inline"), ("blocks", "Blocks"),
                           ("ast", "AST"), ("analysis", "Analysis"),
                           ("files", "Folders")):
            button = QtWidgets.QRadioButton(label)
            view_group.addButton(button)
            views.addWidget(button)
            self.view_buttons[key] = button
        # The "side" view is preselected at the very end of _build_ui(): its
        # toggled signal must not fire while self.stack is still missing.
        self._view_signal_bound = False
        views.addStretch()
        self.btn_prev = QtWidgets.QPushButton("Prev change")
        self.btn_next = QtWidgets.QPushButton("Next change")
        self.btn_wrap = QtWidgets.QPushButton("Wrap: off")
        self.btn_wrap.setCheckable(True)
        self.btn_wrap.toggled.connect(self.toggle_wrap)
        self.btn_prev.clicked.connect(lambda: self.goto_change(-1))
        self.btn_next.clicked.connect(lambda: self.goto_change(1))
        views.addWidget(self.btn_prev)
        views.addWidget(self.btn_next)
        views.addWidget(self.btn_wrap)
        layout.addLayout(views)

        # ---- change list sidebar + filters + search -------------------
        filters = QtWidgets.QHBoxLayout()
        self.filter_added = QtWidgets.QCheckBox("Added")
        self.filter_removed = QtWidgets.QCheckBox("Removed")
        self.filter_changed = QtWidgets.QCheckBox("Changed")
        self.filter_symbols = QtWidgets.QCheckBox("In functions")
        for chk in (self.filter_added, self.filter_removed,
                    self.filter_changed, self.filter_symbols):
            chk.setChecked(True)
            chk.toggled.connect(self.refresh_change_list)
        self.collapse_blocks = QtWidgets.QCheckBox("Collapse unchanged")
        self.collapse_blocks.toggled.connect(self.refresh_view)
        self.low_graphics = QtWidgets.QCheckBox("Low graphics")
        self.low_graphics.toggled.connect(self.refresh_view)
        filters.addWidget(QtWidgets.QLabel("Show:"))
        filters.addWidget(self.filter_added)
        filters.addWidget(self.filter_removed)
        filters.addWidget(self.filter_changed)
        filters.addWidget(self.filter_symbols)
        filters.addSpacing(10)
        filters.addWidget(self.collapse_blocks)
        filters.addWidget(self.low_graphics)

        self.search_edit = QtWidgets.QLineEdit()
        self.search_edit.setPlaceholderText("Search in diff...")
        self.search_edit.textChanged.connect(self.search_changes)
        self.search_prev = QtWidgets.QPushButton("Prev hit")
        self.search_next = QtWidgets.QPushButton("Next hit")
        self.search_prev.clicked.connect(lambda: self.goto_search(-1))
        self.search_next.clicked.connect(lambda: self.goto_search(1))
        filters.addStretch()
        filters.addWidget(self.search_edit, 1)
        filters.addWidget(self.search_prev)
        filters.addWidget(self.search_next)
        layout.addLayout(filters)

        self.progress_bar = QtWidgets.QProgressBar()
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)

        self.summary = QtWidgets.QLabel("")
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet(
            "background:#2d2d2d; border:1px solid #404040; padding:5px;"
            " color:#bfc7d5; font-family:Consolas, monospace;"
        )
        layout.addWidget(self.summary)
        # ---- result area: a stack of the different views ----------------
        body = QtWidgets.QHBoxLayout()
        self.stack = QtWidgets.QStackedWidget()

        # side-by-side view
        self.side_splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.left_view = QtWidgets.QPlainTextEdit()
        self.right_view = QtWidgets.QPlainTextEdit()
        for view, colour in ((self.left_view, "#241c1c"), (self.right_view, "#1c241c")):
            view.setReadOnly(True)
            view.setFont(QFont("Consolas", 10))
            view.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
            view.setStyleSheet(f"background:{colour}; color:#dfe7f5;")
        self.left_view.verticalScrollBar().valueChanged.connect(
            lambda v: self._sync_scroll(self.left_view, self.right_view, v))
        self.right_view.verticalScrollBar().valueChanged.connect(
            lambda v: self._sync_scroll(self.right_view, self.left_view, v))
        self.left_view.cursorPositionChanged.connect(self._update_minimap_marker)
        self.side_splitter.addWidget(self.left_view)
        self.side_splitter.addWidget(self.right_view)
        self.side_splitter.setSizes([480, 480])
        self.stack.addWidget(self.side_splitter)          # index 0 side

        # unified view
        self.unified_view = QtWidgets.QPlainTextEdit()
        self.unified_view.setReadOnly(True)
        self.unified_view.setFont(QFont("Consolas", 10))
        self.unified_view.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
        self.unified_view.setStyleSheet("background:#1f1f1f; color:#dfe7f5;")
        self.stack.addWidget(self.unified_view)           # index 1 unified

        # inline view
        self.inline_view = QtWidgets.QPlainTextEdit()
        self.inline_view.setReadOnly(True)
        self.inline_view.setFont(QFont("Consolas", 10))
        self.inline_view.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
        self.inline_view.setStyleSheet("background:#1f1f1f; color:#dfe7f5;")
        self.stack.addWidget(self.inline_view)            # index 2 inline

        # blocks view (collapse/expand unchanged sections)
        self.block_tree = QtWidgets.QTreeWidget()
        self.block_tree.setAlternatingRowColors(True)
        self.block_tree.setColumnCount(2)
        self.block_tree.setHeaderLabels(["Change", "Preview"])
        self.block_tree.setFont(QFont("Consolas", 10))
        self.stack.addWidget(self.block_tree)             # index 3 blocks

        # AST view
        self.ast_tree = QtWidgets.QTreeWidget()
        self.ast_tree.setAlternatingRowColors(True)
        self.ast_tree.setColumnCount(3)
        self.ast_tree.setHeaderLabels(["Symbol", "Kind", "Status"])
        self.stack.addWidget(self.ast_tree)               # index 4 ast

        # analysis view (language, AST diff, refactors, duplicates)
        self.analysis_tree = QtWidgets.QTreeWidget()
        self.analysis_tree.setAlternatingRowColors(True)
        self.analysis_tree.setColumnCount(2)
        self.analysis_tree.setHeaderLabels(["Item", "Detail"])
        self.analysis_tree.setFont(QFont("Consolas", 10))
        self.stack.addWidget(self.analysis_tree)          # index 5 analysis

        # directory view
        self.dir_tree = QtWidgets.QTreeWidget()
        self.dir_tree.setAlternatingRowColors(True)
        self.dir_tree.setColumnCount(3)
        self.dir_tree.setHeaderLabels(["Path", "Status", "Left / Right"])
        self.dir_tree.itemDoubleClicked.connect(self._open_dir_entry)
        self.stack.addWidget(self.dir_tree)               # index 6 files
        # ---- special modes: merge (3/4-way), live, system, env, hex ----
        # Each special mode is a plain widget so it can be added to the same
        # QStackedWidget the ordinary views use.
        self.merge3_panel = self._build_merge_panel(three_way=True)
        self.stack.addWidget(self.merge3_panel)           # index 7 merge3
        self.merge4_panel = self._build_merge_panel(three_way=False)
        self.stack.addWidget(self.merge4_panel)           # index 8 merge4
        self.live_panel = self._build_live_panel()
        self.stack.addWidget(self.live_panel)             # index 9 live
        self.sysrest_panel = self._build_collect_panel("sysrest")
        self.stack.addWidget(self.sysrest_panel)          # index 10 sysrest
        self.env_panel = self._build_collect_panel("env")
        self.stack.addWidget(self.env_panel)              # index 11 env
        self.hex_panel = self._build_hex_panel()
        self.stack.addWidget(self.hex_panel)              # index 12 hex
        self.change_list = QtWidgets.QListWidget()
        self.change_list.setMinimumWidth(220)
        self.change_list.currentRowChanged.connect(self.change_list_selected)
        self.change_panel = QtWidgets.QWidget()
        change_layout = QtWidgets.QVBoxLayout(self.change_panel)
        change_layout.addWidget(QtWidgets.QLabel("Changes"))
        change_layout.addWidget(self.change_list)
        body.addWidget(self.change_panel)
        body.addWidget(self.stack, 1)
        self.minimap = DiffMinimap(self)
        body.addWidget(self.minimap)
        layout.addLayout(body, 1)

        self.status = QtWidgets.QLabel("")
        layout.addWidget(self.status)

        for button in self.view_buttons.values():
            button.toggled.connect(self.refresh_view)
        self._view_signal_bound = True
        self.view_buttons["side"].setChecked(True)

        self.refresh_pickers()
        self.recompute()

    # ---------------------------------------------------- special mode UI
    def _build_merge_panel(self, three_way=True):
        """Build the three-/four-way merge panel with pickers and results."""
        panel = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(panel)
        rows = (("base", "diff_merge_base"), ("ours", "diff_merge_ours"),
                ("theirs", "diff_merge_theirs"))
        if not three_way:
            rows = rows + (("extra", "diff_merge_ours"),)
        pickers = {}
        for slot, label_key in rows:
            row = QtWidgets.QHBoxLayout()
            row.addWidget(QtWidgets.QLabel(self.t(label_key)))
            combo = QtWidgets.QComboBox()
            combo.setMinimumWidth(220)
            row.addWidget(combo, 1)
            open_btn = QtWidgets.QPushButton(self.t("diff_open_file"))
            open_btn.clicked.connect(lambda _c=False, s=slot: self._merge_open(s))
            row.addWidget(open_btn)
            pickers[slot] = combo
            layout.addLayout(row)
        out_row = QtWidgets.QHBoxLayout()
        out_row.addWidget(QtWidgets.QLabel(self.t("diff_merge_result")))
        out_row.addStretch()
        run_btn = QtWidgets.QPushButton(self.t("diff_merge_run"))
        run_btn.clicked.connect(lambda: self.run_merge())
        out_row.addWidget(run_btn)
        take = QtWidgets.QComboBox()
        take.addItem(self.t("diff_merge_take_left"), "left")
        take.addItem(self.t("diff_merge_take_right"), "right")
        take.addItem(self.t("diff_merge_take_both"), "both")
        out_row.addWidget(take)
        resolve_btn = QtWidgets.QPushButton("OK")
        resolve_btn.setToolTip("Resolve selected conflict")
        resolve_btn.clicked.connect(lambda: self.resolve_merge_conflict(take.currentData()))
        out_row.addWidget(resolve_btn)
        layout.addLayout(out_row)
        tree = QtWidgets.QTreeWidget()
        tree.setAlternatingRowColors(True)
        tree.setColumnCount(4)
        tree.setHeaderLabels(["Tag", "Ours", "Theirs", "Result"])
        tree.setFont(QFont("Consolas", 10))
        layout.addWidget(tree, 1)
        layout.addWidget(QtWidgets.QLabel("Pick two sides (with a common base) and press Merge."))
        panel.merge_three_way = three_way
        panel.merge_slots = {}          # slot -> source dict
        panel.merge_pickers = pickers
        panel.merge_tree = tree
        panel.merge_take = take
        return panel
    def _build_live_panel(self):
        """Build the live diff panel: in-memory buffer vs a file on disk."""
        panel = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(panel)
        top = QtWidgets.QHBoxLayout()
        panel.live_path = QtWidgets.QLabel(self.t("diff_live_no_file"))
        open_btn = QtWidgets.QPushButton(self.t("diff_open_file"))
        live_refresh = QtWidgets.QPushButton(self.t("diff_live_refresh"))
        auto = QtWidgets.QCheckBox(self.t("diff_live_auto"))
        top.addWidget(panel.live_path, 1)
        top.addWidget(open_btn)
        top.addWidget(live_refresh)
        top.addWidget(auto)
        layout.addLayout(top)
        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        memory = QtWidgets.QPlainTextEdit()
        memory.setPlaceholderText(self.t("diff_live_memory"))
        memory.setFont(QFont("Consolas", 10))
        disk = QtWidgets.QPlainTextEdit()
        disk.setReadOnly(True)
        disk.setFont(QFont("Consolas", 10))
        splitter.addWidget(memory)
        splitter.addWidget(disk)
        splitter.setSizes([480, 480])
        layout.addWidget(splitter, 1)
        panel.live_memory = memory
        panel.live_file_view = disk
        open_btn.clicked.connect(self.live_open_file)
        live_refresh.clicked.connect(self.refresh_live_diff)
        memory.textChanged.connect(self.refresh_live_diff)
        timer = QtCore.QTimer(panel)
        timer.setInterval(1000)
        timer.timeout.connect(self.refresh_live_diff)
        panel.live_timer = timer
        auto.toggled.connect(lambda on: timer.start() if on else timer.stop())
        panel.live_auto = auto
        return panel
    def _build_collect_panel(self, kind):
        """Build the environment / system-configuration collector panel."""
        panel = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(panel)
        top = QtWidgets.QHBoxLayout()
        run_btn = QtWidgets.QPushButton(
            self.t("diff_env_run" if kind == "env" else "diff_sysrest_run"))
        hint = QtWidgets.QLabel(
            self.t("diff_env_hint" if kind == "env" else "diff_sysrest_hint"))
        hint.setStyleSheet("color:#9aa0a6;")
        top.addWidget(run_btn)
        top.addWidget(hint, 1)
        layout.addLayout(top)
        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        left = QtWidgets.QPlainTextEdit()
        right = QtWidgets.QPlainTextEdit()
        for view in (left, right):
            view.setReadOnly(True)
            view.setFont(QFont("Consolas", 10))
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setSizes([480, 480])
        layout.addWidget(splitter, 1)
        panel.collect_kind = kind
        panel.collect_left = left
        panel.collect_right = right
        run_btn.clicked.connect(lambda: self.collect_special(kind))
        return panel
    def _build_hex_panel(self):
        """Build the binary hex diff panel."""
        panel = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(panel)
        top = QtWidgets.QHBoxLayout()
        open_left = QtWidgets.QPushButton(self.t("diff_open_file") + " (L)")
        open_right = QtWidgets.QPushButton(self.t("diff_open_file") + " (R)")
        refresh = QtWidgets.QPushButton(self.t("diff_live_refresh"))
        hint = QtWidgets.QLabel(self.t("diff_hex_hint"))
        hint.setStyleSheet("color:#9aa0a6;")
        top.addWidget(open_left)
        top.addWidget(open_right)
        top.addWidget(refresh)
        top.addWidget(hint, 1)
        layout.addLayout(top)
        tree = QtWidgets.QTreeWidget()
        tree.setAlternatingRowColors(True)
        tree.setColumnCount(5)
        tree.setHeaderLabels([self.t("diff_hex_offset"), self.t("diff_hex_side"),
                              self.t("diff_hex_bytes"), self.t("diff_hex_ascii"), ""])
        tree.setFont(QFont("Consolas", 10))
        layout.addWidget(tree, 1)
        panel.hex_tree = tree
        panel.hex_bytes = {"left": b"", "right": b""}
        open_left.clicked.connect(lambda: self.hex_open_file("left"))
        open_right.clicked.connect(lambda: self.hex_open_file("right"))
        refresh.clicked.connect(self.refresh_hex_diff)
        return panel
    # ------------------------------------------------------ special modes
    def activate_mode(self, key):
        """Switch to a special mode from the Modes... menu."""
        if key in DIFF_VIEWS:
            self.view_buttons_stack_mode = key
            # The special modes have no radio button; drive the stack directly.
            index = DIFF_VIEWS.index(key)
            self.stack.setCurrentIndex(index)
            self._current_special = key
            if key in ("merge3", "merge4"):
                self._fill_merge_pickers(key)
            elif key == "live":
                self.refresh_live_diff()
            elif key in ("sysrest", "env"):
                self.collect_special(key)
            elif key == "hex":
                self.refresh_hex_diff()
            self.status.setText(self.t(dict(DIFF_SPECIAL_MODES)[key]))

    def _current_special_mode(self):
        return getattr(self, "_current_special", None)

    # ---- three/four-way merge
    def _merge_field_sources(self):
        return [self._left, self._right]

    def _fill_merge_pickers(self, key):
        panel = self.merge4_panel if key == "merge4" else self.merge3_panel
        for slot, combo in panel.merge_pickers.items():
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("-", None)
            for script in self.window.scripts_only():
                combo.addItem(script.get("name", ""), ("script", script.get("id")))
            combo.blockSignals(False)
            # Sensible defaults: base=left, ours=right, theirs=first script.
            if slot == "base":
                combo.setCurrentIndex(min(1, combo.count() - 1))
            elif slot == "ours":
                combo.setCurrentIndex(min(2, combo.count() - 1))
            elif slot == "theirs":
                combo.setCurrentIndex(min(1, combo.count() - 1))

    def _merge_open(self, slot):
        panel = self.merge4_panel if getattr(self, "_current_special", "") == "merge4" \
            else self.merge3_panel
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, self.t("diff_open_file"), "", supported_extensions_filter())
        if not path:
            return
        source = self._read_source(path, slot)
        panel.merge_slots[slot] = source
        panel.merge_pickers[slot].blockSignals(True)
        panel.merge_pickers[slot].insertItem(0, source["name"] + "  (file)", ("file", id(source)))
        panel.merge_pickers[slot].setCurrentIndex(0)
        panel.merge_pickers[slot].blockSignals(False)

    def _merge_slot_text(self, panel, slot):
        # A file opened explicitly wins, otherwise read the picker's script.
        if slot in panel.merge_slots:
            return panel.merge_slots[slot].get("code", "")
        combo = panel.merge_pickers.get(slot)
        if combo is None:
            return ""
        data = combo.currentData()
        entry = self._source_for_data(data)
        return entry.get("code", "") if entry else ""

    def run_merge(self):
        """Compute the merge and fill the result tree."""
        key = getattr(self, "_current_special", "merge3")
        panel = self.merge4_panel if key == "merge4" else self.merge3_panel
        base = self._merge_slot_text(panel, "base")
        ours = self._merge_slot_text(panel, "ours")
        theirs = self._merge_slot_text(panel, "theirs")
        tree = panel.merge_tree
        tree.clear()
        if key == "merge4":
            extra = self._merge_slot_text(panel, "extra")
            rows, conflicts = merge_four_way(base, ours, theirs, extra)
        else:
            rows = merge_three_way(base, ours, theirs)
            conflicts = [i for i, r in enumerate(rows) if r["tag"] == "conflict"]
        colours = {"both": "#9be9a8", "ours": "#8ab4f8", "theirs": "#c586c0",
                   "conflict": "#ff9a9a"}
        for row in rows:
            item = QtWidgets.QTreeWidgetItem([
                row["tag"],
                "\n".join(row.get("ours") if isinstance(row.get("ours"), list)
                            else [row.get("ours", "")]),
                "\n".join(row.get("theirs") if isinstance(row.get("theirs"), list)
                            else [row.get("theirs", "")]),
                row.get("text", ""),
            ])
            item.setForeground(0, QtGui.QColor(colours.get(row["tag"], "#dfe7f5")))
            tree.addTopLevelItem(item)
        tree.expandAll()
        if conflicts:
            self.status.setText(self.t("diff_merge_conflict", count=len(conflicts)))
        else:
            self.status.setText(self.t("diff_merge_clean"))
        panel.merge_conflicts = conflicts
        panel.merge_result_rows = rows
    def resolve_merge_conflict(self, choice):
        """Apply the chosen resolution to every conflict in the result tree."""
        key = getattr(self, "_current_special", "merge3")
        panel = self.merge4_panel if key == "merge4" else self.merge3_panel
        rows = getattr(panel, "merge_result_rows", None)
        if not rows:
            return
        for row in rows:
            if row["tag"] != "conflict":
                continue
            ours = row.get("ours") or []
            theirs = row.get("theirs") or []
            if choice == "left":
                row["text"] = "\n".join(ours)
            elif choice == "right":
                row["text"] = "\n".join(theirs)
            else:
                row["text"] = "\n".join(list(ours) + list(theirs))
            row["tag"] = "resolved"
        panel.merge_tree.clear()
        colours = {"both": "#9be9a8", "ours": "#8ab4f8", "theirs": "#c586c0",
                   "conflict": "#ff9a9a", "resolved": "#9be9a8"}
        for row in rows:
            item = QtWidgets.QTreeWidgetItem([
                row["tag"],
                "\n".join(row.get("ours") if isinstance(row.get("ours"), list)
                            else [row.get("ours", "")]),
                "\n".join(row.get("theirs") if isinstance(row.get("theirs"), list)
                            else [row.get("theirs", "")]),
                row.get("text", ""),
            ])
            item.setForeground(0, QtGui.QColor(colours.get(row["tag"], "#dfe7f5")))
            panel.merge_tree.addTopLevelItem(item)
        panel.merge_tree.expandAll()
        self.status.setText(self.t("diff_merge_clean"))

    # ---- live diff
    def live_open_file(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, self.t("diff_open_file"), "", supported_extensions_filter())
        if not path:
            return
        self._live_path = path
        self.live_panel.live_path.setText(self.t("diff_live_file", name=path))
        self.refresh_live_diff()

    def refresh_live_diff(self):
        """Re-read the watched file and compare it with the in-memory buffer."""
        panel = self.live_panel
        memory = panel.live_memory.toPlainText()
        path = getattr(self, "_live_path", "")
        if not path or not os.path.isfile(path):
            panel.live_file_view.setPlainText(self.t("diff_live_no_file"))
            return
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                disk = handle.read()
        except OSError as exc:
            panel.live_file_view.setPlainText(str(exc))
            return
        self._left = {"name": self.t("diff_live_memory"), "path": "",
                      "code": memory, "category": "Other"}
        self._right = {"name": os.path.basename(path), "path": path,
                       "code": disk, "category": category_for_extension(path) or "Other"}
        rows = align_lines(memory.splitlines(), disk.splitlines(), "myers")
        marker = {"insert": "+", "delete": "-", "replace": "~", "equal": " "}
        panel.live_file_view.setPlainText("\n".join(
            f"{marker.get(r['tag'], ' ')} "
            f"{r.get('right') if r['tag'] == 'insert' else r.get('left')}"
            for r in rows))

    # ---- environment / system configuration
    def collect_special(self, kind):
        """Snapshot the current environment or system configuration."""
        panel = self.env_panel if kind == "env" else self.sysrest_panel
        lines = environment_snapshot() if kind == "env" else system_configuration()
        panel.collect_left.setPlainText("\n".join(lines))
        panel.collect_right.setPlainText("\n".join(lines))
        self.status.setText(self.t("diff_env_run" if kind == "env" else "diff_sysrest_run"))

    # ---- binary hex diff
    def hex_open_file(self, side):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, self.t("diff_open_file"), "", supported_extensions_filter())
        if not path:
            return
        try:
            with open(path, "rb") as handle:
                data = handle.read()
        except OSError as exc:
            QtWidgets.QMessageBox.warning(self, self.t("warning"), str(exc))
            return
        self.hex_panel.hex_bytes[side] = data
        self.refresh_hex_diff()

    def refresh_hex_diff(self):
        """Rebuild the hex + ASCII diff tree from the two loaded blobs."""
        panel = self.hex_panel
        left = panel.hex_bytes.get("left", b"")
        right = panel.hex_bytes.get("right", b"")
        panel.hex_tree.clear()
        if not left and not right:
            return
        for row in hex_dump_rows(left, right):
            item = QtWidgets.QTreeWidgetItem([
                f"{row['offset']:08x}", "L", row["left_hex"], row["left_ascii"], ""])
            item2 = QtWidgets.QTreeWidgetItem([
                f"{row['offset']:08x}", "R", row["right_hex"], row["right_ascii"], ""])
            colour = "#ff9a9a" if row["changed"] else "#9be9a8"
            item.setForeground(1, QtGui.QColor(colour))
            item2.setForeground(1, QtGui.QColor(colour))
            panel.hex_tree.addTopLevelItem(item)
            panel.hex_tree.addTopLevelItem(item2)

    def audit_comparison(self):
        audit = getattr(self.window, "audit_tab", None)
        if audit is None or not hasattr(audit, "on_file_changed"):
            self.window.show_message(self.t("info"),
                                     self.t("audit_unavailable"))
            return
        left = self._left.get("name", "")
        right = self._right.get("name", "")
        # Record the comparison and send it to the audit as a change event.
        audit.on_file_changed("%s vs %s" % (left, right),
                              self.t("audit_diff_open", "diff reviewed"),
                              self._left.get("code", ""),
                              self._right.get("code", ""))
        self.window.open_audit_tab("audit_tab_timeline")

    # --------------------------------------------------------------- pickers
    def refresh_pickers(self):
        """Fill both pickers with the stored scripts and their revisions."""
        for combo in (self.left_picker, self.right_picker):
            previous = combo.currentData()
            combo.blockSignals(True)
            combo.clear()
            for script in self.window.scripts_only():
                combo.addItem(script.get("name", ""), ("script", script.get("id")))
                revisions = script.get("history", []) or []
                for index in range(len(revisions)):
                    label = f"    {script.get('name', '')} #{len(revisions) - index}"
                    combo.addItem(label, ("script", script.get("id"), index))
            combo.blockSignals(False)
            index = self._combo_index(combo, previous)
            if index >= 0:
                combo.setCurrentIndex(index)

    @staticmethod
    def _combo_index(combo, data):
        for index in range(combo.count()):
            if combo.itemData(index) == data:
                return index
        return -1

    def _source_for_data(self, data):
        if not data:
            return None
        script = self.window.find_script(data[1])
        if script is None:
            return None
        if len(data) > 2:
            revisions = script.get("history", []) or []
            index = data[2]
            if 0 <= index < len(revisions):
                entry = dict(revisions[index])
                entry.setdefault("name", script.get("name", ""))
                return entry
        return script

    def _entry_to_source(self, entry):
        return {
            "name": entry.get("name", ""),
            "path": "",
            "code": entry.get("code", ""),
            "category": entry.get("category", "Python"),
        }
    # ------------------------------------------------------------- options
    def options(self):
        return {
            "algorithm": self.algorithm_combo.currentData() or "myers",
            "mode": self.mode_combo.currentData() or "text",
            "ignore_ws": self.chk_ws.isChecked(),
            "ignore_comments": self.chk_comments.isChecked(),
            "ignore_blank": self.chk_blank.isChecked(),
            "ignore_case": self.chk_case.isChecked(),
            "fast": self.chk_fast.isChecked(),
        }

    def set_sources(self, left, right):
        """Set both sides at once (used by drag & drop and snapshots)."""
        self._left = left
        self._right = right
        self.drop_hint.setText(f"{left['name']}  vs  {right['name']}")
        self.recompute()

    # ------------------------------------------------------------- diffing
    def recompute(self, *args):
        opts = self.options()
        left_code = self._left.get("code", "") or ""
        right_code = self._right.get("code", "") or ""
        algorithm = "myers" if opts["fast"] else opts["algorithm"]

        if opts["mode"] == "bytes":
            self._rows = []
            self._byte_report = bytes_diff_lines(
                left_code.encode("utf-8", "replace"),
                right_code.encode("utf-8", "replace"),
            )
        elif opts["mode"] == "token":
            self._byte_report = None
            self._rows, _lt, _rt = token_diff_lines(left_code, right_code, algorithm)
        else:
            self._byte_report = None
            language = self._left.get("category", "")
            left_lines = prepare_diff_lines(
                left_code, opts["ignore_case"], opts["ignore_ws"],
                opts["ignore_blank"], opts["ignore_comments"], language)
            right_lines = prepare_diff_lines(
                right_code, opts["ignore_case"], opts["ignore_ws"],
                opts["ignore_blank"], opts["ignore_comments"], language)
            self._rows = align_lines(left_lines, right_lines, algorithm)

        self._diff_positions = [
            index for index, row in enumerate(self._rows)
            if row.get("tag") != "equal"
        ]
        self._current_pos = -1
        self.minimap.set_rows(self._rows)
        self._update_summary()
        self._update_symbol_flags()
        self._update_progress()
        self.refresh_view()
        self.refresh_change_list()
        self.search_changes()
        if hasattr(self, "session_manager"):
            self.snapshot_session()
        # Tell any registered plugin that this comparison finished, and log
        # every change block so a terminal front-end can audit the diff.
        # _blocks is read defensively: it is set by refresh_change_list above,
        # but that call bails out early when the sidebar is missing.
        blocks = getattr(self, "_blocks", [])
        HOOKS.fire("before_diff", left=self._left, right=self._right,
                   options=self.options())
        for start, block in blocks:
            HOOKS.fire("change_log", start=start, rows=block)
        HOOKS.fire("after_diff", report={
            "left": self._left, "right": self._right, "rows": self._rows,
            "blocks": blocks,
        })

    def _update_summary(self):
        added = sum(1 for row in self._rows if row.get("tag") == "insert")
        removed = sum(1 for row in self._rows if row.get("tag") == "delete")
        changed = sum(1 for row in self._rows if row.get("tag") == "replace")
        same = sum(1 for row in self._rows if row.get("tag") == "equal")
        identical = (added + removed + changed) == 0
        colour = "#9be9a8" if identical else "#f7c66d"
        self.summary.setStyleSheet(
            "background:#2d2d2d; border:1px solid #404040; padding:5px;"
            f" color:{colour}; font-family:Consolas, monospace;"
        )
        if self.options()["mode"] == "bytes":
            self.summary.setText(
                f"{self._left['name']}  vs  {self._right['name']}  |  "
                f"byte differences: {len(self._byte_report or [])}"
            )
        else:
            self.summary.setText(
                f"{self._left['name']}  vs  {self._right['name']}  |  "
                f"identical: {same}  changed: {changed}  added: {added}  "
                f"removed: {removed}  |  changes: {added + removed + changed}"
            )
    def retranslate(self, table):
        """Apply the language table to the Diff tab's own controls."""
        def label(key, fallback):
            return table.get(key, fallback)
        self.filter_added.setText(label("diff_filter_added", "Added"))
        self.filter_removed.setText(label("diff_filter_removed", "Removed"))
        self.filter_changed.setText(label("diff_filter_changed", "Changed"))
        self.filter_symbols.setText(label("diff_filter_symbols", "In functions"))
        self.collapse_blocks.setText(label("diff_collapse", "Collapse unchanged"))
        self.low_graphics.setText(label("diff_low_graphics", "Low graphics"))
        self.search_edit.setPlaceholderText(label("diff_search", "Search in diff..."))
        self.search_prev.setText(label("diff_prev_hit", "Prev hit"))
        self.search_next.setText(label("diff_next_hit", "Next hit"))
        self.btn_swap.setText(label("compare_swap", "Swap"))
        self.btn_prev.setText(label("compare_prev", "Prev diff"))
        self.btn_next.setText(label("compare_next", "Next diff"))
        if "analysis" in self.view_buttons:
            self.view_buttons["analysis"].setText(label("diff_view_analysis", "Analysis"))
        self.btn_stats.setText(label("statistics", "Statistics"))
        if hasattr(self, "btn_shortcuts"):
            self.btn_shortcuts.setText(label("shortcuts_btn", "Shortcuts"))
        self.btn_export.setText(label("diff_export_menu", "Export..."))
        self.act_export_patch.setText(label("diff_export_patch", "Patch (.patch)"))
        self.act_export_json.setText(label("diff_export_json", "JSON (.json)"))
        self.act_export_txt.setText(label("diff_export_txt", "TXT report (.txt)"))
        self.act_export_diag.setText(label("diff_diagnostics", "Diagnostics (.json)"))
        self.btn_new_session.setText(label("diff_new_session", "New session"))
        self.btn_close_session.setText(label("diff_close_session", "Close session"))
        self.btn_modes.setText(label("diff_modes_menu", "\U0001f9ea Modes..."))
        for key, label_key in DIFF_SPECIAL_MODES:
            if key in self.mode_actions:
                self.mode_actions[key].setText(label(label_key, label_key))

    # ------------------------------------------------- change list & filters
    def _row_passes_filters(self, row):
        kind = change_kind(row)
        if kind == "added" and not self.filter_added.isChecked():
            return False
        if kind == "removed" and not self.filter_removed.isChecked():
            return False
        if kind == "changed" and not self.filter_changed.isChecked():
            return False
        if self.filter_symbols.isChecked():
            # Only keep changes that live inside a known symbol, or that look
            # like a definition rename.
            flags = getattr(self, "_symbol_spans", None) or []
            index = row.get("_index", -1)
            if not (0 <= index < len(flags)) or not flags[index]:
                return False
        return True

    def _current_symbol_names(self, row_index):
        """Symbols whose document range covers the given row (left or right)."""
        names = []
        for offsets, symbols in ((self._left_line_offsets, self._left_symbols),
                                 (self._right_line_offsets, self._right_symbols)):
            if not symbols or not offsets:
                continue
            index = max(0, min(len(offsets) - 1, row_index))
            offset = offsets[index]
            names.extend(symbols_in_range(symbols, offset, offset + 1))
        return sorted(set(names))

    @staticmethod
    def _line_start_offsets(code):
        # Character offset where each 1-based line begins. Built once per
        # comparison, not inside the per-row loop.
        offsets = []
        running = 0
        for line in (code or "").splitlines():
            offsets.append(running)
            running += len(line) + 1
        return offsets

    def refresh_change_list(self, *_args):
        """Rebuild the sidebar list of changes, honouring the filters."""
        if not hasattr(self, "change_list"):
            return
        self.change_list.blockSignals(True)
        self.change_list.clear()
        self._blocks = iter_change_blocks(self._rows)
        shown = 0
        for index, (start, rows) in enumerate(self._blocks):
            first = rows[0]
            first["_index"] = start
            if not self._row_passes_filters(first):
                continue
            added = sum(1 for r in rows if change_kind(r) == "added")
            removed = sum(1 for r in rows if change_kind(r) == "removed")
            changed = sum(1 for r in rows if change_kind(r) == "changed")
            names = self._current_symbol_names(start)
            label = f"#{index + 1}  +{added} -{removed} ~{changed}"
            if names:
                label += "  [" + ", ".join(names) + "]"
            item = QtWidgets.QListWidgetItem(label)
            colours = {"added": "#9be9a8", "removed": "#ff9a9a", "changed": "#f7c66d"}
            key = "added" if added else ("removed" if removed else "changed")
            item.setForeground(QtGui.QColor(colours.get(key, "#dfe7f5")))
            item.setData(QtCore.Qt.UserRole, start)
            self.change_list.addItem(item)
            shown += 1
        self.change_list.blockSignals(False)
        total = len(self._blocks)
        self.status.setText(f"Changes: {shown} / {total} shown")
        self._update_symbol_flags()

    def _update_symbol_flags(self):
        """Mark which rows sit inside a function/class (for the symbol filter)."""
        language = self._left.get("category", "")
        left = self._left.get("code", "") or ""
        right = self._right.get("code", "") or ""
        self._left_symbols = ast_symbols(left, language)
        self._right_symbols = ast_symbols(right, language)
        # Line start offsets are cached here so the per-row symbol lookup in
        # _current_symbol_names stays linear instead of rescanning the code.
        self._left_line_offsets = self._line_start_offsets(left)
        self._right_line_offsets = self._line_start_offsets(right)
        flags = []
        for index, row in enumerate(self._rows):
            inside = bool(self._current_symbol_names(index))
            definition = _structural_name_change(row.get("left"), row.get("right"))
            flags.append(inside or definition)
        self._symbol_spans = flags

    def change_list_selected(self, row):
        if row < 0:
            return
        item = self.change_list.item(row)
        if item is None:
            return
        start = item.data(QtCore.Qt.UserRole)
        if self.current_view() == "unified":
            self._scroll_view(self.unified_view, start)
        elif self.current_view() == "inline":
            self._scroll_view(self.inline_view, start)
        else:
            self._scroll_view(self.left_view, start)
            self._scroll_view(self.right_view, start)
        self.minimap.set_view_row(start)
        self._current_pos = -1

    # ------------------------------------------------------------ searching
    def search_changes(self, _text=None):
        """Highlight every row matching the search box and list the count."""
        needle = self.search_edit.text().strip()
        self._search_hits = search_in_rows(self._rows, needle)
        self._search_pos = -1
        self._apply_search_highlight()
        if needle:
            self.status.setText(
                f"Found {len(self._search_hits)} line(s) for '{needle}'"
            )

    def _search_views(self):
        # The panes a search hit is painted in.
        return (self.left_view, self.right_view,
                self.unified_view, self.inline_view)

    def _search_hit_lines(self, view):
        # Row indices whose text appears in a view, in that view's numbering.
        #
        # A diff row index is a block number only in the side-by-side panes. The
        # unified pane adds a file header and one @@ marker per hunk, and the
        # inline pane prints two blocks for every replaced line, so a shared
        # index highlighted the wrong line there.

        if view is self.left_view or view is self.right_view:
            return set(self._search_hits)
        wanted = []
        for row_index in self._search_hits:
            wanted.extend(self._search_row_texts(row_index, view))
        return self._texts_in_view(view, wanted)

    def _search_row_texts(self, row_index, view):
        # The body lines one diff row contributes to a rendered view.
        if row_index < 0 or row_index >= len(self._rows):
            return []
        row = self._rows[row_index]
        tag = row.get("tag")
        if tag == "equal":
            return [row.get("left", "")]
        if tag == "insert":
            return [] if view is self.left_view else [row.get("right", "")]
        if tag == "delete":
            return [] if view is self.right_view else [row.get("left", "")]
        # A replaced row shows the left text in the left pane and the unified
        # pane, and the right text in the right pane.
        if view is self.right_view:
            return [row.get("right", "")]
        if view is self.unified_view:
            return [row.get("left", ""), row.get("right", "")]
        return [row.get("left", "")]

    @staticmethod
    def _texts_in_view(view, wanted):
        # Block numbers whose text matches one of `wanted`, each taken once.
        #
        # Text is matched ignoring case and surrounding whitespace, so the
        # comparison survives the small rewriting each view applies. Only the
        # first block per distinct body text is kept, which keeps the hit list
        # aligned with the diff rows rather than with the number of panes a row
        # happens to be drawn in.
        targets = set()
        for text in wanted:
            stripped = (text or "").strip()
            if stripped:
                targets.add(stripped.lower())
        found = set()
        seen = set()
        document = view.document()
        for index in range(document.blockCount()):
            block = document.findBlockByNumber(index)
            if not block.isValid():
                continue
            key = block.text().strip().lower()
            if not key:
                continue
            # Each pane prefixes the body line with its own gutter ('- ', '+ ',
            # '@@', a line number), so the block is matched when it *ends* with
            # the body text rather than when it equals it.
            match = key in targets or any(key.endswith(t) for t in targets)
            if match and key not in seen:
                seen.add(key)
                found.add(index)
        return found
    def _apply_search_highlight(self):
        # Paint the search hits in every pane, each in its own numbering.
        hits = set(getattr(self, "_search_hits", []))
        if not hits:
            for view in self._search_views():
                view.setExtraSelections([])
            return
        selection_format = QtGui.QTextCharFormat()
        selection_format.setBackground(QtGui.QColor("#5a4b1a"))
        selection_format.setForeground(QtGui.QColor("#ffe08a"))
        for view in self._search_views():
            selections = []
            document = view.document()
            for index in sorted(self._search_hit_lines(view)):
                block = document.findBlockByNumber(index)
                if not block.isValid():
                    continue
                selection = QtWidgets.QTextEdit.ExtraSelection()
                selection.format = selection_format
                cursor = QtGui.QTextCursor(block)
                cursor.select(QtGui.QTextCursor.LineUnderCursor)
                selection.cursor = cursor
                selections.append(selection)
            # The highlight replaces the per-pane selections instead of being
            # appended to them: the panes mark changes by colour, and appending
            # left the change colours behind a stale highlight.
            view.setExtraSelections(selections)

    def goto_search(self, direction):
        hits = getattr(self, "_search_hits", [])
        if not hits:
            return
        self._search_pos = (self._search_pos + direction) % len(hits)
        row_index = hits[self._search_pos]
        if self.current_view() == "unified":
            self._scroll_view(self.unified_view, row_index)
        elif self.current_view() == "inline":
            self._scroll_view(self.inline_view, row_index)
        else:
            self._scroll_view(self.left_view, row_index)
            self._scroll_view(self.right_view, row_index)
        self.status.setText(f"Hit {self._search_pos + 1} / {len(hits)}")

    # ------------------------------------------------------------- progress
    def _update_progress(self):
        if not hasattr(self, "progress_bar"):
            return
        total = max(1, len(self._rows))
        done = sum(1 for row in self._rows if row.get("tag") == "equal")
        self.progress_bar.setValue(int(done * 100 / total))

    # --------------------------------------------------------------- views
    def _apply_low_graphics(self):
        """Switch the panes to a flat, ASCII-friendly look when enabled."""
        if not hasattr(self, "low_graphics"):
            return
        low = self.low_graphics.isChecked()
        plain = "background:#000; color:#e6e6e6;" if low else None
        defaults = {
            self.left_view: "background:#241c1c; color:#dfe7f5;",
            self.right_view: "background:#1c241c; color:#dfe7f5;",
            self.unified_view: "background:#1f1f1f; color:#dfe7f5;",
            self.inline_view: "background:#1f1f1f; color:#dfe7f5;",
        }
        for view, style in defaults.items():
            view.setStyleSheet(plain if low else style)
        if low:
            for view in defaults:
                view.setExtraSelections([])

    def current_view(self):
        for key, button in self.view_buttons.items():
            if button.isChecked():
                return key
        return "side"

    def refresh_view(self, *args):
        view = self.current_view()
        index = DIFF_VIEWS.index(view) if view in DIFF_VIEWS else 0
        self.stack.setCurrentIndex(index)
        self._apply_low_graphics()

        # Every text pane is rebuilt on each pass, not only the one on top.
        # A pane that was never rendered holds a single empty block, so a
        # search hit mapped into it would land nowhere; rendering them all
        # also makes switching views instant, because the shortcuts in
        # goto_change and change_list rely on those documents being current.
        if self.options()["mode"] == "bytes":
            self.render_side_by_side()
            self.render_blocks()
            return
        self.render_side_by_side()
        self.render_unified()
        self.render_inline()
        self.render_blocks()

        if view == "ast":
            self.render_ast()
        elif view == "analysis":
            self.render_analysis()
        elif view == "files":
            self.render_files()
        elif view == "blocks":
            self.render_blocks()
        # The search highlight is painted last so it is applied to the text
        # the panes now hold, not to the text they held before the rebuild.
        self._apply_search_highlight()

    def _backgrounds(self, view, rows, side):
        row_colour = {"delete": "#3a1d1d", "insert": "#17331f", "replace": "#3b3418"}
        grey = "#3a3d41"
        lines = []
        formats = []
        long_run = 0
        for row in rows:
            if side == "left":
                number, text = row["left_no"], row["left"]
            else:
                number, text = row["right_no"], row["right"]
            tag = row.get("tag")
            if tag == "equal" and self.collapse_blocks.isChecked():
                long_run += 1
                if long_run > 6:
                    # Represent a long unchanged stretch with a single marker
                    # instead of printing every line.
                    continue
            else:
                long_run = 0
            arrow = {"insert": "+", "delete": "-", "replace": "~"}.get(tag, " ")
            if self.low_graphics.isChecked():
                prefix = f"{'----' if number else '    '} "
                lines.append(f"{arrow} {prefix}{text}")
                formats.append(None)
                continue
            prefix = f"{number:>4} | " if number else "     | "
            lines.append(prefix + text)
            formats.append(tag)
        view.setPlainText("\n".join(lines))
        selections = []
        document = view.document()
        for index, tag in enumerate(formats):
            colour = row_colour.get(tag)
            if not colour:
                continue
            block = document.findBlockByNumber(index)
            if not block.isValid():
                continue
            selection = QtWidgets.QTextEdit.ExtraSelection()
            selection.format.setBackground(QtGui.QColor(colour))
            selection.format.setProperty(QtGui.QTextFormat.FullWidthSelection, True)
            cursor = QtGui.QTextCursor(block)
            cursor.clearSelection()
            selection.cursor = cursor
            selections.append(selection)
        view.setExtraSelections(selections)

    def render_side_by_side(self):
        if self.options()["mode"] == "bytes":
            report = "\n".join(self._byte_report or [])
            self.left_view.setPlainText(report)
            self.right_view.setPlainText("")
            return
        self._backgrounds(self.left_view, self._rows, "left")
        self._backgrounds(self.right_view, self._rows, "right")

    def render_unified(self):
        left_lines = [row["left"] for row in self._rows if row.get("tag") != "insert"]
        right_lines = [row["right"] for row in self._rows if row.get("tag") != "delete"]
        diff = "\n".join(difflib.unified_diff(
            left_lines, right_lines,
            fromfile=self._left.get("name", "left"),
            tofile=self._right.get("name", "right"), lineterm="",
        ))
        self.unified_view.setPlainText(diff or self.t("diff_identical"))
        self._paint_unified()

    def _paint_unified(self):
        selections = []
        document = self.unified_view.document()
        for index in range(document.blockCount()):
            block = document.findBlockByNumber(index)
            text = block.text()
            colour = None
            if text.startswith(("+++", "---")):
                colour = "#8ab4f8"
            elif text.startswith("+"):
                colour = "#9be9a8"
            elif text.startswith("-"):
                colour = "#ff9a9a"
            elif text.startswith("@@"):
                colour = "#c586c0"
            if not colour:
                continue
            selection = QtWidgets.QTextEdit.ExtraSelection()
            selection.format.setForeground(QtGui.QColor(colour))
            cursor = QtGui.QTextCursor(block)
            cursor.select(QtGui.QTextCursor.LineUnderCursor)
            selection.cursor = cursor
            selections.append(selection)
        self.unified_view.setExtraSelections(selections)
    def render_inline(self):
        """Show each change with word-level highlights inside the line.

        Definition lines whose declared name changed get an extra marker so a
        rename is obvious at a glance (structural change highlight).
        """
        cursor = QtGui.QTextCursor(self.inline_view.document())
        self.inline_view.clear()
        cursor = self.inline_view.textCursor()
        default = QtGui.QTextCharFormat()
        default.setForeground(QtGui.QColor("#dfe7f5"))
        add_fmt = QtGui.QTextCharFormat()
        add_fmt.setBackground(QtGui.QColor("#17331f"))
        add_fmt.setForeground(QtGui.QColor("#9be9a8"))
        del_fmt = QtGui.QTextCharFormat()
        del_fmt.setBackground(QtGui.QColor("#3a1d1d"))
        del_fmt.setForeground(QtGui.QColor("#ff9a9a"))
        word_fmt = QtGui.QTextCharFormat()
        word_fmt.setForeground(QtGui.QColor("#ffe08a"))
        word_fmt.setFontWeight(QFont.Bold)
        plain = QtGui.QTextCharFormat()
        plain.setForeground(QtGui.QColor("#dfe7f5"))
        for row in self._rows:
            tag = row.get("tag")
            if tag == "equal":
                cursor.insertText(f"  {row['left']}\n", plain)
            elif tag == "delete":
                cursor.insertText(f"- {row['left']}\n", del_fmt)
            elif tag == "insert":
                cursor.insertText(f"+ {row['right']}\n", add_fmt)
            else:
                marker = " (renamed)" if _structural_name_change(
                    row.get("left"), row.get("right")) else ""
                cursor.insertText("- ", del_fmt)
                for text, changed in (row.get("left_segments") or [(row["left"], False)]):
                    cursor.insertText(text, word_fmt if changed else del_fmt)
                cursor.insertText(marker + "\n")
                cursor.insertText("+ ", add_fmt)
                for text, changed in (row.get("right_segments") or [(row["right"], False)]):
                    cursor.insertText(text, word_fmt if changed else add_fmt)
                cursor.insertText(marker + "\n")
        self.inline_view.setTextCursor(cursor)
        # The inline body was just rebuilt, so the search highlight painted on
        # the previous text has to follow it.
        self._apply_search_highlight()

    def render_blocks(self):
        """Group the diff into changed blocks separated by unchanged sections."""
        self.block_tree.clear()
        block_index = 0
        current = None
        for row in self._rows:
            if row.get("tag") == "equal":
                if current is not None:
                    self._add_block_group(block_index, current)
                    block_index += 1
                    current = None
                continue
            if current is None:
                current = []
            current.append(row)
        if current is not None:
            self._add_block_group(block_index, current)
        self.block_tree.expandAll()

    def _add_block_group(self, index, rows):
        added = sum(1 for row in rows if row.get("tag") == "insert")
        removed = sum(1 for row in rows if row.get("tag") == "delete")
        changed = sum(1 for row in rows if row.get("tag") == "replace")
        first = rows[0]
        item = QtWidgets.QTreeWidgetItem([
            f"Block {index + 1}  (+{added} / -{removed} / ~{changed})",
            f"line {first.get('left_no') or first.get('right_no') or '?'}",
        ])
        item.setForeground(0, QtGui.QColor("#f7c66d"))
        for row in rows:
            preview = row.get("left") or row.get("right") or ""
            child = QtWidgets.QTreeWidgetItem([
                row.get("tag", ""), preview[:120],
            ])
            colours = {"insert": "#9be9a8", "delete": "#ff9a9a", "replace": "#f7c66d"}
            child.setForeground(0, QtGui.QColor(colours.get(row.get("tag"), "#dfe7f5")))
            item.addChild(child)
        self.block_tree.addTopLevelItem(item)
    def render_ast(self):
        """Show the diff grouped by top-level symbol (function / class)."""
        self.ast_tree.clear()
        language = self._left.get("category", "")
        left_code = self._left.get("code", "") or ""
        right_code = self._right.get("code", "") or ""
        left_symbols = {name: (kind, left_code[a:b])
                        for name, kind, a, b in ast_symbols(left_code, language)}
        right_symbols = {name: (kind, right_code[a:b])
                         for name, kind, a, b in ast_symbols(right_code, language)}
        names = list(dict.fromkeys(list(left_symbols) + list(right_symbols)))
        for name in names:
            if name in left_symbols and name in right_symbols:
                status = "same" if left_symbols[name][1] == right_symbols[name][1] else "changed"
                kind = right_symbols[name][0]
            elif name in left_symbols:
                status, kind = "removed", left_symbols[name][0]
            else:
                status, kind = "added", right_symbols[name][0]
            item = QtWidgets.QTreeWidgetItem([name, kind, status])
            colours = {"same": "#9be9a8", "changed": "#f7c66d",
                       "added": "#9be9a8", "removed": "#ff9a9a"}
            item.setForeground(2, QtGui.QColor(colours.get(status, "#dfe7f5")))
            if status == "changed":
                detail = QtWidgets.QTreeWidgetItem([
                    "diff", "", f"{len(left_symbols[name][1].splitlines())} -> "
                                f"{len(right_symbols[name][1].splitlines())} lines",
                ])
                item.addChild(detail)
            self.ast_tree.addTopLevelItem(item)
        self.ast_tree.expandAll()

    def render_analysis(self):
        """Language, AST-level diff, refactorings and duplicate blocks.

        Everything here is derived from the two code bodies, so the panel
        keeps working for file drops, snapshots and script revisions alike.
        """
        self.analysis_tree.clear()
        left_code = self._left.get("code", "") or ""
        right_code = self._right.get("code", "") or ""
        left_name = self._left.get("name", "")
        right_name = self._right.get("name", "")

        left_kind, left_label = detect_language_kind(left_code, left_name,
                                                     self._left.get("category", ""))
        _right_kind, right_label = detect_language_kind(right_code, right_name,
                                                        self._right.get("category", ""))

        root = QtWidgets.QTreeWidgetItem([
            "Language",
            f"{left_label}  ->  {right_label}" if left_label != right_label
            else left_label,
        ])
        root.setForeground(0, QtGui.QColor("#8ab4f8"))
        self.analysis_tree.addTopLevelItem(root)

        report = compare_code_ast(left_code, right_code, left_name, right_name)
        sections = [
            ("Imports added", report["imports_added"], "#9be9a8"),
            ("Imports removed", report["imports_removed"], "#ff9a9a"),
            ("Classes added", report["classes_added"], "#9be9a8"),
            ("Classes removed", report["classes_removed"], "#ff9a9a"),
            ("Functions added", report["functions_added"], "#9be9a8"),
            ("Functions removed", report["functions_removed"], "#ff9a9a"),
            ("Functions changed", report["functions_changed"], "#f7c66d"),
        ]
        for title, values, colour in sections:
            node = QtWidgets.QTreeWidgetItem([title, str(len(values))])
            node.setForeground(1, QtGui.QColor(colour if values else "#6a737d"))
            if values:
                node.setForeground(0, QtGui.QColor(colour))
                for value in values:
                    node.addChild(QtWidgets.QTreeWidgetItem(["", str(value)]))
            self.analysis_tree.addTopLevelItem(node)

        # Parameter changes are shown as before -> after for each function.
        if report["parameter_deltas"]:
            node = QtWidgets.QTreeWidgetItem(["Parameters changed",
                                              str(len(report["parameter_deltas"]))])
            node.setForeground(0, QtGui.QColor("#f7c66d"))
            for name, before, after in report["parameter_deltas"]:
                child = QtWidgets.QTreeWidgetItem([
                    name, f"({', '.join(before)})  ->  ({', '.join(after)})"])
                child.setForeground(1, QtGui.QColor("#f7c66d"))
                node.addChild(child)
            self.analysis_tree.addTopLevelItem(node)

        # Refactorings: rename / move / extract.
        refactors = detect_refactorings(left_code, right_code, left_name, right_name)
        node = QtWidgets.QTreeWidgetItem(["Refactorings", str(len(refactors))])
        node.setForeground(0, QtGui.QColor("#c586c0"))
        for event in refactors:
            child = QtWidgets.QTreeWidgetItem([event["kind"], event["detail"]])
            child.setForeground(0, QtGui.QColor("#c586c0"))
            node.addChild(child)
        if not refactors:
            placeholder = QtWidgets.QTreeWidgetItem(["", "none detected"])
            placeholder.setForeground(1, QtGui.QColor("#6a737d"))
            node.addChild(placeholder)
        self.analysis_tree.addTopLevelItem(node)

        # Duplicated / moved blocks.
        duplicates = detect_duplicate_blocks(left_code, right_code)
        node = QtWidgets.QTreeWidgetItem(["Duplicate blocks", str(len(duplicates))])
        node.setForeground(0, QtGui.QColor("#f7c66d"))
        for block, locations in duplicates:
            where = ", ".join(f"{side}:{line}" for side, line in locations)
            first_line = block.splitlines()[0] if block else ""
            child = QtWidgets.QTreeWidgetItem([where, first_line[:80]])
            child.setForeground(0, QtGui.QColor("#f7c66d"))
            for text in block.splitlines():
                child.addChild(QtWidgets.QTreeWidgetItem(["", text[:120]]))
            node.addChild(child)
        if not duplicates:
            placeholder = QtWidgets.QTreeWidgetItem(["", "none detected"])
            placeholder.setForeground(1, QtGui.QColor("#6a737d"))
            node.addChild(placeholder)
        self.analysis_tree.addTopLevelItem(node)

        self.analysis_tree.expandAll()

    def render_files(self):
        """Directory comparison view (filled by open_directories)."""
        if not getattr(self, "_dir_rows", None):
            self.dir_tree.clear()
            return
        self.dir_tree.clear()
        colours = {"same": "#9be9a8", "different": "#f7c66d",
                   "only-left": "#ff9a9a", "only-right": "#8ab4f8", "error": "#ff7b72"}
        for row in self._dir_rows:
            item = QtWidgets.QTreeWidgetItem([
                row["path"], row["status"],
                f"{'L' if row['left'] else '-'} / {'R' if row['right'] else '-'}",
            ])
            item.setForeground(1, QtGui.QColor(colours.get(row["status"], "#dfe7f5")))
            item.setData(0, QtCore.Qt.UserRole, row)
            self.dir_tree.addTopLevelItem(item)

    def _open_dir_entry(self, item, _column):
        row = item.data(0, QtCore.Qt.UserRole)
        if not row:
            return
        if row.get("left"):
            self._left = self._read_source(row["left"], "left")
        if row.get("right"):
            self._right = self._read_source(row["right"], "right")
        self.view_buttons["side"].setChecked(True)
        self.recompute()
    # ----------------------------------------------------------- file input
    def _read_source(self, path, side_name):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                code = handle.read()
        except OSError as exc:
            QtWidgets.QMessageBox.warning(self, self.t("warning"), str(exc))
            return self._blank_source(side_name)
        return {
            "name": os.path.basename(path),
            "path": path,
            "code": code,
            "category": category_for_extension(path) or guess_category(code) or "Other",
        }

    def open_file(self, side):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, self.t("diff_open_file"), "", supported_extensions_filter())
        if not path:
            return
        source = self._read_source(path, side)
        if side == "left":
            self._left = source
        else:
            self._right = source
        self.recompute()

    def open_directories(self):
        left_dir = QtWidgets.QFileDialog.getExistingDirectory(self, self.t("diff_left_dir"))
        if not left_dir:
            return
        right_dir = QtWidgets.QFileDialog.getExistingDirectory(self, self.t("diff_right_dir"))
        if not right_dir:
            return
        self._dir_rows = compare_directories(left_dir, right_dir)
        self.view_buttons["files"].setChecked(True)
        self.render_files()
        self.status.setText(f"{left_dir} <->  {right_dir}")

    def load_from_stdin(self):
        """Read the current stdin content (piped) and compare it with the left."""
        try:
            text = sys.stdin.read()
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, self.t("warning"), str(exc))
            return
        if not text:
            QtWidgets.QMessageBox.information(
                self, self.t("info"), self.t("diff_empty_stdin"))
            return
        self._right = {"name": "stdin", "path": "", "code": text, "category": "Other"}
        self.recompute()

    def load_snapshot(self):
        """Copy the currently selected script on the Scripts tab to the right."""
        script = self.window.selected_script()
        if script is None:
            QtWidgets.QMessageBox.information(
                self, self.t("info"), self.t("select_script"))
            return
        self._right = self._entry_to_source(script)
        self.recompute()

    def pick_left(self, index):
        data = self.left_picker.itemData(index)
        entry = self._source_for_data(data)
        if entry is not None:
            self._left = self._entry_to_source(entry)
            self.recompute()

    def pick_right(self, index):
        data = self.right_picker.itemData(index)
        entry = self._source_for_data(data)
        if entry is not None:
            self._right = self._entry_to_source(entry)
            self.recompute()

    def swap_sides(self):
        self._left, self._right = self._right, self._left
        self.recompute()

    def push_recent(self, label):
        """Remember the current comparison so it can be re-run later."""
        entry = (label, self._left, self._right)
        self._recent = [item for item in self._recent if item[0] != label]
        self._recent.insert(0, entry)
        del self._recent[15:]
        self.recent_combo.blockSignals(True)
        self.recent_combo.clear()
        self.recent_combo.addItem(self.t("diff_recent"))
        for name, _l, _r in self._recent:
            self.recent_combo.addItem(name)
        self.recent_combo.blockSignals(False)

    def load_recent(self, index):
        if index <= 0 or index - 1 >= len(self._recent):
            return
        _label, left, right = self._recent[index - 1]
        self._left, self._right = left, right
        self.recompute()
    # ---------------------------------------------------------- drag & drop
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        paths = [p for p in paths if p]
        if not paths:
            return
        if len(paths) == 1 and os.path.isdir(paths[0]):
            self.status.setText(self.t("diff_need_two_files"))
            return
        if len(paths) == 1:
            # A single file drops onto the right side, keeping the left intact.
            self._right = self._read_source(paths[0], "right")
        else:
            self._left = self._read_source(paths[0], "left")
            self._right = self._read_source(paths[1], "right")
        self.push_recent(f"{self._left['name']}  vs  {self._right['name']}")
        self.recompute()
        event.acceptProposedAction()

    # ---------------------------------------------------------- navigation
    def goto_change(self, direction):
        if not self._diff_positions:
            return
        self._current_pos = (self._current_pos + direction) % len(self._diff_positions)
        row_index = self._diff_positions[self._current_pos]
        if self.current_view() == "unified":
            self._scroll_view(self.unified_view, row_index)
        elif self.current_view() == "inline":
            self._scroll_view(self.inline_view, row_index)
        else:
            self._scroll_view(self.left_view, row_index)
            self._scroll_view(self.right_view, row_index)
        self.minimap.set_view_row(row_index)

    @staticmethod
    def _scroll_view(view, row_index):
        cursor = QtGui.QTextCursor(view.document())
        cursor.movePosition(QtGui.QTextCursor.Start)
        cursor.movePosition(QtGui.QTextCursor.Down, QtGui.QTextCursor.MoveAnchor,
                            max(0, row_index))
        view.setTextCursor(cursor)
        view.ensureCursorVisible()

    def scroll_to_row(self, row_index):
        self._scroll_view(self.left_view, row_index)
        self._scroll_view(self.right_view, row_index)

    def _update_minimap_marker(self):
        self.minimap.set_view_row(self.left_view.textCursor().blockNumber())

    def _sync_scroll(self, source, follower, value):
        if self._syncing_scroll:
            return
        self._syncing_scroll = True
        try:
            follower.verticalScrollBar().setValue(value)
        finally:
            self._syncing_scroll = False

    def toggle_wrap(self, checked):
        mode = (QtWidgets.QPlainTextEdit.WidgetWidth if checked
                else QtWidgets.QPlainTextEdit.NoWrap)
        for view in (self.left_view, self.right_view, self.unified_view, self.inline_view):
            view.setLineWrapMode(mode)
        self.btn_wrap.setText("Wrap: on" if checked else "Wrap: off")

    # ------------------------------------------------------------- reporting
    def current_report(self):
        """Build a report dict for the current sides (used by every export)."""
        stats = None
        rows = getattr(self, "_rows", [])
        report = {
            "left": self._left,
            "right": self._right,
            "rows": rows,
            "blocks": iter_change_blocks(rows),
            "analysis": compare_code_ast(self._left.get("code", "") or "",
                                         self._right.get("code", "") or "",
                                         self._left.get("name", ""),
                                         self._right.get("name", "")),
            "refactors": detect_refactorings(self._left.get("code", "") or "",
                                             self._right.get("code", "") or ""),
            "duplicates": detect_duplicate_blocks(self._left.get("code", "") or "",
                                                  self._right.get("code", "") or ""),
        }
        report["statistics"] = stats or diff_statistics(report)
        return report

    def show_statistics(self):
        """Show the statistics window (counts, percent, histogram, per symbol)."""
        report = self.current_report()
        text = format_statistics(report["statistics"])
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle(self.t("diff_stats_title", "Diff statistics"))
        dialog.resize(520, 480)
        layout = QtWidgets.QVBoxLayout(dialog)
        view = QtWidgets.QPlainTextEdit(text)
        view.setReadOnly(True)
        view.setFont(QFont("Consolas", 10))
        layout.addWidget(view)
        close_btn = QtWidgets.QPushButton("Close")
        close_btn.clicked.connect(dialog.close)
        footer = QtWidgets.QHBoxLayout()
        footer.addStretch()
        footer.addWidget(close_btn)
        layout.addLayout(footer)
        dialog.exec_()

    def show_export_menu(self):
        """Drop the export menu down from the Export... button."""
        self.export_menu.exec_(
            self.btn_export.mapToGlobal(
                QtCore.QPoint(0, self.btn_export.height())))

    def export_report(self, extension=".txt"):
        """Save the current comparison as .patch, .json or .txt."""
        report = self.current_report()
        default = self.t("diff_report_name", "diff_report")
        filters = {
            ".patch": "Patch files (*.patch)",
            ".json": "JSON files (*.json)",
            ".txt": "Text files (*.txt)",
        }
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, self.t("diff_export", "Export report"),
            default + extension, filters.get(extension, "All files (*.*)"))
        if not path:
            return
        try:
            write_report(report, path)
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, self.t("export_error"), str(exc))
            return
        self.window.show_message(self.t("info"),
                                 f"{self.t('exported_file')}{chr(10)}{path}")

    def export_diagnostics(self):
        """Save the diagnostic artefacts (JSON) for other modules."""
        report = self.current_report()
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, self.t("diff_diagnostics", "Diagnostics"),
            self.t("diff_report_name", "diff_report") + "_diag.json",
            "JSON files (*.json)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(diagnostic_artifacts(report), handle,
                          ensure_ascii=False, indent=2)
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, self.t("export_error"), str(exc))
            return
        self.window.show_message(self.t("info"),
                                 f"{self.t('exported_file')}{chr(10)}{path}")

    # -------------------------------------------------------------- sessions
    def _ensure_session_manager(self):
        if not hasattr(self, "session_manager"):
            self.session_manager = SessionManager()
            combo = self.session_combo
            combo.blockSignals(True)
            combo.clear()
            combo.blockSignals(False)
        return self.session_manager

    def new_session(self, title="", activate=True):
        """Open a fresh diff session (keeps the current one in the list)."""
        manager = self._ensure_session_manager()
        session = manager.new_session(title, self._left, self._right)
        self._refresh_session_combo()
        if activate:
            self.session_combo.setCurrentIndex(self.session_combo.count() - 1)
        return session

    def _refresh_session_combo(self):
        manager = self._ensure_session_manager()
        self.session_combo.blockSignals(True)
        self.session_combo.clear()
        for session in manager.sessions.values():
            self.session_combo.addItem(session.title, session.id)
        index = self.session_combo.findData(manager.active_id)
        if index >= 0:
            self.session_combo.setCurrentIndex(index)
        self.session_combo.blockSignals(False)

    def switch_session(self, index):
        if index < 0:
            return
        session_id = self.session_combo.itemData(index)
        manager = self._ensure_session_manager()
        session = manager.activate(session_id)
        if session is None:
            return
        self._left, self._right = session.left, session.right
        self.recompute()

    def close_session(self):
        manager = self._ensure_session_manager()
        if manager.active_id is None:
            return
        manager.remove(manager.active_id)
        self._refresh_session_combo()
        active = manager.active()
        if active is not None:
            self._left, self._right = active.left, active.right
            self.recompute()

    def snapshot_session(self):
        """Store the current sides back into the active session."""
        manager = self._ensure_session_manager()
        session = manager.active()
        if session is not None:
            session.left, session.right = self._left, self._right

    # ------------------------------------------------------------- embedding
    def dock_as_panel(self, container, side="right", width=520):
        """Embed this diff view in a host widget as a side panel.

        Returns the container so a terminal front-end can keep a reference.
        ``side`` is "left" or "right" to choose which edge the panel sits on.
        The widget is detached from any previous parent first, so a front-end
        can switch between the panel/top/tab layouts at run time.
        """
        self.setParent(None)
        self.setParent(container)
        container.setMinimumWidth(width)
        self.show()
        self._embed_side = side
        return container

    def dock_as_top(self, container, height=260):
        """Embed this diff view as a top panel of a host widget."""
        self.setParent(None)
        self.setParent(container)
        container.setMinimumHeight(height)
        self.show()
        self._embed_side = "top"
        return container

    def embed_into_tab(self, tab_widget, title="Diff"):
        """Embed this diff view as a tab of an existing QTabWidget."""
        if tab_widget.indexOf(self) < 0:
            tab_widget.addTab(self, title)
        tab_widget.setCurrentWidget(self)
        self._embed_side = "tab"
        return tab_widget

    def open_paths(self, left_path, right_path):
        """Load two files straight from disk (used by the CLI entry point)."""
        self._left = self._read_source(left_path, "left")
        self._right = self._read_source(right_path, "right")
        self.push_recent(f"{self._left['name']}  vs  {self._right['name']}")
        self.recompute()

    def compare_entry(self, left_entry, right_entry):
        """Entry point used by the Scripts tab ("Compare with...")."""
        self._left = self._entry_to_source(left_entry)
        self._right = self._entry_to_source(right_entry)
        self.push_recent(f"{self._left['name']}  vs  {self._right['name']}")
        self.recompute()

# ===========================================================================
# Terminal integration: module API, hooks, sessions and the CLI entry point.
# ===========================================================================


class DiffHooks:
    """A tiny pub/sub bus so plugins can observe the diff lifecycle.

    Supported events:
      - before_diff : fired right before two sides are compared.
      - after_diff  : fired once the rows are ready.
      - change_log  : fired for every detected change block (audit log).
    """

    def __init__(self):
        self._hooks = {"before_diff": [], "after_diff": [], "change_log": []}

    def register(self, event, callback):
        """Subscribe `callback` to `event`; returns the callback for chaining."""
        if event not in self._hooks:
            self._hooks[event] = []
        self._hooks[event].append(callback)
        return callback

    def unregister(self, event, callback):
        if event in self._hooks and callback in self._hooks[event]:
            self._hooks[event].remove(callback)

    def fire(self, event, **payload):
        """Run every callback registered for `event`, ignoring their errors."""
        for callback in list(self._hooks.get(event, [])):
            try:
                callback(**payload)
            except Exception:
                traceback.print_exc()


# The process-wide bus. A plugin imports this module and calls
# HOOKS.register("after_diff", fn) to be notified after every comparison.
HOOKS = DiffHooks()

# Registry of callables exposed to plugins ("plugin-ready" API surface).
API_REGISTRY = {}


def register_api(name, func=None):
    """Expose func under name in the plugin API registry.

    Usable as register_api("name")(fn) and also directly as
    register_api("name", fn); the one-argument form is kept so existing
    plugin code keeps working.
    """
    if func is None:
        def decorator(real):
            API_REGISTRY[name] = real
            return real
        return decorator
    API_REGISTRY[name] = func
    return func


def read_text_source(path, side_name="file"):
    """Read a file into a diff source dict (used by the CLI and plugins)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            code = handle.read()
    except IOError as e:
        raise IOError(f"Failed to read {side_name} file '{path}': {e}")
    return {
        "name": os.path.basename(path),
        "path": path,
        "code": code,
        "category": category_for_extension(path) or guess_category(code) or "Other",
    }


def compare_files(left_path, right_path, algorithm="myers", mode="text", **flags):
    """Head-less comparison used by the CLI, plugins and tests.

    Fires the before_diff/after_diff/change_log hooks so a plugin sees exactly
    the same lifecycle whether the GUI or a script drives the comparison.
    """
    left = read_text_source(left_path, "left")
    right = read_text_source(right_path, "right")
    HOOKS.fire("before_diff", left=left, right=right, options=flags)

    if mode == "bytes":
        rows = []
    else:
        language = left.get("category", "")
        left_lines = prepare_diff_lines(
            left["code"], flags.get("ignore_case", False), flags.get("ignore_ws", False),
            flags.get("ignore_blank", False), flags.get("ignore_comments", False), language)
        right_lines = prepare_diff_lines(
            right["code"], flags.get("ignore_case", False), flags.get("ignore_ws", False),
            flags.get("ignore_blank", False), flags.get("ignore_comments", False), language)
        rows = align_lines(left_lines, right_lines, algorithm)

    report = {
        "left": left,
        "right": right,
        "rows": rows,
        "blocks": iter_change_blocks(rows),
        "analysis": compare_code_ast(left["code"], right["code"], left["name"], right["name"]),
        "refactors": detect_refactorings(left["code"], right["code"]),
        "duplicates": detect_duplicate_blocks(left["code"], right["code"]),
    }
    for start, block in report["blocks"]:
        HOOKS.fire("change_log", start=start, rows=block)
    HOOKS.fire("after_diff", report=report)
    return report


@register_api("compare_files")
def _api_compare_files(*args, **kwargs):
    return compare_files(*args, **kwargs)


@register_api("diff_tab_class")
def _api_diff_tab_class():
    return DiffTab


@register_api("hooks")
def _api_hooks():
    return HOOKS


@register_api("sessions")
def _api_sessions():
    return SessionManager


@register_api("compare_text")
def compare_text(left_text, right_text, algorithm="myers"):
    """Compare two in-memory strings and return the diff report."""
    left = {"name": "left", "path": "", "code": left_text, "category": "Other"}
    right = {"name": "right", "path": "", "code": right_text, "category": "Other"}
    HOOKS.fire("before_diff", left=left, right=right, options={})
    rows = align_lines(left_text.splitlines(), right_text.splitlines(), algorithm)
    report = {"left": left, "right": right, "rows": rows,
              "blocks": iter_change_blocks(rows)}
    HOOKS.fire("after_diff", report=report)
    return report


def format_report(report, style="text"):
    """Render a diff report as plain text, unified or JSON (for the CLI)."""
    if style == "json":
        return json.dumps({
            "left": report["left"]["name"],
            "right": report["right"]["name"],
            "rows": [{"tag": r["tag"], "left_no": r["left_no"], "right_no": r["right_no"],
                      "left": r["left"], "right": r["right"]} for r in report["rows"]],
            "analysis": report.get("analysis", {}),
        }, ensure_ascii=False, indent=2)
    if style == "unified":
        left_lines = [r["left"] for r in report["rows"] if r["tag"] != "insert"]
        right_lines = [r["right"] for r in report["rows"] if r["tag"] != "delete"]
        return "\n".join(difflib.unified_diff(
            left_lines, right_lines,
            fromfile=report["left"]["name"], tofile=report["right"]["name"], lineterm=""))
    marker = {"insert": "+", "delete": "-", "replace": "~", "equal": " "}
    return chr(10).join(f"{marker.get(r['tag'], ' ')} {r['left'] or r['right']}"
                      for r in report["rows"])


# ---------------------------------------------------------------------------
# Reports and export: statistics, .patch / JSON / TXT writers and diagnostic
# artefacts for other modules.
# ---------------------------------------------------------------------------


def _symbol_for_line(symbols, line_number, code=""):
    """Name of the symbol owning a 1-based line, or "(top level)".

    `ast_symbols` reports character offsets, so the line number is converted
    into a character offset before it is matched against the ranges.
    """
    lines = (code or "").splitlines()
    if not lines:
        return "(top level)"
    index = max(0, min(len(lines) - 1, line_number - 1))
    offset = sum(len(line) + 1 for line in lines[:index])
    for name, _kind, start, end in symbols:
        if start <= offset < end:
            return name
    return "(top level)"


def diff_statistics(report):
    """Compute the numbers every report and histogram is built from."""
    rows = report["rows"]
    added = sum(1 for row in rows if row.get("tag") == "insert")
    removed = sum(1 for row in rows if row.get("tag") == "delete")
    changed = sum(1 for row in rows if row.get("tag") == "replace")
    same = sum(1 for row in rows if row.get("tag") == "equal")
    total = max(1, len(rows))
    changes = added + removed + changed
    left_code = report["left"].get("code", "") or ""
    right_code = report["right"].get("code", "") or ""
    left_lines = max(1, len(left_code.splitlines()))
    right_lines = max(1, len(right_code.splitlines()))

    buckets = {"1": 0, "2-3": 0, "4-10": 0, "11+": 0}
    for _start, rows_in_block in iter_change_blocks(rows):
        size = len(rows_in_block)
        if size == 1:
            buckets["1"] += 1
        elif size <= 3:
            buckets["2-3"] += 1
        elif size <= 10:
            buckets["4-10"] += 1
        else:
            buckets["11+"] += 1

    per_symbol = {}
    left_symbols = ast_symbols(left_code, report["left"].get("category", ""))
    right_symbols = ast_symbols(right_code, report["right"].get("category", ""))
    for row in rows:
        if row.get("tag") == "equal":
            continue
        name = "(top level)"
        if row.get("left_no") is not None:
            name = _symbol_for_line(left_symbols, row["left_no"], left_code)
        elif row.get("right_no") is not None:
            name = _symbol_for_line(right_symbols, row["right_no"], right_code)
        per_symbol[name] = per_symbol.get(name, 0) + 1

    per_file = {}
    for side in ("left", "right"):
        name = report[side].get("name", side) or side
        lines = len((report[side].get("code", "") or "").splitlines())
        per_file[name] = {"lines": lines,
                          "changes": changes if side == "right" else removed}

    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "same": same,
        "total_lines": len(rows),
        "changes": changes,
        "percent_different": round(changes * 100.0 / total, 1),
        "left_lines": left_lines,
        "right_lines": right_lines,
        "histogram": buckets,
        "per_symbol": dict(sorted(per_symbol.items(), key=lambda kv: -kv[1])),
        "per_file": per_file,
    }


def format_statistics(stats):
    """Human readable statistics block shared by the TXT and report writers."""
    lines = [
        "Statistics",
        "----------",
        "Changes: {} (+{} -{} ~{})".format(
            stats["changes"], stats["added"], stats["removed"], stats["changed"]),
        "Unchanged lines: {} / {}".format(stats["same"], stats["total_lines"]),
        "Difference: {}%".format(stats["percent_different"]),
        "Lines: left {} -> right {}".format(stats["left_lines"], stats["right_lines"]),

        "Change histogram (block size -> count)",
    ]
    widest = max(1, max(stats["histogram"].values()))
    for label in ("1", "2-3", "4-10", "11+"):
        count = stats["histogram"][label]
        bar = "#" * int(round(count * 30.0 / widest)) if count else ""
        lines.append("  {:>5} | {} {}".format(label, bar, count))
    if stats["per_symbol"]:
        lines.extend(["", "Changes per function"])
        for name, count in stats["per_symbol"].items():
            lines.append("  {}: {}".format(name, count))
    lines.extend(["", "Changes per file"])
    for name, info in stats["per_file"].items():
        lines.append("  {}: {} change(s), {} line(s)".format(
            name, info["changes"], info["lines"]))
    return chr(10).join(lines)


def report_as_patch(report):
    """Render a unified diff suitable for `patch` / `git apply`."""
    left_lines = [r["left"] for r in report["rows"] if r.get("tag") != "insert"]
    right_lines = [r["right"] for r in report["rows"] if r.get("tag") != "delete"]
    left_name = report["left"].get("name", "left") or "left"
    right_name = report["right"].get("name", "right") or "right"
    # difflib already emits the ---/+++ header, so nothing is prepended here;
    # a real newline is kept as the last byte so the patch ends with a line
    # terminator the way patch(1) expects it.
    body = chr(10).join(difflib.unified_diff(
        left_lines, right_lines, fromfile=left_name, tofile=right_name, lineterm=""))
    return body + chr(10)


def report_as_txt(report, stats=None):
    """Plain ASCII report: header, statistics and the diff body."""
    stats = stats or diff_statistics(report)
    marker = {"insert": "+", "delete": "-", "replace": "~", "equal": " "}
    lines = [
        "ScriptDB diff report",
        "====================",
        "Left : " + report["left"].get("name", ""),
        "Right: " + report["right"].get("name", ""),
        "",
        format_statistics(stats),
        "",
        "Diff",
        "----",
    ]
    for row in report["rows"]:
        tag = row.get("tag")
        text = row.get("right") if tag == "insert" else row.get("left")
        lines.append(marker.get(tag, " ") + " " + (text or ""))
    return chr(10).join(lines) + chr(10)


def report_as_json(report, stats=None):
    """JSON with the diff, the statistics and the code analysis combined."""
    stats = stats or diff_statistics(report)
    return json.dumps({
        "left": {"name": report["left"].get("name", ""),
                 "path": report["left"].get("path", "")},
        "right": {"name": report["right"].get("name", ""),
                  "path": report["right"].get("path", "")},
        "statistics": stats,
        "analysis": report.get("analysis", {}),
        "refactors": report.get("refactors", []),
        "duplicates": [
            {"block": block, "locations": [list(loc) for loc in locations]}
            for block, locations in report.get("duplicates", [])
        ],
        "rows": [
            {"tag": row.get("tag"), "left_no": row.get("left_no"),
             "right_no": row.get("right_no"), "left": row.get("left"),
             "right": row.get("right")}
            for row in report["rows"]
        ],
    }, ensure_ascii=False, indent=2)


REPORT_WRITERS = {
    ".patch": report_as_patch,
    ".diff": report_as_patch,
    ".txt": report_as_txt,
    ".json": report_as_json,
}


def write_report(report, path):
    """Write a report to `path`, picking the writer from its extension."""
    extension = os.path.splitext(path)[1].lower() or ".txt"
    writer = REPORT_WRITERS.get(extension, report_as_txt)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(writer(report))
    return path


def diagnostic_artifacts(report):
    """Build machine readable artefacts for other modules (diagnostics)."""
    stats = diff_statistics(report)
    changes = [
        {"tag": row.get("tag"), "left_no": row.get("left_no"),
         "right_no": row.get("right_no"),
         "text": (row.get("right") if row.get("tag") == "insert" else row.get("left")) or ""}
        for row in report["rows"] if row.get("tag") != "equal"
    ]
    return {
        "version": 1,
        "generator": "ScriptDB diff",
        "summary": {
            "changes": stats["changes"],
            "added": stats["added"],
            "removed": stats["removed"],
            "changed": stats["changed"],
            "percent_different": stats["percent_different"],
        },
        "histogram": stats["histogram"],
        "per_file": stats["per_file"],
        "per_symbol": stats["per_symbol"],
        "analysis": report.get("analysis", {}),
        "refactors": report.get("refactors", []),
        "duplicates": len(report.get("duplicates", [])),
        "changes": changes,
        "status": "clean" if stats["changes"] == 0 else "modified",
    }


@register_api("statistics")
def _api_statistics(report):
    return diff_statistics(report)


@register_api("write_report")
def _api_write_report(report, path):
    return write_report(report, path)


@register_api("diagnostics")
def _api_diagnostics(report):
    return diagnostic_artifacts(report)


class DiffSession:
    """One diff comparison held as an independent, re-openable session."""

    _counter = [0]

    def __init__(self, title="", left=None, right=None):
        DiffSession._counter[0] += 1
        self.id = DiffSession._counter[0]
        self.title = title or f"Session {self.id}"
        self.left = left or {"name": "(left)", "path": "", "code": "", "category": "Other"}
        self.right = right or {"name": "(right)", "path": "", "code": "", "category": "Other"}
        self.options = {}

    def to_dict(self):
        return {"id": self.id, "title": self.title,
                "left": self.left, "right": self.right, "options": self.options}


class SessionManager:
    """Keeps several diff sessions alive at once and tracks the active one."""

    def __init__(self):
        self.sessions = collections.OrderedDict()
        self.active_id = None

    def add(self, session):
        self.sessions[session.id] = session
        self.active_id = session.id
        return session

    def new_session(self, title="", left=None, right=None):
        return self.add(DiffSession(title, left, right))

    def get(self, session_id):
        return self.sessions.get(session_id)

    def activate(self, session_id):
        if session_id in self.sessions:
            self.active_id = session_id
        return self.sessions.get(session_id)

    def remove(self, session_id):
        self.sessions.pop(session_id, None)
        if self.active_id == session_id:
            self.active_id = next(iter(self.sessions), None)

    def active(self):
        return self.sessions.get(self.active_id)

    def titles(self):
        return [session.title for session in self.sessions.values()]


def run_cli_report(args):
    """Handle the head-less CLI paths: report writing and text output.

    Returns an exit code. Runs without ever touching the GUI, so it is
    safe to call from scripts and tests.
    """
    if not args.left or not args.right:
        # Caller is responsible for printing help before deciding this.
        return 0

    if not os.path.exists(args.left):
        print(f"Error: left file does not exist: {args.left}", file=sys.stderr)
        return 1
    if not os.path.exists(args.right):
        print(f"Error: right file does not exist: {args.right}", file=sys.stderr)
        return 1

    flags = {
        "ignore_ws": args.ignore_ws,
        "ignore_case": args.ignore_case,
        "ignore_comments": args.ignore_comments,
        "ignore_blank": args.ignore_blank,
    }
    try:
        report = compare_files(args.left, args.right, args.algorithm, args.mode, **flags)
    except IOError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    # Report / diagnostics writers run head-less and then exit, so scripts can
    # generate artefacts without ever starting the GUI.
    wrote_artifact = False
    if args.report:
        report_ext = os.path.splitext(args.report)[1].lower()
        valid_extensions = [".patch", ".diff", ".txt", ".json"]
        if report_ext not in valid_extensions:
            print(f"Error: report file must have one of these extensions: {', '.join(valid_extensions)}", file=sys.stderr)
            return 1
        write_report(report, args.report)
        print("report written: " + args.report)
        wrote_artifact = True
    if args.diagnostics:
        with open(args.diagnostics, "w", encoding="utf-8") as handle:
            json.dump(diagnostic_artifacts(report), handle, ensure_ascii=False, indent=2)
        print("diagnostics written: " + args.diagnostics)
        wrote_artifact = True
    if args.stats:
        print(format_statistics(diff_statistics(report)))
        wrote_artifact = True
    if wrote_artifact:
        return 0

    style = "text" if args.no_gui and args.format == "gui" else args.format

    if style in ("text", "unified", "json"):
        print(format_report(report, style))
        return 0

    # Not handled here: the caller falls back to the GUI path.
    return None


def run_cli_gui(args):
    """Open the diff tab in the GUI with the two files pre-loaded.

    The manager is shut down in a finally block so a partially-initialised
    QApplication never leaks when open_paths or show() raises.
    """
    manager = ApplicationManager()
    try:
        app = manager.initialize_application()
        win = manager.initialize_window()
        win.diff_tab.open_paths(args.left, args.right)
        win.tabs.setCurrentWidget(win.diff_tab)
        win.show()
        if QtWidgets.QApplication.instance() is None:
            return app.exec_()
        return 0
    finally:
        manager.shutdown()


def run_cli(argv):
    """Entry point used both by the CLI and by tests (returns an exit code).

    `diffgui file1 file2` compares two files. Output format is selected with
    --format (text/unified/json); a graphical run is the default unless the
    caller asks for --no-gui or the platform has no display.
    """
    parser = argparse.ArgumentParser(
        prog="diffgui",
        description="ScriptDB diff viewer - compare two files from the terminal.")
    parser.add_argument("left", nargs="?", help="left file")
    parser.add_argument("right", nargs="?", help="right file")
    parser.add_argument("--algorithm", default="myers",
                        choices=["myers", "patience", "histogram"])
    parser.add_argument("--mode", default="text",
                        choices=["text", "bytes"],
                        help="comparison mode: text (default) or bytes")
    parser.add_argument("--format", default="gui",
                        choices=["gui", "text", "unified", "json"])
    parser.add_argument("--no-gui", action="store_true",
                        help="print the diff instead of opening the window")
    parser.add_argument("--ignore-ws", action="store_true")
    parser.add_argument("--ignore-case", action="store_true")
    parser.add_argument("--ignore-comments", action="store_true")
    parser.add_argument("--ignore-blank", action="store_true")
    parser.add_argument("--stats", action="store_true",
                        help="print only the diff statistics")
    parser.add_argument("--report", metavar="PATH",
                        help="write a report (.patch/.json/.txt) to PATH")
    parser.add_argument("--diagnostics", metavar="PATH",
                        help="write diagnostic artefacts (JSON) to PATH")
    args = parser.parse_args(argv)

    if not args.left or not args.right:
        parser.print_help()
        return 0

    result = run_cli_report(args)
    if result is not None:
        return result

    # GUI mode: open the window with the two files pre-loaded.
    return run_cli_gui(args)


def shutdown_application(window=None):
    """Tear the GUI down in a safe order before the interpreter exits.

    If the window is still alive when `main()` returns, Qt destroys the whole
    widget tree during interpreter finalisation and PyQt5 crashes there with
    an access violation (no traceback, only a non-zero exit code). Hiding the
    window, deleting it through the event loop and flushing the pending
    events before the process ends avoids that.

    This function now delegates to ApplicationManager for consistency.
    """
    ApplicationManager().shutdown()


def main(argv=None):
    """Program entry point.

    With no arguments it opens the full GUI; with two file arguments it
    behaves like the `diffgui file1 file2` command, so the same script can
    be driven from a shell or embedded in another application.

    Returns an exit code instead of exiting directly, so tests (and any
    other caller) can inspect the result. The `__main__` block performs
    the actual sys.exit.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv:
        return run_cli(argv)

    manager = ApplicationManager()
    try:
        app = manager.initialize_application()
        window = manager.initialize_window()
        # Shutdown must run even when show() or anything between here and
        # exec_() raises, otherwise a half-built window is left behind and
        # the process dies with the PyQt5 access violation on exit.
        window.show()
        exit_code = app.exec_()
    finally:
        shutdown_application()
    return exit_code


class ApplicationManager:
    """Singleton manager for QApplication and main window instances.

    This singleton pattern ensures the QApplication and window instances
    survive for the entire program lifetime, avoiding PyQt5 crash-on-exit
    issues while providing better encapsulation than raw module globals.
    """
    _instance = None
    _app = None            # type: Optional[QtWidgets.QApplication]
    _main_window = None    # type: Optional["ScriptDatabaseWindow"]

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    @property
    def app(self):
        """Get the QApplication instance."""
        return self._app

    @property
    def main_window(self):
        """Get the main window instance."""
        return self._main_window

    def initialize_application(self, argv=None):
        """Initialize QApplication instance if not already created."""
        if self._app is None:
            self._app = QtWidgets.QApplication(argv or sys.argv)
            self._app.setStyle("Fusion")
            apply_application_theme(self._app)
            self._app.setWindowIcon(load_app_icon())
            install_wheel_scrolling(self._app)
        return self._app

    def initialize_window(self):
        """Initialize main window instance if not already created."""
        if self._main_window is None:
            self._main_window = ScriptDatabaseWindow()
        return self._main_window

    def shutdown(self):
        """Safely shut down the application and window.

        Errors are logged with a traceback and re-raised so a teardown
        crash is diagnosable instead of silently swallowed. Both class
        attributes are cleared in the finally block so a clean state is
        guaranteed even when shutdown raises.
        """
        try:
            if ApplicationManager._main_window is not None:
                self._main_window.hide()
                self._main_window.deleteLater()
            if ApplicationManager._app is not None:
                self._app.quit()
                self._app.processEvents()
        except Exception:
            traceback.print_exc()
            raise
        finally:
            ApplicationManager._main_window = None
            ApplicationManager._app = None

    def reset(self):
        """Reset all instances (useful for testing).

        shutdown() already clears _main_window and _app in its finally
        block, so only the singleton instance itself needs to be dropped
        here; the redundant lines are gone and state is guaranteed clean
        even if shutdown raised.
        """
        try:
            self.shutdown()
        finally:
            # Drop the singleton last, so a failed shutdown still produces a
            # truly fresh instance on the next reset().
            ApplicationManager._instance = None


if __name__ == "__main__":
    sys.exit(main())