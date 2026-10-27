# -*- coding: utf-8 -*-
"""The Metadata tab: read, edit and validate the metadata of every script.

This is the interface for :mod:`tabs.metadata_core`.  The engine already knows
how to scan a folder, parse and render a header, validate a record and write its
metadata back safely; this module adds the part a person actually uses -- a
table of what was found, a form for the selected record, and the buttons that
turn the form into a write.

The tab owns nothing the engine owns.  It keeps one :class:`MetadataStore` for
the lifetime of the tab, shows that store's own records, and performs every
change through the store's own methods (``save``, ``bulk_set``, ``undo``), so
the audit trail and the undo stack can never disagree with what is on screen.

The pattern is the one every other tab here follows:

    class MetadataTab(QtWidgets.QWidget)
        __init__(self, window)   the main window, for translations and the
                                 folder the scripts came from
        t(key, fallback)         every label goes through the window's table, so
                                 the tab is translated with the rest of the app
        _build_ui()              the widgets, built once

The store is given the folder the application is working in.  A scan is only
ever run when the user asks for one, because a scan walks the disk.
"""

import os

from PyQt5 import QtCore, QtGui, QtWidgets

from . import metadata_core
from .metadata_core import (METADATA_CHOICES, METADATA_FIELD_KINDS,
                            METADATA_FIELD_LABELS, METADATA_FIELD_KEYS,
                            METADATA_ROLE_LABELS, MetadataStore)


#: The font the code previews and the header sample use, as the rest of the
#: application does.
MONO_FONT = "Consolas"

#: How wide a metadata field may be.  A value like a version number or a tag
#: list does not need a box running the width of the window; capping the width
#: keeps the form readable and saves horizontal space the other tabs can use.
FIELD_MAX_WIDTH = 420

#: The height of a long field (a description, a dependency list).  These are
#: genuinely several lines, so they keep a normal box height.
FIELD_LONG_HEIGHT = 64


class MetadataTab(QtWidgets.QWidget):
    """Every script under a root, with its metadata, and the edits to it.

    The tab is a thin layer over :class:`metadata_core.MetadataStore`: the table
    shows the store's records, the form edits one record's values, and the
    buttons call the store's methods.  Nothing is written to disk by any control
    other than Save and the bulk operations, and every one of those leaves an
    undo entry behind.
    """

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        # One store for the life of the tab.  The root is set from the folder the
        # application is working in, so the tab and the Scripts list look at the
        # same place by default.
        self.store = MetadataStore(self._default_root())
        self.records = []            # the last filtered, sorted list shown
        self._fields = {}            # field key -> editor widget for the form
        self._loading = False        # true while the form is being filled
        self._build_ui()

    # --------------------------------------------------------------- helpers
    def t(self, key, fallback=None, **kwargs):
        """Look a label up in the main window's translations."""
        try:
            return self.window.t(key, fallback, **kwargs)
        except Exception:
            return fallback or key

    def _default_root(self):
        """The folder to scan: wherever the scripts the app shows live."""
        for name in ("base_dir", "BASE_DIR"):
            base = getattr(self.window, name, None)
            if base and os.path.isdir(base):
                return base
        # Fall back to the project folder this file sits in, which is where the
        # application's own scripts are when it runs from source.
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def _role_key(self):
        """The role the tab is acting as, from the window when it has one."""
        role = getattr(self.window, "metadata_role", None)
        return role if role in metadata_core.METADATA_ROLE_KEYS else "editor"

    # ------------------------------------------------------------------- UI
    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)

        # --- header: title, folder and the role the tab writes as ---------
        header = QtWidgets.QHBoxLayout()
        self.title_label = QtWidgets.QLabel(self.t("metadata", "Metadata"))
        self.title_label.setStyleSheet(
            "font-size: 14px; font-weight: bold; color: #8ab4f8;")
        header.addWidget(self.title_label)

        header.addWidget(QtWidgets.QLabel("Folder:"))
        self.folder_edit = QtWidgets.QLineEdit(self.store.root)
        self.folder_edit.setReadOnly(True)
        self.folder_edit.setStyleSheet("color: #9aa0a6;")
        header.addWidget(self.folder_edit, 1)

        header.addWidget(QtWidgets.QLabel("Role:"))
        self.role_combo = QtWidgets.QComboBox()
        for key in metadata_core.METADATA_ROLE_KEYS:
            self.role_combo.addItem(METADATA_ROLE_LABELS.get(key, key), key)
        index = self.role_combo.findData(self._role_key())
        self.role_combo.setCurrentIndex(index if index >= 0 else 0)
        self.role_combo.currentIndexChanged.connect(self._on_role_changed)
        header.addWidget(self.role_combo)
        layout.addLayout(header)

        # --- controls: scan, filter and the summary line ------------------
        controls = QtWidgets.QHBoxLayout()
        self.scan_button = QtWidgets.QPushButton(
            self.t("meta_detect", "Scan for metadata"))
        self.scan_button.clicked.connect(self.scan)
        controls.addWidget(self.scan_button)

        controls.addWidget(QtWidgets.QLabel(self.t("meta_filter", "Filter:")))
        self.filter_edit = QtWidgets.QLineEdit()
        self.filter_edit.setPlaceholderText(self.t(
            "meta_filter_hint", "Filter by name, author, description..."))
        self.filter_edit.textChanged.connect(self.refresh)
        controls.addWidget(self.filter_edit, 1)

        self.invalid_only = QtWidgets.QCheckBox(
            self.t("meta_show_all", "Only records with errors"))
        self.invalid_only.stateChanged.connect(self.refresh)
        controls.addWidget(self.invalid_only)
        layout.addLayout(controls)

        # The table takes the top half; the form and the findings take the rest.
        splitter = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        splitter.addWidget(self._build_table())
        # The form is put in a scroll area: when it is taller than the space
        # the window gives it, the wheel scrolls it, so every field keeps its
        # normal height and nothing is squeezed to fit or cut off.
        self.detail_scroll = QtWidgets.QScrollArea()
        self.detail_scroll.setWidget(self._build_detail())
        self.detail_scroll.setWidgetResizable(True)
        self.detail_scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.detail_scroll.setHorizontalScrollBarPolicy(
            QtCore.Qt.ScrollBarAsNeeded)
        self.detail_scroll.setVerticalScrollBarPolicy(
            QtCore.Qt.ScrollBarAsNeeded)
        splitter.addWidget(self.detail_scroll)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        layout.addWidget(splitter, 1)

        self.summary_label = QtWidgets.QLabel("")
        self.summary_label.setStyleSheet("color: #9aa0a6;")
        layout.addWidget(self.summary_label)

    def _build_table(self):
        """The list of records: one row per script, sorted and filterable."""
        self.table = QtWidgets.QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels([
            self.t("meta_language", "File"), "Language",
            self.t("meta_version", "Version"), self.t("meta_author", "Author"),
            self.t("meta_status", "Status")])
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(
            QtWidgets.QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        return self.table

    def _build_detail(self):
        """The form for one record, the findings, and the write buttons."""
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)

        columns = QtWidgets.QHBoxLayout()

        # --- the form: one editor per field -------------------------------
        # The fields are capped in width: a value like a version number does
        # not need a box stretching across the window, and the form is easier to
        # read when the labels and values line up in a column.
        form_box = QtWidgets.QGroupBox(self.t("meta_load", "Selected record"))
        form_layout = QtWidgets.QFormLayout(form_box)
        form_layout.setSpacing(5)
        form_layout.setFieldGrowthPolicy(
            QtWidgets.QFormLayout.AllNonFixedFieldsGrow)
        form_layout.setLabelAlignment(QtCore.Qt.AlignRight)

        for key in METADATA_FIELD_KEYS:
            kind = METADATA_FIELD_KINDS.get(key, "text")
            label = METADATA_FIELD_LABELS.get(key, key)
            if kind == "choice":
                editor = QtWidgets.QComboBox()
                editor.addItem("", "")
                for value, _lk, choice_label in METADATA_CHOICES.get(key, ()):
                    editor.addItem(choice_label, value)
                editor.setMaximumWidth(FIELD_MAX_WIDTH)
            elif kind == "multiline":
                # A description or a dependency list is genuinely several lines,
                # so it keeps the normal box height rather than one row.
                editor = QtWidgets.QPlainTextEdit()
                editor.setFixedHeight(FIELD_LONG_HEIGHT)
                editor.setMaximumWidth(FIELD_MAX_WIDTH * 2)
            else:
                editor = QtWidgets.QLineEdit()
                editor.setMaximumWidth(FIELD_MAX_WIDTH)
            self._fields[key] = editor
            form_layout.addRow(label, editor)

        # The form is as wide as the widest field, not the window: the wrapper
        # below keeps it to the left instead of stretching it.
        form_wrapper = QtWidgets.QWidget()
        form_wrapper_layout = QtWidgets.QHBoxLayout(form_wrapper)
        form_wrapper_layout.setContentsMargins(0, 0, 0, 0)
        form_wrapper_layout.addWidget(form_box)
        columns.addWidget(form_wrapper, 3)


        # --- the findings: what validation says about this record ---------
        findings_box = QtWidgets.QGroupBox("Findings")
        findings_layout = QtWidgets.QVBoxLayout(findings_box)
        self.findings_view = QtWidgets.QPlainTextEdit()
        self.findings_view.setReadOnly(True)
        self.findings_view.setFont(QtGui.QFont(MONO_FONT, 9))
        findings_layout.addWidget(self.findings_view)
        columns.addWidget(findings_box, 2)

        layout.addLayout(columns)

        # --- the buttons: every write goes through the store --------------
        buttons = QtWidgets.QHBoxLayout()
        self.save_button = QtWidgets.QPushButton(
            self.t("meta_apply", "Save to file"))
        self.save_button.clicked.connect(self.save_current)
        buttons.addWidget(self.save_button)

        self.load_button = QtWidgets.QPushButton(
            self.t("meta_load", "Load into form"))
        self.load_button.clicked.connect(self.load_current)
        buttons.addWidget(self.load_button)

        self.bump_button = QtWidgets.QPushButton(
            self.t("meta_bump", "Bump version"))
        self.bump_button.clicked.connect(self.bump_version)
        buttons.addWidget(self.bump_button)

        self.undo_button = QtWidgets.QPushButton("Undo")
        self.undo_button.clicked.connect(self.undo)
        buttons.addWidget(self.undo_button)

        buttons.addStretch(1)

        self.tag_edit = QtWidgets.QLineEdit()
        self.tag_edit.setPlaceholderText("tag")
        self.tag_edit.setFixedWidth(120)
        buttons.addWidget(self.tag_edit)

        self.tag_button = QtWidgets.QPushButton("Add tag to selected")
        self.tag_button.clicked.connect(self.add_tag_to_selection)
        buttons.addWidget(self.tag_button)

        layout.addLayout(buttons)
        return page

    # --------------------------------------------------------------- actions
    def scan(self):
        """Walk the folder and read every script's metadata.

        A scan touches the disk, so it only ever runs when the user asks.  The
        summary is shown rather than the records: the records are held in the
        store, and the table reads them back through :meth:`refresh`.
        """
        result = self.store.scan()
        if not result.get("ok"):
            self._say(result.get("error", "the folder could not be scanned"))
            return
        self.refresh()
        self._say("%d script(s) scanned, %d valid"
                  % (len(self.store.records),
                     self.store.summary().get("valid", 0)))

    def refresh(self):
        """Rebuild the table from the store, applying the filters."""
        needle = self.filter_edit.text().strip()
        self.records = self.store.records_list(
            needle=needle,
            only_invalid=self.invalid_only.isChecked(),
            sort="name")
        self.table.setRowCount(0)
        for record in self.records:
            values = record.get("values") or {}
            row = self.table.rowCount()
            self.table.insertRow(row)
            # A record that fails validation is marked, so a problem is visible
            # in the list without opening the record.
            name_item = QtWidgets.QTableWidgetItem(
                ("! " if not record.get("valid") else "") + record["name"])
            if not record.get("valid"):
                name_item.setForeground(QtGui.QColor("#f28b82"))
            self.table.setItem(row, 0, name_item)
            for column, text in ((1, record.get("language_label", "")),
                                 (2, values.get("version", "")),
                                 (3, values.get("author", "")),
                                 (4, values.get("status", ""))):
                self.table.setItem(row, column, QtWidgets.QTableWidgetItem(
                    str(text or "")))
        self._update_summary()

    def load_current(self):
        """Put the selected record's values into the form."""
        record = self._selected_record()
        if record is None:
            self._say(self.t("meta_select_hint", "Select a script first."))
            return
        self._fill_form(record)

    def save_current(self):
        """Write the form back to the selected record's file."""
        record = self._selected_record()
        if record is None:
            self._say(self.t("meta_select_hint", "Select a script first."))
            return
        values = self._read_form()
        result = self.store.save(record["path"], values,
                                 record.get("custom"), store=record.get("store",
                                                                       "header"))
        if not result.get("ok"):
            # The engine refuses a write that would leave the record invalid.
            # That is the honest answer, so it is shown with the reasons rather
            # than overridden silently.
            reasons = [item["detail"] for item in result.get("findings") or []]
            self._say(result.get("error", "not saved")
                      + (": " + "; ".join(reasons[:4]) if reasons else ""))
            self._show_findings(record)
            return
        self.refresh()
        self._say(result.get("detail", "saved"))

    def bump_version(self):
        """Raise the selected record's version by one patch step."""
        record = self._selected_record()
        if record is None:
            self._say(self.t("meta_select_hint", "Select a script first."))
            return
        values = dict(record.get("values") or {})
        values["version"] = _next_version(values.get("version"))
        result = self.store.save(record["path"], values, record.get("custom"),
                                 store=record.get("store", "header"), force=True)
        if not result.get("ok"):
            self._say(result.get("error", "not saved"))
            return
        self.refresh()
        self.load_current()
        self._say("version is now %s" % values["version"])

    def add_tag_to_selection(self):
        """Add one tag to every selected record, keeping their existing tags."""
        tag = self.tag_edit.text().strip()
        if not tag:
            self._say("type a tag first")
            return
        paths = self._selected_paths()
        if not paths:
            self._say(self.t("meta_select_hint", "Select a script first."))
            return
        result = self.store.bulk_set(paths, "tags", tag, append=True)
        if not result.get("ok"):
            self._say(result.get("error", "the tags were not changed"))
            return
        self.refresh()
        self._say("%d record(s) tagged %r, %d refused"
                  % (result.get("changed", 0), tag, result.get("refused", 0)))

    def undo(self):
        """Undo the last write the store made."""
        record = self._selected_record()
        path = record["path"] if record else None
        result = self.store.undo()
        self.refresh()
        if path is not None and self._select_path(path):
            self.load_current()
        # The outcome of the undo is reported last, so nothing it did is hidden
        # by a message from reselecting the row.
        self._say(result.get("detail") or result.get("error", "nothing to undo"))

    def _on_role_changed(self):
        """Tell the store which role the tab is acting as, and refit the UI."""
        key = self.role_combo.currentData()
        self.store.set_role(key)
        # A role that may not edit has the write controls disabled, so the tab
        # cannot offer an action the engine will refuse.
        may_edit = self.store.may_edit()
        for widget in (self.save_button, self.bump_button, self.tag_button,
                       self.undo_button):
            widget.setEnabled(may_edit)
        self._say("acting as %s%s" % (METADATA_ROLE_LABELS.get(key, key),
                                      "" if may_edit else " (read-only)"))

    def _on_selection_changed(self):
        """Show the newly selected record in the form and the findings."""
        record = self._selected_record()
        if record is None:
            return
        self._fill_form(record)
        self._show_findings(record)

    # ---------------------------------------------------------- form helpers
    def _fill_form(self, record):
        """Copy a record's values into the form without triggering a write."""
        self._loading = True
        try:
            values = record.get("values") or {}
            for key, editor in self._fields.items():
                value = str(values.get(key) or "")
                if isinstance(editor, QtWidgets.QComboBox):
                    index = editor.findData(value)
                    editor.setCurrentIndex(index if index >= 0 else 0)
                elif isinstance(editor, QtWidgets.QPlainTextEdit):
                    editor.setPlainText(value)
                else:
                    editor.setText(value)
        finally:
            self._loading = False

    def _read_form(self):
        """The form's values, as the field mapping the engine expects."""
        values = {}
        for key, editor in self._fields.items():
            if isinstance(editor, QtWidgets.QComboBox):
                values[key] = editor.currentData() or ""
            elif isinstance(editor, QtWidgets.QPlainTextEdit):
                values[key] = editor.toPlainText()
            else:
                values[key] = editor.text()
        return values

    def _show_findings(self, record):
        """Write what validation says about a record into the panel."""
        findings = record.get("findings") or []
        if not findings:
            self.findings_view.setPlainText(
                metadata_core.validation_summary(findings))
            return
        lines = []
        for item in findings:
            lines.append("[%s] %s: %s" % (item["severity"].upper(),
                                          item["field"], item["detail"]))
        self.findings_view.setPlainText("\n".join(lines))

    def _selected_record(self):
        row = self.table.currentRow()
        if row < 0 or row >= len(self.records):
            return None
        return self.records[row]

    def _select_path(self, path):
        """Select the row for one path after a refresh; True when found."""
        for row, record in enumerate(self.records):
            if record.get("path") == path:
                self.table.selectRow(row)
                return True
        return False

    def _selected_paths(self):
        paths = []
        for index in self.table.selectionModel().selectedRows():
            row = index.row()
            if 0 <= row < len(self.records):
                paths.append(self.records[row]["path"])
        return paths

    def _update_summary(self):
        stats = self.store.summary()
        self.summary_label.setText(
            "%d script(s), %d valid, %d error(s), %d warning(s)"
            % (stats.get("total", 0), stats.get("valid", 0),
               stats.get("errors", 0), stats.get("warnings", 0)))

    def _say(self, message):
        """Report the outcome of an action in the application's status bar."""
        try:
            self.window.statusBar().showMessage(str(message), 5000)
        except Exception:
            pass


def _next_version(version):
    """The next patch version after ``version``.

    A version that does not read as x.y.z is left alone rather than guessed at:
    inventing a number for an unparseable version would hide the real problem,
    which is that the field needs a person to look at it.
    """
    text = str(version or "").strip()
    parts = text.split(".")
    if not parts or not all(piece.isdigit() for piece in parts):
        return text or "0.0.1"
    parts[-1] = str(int(parts[-1]) + 1)
    return ".".join(parts)
