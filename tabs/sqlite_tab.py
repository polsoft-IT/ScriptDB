# -*- coding: utf-8 -*-
"""SQLite tab for ScriptDB.

The tab is the GUI over two independent layers, and it deliberately holds no
data logic of its own:

    tabs/sqlite_offline.py   the offline Data Layer (databases in files)
    tabs/sqlite_online.py    the online Data Layer (a database behind an API)
    tabs/sqlite_sync.py      the Sync Layer that joins them

It presents them as six inner tabs, exactly as the architecture asks:

    Offline   the file explorer, the table explorer, the record grid, the
              schema panel, the SQL console and the statistics panel
    Online    the login panel, the server panel, the online table explorer,
              the online record grid and the API console
    Sync      the mode, the conflict policy, the comparison, the plan and the
              change report
    Tools     backup and restore, integrity check, repair, compaction,
              import and export, duplicate detection
    Logs      the operation log of every layer, merged and filterable
    Settings  the defaults the tab starts with

The offline half is the main one: it is fully usable with no server anywhere.
The online half is deliberately smaller -- login, tables, CRUD, sync.
"""

import datetime as dt
import json
import os
import re

from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtGui import QFont

from . import sqlite_offline
from . import sqlite_online
from . import sqlite_sync
from .sqlite_offline import (OfflineDatabase, OFFLINE_FORMAT_LABELS,
                             candidate_files, guess_format)
from .sqlite_online import OnlineDatabase, CONSOLE_METHODS
from .sqlite_sync import (SyncEngine, SYNC_MODES, SYNC_MODE_KEYS,
                          SYNC_MODE_LABELS, CONFLICT_POLICIES,
                          CONFLICT_POLICY_KEYS, CONFLICT_POLICY_LABELS)
SQLITE_FONT = "Consolas"
SQLITE_FONT_SIZE = 10

#: How many rows one page of the record grid holds.
PAGE_SIZE = 200

#: The six inner tabs, as (key, label key, fallback, what it holds).
SQLITE_SECTIONS = (
    ("offline", "sqlite_tab_offline", "Offline database",
     "file explorer, tables, records, schema, SQL and statistics"),
    ("online", "sqlite_tab_online", "Online database",
     "login, server tables, records and the API console"),
    ("sync", "sqlite_tab_sync", "Sync",
     "modes, conflicts, the plan and the change report"),
    ("tools", "sqlite_tab_tools", "Tools",
     "backup, restore, integrity, repair, import and export"),
    ("logs", "sqlite_tab_logs", "Logs",
     "the operation log of every layer, merged"),
    ("settings", "sqlite_tab_settings", "Settings",
     "the defaults the tab starts with"),
)

SQLITE_SECTION_KEYS = tuple(key for key, _lk, _f, _d in SQLITE_SECTIONS)
SQLITE_SECTION_LABELS = {key: fallback for key, _lk, fallback, _d
                         in SQLITE_SECTIONS}

#: The settings the tab remembers, as (key, label key, fallback, default).
SQLITE_SETTINGS = (
    ("default_folder", "sqlite_set_folder", "Default folder", ""),
    ("page_size", "sqlite_set_page", "Rows per page", 200),
    ("auto_backup", "sqlite_set_backup", "Back up before a schema change",
     True),
    ("read_only_default", "sqlite_set_readonly", "Open new databases read-only",
     False),
    ("online_base", "sqlite_set_online_base", "Online server address", ""),
    ("online_api", "sqlite_set_online_api", "API path", "/api"),
)

SQLITE_SETTING_KEYS = tuple(key for key, _lk, _f, _d in SQLITE_SETTINGS)
SQLITE_SETTING_LABELS = {key: fallback for key, _lk, fallback, _d
                         in SQLITE_SETTINGS}

#: The tools the Tools tab offers, as (key, label key, fallback).
SQLITE_TOOLS = (
    ("backup", "sqlite_tool_backup", "Back up now"),
    ("restore", "sqlite_tool_restore", "Restore a backup"),
    ("integrity", "sqlite_tool_integrity", "Integrity check"),
    ("repair", "sqlite_tool_repair", "Repair (REINDEX + VACUUM)"),
    ("compact", "sqlite_tool_compact", "Write a compacted copy"),
    ("import", "sqlite_tool_import", "Import a file"),
    ("export", "sqlite_tool_export", "Export the database"),
    ("duplicates", "sqlite_tool_duplicates", "Find duplicate rows"),
    ("validate", "sqlite_tool_validate", "Validate the table"),
    ("stats", "sqlite_tool_stats", "Table statistics"),
)

SQLITE_TOOL_KEYS = tuple(key for key, _lk, _f in SQLITE_TOOLS)
SQLITE_TOOL_LABELS = {key: fallback for key, _lk, fallback in SQLITE_TOOLS}


def _now():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _stamp():
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def _text(value, limit=200):
    """Any value as one short, single-line string."""
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        value = value.hex()
    text = str(value)
    if len(text) > limit:
        return text[:limit] + "..."
    return text


def _dialog(parent, title, body, close_label="Close", editable=False):
    """A read-only text dialog, used for every report and result panel."""
    dialog = QtWidgets.QDialog(parent)
    dialog.setWindowTitle(title)
    dialog.resize(820, 560)
    layout = QtWidgets.QVBoxLayout(dialog)
    view = QtWidgets.QPlainTextEdit(body)
    view.setReadOnly(not editable)
    view.setFont(QFont(SQLITE_FONT, SQLITE_FONT_SIZE))
    layout.addWidget(view, 1)
    buttons = QtWidgets.QHBoxLayout()
    copy = QtWidgets.QPushButton("Copy")
    copy.clicked.connect(
        lambda: QtWidgets.QApplication.clipboard().setText(view.toPlainText()))
    close = QtWidgets.QPushButton(close_label)
    close.clicked.connect(dialog.close)
    buttons.addStretch()
    buttons.addWidget(copy)
    buttons.addWidget(close)
    layout.addLayout(buttons)
    dialog.view = view
    dialog.exec_()
    return dialog


def _row_dialog(parent, columns, row, key_column="", read_only=False):
    """The record editor: one field per column, with the key locked.

    The key is shown but not editable, because changing it would make the row
    unfindable rather than edited -- the correct operation for that is a delete
    and an insert, and the panel offers both.
    """
    dialog = QtWidgets.QDialog(parent)
    dialog.setWindowTitle("Edit record" if not read_only else "Record")
    dialog.resize(620, 520)
    layout = QtWidgets.QVBoxLayout(dialog)
    form = QtWidgets.QFormLayout()
    edits = {}
    for column in columns:
        name = column.get("name", "")
        widget = QtWidgets.QLineEdit(_text(row.get(name), 4000)
                                     if row else "")
        widget.setFont(QFont(SQLITE_FONT, SQLITE_FONT_SIZE))
        if name == key_column:
            widget.setReadOnly(True)
            widget.setStyleSheet("color: #9aa0a6;")
        if read_only:
            widget.setReadOnly(True)
        edits[name] = widget
        label = "%s (%s)%s" % (name, column.get("type", "?"),
                               "  [key]" if name == key_column else "")
        form.addRow(label, widget)
    layout.addLayout(form)
    layout.addStretch()
    buttons = QtWidgets.QHBoxLayout()
    save = QtWidgets.QPushButton("Save")
    save.setDefault(True)
    save.clicked.connect(dialog.accept)
    cancel = QtWidgets.QPushButton("Cancel")
    cancel.clicked.connect(dialog.reject)
    buttons.addStretch()
    buttons.addWidget(cancel)
    if not read_only:
        buttons.addWidget(save)
    layout.addLayout(buttons)
    dialog.edits = edits
    dialog.columns = columns
    if dialog.exec_() != QtWidgets.QDialog.Accepted or read_only:
        return None
    # An empty field means NULL rather than an empty string, which is the
    # distinction a database actually stores.
    payload = {}
    for name, widget in edits.items():
        if name == key_column:
            continue
        text = widget.text()
        payload[name] = None if text == "" else _coerce(text, columns, name)
    return payload


def _coerce(text, columns, name):
    """A typed field value, when the column declares a type it can hold."""
    kind = ""
    for column in columns:
        if column.get("name") == name:
            kind = column.get("kind", "")
            break
    if kind == "integer":
        try:
            return int(text)
        except ValueError:
            return text
    if kind == "real":
        try:
            return float(text)
        except ValueError:
            return text
    return text
class SqliteTab(QtWidgets.QWidget):
    """The SQLite tab: two data layers and the sync layer between them.

    The tab owns one offline database, one online connection and one sync
    engine. Nothing is read or written except through those three, and the tab
    only ever shows what they report -- so the GUI cannot disagree with the data
    layer about what is in the database.
    """

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.offline = OfflineDatabase()
        self.online = OnlineDatabase()
        self.sync = SyncEngine(self.offline, self.online)
        self.settings = {key: default for key, _lk, _f, default
                         in SQLITE_SETTINGS}
        self.current_table = ""
        self.page = 0
        self._build_ui()
        self.load_settings()

    # ------------------------------------------------------------- helpers
    def t(self, key, fallback=None, **kwargs):
        """Look a label up in the main window translations."""
        try:
            return self.window.t(key, fallback, **kwargs)
        except Exception:
            return fallback or key

    def _status(self, message):
        self.status.setText(message)
        return message

    def _announce(self, detail):
        """Record an action on the shared audit timeline."""
        self.offline._record("tab", detail)
        audit = getattr(self.window, "audit_tab", None)
        if audit is not None and hasattr(audit, "log_change"):
            try:
                audit.log_change("modules", "sqlite", detail, "sqlite")
                return
            except Exception:
                pass
        notify = getattr(self.window, "notify_audit", None)
        if callable(notify):
            try:
                notify("module_updated", where="sqlite", detail=detail)
            except Exception:
                pass

    def _data_dir(self):
        base = (getattr(self.window, "base_dir", None)
                or getattr(self.window, "BASE_DIR", None)
                or os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        return base

    # ------------------------------------------------------------------- UI
    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)

        header = QtWidgets.QHBoxLayout()
        self.title_label = QtWidgets.QLabel(self.t("sqlite", "SQLITE"))
        self.title_label.setStyleSheet(
            "font-size: 14px; font-weight: bold; color: #8ab4f8;")
        header.addWidget(self.title_label)
        self.db_label = QtWidgets.QLabel("")
        self.db_label.setStyleSheet("color: #9aa0a6;")
        header.addWidget(self.db_label)
        header.addStretch()
        self.offline_state = QtWidgets.QLabel("")
        self.offline_state.setStyleSheet("color: #9aa0a6;")
        header.addWidget(self.offline_state)
        self.online_state = QtWidgets.QLabel("")
        self.online_state.setStyleSheet("color: #9aa0a6;")
        header.addWidget(self.online_state)
        layout.addLayout(header)

        self.sections = QtWidgets.QTabWidget()
        self.sections.addTab(self._build_offline_panel(),
                             self.t("sqlite_tab_offline", "Offline database"))
        self.sections.addTab(self._build_online_panel(),
                             self.t("sqlite_tab_online", "Online database"))
        self.sections.addTab(self._build_sync_panel(),
                             self.t("sqlite_tab_sync", "Sync"))
        self.sections.addTab(self._build_tools_panel(),
                             self.t("sqlite_tab_tools", "Tools"))
        self.sections.addTab(self._build_logs_panel(),
                             self.t("sqlite_tab_logs", "Logs"))
        self.sections.addTab(self._build_settings_panel(),
                             self.t("sqlite_tab_settings", "Settings"))
        layout.addWidget(self.sections, 1)

        self.status = QtWidgets.QLabel(
            self.t("sqlite_ready", "Open a database file to begin."))
        self.status.setStyleSheet("color: #9aa0a6;")
        layout.addWidget(self.status)

    def _build_offline_panel(self):
        """Explorer, tables, records, schema, SQL console and statistics."""
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)

        # ---- database row -------------------------------------------------
        db_row = QtWidgets.QHBoxLayout()
        self.btn_open = QtWidgets.QPushButton(
            self.t("sqlite_open", "Open database..."))
        self.btn_open.clicked.connect(self.open_file_dialog)
        self.btn_create = QtWidgets.QPushButton(
            self.t("sqlite_create", "New database..."))
        self.btn_create.clicked.connect(self.create_database)
        self.btn_close = QtWidgets.QPushButton(self.t("sqlite_close", "Close"))
        self.btn_close.clicked.connect(self.close_database)
        self.btn_save = QtWidgets.QPushButton(self.t("sqlite_save", "Save"))
        self.btn_save.clicked.connect(self.save_database)
        self.chk_read_only = QtWidgets.QCheckBox(
            self.t("sqlite_read_only", "Read-only"))
        self.chk_read_only.toggled.connect(self.toggle_read_only)
        for widget in (self.btn_open, self.btn_create, self.btn_close,
                       self.btn_save, self.chk_read_only):
            db_row.addWidget(widget)
        db_row.addStretch()
        self.format_label = QtWidgets.QLabel("")
        self.format_label.setStyleSheet("color: #9aa0a6;")
        db_row.addWidget(self.format_label)
        layout.addLayout(db_row)

        # ---- split: explorer+tables | records -----------------------------
        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)

        left = QtWidgets.QWidget()
        left_layout = QtWidgets.QVBoxLayout(left)
        self.folder_edit = QtWidgets.QLineEdit(self._data_dir())
        self.folder_edit.setPlaceholderText(
            self.t("sqlite_folder", "Folder to explore..."))
        browse = QtWidgets.QPushButton(self.t("sqlite_browse", "Browse..."))
        browse.clicked.connect(self.browse_folder)
        folder_row = QtWidgets.QHBoxLayout()
        folder_row.addWidget(self.folder_edit, 1)
        folder_row.addWidget(browse)
        left_layout.addLayout(folder_row)

        self.file_list = QtWidgets.QListWidget()
        self.file_list.itemDoubleClicked.connect(self.open_listed_file)
        left_layout.addWidget(self.file_list, 1)

        self.tables_label = QtWidgets.QLabel(
            self.t("sqlite_tables", "Tables"))
        self.tables_label.setStyleSheet("font-weight: bold; color: #8ab4f8;")
        left_layout.addWidget(self.tables_label)
        self.table_list = QtWidgets.QListWidget()
        self.table_list.currentItemChanged.connect(self.on_table_chosen)
        left_layout.addWidget(self.table_list, 1)
        splitter.addWidget(left)

        right = QtWidgets.QWidget()
        right_layout = QtWidgets.QVBoxLayout(right)

        self.side_tabs = QtWidgets.QTabWidget()

        # records page
        records = QtWidgets.QWidget()
        records_layout = QtWidgets.QVBoxLayout(records)
        grid_row = QtWidgets.QHBoxLayout()
        self.record_search = QtWidgets.QLineEdit()
        self.record_search.setPlaceholderText(
            self.t("sqlite_search_rows", "Filter rows..."))
        self.record_search.returnPressed.connect(self.load_records)
        self.btn_load = QtWidgets.QPushButton(
            self.t("sqlite_load", "Load rows"))
        self.btn_load.clicked.connect(lambda: self.load_records(reset=True))
        self.btn_add_row = QtWidgets.QPushButton(self.t("sqlite_add_row", "Add"))
        self.btn_add_row.clicked.connect(self.add_record)
        self.btn_edit_row = QtWidgets.QPushButton(
            self.t("sqlite_edit_row", "Edit"))
        self.btn_edit_row.clicked.connect(self.edit_record)
        self.btn_copy_row = QtWidgets.QPushButton(
            self.t("sqlite_copy_row", "Copy"))
        self.btn_copy_row.clicked.connect(self.copy_record)
        self.btn_delete_row = QtWidgets.QPushButton(
            self.t("sqlite_delete_row", "Delete"))
        self.btn_delete_row.clicked.connect(self.delete_record)
        for widget in (self.record_search, self.btn_load, self.btn_add_row,
                       self.btn_edit_row, self.btn_copy_row,
                       self.btn_delete_row):
            grid_row.addWidget(widget)
        records_layout.addLayout(grid_row)

        self.grid = QtWidgets.QTableWidget()
        self.grid.setAlternatingRowColors(True)
        self.grid.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.grid.itemDoubleClicked.connect(lambda _item: self.edit_record())
        records_layout.addWidget(self.grid, 1)

        page_row = QtWidgets.QHBoxLayout()
        self.btn_prev = QtWidgets.QPushButton(self.t("sqlite_prev", "Previous"))
        self.btn_prev.clicked.connect(lambda: self.page_by(-1))
        self.btn_next = QtWidgets.QPushButton(self.t("sqlite_next", "Next"))
        self.btn_next.clicked.connect(lambda: self.page_by(1))
        self.page_label = QtWidgets.QLabel("")
        self.page_label.setStyleSheet("color: #9aa0a6;")
        page_row.addWidget(self.btn_prev)
        page_row.addWidget(self.btn_next)
        page_row.addWidget(self.page_label)
        page_row.addStretch()
        records_layout.addLayout(page_row)
        self.side_tabs.addTab(records, self.t("sqlite_records", "Records"))

        # schema page
        schema = QtWidgets.QWidget()
        schema_layout = QtWidgets.QVBoxLayout(schema)
        schema_row = QtWidgets.QHBoxLayout()
        self.btn_add_column = QtWidgets.QPushButton(
            self.t("sqlite_add_column", "Add column"))
        self.btn_add_column.clicked.connect(self.add_column)
        self.btn_new_table = QtWidgets.QPushButton(
            self.t("sqlite_new_table", "New table"))
        self.btn_new_table.clicked.connect(self.create_table)
        self.btn_rename_table = QtWidgets.QPushButton(
            self.t("sqlite_rename_table", "Rename table"))
        self.btn_rename_table.clicked.connect(self.rename_table)
        self.btn_drop_table = QtWidgets.QPushButton(
            self.t("sqlite_drop_table", "Drop table"))
        self.btn_drop_table.clicked.connect(self.drop_table)
        for widget in (self.btn_add_column, self.btn_new_table,
                       self.btn_rename_table, self.btn_drop_table):
            schema_row.addWidget(widget)
        schema_row.addStretch()
        schema_layout.addLayout(schema_row)
        self.schema_tree = QtWidgets.QTreeWidget()
        self.schema_tree.setColumnCount(4)
        self.schema_tree.setHeaderLabels([
            self.t("sqlite_col_name", "Name"),
            self.t("sqlite_col_type", "Type"),
            self.t("sqlite_col_null", "Nullable"),
            self.t("sqlite_col_key", "Key")])
        schema_layout.addWidget(self.schema_tree, 1)
        self.schema_view = QtWidgets.QPlainTextEdit()
        self.schema_view.setReadOnly(True)
        self.schema_view.setFont(QFont(SQLITE_FONT, SQLITE_FONT_SIZE))
        self.schema_view.setMaximumHeight(120)
        schema_layout.addWidget(self.schema_view)
        self.side_tabs.addTab(schema, self.t("sqlite_schema", "Schema"))

        # SQL page
        sql_page = QtWidgets.QWidget()
        sql_layout = QtWidgets.QVBoxLayout(sql_page)
        sql_row = QtWidgets.QHBoxLayout()
        self.btn_run_sql = QtWidgets.QPushButton(
            self.t("sqlite_run_sql", "Run SQL"))
        self.btn_run_sql.clicked.connect(self.run_sql)
        self.btn_explain = QtWidgets.QPushButton(
            self.t("sqlite_explain", "Explain"))
        self.btn_explain.clicked.connect(self.explain_sql)
        self.chk_sql_all = QtWidgets.QCheckBox(
            self.t("sqlite_sql_all", "Whole script"))
        self.chk_sql_all.setChecked(True)
        sql_row.addWidget(self.btn_run_sql)
        sql_row.addWidget(self.btn_explain)
        sql_row.addWidget(self.chk_sql_all)
        sql_row.addStretch()
        sql_layout.addLayout(sql_row)
        self.sql_edit = QtWidgets.QPlainTextEdit()
        self.sql_edit.setFont(QFont(SQLITE_FONT, SQLITE_FONT_SIZE))
        self.sql_edit.setPlaceholderText("SELECT * FROM ...")
        sql_layout.addWidget(self.sql_edit, 1)
        self.sql_result = QtWidgets.QTableWidget()
        self.sql_result.setAlternatingRowColors(True)
        sql_layout.addWidget(self.sql_result, 1)
        self.side_tabs.addTab(sql_page, self.t("sqlite_sql", "SQL"))

        # statistics page
        stats_page = QtWidgets.QWidget()
        stats_layout = QtWidgets.QVBoxLayout(stats_page)
        stats_row = QtWidgets.QHBoxLayout()
        self.btn_stats = QtWidgets.QPushButton(
            self.t("sqlite_refresh_stats", "Refresh statistics"))
        self.btn_stats.clicked.connect(self.show_statistics)
        stats_row.addWidget(self.btn_stats)
        stats_row.addStretch()
        stats_layout.addLayout(stats_row)
        self.stats_tree = QtWidgets.QTreeWidget()
        self.stats_tree.setColumnCount(3)
        self.stats_tree.setHeaderLabels([
            self.t("sqlite_col_name", "Name"),
            self.t("sqlite_col_value", "Value"),
            self.t("sqlite_col_detail", "Detail")])
        stats_layout.addWidget(self.stats_tree, 1)
        self.side_tabs.addTab(stats_page, self.t("sqlite_stats", "Statistics"))

        right_layout.addWidget(self.side_tabs, 1)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, 1)
        return page

    def _build_online_panel(self):
        """Login, server, online tables, online records and the API console."""
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)

        login_row = QtWidgets.QHBoxLayout()
        self.server_edit = QtWidgets.QLineEdit()
        self.server_edit.setPlaceholderText("https://host:port")
        self.api_edit = QtWidgets.QLineEdit("/api")
        self.api_edit.setMaximumWidth(120)
        self.user_edit = QtWidgets.QLineEdit()
        self.user_edit.setPlaceholderText(
            self.t("sqlite_online_user", "User"))
        self.password_edit = QtWidgets.QLineEdit()
        self.password_edit.setEchoMode(QtWidgets.QLineEdit.Password)
        self.password_edit.setPlaceholderText(
            self.t("sqlite_online_pass", "Password"))
        self.btn_login = QtWidgets.QPushButton(
            self.t("sqlite_online_login", "Sign in"))
        self.btn_login.clicked.connect(self.online_login)
        self.btn_logout = QtWidgets.QPushButton(
            self.t("sqlite_online_logout", "Sign out"))
        self.btn_logout.clicked.connect(self.online_logout)
        self.btn_ping = QtWidgets.QPushButton(self.t("sqlite_online_ping", "Ping"))
        self.btn_ping.clicked.connect(self.online_ping)
        self.btn_refresh_token = QtWidgets.QPushButton(
            self.t("sqlite_online_refresh", "Refresh session"))
        self.btn_refresh_token.clicked.connect(self.online_refresh)
        for widget in (self.server_edit, self.api_edit, self.user_edit,
                       self.password_edit, self.btn_login, self.btn_logout,
                       self.btn_ping, self.btn_refresh_token):
            login_row.addWidget(widget)
        login_row.addStretch()
        layout.addLayout(login_row)

        middle = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.online_tables = QtWidgets.QListWidget()
        self.online_tables.currentItemChanged.connect(self.on_online_table)
        middle.addWidget(self.online_tables)

        online_right = QtWidgets.QWidget()
        online_right_layout = QtWidgets.QVBoxLayout(online_right)
        online_row = QtWidgets.QHBoxLayout()
        self.btn_online_tables = QtWidgets.QPushButton(
            self.t("sqlite_online_tables", "Load tables"))
        self.btn_online_tables.clicked.connect(self.load_online_tables)
        self.btn_online_rows = QtWidgets.QPushButton(
            self.t("sqlite_online_rows", "Load rows"))
        self.btn_online_rows.clicked.connect(self.load_online_rows)
        self.btn_online_add = QtWidgets.QPushButton(
            self.t("sqlite_online_add", "Create row"))
        self.btn_online_add.clicked.connect(lambda: self.online_crud("create"))
        self.btn_online_update = QtWidgets.QPushButton(
            self.t("sqlite_online_update", "Update row"))
        self.btn_online_update.clicked.connect(lambda: self.online_crud("update"))
        self.btn_online_delete = QtWidgets.QPushButton(
            self.t("sqlite_online_delete", "Delete row"))
        self.btn_online_delete.clicked.connect(lambda: self.online_crud("delete"))
        for widget in (self.btn_online_tables, self.btn_online_rows,
                       self.btn_online_add, self.btn_online_update,
                       self.btn_online_delete):
            online_row.addWidget(widget)
        online_row.addStretch()
        online_right_layout.addLayout(online_row)
        self.online_grid = QtWidgets.QTableWidget()
        self.online_grid.setAlternatingRowColors(True)
        online_right_layout.addWidget(self.online_grid, 1)
        middle.addWidget(online_right)
        middle.setStretchFactor(0, 1)
        middle.setStretchFactor(1, 2)
        layout.addWidget(middle, 1)

        console_row = QtWidgets.QHBoxLayout()
        self.console_method = QtWidgets.QComboBox()
        for method in CONSOLE_METHODS:
            self.console_method.addItem(method, method)
        self.console_path = QtWidgets.QLineEdit("/tables")
        self.console_body = QtWidgets.QLineEdit()
        self.console_body.setPlaceholderText(
            self.t("sqlite_console_body", "JSON body (optional)"))
        self.btn_console_send = QtWidgets.QPushButton(
            self.t("sqlite_console_send", "Send"))
        self.btn_console_send.clicked.connect(self.api_console_send)
        self.chk_console_token = QtWidgets.QCheckBox(
            self.t("sqlite_console_token", "Attach the token"))
        self.chk_console_token.setChecked(True)
        for widget in (self.console_method, self.console_path,
                       self.console_body, self.chk_console_token,
                       self.btn_console_send):
            console_row.addWidget(widget)
        layout.addLayout(console_row)
        self.console_view = QtWidgets.QPlainTextEdit()
        self.console_view.setReadOnly(True)
        self.console_view.setFont(QFont(SQLITE_FONT, SQLITE_FONT_SIZE))
        self.console_view.setMaximumHeight(180)
        layout.addWidget(self.console_view)
        return page
    def _build_sync_panel(self):
        """Mode, policy, comparison, plan and change report."""
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)

        setup_row = QtWidgets.QHBoxLayout()
        self.sync_mode = QtWidgets.QComboBox()
        for key, label_key, fallback, detail in SYNC_MODES:
            self.sync_mode.addItem(self.t(label_key, fallback), key)
            self.sync_mode.setItemData(self.sync_mode.count() - 1, detail,
                                       QtCore.Qt.ToolTipRole)
        self.sync_policy = QtWidgets.QComboBox()
        for key, label_key, fallback in CONFLICT_POLICIES:
            self.sync_policy.addItem(self.t(label_key, fallback), key)
        self.sync_table = QtWidgets.QLineEdit()
        self.sync_table.setPlaceholderText(
            self.t("sqlite_sync_table", "Table to reconcile"))
        self.sync_key = QtWidgets.QLineEdit("id")
        self.sync_key.setMaximumWidth(90)
        self.btn_compare = QtWidgets.QPushButton(
            self.t("sqlite_sync_compare", "Compare"))
        self.btn_compare.clicked.connect(self.sync_compare)
        self.btn_plan = QtWidgets.QPushButton(self.t("sqlite_sync_plan", "Plan"))
        self.btn_plan.clicked.connect(self.sync_plan)
        self.btn_run_sync = QtWidgets.QPushButton(
            self.t("sqlite_sync_run", "Run the sync"))
        self.btn_run_sync.clicked.connect(self.sync_run)
        self.btn_resolve = QtWidgets.QPushButton(
            self.t("sqlite_sync_resolve", "Resolve the selected conflict"))
        self.btn_resolve.clicked.connect(self.sync_resolve)
        for widget in (self.sync_mode, self.sync_policy, self.sync_table,
                       self.sync_key, self.btn_compare, self.btn_plan,
                       self.btn_run_sync, self.btn_resolve):
            setup_row.addWidget(widget)
        setup_row.addStretch()
        layout.addLayout(setup_row)

        self.sync_summary = QtWidgets.QLabel("")
        self.sync_summary.setStyleSheet("color: #9aa0a6;")
        layout.addWidget(self.sync_summary)

        self.sync_tree = QtWidgets.QTreeWidget()
        self.sync_tree.setColumnCount(4)
        self.sync_tree.setHeaderLabels([
            self.t("sqlite_col_key", "Key"),
            self.t("sqlite_col_kind", "Difference"),
            self.t("sqlite_col_offline", "Offline"),
            self.t("sqlite_col_online", "Online")])
        self.sync_tree.setSelectionMode(
            QtWidgets.QAbstractItemView.ExtendedSelection)
        layout.addWidget(self.sync_tree, 1)

        report_row = QtWidgets.QHBoxLayout()
        self.btn_change_report = QtWidgets.QPushButton(
            self.t("sqlite_sync_report", "Change report"))
        self.btn_change_report.clicked.connect(self.sync_report)
        self.btn_save_report = QtWidgets.QPushButton(
            self.t("sqlite_sync_save_report", "Save the report"))
        self.btn_save_report.clicked.connect(self.sync_save_report)
        report_row.addWidget(self.btn_change_report)
        report_row.addWidget(self.btn_save_report)
        report_row.addStretch()
        layout.addLayout(report_row)
        return page

    def _build_tools_panel(self):
        """Backup, restore, integrity, repair, import, export, duplicates."""
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)

        grid = QtWidgets.QGridLayout()
        self.tool_buttons = {}
        for index, (key, label_key, fallback) in enumerate(SQLITE_TOOLS):
            button = QtWidgets.QPushButton(self.t(label_key, fallback))
            button.clicked.connect(
                lambda _checked=False, k=key: self.run_tool(k))
            self.tool_buttons[key] = button
            grid.addWidget(button, index // 5, index % 5)
        layout.addLayout(grid)

        keep_row = QtWidgets.QHBoxLayout()
        self.backup_keep = QtWidgets.QSpinBox()
        self.backup_keep.setRange(0, 50)
        self.backup_keep.setValue(5)
        keep_row.addWidget(QtWidgets.QLabel(
            self.t("sqlite_backup_keep", "Backups to keep:")))
        keep_row.addWidget(self.backup_keep)
        keep_row.addStretch()
        layout.addLayout(keep_row)

        self.tools_view = QtWidgets.QPlainTextEdit()
        self.tools_view.setReadOnly(True)
        self.tools_view.setFont(QFont(SQLITE_FONT, SQLITE_FONT_SIZE))
        layout.addWidget(self.tools_view, 1)
        return page

    def _build_logs_panel(self):
        """Every layer log, merged and filterable."""
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        row = QtWidgets.QHBoxLayout()
        self.log_filter = QtWidgets.QLineEdit()
        self.log_filter.setPlaceholderText(
            self.t("sqlite_log_filter", "Filter the log..."))
        self.log_filter.textChanged.connect(self.refresh_logs)
        self.btn_log_refresh = QtWidgets.QPushButton(
            self.t("sqlite_log_refresh", "Refresh"))
        self.btn_log_refresh.clicked.connect(self.refresh_logs)
        self.btn_log_save = QtWidgets.QPushButton(
            self.t("sqlite_log_save", "Save the log"))
        self.btn_log_save.clicked.connect(self.save_log)
        for widget in (self.log_filter, self.btn_log_refresh,
                       self.btn_log_save):
            row.addWidget(widget)
        row.addStretch()
        layout.addLayout(row)

        self.log_tree = QtWidgets.QTreeWidget()
        self.log_tree.setColumnCount(4)
        self.log_tree.setHeaderLabels([
            self.t("sqlite_col_time", "Time"),
            self.t("sqlite_col_layer", "Layer"),
            self.t("sqlite_col_action", "Action"),
            self.t("sqlite_col_detail", "Detail")])
        self.log_tree.setAlternatingRowColors(True)
        layout.addWidget(self.log_tree, 1)
        return page

    def _build_settings_panel(self):
        """The defaults the tab starts with."""
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        form = QtWidgets.QFormLayout()
        self.setting_widgets = {}
        for key, label_key, fallback, default in SQLITE_SETTINGS:
            if isinstance(default, bool):
                widget = QtWidgets.QCheckBox()
                widget.setChecked(bool(default))
            elif isinstance(default, int):
                widget = QtWidgets.QSpinBox()
                widget.setRange(0, 100000)
                widget.setValue(int(default))
            else:
                widget = QtWidgets.QLineEdit(str(default))
            self.setting_widgets[key] = widget
            form.addRow(self.t(label_key, fallback), widget)
        layout.addLayout(form)

        note = QtWidgets.QLabel(self.t(
            "sqlite_settings_note",
            "The settings decide what the tab starts with. They are kept in "
            "memory for this session; the tab never writes them anywhere "
            "without being asked."))
        note.setWordWrap(True)
        note.setStyleSheet("color: #9aa0a6;")
        layout.addWidget(note)

        buttons = QtWidgets.QHBoxLayout()
        apply_button = QtWidgets.QPushButton(self.t("sqlite_apply", "Apply"))
        apply_button.clicked.connect(self.apply_settings)
        reset_button = QtWidgets.QPushButton(self.t("sqlite_reset", "Reset"))
        reset_button.clicked.connect(self.reset_settings)
        buttons.addStretch()
        buttons.addWidget(reset_button)
        buttons.addWidget(apply_button)
        layout.addLayout(buttons)
        layout.addStretch()
        return page

    # -------------------------------------------------------------- settings
    def read_settings(self):
        """Whatever the Settings panel currently holds."""
        values = {}
        for key, widget in self.setting_widgets.items():
            if isinstance(widget, QtWidgets.QCheckBox):
                values[key] = widget.isChecked()
            elif isinstance(widget, QtWidgets.QSpinBox):
                values[key] = widget.value()
            else:
                values[key] = widget.text()
        return values

    def apply_settings(self):
        self.settings.update(self.read_settings())
        self.page_size = max(10, int(self.settings.get("page_size") or
                                     PAGE_SIZE))
        self.folder_edit.setText(self.settings.get("default_folder")
                                 or self.folder_edit.text())
        self.api_edit.setText(self.settings.get("online_api") or "/api")
        if self.settings.get("online_base") and not self.server_edit.text():
            self.server_edit.setText(self.settings["online_base"])
        self.chk_read_only.setChecked(
            bool(self.settings.get("read_only_default")))
        self.refresh_file_list()
        self._status(self.t("sqlite_settings_applied", "Settings applied."))
        return self.settings

    def reset_settings(self):
        self.settings = {key: default for key, _lk, _f, default
                         in SQLITE_SETTINGS}
        for key, widget in self.setting_widgets.items():
            value = self.settings[key]
            if isinstance(widget, QtWidgets.QCheckBox):
                widget.setChecked(bool(value))
            elif isinstance(widget, QtWidgets.QSpinBox):
                widget.setValue(int(value or 0))
            else:
                widget.setText(str(value))
        self._status(self.t("sqlite_settings_reset", "Settings reset."))
        return self.settings

    def load_settings(self):
        """Bring the tab into line with the settings it starts with."""
        for key, widget in self.setting_widgets.items():
            value = self.settings.get(key)
            if isinstance(widget, QtWidgets.QCheckBox):
                widget.setChecked(bool(value))
            elif isinstance(widget, QtWidgets.QSpinBox):
                widget.setValue(int(value or 0))
            else:
                widget.setText(str(value or ""))
        self.page_size = max(10, int(self.settings.get("page_size")
                                     or PAGE_SIZE))
        self.refresh_file_list()
        self.refresh_state()
        return self.settings
    # ------------------------------------------------------- offline actions
    def refresh_state(self):
        """Bring the labels and the enabled buttons in line with the layers."""
        opened = self.offline.is_open()
        self.offline_state.setText(self.t(
            "sqlite_state_open" if opened else "sqlite_state_closed",
            "open" if opened else "closed"))
        self.offline_state.setStyleSheet(
            "color: #9ece6a;" if opened else "color: #9aa0a6;")
        self.db_label.setText(os.path.basename(self.offline.path)
                              if opened else "")
        self.db_label.setToolTip(self.offline.path if opened else "")
        self.format_label.setText(OFFLINE_FORMAT_LABELS.get(
            self.offline.kind, "") if opened else "")
        self.chk_read_only.blockSignals(True)
        self.chk_read_only.setChecked(bool(self.offline.read_only))
        self.chk_read_only.blockSignals(False)
        for widget in (self.btn_close, self.btn_save, self.btn_add_row,
                       self.btn_edit_row, self.btn_copy_row,
                       self.btn_delete_row, self.btn_load, self.btn_run_sql,
                       self.btn_explain, self.btn_new_table,
                       self.btn_rename_table, self.btn_drop_table,
                       self.btn_add_column, self.btn_stats):
            widget.setEnabled(opened)
        online = self.online.is_signed_in()
        self.online_state.setText(self.t(
            "sqlite_state_signed_in" if online else "sqlite_state_signed_out",
            "signed in" if online else "signed out"))
        self.online_state.setStyleSheet(
            "color: #9ece6a;" if online else "color: #9aa0a6;")
        for widget in (self.btn_online_tables, self.btn_online_rows,
                       self.btn_online_add, self.btn_online_update,
                       self.btn_online_delete, self.btn_logout,
                       self.btn_refresh_token):
            widget.setEnabled(online)
        self.btn_login.setEnabled(not online)
        for widget in (self.btn_compare, self.btn_plan, self.btn_run_sync,
                       self.btn_resolve, self.btn_change_report,
                       self.btn_save_report):
            widget.setEnabled(opened and online)

    def refresh_file_list(self):
        """The explorer list: the database-ish files in the chosen folder."""
        folder = self.folder_edit.text().strip() or self._data_dir()
        self.file_list.clear()
        for entry in candidate_files(folder):
            item = QtWidgets.QListWidgetItem("%s   [%s]" % (entry["name"],
                                                           entry["format"]))
            item.setData(QtCore.Qt.UserRole, entry["path"])
            item.setToolTip("%s\n%s\n%s byte(s)" % (entry["path"],
                                                    entry["modified"],
                                                    entry["size"]))
            self.file_list.addItem(item)
        return self.file_list.count()

    def browse_folder(self):
        folder = QtWidgets.QFileDialog.getExistingDirectory(
            self, self.t("sqlite_browse", "Browse..."),
            self.folder_edit.text() or self._data_dir())
        if folder:
            self.folder_edit.setText(folder)
            self.refresh_file_list()
        return folder

    def open_listed_file(self, item):
        return self.open_database(item.data(QtCore.Qt.UserRole))

    def open_file_dialog(self):
        path, _filter = QtWidgets.QFileDialog.getOpenFileName(
            self, self.t("sqlite_open", "Open database..."),
            self.folder_edit.text() or self._data_dir(),
            self.t("sqlite_open_filter",
                   "Databases (*.db *.sqlite *.sqlite3 *.db3 *.json *.csv "
                   "*.txt);;All files (*.*)"))
        return self.open_database(path) if path else None

    def open_database(self, path, create=False):
        """Open a file through the offline layer and show what it holds."""
        if not path:
            return None
        outcome = self.offline.open(path, read_only=self.chk_read_only.isChecked()
                                    and not create, create=create)
        if not outcome["ok"]:
            self._status(outcome.get("detail", ""))
            return outcome
        self.current_table = ""
        self.page = 0
        self.populate_tables()
        self.refresh_state()
        self.schema_tree.clear()
        self.schema_view.clear()
        self.grid.clear()
        self.grid.setRowCount(0)
        self.grid.setColumnCount(0)
        self.stats_tree.clear()
        self.refresh_file_list()
        self._announce("opened %s" % os.path.basename(path))
        self._status(self.t("sqlite_opened", "Opened {name} ({kind}).").format(
            name=os.path.basename(path),
            kind=OFFLINE_FORMAT_LABELS.get(outcome.get("kind", ""), "")))
        return outcome

    def create_database(self):
        """Start a new, empty database file."""
        suggested = os.path.join(self.folder_edit.text() or self._data_dir(),
                                 "new_database_%s.db" % _stamp())
        path, _filter = QtWidgets.QFileDialog.getSaveFileName(
            self, self.t("sqlite_create", "New database..."), suggested,
            "SQLite database (*.db *.sqlite)")
        if not path:
            return None
        outcome = self.open_database(path, create=True)
        if outcome and outcome.get("ok"):
            self._create_table_dialog()
        return outcome

    def close_database(self):
        outcome = self.offline.close()
        self.current_table = ""
        self.table_list.clear()
        self.grid.clear()
        self.grid.setRowCount(0)
        self.grid.setColumnCount(0)
        self.schema_tree.clear()
        self.schema_view.clear()
        self.stats_tree.clear()
        self.sql_result.clear()
        self.refresh_state()
        self._status(self.t("sqlite_closed", "Database closed."))
        return outcome

    def save_database(self):
        outcome = self.offline.save()
        self._status(outcome.get("detail", ""))
        return outcome

    def toggle_read_only(self, enabled):
        """Reopen the file read-only, or writable, through the layer."""
        if not self.offline.is_open():
            return None
        path = self.offline.path
        outcome = self.offline.open(path, read_only=enabled)
        self.refresh_state()
        self._status(self.t("sqlite_readonly_state",
                            "read-only: {state}").format(
            state="on" if enabled else "off"))
        return outcome

    def populate_tables(self):
        """Fill the table list from the open database."""
        self.table_list.clear()
        for name in self.offline.list_tables():
            item = QtWidgets.QListWidgetItem("%s  (%d rows)"
                                             % (name, self.offline.count_rows(name
                                                                              )))
            item.setData(QtCore.Qt.UserRole, name)
            self.table_list.addItem(item)
        return self.table_list.count()

    def on_table_chosen(self, current, _previous):
        """A table was clicked: show its columns, records and schema."""
        if current is None:
            return None
        self.current_table = current.data(QtCore.Qt.UserRole)
        self.page = 0
        self.load_records(reset=True)
        self.show_schema()
        self.show_statistics()
        return self.current_table

    def load_records(self, reset=False):
        """Fetch one page of rows and put them in the grid."""
        if not self.current_table:
            self._status(self.t("sqlite_choose_table",
                                "Choose a table first."))
            return None
        if reset:
            self.page = 0
        rows = self.offline.fetch_rows(
            self.current_table, limit=self.page_size,
            offset=self.page * self.page_size,
            search=self.record_search.text().strip())
        columns = self.offline.list_columns(self.current_table)
        self._fill_grid(self.grid, columns, rows, self.current_table)
        total = self.offline.count_rows(self.current_table)
        self.page_label.setText(self.t(
            "sqlite_page", "rows {first}-{last} of {total}").format(
            first=self.page * self.page_size + 1 if rows else 0,
            last=self.page * self.page_size + len(rows),
            total=total))
        self._status(self.t("sqlite_loaded_rows", "Loaded {count} row(s).").format(
            count=len(rows)))
        return rows

    def _fill_grid(self, grid, columns, rows, table):
        """One grid, filled from the layer's own column and row reports."""
        names = [column.get("name", "") for column in columns]
        grid.clear()
        grid.setColumnCount(len(names))
        grid.setHorizontalHeaderLabels(names)
        grid.setRowCount(len(rows))
        key = self.offline.primary_key(table) if table else ""
        for row_index, row in enumerate(rows):
            for column_index, name in enumerate(names):
                value = _text(row.get(name))
                item = QtWidgets.QTableWidgetItem(value)
                if name == key:
                    item.setForeground(QtGui.QColor("#8ab4f8"))
                grid.setItem(row_index, column_index, item)
            # The key is kept on the first cell, so a selected row can be
            # addressed without re-reading the table.
            if names:
                cell = grid.item(row_index, 0)
                if cell is not None:
                    cell.setData(QtCore.Qt.UserRole, row.get(key))
        grid.resizeColumnsToContents()
        return grid.rowCount()

    def _selected_key(self, grid=None):
        """The key of the selected row in a grid, or None."""
        grid = grid or self.grid
        row = grid.currentRow()
        if row < 0 or grid.columnCount() == 0:
            return None
        item = grid.item(row, 0)
        if item is None:
            return None
        return item.data(QtCore.Qt.UserRole)

    def page_by(self, direction):
        if not self.current_table:
            return None
        target = self.page + direction
        if target < 0:
            self._status(self.t("sqlite_first_page", "Already at the first page."))
            return None
        self.page = target
        return self.load_records()

    # ------------------------------------------------------------- CRUD GUI
    def _guard_write(self, action="change the database"):
        """Every write passes through here."""
        if not self.offline.is_open():
            self._status(self.t("sqlite_open_first", "Open a database first."))
            return False
        if self.offline.read_only:
            self._status(self.t("sqlite_is_read_only",
                                "The database is open read-only."))
            return False
        if not self.current_table:
            self._status(self.t("sqlite_choose_table", "Choose a table first."))
            return False
        return True

    def add_record(self):
        if not self._guard_write():
            return None
        columns = self.offline.list_columns(self.current_table)
        if not columns:
            return None
        payload = _row_dialog(self, columns, None,
                              self.offline.primary_key(self.current_table))
        if payload is None:
            return None
        outcome = self.offline.insert_row(self.current_table, payload)
        if not outcome["ok"]:
            self._status(outcome.get("detail", ""))
            return outcome
        self._announce("row inserted into %s" % self.current_table)
        self.load_records(reset=True)
        self._status(self.t("sqlite_row_added", "Row added."))
        return outcome

    def edit_record(self):
        if not self._guard_write():
            return None
        key = self._selected_key()
        if key is None:
            self._status(self.t("sqlite_select_row", "Select a row first."))
            return None
        key_column = self.offline.primary_key(self.current_table)
        rows = self.offline.fetch_rows(self.current_table, limit=1,
                                       offset=self.page * self.page_size,
                                       search=self.record_search.text().strip())
        current = None
        for row in self.offline.fetch_rows(self.current_table):
            if str(row.get(key_column)) == str(key):
                current = row
                break
        if current is None:
            self._status(self.t("sqlite_row_missing", "That row is gone."))
            return None
        columns = self.offline.list_columns(self.current_table)
        payload = _row_dialog(self, columns, current, key_column)
        if payload is None:
            return None
        outcome = self.offline.update_row(self.current_table, key, payload,
                                          key_column=key_column)
        if not outcome["ok"]:
            self._status(outcome.get("detail", ""))
            return outcome
        self._announce("row updated in %s" % self.current_table)
        self.load_records()
        self._status(self.t("sqlite_row_updated", "Row updated."))
        return outcome

    def copy_record(self):
        """Add a duplicate of the selected row, with the key left out."""
        if not self._guard_write():
            return None
        key = self._selected_key()
        if key is None:
            self._status(self.t("sqlite_select_row", "Select a row first."))
            return None
        key_column = self.offline.primary_key(self.current_table)
        for row in self.offline.fetch_rows(self.current_table):
            if str(row.get(key_column)) == str(key):
                payload = {name: value for name, value in row.items()
                           if name != key_column}
                outcome = self.offline.insert_row(self.current_table, payload)
                if outcome["ok"]:
                    self._announce("row copied in %s" % self.current_table)
                    self.load_records()
                    self._status(self.t("sqlite_row_copied", "Row copied."))
                else:
                    self._status(outcome.get("detail", ""))
                return outcome
        return None

    def delete_record(self):
        if not self._guard_write():
            return None
        key = self._selected_key()
        if key is None:
            self._status(self.t("sqlite_select_row", "Select a row first."))
            return None
        answer = QtWidgets.QMessageBox.question(
            self, self.t("sqlite_delete_row", "Delete"),
            self.t("sqlite_delete_confirm",
                   "Delete the selected row? This cannot be undone."),
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No)
        if answer != QtWidgets.QMessageBox.Yes:
            return None
        outcome = self.offline.delete_row(self.current_table, key)
        if not outcome["ok"]:
            self._status(outcome.get("detail", ""))
            return outcome
        self._announce("row deleted from %s" % self.current_table)
        self.load_records()
        self._status(self.t("sqlite_row_deleted", "Row deleted."))
        return outcome
    # ----------------------------------------------------------- schema GUI
    def show_schema(self):
        """Columns, indexes and relationships of the chosen table."""
        self.schema_tree.clear()
        if not self.current_table:
            return None
        for column in self.offline.list_columns(self.current_table):
            item = QtWidgets.QTreeWidgetItem([
                column.get("name", ""), column.get("type", ""),
                "" if column.get("notnull") else "yes",
                "primary" if column.get("primary_key") else ""])
            self.schema_tree.addTopLevelItem(item)
        for index in self.offline.indexes(self.current_table):
            item = QtWidgets.QTreeWidgetItem([
                index.get("name", ""),
                "unique" if index.get("unique") else "index",
                ", ".join(index.get("columns") or []), ""])
            item.setForeground(0, QtGui.QColor("#f7c66d"))
            self.schema_tree.addTopLevelItem(item)
        for relation in self.offline.foreign_keys(self.current_table):
            item = QtWidgets.QTreeWidgetItem([
                relation.get("column", ""), "references",
                "%s.%s" % (relation.get("target_table", ""),
                           relation.get("target_column", "")),
                relation.get("on_delete", "")])
            item.setForeground(0, QtGui.QColor("#bb9af7"))
            self.schema_tree.addTopLevelItem(item)
        self.schema_tree.expandAll()
        self.schema_view.setPlainText(
            self.offline.schema(self.current_table)
            or self.t("sqlite_no_schema",
                      "This format has no CREATE statement."))
        return self.schema_tree.topLevelItemCount()

    def _create_table_dialog(self, preset=None):
        """A small schema editor: a name and a list of typed columns."""
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle(self.t("sqlite_new_table", "New table"))
        dialog.resize(620, 480)
        layout = QtWidgets.QVBoxLayout(dialog)
        form = QtWidgets.QFormLayout()
        name_edit = QtWidgets.QLineEdit(preset or "")
        form.addRow(self.t("sqlite_col_name", "Name"), name_edit)
        layout.addLayout(form)
        editor = QtWidgets.QPlainTextEdit("id INTEGER PRIMARY KEY\n"
                                         "name TEXT\ncreated TEXT")
        editor.setFont(QFont(SQLITE_FONT, SQLITE_FONT_SIZE))
        layout.addWidget(QtWidgets.QLabel(self.t(
            "sqlite_columns_hint",
            "One column per line: name TYPE [PRIMARY KEY] [NOT NULL]")))
        layout.addWidget(editor, 1)
        buttons = QtWidgets.QHBoxLayout()
        ok_button = QtWidgets.QPushButton(self.t("sqlite_apply", "Apply"))
        ok_button.clicked.connect(dialog.accept)
        cancel = QtWidgets.QPushButton(self.t("sqlite_cancel", "Cancel"))
        cancel.clicked.connect(dialog.reject)
        buttons.addStretch()
        buttons.addWidget(cancel)
        buttons.addWidget(ok_button)
        layout.addLayout(buttons)
        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            return None
        columns = _parse_column_lines(editor.toPlainText())
        if not name_edit.text().strip() or not columns:
            self._status(self.t("sqlite_need_name",
                                "A table needs a name and at least one column."))
            return None
        outcome = self.offline.create_table(name_edit.text().strip(), columns)
        if not outcome["ok"]:
            self._status(outcome.get("detail", ""))
            return outcome
        self.populate_tables()
        self._announce("table created: %s" % name_edit.text().strip())
        self._status(self.t("sqlite_table_created", "Table {name} created.").format(
            name=name_edit.text().strip()))
        return outcome

    def create_table(self):
        if not self._guard_write():
            return None
        return self._create_table_dialog()

    def add_column(self):
        if not self._guard_write():
            return None
        text, accepted = QtWidgets.QInputDialog.getText(
            self, self.t("sqlite_add_column", "Add column"),
            self.t("sqlite_column_spec", "name TYPE (for example: note TEXT):"))
        if not accepted or not text.strip():
            return None
        pieces = text.split()
        name = pieces[0]
        declared = pieces[1] if len(pieces) > 1 else "TEXT"
        outcome = self.offline.add_column(self.current_table, name, declared)
        if not outcome["ok"]:
            self._status(outcome.get("detail", ""))
            return outcome
        self.show_schema()
        self.load_records()
        self._announce("column added to %s" % self.current_table)
        self._status(self.t("sqlite_column_added", "Column added."))
        return outcome

    def rename_table(self):
        if not self._guard_write():
            return None
        name, accepted = QtWidgets.QInputDialog.getText(
            self, self.t("sqlite_rename_table", "Rename table"),
            self.t("sqlite_new_name", "New name:"), QtWidgets.QLineEdit.Normal,
            self.current_table)
        if not accepted or not name.strip():
            return None
        outcome = self.offline.rename_table(self.current_table, name.strip())
        if not outcome["ok"]:
            self._status(outcome.get("detail", ""))
            return outcome
        self.current_table = name.strip()
        self.populate_tables()
        self.show_schema()
        self._announce("table renamed to %s" % name.strip())
        self._status(self.t("sqlite_table_renamed", "Table renamed."))
        return outcome

    def drop_table(self):
        if not self._guard_write():
            return None
        answer = QtWidgets.QMessageBox.question(
            self, self.t("sqlite_drop_table", "Drop table"),
            self.t("sqlite_drop_confirm",
                   "Drop the table {name} and every row in it?").format(
                name=self.current_table),
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No)
        if answer != QtWidgets.QMessageBox.Yes:
            return None
        if self.settings.get("auto_backup"):
            self.offline.backup(keep=int(self.backup_keep.value()))
        outcome = self.offline.drop_table(self.current_table)
        if not outcome["ok"]:
            self._status(outcome.get("detail", ""))
            return outcome
        self._announce("table dropped: %s" % self.current_table)
        self.current_table = ""
        self.populate_tables()
        self.schema_tree.clear()
        self.grid.clear()
        self.grid.setRowCount(0)
        self.grid.setColumnCount(0)
        self._status(self.t("sqlite_table_dropped", "Table dropped."))
        return outcome

    # ------------------------------------------------------------ SQL console
    def run_sql(self):
        """Run the console contents through the offline layer."""
        if not self.offline.is_open():
            self._status(self.t("sqlite_open_first", "Open a database first."))
            return None
        sql = self.sql_edit.toPlainText().strip()
        if not sql:
            self._status(self.t("sqlite_no_sql", "Write a statement first."))
            return None
        if self.chk_sql_all.isChecked():
            outcome = self.offline.execute_sql(sql)
        else:
            outcome = self.offline.execute_sql(sql, statements=[sql])
        if not outcome["ok"]:
            self._status(outcome.get("detail", ""))
            return outcome
        results = outcome.get("results") or []
        self._show_sql_results(results)
        self.populate_tables()
        if self.current_table:
            self.load_records()
        self._announce("SQL run: %d statement(s)" % len(results))
        self._status(self.t("sqlite_sql_done", "{count} statement(s) run.").format(
            count=len(results)))
        return outcome

    def _show_sql_results(self, results):
        """The result grid: the rows of the last read statement."""
        last = None
        for result in results:
            if result.get("kind") == "read":
                last = result
        self.sql_result.clear()
        if last is None:
            self.sql_result.setRowCount(0)
            self.sql_result.setColumnCount(0)
            return 0
        columns = last.get("columns") or []
        rows = last.get("rows") or []
        self.sql_result.setColumnCount(len(columns))
        self.sql_result.setHorizontalHeaderLabels(columns)
        self.sql_result.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            for column_index, name in enumerate(columns):
                self.sql_result.setItem(row_index, column_index,
                                        QtWidgets.QTableWidgetItem(
                                            _text(row.get(name))))
        self.sql_result.resizeColumnsToContents()
        return len(rows)

    def explain_sql(self):
        """The query plan for the statement in the console."""
        sql = self.sql_edit.toPlainText().strip().split(";")[0].strip()
        if not sql:
            self._status(self.t("sqlite_no_sql", "Write a statement first."))
            return None
        outcome = self.offline.explain(sql)
        if not outcome["ok"]:
            self._status(outcome.get("detail", ""))
            return outcome
        lines = ["%s" % sql, ""]
        for step in outcome.get("plan") or []:
            lines.append(" | ".join(_text(value) for value in step.values()))
        _dialog(self, self.t("sqlite_explain", "Query plan"), "\n".join(lines))
        return outcome

    # ------------------------------------------------------------ statistics
    def show_statistics(self):
        """Per-column numbers for the chosen table, in the statistics panel."""
        self.stats_tree.clear()
        if not self.current_table:
            return None
        outcome = self.offline.table_stats(self.current_table)
        if not outcome["ok"]:
            return outcome
        root = QtWidgets.QTreeWidgetItem(["%s  (%d rows)"
                                          % (outcome["table"], outcome["rows"]),
                                          "", ""])
        root.setForeground(0, QtGui.QColor("#8ab4f8"))
        self.stats_tree.addTopLevelItem(root)
        for column in outcome["columns"]:
            item = QtWidgets.QTreeWidgetItem([column["name"],
                                              _text(column.get("type")),
                                              ""])
            root.addChild(item)
            for key in ("kind", "filled", "empty", "distinct", "min", "max",
                        "average"):
                if key in column:
                    item.addChild(QtWidgets.QTreeWidgetItem([key,
                                                             _text(column[key]),
                                                             ""]))
        self.stats_tree.expandAll()
        return outcome

    # --------------------------------------------------------- online actions
    def online_login(self):
        """Sign in through the online layer and remember nothing but the token."""
        outcome = self.online.login(self.server_edit.text().strip(),
                                    self.user_edit.text().strip(),
                                    self.password_edit.text(),
                                    api_base=self.api_edit.text().strip())
        self.password_edit.clear()          # never left in the widget
        self.refresh_state()
        if not outcome["ok"]:
            self._status(outcome.get("detail", ""))
            return outcome
        self.sync.configure(table=self.sync_table.text().strip(),
                            policy=self.sync_policy.currentData())
        self._announce("online session opened as %s" % self.online.user)
        self._status(self.t("sqlite_online_welcome", "Signed in as {user}.").format(
            user=self.online.user))
        return outcome

    def online_logout(self):
        outcome = self.online.logout()
        self.refresh_state()
        self.online_tables.clear()
        self.online_grid.clear()
        self.online_grid.setRowCount(0)
        self.online_grid.setColumnCount(0)
        self.console_view.clear()
        self._status(self.t("sqlite_online_bye", "Signed out."))
        return outcome

    def online_ping(self):
        outcome = self.online.ping()
        self._status(outcome.get("detail", ""))
        return outcome

    def online_refresh(self):
        outcome = self.online.refresh()
        self.refresh_state()
        self._status(outcome.get("detail", ""))
        return outcome

    def load_online_tables(self):
        outcome = self.online.list_tables()
        if not outcome["ok"]:
            self._status(outcome.get("detail", ""))
            return outcome
        self.online_tables.clear()
        for name in outcome.get("tables") or []:
            item = QtWidgets.QListWidgetItem(str(name))
            item.setData(QtCore.Qt.UserRole, str(name))
            self.online_tables.addItem(item)
        self._status(self.t("sqlite_online_tables_loaded",
                            "{count} table(s) on the server.").format(
            count=len(outcome.get("tables") or [])))
        return outcome

    def on_online_table(self, current, _previous):
        if current is None:
            return None
        name = current.data(QtCore.Qt.UserRole)
        self.sync_table.setText(str(name))
        self.sync.configure(table=str(name),
                            policy=self.sync_policy.currentData())
        return self.load_online_rows()

    def load_online_rows(self):
        table = self._online_table()
        if not table:
            return None
        columns = self.online.list_columns(table)
        outcome = self.online.fetch_rows(table, limit=self.page_size)
        if not outcome["ok"]:
            self._status(outcome.get("detail", ""))
            return outcome
        names = [column.get("name") for column in
                 (columns.get("columns") or [])]
        self._fill_online_grid(names, outcome.get("rows") or [])
        self._status(self.t("sqlite_online_rows_loaded",
                            "{count} row(s) from the server.").format(
            count=len(outcome.get("rows") or [])))
        return outcome

    def _fill_online_grid(self, names, rows):
        if not names and rows:
            names = sorted(rows[0])
        self.online_grid.clear()
        self.online_grid.setColumnCount(len(names))
        self.online_grid.setHorizontalHeaderLabels(names)
        self.online_grid.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            for column_index, name in enumerate(names):
                self.online_grid.setItem(row_index, column_index,
                                         QtWidgets.QTableWidgetItem(
                                             _text(row.get(name))))
            if names:
                cell = self.online_grid.item(row_index, 0)
                if cell is not None:
                    cell.setData(QtCore.Qt.UserRole, row.get(names[0]))
        self.online_grid.resizeColumnsToContents()
        return len(rows)

    def _online_table(self):
        item = self.online_tables.currentItem()
        if item is not None:
            return item.data(QtCore.Qt.UserRole)
        return self.sync_table.text().strip()

    def online_crud(self, action):
        """Create, update or delete a row through the online layer."""
        table = self._online_table()
        if not table:
            self._status(self.t("sqlite_choose_table", "Choose a table first."))
            return None
        if action == "create":
            payload = _ask_json(self, self.t("sqlite_online_add", "Create row"))
            if payload is None:
                return None
        elif action in ("update", "delete"):
            key = self._selected_key(self.online_grid)
            if key is None:
                self._status(self.t("sqlite_select_row", "Select a row first."))
                return None
            payload = None
            if action == "update":
                payload = _ask_json(self, self.t("sqlite_online_update",
                                                 "Update row"))
                if payload is None:
                    return None
        else:
            return None
        outcome = self.online.crud(table, action, payload, key=locals().get("key"))
        if not outcome["ok"]:
            self._status(outcome.get("detail", ""))
            return outcome
        self._announce("online %s on %s" % (action, table))
        self.load_online_rows()
        self._status(self.t("sqlite_online_done", "{action} done.").format(
            action=action))
        return outcome

    def api_console_send(self):
        """Send the console request and show exactly what came back."""
        body = None
        raw = self.console_body.text().strip()
        if raw:
            try:
                body = json.loads(raw)
            except ValueError as exc:
                self.console_view.setPlainText("Body is not valid JSON: %s" % exc)
                return None
        text = self.online.console_lines(
            self.console_method.currentData(), self.console_path.text().strip(),
            body, token=self.chk_console_token.isChecked())
        self.console_view.setPlainText(text)
        self._status(self.t("sqlite_console_sent", "Console request sent."))
        return text
    # ---------------------------------------------------------- sync actions
    def _sync_ready(self):
        """Configure the engine from the panel and check both sides are ready."""
        self.sync.configure(table=self.sync_table.text().strip(),
                            key_column=self.sync_key.text().strip() or "id",
                            policy=self.sync_policy.currentData())
        readiness = self.sync.is_ready()
        if not readiness["ok"]:
            self._status(readiness.get("detail", ""))
        return readiness["ok"]

    def sync_compare(self):
        if not self._sync_ready():
            return None
        outcome = self.sync.compare()
        if not outcome["ok"]:
            self._status(outcome.get("detail", ""))
            return outcome
        self._fill_sync_tree(outcome)
        self.sync_summary.setText(outcome.get("detail", ""))
        self._status(outcome.get("detail", ""))
        return outcome

    def _fill_sync_tree(self, outcome):
        """One row per difference, with both sides shown."""
        self.sync_tree.clear()
        for item in outcome.get("differences") or []:
            offline = item.get("offline") or {}
            online = item.get("online") or {}
            entry = QtWidgets.QTreeWidgetItem([
                str(item.get("key")), item.get("kind", ""),
                _text(offline.get("name") or offline.get("title") or ""),
                _text(online.get("name") or online.get("title") or "")])
            entry.setData(0, QtCore.Qt.UserRole, item)
            colour = {"changed_both": "#f7768e", "added_offline": "#8ab4f8",
                      "added_online": "#9ece6a"}.get(item.get("kind"))
            if colour:
                entry.setForeground(1, QtGui.QColor(colour))
            for column in item.get("columns") or []:
                entry.addChild(QtWidgets.QTreeWidgetItem([
                    column, "changed",
                    _text(offline.get(column)), _text(online.get(column))]))
            self.sync_tree.addTopLevelItem(entry)
        self.sync_tree.expandAll()
        return self.sync_tree.topLevelItemCount()

    def sync_plan(self):
        """Show what the chosen mode would do, changing nothing."""
        if not self._sync_ready():
            return None
        outcome = self.sync.plan(self.sync_mode.currentData(),
                                 self.sync_policy.currentData())
        if not outcome["ok"]:
            self._status(outcome.get("detail", ""))
            return outcome
        lines = ["%s -> %s" % (self.t("sqlite_sync_mode", "Mode"),
                               SYNC_MODE_LABELS.get(outcome["mode"], "")),
                 "%s -> %s" % (self.t("sqlite_sync_policy", "Conflicts"),
                               CONFLICT_POLICY_LABELS.get(outcome["policy"], "")),
                 "", outcome.get("detail", ""), ""]
        for step in outcome["steps"]:
            lines.append("  %-8s %-8s key=%s  [%s]"
                         % (step["action"], step["target"], step["key"],
                            step["kind"]))
        for skip in outcome["skipped"]:
            lines.append("  skipped  key=%-8s %s" % (skip["key"],
                                                     skip["reason"]))
        self.sync_summary.setText(outcome.get("detail", ""))
        _dialog(self, self.t("sqlite_sync_plan", "Sync plan"), "\n".join(lines))
        self._status(outcome.get("detail", ""))
        return outcome

    def sync_run(self):
        """Perform the sync and report what each step did."""
        if not self._sync_ready():
            return None
        mode = self.sync_mode.currentData()
        answer = QtWidgets.QMessageBox.question(
            self, self.t("sqlite_sync_run", "Run the sync"),
            self.t("sqlite_sync_confirm",
                   "Run a '{mode}' sync on {table}?\n\n"
                   "The plan is built first, then applied.").format(
                mode=SYNC_MODE_LABELS.get(mode, mode),
                table=self.sync_table.text().strip()),
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.Yes)
        if answer != QtWidgets.QMessageBox.Yes:
            return None
        outcome = self.sync.run(mode, self.sync_policy.currentData())
        if not outcome["ok"]:
            self._status(outcome.get("detail", ""))
            return outcome
        lines = [outcome.get("detail", ""), ""]
        for record in outcome["applied"]:
            lines.append("  applied  %-8s %-8s key=%s"
                         % (record["action"], record["target"], record["key"]))
        for record in outcome["failed"]:
            lines.append("  failed   %-8s %-8s key=%s  %s"
                         % (record["action"], record["target"], record["key"],
                            record["detail"]))
        for skip in outcome["skipped"]:
            lines.append("  skipped  key=%s  %s" % (skip["key"], skip["reason"]))
        _dialog(self, self.t("sqlite_sync_result", "Sync result"),
                "\n".join(lines))
        self._announce("sync run: %s" % outcome.get("detail", ""))
        self._status(outcome.get("detail", ""))
        self.sync_compare()
        return outcome

    def sync_resolve(self):
        """Apply a decision to the selected conflicts."""
        items = [item.data(0, QtCore.Qt.UserRole)
                 for item in self.sync_tree.selectedItems()
                 if item.data(0, QtCore.Qt.UserRole)]
        if not items:
            self._status(self.t("sqlite_sync_select",
                                "Select a conflict first."))
            return None
        label, accepted = QtWidgets.QInputDialog.getItem(
            self, self.t("sqlite_sync_resolve", "Resolve"),
            self.t("sqlite_sync_side", "Which side wins?"),
            [self.t("sqlite_side_offline", "Offline"),
             self.t("sqlite_side_online", "Online"),
             self.t("sqlite_side_skip", "Skip")], 0, False)
        if not accepted:
            return None
        side = {"Offline": "offline", "Online": "online"}.get(label, "skip")
        if label == self.t("sqlite_side_offline", "Offline"):
            side = "offline"
        elif label == self.t("sqlite_side_online", "Online"):
            side = "online"
        else:
            side = "skip"
        applied = 0
        for item in items:
            outcome = self.sync.resolve(item, side)
            if outcome.get("ok"):
                applied += 1
        self._announce("conflicts resolved: %d" % applied)
        self._status(self.t("sqlite_sync_resolved",
                            "Resolved {count} conflict(s).").format(
            count=applied))
        self.sync_compare()
        return applied

    def sync_report(self):
        text = self.sync.change_report()
        _dialog(self, self.t("sqlite_sync_report", "Change report"), text)
        return text

    def sync_save_report(self):
        """Write the report so the Reports tab can index it."""
        path, _filter = QtWidgets.QFileDialog.getSaveFileName(
            self, self.t("sqlite_sync_save_report", "Save the report"),
            os.path.join(self._data_dir(), "reports",
                         "sqlite_sync_%s.md" % _stamp()),
            "Markdown (*.md)")
        if not path:
            return None
        try:
            folder = os.path.dirname(path)
            if folder and not os.path.isdir(folder):
                os.makedirs(folder)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(self.sync.change_report())
        except OSError as exc:
            self._status(str(exc))
            return None
        reports = getattr(self.window, "reports_tab", None)
        if reports is not None and hasattr(reports, "refresh"):
            try:
                reports.refresh()
            except Exception:
                pass
        self._status(self.t("sqlite_report_saved", "Report saved: {path}").format(
            path=path))
        return path
    # --------------------------------------------------------- tool actions
    def run_tool(self, key):
        """Dispatch one of the tools and report the outcome in the panel."""
        outcomes = {
            "backup": self.tool_backup,
            "restore": self.tool_restore,
            "integrity": self.tool_integrity,
            "repair": self.tool_repair,
            "compact": self.tool_compact,
            "import": self.tool_import,
            "export": self.tool_export,
            "duplicates": self.tool_duplicates,
            "validate": self.tool_validate,
            "stats": self.tool_stats,
        }
        handler = outcomes.get(key)
        if handler is None:
            return None
        if not self.offline.is_open():
            self._status(self.t("sqlite_open_first", "Open a database first."))
            return None
        return handler()

    def _report_lines(self, text):
        """Append a line to the Tools panel and to the status."""
        self.tools_view.appendPlainText(text)
        self._status(text)
        return text

    def tool_backup(self):
        keep = int(self.backup_keep.value())
        outcome = self.offline.backup(keep=keep)
        if outcome["ok"]:
            self._announce("backup written: %s" % outcome["path"])
            self._report_lines(self.t("sqlite_backup_done",
                                      "Backup written: {path} (kept {keep})")
                               .format(path=outcome["path"], keep=keep))
        else:
            self._report_lines(outcome.get("detail", ""))
        return outcome

    def tool_restore(self):
        path, _filter = QtWidgets.QFileDialog.getOpenFileName(
            self, self.t("sqlite_tool_restore", "Restore a backup"),
            os.path.dirname(self.offline.path),
            "Databases (*.db *.sqlite *.sqlite3 *.json *.csv);;All files (*.*)")
        if not path:
            return None
        answer = QtWidgets.QMessageBox.question(
            self, self.t("sqlite_tool_restore", "Restore a backup"),
            self.t("sqlite_restore_confirm",
                   "Replace the open database with this backup?\n\n{path}\n\n"
                   "A safety copy of the current file is kept.").format(
                path=path),
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No)
        if answer != QtWidgets.QMessageBox.Yes:
            return None
        outcome = self.offline.restore(path)
        if outcome["ok"]:
            self.populate_tables()
            self.refresh_state()
            self._report_lines(self.t("sqlite_restored",
                                      "Restored. Safety copy: {path}").format(
                path=outcome.get("safety", "")))
        else:
            self._report_lines(outcome.get("detail", ""))
        return outcome

    def tool_integrity(self):
        outcome = self.offline.integrity_check()
        lines = [outcome.get("detail", "")]
        for finding in outcome.get("findings") or []:
            lines.append("  %-16s %-8s %s" % (finding["check"],
                                              finding["state"],
                                              finding["detail"]))
        self._report_lines("\n".join(lines))
        return outcome

    def tool_repair(self):
        if self.offline.read_only:
            self._report_lines(self.t("sqlite_is_read_only",
                                      "The database is open read-only."))
            return None
        outcome = self.offline.repair()
        self._report_lines(outcome.get("detail", ""))
        return outcome

    def tool_compact(self):
        path, _filter = QtWidgets.QFileDialog.getSaveFileName(
            self, self.t("sqlite_tool_compact", "Write a compacted copy"),
            os.path.join(os.path.dirname(self.offline.path),
                         "compact_%s.db" % _stamp()),
            "SQLite database (*.db)")
        if not path:
            return None
        outcome = self.offline.vacuum_into(path)
        self._report_lines(outcome.get("detail", ""))
        return outcome

    def tool_import(self):
        if self.offline.read_only:
            self._report_lines(self.t("sqlite_is_read_only",
                                      "The database is open read-only."))
            return None
        path, _filter = QtWidgets.QFileDialog.getOpenFileName(
            self, self.t("sqlite_tool_import", "Import a file"), "",
            "Data files (*.json *.csv *.txt);;All files (*.*)")
        if not path:
            return None
        outcome = self.offline.import_file(path, table=self.current_table)
        if outcome["ok"]:
            self.populate_tables()
            self._announce("imported from %s" % path)
            self._report_lines(self.t("sqlite_imported",
                                      "Imported {rows} row(s) into {table}.")
                               .format(rows=outcome.get("rows", 0),
                                       table=outcome.get("table", "")))
        else:
            self._report_lines(outcome.get("detail", ""))
        return outcome

    def tool_export(self):
        path, _filter = QtWidgets.QFileDialog.getSaveFileName(
            self, self.t("sqlite_tool_export", "Export the database"),
            os.path.join(os.path.dirname(self.offline.path),
                         "export_%s.json" % _stamp()),
            "JSON (*.json);;CSV (*.csv);;SQL (*.sql)")
        if not path:
            return None
        outcome = self.offline.export(path, table=self.current_table)
        if outcome["ok"]:
            self._announce("exported to %s" % path)
            self._report_lines(self.t("sqlite_exported",
                                      "Exported {count} table(s) to {path}.")
                               .format(count=len(outcome.get("tables") or []),
                                       path=path))
        else:
            self._report_lines(outcome.get("detail", ""))
        return outcome

    def tool_duplicates(self):
        if not self.current_table:
            self._report_lines(self.t("sqlite_choose_table",
                                      "Choose a table first."))
            return None
        outcome = self.offline.duplicates(self.current_table)
        lines = [outcome.get("detail", ""), ""]
        for group in (outcome.get("groups") or [])[:20]:
            lines.append("  %s  x%d" % (group["key"], group["count"]))
        self._report_lines("\n".join(lines))
        return outcome

    def tool_validate(self):
        if not self.current_table:
            self._report_lines(self.t("sqlite_choose_table",
                                      "Choose a table first."))
            return None
        outcome = self.offline.validate_table(self.current_table)
        lines = [outcome.get("detail", ""),
                 "  key: %s" % (outcome.get("key") or "(none)")]
        for issue in outcome.get("issues") or []:
            lines.append("  %-14s %s" % (issue["kind"], issue["detail"]))
        self._report_lines("\n".join(lines))
        return outcome

    def tool_stats(self):
        if not self.current_table:
            self._report_lines(self.t("sqlite_choose_table",
                                      "Choose a table first."))
            return None
        outcome = self.offline.table_stats(self.current_table)
        if not outcome["ok"]:
            self._report_lines(outcome.get("detail", ""))
            return outcome
        lines = ["%s: %d row(s)" % (outcome["table"], outcome["rows"])]
        for column in outcome["columns"]:
            lines.append("  %-16s %-8s filled=%d empty=%d distinct=%d"
                         % (column["name"], column.get("type", ""),
                            column["filled"], column["empty"],
                            column["distinct"]))
        self._report_lines("\n".join(lines))
        self.show_statistics()
        return outcome

    # ----------------------------------------------------------- logs panel
    def merged_log(self):
        """Every layer log as one list, newest first."""
        rows = []
        for entry in self.offline.log_rows():
            rows.append(("offline", entry))
        for entry in self.online.log_rows():
            rows.append(("online", {"time": entry.get("time", ""),
                                    "action": "%s %s" % (entry.get("method", ""),
                                                         entry.get("status", "")),
                                    "detail": entry.get("url", "")}))
        for entry in self.sync.log_rows():
            rows.append(("sync", entry))
        rows.sort(key=lambda item: item[1].get("time", ""), reverse=True)
        needle = self.log_filter.text().strip().lower()
        if needle:
            rows = [item for item in rows
                    if needle in json.dumps(item, default=str).lower()]
        return rows

    def refresh_logs(self):
        self.log_tree.clear()
        for layer, entry in self.merged_log()[:400]:
            item = QtWidgets.QTreeWidgetItem([
                entry.get("time", ""), layer, entry.get("action", ""),
                _text(entry.get("detail", ""), 160)])
            colour = {"offline": "#8ab4f8", "online": "#9ece6a",
                      "sync": "#f7c66d"}.get(layer)
            if colour:
                item.setForeground(1, QtGui.QColor(colour))
            self.log_tree.addTopLevelItem(item)
        self._status(self.t("sqlite_log_count", "{count} log entry(s).").format(
            count=self.log_tree.topLevelItemCount()))
        return self.log_tree.topLevelItemCount()

    def save_log(self):
        path, _filter = QtWidgets.QFileDialog.getSaveFileName(
            self, self.t("sqlite_log_save", "Save the log"),
            os.path.join(self._data_dir(), "sqlite_log_%s.txt" % _stamp()),
            "Text (*.txt)")
        if not path:
            return None
        lines = ["%-20s %-8s %-14s %s" % ("time", "layer", "action", "detail")]
        for layer, entry in self.merged_log():
            lines.append("%-20s %-8s %-14s %s"
                         % (entry.get("time", ""), layer,
                            entry.get("action", ""), entry.get("detail", "")))
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("\n".join(lines) + "\n")
        except OSError as exc:
            self._status(str(exc))
            return None
        self._status(self.t("sqlite_log_saved", "Log saved: {path}").format(
            path=path))
        return path

    # ------------------------------------------------------------ translation
    def retranslate(self, table):
        """Apply the main window language table to this tab."""
        def label(key, fallback):
            return table.get(key, fallback)

        self.title_label.setText(label("sqlite", "SQLITE"))
        self.sections.setTabText(0, label("sqlite_tab_offline",
                                          "Offline database"))
        self.sections.setTabText(1, label("sqlite_tab_online", "Online database"))
        self.sections.setTabText(2, label("sqlite_tab_sync", "Sync"))
        self.sections.setTabText(3, label("sqlite_tab_tools", "Tools"))
        self.sections.setTabText(4, label("sqlite_tab_logs", "Logs"))
        self.sections.setTabText(5, label("sqlite_tab_settings", "Settings"))
        self.side_tabs.setTabText(0, label("sqlite_records", "Records"))
        self.side_tabs.setTabText(1, label("sqlite_schema", "Schema"))
        self.side_tabs.setTabText(2, label("sqlite_sql", "SQL"))
        self.side_tabs.setTabText(3, label("sqlite_stats", "Statistics"))
        for key, label_key, fallback in SQLITE_TOOLS:
            if key in self.tool_buttons:
                self.tool_buttons[key].setText(label(label_key, fallback))
        for index, (key, label_key, fallback, detail) in enumerate(SYNC_MODES):
            self.sync_mode.setItemText(index, label(label_key, fallback))
            self.sync_mode.setItemData(index, detail, QtCore.Qt.ToolTipRole)
        for index, (key, label_key, fallback) in enumerate(CONFLICT_POLICIES):
            self.sync_policy.setItemText(index, label(label_key, fallback))
        self.refresh_state()


def _parse_column_lines(text):
    """Parse the schema editor text into column definitions."""
    columns = []
    for raw in (text or "").split("\n"):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        pieces = line.split()
        name = pieces[0]
        declared = pieces[1].upper() if len(pieces) > 1 else "TEXT"
        upper = line.upper()
        columns.append({"name": name, "type": declared,
                        "primary_key": "PRIMARY KEY" in upper,
                        "notnull": "NOT NULL" in upper})
    return columns


def _ask_json(parent, title):
    """Ask for a JSON object, returning it or None."""
    text, accepted = QtWidgets.QInputDialog.getMultiLineText(
        parent, title, "JSON row:", "{\n  \n}")
    if not accepted or not text.strip():
        return None
    try:
        payload = json.loads(text)
    except ValueError as exc:
        QtWidgets.QMessageBox.warning(parent, title,
                                      "That is not valid JSON: %s" % exc)
        return None
    return payload if isinstance(payload, dict) else None
