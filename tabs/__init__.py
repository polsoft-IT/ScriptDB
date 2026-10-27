# -*- coding: utf-8 -*-
"""Per-tab modules for ScriptDB.

Each tab of the main window lives in its own file here so that sdb_scripts.py
stays small. A tab module exposes a ``<Name>Tab`` widget class that takes the
main window (as ``window``) and builds its own UI in ``__init__``.

Modules:
    analyzer.py       ->  ScriptAnalyzerTab
    audit.py          ->  AuditTab
    todo_tab.py       ->  TodoTab (calendar, tasks and reminders) and the
                          TodoWindow that hosts it as a popup, plus
                          todo_core.py (the holiday/task engine) and
                          todo_notify.py (the popups shared with the window)
    debugger.py       ->  DebuggerTab (the offline debugging workspace)
    sandbox.py        ->  SandboxTab (the isolated script execution environment)
    docs_tab.py       ->  DocsTab (the document repository, "DOCs")
    editor.py         ->  EditorTab
    reports_tab.py    ->  ReportsTab (the report artifact repository),
                         plus reports_analysis.py (report analysis, cross-module
                         integration and report operations) and reports_premium
                         .py (automatic generation, the scheduler, comparative /
                         env-diff / security reports)
    search_tab.py     ->  SearchTab
    search_engine.py  ->  the search engine (16 search kinds)
    metadata_tab.py   ->  MetadataTab (the header metadata of the scripts),
                          plus metadata_core.py (scan, header read/write,
                          validation, bulk edit, audit and export)
    nav_engine.py     ->  the editor's code navigation (10 jump kinds)
    templates.py      ->  the editor's template engine (9 kinds of code block)
    code_generator.py ->  the editor's code generator (6 artifact kinds, built
                          from configs, metadata, JSON/YAML, API, module and
                          project definitions)
    code_rules.py     ->  the editor's code rules engine (10 automatic lint +
                          fix rules, no AI: imports, formatting, indentation,
                          return, break, except, docstrings, types, config
                          sections, style)
    vault_core.py     ->  the vault's security core and encrypted store
    vault_tab.py      ->  VaultTab (the encrypted, isolated private safe)
    secure_core.py    ->  the script-security core (digests, HMAC, KDF, sealing,
                          signatures, the integrity registry, backups,
                          versioning and the six reports)
    secure_tab.py     ->  SecurityTab (the script-securing workspace)
    structure_core.py ->  the project-structure engine (config parser, generator,
                          template engine, validator, compiler/pipeline, preset
                          repository, plugin registry, git/CI/IDE emitters,
                          reverse analysis, migrations and the reports)
    structure_tab.py  ->  StructureTab (the project-structure workspace)
    sqlite_offline.py ->  the offline Data Layer (databases in files)
    sqlite_online.py  ->  the online Data Layer (a database behind an API)
    sqlite_sync.py    ->  the Sync Layer that joins the two
    sqlite_tab.py     ->  SqliteTab (the database workbench)
    syntax/           ->  one hot-pluggable syntax module per language
"""""

from .analyzer import ScriptAnalyzerTab
from .audit import AuditTab
from .debugger import DebuggerTab
from .sandbox import SandboxTab
from .docs_tab import DocsTab
from .editor import EditorTab
from .reports_tab import ReportsTab
from .search_tab import SearchTab
# The Metadata tab edits the header metadata of the scripts themselves; the
# engine behind it (scan, validate, header read/write, bulk edit, export) is in
# tabs/metadata_core.py, so the tab is only its interface.
from .metadata_tab import MetadataTab
# The Vault is the encrypted, isolated safe: it reuses the DOCs crypto and
# repository (tabs/vault_core.py) instead of duplicating them.
from .vault_tab import VaultTab
# The Security tab hardens the scripts themselves: hashing, HMAC, KDF, sealing,
# signatures, the integrity registry and the reports all live in
# tabs/secure_core.py, so the tab is only their interface.
from .secure_tab import SecurityTab
# The Structure tab builds file trees: the preset parser, the generator, the
# template engine, the validator, the pipeline and the integrations all live in
# tabs/structure_core.py, so the tab is only their interface.
from .structure_tab import StructureTab
# The SQLite workbench: two data layers (offline files, online API) plus the
# sync layer that joins them (tabs/sqlite_offline.py, sqlite_online.py,
# sqlite_sync.py).
from .sqlite_tab import SqliteTab
from .apps import AppsTab
# The ToDo workspace: a PL/UK holiday calendar, a task list and the reminders
# that fire through tabs/todo_notify.py wherever the user happens to be. The
# rules live in tabs/todo_core.py so the tab is only its interface. It is
# shown as a popup window (TodoWindow), not as a tab.
from .todo_tab import TodoTab, TodoWindow
__all__ = ["ScriptAnalyzerTab", "AuditTab", "DebuggerTab", "SandboxTab",
           "DocsTab", "EditorTab", "ReportsTab", "SearchTab", "VaultTab",
           "SecurityTab", "StructureTab", "SqliteTab", "MetadataTab",
           "AppsTab", "TodoTab", "TodoWindow"]
