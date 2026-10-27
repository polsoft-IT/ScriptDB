# -*- coding: utf-8 -*-
"""DOCs tab for ScriptDB -- a document repository.

This module owns the "DOCs" tab: a place to organise, store, archive, manage
and edit documents of every common file type, kept in one portable database
file. It follows the same one-file-per-tab pattern as the other tabs and
exposes a small, stable surface to the rest of the application:

    DocsTab(window)               -- build the tab
    tab.refresh()                 -- reload the document list
    tab.retranslate(table)        -- apply a language table (see TRANSLATIONS)
    tab.export_database(path)     -- write the repository to a portable file
    tab.send_to_editor(doc)       -- hand a document to the Editor tab
    tab.search_documents()        -- documents as (name, code, category)

The heavy lifting lives in :class:`DocsRepository`, a small, dependency-free
class built on the standard-library ``sqlite3`` module. It stores every
document in a single SQLite file, so the same database can be opened, read and
used by any other program that speaks SQLite or SQL (DB Browser for SQLite,
Python, Excel via ODBC, MySQL migrations, ...). A second, migration-friendly
JSON export is offered next to it.

Password protection: when a repository has a password, the document body of
every row is stored encrypted (PBKDF2-HMAC-SHA256 key derivation + an
HMAC-SHA256 keystream with an integrity tag). No third-party cryptography is
needed -- it is the standard library only -- and the metadata (title, category,
tags, dates) stays readable so the list still works before the password is
entered.

Offline documentation engine (Core Knowledge Engine): the tab keeps three
indexes over the documents -- a catalogue, a full-text content index (SQLite
FTS5, with a portable inverted-index fallback) and a metadata index -- plus an
offline text cache, point-in-time documentation snapshots and automatic
re-indexing of tracked local files that changed on disk. Everything lives in
the one database file and works with no network at all; see
``tabs/docs_engine.py`` for the engine itself.

Cross-tab cooperation: like every other tab, this tab is part of one program.
A document can be pushed to the Editor for editing, its text can be searched
with the Search engine, and new database files announce a change on the shared
audit event bus (``window.notify_audit``) so the Audit timeline stays complete.
"""

import base64
import csv
import datetime as dt
import hashlib
import hmac
import json
import os
import sqlite3
from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtGui import QFont
from .docs_engine import DocsIndex
from .docs_types import (DOCUMENT_TYPES as DOC_TYPE_RECORDS,
                         GROUPS as DOC_GROUPS, guess_type,
                         is_binary_type, type_for_key, type_label,
                         types_in_group)
# File names / defaults -------------------------------------------------------
DOCS_DB_FILE = "docs_db.sqlite"
DOCS_FONT = "Consolas"
DOCS_FONT_SIZE = 10

# Schema version so a future migration can recognise an old file.
DOCS_SCHEMA_VERSION = 1

# The document picker, built from the offline type registry (tabs/docs_types.py)
# so the tab, the importer and the engine all agree on the supported types.
# Each entry keeps the (label, key, binary) shape the UI uses.
DOCUMENT_TYPES = tuple(
    (record.label, record.key, record.binary)
    for record in DOC_TYPE_RECORDS
)

# The preview views offered by the "Preview" page, in menu order.
PREVIEW_KINDS = (
    ("typed", "docs_preview_typed", "By document type"),
    ("ascii", "docs_preview_ascii", "ASCII / plain text"),
    ("structure", "docs_preview_structure", "Structure (outline)"),
    ("minimap", "docs_preview_minimap", "Mini-map"),
    ("metadata", "docs_preview_metadata", "Metadata"),
    ("links", "docs_preview_links", "Links (relations)"),
    ("versions", "docs_preview_versions", "Changes vs snapshot"),
)

def guess_document_type(filename):
    """The document type KEY for a file name (via the offline registry)."""
    return guess_type(filename).key


def document_type_label(key):
    """The human label for a document type key."""
    return type_for_key(key).label


def _now():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Password protection: standard-library only encryption
# ---------------------------------------------------------------------------
#
# We derive a 32-byte key from the password with PBKDF2-HMAC-SHA256 and a per
# repository salt, then encrypt each body with an HMAC-SHA256 keystream and a
# tag that authenticates the ciphertext. This is deliberately dependency free;
# it protects a database file at rest, it is not a substitute for full-disk
# encryption of very sensitive data.


def derive_key(password, salt, iterations=200000):
    """A 32-byte key from a password and a salt (PBKDF2-HMAC-SHA256)."""
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt,
                               iterations, dklen=32)


def _keystream(key, nonce, length):
    """Deterministic keystream bytes from a key and a per-record nonce."""
    out = bytearray()
    counter = 0
    while len(out) < length:
        block = hmac.new(key, nonce + counter.to_bytes(8, "big"),
                         hashlib.sha256).digest()
        out.extend(block)
        counter += 1
    return bytes(out[:length])


def encrypt_text(plain_text, key, nonce):
    """Encrypt a string, returning a base64 'nonce:tag:cipher' envelope."""
    data = (plain_text or "").encode("utf-8")
    stream = _keystream(key, nonce, len(data))
    cipher = bytes(a ^ b for a, b in zip(data, stream))
    tag = hmac.new(key, nonce + cipher, hashlib.sha256).digest()[:16]
    return ":".join((
        base64.b64encode(nonce).decode("ascii"),
        base64.b64encode(tag).decode("ascii"),
        base64.b64encode(cipher).decode("ascii"),
    ))


def decrypt_text(envelope, key):
    """Reverse :func:`encrypt_text`; raises ValueError on a bad password."""
    try:
        nonce_b64, tag_b64, cipher_b64 = envelope.split(":")
        nonce = base64.b64decode(nonce_b64)
        tag = base64.b64decode(tag_b64)
        cipher = base64.b64decode(cipher_b64)
    except (ValueError, base64.binascii.Error):
        raise ValueError("Corrupt encrypted payload")
    expected = hmac.new(key, nonce + cipher, hashlib.sha256).digest()[:16]
    if not hmac.compare_digest(expected, tag):
        raise ValueError("Wrong password or tampered data")
    stream = _keystream(key, nonce, len(cipher))
    data = bytes(a ^ b for a, b in zip(cipher, stream))
    return data.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Repository: a single portable SQLite database of documents
# ---------------------------------------------------------------------------


class DocsRepository(object):
    """A document repository stored in one SQLite file.

    Every mutation is committed immediately, so the on-disk database is always
    consistent and can be opened by any other SQLite-aware program. Bodies may
    be stored encrypted when a password is set.
    """

    def __init__(self, path=None, password=None):
        self.path = path or ":memory:"
        self.key = None
        self.salt = None
        self.encrypted = False
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._create_schema()
        if password and self.is_encrypted():
            self.unlock(password)
        elif self.is_encrypted():
            self.encrypted = True

    # ------------------------------------------------------------- schema
    def _create_schema(self):
        cur = self._conn.cursor()
        cur.execute(
            "CREATE TABLE IF NOT EXISTS documents ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " title TEXT NOT NULL,"
            " doc_type TEXT,"
            " category TEXT,"
            " tags TEXT,"
            " description TEXT,"
            " body TEXT,"
            " encrypted INTEGER DEFAULT 0,"
            " archived INTEGER DEFAULT 0,"
            " created TEXT,"
            " modified TEXT,"
            " meta TEXT)")
        cur.execute(
            "CREATE TABLE IF NOT EXISTS repository ("
            " key TEXT PRIMARY KEY, value TEXT)")
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_documents_category "
            "ON documents(category)")
        self._conn.commit()
        if self._meta("schema_version") is None:
            self._set_meta("schema_version", str(DOCS_SCHEMA_VERSION))

    def _meta(self, key, default=None):
        cur = self._conn.execute(
            "SELECT value FROM repository WHERE key = ?", (key,))
        row = cur.fetchone()
        return row["value"] if row else default

    def _set_meta(self, key, value):
        self._conn.execute(
            "INSERT INTO repository(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)))
        self._conn.commit()

    # ------------------------------------------------------------- crypto
    def is_encrypted(self):
        return self._meta("encrypted", "0") == "1"

    def set_password(self, password):
        """Encrypt this repository with a password (or clear it with "")."""
        if not password:
            self._clear_password()
            return
        salt = os.urandom(16)
        key = derive_key(password, salt, self._iterations())
        rows = self._conn.execute(
            "SELECT id, body, encrypted FROM documents").fetchall()
        for row in rows:
            plain = self._decode_body(row["body"], row["encrypted"])
            nonce = os.urandom(16)
            envelope = encrypt_text(plain, key, nonce)
            self._conn.execute(
                "UPDATE documents SET body = ?, encrypted = 1 WHERE id = ?",
                (envelope, row["id"]))
        self._set_meta("salt", base64.b64encode(salt).decode("ascii"))
        self._set_meta("kdf_iterations", str(self._iterations()))
        self._set_meta("encrypted", "1")
        self._conn.commit()
        self.salt = salt
        self.key = key
        self.encrypted = True

    def _clear_password(self):
        if self.key is not None:
            rows = self._conn.execute(
                "SELECT id, body FROM documents WHERE encrypted = 1").fetchall()
            for row in rows:
                plain = self._decode_body(row["body"], 1)
                self._conn.execute(
                    "UPDATE documents SET body = ?, encrypted = 0 WHERE id = ?",
                    (plain, row["id"]))
        self._set_meta("encrypted", "0")
        self._conn.commit()
        self.key = None
        self.salt = None
        self.encrypted = False

    def unlock(self, password):
        """Derive the key from the stored salt; True when the password fits."""
        salt_b64 = self._meta("salt")
        if not salt_b64:
            return False
        salt = base64.b64decode(salt_b64)
        iterations = int(self._meta("kdf_iterations",
                                    str(self._iterations())))
        candidate = derive_key(password, salt, iterations)
        row = self._conn.execute(
            "SELECT body FROM documents WHERE encrypted = 1 "
            "AND body IS NOT NULL LIMIT 1").fetchone()
        if row is not None:
            try:
                decrypt_text(row["body"], candidate)
            except ValueError:
                return False
        self.salt = salt
        self.key = candidate
        self.encrypted = True
        return True

    @staticmethod
    def _iterations():
        return 200000

    def _decode_body(self, body, encrypted):
        if not encrypted:
            return body or ""
        if self.key is None:
            raise ValueError("Repository is encrypted; password required")
        return decrypt_text(body, self.key)

    def _encode_body(self, body, encrypt):
        if not encrypt:
            return body or "", 0
        if self.key is None:
            raise ValueError("Repository is encrypted; password required")
        return encrypt_text(body or "", self.key, os.urandom(16)), 1

    # --------------------------------------------------------------- CRUD
    def add(self, title, body="", doc_type="text", category="General",
            tags="", description="", archived=False, meta=None):
        """Insert a document and return its id."""
        stamp = _now()
        stored, flag = self._encode_body(body, self.is_encrypted())
        cur = self._conn.execute(
            "INSERT INTO documents "
            "(title, doc_type, category, tags, description, body, encrypted,"
            " archived, created, modified, meta) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (title or "untitled", doc_type, category, tags, description,
             stored, flag, 1 if archived else 0, stamp, stamp,
             json.dumps(meta or {}, ensure_ascii=False)))  # 11 values
        self._conn.commit()
        return cur.lastrowid

    def update(self, doc_id, **fields):
        """Update the given columns of one document."""
        allowed = ("title", "doc_type", "category", "tags", "description",
                   "archived", "meta")
        assignments = []
        values = []
        for key, value in fields.items():
            if key == "body":
                stored, flag = self._encode_body(value, self.is_encrypted())
                assignments.append("body = ?")
                values.append(stored)
                assignments.append("encrypted = ?")
                values.append(flag)
            elif key in allowed:
                assignments.append("%s = ?" % key)
                if key == "archived":
                    value = 1 if value else 0
                elif key == "meta" and not isinstance(value, str):
                    value = json.dumps(value or {}, ensure_ascii=False)
                values.append(value)
        if not assignments:
            return False
        assignments.append("modified = ?")
        values.append(_now())
        values.append(doc_id)
        self._conn.execute(
            "UPDATE documents SET %s WHERE id = ?" % ", ".join(assignments),
            values)
        self._conn.commit()
        return True

    def get(self, doc_id):
        """One document as a dict, or None. Body is decrypted when possible."""
        row = self._conn.execute(
            "SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
        return self._row_to_dict(row) if row else None

    def delete(self, doc_ids):
        if isinstance(doc_ids, int):
            doc_ids = [doc_ids]
        ids = list(doc_ids or [])
        if not ids:
            return 0
        marks = ",".join("?" for _ in ids)
        cur = self._conn.execute(
            "DELETE FROM documents WHERE id IN (%s)" % marks, ids)
        self._conn.commit()
        return cur.rowcount

    def list_documents(self, include_archived=True, category=None):
        """Every document ordered by category then title."""
        query = "SELECT * FROM documents"
        clauses = []
        params = []
        if not include_archived:
            clauses.append("archived = 0")
        if category:
            clauses.append("category = ?")
            params.append(category)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY category COLLATE NOCASE, title COLLATE NOCASE"
        rows = self._conn.execute(query, params).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def categories(self):
        rows = self._conn.execute(
            "SELECT DISTINCT category FROM documents "
            "WHERE category IS NOT NULL AND category <> '' "
            "ORDER BY category COLLATE NOCASE").fetchall()
        return [row["category"] for row in rows]

    def count(self, include_archived=True):
        query = "SELECT COUNT(*) AS n FROM documents"
        if not include_archived:
            query += " WHERE archived = 0"
        return self._conn.execute(query).fetchone()["n"]

    def search(self, needle, include_archived=True, encrypted_only_meta=False):
        """Documents whose title/tags/description/body contain `needle`.

        When the repository is encrypted and locked, only the metadata columns
        are searched (the body cannot be read without the password).
        """
        needle = (needle or "").strip().lower()
        if not needle:
            return self.list_documents(include_archived=include_archived)
        results = []
        for doc in self.list_documents(include_archived=include_archived):
            haystack = " ".join(str(doc.get(field, "")) for field in
                                ("title", "tags", "description", "category"))
            if needle in haystack.lower():
                results.append(doc)
                continue
            if not encrypted_only_meta:
                try:
                    if needle in (doc.get("body") or "").lower():
                        results.append(doc)
                except Exception:
                    continue
        return results

    def _row_to_dict(self, row):
        if row is None:
            return None
        data = dict(row)
        try:
            data["body"] = self._decode_body(data.get("body"),
                                             data.get("encrypted"))
            data["locked"] = False
        except ValueError:
            data["body"] = ""
            data["locked"] = True
        try:
            data["meta"] = json.loads(data.get("meta") or "{}")
        except (ValueError, TypeError):
            data["meta"] = {}
        return data

    # ------------------------------------------------------------- export
    def export_json(self, path):
        """Write the whole repository as a portable JSON document."""
        payload = {
            "format": "scriptdb-docs",
            "schema_version": DOCS_SCHEMA_VERSION,
            "exported": _now(),
            "encrypted": self.is_encrypted(),
            "documents": [self._export_record(doc)
                          for doc in self.list_documents()],
        }
        _atomic_write_json(path, payload)
        return path

    def export_csv(self, path):
        """Write the documents as a CSV table (readable by spreadsheets)."""
        fields = ("title", "_type", "category", "tags", "description",
                  "body", "archived", "created", "modified")
        with open(path, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for doc in self.list_documents():
                writer.writerow({key: doc.get(key, "") for key in fields})
        return path

    def export_sql(self, path):
        """Write a portable SQL script that rebuilds the repository."""
        lines = [
            "-- ScriptDB DOCs export",
            "-- Recreate this repository in any SQL database.",
            "CREATE TABLE IF NOT EXISTS documents (",
            "  id INTEGER PRIMARY KEY,",
            "  title TEXT, doc_type TEXT, category TEXT, tags TEXT,",
            "  description TEXT, body TEXT, encrypted INTEGER,",
            "  archived INTEGER, created TEXT, modified TEXT, meta TEXT);",
            "",
        ]
        for doc in self.list_documents():
            values = (
                doc.get("id"), doc.get("title", ""), doc.get("doc_type", ""),
                doc.get("category", ""), doc.get("tags", ""),
                doc.get("description", ""), doc.get("body", ""),
                1 if doc.get("encrypted") else 0,
                1 if doc.get("archived") else 0,
                doc.get("created", ""), doc.get("modified", ""),
                json.dumps(doc.get("meta", {}), ensure_ascii=False),
            )
            lines.append(
                "INSERT INTO documents VALUES (%s);" % ", ".join(
                    _sql_literal(value) for value in values))
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        return path

    def import_json(self, path):
        """Merge a previously exported JSON repository; returns rows added."""
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        records = payload.get("documents") if isinstance(payload, dict) else payload
        added = 0
        for record in records or []:
            if not isinstance(record, dict):
                continue
            self.add(
                record.get("title", "untitled"),
                record.get("body", ""),
                record.get("doc_type", "text"),
                record.get("category", "Imported"),
                record.get("tags", ""),
                record.get("description", ""),
                bool(record.get("archived")),
                record.get("meta") or {})
            added += 1
        return added

    def _export_record(self, doc):
        return {
            "id": doc.get("id"), "title": doc.get("title", ""),
            "doc_type": doc.get("doc_type", ""), "category": doc.get("category", ""),
            "tags": doc.get("tags", ""), "description": doc.get("description", ""),
            "body": doc.get("body", ""), "archived": bool(doc.get("archived")),
            "created": doc.get("created", ""), "modified": doc.get("modified", ""),
            "meta": doc.get("meta", {}),
        }

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass


def _atomic_write_json(path, payload):
    directory = os.path.dirname(path) or "."
    handle, temp = __import__("tempfile").mkstemp(
        dir=directory, prefix=".docs-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    except Exception:
        try:
            os.remove(temp)
        except OSError:
            pass
        raise


def _sql_literal(value):
    if value is None:
        return "NULL"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


# ---------------------------------------------------------------------------
# The Qt tab
# ---------------------------------------------------------------------------


class DocsTab(QtWidgets.QWidget):
    """A document repository: organise, store, archive, manage and edit docs.

    The tab keeps one :class:`DocsRepository` open at a time. Every document
    lives in a single SQLite file that can be exported (JSON / CSV / SQL) and
    opened by other database programs; an optional password encrypts the stored
    bodies.
    """

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.repo = None
        self.index = None            # the offline documentation engine
        self._current_id = None
        self._dirty = False
        self._build_ui()
        self.open_database(self._default_db_path(), silent=True)

    # --------------------------------------------------------------- helpers
    def t(self, key, fallback=None, **kwargs):
        """Look a label up in the main window's translations."""
        try:
            return self.window.t(key, fallback, **kwargs)
        except Exception:
            return fallback or key

    def _default_db_path(self):
        base = getattr(self.window, "base_dir", None) or getattr(
            self.window, "BASE_DIR", None) or os.path.dirname(
                os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(base, DOCS_DB_FILE)

    # ------------------------------------------------------------------- UI
    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)

        header = QtWidgets.QHBoxLayout()
        self.title_label = QtWidgets.QLabel(self.t("docs", "DOCS"))
        self.title_label.setStyleSheet(
            "font-size: 14px; font-weight: bold; color: #8ab4f8;")
        header.addWidget(self.title_label)
        self.db_label = QtWidgets.QLabel("")
        self.db_label.setStyleSheet("color: #9aa0a6;")
        header.addWidget(self.db_label)
        header.addStretch()
        self.count_label = QtWidgets.QLabel("")
        self.count_label.setStyleSheet("color: #9aa0a6;")
        header.addWidget(self.count_label)
        layout.addLayout(header)

        # ---- database toolbar --------------------------------------------
        db_row = QtWidgets.QHBoxLayout()
        self.btn_new_db = QtWidgets.QPushButton(self.t("docs_new_db", "New database"))
        self.btn_new_db.clicked.connect(self.new_database)
        self.btn_open_db = QtWidgets.QPushButton(self.t("docs_open_db", "Open..."))
        self.btn_open_db.clicked.connect(self.open_database_dialog)
        # Save is one button like Import/Export: a direct click saves the open
        # document, the arrow opens a menu that also offers "Save database as...".
        self.btn_save_db = QtWidgets.QToolButton()
        self.btn_save_db.setText(self.t("docs_save", "Save"))
        self.btn_save_db.setPopupMode(QtWidgets.QToolButton.MenuButtonPopup)
        self.save_menu = QtWidgets.QMenu(self.btn_save_db)
        self.act_save_doc = self.save_menu.addAction(
            self.t("docs_save_doc", "Save document"))
        self.act_save_db = self.save_menu.addAction(
            self.t("docs_save_db", "Save database as..."))
        self.act_save_doc.triggered.connect(self.save_current)
        self.act_save_db.triggered.connect(self.save_database_as)
        self.btn_save_db.setMenu(self.save_menu)
        self.btn_save_db.clicked.connect(self.save_current)
        self.btn_password = QtWidgets.QPushButton(
            self.t("docs_password", "Password..."))
        self.btn_password.clicked.connect(self.manage_password)
        self.btn_export = QtWidgets.QToolButton()
        self.btn_export.setText(self.t("docs_export", "Export..."))
        self.btn_export.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.export_menu = QtWidgets.QMenu(self.btn_export)
        self.act_export_json = self.export_menu.addAction("JSON (.json)")
        self.act_export_csv = self.export_menu.addAction("CSV (.csv)")
        self.act_export_sql = self.export_menu.addAction("SQL (.sql)")
        self.act_export_json.triggered.connect(lambda: self.export_database("json"))
        self.act_export_csv.triggered.connect(lambda: self.export_database("csv"))
        self.act_export_sql.triggered.connect(lambda: self.export_database("sql"))
        self.btn_export.setMenu(self.export_menu)
        for button in (self.btn_new_db, self.btn_open_db, self.btn_save_db,
                       self.btn_password, self.btn_export):
            db_row.addWidget(button)

        # ---- document toolbar --------------------------------------------
        self.btn_add = QtWidgets.QPushButton(self.t("docs_add", "Add document"))
        self.btn_add.clicked.connect(self.add_document)
        self.btn_import = QtWidgets.QPushButton(self.t("docs_import", "Import files..."))
        self.btn_import.clicked.connect(self.import_files)
        self.btn_archive = QtWidgets.QPushButton(self.t("docs_archive", "Archive"))
        self.btn_archive.clicked.connect(self.toggle_archive)
        self.btn_delete = QtWidgets.QPushButton(self.t("docs_delete", "Delete"))
        self.btn_delete.clicked.connect(self.delete_selected)
        self.btn_edit = QtWidgets.QPushButton(self.t("docs_edit_external", "Open in editor"))
        self.btn_edit.clicked.connect(self.send_to_editor)
        self.btn_search = QtWidgets.QPushButton(self.t("docs_search_tab", "Search code"))
        self.btn_search.clicked.connect(self.send_to_search)
        for button in (self.btn_add, self.btn_import,
                       self.btn_archive, self.btn_delete, self.btn_edit,
                       self.btn_search):
            db_row.addWidget(button)
        db_row.addStretch()
        self.lock_label = QtWidgets.QLabel("")
        self.lock_label.setStyleSheet("color: #f7c66d;")
        db_row.addWidget(self.lock_label)
        layout.addLayout(db_row)

        # ---- offline documentation engine toolbar -------------------------
        engine_row = QtWidgets.QHBoxLayout()
        self.btn_index = QtWidgets.QPushButton(self.t("docs_index", "Rebuild index"))
        self.btn_index.clicked.connect(self.reindex_documents)
        self.btn_update_local = QtWidgets.QPushButton(
            self.t("docs_update_local", "Update local docs"))
        self.btn_update_local.clicked.connect(self.update_stale_documents)
        self.chk_fulltext = QtWidgets.QCheckBox(
            self.t("docs_fulltext", "Full-text index"))
        self.chk_fulltext.setChecked(False)
        self.chk_fulltext.setToolTip(self.t(
            "docs_fulltext_hint",
            "Search the offline full-text index instead of the plain filter"))
        self.chk_fulltext.stateChanged.connect(self.refresh)
        self.btn_snapshot = QtWidgets.QPushButton(
            self.t("docs_snapshot", "Snapshot"))
        self.btn_snapshot.clicked.connect(lambda: self.take_snapshot())
        self.snapshot_combo = QtWidgets.QComboBox()
        self.snapshot_combo.setMinimumWidth(140)
        self.snapshot_combo.addItem(self.t("docs_snapshots", "Snapshots"), None)
        self.btn_restore = QtWidgets.QPushButton(
            self.t("docs_restore", "Restore"))
        self.btn_restore.clicked.connect(self.restore_selected_snapshot)
        engine_row.addWidget(self.btn_index)
        engine_row.addWidget(self.btn_update_local)
        engine_row.addWidget(self.chk_fulltext)
        engine_row.addStretch()
        self.engine_label = QtWidgets.QLabel("")
        self.engine_label.setStyleSheet("color: #9aa0a6;")
        engine_row.addWidget(self.engine_label)
        engine_row.addWidget(self.btn_snapshot)
        engine_row.addWidget(self.snapshot_combo)
        engine_row.addWidget(self.btn_restore)
        layout.addLayout(engine_row)

        # ---- filter row ---------------------------------------------------
        filter_row = QtWidgets.QHBoxLayout()
        self.filter_edit = QtWidgets.QLineEdit()
        self.filter_edit.setPlaceholderText(self.t("docs_filter", "Filter documents..."))
        self.filter_edit.textChanged.connect(self.refresh)
        filter_row.addWidget(self.filter_edit, 1)
        self.category_filter = QtWidgets.QComboBox()
        self.category_filter.addItem(self.t("docs_all_categories", "All categories"), "")
        self.category_filter.currentIndexChanged.connect(self.refresh)
        filter_row.addWidget(self.category_filter)
        self.show_archived = QtWidgets.QCheckBox(self.t("docs_show_archived", "Show archived"))
        self.show_archived.setChecked(True)
        self.show_archived.stateChanged.connect(self.refresh)
        filter_row.addWidget(self.show_archived)
        layout.addLayout(filter_row)

        # ---- split: list | editor ----------------------------------------
        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.tree = QtWidgets.QTreeWidget()
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels([
            self.t("docs_col_title", "Title"),
            self.t("docs_col_type", "Type"),
            self.t("docs_col_category", "Category"),
            self.t("docs_col_modified", "Modified"),
        ])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.itemSelectionChanged.connect(self.on_select)
        splitter.addWidget(self.tree)

        edit_side = QtWidgets.QWidget()
        edit_layout = QtWidgets.QVBoxLayout(edit_side)
        self.side_tabs = QtWidgets.QTabWidget()
        edit_layout.addWidget(self.side_tabs, 1)

        # ---- Edit page ----------------------------------------------------
        edit_page = QtWidgets.QWidget()
        edit_page_layout = QtWidgets.QVBoxLayout(edit_page)
        meta_row = QtWidgets.QFormLayout()
        self.title_edit = QtWidgets.QLineEdit()
        self.type_combo = QtWidgets.QComboBox()
        for label, key, _binary in DOCUMENT_TYPES:
            self.type_combo.addItem(label, key)
        self.category_edit = QtWidgets.QLineEdit()
        self.tags_edit = QtWidgets.QLineEdit()
        self.desc_edit = QtWidgets.QLineEdit()
        meta_row.addRow(self.t("docs_field_title", "Title"), self.title_edit)
        meta_row.addRow(self.t("docs_field_type", "Type"), self.type_combo)
        meta_row.addRow(self.t("docs_field_category", "Category"), self.category_edit)
        meta_row.addRow(self.t("docs_field_tags", "Tags"), self.tags_edit)
        meta_row.addRow(self.t("docs_field_description", "Description"), self.desc_edit)
        edit_page_layout.addLayout(meta_row)
        self.body_edit = QtWidgets.QPlainTextEdit()
        self.body_edit.setFont(QFont(DOCS_FONT, DOCS_FONT_SIZE))
        self.body_edit.textChanged.connect(self._mark_dirty)
        edit_page_layout.addWidget(self.body_edit, 1)
        self.side_tabs.addTab(edit_page, self.t("docs_edit_page", "Edit"))

        # ---- Preview page (offline document preview) ----------------------
        preview_page = QtWidgets.QWidget()
        preview_layout = QtWidgets.QVBoxLayout(preview_page)
        preview_row = QtWidgets.QHBoxLayout()
        self.preview_kind = QtWidgets.QComboBox()
        for key, label_key, fallback in PREVIEW_KINDS:
            self.preview_kind.addItem(self.t(label_key, fallback), key)
        self.preview_kind.currentIndexChanged.connect(self.refresh_preview)
        self.btn_preview = QtWidgets.QPushButton(
            self.t("docs_preview_run", "Refresh preview"))
        self.btn_preview.clicked.connect(self.refresh_preview)
        self.chk_ascii_only = QtWidgets.QCheckBox(
            self.t("docs_preview_ascii_only", "ASCII only"))
        self.chk_ascii_only.setChecked(True)
        self.chk_ascii_only.stateChanged.connect(self.refresh_preview)
        self.btn_preview_export = QtWidgets.QPushButton(
            self.t("docs_preview_export", "Export preview"))
        self.btn_preview_export.clicked.connect(self.export_preview)
        preview_row.addWidget(QtWidgets.QLabel(self.t("docs_preview", "Preview:")))
        preview_row.addWidget(self.preview_kind)
        preview_row.addWidget(self.btn_preview)
        preview_row.addWidget(self.chk_ascii_only)
        preview_row.addStretch()
        preview_row.addWidget(self.btn_preview_export)
        preview_layout.addLayout(preview_row)

        self.minimap_view = QtWidgets.QPlainTextEdit()
        self.minimap_view.setReadOnly(True)
        self.minimap_view.setFont(QFont(DOCS_FONT, DOCS_FONT_SIZE - 1))
        self.minimap_view.setMaximumHeight(140)
        self.minimap_view.setVisible(False)
        preview_layout.addWidget(self.minimap_view)

        self.preview_view = QtWidgets.QPlainTextEdit()
        self.preview_view.setReadOnly(True)
        self.preview_view.setFont(QFont(DOCS_FONT, DOCS_FONT_SIZE))
        preview_layout.addWidget(self.preview_view, 1)
        self.side_tabs.addTab(preview_page, self.t("docs_preview_page", "Preview"))

        splitter.addWidget(edit_side)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, 1)

        self.status = QtWidgets.QLabel("")
        self.status.setStyleSheet("color: #9aa0a6;")
        layout.addWidget(self.status)

    def _mark_dirty(self):
        self._dirty = True

    # --------------------------------------------------------- repository
    def open_database(self, path, silent=False):
        """Open (or create) a repository file and load it into the UI."""
        try:
            if self.repo is not None:
                self.repo.close()
            self.repo = DocsRepository(path)
        except Exception as exc:
            if not silent:
                self.status.setText(str(exc))
            return None
        self.db_label.setText(os.path.basename(self.repo.path))
        self.db_label.setToolTip(self.repo.path)
        # Build the offline documentation engine over the same connection and
        # make sure every document is indexed (cheap: cached by checksum).
        try:
            self.index = DocsIndex(self.repo._conn)
            self.reindex_documents(quiet=silent)
        except Exception as exc:
            self.index = None
            if not silent:
                self.status.setText(str(exc))
        self._update_lock_label()
        self.refresh()
        return self.repo

    def _update_lock_label(self):
        if self.repo is None:
            self.lock_label.setText("")
        elif self.repo.is_encrypted():
            self.lock_label.setText(
                self.t("docs_locked", "\U0001f512 encrypted"))
        else:
            self.lock_label.setText("")

    def new_database(self):
        path, _filter = QtWidgets.QFileDialog.getSaveFileName(
            self, self.t("docs_new_db", "New database"),
            os.path.join(os.path.dirname(self._default_db_path()),
                         "docs_db.sqlite"),
            "SQLite database (*.sqlite *.db);;All files (*)")
        if path:
            self.open_database(path)
            self.status.setText(self.t("docs_db_opened", "database: {path}").format(
                path=path))

    def open_database_dialog(self):
        path, _filter = QtWidgets.QFileDialog.getOpenFileName(
            self, self.t("docs_open_db", "Open..."),
            os.path.dirname(self._default_db_path()),
            "SQLite database (*.sqlite *.db);;All files (*)")
        if path:
            self.open_database(path)
            self.status.setText(self.t("docs_db_opened", "database: {path}").format(
                path=path))

    def save_database_as(self):
        """Copy the open repository to a new file (a portable DB file)."""
        if self.repo is None:
            return ""
        path, _filter = QtWidgets.QFileDialog.getSaveFileName(
            self, self.t("docs_save_db", "Save as..."),
            os.path.join(os.path.dirname(self.repo.path), "docs_export.sqlite"),
            "SQLite database (*.sqlite *.db);;All files (*)")
        if not path:
            return ""
        import shutil
        try:
            self.repo._conn.commit()
            shutil.copyfile(self.repo.path, path)
        except Exception as exc:
            self.status.setText(str(exc))
            return ""
        self.status.setText(self.t("docs_db_saved", "saved: {path}").format(
            path=path))
        return path

    def export_database(self, fmt="json"):
        """Export the repository to JSON / CSV / SQL for other programs."""
        if self.repo is None:
            return ""
        labels = {"json": "JSON (*.json)", "csv": "CSV (*.csv)",
                  "sql": "SQL (*.sql)"}
        default = os.path.splitext(self.repo.path)[0] + "." + fmt
        path, _filter = QtWidgets.QFileDialog.getSaveFileName(
            self, self.t("docs_export", "Export..."), default, labels.get(fmt))
        if not path:
            return ""
        try:
            if fmt == "csv":
                self.repo.export_csv(path)
            elif fmt == "sql":
                self.repo.export_sql(path)
            else:
                self.repo.export_json(path)
        except Exception as exc:
            self.status.setText(str(exc))
            return ""
        self.status.setText(self.t("docs_exported", "exported: {path}").format(
            path=path))
        return path

    # ------------------------------------------------------------- password
    def manage_password(self):
        """Set, change or clear the repository."""
        if self.repo is None:
            return
        if self.repo.is_encrypted():
            text, ok = QtWidgets.QInputDialog.getText(
                self, self.t("docs_password", "Password..."),
                self.t("docs_password_ask",
                       "Enter the current password (or leave empty to remove):"),
                QtWidgets.QLineEdit.Password)
            if not ok:
                return
            if text:
                if not self.repo.unlock(text):
                    self.status.setText(self.t("docs_bad_password",
                                               "wrong password"))
                    return
            else:
                self.repo.set_password("")
                self.status.setText(self.t("docs_password_cleared",
                                           "password removed"))
                self._update_lock_label()
                self.refresh()
                return
        text, ok = QtWidgets.QInputDialog.getText(
            self, self.t("docs_password", "Password..."),
            self.t("docs_password_new", "New password for this database:"),
            QtWidgets.QLineEdit.Password)
        if not ok or not text:
            return
        try:
            self.repo.set_password(text)
        except Exception as exc:
            self.status.setText(str(exc))
            return
        self.status.setText(self.t("docs_password_set", "password set"))
        self._update_lock_label()
        self.refresh()

    def _ensure_unlocked(self):
        """Prompt for the password when the repository is encrypted+locked."""
        if self.repo is None or not self.repo.is_encrypted():
            return True
        if self.repo.key is not None:
            return True
        text, ok = QtWidgets.QInputDialog.getText(
            self, self.t("docs_password", "Password..."),
            self.t("docs_unlock", "This database is encrypted. Password:"),
            QtWidgets.QLineEdit.Password)
        if not ok or not text:
            return False
        if not self.repo.unlock(text):
            self.status.setText(self.t("docs_bad_password", "wrong password"))
            return False
        self.refresh()
        return True

    # -------------------------------------------------------------- people
    def refresh(self):
        """Reload the document list from the repository."""
        if self.repo is None:
            return
        tree = self.tree
        # The selection is remembered across the rebuild. An operation that
        # changes a document refreshes the list, and losing the user's place
        # every time would make the operations unusable in sequence: the next
        # one would act on nothing because nothing was selected any more.
        previous = tree.currentItem()
        selected_id = (previous.data(0, QtCore.Qt.UserRole)
                       if previous is not None else None)
        tree.blockSignals(True)
        tree.clear()
        needle = self.filter_edit.text()
        include_archived = self.show_archived.isChecked()
        category = self.category_filter.currentData() or ""
        if self.chk_fulltext.isChecked() and self.index is not None and needle:
            # Offline full-text search over the content index.
            docs = self.engine_search(needle, category=category or None)
            if not include_archived:
                docs = [d for d in docs if not d.get("archived")]
        else:
            docs = self.repo.search(needle, include_archived=include_archived)
            if category:
                docs = [d for d in docs if d.get("category") == category]
        for doc in docs:
            title = doc.get("title", "")
            if doc.get("archived"):
                title = "\U0001f5c4 " + title
            item = QtWidgets.QTreeWidgetItem([
                title, doc.get("doc_type", ""), doc.get("category", ""),
                doc.get("modified", "")])
            item.setData(0, QtCore.Qt.UserRole, doc.get("id"))
            if doc.get("locked"):
                item.setForeground(0, QtGui.QColor("#f7c66d"))
            tree.addTopLevelItem(item)
        for column in range(tree.columnCount()):
            tree.resizeColumnToContents(column)
        # Put the selection back on the same document when it is still listed.
        if selected_id is not None:
            for index in range(tree.topLevelItemCount()):
                item = tree.topLevelItem(index)
                if item.data(0, QtCore.Qt.UserRole) == selected_id:
                    tree.setCurrentItem(item)
                    break
        tree.blockSignals(False)
        self._reload_categories()
        self.count_label.setText(self.t(
            "docs_count", "{count} document(s)").format(
            count=self.repo.count(include_archived=include_archived)))
        self._refresh_engine_label()

    def _refresh_engine_label(self):
        """Show the offline engine status (index mode, document count)."""
        if not hasattr(self, "engine_label"):
            return
        info = self.engine_info()
        self.engine_label.setText(self.t(
            "docs_engine_status", "index: {mode} - {count} document(s)").format(
            mode=info.get("content_index", "offline"),
            count=info.get("indexed", 0)))
        self.refresh_snapshots()

    def _reload_categories(self):
        current = self.category_filter.currentData()
        self.category_filter.blockSignals(True)
        self.category_filter.clear()
        self.category_filter.addItem(
            self.t("docs_all_categories", "All categories"), "")
        for category in self.repo.categories() if self.repo else []:
            self.category_filter.addItem(category, category)
        index = self.category_filter.findData(current)
        if index >= 0:
            self.category_filter.setCurrentIndex(index)
        self.category_filter.blockSignals(False)

    def on_select(self):
        item = self.tree.currentItem()
        if item is None or self.repo is None:
            return
        doc_id = item.data(0, QtCore.Qt.UserRole)
        self.load_document(doc_id)

    def load_document(self, doc_id):
        """Fill the editor pane from one document."""
        if not self._ensure_unlocked():
            return
        doc = self.repo.get(doc_id)
        if doc is None:
            return
        self._current_id = doc_id
        self.title_edit.setText(doc.get("title", ""))
        index = self.type_combo.findData(doc.get("doc_type", "text"))
        self.type_combo.setCurrentIndex(index if index >= 0 else 0)
        self.category_edit.setText(doc.get("category", ""))
        self.tags_edit.setText(doc.get("tags", ""))
        self.desc_edit.setText(doc.get("description", ""))
        self.body_edit.setPlainText(doc.get("body", ""))
        if doc.get("locked"):
            self.body_edit.setPlaceholderText(
                self.t("docs_locked_body", "(encrypted - enter password)"))
        self._dirty = False
        self.refresh_preview()

    # ---------------------------------------------------------------- CRUD
    def add_document(self):
        """Create a blank document and select it for editing."""
        if self.repo is None:
            return
        if not self._ensure_unlocked():
            return
        doc_id = self.repo.add(
            self.t("docs_untitled", "untitled"), "", "Text",
            self.category_edit.text() or "General")
        self.refresh()
        self.select_id(doc_id)
        self.title_edit.setFocus()
        self.title_edit.selectAll()
        self.status.setText(self.t("docs_added", "document added"))

    def save_current(self):
        """Write the editor pane back into the repository."""
        if self.repo is None or self._current_id is None:
            return False
        if not self._ensure_unlocked():
            return False
        try:
            self.repo.update(
                self._current_id,
                title=self.title_edit.text() or "untitled",
                doc_type=self.type_combo.currentData() or "text",
                category=self.category_edit.text() or "General",
                tags=self.tags_edit.text(),
                description=self.desc_edit.text(),
                body=self.body_edit.toPlainText())
        except Exception as exc:
            self.status.setText(str(exc))
            return False
        self._dirty = False
        self.index_document_into_engine(self._current_id)
        self.refresh()
        self.select_id(self._current_id)
        self.status.setText(self.t("docs_saved", "document saved"))
        self._notify_audit("config_changed")
        return True

    def toggle_archive(self):
        """Archive or unarchive the selected documents."""
        if self.repo is None:
            return
        for doc_id in self._selected_ids():
            doc = self.repo.get(doc_id)
            if doc is not None:
                self.repo.update(doc_id, archived=not doc.get("archived"))
        self.refresh()
        self.status.setText(self.t("docs_archived_changed", "archive updated"))

    def delete_selected(self):
        ids = self._selected_ids()
        if not ids or self.repo is None:
            return
        answer = QtWidgets.QMessageBox.question(
            self, self.t("docs_delete", "Delete"),
            self.t("docs_delete_ask", "Delete {count} document(s)?").format(
                count=len(ids)),
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No)
        if answer != QtWidgets.QMessageBox.Yes:
            return
        self.repo.delete(ids)
        if self.index is not None:
            for doc_id in ids:
                self.index.remove_document(doc_id)
        self._current_id = None
        self.body_edit.clear()
        self.refresh()
        self.status.setText(self.t("docs_deleted", "document(s) deleted"))

    def _selected_ids(self):
        return [self.tree.topLevelItem(i).data(0, QtCore.Qt.UserRole)
                for i in range(self.tree.topLevelItemCount())
                if self.tree.topLevelItem(i).isSelected()]

    def select_id(self, doc_id):
        """Select a document in the tree and load it into the editor."""
        for index in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(index)
            if item.data(0, QtCore.Qt.UserRole) == doc_id:
                item.setSelected(True)
                self.tree.setCurrentItem(item)
                # Load explicitly: selection change signals are not guaranteed
                # to fire in a headless/test build, and the editor must always
                # show the document we just selected.
                self.load_document(doc_id)
                return True
        return False

    # -------------------------------------------------------------- import
    def import_files(self):
        """Import files of any common type into the repository."""
        if not self._ensure_unlocked():
            return
        paths, _filter = QtWidgets.QFileDialog.getOpenFileNames(
            self, self.t("docs_import", "Import files..."), "",
            "Documents (*);;All files (*)")
        if not paths:
            return
        added = self.import_paths(paths)
        self.refresh()
        self.status.setText(self.t("docs_imported",
                                   "imported {count} file(s)").format(count=added))

    def import_paths(self, paths):
        """Import an explicit list of paths; returns the number added."""
        if self.repo is None:
            return 0
        added = 0
        for path in paths:
            try:
                doc_type = guess_document_type(path)
                if is_binary_type(doc_type):
                    size = os.path.getsize(path)
                    body = self.t(
                        "docs_binary_note",
                        "(binary file, {size} bytes)").format(size=size)
                else:
                    with open(path, "r", encoding="utf-8",
                              errors="replace") as handle:
                        body = handle.read()
            except OSError:
                continue
            except OSError:
                continue
            doc_id = self.repo.add(
                os.path.basename(path), body, doc_type,
                self.category_edit.text() or "Imported", "", path)
            # Index the new document and remember where it came from, so an
            # automatic update can catch up when the local file changes.
            self.index_document_into_engine(doc_id, source_path=path)
            added += 1
        return added

    # ------------------------------------------- offline documentation engine
    def index_document_into_engine(self, doc_id, source_path=None):
        """(Re)index one document in the knowledge engine, if available."""
        if self.index is None or self.repo is None:
            return False
        doc = self.repo.get(doc_id)
        if doc is None:
            return False
        if source_path is None:
            source_path = self.index.source_path_for(doc_id)
        try:
            self.index.index_document(doc, source_path=source_path or None)
            return True
        except Exception:
            return False

    def reindex_documents(self, quiet=False):
        """Rebuild the whole knowledge base from the repository."""
        if self.index is None or self.repo is None:
            return 0
        try:
            count = self.index.reindex_all(self.repo.list_documents())
        except Exception as exc:
            if not quiet:
                self.status.setText(str(exc))
            return 0
        if not quiet:
            self.status.setText(self.t(
                "docs_reindexed", "indexed {count} document(s)").format(
                count=count))
        return count

    def engine_search(self, query, category=None, doc_type=None, tag=None):
        """Offline full-text search: query the index, return documents."""
        if self.index is None:
            return []
        rows = self.index.search(query, category=category, doc_type=doc_type,
                                 tag=tag)
        documents = []
        for row in rows:
            doc = self.repo.get(row["doc_id"]) if self.repo else None
            if doc is not None:
                documents.append(doc)
        return documents

    # --------------------------------------------- advanced search (13 kinds)
    def advanced_search(self, query, limit=100):
        """Search by any of the thirteen kinds, returning documents.

        The query language selects the kind (`heading:install`, `author:kowalski`,
        `~backup`, `version>=2.0`, ...) and the same parsed query is available to
        the caller, so a UI can show *why* a document matched rather than only
        that it did.

        The `documents` argument passed to the search module is the whole
        repository, because the section and heading kinds read a document's
        structure from its body -- structure the index stores only as text.
        """
        if self.index is None:
            return []
        try:
            from . import docs_search
        except Exception:
            return []
        documents = self.repo.list_documents() if self.repo else []
        hits = docs_search.run(self.index, query, documents, limit=limit)
        return self.hits_to_documents(hits)

    def hits_to_documents(self, hits):
        """The documents behind a list of search hits (`[{doc_id, score,
        why, kind}]`), in the order the search ranked them.

        Kept separate so a caller that already holds hits -- a replay, a report,
        a test -- can turn them into documents without searching again.
        """
        documents = []
        for hit in hits or ():
            doc_id = hit.get("doc_id")
            if doc_id is None or self.repo is None:
                continue
            doc = self.repo.get(doc_id)
            if doc is not None:
                # The hit's own explanation travels with the document, so a
                # view can show why it matched without re-running the search.
                doc = dict(doc)
                doc["match"] = {"score": hit.get("score"),
                                "why": hit.get("why", ""),
                                "kind": hit.get("kind", "")}
                documents.append(doc)
        return documents

    def search_kinds(self):
        """The search kinds the documentation offers, for a picker or a CLI."""
        try:
            from . import docs_search
        except Exception:
            return []
        return [{"key": key, "label": docs_search.SEARCH_KIND_LABELS[key],
                 "meaning": docs_search.SEARCH_KIND_MEANINGS[key],
                 "example": docs_search.SEARCH_KIND_EXAMPLES[key]}
                for key in docs_search.SEARCH_KIND_KEYS]

    def explain_search(self, query):
        """A parsed query as ASCII lines, so a search explains itself."""
        try:
            from . import docs_search
        except Exception:
            return ["(advanced search unavailable)"]
        return docs_search.format_query(docs_search.parse_query(query))

    def engine_info(self):
        """Describe the knowledge engine for the UI / audit report.

        The advanced-search capabilities are folded in here, so a caller
        that already reads the engine's description learns what the search
        can do without a second call and without knowing the module name.
        """
        if self.index is None:
            return {"content_index": "offline", "indexed": 0, "cached": 0,
                    "snapshots": 0, "offline": True}
        info = self.index.engine_info()
        try:
            from . import docs_search
            info["advanced_search"] = docs_search.search_capabilities()
            info["search_kinds"] = list(docs_search.SEARCH_KIND_KEYS)
        except Exception:
            info["search_kinds"] = []
        return info

    def take_snapshot(self, name=None):
        """Store a point-in-time snapshot of the whole document base."""
        if self.index is None or self.repo is None:
            return None
        documents = self.repo.list_documents()
        snapshot_id = self.index.snapshot(
            name or self.t("docs_snapshot_default", "snapshot"), documents)
        self.refresh_snapshots()
        self.status.setText(self.t(
            "docs_snapshot_taken", "snapshot stored ({count} docs)").format(
            count=len(documents)))
        return snapshot_id

    def restore_snapshot(self, snapshot_id):
        """Restore the documents captured in a snapshot (adds copies)."""
        if self.index is None or self.repo is None:
            return 0

        def _add(doc):
            self.repo.add(doc.get("title", "untitled"), doc.get("body", ""),
                          doc.get("doc_type", "text"),
                          doc.get("category", "Restored"), doc.get("tags", ""),
                          doc.get("description", ""), False, doc.get("meta"))

        restored = self.index.restore_snapshot(snapshot_id, _add)
        self.reindex_documents(quiet=True)
        self.refresh()
        self.status.setText(self.t(
            "docs_snapshot_restored", "restored {count} document(s)").format(
            count=restored))
        return restored

    def update_stale_documents(self):
        """Re-read tracked local files that changed on disk (offline).

        Nothing is downloaded: for every tracked source file whose size or
        modification time differs from the indexed one, the file is read again
        and the document + index are refreshed. Returns the number updated.
        """
        if self.index is None or self.repo is None:
            return 0
        updated = 0
        for stale in self.index.stale_sources():
            text, _size, _mtime = self.index.read_source(stale["path"])
            if text is None:
                continue
            self.repo.update(stale["doc_id"], body=text)
            self.index_document_into_engine(stale["doc_id"], source_path=stale["path"])
            updated += 1
        if updated:
            self.refresh()
            self.status.setText(self.t(
                "docs_updated_stale", "updated {count} document(s)").format(
                count=updated))
        else:
            self.status.setText(self.t(
                "docs_no_updates", "everything up to date"))
        return updated

    def refresh_snapshots(self):
        """Reload the snapshot picker from the engine (no-op if absent)."""
        combo = getattr(self, "snapshot_combo", None)
        if combo is None or self.index is None:
            return
        combo.blockSignals(True)
        combo.clear()
        combo.addItem(self.t("docs_snapshots", "Snapshots"), None)
        for snap in self.index.snapshots():
            combo.addItem("%s (%d)" % (snap["name"], snap["doc_count"]),
                          snap["id"])
        combo.blockSignals(False)


    def restore_selected_snapshot(self):
        """Restore the snapshot currently chosen in the picker."""
        snapshot_id = self.snapshot_combo.currentData()
        if snapshot_id is None:
            self.status.setText(self.t("docs_no_snapshot", "no snapshot chosen"))
            return 0
        return self.restore_snapshot(snapshot_id)

    # ------------------------------------------------ offline document preview
    def current_preview_kind(self):
        """The preview view chosen in the combo."""
        combo = getattr(self, "preview_kind", None)
        return (combo.currentData() if combo is not None else "ascii") or "ascii"

    def _current_document(self):
        """The document dict currently selected (decrypted), or None."""
        if self.repo is None or self._current_id is None:
            return None
        return self.repo.get(self._current_id)

    def preview_text(self, doc=None):
        """Render the selected preview view for a document as plain text."""
        doc = doc or self._current_document()
        if doc is None:
            return ""
        kind = self.current_preview_kind()
        body = doc.get("body") or ""
        if kind == "typed":
            # The document type decides the best offline preview strategy.
            return self.index.type_preview(
                doc.get("doc_type", "text"), body,
                ascii_only=self.chk_ascii_only.isChecked())
        if kind == "ascii":
            return self.index.preview_ascii(
                body, width=100,
                ascii_only=getattr(self, "chk_ascii_only", None) is not None
                and self.chk_ascii_only.isChecked())
        if kind == "structure":
            return self._render_structure(self.index.document_structure(body))
        if kind == "minimap":
            return "\n".join(self.index.minimap(body, rows=16, buckets=72))
        if kind == "metadata":
            return self._render_metadata(self.index.document_metadata(doc))
        if kind == "links":
            return self._render_links(self.index.document_links(
                doc, self.repo.list_documents() if self.repo else None))
        if kind == "versions":
            return self._render_versions(doc)
        return body

    def _render_structure(self, records):
        if not records:
            return self.t("docs_preview_empty", "(nothing to preview)")
        lines = []
        for rec in records:
            if rec["kind"] == "heading":
                lines.append("%s %s" % ("  " * rec["level"] + "#" * rec["level"],
                                        rec["text"]))
            elif rec["kind"] == "section":
                lines.append("== %s ==" % rec["text"])
            elif rec["kind"] == "code":
                lines.append("[code] line %d: %s" % (rec["line"], rec["text"]))
            elif rec["kind"] == "list":
                lines.append("[list] line %d: %s" % (rec["line"], rec["text"]))
            elif rec["kind"] == "rule":
                lines.append("-" * 40)
        return "\n".join(lines)

    def _render_metadata(self, meta):
        rows = [
            (self.t("docs_field_title", "Title"), meta["title"]),
            (self.t("docs_field_type", "Type"), meta["doc_type"]),
            (self.t("docs_field_category", "Category"), meta["category"]),
            (self.t("docs_field_tags", "Tags"), ", ".join(meta["tags"])),
            (self.t("docs_preview_created", "Created"), meta["created"]),
            (self.t("docs_col_modified", "Modified"), meta["modified"]),
            (self.t("docs_preview_chars", "Characters"), meta["characters"]),
            (self.t("docs_preview_lines", "Lines"), meta["lines"]),
            (self.t("docs_preview_words", "Words"), meta["words"]),
            (self.t("docs_preview_unique", "Unique words"), meta["unique_words"]),
            (self.t("docs_preview_checksum", "Checksum"), meta["checksum"]),
            (self.t("docs_preview_archived", "Archived"),
             str(meta["archived"])),
            (self.t("docs_preview_encrypted", "Encrypted"),
             str(meta["encrypted"])),
        ]
        for key, value in (meta.get("meta") or {}).items():
            rows.append(("meta:%s" % key, value))
        width = max((len(str(label)) for label, _ in rows), default=0)
        return "\n".join("%-*s : %s" % (width, label, value)
                          for label, value in rows)

    def _render_links(self, links):
        lines = []
        lines.append("%s: %s" % (self.t("docs_field_tags", "Tags"),
                                 ", ".join(links["tags"]) or "-"))
        lines.append("%s: %s" % (self.t("docs_field_category", "Category"),
                                 links["category"] or "-"))
        lines.append("%s: %s" % (self.t("docs_preview_meta_keys", "Meta keys"),
                                 ", ".join(links["meta_keys"]) or "-"))
        lines.append("")
        lines.append(self.t("docs_preview_related", "Related documents:"))
        if not links["related"]:
            lines.append("  " + self.t("docs_preview_none", "(none)"))
        for item in links["related"]:
            lines.append("  - %s  [%s]" % (item["title"],
                                           "; ".join(item["reasons"])))
        return "\n".join(lines)

    def _render_versions(self, doc):
        snapshot_id = self.snapshot_combo.currentData() if hasattr(
            self, "snapshot_combo") else None
        if snapshot_id is None:
            # Fall back to the newest snapshot so the view still works
            # before the user picks one explicitly.
            available = self.index.snapshots() if self.index else []
            snapshot_id = available[0]["id"] if available else None
        if snapshot_id is None:
            return self.t("docs_preview_no_snapshot",
                          "(choose a snapshot to compare against)")
        old_text = self.index.snapshot_text(snapshot_id, doc.get("id"))
        diff = self.index.compare_versions(old_text, doc.get("body") or "")
        if diff["identical"]:
            return self.t("docs_preview_identical",
                          "no changes compared with the snapshot")
        lines = [self.t(
            "docs_preview_diff_summary",
            "+{added} / -{removed} line(s), {changed} changed block(s)").format(
                added=diff["additions"], removed=diff["removals"],
                changed=diff["changed"]), ""]
        for hunk in diff["hunks"]:
            lines.append("[%s] old %d -> new %d" % (
                hunk["kind"], hunk["old_line"], hunk["new_line"]))
            for text in (hunk["old_text"] or "").splitlines():
                lines.append("  - " + text)
            for text in (hunk["new_text"] or "").splitlines():
                lines.append("  + " + text)
        return "\n".join(lines)

    def refresh_preview(self):
        """Re-render the preview pane for the selected document."""
        if not hasattr(self, "preview_view"):
            return
        if self.index is None or self.repo is None:
            self.preview_view.setPlainText("")
            return
        doc = self._current_document()
        if doc is None:
            self.preview_view.setPlainText("")
            return
        kind = self.current_preview_kind()
        if hasattr(self, "minimap_view"):
            self.minimap_view.setVisible(kind == "minimap")
        text = self.preview_text(doc)
        self.preview_view.setPlainText(text)
        if kind == "minimap" and hasattr(self, "minimap_view"):
            self.minimap_view.setPlainText(
                "\n".join(self.index.minimap(doc.get("body") or "",
                                             rows=6, buckets=100)))

    def export_preview(self):
        """Write the current preview text to a file (offline)."""
        text = self.preview_view.toPlainText() if hasattr(
            self, "preview_view") else ""
        if not text:
            self.status.setText(self.t("docs_preview_nothing_export",
                                       "nothing to export"))
            return ""
        default = os.path.join(
            os.path.dirname(self.repo.path) if self.repo else os.getcwd(),
            "preview.txt")
        path, _filter = QtWidgets.QFileDialog.getSaveFileName(
            self, self.t("docs_preview_export", "Export preview"), default,
            "Text (*.txt);;All files (*)")
        if not path:
            return ""
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(text)
        except OSError as exc:
            self.status.setText(str(exc))
            return ""
        self.status.setText(self.t("docs_exported", "exported: {path}").format(
            path=path))
        return path

    # --------------------------------------------------------- cross-tab
    def search_documents(self):
        """Documents as (name, code, category) for the Search engine."""
        if self.repo is None:
            return []
        docs = []
        for doc in self.repo.list_documents():
            docs.append((doc.get("title", ""), doc.get("body", "") or "",
                         doc.get("doc_type", "Text")))
        return docs

    # ------------------------------------------- document actions (11)
    def _selected_document(self):
        """The document the tree has selected (or None).

        One helper rather than the four lines repeated in every method below:
        the tree stores the id in its first column's user data, and a method
        that read it differently from its neighbour would act on another
        document than the one on screen.
        """
        if self.repo is None:
            return None
        item = self.tree.currentItem() if hasattr(self, "tree") else None
        if item is None:
            return None
        doc_id = item.data(0, QtCore.Qt.UserRole)
        return self.repo.get(doc_id)

    def run_document_action(self, action, **kwargs):
        """Run one of the eleven document operations on the selection.

        The operation runs head-less in `tabs.docs_actions`; this method is the
        tab's binding to it, so a button, an API call and a CLI call all end up
        in the same place with the same result shape.
        """
        from . import docs_actions
        doc = self._selected_document()
        doc_id = kwargs.pop("doc_id", None)
        if doc_id is None and doc is not None:
            doc_id = doc.get("id")
        if doc_id is None:
            self.status.setText(self.t("docs_no_selection",
                                       "(no document selected)"))
            return docs_actions.result(action, False, None,
                                       "no document selected")
        outcome = docs_actions.run(self.repo, action, doc_id,
                                   index=getattr(self, "index", None),
                                   **kwargs)
        self._after_action(action, outcome)
        return outcome

    def _after_action(self, action, outcome):
        """Refresh what an operation changed, and say what happened.

        One place rather than one per operation, because every operation that
        changed something needs the same three things: the tree repainted, the
        text re-exported to the search index, and the audit told. Doing it per
        operation is how one of them ends up not doing it.
        """
        if not outcome.get("ok"):
            self.status.setText(outcome.get("detail", "operation failed"))
            return
        if action in ("edit", "move", "delete", "tag", "copy",
                      "mark_important", "pin"):
            try:
                self.refresh()
            except Exception:
                pass
        self.status.setText(outcome.get("detail", ""))
        self._notify_audit("docs_" + action, where=outcome.get("doc_id"),
                           detail=outcome.get("detail", ""))

    def document_action(self, action):
        """Run a plain operation on the selection (no arguments)."""
        return self.run_document_action(action)

    def edit_document_field(self, field, value):
        """Edit one field of the selected document."""
        return self.run_document_action("edit", **{field: value})

    def move_document_to(self, category):
        """Move the selected document to a category."""
        return self.run_document_action("move", category=category)

    def copy_document_as(self, title=""):
        """Copy the selected document under a new title."""
        return self.run_document_action("copy", title=title)

    def toggle_important(self):
        """Flip the selected document's important flag."""
        from . import docs_actions
        doc = self._selected_document()
        if doc is None:
            self.status.setText(self.t("docs_no_selection",
                                       "(no document selected)"))
            return None
        return self.run_document_action(
            "mark_important", important=not docs_actions.is_important(doc))

    def pin_document_to(self, project, detach=False):
        """Pin the selected document to a project (or detach it)."""
        return self.run_document_action("pin", project=project, detach=detach)

    def tag_document(self, add=(), remove=()):
        """Add and remove tags on the selected document."""
        return self.run_document_action("tag", add=add, remove=remove)

    def link_document_to(self, kind, name, detach=False):
        """Link the selected document to a module, a script or a project."""
        return self.run_document_action("link", kind=kind, name=name,
                                        detach=detach)

    def export_document_as(self, fmt="txt", path=""):
        """Export the selected document as TXT, JSON or LOG."""
        return self.run_document_action("export", fmt=fmt, path=path)

    # ------------------------------------- module integrations (6)
    def open_in_module(self, target, **kwargs):
        """Open the selected document in one of the six other modules.

        Returns the bridge's own result, so a caller can tell "the tab is not
        open" from "the tab refused it" without reading a string.
        """
        from . import docs_integration
        doc = self._selected_document()
        if doc is None:
            self.status.setText(self.t("docs_no_selection",
                                       "(no document selected)"))
            return docs_integration.bridge_result(target, False,
                                                  "no document selected")
        outcome = docs_integration.open_in(self.window, target, doc, **kwargs)
        self.status.setText(outcome.get("detail", ""))
        self._notify_audit("docs_open_" + target,
                           where=doc.get("title", ""),
                           detail=outcome.get("detail", ""))
        return outcome

    def open_in_diff(self):
        """Open the selected document in the Diff tab, against its snapshot."""
        from . import docs_actions, docs_integration
        doc = self._selected_document()
        if doc is None:
            self.status.setText(self.t("docs_no_selection",
                                       "(no document selected)"))
            return None
        # The previous version, when the knowledge index holds a snapshot of
        # this document: that is what makes the diff a comparison of two
        # versions rather than of a document with nothing.
        previous = None
        if getattr(self, "index", None) is not None and self.index.snapshots():
            result = docs_actions.diff_versions(
                self.repo, self.index, doc.get("id"))
            if result.get("ok"):
                previous = {"name": doc.get("title", ""),
                            "text": result["changed"]["diff"].get("old_text", "")}
        outcome = docs_integration.open_in(self.window, "diff", doc,
                                           previous=previous)
        self.status.setText(outcome.get("detail", ""))
        return outcome

    def open_in_analyzer(self):
        return self.open_in_module("analyzer")

    def open_in_runner(self, run=False):
        return self.open_in_module("runner", run=run)

    def open_in_debugger(self):
        return self.open_in_module("debugger")

    def open_in_audit(self, action="docs_open"):
        return self.open_in_module("audit", action=action)

    # -------------------------------------------- reports (4 kinds)
    def generate_report(self, kind="document", store=True):
        """Build a report from the selected document.

        When `store` is set the report is written into the repository as a new
        document of type `report`, which is what makes it reachable from the
        Reports tab without a second store: the report *is* a document, and it
        inherits the search, the export and the links for free.
        """
        from . import docs_integration
        doc = self._selected_document()
        if doc is None:
            self.status.setText(self.t("docs_no_selection",
                                       "(no document selected)"))
            return None
        record = docs_integration.report_document(
            doc, kind, index=getattr(self, "index", None))
        if not record:
            self.status.setText(self.t("docs_report_unknown",
                                       "unknown report kind: {kind}").format(
                kind=kind))
            return None
        if store and self.repo is not None:
            try:
                new_id = self.repo.add(
                    record["title"], body=record["text"],
                    doc_type="report", category="Documentation",
                    tags="report, %s" % kind,
                    description="Generated from document %s" % doc.get("id"),
                    meta={"_generated_from": doc.get("id"),
                          "_report_kind": kind})
                record["doc_id"] = new_id
            except Exception as exc:
                record["store_error"] = str(exc)
        self.status.setText(self.t(
            "docs_report_generated",
            "report '{title}' generated ({lines} line(s))").format(
                title=record.get("title", ""), lines=record.get("lines", 0)))
        self._notify_audit("docs_report", where=doc.get("title", ""),
                           detail=kind)
        if store:
            try:
                self.refresh()
            except Exception:
                pass
        return record

    def report_from_document(self, kind="document"):
        """The report's text, without storing it (for a preview or a caller)."""
        from . import docs_integration
        doc = self._selected_document()
        if doc is None:
            return ""
        return docs_integration.report_for(doc, kind,
                                          index=getattr(self, "index", None))[1]

    def document_link_table(self):
        """The links of the selected document, as plain data."""
        from . import docs_actions
        doc = self._selected_document()
        if doc is None:
            return {}
        return docs_actions.links_of(doc)

    def send_to_editor(self):
        """Open the selected document in the Editor tab."""
        if self.repo is None:
            return
        item = self.tree.currentItem()
        if item is None:
            return
        doc = self.repo.get(item.data(0, QtCore.Qt.UserRole))
        if doc is None:
            return
        editor = getattr(self.window, "editor_tab", None)
        if editor is None or not hasattr(editor, "editor"):
            self.status.setText(self.t("docs_no_editor", "editor unavailable"))
            return
        try:
            entry = {
                "id": None, "name": doc.get("title", "document"),
                "category": doc.get("doc_type", "Text"),
                "tags": doc.get("tags", ""), "desc": doc.get("description", ""),
                "code": doc.get("body", ""), "date": _now(),
            }
            loader = getattr(editor, "load_script", None)
            if callable(loader):
                loader(entry)
            else:
                editor.editor.setPlainText(doc.get("body", ""))
            tabs = getattr(self.window, "tabs", None)
            if tabs is not None:
                tabs.setCurrentWidget(editor)
            self.status.setText(self.t("docs_sent_editor",
                                       "document sent to the editor"))
        except Exception as exc:
            self.status.setText(str(exc))

    def send_to_search(self):
        """Run a full-text search of the selected document's text."""
        item = self.tree.currentItem()
        if item is None or self.repo is None:
            return ""
        doc = self.repo.get(item.data(0, QtCore.Qt.UserRole))
        if doc is None:
            return ""
        search = getattr(self.window, "search_tab", None)
        snippet = (doc.get("body") or "").strip().splitlines()
        needle = snippet[0][:60] if snippet else doc.get("title", "")
        if search is not None and hasattr(search, "query_edit"):
            search.query_edit.setText(needle)
            tabs = getattr(self.window, "tabs", None)
            if tabs is not None:
                tabs.setCurrentWidget(search)
        return needle

    def _notify_audit(self, event, **payload):
        notifier = getattr(self.window, "notify_audit", None)
        if callable(notifier):
            try:
                notifier(event, **payload)
            except Exception:
                pass

    # ------------------------------------------------------- translation
    def retranslate(self, table):
        """Apply the main window's language table to this tab."""
        def label(key, fallback):
            return table.get(key, fallback)

        self.title_label.setText(label("docs", "DOCS"))
        self.btn_new_db.setText(label("docs_new_db", "New database"))
        self.btn_open_db.setText(label("docs_open_db", "Open..."))
        self.btn_save_db.setText(label("docs_save", "Save"))
        self.act_save_doc.setText(label("docs_save_doc", "Save document"))
        self.act_save_db.setText(label("docs_save_db", "Save database as..."))
        self.btn_password.setText(label("docs_password", "Password..."))
        self.btn_export.setText(label("docs_export", "Export..."))
        self.btn_add.setText(label("docs_add", "Add document"))
        self.btn_import.setText(label("docs_import", "Import files..."))
        self.btn_archive.setText(label("docs_archive", "Archive"))
        self.btn_delete.setText(label("docs_delete", "Delete"))
        self.btn_edit.setText(label("docs_edit_external", "Open in editor"))
        self.btn_search.setText(label("docs_search_tab", "Search code"))
        self.btn_index.setText(label("docs_index", "Rebuild index"))
        self.btn_update_local.setText(
            label("docs_update_local", "Update local docs"))
        self.chk_fulltext.setText(label("docs_fulltext", "Full-text index"))
        self.btn_snapshot.setText(label("docs_snapshot", "Snapshot"))
        self.btn_restore.setText(label("docs_restore", "Restore"))
        self.filter_edit.setPlaceholderText(
            label("docs_filter", "Filter documents..."))
        self.show_archived.setText(label("docs_show_archived", "Show archived"))
        self.tree.setHeaderLabels([
            label("docs_col_title", "Title"),
            label("docs_col_type", "Type"),
            label("docs_col_category", "Category"),
            label("docs_col_modified", "Modified"),
        ])
        self.side_tabs.setTabText(0, label("docs_edit_page", "Edit"))
        self.side_tabs.setTabText(1, label("docs_preview_page", "Preview"))
        for index, (key, label_key, fallback) in enumerate(PREVIEW_KINDS):
            self.preview_kind.setItemText(index, label(label_key, fallback))
        for index, (label_text, key, _binary) in enumerate(DOCUMENT_TYPES):
            self.type_combo.setItemText(index, type_label(key, table))
        self.btn_preview.setText(label("docs_preview_run", "Refresh preview"))
        self.chk_ascii_only.setText(
            label("docs_preview_ascii_only", "ASCII only"))
        self.btn_preview_export.setText(
            label("docs_preview_export", "Export preview"))
        self.refresh()
        self.refresh_preview()
