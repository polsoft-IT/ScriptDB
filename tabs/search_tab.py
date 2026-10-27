# -*- coding: utf-8 -*-
"""Search tab for ScriptDB.

This module owns the "Search" tab: a dedicated surface for the search kinds
that live in tabs/search_engine.py. The Editor tab already runs those searches
against the open buffer, but a search is a task of its own -- you often want to
sweep the whole database without loading a script first -- so this tab gives the
engine a home of its own.

    SearchTab(window)          -- build the tab
    tab.refresh()              -- reload the script count from the window
    tab.retranslate(table)     -- apply a language table (see TRANSLATIONS)
    tab.run_search()           -- run the current query and fill the results
    tab.goto_hit(item)         -- jump to a result, loading its script first

The tab holds no search state of its own beyond the last result: it reads the
script list from the main window and hands documents to the engine, so the
Scripts, Editor and Search tabs never disagree about what a script contains.

Hot plugging: the kind combo is built from ``search_engine.SEARCH_KINDS``, so a
kind registered with ``search_engine.register_kind`` shows up here after a
``refresh()`` without a change to this file.
"""

import os
import re

from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtGui import QFont

from . import search_engine

# Default programming font stack (cross-platform; falls back gracefully).
SEARCH_FONT = "Consolas"
SEARCH_FONT_SIZE = 10

# Search-row options: (key, label key, fallback, default checked).
SEARCH_OPTIONS = (
    ("case_sensitive", "search_case", "Case", False),
    ("definitions", "search_mark_definitions", "Mark definitions", True),
    ("include_methods", "search_include_methods", "Method calls", True),
)

# Where a query runs: the whole database or only the selected script.
SEARCH_SCOPES = (
    ("database", "search_scope_database", "Whole database"),
    ("selection", "search_scope_selection", "Selected script"),
    ("editor", "search_scope_editor", "Open in editor"),
    ("directory", "search_scope_directory", "Directory (recursive)"),
    ("workspace", "search_scope_workspace", "Open workspace"),
)

# Result colours, shared with the Editor tab so a kind looks the same in both.
SEARCH_COLOURS = {
    "definition": "#9be9a8", "class": "#9be9a8",
    "function": "#8ab4f8", "method": "#8ab4f8",
    "call": "#c586c0", "method call": "#c586c0",
    "reference": "#bfc7d5", "variable": "#f7c66d",
    "import": "#4ec9b0", "TODO": "#f7c66d", "FIXME": "#ff9a9a",
    "HACK": "#ff9a9a", "XXX": "#ff9a9a", "BUG": "#ff9a9a",
}


class SearchTab(QtWidgets.QWidget):
    """Search the whole script database with the search-engine kinds.

    The tab mirrors the Editor tab's search row, but targets the database rather
    than the open buffer and keeps its own results table, so a search here never
    disturbs what is being edited.
    """

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self._result = None
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

    def _open_editor_document(self):
        """The buffer of the Editor tab, when one is loaded."""
        editor = getattr(self.window, "editor_tab", None)
        if editor is None or not hasattr(editor, "editor"):
            return None
        name = editor.name_label.text().split("  [")[0].strip() or "(buffer)"
        try:
            category = editor.current_category()
        except Exception:
            category = "Python"
        return (name, editor.editor.toPlainText(), category)

    # ------------------------------------------------------------------- UI
    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)

        header = QtWidgets.QHBoxLayout()
        self.title_label = QtWidgets.QLabel(self.t("search", "Search"))
        self.title_label.setStyleSheet(
            "font-size: 14px; font-weight: bold; color: #8ab4f8;")
        header.addWidget(self.title_label)
        header.addStretch()
        self.count_label = QtWidgets.QLabel("")
        self.count_label.setStyleSheet("color: #9aa0a6;")
        header.addWidget(self.count_label)
        self.btn_refresh = QtWidgets.QPushButton(
            self.t("search_refresh", "Refresh"))
        self.btn_refresh.clicked.connect(self.refresh)
        header.addWidget(self.btn_refresh)
        self.btn_export = QtWidgets.QPushButton(
            self.t("search_export", "Export results"))
        self.btn_export.clicked.connect(self.export_results)
        header.addWidget(self.btn_export)
        layout.addLayout(header)

        # ---- scope + kind ------------------------------------------------
        scope_row = QtWidgets.QHBoxLayout()
        self.scope_label = QtWidgets.QLabel(self.t("search_scope", "Scope:"))
        scope_row.addWidget(self.scope_label)
        self.scope_combo = QtWidgets.QComboBox()
        for key, label_key, fallback in SEARCH_SCOPES:
            self.scope_combo.addItem(self.t(label_key, fallback), key)
        self.scope_combo.currentIndexChanged.connect(self._on_scope_changed)
        scope_row.addWidget(self.scope_combo)
        self.path_edit = QtWidgets.QLineEdit()
        self.path_edit.setPlaceholderText("Directory or workspace roots")
        self.path_edit.setVisible(False)
        scope_row.addWidget(self.path_edit, 1)
        self.btn_path = QtWidgets.QPushButton("Browse")
        self.btn_path.setVisible(False)
        self.btn_path.clicked.connect(self._choose_search_path)
        scope_row.addWidget(self.btn_path)

        self.search_label = QtWidgets.QLabel(self.t("search_kind", "Search:"))
        scope_row.addWidget(self.search_label)
        self.kind_combo = QtWidgets.QComboBox()
        for key, label_key, fallback in search_engine.SEARCH_KINDS:
            self.kind_combo.addItem(self.t(label_key, fallback), key)
        self.kind_combo.currentIndexChanged.connect(self._on_kind_changed)
        scope_row.addWidget(self.kind_combo)

        # Second combo: semantic sub-kind or AST node kind, shown on demand.
        self.subkind_combo = QtWidgets.QComboBox()
        self.subkind_combo.setVisible(False)
        scope_row.addWidget(self.subkind_combo)
        scope_row.addStretch()
        layout.addLayout(scope_row)

        # ---- query --------------------------------------------------------
        query_row = QtWidgets.QHBoxLayout()
        self.query_edit = QtWidgets.QLineEdit()
        self.query_edit.setPlaceholderText(
            self.t("search_hint", "Search in code..."))
        self.query_edit.returnPressed.connect(self.run_search)
        query_row.addWidget(self.query_edit, 1)
        self.btn_search = QtWidgets.QPushButton(self.t("search_run", "Search"))
        self.btn_search.clicked.connect(self.run_search)
        query_row.addWidget(self.btn_search)
        self.btn_clear = QtWidgets.QPushButton(self.t("search_clear", "Clear"))
        self.btn_clear.clicked.connect(self.clear_results)
        query_row.addWidget(self.btn_clear)

        # The remaining options live in one compact menu.
        self.btn_options = QtWidgets.QToolButton()
        self.btn_options.setText(self.t("search_options", "Options"))
        self.btn_options.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.options_menu = QtWidgets.QMenu(self.btn_options)
        self.option_checks = {}
        for key, label_key, fallback, default in SEARCH_OPTIONS:
            action = self.options_menu.addAction(self.t(label_key, fallback))
            action.setCheckable(True)
            action.setChecked(default)
            self.option_checks[key] = action
        self.btn_options.setMenu(self.options_menu)
        query_row.addWidget(self.btn_options)
        layout.addLayout(query_row)

        # ---- results ------------------------------------------------------
        self.results = QtWidgets.QTreeWidget()
        self.results.setAlternatingRowColors(True)
        self.results.setColumnCount(5)
        self.results.setHeaderLabels([
            self.t("search_col_doc", "Script"),
            self.t("search_col_kind", "Kind"),
            self.t("search_col_line", "Line"),
            self.t("search_col_text", "Match"),
            self.t("search_detail", "Detail"),
        ])
        self.results.setFont(QFont(SEARCH_FONT, SEARCH_FONT_SIZE))
        self.results.itemActivated.connect(self.goto_hit)
        self.results.itemDoubleClicked.connect(self.goto_hit)
        self.results.setSortingEnabled(True)
        layout.addWidget(self.results, 1)

        self.status = QtWidgets.QLabel("")
        self.status.setStyleSheet("color: #9aa0a6;")
        layout.addWidget(self.status)

        self._on_kind_changed(0)
        self._on_scope_changed(0)

    def _on_kind_changed(self, _index):
        """Swap the second combo between semantic kinds and AST node kinds."""
        kind = self.current_kind()
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
        self.subkind_combo.blockSignals(True)
        self.subkind_combo.clear()
        if table:
            for key, label_key, fallback in table:
                self.subkind_combo.addItem(self.t(label_key, fallback), key)
            self.subkind_combo.setVisible(True)
        else:
            self.subkind_combo.setVisible(False)
        self.subkind_combo.blockSignals(False)
        # The filesystem kinds read their target from the path field, so reveal
        # it exactly like the directory/workspace scopes do.
        if kind in ("files", "workspace"):
            self._show_path_row(kind == "workspace")
        # A few kinds need no query at all.
        needs_query = kind not in ("semantic", "imports", "blocks",
                                   "comments", "ast", "todo")
        self.query_edit.setEnabled(needs_query)
        self.query_edit.setPlaceholderText(
            self.t("search_hint", "Search in code...") if needs_query
            else self.t("search_optional", "(optional filter)"))

    def _show_path_row(self, workspace=False):
        # Reveal the directory/workspace path field for a filesystem kind.
        if workspace and not self.path_edit.text():
            self.path_edit.setText(";".join(self._workspace_roots()))
        self.path_edit.setVisible(True)
        self.btn_path.setVisible(not workspace)

    def _on_scope_changed(self, _index):
        """Describe the documents the current scope covers."""
        scope = self.current_scope()
        filesystem = scope in ("directory", "workspace")
        if filesystem:
            self._show_path_row(scope == "workspace")
        else:
            self.path_edit.setVisible(False)
            self.btn_path.setVisible(False)
        self.status.setText(self.t(
            "search_scope_set", "scope: {name}").format(
            name=self.scope_combo.currentText()))

    def _choose_search_path(self):
        path = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Select search directory", self.path_edit.text() or os.getcwd())
        if path:
            self.path_edit.setText(path)

    def _workspace_roots(self):
        '''Return workspace roots exposed by the host window.'''
        for attribute in ("workspace_roots", "project_roots", "open_projects"):
            value = getattr(self.window, attribute, None)
            if callable(value):
                try:
                    value = value()
                except Exception:
                    value = None
            if isinstance(value, str):
                value = [value]
            if value:
                return [str(root) for root in value if root]
        base = getattr(self.window, "base_dir", None) or getattr(
            self.window, "BASE_DIR", None)
        return [base] if base else []

    # Default file extensions the filesystem searches read.
    DEFAULT_EXTENSIONS = ("py", "js", "ts", "java", "cpp", "h", "hpp",
                          "rb", "rs", "go", "html", "css", "json", "yaml",
                          "yml", "ini", "md", "txt")

    # ------------------------------------------------------------- queries
    def current_kind(self):
        return self.kind_combo.currentData() or "text"

    def current_scope(self):
        return self.scope_combo.currentData() or "database"

    def search_documents(self):
        """The documents the current scope should search over."""
        scope = self.current_scope()
        if scope == "selection":
            script = self._selected_script()
            if script is None:
                return []
            return [(script.get("name", ""), script.get("code", "") or "",
                     script.get("category", "Python"))]
        if scope == "editor":
            document = self._open_editor_document()
            return [document] if document is not None else []
        if scope in ("directory", "workspace"):
            return []
        documents = []
        for script in self._scripts():
            documents.append((script.get("name", ""),
                              script.get("code", "") or "",
                              script.get("category", "Python")))
        return documents

    def _options(self):
        """The engine options the current kind reads."""
        kind = self.current_kind()
        options = {"case_sensitive": self.option_checks[
            "case_sensitive"].isChecked()}
        if kind in ("files", "workspace") or self.current_scope() in (
                "directory", "workspace"):
            options["extensions"] = self.DEFAULT_EXTENSIONS
        if kind == "string":
            options["match"] = self.subkind_combo.currentData() or "substring"
        elif kind == "multifile":
            options["mode"] = self.subkind_combo.currentData() or "text"
        elif kind == "semantic":
            options["symbol_kind"] = self.subkind_combo.currentData() or "any"
        elif kind == "imports":
            options["import_kind"] = self.subkind_combo.currentData() or "any"
        elif kind == "blocks":
            options["block_kind"] = self.subkind_combo.currentData() or "any"
        elif kind == "comments":
            options["comment_kind"] = self.subkind_combo.currentData() or "any"
        elif kind == "ast":
            options["node_kind"] = self.subkind_combo.currentData() or "any"
        elif kind == "calls":
            options["include_methods"] = self.option_checks[
                "include_methods"].isChecked()
        elif kind == "references":
            options["mark_definitions"] = self.option_checks[
                "definitions"].isChecked()
        return options
    def _search_mode(self):
        # The textual sub-mode (text/string/regex/multiline) to run.
        kind = self.current_kind()
        if kind in ("regex", "multiline", "string"):
            return kind
        if kind == "multifile":
            return self.subkind_combo.currentData() or "text"
        return "text"

    def _workspace_targets(self):
        # Explicit roots typed in the path field (or the defaults).
        return [part.strip() for part in self.path_edit.text().split(";")
                if part.strip()] or self._workspace_roots() or [os.getcwd()]

    def _filesystem_result(self, query, options):
        # Run a filesystem search for the current kind or scope.
        kind = self.current_kind()
        mode = self._search_mode()
        options = dict(options)
        options.pop("mode", None)
        if kind == "files" or self.current_scope() == "directory":
            return search_engine.search_filesystem(
                self.path_edit.text(), query, mode=mode, **options)
        return search_engine.search_workspace(
            self._workspace_targets(), query, mode=mode, **options)

    def run_search(self, quiet=False):
        """Run the current search and fill the results table."""
        query = self.query_edit.text()
        kind = self.current_kind()
        options = self._options()
        filesystem = (kind in ("files", "workspace")
                      or self.current_scope() in ("directory", "workspace"))
        if filesystem:
            result = self._filesystem_result(query, options)
        elif kind == "multifile":
            result = search_engine.search_multifile(
                self.search_documents(), query, paths=[], **options)
        else:
            documents = self.search_documents()
            result = search_engine.search(documents, query, kind, **options)
        self._result = result
        self.fill_results(result)
        if not quiet:
            self.status.setText(result.summary())
        return result

    def fill_results(self, result):
        """Render a SearchResult into the results tree."""
        tree = self.results
        tree.setSortingEnabled(False)
        tree.clear()
        for hit in result.hits:
            item = QtWidgets.QTreeWidgetItem([
                hit.doc, hit.kind, str(hit.line) if hit.line else "-",
                hit.text, hit.detail])
            colour = SEARCH_COLOURS.get(hit.kind)
            if colour is not None:
                item.setForeground(1, QtGui.QColor(colour))
            # Keep the hit on the row so activating it can jump to it.
            item.setData(0, QtCore.Qt.UserRole, hit)
            tree.addTopLevelItem(item)
        for column in range(tree.columnCount()):
            tree.resizeColumnToContents(column)
        tree.setSortingEnabled(True)
        if result.error:
            self.status.setText(result.error)

    def clear_results(self):
        """Empty the results table and forget the last search."""
        self.results.clear()
        self._result = None
        self.status.setText(self.t("search_cleared", "search results cleared"))

    def refresh(self):
        """Reload the script count so the header stays accurate."""
        count = len(self._scripts())
        self.count_label.setText(self.t(
            "search_count", "{count} script(s) searchable").format(
            count=count))
        if self._result is None:
            self.status.setText(self.t(
                "search_ready", "Pick a kind and run a search."))

    # ------------------------------------------------------------- results
    def goto_hit(self, item, _column=0):
        """Open the script a result row points at, on the matching line."""
        hit = item.data(0, QtCore.Qt.UserRole) if item is not None else None
        if hit is None:
            return
        script = self._find_script_by_name(hit.doc)
        if script is not None:
            self._load_into_editor(script)
            editor = getattr(self.window, "editor_tab", None)
            if editor is not None and hit.line:
                editor.editor.goto_line(hit.line - 1)
                self._select_hit(editor, hit)
        self.status.setText(self.t(
            "search_jumped", "opened {doc} at line {line}").format(
            doc=hit.doc, line=hit.line))

    def _find_script_by_name(self, name):
        for script in self._scripts():
            if script.get("name", "") == name:
                return script
        return None

    def _load_into_editor(self, script):
        """Open a script on the Editor tab (best effort)."""
        editor = getattr(self.window, "editor_tab", None)
        loader = getattr(editor, "load_script", None)
        if callable(loader):
            try:
                loader(script)
            except Exception:
                pass

    @staticmethod
    def _select_hit(editor, hit):
        """Highlight the matched text inside the target line."""
        document = editor.editor.document()
        block = document.findBlockByNumber(max(0, hit.line - 1))
        if not block.isValid():
            return
        start = block.position() + max(0, hit.column)
        size = max(1, len(hit.detail) or len(hit.text.split()[-1] or ""))
        cursor = QtGui.QTextCursor(document)
        cursor.setPosition(start)
        cursor.setPosition(min(start + size, block.position()
                               + len(block.text())),
                           QtGui.QTextCursor.KeepAnchor)
        editor.editor.setTextCursor(cursor)

    # -------------------------------------------------------------- export
    def export_results(self):
        """Write the current results to a plain text file."""
        if self._result is None or not self._result.hits:
            self.status.setText(self.t("search_nothing_export",
                                       "run a search first"))
            return ""
        directory = self._data_dir()
        path = os.path.join(directory, "search_results.txt")
        lines = ["# %s" % self._result.summary(), ""]
        for hit in self._result.hits:
            lines.append("%s:%s  [%s]  %s  %s" % (
                hit.doc, hit.line, hit.kind, hit.text, hit.detail))
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("\n".join(lines))
        except OSError as exc:
            self.status.setText(str(exc))
            return ""
        self.status.setText(self.t(
            "search_exported", "exported: {path}").format(path=path))
        return path

    def _data_dir(self):
        base = getattr(self.window, "base_dir", None) or getattr(
            self.window, "BASE_DIR", None)
        if base:
            return base
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # ------------------------------------------------------- translation
    def retranslate(self, table):
        """Apply the main window's language table to this tab."""
        def label(key, fallback):
            return table.get(key, fallback)

        self.title_label.setText(label("search", "Search"))
        self.btn_refresh.setText(label("search_refresh", "Refresh"))
        self.btn_export.setText(label("search_export", "Export results"))
        self.scope_label.setText(label("search_scope", "Scope:"))
        for index, (key, label_key, fallback) in enumerate(SEARCH_SCOPES):
            self.scope_combo.setItemText(index, label(label_key, fallback))
        self.search_label.setText(label("search_kind", "Search:"))
        for index, (key, label_key, fallback) in enumerate(
                search_engine.SEARCH_KINDS):
            self.kind_combo.setItemText(index, label(label_key, fallback))
        self.query_edit.setPlaceholderText(
            label("search_hint", "Search in code..."))
        self.btn_search.setText(label("search_run", "Search"))
        self.btn_clear.setText(label("search_clear", "Clear"))
        self.btn_options.setText(label("search_options", "Options"))
        for key, label_key, _fallback, _default in SEARCH_OPTIONS:
            if key in self.option_checks:
                self.option_checks[key].setText(label(label_key, fallback))
        self.results.setHeaderLabels([
            label("search_col_doc", "Script"),
            label("search_col_kind", "Kind"),
            label("search_col_line", "Line"),
            label("search_col_text", "Match"),
            label("search_col_detail", "Detail"),
        ])
        # Rebuild the second combo so its labels pick up the new language too.
        self._on_kind_changed(self.kind_combo.currentIndex())
        self.refresh()
