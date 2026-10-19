# -*- coding: utf-8 -*-
"""The Shopping tab: a shopping list with totals and file export.

The list is kept in its own JSON file next to the other database files, so it
survives restarts and can be edited by hand. Every change is written through
``ShoppingStore`` which owns the file, exactly like the other tabs, so nothing
in the interface touches the disk directly.

Export is the point of the tab as much as the list is: the whole list (or just
the items still to buy) can be written to a plain text file, a CSV file, a
Markdown checklist or JSON, and it can be printed through the standard dialog.
"""

import csv
import datetime as dt
import json
import os
import tempfile

import app_paths

from PyQt5 import QtCore, QtGui, QtWidgets

#: The units offered by the dialog; the field stays editable, so a unit that is
#: not listed here can still be typed in.
UNITS = ("", "pcs", "kg", "g", "l", "ml", "pack", "box", "bottle")


def _now_stamp():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _money(value):
    try:
        return "%.2f" % float(value or 0)
    except (TypeError, ValueError):
        return "0.00"


class ShoppingStore:
    """The shopping list: the file, the queries and every mutation."""

    DATA_FILE = "shopping_db.json"
    SCHEMA = 1

    def __init__(self, path):
        self.path = path
        self.items = []
        self.settings = {"currency": "PLN", "default_category": "General"}
        self.load()

    # -- persistence -------------------------------------------------------
    def load(self):
        if not os.path.exists(self.path):
            return []
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError):
            return []
        if isinstance(payload, list):
            records, settings = payload, {}
        elif isinstance(payload, dict):
            records = payload.get("items", [])
            settings = payload.get("settings", {}) or {}
        else:
            return []
        self.items = [item for item in records if isinstance(item, dict)]
        if isinstance(settings, dict):
            self.settings.update(settings)
        for index, item in enumerate(self.items):
            item.setdefault("id", "item-%d" % index)
            item.setdefault("name", "")
            item.setdefault("category", self.settings.get("default_category",
                                                           "General"))
            item["quantity"] = float(item.get("quantity") or 1)
            item["price"] = float(item.get("price") or 0)
            item["done"] = bool(item.get("done"))
            item.setdefault("unit", "")
            item.setdefault("note", "")
        return self.items

    def save(self):
        """Write the file atomically so a crash cannot truncate it."""
        payload = {"schema": self.SCHEMA, "saved": _now_stamp(),
                   "settings": self.settings, "items": self.items}
        directory = os.path.dirname(self.path) or "."
        handle, temporary = tempfile.mkstemp(dir=directory, prefix=".shopping-",
                                             suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except Exception:
            try:
                os.remove(temporary)
            except OSError:
                pass
            raise
        return self.path

    # -- queries -----------------------------------------------------------
    def categories(self):
        """Every category used, sorted, with the default one first."""
        found = {item.get("category", "") for item in self.items
                 if item.get("category")}
        if self.settings.get("default_category"):
            found.add(self.settings["default_category"])
        return sorted(found, key=lambda name: name.lower())

    def total(self, only_pending=False):
        """The sum of price * quantity, optionally only on pending items."""
        return sum(item["price"] * item["quantity"] for item in self.items
                   if not only_pending or not item.get("done"))

    def counts(self):
        done = sum(1 for item in self.items if item.get("done"))
        return len(self.items), done, len(self.items) - done


class ShoppingTab(QtWidgets.QWidget):
    """A shopping list: add, tick off, total, and save to a file."""

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        base = (getattr(window, "db_dir", None)
                or getattr(window, "DB_DIR", None)
                or app_paths.ensure_db_dir())
        self.base_dir = base
        self.store = ShoppingStore(os.path.join(base,
                                                ShoppingStore.DATA_FILE))
        self.filter_text = ""
        self.hide_done = False
        self._build()
        self.refresh()

    # ------------------------------------------------------------------ t
    def t(self, key, fallback=None, **kwargs):
        """Translate through the main window, falling back to English."""
        try:
            return self.window.t(key, fallback, **kwargs)
        except Exception:
            text = fallback if fallback is not None else key
            try:
                return text.format(**kwargs)
            except (KeyError, IndexError, ValueError):
                return text

    def save(self):
        """Persist the list, reporting a failure instead of crashing."""
        try:
            self.store.save()
            return True
        except Exception as exc:
            self.status.setText(str(exc))
            return False

    # ------------------------------------------------------------- build UI
    def _build(self):
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 8)
        layout.setSpacing(6)

        header = QtWidgets.QHBoxLayout()
        self.title = QtWidgets.QLabel(self.t("shopping_title", "SHOPPING"))
        self.title.setProperty("role", "title")
        header.addWidget(self.title)
        self.subtitle = QtWidgets.QLabel(
            self.t("shopping_subtitle", "Shopping list with a running total"))
        self.subtitle.setProperty("role", "hint")
        header.addWidget(self.subtitle)
        header.addStretch()
        self.find_edit = QtWidgets.QLineEdit()
        self.find_edit.setPlaceholderText(self.t("shopping_search",
                                                 "Filter items..."))
        self.find_edit.setClearButtonEnabled(True)
        self.find_edit.setMaximumWidth(260)
        self.find_edit.textChanged.connect(self.on_filter_changed)
        header.addWidget(self.find_edit)
        layout.addLayout(header)

        form = QtWidgets.QHBoxLayout()
        self.name_edit = QtWidgets.QLineEdit()
        self.name_edit.setPlaceholderText(self.t("shopping_name_hint",
                                                 "What to buy..."))
        self.name_edit.returnPressed.connect(self.add_item)
        form.addWidget(self.name_edit, 3)
        self.quantity_spin = QtWidgets.QDoubleSpinBox()
        self.quantity_spin.setRange(0.01, 9999)
        self.quantity_spin.setDecimals(2)
        self.quantity_spin.setValue(1)
        self.quantity_spin.setMaximumWidth(90)
        form.addWidget(self.quantity_spin)
        self.unit_combo = QtWidgets.QComboBox()
        self.unit_combo.setEditable(True)
        self.unit_combo.addItems(UNITS)
        self.unit_combo.setMaximumWidth(90)
        form.addWidget(self.unit_combo)
        self.price_spin = QtWidgets.QDoubleSpinBox()
        self.price_spin.setRange(0, 9999)
        self.price_spin.setDecimals(2)
        self.price_spin.setMaximumWidth(110)
        self.price_spin.setSuffix(" " + self.store.settings.get("currency", "PLN"))
        form.addWidget(self.price_spin)
        self.category_combo = QtWidgets.QComboBox()
        self.category_combo.setEditable(True)
        self.category_combo.setMaximumWidth(170)
        form.addWidget(self.category_combo)
        self.add_btn = QtWidgets.QPushButton(self.t("shopping_add", "+ Add item"))
        self.add_btn.clicked.connect(self.add_item)
        form.addWidget(self.add_btn)
        layout.addLayout(form)

        self.table = QtWidgets.QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels([
            self.t("shopping_col_done", "Bought"),
            self.t("shopping_col_name", "Item"),
            self.t("shopping_col_quantity", "Qty"),
            self.t("shopping_col_price", "Price"),
            self.t("shopping_col_category", "Category"),
            self.t("shopping_col_note", "Note")])
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.show_context_menu)
        self.table.itemChanged.connect(self.on_item_changed)
        self.table.itemDoubleClicked.connect(self.rename_selected)
        header_view = self.table.horizontalHeader()
        header_view.setStretchLastSection(True)
        layout.addWidget(self.table, 1)

        footer = QtWidgets.QHBoxLayout()
        self.status = QtWidgets.QLabel("")
        self.status.setProperty("role", "hint")
        footer.addWidget(self.status, 1)
        self.total_label = QtWidgets.QLabel("")
        self.total_label.setProperty("role", "title")
        footer.addWidget(self.total_label)
        layout.addLayout(footer)

        tools = QtWidgets.QHBoxLayout()
        self.done_btn = QtWidgets.QPushButton(self.t("shopping_toggle_done",
                                                     "Tick / untick"))
        self.done_btn.clicked.connect(self.toggle_done)
        self.remove_btn = QtWidgets.QPushButton(self.t("shopping_remove",
                                                       "Remove"))
        self.remove_btn.clicked.connect(self.remove_selected)
        self.clear_done_btn = QtWidgets.QPushButton(
            self.t("shopping_clear_done", "Remove bought items"))
        self.clear_done_btn.clicked.connect(self.clear_done)
        self.clear_all_btn = QtWidgets.QPushButton(
            self.t("shopping_clear_all", "Clear list"))
        self.clear_all_btn.clicked.connect(self.clear_all)
        tools.addWidget(self.done_btn)
        tools.addWidget(self.remove_btn)
        tools.addWidget(self.clear_done_btn)
        tools.addStretch()
        tools.addWidget(self.clear_all_btn)
        layout.addLayout(tools)

        export = QtWidgets.QHBoxLayout()
        self.export_btn = QtWidgets.QToolButton()
        self.export_btn.setText(self.t("shopping_export", "Save to file..."))
        self.export_btn.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.export_menu = QtWidgets.QMenu(self.export_btn)
        self.act_export_txt = self.export_menu.addAction(
            self.t("shopping_export_txt", "Text list (.txt)"))
        self.act_export_csv = self.export_menu.addAction(
            self.t("shopping_export_csv", "Spreadsheet (.csv)"))
        self.act_export_md = self.export_menu.addAction(
            self.t("shopping_export_md", "Markdown checklist (.md)"))
        self.act_export_json = self.export_menu.addAction(
            self.t("shopping_export_json", "JSON (.json)"))
        self.export_menu.addSeparator()
        self.act_export_pending = self.export_menu.addAction(
            self.t("shopping_export_pending", "Only what is left to buy"))
        self.act_export_pending.setCheckable(True)
        self.act_export_txt.triggered.connect(lambda: self.export_to_file("txt"))
        self.act_export_csv.triggered.connect(lambda: self.export_to_file("csv"))
        self.act_export_md.triggered.connect(lambda: self.export_to_file("md"))
        self.act_export_json.triggered.connect(
            lambda: self.export_to_file("json"))
        self.export_btn.setMenu(self.export_menu)
        self.import_btn = QtWidgets.QPushButton(
            self.t("shopping_import", "Load from file..."))
        self.import_btn.clicked.connect(self.import_from_file)
        self.print_btn = QtWidgets.QPushButton(self.t("shopping_print", "Print"))
        self.print_btn.clicked.connect(self.print_list)
        export.addWidget(self.export_btn)
        export.addWidget(self.import_btn)
        export.addWidget(self.print_btn)
        export.addStretch()
        self.open_dir_btn = QtWidgets.QPushButton(
            self.t("shopping_open_dir", "Open list folder"))
        self.open_dir_btn.clicked.connect(self.open_list_folder)
        export.addWidget(self.open_dir_btn)
        layout.addLayout(export)

    # --------------------------------------------------------------- filtering
    def on_filter_changed(self, text):
        self.filter_text = text
        self.refresh()

    def visible_items(self):
        """The list after the text filter and the hide-bought switch."""
        query = self.filter_text.strip().lower()
        result = []
        for item in self.store.items:
            if self.hide_done and item.get("done"):
                continue
            if query and query not in " ".join([
                    item.get("name", ""), item.get("category", ""),
                    item.get("note", "")]).lower():
                continue
            result.append(item)
        return result

    # ---------------------------------------------------------------- refresh
    def refresh(self):
        if not hasattr(self, "table"):
            return
        items = self.visible_items()
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        for item in items:
            row = self.table.rowCount()
            self.table.insertRow(row)
            check = QtWidgets.QTableWidgetItem()
            check.setFlags(QtCore.Qt.ItemIsUserCheckable | QtCore.Qt.ItemIsEnabled
                           | QtCore.Qt.ItemIsSelectable)
            check.setCheckState(QtCore.Qt.Checked if item.get("done")
                                else QtCore.Qt.Unchecked)
            check.setData(QtCore.Qt.UserRole, item.get("id"))
            self.table.setItem(row, 0, check)
            values = (item.get("name", ""),
                      "%g %s" % (item.get("quantity", 1), item.get("unit", "")),
                      _money(item.get("price")),
                      item.get("category", ""),
                      item.get("note", ""))
            for column, value in enumerate(values, start=1):
                cell = QtWidgets.QTableWidgetItem(str(value))
                if column == 3:
                    cell.setTextAlignment(QtCore.Qt.AlignRight
                                          | QtCore.Qt.AlignVCenter)
                if item.get("done"):
                    font = cell.font()
                    font.setStrikeOut(True)
                    cell.setFont(font)
                    cell.setForeground(QtGui.QColor("#9aa0a6"))
                self.table.setItem(row, column, cell)
        self.table.blockSignals(False)
        if self.table.columnCount():
            self.table.resizeColumnsToContents()
        self.refresh_summary()
        self.refresh_categories()

    def refresh_summary(self):
        total, done, pending = self.store.counts()
        currency = self.store.settings.get("currency", "PLN")
        self.total_label.setText(self.t("shopping_total",
                                        "Total: {total} {currency}  |  "
                                        "{pending} to buy, {done} bought",
                                        total=_money(self.store.total()),
                                        currency=currency,
                                        pending=pending, done=done))
        self.status.setText(self.t("shopping_count", "{count} item(s)",
                                   count=len(self.visible_items())))

    def refresh_categories(self):
        current = self.category_combo.currentText()
        self.category_combo.blockSignals(True)
        self.category_combo.clear()
        self.category_combo.addItems(self.store.categories())
        if current:
            self.category_combo.setCurrentText(current)
        self.category_combo.blockSignals(False)

    # ------------------------------------------------------------- mutations
    def add_item(self):
        name = self.name_edit.text().strip()
        if not name:
            return
        self.store.items.append({
            "id": "item-%s" % dt.datetime.now().timestamp(),
            "name": name,
            "quantity": float(self.quantity_spin.value()),
            "unit": self.unit_combo.currentText().strip(),
            "price": float(self.price_spin.value()),
            "category": (self.category_combo.currentText().strip()
                         or self.store.settings.get("default_category",
                                                    "General")),
            "note": "",
            "done": False,
            "added": _now_stamp(),
        })
        self.name_edit.clear()
        self.price_spin.setValue(0)
        self.quantity_spin.setValue(1)
        self.save()
        self.refresh()
        self.name_edit.setFocus()

    def item_by_id(self, item_id):
        return next((item for item in self.store.items
                     if item.get("id") == item_id), None)

    def selected_items(self):
        """Every item behind the current selection, in table order."""
        rows = sorted({index.row() for index in
                       self.table.selectionModel().selectedRows()})
        result = []
        for row in rows:
            cell = self.table.item(row, 0)
            if cell is None:
                continue
            item = self.item_by_id(cell.data(QtCore.Qt.UserRole))
            if item is not None:
                result.append(item)
        return result

    def selected_item(self):
        items = self.selected_items()
        return items[0] if items else None

    def on_item_changed(self, item):
        """Write back the checked state and the edited cells."""
        if item is None:
            return
        row = item.row()
        anchor = self.table.item(row, 0)
        if anchor is None:
            return
        target = self.item_by_id(anchor.data(QtCore.Qt.UserRole))
        if target is None:
            return
        column = item.column()
        text = item.text()
        try:
            if column == 0:
                target["done"] = item.checkState() == QtCore.Qt.Checked
            elif column == 1:
                target["name"] = text.strip() or target.get("name", "")
            elif column == 2:
                parts = text.split()
                if parts:
                    target["quantity"] = float(parts[0])
                    target["unit"] = " ".join(parts[1:])
            elif column == 3:
                target["price"] = float(text.replace(",", ".") or 0)
            elif column == 4:
                target["category"] = text.strip()
            elif column == 5:
                target["note"] = text.strip()
        except (TypeError, ValueError):
            self.refresh()
            return
        self.save()
        self.refresh_summary()

    def toggle_done(self):
        items = self.selected_items()
        if not items:
            return
        make_done = not all(item.get("done") for item in items)
        for item in items:
            item["done"] = make_done
        self.save()
        self.refresh()

    def remove_selected(self):
        items = self.selected_items()
        if not items:
            return
        doomed = {id(item) for item in items}
        self.store.items = [item for item in self.store.items
                            if id(item) not in doomed]
        self.save()
        self.refresh()

    def rename_selected(self, item=None):
        target = self.selected_item()
        if target is None:
            return
        text, accepted = QtWidgets.QInputDialog.getText(
            self, self.t("shopping_rename", "Rename item"),
            self.t("shopping_rename_prompt", "New name:"),
            QtWidgets.QLineEdit.Normal, target.get("name", ""))
        if accepted and text.strip():
            target["name"] = text.strip()
            self.save()
            self.refresh()

    def clear_done(self):
        bought = [item for item in self.store.items if item.get("done")]
        if not bought:
            self.status.setText(self.t("shopping_nothing_done",
                                       "nothing bought yet"))
            return
        if QtWidgets.QMessageBox.question(
                self, self.t("shopping_clear_done", "Remove bought items"),
                self.t("shopping_clear_done_ask",
                       "Remove {count} bought item(s)?", count=len(bought))
        ) != QtWidgets.QMessageBox.Yes:
            return
        self.store.items = [item for item in self.store.items
                            if not item.get("done")]
        self.save()
        self.refresh()

    def clear_all(self):
        if not self.store.items:
            return
        if QtWidgets.QMessageBox.question(
                self, self.t("shopping_clear_all", "Clear list"),
                self.t("shopping_clear_all_ask",
                       "Remove all {count} item(s)?", count=len(self.store.items))
        ) != QtWidgets.QMessageBox.Yes:
            return
        self.store.items = []
        self.save()
        self.refresh()

    # -------------------------------------------------------------- exporting
    def items_to_write(self):
        """The items an export should contain (pending only, when asked)."""
        if self.act_export_pending.isChecked():
            return [item for item in self.store.items if not item.get("done")]
        return list(self.store.items)

    def as_text(self, items):
        """A plain, ASCII shopping list: header, one line per item, total."""
        currency = self.store.settings.get("currency", "PLN")
        lines = [self.t("shopping_report_title", "Shopping list"), "=" * 20,
                 self.t("shopping_report_generated", "Generated {when}",
                        when=_now_stamp()), ""]
        category = None
        for item in items:
            if item.get("category") != category:
                category = item.get("category", "")
                lines.append("")
                lines.append("[%s]" % (category or "General"))
            mark = "[x]" if item.get("done") else "[ ]"
            unit = (" " + item["unit"]) if item.get("unit") else ""
            price = ("  %s %s" % (_money(item["price"] * item["quantity"]),
                                  currency)) if item.get("price") else ""
            line = "  %s %g%s  %s%s" % (mark, item.get("quantity", 1), unit,
                                        item.get("name", ""), price)
            if item.get("note"):
                line += "  -- " + item["note"]
            lines.append(line)
        total = sum(item["price"] * item["quantity"] for item in items)
        lines.extend(["", "-" * 20,
                      "%s %s" % (_money(total), currency), ""])
        return "\n".join(lines)

    def as_markdown(self, items):
        """A Markdown checklist, so the file can be ticked off in any editor."""
        currency = self.store.settings.get("currency", "PLN")
        lines = ["# " + self.t("shopping_report_title", "Shopping list"), "",
                 self.t("shopping_report_generated", "Generated {when}",
                        when=_now_stamp()), ""]
        for item in items:
            box = "[x]" if item.get("done") else "[ ]"
            unit = (" " + item["unit"]) if item.get("unit") else ""
            price = ("%s %s" % (_money(item["price"] * item["quantity"]),
                                 currency)) if item.get("price") else ""
            note = ("_%s_" % item["note"]) if item.get("note") else ""
            lines.append("- %s %g%s %s" % (box, item.get("quantity", 1),
                                              unit, item.get("name", ""))
                         + (" - %s" % price if price else "")
                         + (" %s" % note if note else ""))
        total = sum(item["price"] * item["quantity"] for item in items)
        lines.extend(["", "**%s: %s %s**" % (self.t("shopping_col_total",
                                                    "Total"), _money(total),
                                             currency), ""])
        return "\n".join(lines)

    def write_text(self, path, items=None):
        """Write the plain text list (head-less entry point, used by tests)."""
        items = self.items_to_write() if items is None else items
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(self.as_text(items))
        return path

    def write_csv(self, path, items=None):
        items = self.items_to_write() if items is None else items
        fields = ("name", "quantity", "unit", "price", "category", "done",
                  "note")
        with open(path, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields,
                                    extrasaction="ignore")
            writer.writeheader()
            for item in items:
                row = dict(item)
                row["done"] = "yes" if item.get("done") else "no"
                writer.writerow(row)
        return path

    def write_markdown(self, path, items=None):
        items = self.items_to_write() if items is None else items
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(self.as_markdown(items))
        return path

    def write_json(self, path, items=None):
        items = self.items_to_write() if items is None else items
        payload = {"schema": ShoppingStore.SCHEMA, "saved": _now_stamp(),
                   "settings": self.store.settings, "items": items,
                   "total": sum(item["price"] * item["quantity"]
                                for item in items)}
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        return path

    def export_to_file(self, kind):
        """Ask for a path, then write the list in the requested format."""
        formats = {
            "txt": (self.t("shopping_export_txt", "Text list (.txt)"),
                    "shopping_list.txt", "Text files (*.txt);;All files (*)",
                    self.write_text),
            "csv": (self.t("shopping_export_csv", "Spreadsheet (.csv)"),
                    "shopping_list.csv", "CSV files (*.csv);;All files (*)",
                    self.write_csv),
            "md": (self.t("shopping_export_md", "Markdown checklist (.md)"),
                   "shopping_list.md", "Markdown files (*.md);;All files (*)",
                   self.write_markdown),
            "json": (self.t("shopping_export_json", "JSON (.json)"),
                     "shopping_list.json", "JSON files (*.json);;All files (*)",
                     self.write_json),
        }
        caption, suggested, filters, writer = formats[kind]
        path, _selected = QtWidgets.QFileDialog.getSaveFileName(
            self, caption, os.path.join(self.base_dir, suggested), filters)
        if not path:
            return ""
        try:
            writer(path)
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, self.t("export_error",
                                                        "Export error"),
                                           str(exc))
            return ""
        self.status.setText(self.t("shopping_saved", "saved: {path}",
                                   path=path))
        return path

    def import_from_file(self):
        """Load a list written by this tab, or a CSV/spreadsheet of items."""
        path, _selected = QtWidgets.QFileDialog.getOpenFileName(
            self, self.t("shopping_import", "Load from file..."), "",
            "Shopping lists (*.json *.csv *.txt *.md);;All files (*)")
        if not path:
            return 0
        try:
            added = self.load_file(path)
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, self.t("import_error",
                                                        "Import error"),
                                           str(exc))
            return 0
        self.save()
        self.refresh()
        self.status.setText(self.t("shopping_imported", "imported {count}",
                                   count=added))
        return added

    def load_file(self, path):
        """Read a file and merge its items; returns how many were added."""
        extension = os.path.splitext(path)[1].lower()
        records = []
        if extension == ".json":
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if isinstance(payload, dict):
                records = payload.get("items", [])
            elif isinstance(payload, list):
                records = payload
        elif extension == ".csv":
            with open(path, "r", encoding="utf-8", newline="") as handle:
                records = list(csv.DictReader(handle))
        else:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    text = line.strip().lstrip("-*").strip()
                    text = text.replace("[ ]", "").replace("[x]", "").strip()
                    if text and not text.startswith(("[", "#", "=", "-")):
                        records.append({"name": text})
        known = {(item.get("name", ""), item.get("category", ""))
                 for item in self.store.items}
        added = 0
        for entry in records:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name", "")).strip()
            if not name:
                continue
            category = str(entry.get("category", "")
                           or self.store.settings.get("default_category",
                                                      "General"))
            if (name, category) in known:
                continue
            self.store.items.append({
                "id": "item-%s-%d" % (dt.datetime.now().timestamp(), added),
                "name": name,
                "quantity": float(entry.get("quantity") or 1),
                "unit": str(entry.get("unit", "")),
                "price": float(entry.get("price") or 0),
                "category": category,
                "note": str(entry.get("note", "")),
                "done": str(entry.get("done", "")).lower() in ("yes", "true", "1"),
                "added": _now_stamp(),
            })
            known.add((name, category))
            added += 1
        return added

    def print_list(self):
        """Send the list to the system print dialog through a text document."""
        text = self.as_text(self.items_to_write())
        printer = QtGui.QTextDocument()
        printer.setPlainText(text)
        dialog = QtGui.QPrintDialog(
            QtGui.QPrinter(QtGui.QPrinter.HighResolution), self)
        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            return False
        printer.print_(dialog.printer())
        self.status.setText(self.t("shopping_printed", "sent to the printer"))
        return True

    def open_list_folder(self):
        """Open the folder holding the list's data file."""
        folder = os.path.dirname(self.store.path) or os.getcwd()
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(folder))

    # -------------------------------------------------------- context menu
    def show_context_menu(self, point):
        menu = QtWidgets.QMenu(self)
        menu.addAction(self.t("shopping_toggle_done", "Tick / untick"),
                       self.toggle_done)
        menu.addAction(self.t("shopping_rename", "Rename item"),
                       lambda: self.rename_selected())
        menu.addAction(self.t("shopping_remove", "Remove"), self.remove_selected)
        menu.addSeparator()
        menu.addAction(self.t("shopping_export", "Save to file..."),
                       lambda: self.export_to_file("txt"))
        menu.addSeparator()
        hide = menu.addAction(self.t("shopping_hide_done",
                                     "Hide bought items"), self.toggle_hide_done)
        hide.setCheckable(True)
        hide.setChecked(self.hide_done)
        menu.exec_(self.table.viewport().mapToGlobal(point))

    def toggle_hide_done(self):
        self.hide_done = not self.hide_done
        self.refresh()

    # ---------------------------------------------------------- translation
    def retranslate(self, _table=None):
        self.title.setText(self.t("shopping_title", "SHOPPING"))
        self.subtitle.setText(self.t("shopping_subtitle",
                                     "Shopping list with a running total"))
        self.find_edit.setPlaceholderText(self.t("shopping_search",
                                                 "Filter items..."))
        self.name_edit.setPlaceholderText(self.t("shopping_name_hint",
                                                 "What to buy..."))
        self.add_btn.setText(self.t("shopping_add", "+ Add item"))
        self.done_btn.setText(self.t("shopping_toggle_done", "Tick / untick"))
        self.remove_btn.setText(self.t("shopping_remove", "Remove"))
        self.clear_done_btn.setText(self.t("shopping_clear_done",
                                           "Remove bought items"))
        self.clear_all_btn.setText(self.t("shopping_clear_all", "Clear list"))
        self.export_btn.setText(self.t("shopping_export", "Save to file..."))
        self.import_btn.setText(self.t("shopping_import", "Load from file..."))
        self.print_btn.setText(self.t("shopping_print", "Print"))
        self.open_dir_btn.setText(self.t("shopping_open_dir",
                                         "Open list folder"))
        self.act_export_txt.setText(self.t("shopping_export_txt",
                                           "Text list (.txt)"))
        self.act_export_csv.setText(self.t("shopping_export_csv",
                                           "Spreadsheet (.csv)"))
        self.act_export_md.setText(self.t("shopping_export_md",
                                          "Markdown checklist (.md)"))
        self.act_export_json.setText(self.t("shopping_export_json",
                                            "JSON (.json)"))
        self.act_export_pending.setText(self.t("shopping_export_pending",
                                               "Only what is left to buy"))
        self.table.setHorizontalHeaderLabels([
            self.t("shopping_col_done", "Bought"),
            self.t("shopping_col_name", "Item"),
            self.t("shopping_col_quantity", "Qty"),
            self.t("shopping_col_price", "Price"),
            self.t("shopping_col_category", "Category"),
            self.t("shopping_col_note", "Note")])
        self.refresh()
