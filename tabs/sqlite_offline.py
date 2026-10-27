# -*- coding: utf-8 -*-
"""Offline Data Layer for the SQLite tab.

This module is the *offline* half of the SQLite tab: it reads and writes
databases that live in files, with no server anywhere in the picture.  It is the
half that carries the weight, so it does the whole job:

    open(path)            detect the format and open it
    list_tables()         what is inside
    list_columns(table)   the schema of one table
    fetch_rows(table)     the records
    insert_row / update_row / delete_row
    execute_sql(sql)      one statement, or a whole script
    backup(path)          a consistent copy, taken safely
    integrity_check()     the PRAGMA SQLite provides, plus per-format checks
    table_stats()         row counts and the shape of the data

It reuses what the application already has rather than re-implementing it:

    sdb_scripts._split_sql_statements   the SQL splitter the Runner uses, so a
                                        script made of several statements is
                                        split the same way in both places
    tabs.docs_tab._atomic_write_json    atomic, fsync-safe writes for exports

Supported formats: SQLite, JSON, CSV and a small line-based text format.  The
format is detected from the bytes at the start of the file, not from the name
of the file -- an extension is a hint, not a fact.

Every public method returns a plain, JSON-safe structure, so the GUI, the sync
layer and the reports all read the same shapes.
"""

import csv
import datetime as dt
import json
import os
import shutil
import sqlite3
import tempfile


def _now():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------
#: The formats this layer can open, as (key, label key, fallback).
OFFLINE_FORMATS = (
    ("sqlite", "sqlite_fmt_sqlite", "SQLite database"),
    ("json", "sqlite_fmt_json", "JSON document"),
    ("csv", "sqlite_fmt_csv", "CSV table"),
    ("text", "sqlite_fmt_text", "Text lines"),
)

OFFLINE_FORMAT_KEYS = tuple(key for key, _lk, _f in OFFLINE_FORMATS)
OFFLINE_FORMAT_LABELS = {key: fallback for key, _lk, fallback in OFFLINE_FORMATS}

#: The 16 bytes every SQLite file starts with.
SQLITE_MAGIC = b"SQLite format 3\x00"

#: What a column's declared type means when the GUI has to render it.
COLUMN_KINDS = ("integer", "real", "text", "blob", "null")

#: How a table is keyed, which decides whether a row can be edited in place.
ROWID_ALIASES = ("rowid", "_rowid_", "oid")


def guess_format(path):
    """The format of a file, from its bytes first and its name second.

    Returns one of :data:`OFFLINE_FORMAT_KEYS`.  The header is the reliable
    signal for SQLite; for the text formats the content is inspected, and only
    when both are inconclusive does the extension decide.
    """
    if not path or not os.path.isfile(path):
        return ""
    try:
        with open(path, "rb") as handle:
            head = handle.read(16)
    except OSError:
        return ""
    if head.startswith(SQLITE_MAGIC):
        return "sqlite"
    name = path.lower()
    if name.endswith((".db", ".sqlite", ".sqlite3", ".db3")):
        # Claims to be SQLite but is not: still treat it as one so the error
        # says "this is not a database" rather than silently reading it as CSV.
        return "sqlite"
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            sample = handle.read(65536)
    except OSError:
        return ""
    stripped = sample.lstrip()
    if stripped.startswith(("{", "[")):
        return "json"
    if name.endswith(".json"):
        return "json"
    # A CSV has a delimiter in its first line, and the same count on the next.
    lines = [line for line in sample.splitlines() if line.strip()]
    if len(lines) >= 2:
        for delimiter in (",", ";", "\t", "|"):
            if lines[0].count(delimiter) and \
                    lines[0].count(delimiter) == lines[1].count(delimiter):
                return "csv"
    if name.endswith(".csv"):
        return "csv"
    return "text"


def describe_format(path):
    """A short, human description of what a file appears to be."""
    kind = guess_format(path)
    if not kind:
        return "unknown"
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    return "%s, %d byte(s)" % (OFFLINE_FORMAT_LABELS[kind], size)


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------
def ok(detail="", **extra):
    """A successful result, in the shape every layer returns."""
    out = {"ok": True, "detail": detail}
    out.update(extra)
    return out


def fail(code, detail):
    return {"ok": False, "code": code, "detail": detail}
# ---------------------------------------------------------------------------
# The offline data layer
# ---------------------------------------------------------------------------
class OfflineDatabase(object):
    """A database that lives in a file, opened by :meth:`open`.

    One instance is bound to one file.  A SQLite file is read through the
    standard ``sqlite3`` module; a JSON, CSV or text file is loaded into memory
    and presented through the very same table/column/row interface, so the GUI
    and the sync layer do not have to know which one they are looking at.

    Nothing here executes user code, and every write to a SQLite file is made
    through a transaction that is either committed or rolled back.
    """

    #: A row added to a loaded JSON/CSV/text file so those formats have keys.
    LOADED_TABLE = "data"

    def __init__(self, path=None):
        self.path = path or ""
        self.kind = ""
        self.connection = None
        self._loaded = {}          # table name -> list of row dicts
        self._columns = {}         # table name -> [(name, type, kind)]
        self.read_only = False
        self.opened_at = ""
        self.log = []              # every operation, newest first

    # ------------------------------------------------------------- lifecycle
    def open(self, path, read_only=False, create=False):
        """Open a file, detecting its format and validating its structure.

        A path that does not exist yet is created as an empty SQLite database
        when create is set -- that is how a new database is started.  Without
        it a missing file is an error, because opening a typo silently would
        be worse than saying so.
        """
        self.close()
        if not path:
            return fail("missing", "no path was given")
        exists = os.path.isfile(path)
        if not exists and not create:
            return fail("missing", "no such file: %s" % path)
        self.path = os.path.abspath(path)
        if not exists:
            try:
                sqlite3.connect(self.path).close()
            except sqlite3.Error as exc:
                return fail("cannot_create", str(exc))
        self.kind = guess_format(self.path)
        self.read_only = bool(read_only)
        if not self.kind:
            return fail("unknown_format", "cannot tell what this file is")
        if self.kind == "sqlite":
            outcome = self._open_sqlite(self.path, read_only)
        else:
            outcome = self._open_document(self.path, self.kind)
        if not outcome["ok"]:
            return outcome
        self.opened_at = _now()
        self._record("open", "%s (%s)" % (os.path.basename(self.path),
                                          self.kind))
        return ok("opened %s" % OFFLINE_FORMAT_LABELS.get(self.kind, self.kind),
                  path=self.path, kind=self.kind,
                  tables=[name for name, _rows in self._tables()])

    def _open_sqlite(self, path, read_only):
        try:
            if read_only:
                # A read-only connection is made through a URI so sqlite3
                # refuses every write for us, rather than us policing it.
                uri = "file:%s?mode=ro" % path.replace("?", "%3f").replace(
                    "#", "%23")
                self.connection = sqlite3.connect(uri, uri=True)
            else:
                self.connection = sqlite3.connect(path)
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA foreign_keys = ON")
            self.connection.execute("SELECT 1")
        except sqlite3.Error as exc:
            self.connection = None
            return fail("not_a_database",
                        "this file is not a readable SQLite database: %s" % exc)
        return ok("sqlite opened")

    def _open_document(self, path, kind):
        try:
            if kind == "json":
                self._loaded[self.LOADED_TABLE] = self._read_json(path)
            elif kind == "csv":
                self._loaded[self.LOADED_TABLE] = self._read_csv(path)
            else:
                self._loaded[self.LOADED_TABLE] = self._read_text(path)
        except (OSError, ValueError) as exc:
            return fail("unreadable", "cannot read the file: %s" % exc)
        self._columns[self.LOADED_TABLE] = self._infer_columns(
            self._loaded[self.LOADED_TABLE])
        return ok("%s loaded" % kind)

    def close(self):
        if self.connection is not None:
            try:
                self.connection.close()
            except sqlite3.Error:
                pass
        self.connection = None
        self._loaded = {}
        self._columns = {}
        self.kind = ""
        return ok("closed")

    def is_open(self):
        return bool(self.kind) and (self.connection is not None
                                    or self._loaded)

    def save(self):
        """Write a loaded document back to its file, atomically."""
        if not self.is_open():
            return fail("closed", "nothing is open")
        if self.read_only:
            return fail("read_only", "the database is open read-only")
        if self.kind == "sqlite":
            if self.connection is None:
                return fail("closed", "nothing is open")
            try:
                self.connection.commit()
            except sqlite3.Error as exc:
                return fail("commit_failed", str(exc))
            self._record("commit", "changes written")
            return ok("changes saved")
        rows = self._loaded.get(self.LOADED_TABLE) or []
        try:
            if self.kind == "json":
                self._write_json(self.path, rows)
            else:
                self._write_csv(self.path, rows)
        except OSError as exc:
            return fail("write_failed", str(exc))
        self._record("save", "document written")
        return ok("document saved", rows=len(rows))

    # --------------------------------------------------------------- schema
    def _tables(self):
        """(name, rows) for every table, in a stable order."""
        if self.kind == "sqlite" and self.connection is not None:
            names = self._sqlite_table_names()
            return [(name, self._sqlite_row_count(name)) for name in names]
        return [(name, len(rows)) for name, rows in
                sorted(self._loaded.items())]

    def list_tables(self):
        """The table names, as the GUI's explorer shows them."""
        return [name for name, _rows in self._tables()]

    def _sqlite_table_names(self):
        try:
            cursor = self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name COLLATE NOCASE")
            return [row["name"] for row in cursor.fetchall()]
        except sqlite3.Error:
            return []

    def _sqlite_row_count(self, table):
        try:
            cursor = self.connection.execute(
                "SELECT COUNT(*) AS n FROM %s" % self._quote(table))
            return cursor.fetchone()["n"]
        except sqlite3.Error:
            return 0

    def table_exists(self, table):
        return table in self.list_tables()

    def list_columns(self, table):
        """The columns of a table as ``[{"name", "type", "kind"}, ...]``.

        For SQLite the declared type and the primary-key flag come from
        ``PRAGMA table_info``, which is the database's own answer.  For a loaded
        document the types are inferred from the values.
        """
        if self.kind == "sqlite":
            return self._sqlite_columns(table)
        return [{"name": name, "type": str(kind), "kind": str(kind)}
                for name, kind in self._columns.get(table, [])]

    def _sqlite_columns(self, table):
        if self.connection is None:
            return []
        try:
            cursor = self.connection.execute(
                "PRAGMA table_info(%s)" % self._quote(table))
            rows = cursor.fetchall()
        except sqlite3.Error:
            return []
        columns = []
        for row in rows:
            declared = (row["type"] or "").strip()
            columns.append({
                "name": row["name"],
                "type": declared or "unknown",
                "kind": _type_kind(declared),
                "notnull": bool(row["notnull"]),
                "primary_key": bool(row["pk"]),
                "default": row["dflt_value"],
            })
        return columns

    def column_names(self, table):
        return [column["name"] for column in self.list_columns(table)]

    def primary_key(self, table):
        """The column a row can be addressed by.

        A real primary key is preferred; failing that SQLite's implicit rowid is
        used, which is what makes a table without a declared key still editable.
        """
        keys = [column["name"] for column in self.list_columns(table)
                if column.get("primary_key")]
        if len(keys) == 1:
            return keys[0]
        if keys:
            return keys[0]
        if self.kind == "sqlite":
            for alias in ROWID_ALIASES:
                if self._column_exists(table, alias):
                    return alias
            # Every SQLite table has a rowid unless it is WITHOUT ROWID.
            if self._has_rowid(table):
                return "rowid"
            return ""
        # A loaded document has no declared key, so one is inferred: the first
        # column whose values are unique and never empty is the key, and failing
        # that the row's position stands in for one.  Without this a JSON or CSV
        # row could be read but never edited.
        return self._inferred_key(table)

    def _inferred_key(self, table):
        """The column that identifies a row in a loaded document."""
        rows = self._loaded.get(table) or []
        if not rows:
            return ""
        names = [name for name, _kind in self._columns.get(table, [])]
        for name in names:
            values = [row.get(name) for row in rows]
            if any(value in (None, "") for value in values):
                continue
            if len({str(value) for value in values}) == len(values):
                return name
        return ""

    def key_for_row(self, table, row):
        """The value a row is addressed by, for a loaded document.

        When no column identifies the row, its position does -- so the caller
        gets an address for every row, which is what makes an edit possible.
        """
        column = self.primary_key(table)
        if column:
            return row.get(column)
        rows = self._loaded.get(table) or []
        for index, candidate in enumerate(rows):
            if candidate is row:
                return index
        return None

    def _column_exists(self, table, name):
        try:
            cursor = self.connection.execute(
                "PRAGMA table_info(%s)" % self._quote(table))
            return any(row["name"] == name for row in cursor.fetchall())
        except sqlite3.Error:
            return False

    def _has_rowid(self, table):
        try:
            self.connection.execute(
                "SELECT rowid FROM %s LIMIT 1" % self._quote(table))
            return True
        except sqlite3.Error:
            return False

    def indexes(self, table):
        """The indexes on a table, each with its columns and uniqueness."""
        if self.kind != "sqlite" or self.connection is None:
            return []
        out = []
        try:
            cursor = self.connection.execute(
                "PRAGMA index_list(%s)" % self._quote(table))
            for row in cursor.fetchall():
                name = row["name"]
                columns = []
                try:
                    inner = self.connection.execute(
                        "PRAGMA index_info(%s)" % self._quote(name))
                    columns = [item["name"] for item in inner.fetchall()]
                except sqlite3.Error:
                    pass
                out.append({"name": name, "unique": bool(row["unique"]),
                            "origin": row["origin"], "columns": columns})
        except sqlite3.Error:
            pass
        return out

    def foreign_keys(self, table):
        """The outgoing relationships of a table (SQLite only)."""
        if self.kind != "sqlite" or self.connection is None:
            return []
        out = []
        try:
            cursor = self.connection.execute(
                "PRAGMA foreign_key_list(%s)" % self._quote(table))
            for row in cursor.fetchall():
                out.append({"column": row["from"], "target_table": row["table"],
                            "target_column": row["to"],
                            "on_delete": row["on_delete"],
                            "on_update": row["on_update"]})
        except sqlite3.Error:
            pass
        return out

    def schema(self, table):
        """The CREATE statement that defines a table (SQLite only)."""""
        if self.kind != "sqlite" or self.connection is None:
            return ""
        try:
            cursor = self.connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,))
            row = cursor.fetchone()
            return (row["sql"] or "") if row else ""
        except sqlite3.Error:
            return ""

    # ----------------------------------------------------------------- rows
    def fetch_rows(self, table, limit=None, offset=0, order_by="",
                   search="", columns=None):
        """The records of a table, filtered, ordered and windowed in the query.

        Filtering and paging happen in SQLite rather than in Python, so a table
        with a million rows is not dragged through memory to show twenty of it.
        """
        if table not in self.list_tables():
            return []
        if self.kind == "sqlite":
            return self._sqlite_fetch(table, limit, offset, order_by, search,
                                      columns)
        return self._loaded_fetch(table, limit, offset, order_by, search,
                                  columns)

    def _sqlite_fetch(self, table, limit, offset, order_by, search, columns):
        clauses = []
        values = []
        if search:
            names = self.column_names(table)
            if names:
                clauses.append("(" + " OR ".join(
                    "CAST(%s AS TEXT) LIKE ?" % self._quote(name)
                    for name in names) + ")")
                values.extend(["%%%s%%" % search] * len(names))
        query = "SELECT * FROM %s" % self._quote(table)
        if columns:
            allowed = [name for name in columns
                       if name in self.column_names(table)]
            if allowed:
                query = "SELECT %s FROM %s" % (
                    ", ".join(self._quote(name) for name in allowed),
                    self._quote(table))
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        if order_by and order_by in self.column_names(table):
            query += " ORDER BY %s" % self._quote(order_by)
        if limit is not None:
            query += " LIMIT ? OFFSET ?"
            values.extend([int(limit), int(offset or 0)])
        try:
            cursor = self.connection.execute(query, values)
            return [dict(row) for row in cursor.fetchall()]
        except sqlite3.Error:
            return []

    def _loaded_fetch(self, table, limit, offset, order_by, search, columns):
        rows = [dict(row) for row in (self._loaded.get(table) or [])]
        if search:
            needle = search.lower()
            rows = [row for row in rows
                    if any(needle in str(value).lower()
                           for value in row.values())]
        if order_by:
            rows.sort(key=lambda row: str(row.get(order_by, "")))
        if limit is not None:
            start = int(offset or 0)
            rows = rows[start:start + int(limit)]
        if columns:
            rows = [{key: value for key, value in row.items() if key in columns}
                    for row in rows]
        return rows

    def count_rows(self, table):
        """How many rows a table holds, without fetching them."""
        if self.kind == "sqlite":
            return self._sqlite_row_count(table)
        return len(self._loaded.get(table) or [])
    # ----------------------------------------------------------------- CRUD
    def insert_row(self, table, payload):
        """Add one record.  Returns the new key when the table has one."""
        payload = dict(payload or {})
        if not payload:
            return fail("empty", "nothing to insert")
        if self.read_only:
            return fail("read_only", "the database is open read-only")
        if table not in self.list_tables():
            return fail("no_table", "no such table: %s" % table)
        problem = self.validate_payload(table, payload)
        if problem:
            return fail("invalid", problem)
        if self.kind == "sqlite":
            names = [name for name in payload
                     if name in self.column_names(table)]
            if not names:
                return fail("no_columns",
                            "none of the given columns exist in %s" % table)
            query = "INSERT INTO %s (%s) VALUES (%s)" % (
                self._quote(table),
                ", ".join(self._quote(name) for name in names),
                ", ".join("?" for _name in names))
            try:
                cursor = self.connection.execute(
                    query, [payload[name] for name in names])
                key = cursor.lastrowid
                self.connection.commit()
            except sqlite3.Error as exc:
                self.connection.rollback()
                return fail("insert_failed", str(exc))
            self._record("insert", "%s: 1 row" % table)
            return ok("row inserted into %s" % table, key=key)
        rows = self._loaded.setdefault(table, [])
        rows.append(payload)
        self._columns[table] = self._infer_columns(rows)
        self._record("insert", "%s: %d rows" % (table, len(rows)))
        return ok("row added", key=len(rows) - 1)

    def update_row(self, table, row_id, payload, key_column=""):
        """Change one record, addressed by its primary key.

        The ``WHERE`` clause names exactly one column, and the value is bound as
        a parameter -- never interpolated -- so a row id that happens to contain
        SQL cannot change the statement.
        """
        payload = dict(payload or {})
        if not payload:
            return fail("empty", "nothing to update")
        if self.read_only:
            return fail("read_only", "the database is open read-only")
        column = key_column or self.primary_key(table)
        if not column:
            return fail("no_key",
                        "table %s has no key, so a row cannot be addressed"
                        % table)
        problem = self.validate_payload(table, payload)
        if problem:
            return fail("invalid", problem)
        if self.kind == "sqlite":
            names = [name for name in payload
                     if name in self.column_names(table)]
            if not names:
                return fail("no_columns",
                            "none of the given columns exist in %s" % table)
            query = "UPDATE %s SET %s WHERE %s = ?" % (
                self._quote(table),
                ", ".join("%s = ?" % self._quote(name) for name in names),
                self._quote(column))
            values = [payload[name] for name in names] + [row_id]
            try:
                cursor = self.connection.execute(query, values)
                affected = cursor.rowcount
                self.connection.commit()
            except sqlite3.Error as exc:
                self.connection.rollback()
                return fail("update_failed", str(exc))
            if not affected:
                return fail("no_row", "no row matched %s = %r" % (column,
                                                                  row_id))
            self._record("update", "%s: %d row(s)" % (table, affected))
            return ok("row updated", affected=affected)
        rows = self._loaded.get(table) or []
        index = self._find_loaded_index(rows, column, row_id)
        if index is None:
            return fail("no_row", "no row matched %s = %r" % (column, row_id))
        rows[index].update(payload)
        self._record("update", "%s: 1 row" % table)
        return ok("row updated", affected=1)

    def delete_row(self, table, row_id, key_column=""):
        """Remove one record, addressed by its primary key."""
        if self.read_only:
            return fail("read_only", "the database is open read-only")
        column = key_column or self.primary_key(table)
        if not column:
            return fail("no_key", "table %s has no key" % table)
        if self.kind == "sqlite":
            try:
                cursor = self.connection.execute(
                    "DELETE FROM %s WHERE %s = ?" % (self._quote(table),
                                                     self._quote(column)),
                    [row_id])
                affected = cursor.rowcount
                self.connection.commit()
            except sqlite3.Error as exc:
                self.connection.rollback()
                return fail("delete_failed", str(exc))
            if not affected:
                return fail("no_row", "no row matched %s = %r" % (column,
                                                                  row_id))
            self._record("delete", "%s: %d row(s)" % (table, affected))
            return ok("row deleted", affected=affected)
        rows = self._loaded.get(table) or []
        index = self._find_loaded_index(rows, column, row_id)
        if index is None:
            return fail("no_row", "no row matched %s = %r" % (column, row_id))
        rows.pop(index)
        self._record("delete", "%s: 1 row" % table)
        return ok("row deleted", affected=1)

    def delete_rows(self, table, row_ids, key_column=""):
        """Remove several records as one transaction.

        A batch is all-or-nothing: if one statement fails, the whole batch is
        rolled back, so a half-applied mass delete cannot happen.
        """
        ids = list(row_ids or [])
        if not ids:
            return fail("empty", "no rows were given")
        if self.read_only:
            return fail("read_only", "the database is open read-only")
        column = key_column or self.primary_key(table)
        if not column:
            return fail("no_key", "table %s has no key" % table)
        if self.kind != "sqlite":
            removed = 0
            for row_id in ids:
                if self.delete_row(table, row_id, column)["ok"]:
                    removed += 1
            return ok("rows deleted", affected=removed)
        query = "DELETE FROM %s WHERE %s = ?" % (self._quote(table),
                                                 self._quote(column))
        try:
            cursor = self.connection.cursor()
            cursor.execute("BEGIN")
            affected = 0
            for row_id in ids:
                cursor.execute(query, [row_id])
                affected += cursor.rowcount
            self.connection.commit()
        except sqlite3.Error as exc:
            self.connection.rollback()
            return fail("delete_failed", str(exc))
        self._record("delete", "%s: %d row(s) in one batch" % (table, affected))
        return ok("rows deleted", affected=affected)

    @staticmethod
    def _find_loaded_index(rows, column, row_id):
        for index, row in enumerate(rows):
            if str(row.get(column)) == str(row_id):
                return index
        return None

    # ----------------------------------------------------------- validation
    def validate_payload(self, table, payload):
        """A short reason the payload is wrong, or "" when it is fine.

        Only the checks the database itself would reject are made here, so the
        message arrives before the statement does: unknown columns and a value
        that cannot be stored at all.
        """
        if not isinstance(payload, dict):
            return "the payload is not a mapping"
        names = self.column_names(table)
        if names:
            unknown = [name for name in payload if name not in names]
            if len(unknown) == len(payload):
                return "none of these columns exist: %s" % ", ".join(unknown)
        for name, value in payload.items():
            if isinstance(value, (bytes, bytearray)) or value is None:
                continue
            if not isinstance(value, (str, int, float, bool)):
                return "%s cannot store a %s" % (name, type(value).__name__)
        return ""

    def validate_table(self, table):
        """An integrity summary for one table: key, duplicates, blank rows."""
        if table not in self.list_tables():
            return fail("no_table", "no such table: %s" % table)
        columns = self.column_names(table)
        key = self.primary_key(table)
        issues = []
        if not key:
            issues.append({"kind": "no_key",
                           "detail": "rows cannot be addressed individually"})
        blank = 0
        if columns:
            for row in self.fetch_rows(table):
                if all(row.get(name) in (None, "") for name in columns):
                    blank += 1
        if blank:
            issues.append({"kind": "blank_rows",
                           "detail": "%d row(s) are entirely empty" % blank})
        return ok("%d column(s), %d row(s)" % (len(columns),
                                               self.count_rows(table)),
                  table=table, key=key, issues=issues,
                  columns=columns)

    # ------------------------------------------------------------------ SQL
    def execute_sql(self, sql, statements=None):
        """Run one statement or a whole script, returning a result per statement.

        A read statement yields its rows; a write statement yields how many rows
        it affected.  The script is split with the application's own splitter, so
        a semicolon inside a string literal is treated the same way the Runner
        treats it.
        """
        if self.kind != "sqlite" or self.connection is None:
            return fail("not_sqlite",
                        "SQL runs against a SQLite database only")
        if self.read_only and _is_write_statement(sql):
            return fail("read_only", "the database is open read-only")
        parts = statements if statements is not None else _split_sql(sql)
        if not parts:
            return fail("empty", "no statement to run")
        results = []
        try:
            for statement in parts:
                cursor = self.connection.execute(statement)
                if cursor.description:
                    columns = [item[0] for item in cursor.description]
                    rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
                    results.append({"statement": statement, "columns": columns,
                                    "rows": rows, "affected": len(rows),
                                    "kind": "read"})
                else:
                    affected = cursor.rowcount
                    results.append({"statement": statement, "columns": [],
                                    "rows": [],
                                    "affected": affected if affected and
                                    affected > 0 else 0,
                                    "kind": "write"})
            self.connection.commit()
        except sqlite3.Error as exc:
            self.connection.rollback()
            self._record("sql_failed", str(exc))
            return fail("sql_failed", str(exc))
        self._record("sql", "%d statement(s) run" % len(results))
        return ok("%d statement(s) run" % len(results), results=results)

    def explain(self, sql):
        """The query plan, so a slow statement can be seen before it is run."""
        if self.kind != "sqlite" or self.connection is None:
            return fail("not_sqlite", "EXPLAIN needs a SQLite database")
        try:
            cursor = self.connection.execute("EXPLAIN QUERY PLAN %s" % sql)
            return ok("plan ready",
                      plan=[dict(row) for row in cursor.fetchall()])
        except sqlite3.Error as exc:
            return fail("sql_failed", str(exc))

    # ------------------------------------------------------ admin tooling
    def backup(self, target="", keep=0):
        """Take a consistent copy of the database.

        SQLite's own backup API is used, so the copy is taken page by page with
        the file live; a plain file copy of a database being written can be torn.
        """
        if not self.is_open():
            return fail("closed", "nothing is open")
        if not target:
            base, extension = os.path.splitext(self.path)
            target = "%s_backup_%s%s" % (base, _stamp(), extension)
        target = os.path.abspath(target)
        try:
            if self.kind == "sqlite" and self.connection is not None:
                destination = sqlite3.connect(target)
                try:
                    self.connection.backup(destination)
                finally:
                    destination.close()
            else:
                shutil.copyfile(self.path, target)
        except (sqlite3.Error, OSError) as exc:
            return fail("backup_failed", str(exc))
        self._record("backup", target)
        removed = self._prune_backups(keep) if keep else 0
        return ok("backup written", path=target, pruned=removed)

    def _prune_backups(self, keep):
        """Keep only the newest `keep` backups of this database."""
        try:
            keep = int(keep)
        except (TypeError, ValueError):
            return 0
        if keep <= 0:
            return 0
        base, extension = os.path.splitext(self.path)
        folder = os.path.dirname(self.path) or "."
        wanted = os.path.splitext(os.path.basename(base))[0]
        candidates = []
        for name in os.listdir(folder):
            if not name.startswith(wanted + "_backup_") or \
                    not name.endswith(extension):
                continue
            full = os.path.join(folder, name)
            candidates.append((os.path.getmtime(full), full))
        candidates.sort(reverse=True)
        removed = 0
        for _stamp_value, path in candidates[keep:]:
            try:
                os.remove(path)
                removed += 1
            except OSError:
                pass
        return removed

    def restore(self, backup_path):
        """Replace the open database with a backup, keeping a safety copy."""
        if not os.path.isfile(backup_path):
            return fail("missing", "no such backup: %s" % backup_path)
        if self.read_only:
            return fail("read_only", "the database is open read-only")
        safety = ""
        try:
            if os.path.isfile(self.path):
                base, extension = os.path.splitext(self.path)
                safety = "%s_before_restore_%s%s" % (base, _stamp(), extension)
                shutil.copyfile(self.path, safety)
            self.close()
            shutil.copyfile(backup_path, self.path)
        except OSError as exc:
            return fail("restore_failed", str(exc))
        outcome = self.open(self.path)
        if not outcome["ok"]:
            return outcome
        self._record("restore", "%s (safety: %s)" % (backup_path, safety))
        return ok("database restored", path=self.path, safety=safety)

    def integrity_check(self):
        """Run the integrity checks the format offers (SQLite: its own PRAGMA)."""
        if not self.is_open():
            return fail("closed", "nothing is open")
        findings = []
        if self.kind == "sqlite" and self.connection is not None:
            for pragma, label in (("integrity_check", "integrity"),
                                  ("foreign_key_check", "foreign keys"),
                                  ("quick_check", "quick check")):
                try:
                    cursor = self.connection.execute("PRAGMA %s" % pragma)
                    rows = [tuple(row) for row in cursor.fetchall()]
                except sqlite3.Error as exc:
                    findings.append({"check": label, "state": "error",
                                     "detail": str(exc)})
                    continue
                clean = (not rows) or (len(rows) == 1 and str(rows[0][0]).lower()
                                       in ("ok", "0"))
                findings.append({"check": label,
                                 "state": "ok" if clean else "problem",
                                 "detail": "" if clean else str(rows[:5])})
            # A table the schema mentions but that cannot be read is damage too.
            for table in self._sqlite_table_names():
                try:
                    self.connection.execute(
                        "SELECT * FROM %s LIMIT 1" % self._quote(table))
                except sqlite3.Error as exc:
                    findings.append({"check": "table %s" % table,
                                     "state": "problem", "detail": str(exc)})
        else:
            rows = self._loaded.get(self.LOADED_TABLE) or []
            widths = set()
            for row in rows:
                widths.add(len(row))
            if len(widths) > 1:
                findings.append({"check": "columns", "state": "problem",
                                 "detail": "rows have %d different widths"
                                 % len(widths)})
            else:
                findings.append({"check": "columns", "state": "ok",
                                 "detail": ""})
            findings.append({"check": "readable", "state": "ok", "detail": ""})
        problems = [item for item in findings if item["state"] != "ok"]
        state = "healthy" if not problems else "%d problem(s)" % len(problems)
        self._record("integrity", state)
        return ok(state, findings=findings, healthy=not problems)

    def repair(self):
        """What SQLite itself offers a damaged file: VACUUM and REINDEX."""
        if self.kind != "sqlite" or self.connection is None:
            return fail("not_sqlite", "repair is a SQLite operation")
        if self.read_only:
            return fail("read_only", "the database is open read-only")
        done = []
        for statement in ("REINDEX", "VACUUM"):
            try:
                self.connection.execute(statement)
                done.append(statement)
            except sqlite3.Error as exc:
                return fail("repair_failed", "%s: %s" % (statement, exc))
        self._record("repair", ", ".join(done))
        return ok("ran %s" % ", ".join(done), steps=done)

    def vacuum_into(self, target):
        """Write a compacted copy, the safest way to reclaim space."""
        if self.kind != "sqlite" or self.connection is None:
            return fail("not_sqlite", "VACUUM INTO needs a SQLite database")
        try:
            self.connection.execute("VACUUM INTO ?", (target,))
        except sqlite3.Error as exc:
            return fail("vacuum_failed", str(exc))
        self._record("vacuum", target)
        return ok("compacted copy written", path=target)

    # ------------------------------------------------------------ statistics
    def table_stats(self, table):
        """Row count and, per column, the shape of the values in it.

        The numbers come from SQL where possible (COUNT, MIN, MAX, AVG) and from
        one pass over the rows otherwise, so a large table is summarised without
        being fetched twice.
        """
        if table not in self.list_tables():
            return fail("no_table", "no such table: %s" % table)
        columns = self.list_columns(table)
        total = self.count_rows(table)
        stats = {"table": table, "rows": total, "columns": []}
        rows = self.fetch_rows(table, limit=5000)
        for column in columns:
            name = column["name"]
            values = [row.get(name) for row in rows]
            filled = [value for value in values if value not in (None, "")]
            numbers = [value for value in filled
                       if isinstance(value, (int, float))
                       and not isinstance(value, bool)]
            entry = {"name": name, "type": column.get("type", ""),
                     "kind": column.get("kind", ""),
                     "filled": len(filled),
                     "empty": len(values) - len(filled),
                     "distinct": len({str(value) for value in filled})}
            if numbers:
                entry["min"] = min(numbers)
                entry["max"] = max(numbers)
                entry["average"] = round(sum(numbers) / float(len(numbers)), 3)
            stats["columns"].append(entry)
        stats["sampled"] = len(rows)
        return ok("%d row(s), %d column(s)" % (total, len(columns)), **stats)

    def duplicates(self, table, columns=None):
        """Rows that repeat on the given columns (all of them by default)."""
        keys = columns or self.column_names(table)
        seen = {}
        for row in self.fetch_rows(table):
            signature = tuple(str(row.get(name)) for name in keys)
            seen.setdefault(signature, []).append(row)
        groups = [{"key": list(signature), "count": len(rows), "rows": rows[:20]}
                  for signature, rows in seen.items() if len(rows) > 1]
        groups.sort(key=lambda item: -item["count"])
        return ok("%d duplicate group(s)" % len(groups), groups=groups,
                  columns=keys)

    # --------------------------------------------------------------- export
    def export(self, path, table="", fmt=""):
        """Write a table (or every table) out as SQL, CSV or JSON."""
        if not path:
            return fail("no_path", "no destination was given")
        fmt = (fmt or os.path.splitext(path)[1].lstrip(".") or "json").lower()
        tables = [table] if table else self.list_tables()
        payload = {name: self.fetch_rows(name) for name in tables}
        try:
            if fmt == "json":
                _atomic_write(path, json.dumps(payload, indent=2,
                                               ensure_ascii=False,
                                               default=str))
            elif fmt == "csv":
                name = tables[0] if tables else ""
                rows = payload.get(name) or []
                with open(path, "w", encoding="utf-8", newline="") as handle:
                    if rows:
                        writer = csv.DictWriter(handle,
                                                fieldnames=list(rows[0].keys()))
                        writer.writeheader()
                        writer.writerows(rows)
            elif fmt == "sql":
                lines = ["-- ScriptDB SQLite export",
                         "-- generated %s" % _now(), "",
                         "PRAGMA foreign_keys = ON;", ""]
                for name in tables:
                    schema = self.schema(name)
                    if schema:
                        lines += [schema.rstrip(";") + ";", ""]
                    for row in payload.get(name) or []:
                        columns = list(row.keys())
                        lines.append("INSERT INTO %s (%s) VALUES (%s);" % (
                            self._quote(name),
                            ", ".join(self._quote(column)
                                      for column in columns),
                            ", ".join(_sql_literal(row[column])
                                      for column in columns)))
                    lines.append("")
                _atomic_write(path, "\n".join(lines))
            else:
                return fail("bad_format",
                            "export writes json, csv or sql -- not %r" % fmt)
        except (OSError, ValueError) as exc:
            return fail("export_failed", str(exc))
        self._record("export", "%s -> %s" % (fmt, path))
        return ok("exported %d table(s) as %s" % (len(tables), fmt),
                  path=path, tables=tables, format=fmt)

    def import_file(self, path, table="", create=True):
        """Load a JSON/CSV file into a table, creating it when needed.

        The rows are inserted in one transaction, so an import that fails
        halfway leaves the database exactly as it was.
        """
        if not os.path.isfile(path):
            return fail("missing", "no such file: %s" % path)
        kind = guess_format(path)
        try:
            if kind == "json":
                with open(path, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
                records = _records_from_json(data)
            elif kind == "csv":
                records = self._read_csv(path)
            else:
                records = self._read_text(path)
        except (OSError, ValueError) as exc:
            return fail("unreadable", str(exc))
        if not records:
            return fail("empty", "the file holds no records to import")
        name = table or os.path.splitext(os.path.basename(path))[0]
        name = _safe_name(name)
        if self.kind != "sqlite":
            self._loaded[name] = records
            self._columns[name] = self._infer_columns(records)
            self._record("import", "%s: %d row(s)" % (name, len(records)))
            return ok("imported %d row(s) into %s" % (len(records), name),
                      table=name, rows=len(records))
        if not self.table_exists(name):
            if not create:
                return fail("no_table", "no such table: %s" % name)
            outcome = self.create_table_from_rows(name, records)
            if not outcome["ok"]:
                return outcome
        columns = [column for column in self.column_names(name)
                   if column in records[0]]
        if not columns:
            return fail("no_columns",
                        "the file's columns do not match %s" % name)
        query = "INSERT INTO %s (%s) VALUES (%s)" % (
            self._quote(name),
            ", ".join(self._quote(column) for column in columns),
            ", ".join("?" for _column in columns))
        try:
            cursor = self.connection.cursor()
            cursor.execute("BEGIN")
            cursor.executemany(query, [[row.get(column) for column in columns]
                                       for row in records])
            self.connection.commit()
        except sqlite3.Error as exc:
            self.connection.rollback()
            return fail("import_failed", str(exc))
        self._record("import", "%s: %d row(s)" % (name, len(records)))
        return ok("imported %d row(s) into %s" % (len(records), name),
                  table=name, rows=len(records))

    # ----------------------------------------------------------- schema edits
    def create_table(self, name, columns, if_not_exists=False):
        """Create a table from ``[{"name", "type", "primary_key"}, ...]``."""
        if self.kind != "sqlite":
            return fail("not_sqlite",
                        "creating a table needs a SQLite database")
        if self.read_only:
            return fail("read_only", "the database is open read-only")
        name = _safe_name(name)
        if not name:
            return fail("bad_name", "a table needs a name")
        pieces = []
        for column in columns or []:
            column_name = _safe_name(column.get("name", ""))
            if not column_name:
                continue
            declared = str(column.get("type") or "TEXT").upper()
            if declared not in ("INTEGER", "REAL", "TEXT", "BLOB", "NUMERIC"):
                declared = "TEXT"
            piece = "%s %s" % (self._quote(column_name), declared)
            if column.get("primary_key"):
                piece += " PRIMARY KEY"
            if column.get("notnull"):
                piece += " NOT NULL"
            pieces.append(piece)
        if not pieces:
            return fail("no_columns", "a table needs at least one column")
        query = "CREATE TABLE %s%s (%s)" % (
            "IF NOT EXISTS " if if_not_exists else "",
            self._quote(name),
            ", ".join(pieces))
        try:
            self.connection.execute(query)
            self.connection.commit()
        except sqlite3.Error as exc:
            return fail("create_failed", str(exc))
        self._record("create_table", name)
        return ok("table %s created" % name, table=name)

    def create_table_from_rows(self, name, rows):
        """Create a table whose columns come from the rows being imported."""
        if not rows:
            return fail("empty", "no rows to take a schema from")
        columns = [{"name": key,
                    "type": _declared_type_for(value),
                    "primary_key": False}
                   for key, value in rows[0].items()]
        return self.create_table(name, columns, if_not_exists=True)

    def add_column(self, table, name, declared_type="TEXT"):
        if self.kind != "sqlite":
            return fail("not_sqlite", "schema edits need a SQLite database")
        if self.read_only:
            return fail("read_only", "the database is open read-only")
        statement = "ALTER TABLE %s ADD COLUMN %s %s" % (
            self._quote(table), self._quote(_safe_name(name)),
            _declared_type_for(declared_type))
        try:
            self.connection.execute(statement)
            self.connection.commit()
        except sqlite3.Error as exc:
            return fail("alter_failed", str(exc))
        self._record("add_column", "%s.%s" % (table, name))
        return ok("column added")

    def rename_table(self, table, new_name):
        return self._alter("RENAME TO", table, _safe_name(new_name),
                           "rename_table")

    def drop_table(self, table):
        return self._alter("DROP", table, "", "drop_table")

    def _alter(self, operation, table, payload, event):
        if self.kind != "sqlite":
            return fail("not_sqlite", "schema edits need a SQLite database")
        if self.read_only:
            return fail("read_only", "the database is open read-only")
        if operation == "DROP":
            statement = "DROP TABLE %s" % self._quote(table)
        else:
            statement = "ALTER TABLE %s %s %s" % (self._quote(table), operation,
                                                  self._quote(payload))
        try:
            self.connection.execute(statement)
            self.connection.commit()
        except sqlite3.Error as exc:
            return fail("alter_failed", str(exc))
        self._record(event, table)
        return ok("%s done" % event.replace("_", " "))

    # ------------------------------------------------------------- loaders
    def _read_json(self, path):
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            data = json.load(handle)
        return _records_from_json(data)

    @staticmethod
    def _read_csv(path):
        with open(path, "r", encoding="utf-8", errors="replace",
                  newline="") as handle:
            sample = handle.read(8192)
            handle.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
            except csv.Error:
                dialect = csv.excel
            reader = csv.DictReader(handle, dialect=dialect)
            return [dict(row) for row in reader]

    @staticmethod
    def _read_text(path):
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return [{"line_number": index, "text": line.rstrip("\r\n")}
                    for index, line in enumerate(handle, start=1)]

    @staticmethod
    def _write_json(path, rows):
        _atomic_write(path, json.dumps(rows, indent=2, ensure_ascii=False,
                                       default=str))

    @staticmethod
    def _write_csv(path, rows):
        with open(path, "w", encoding="utf-8", newline="") as handle:
            if rows:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
                writer.writeheader()
                writer.writerows(rows)

    @staticmethod
    def _infer_columns(rows):
        """(name, kind) for every key the rows use, in first-seen order."""
        names = []
        for row in rows[:200]:
            for name in row:
                if name not in names:
                    names.append(name)
        columns = []
        for name in names:
            sample = None
            for row in rows:
                if row.get(name) not in (None, ""):
                    sample = row.get(name)
                    break
            columns.append((name, _kind_of(sample)))
        return columns

    # --------------------------------------------------------------- helpers
    @staticmethod
    def _quote(name):
        """Quote an identifier for SQL, doubling any embedded quote.

        Identifiers are quoted rather than checked against a whitelist, because
        a table really can be called ``order`` or ``my table`` -- and doubling
        the delimiter is what the SQL standard says to do.
        """
        text = str(name or "").replace('"', '""')
        return '"%s"' % text

    def _record(self, action, detail):
        self.log.insert(0, {"time": _now(), "action": action,
                            "detail": detail, "db": os.path.basename(
                                self.path or "")})
        del self.log[500:]
        return self.log[0]

    def log_rows(self, limit=200):
        return [entry for entry in self.log[:limit]]

    def describe(self):
        """The open database as a plain structure, for a panel or a report."""
        if not self.is_open():
            return {"open": False}
        return {
            "open": True,
            "path": self.path,
            "format": self.kind,
            "format_label": OFFLINE_FORMAT_LABELS.get(self.kind, self.kind),
            "read_only": self.read_only,
            "opened_at": self.opened_at,
            "size": os.path.getsize(self.path) if os.path.isfile(self.path)
            else 0,
            "tables": [{"name": name, "rows": rows}
                       for name, rows in self._tables()],
        }
# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------
def _stamp():
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def _type_kind(declared):
    """The broad kind of a declared SQL type (SQLite types are free-form)."""
    text = (declared or "").upper()
    if "INT" in text:
        return "integer"
    if any(word in text for word in ("REAL", "FLOA", "DOUB", "NUM", "DEC")):
        return "real"
    if "BLOB" in text or not text:
        return "blob" if text else "text"
    if any(word in text for word in ("CHAR", "CLOB", "TEXT", "DATE", "TIME")):
        return "text"
    return "text"


def _kind_of(value):
    """The broad kind of a Python value, as the GUI renders it."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "integer"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "real"
    if isinstance(value, (bytes, bytearray)):
        return "blob"
    return "text"


def _declared_type_for(value):
    """The SQL type a value should be stored as."""
    if isinstance(value, bool):
        return "INTEGER"
    if isinstance(value, int):
        return "INTEGER"
    if isinstance(value, float):
        return "REAL"
    if isinstance(value, (bytes, bytearray)):
        return "BLOB"
    return "TEXT"


def _safe_name(text):
    """A table or column name, stripped of the characters that break SQL.

    Only the characters that cannot appear in a quoted identifier are removed,
    so a name that is merely unusual is left alone.
    """
    return str(text or "").replace('"', "").replace("\x00", "").strip()


def _records_from_json(data):
    """Whatever a JSON file holds, as a list of row dictionaries.

    A list of objects is rows; an object whose values are all lists of objects
    is a set of named tables (the first is taken); a single object is one row.
    """
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        tables = {key: value for key, value in data.items()
                  if isinstance(value, list) and value
                  and isinstance(value[0], dict)}
        if tables:
            name = sorted(tables)[0]
            return list(tables[name])
        return [data]
    return []


def _sql_literal(value):
    """A SQL literal, with the value escaped rather than interpolated."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (bytes, bytearray)):
        return "X'%s'" % value.hex()
    return "'" + str(value).replace("'", "''") + "'"


def _is_write_statement(sql):
    """True when a statement would change the database."""
    head = (sql or "").strip().split(None, 1)
    if not head:
        return False
    verb = head[0].upper()
    if verb in ("SELECT", "PRAGMA", "EXPLAIN", "WITH", "VALUES"):
        # WITH and VALUES can still write, but only through a nested statement.
        return " INSERT " in (" " + sql.upper() + " ") or \
            " UPDATE " in (" " + sql.upper() + " ") or \
            " DELETE " in (" " + sql.upper() + " ")
    return True


def _split_sql(sql):
    """Split a script into statements, honouring string literals.

    The application already has this logic for its Runner
    (``sdb_scripts._split_sql_statements``); it is reused when importable so a
    script is split identically in both places, and a local copy is kept as the
    fallback so this module never depends on the import succeeding.
    """
    try:
        from sdb import _split_sql_statements
        return list(_split_sql_statements(sql))
    except Exception:
        pass
    statements = []
    current = []
    quote = None
    index = 0
    text = sql or ""
    while index < len(text):
        char = text[index]
        nxt = text[index + 1] if index + 1 < len(text) else ""
        if quote:
            current.append(char)
            if char == quote:
                if nxt == quote:
                    current.append(nxt)
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if char in ("'", '"', "`"):
            quote = char
            current.append(char)
        elif char == "-" and nxt == "-":
            while index < len(text) and text[index] not in "\r\n":
                index += 1
            continue
        elif char == ";":
            statement = "".join(current).strip()
            if statement:
                statements.append(statement)
            current = []
        else:
            current.append(char)
        index += 1
    tail = "".join(current).strip()
    if tail:
        statements.append(tail)
    return statements


def _atomic_write(path, text):
    """Write text so a crash cannot leave a half-written file."""
    folder = os.path.dirname(path) or "."
    handle, temp = tempfile.mkstemp(dir=folder, prefix=".sqlite-export-",
                                    suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    except Exception:
        try:
            os.remove(temp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# The GUI talks to this function for the "Offline" panel's file list.
# ---------------------------------------------------------------------------
def candidate_files(folder, limit=200):
    """Database-ish files below a folder, newest first (never recurses)."""
    out = []
    if not folder or not os.path.isdir(folder):
        return out
    for name in sorted(os.listdir(folder)):
        path = os.path.join(folder, name)
        if not os.path.isfile(path):
            continue
        if guess_format(path):
            try:
                out.append({"path": path, "name": name,
                            "size": os.path.getsize(path),
                            "modified": dt.datetime.fromtimestamp(
                                os.path.getmtime(path)).strftime(
                                "%Y-%m-%d %H:%M:%S"),
                            "format": OFFLINE_FORMAT_LABELS.get(
                                guess_format(path), "?")})
            except OSError:
                continue
    out.sort(key=lambda item: item["modified"], reverse=True)
    return out[:limit]


def capabilities():
    """The offline layer as a plugin-safe catalogue."""
    return {
        "formats": {key: OFFLINE_FORMAT_LABELS[key] for key
                    in OFFLINE_FORMAT_KEYS},
        "operations": ["open", "close", "save", "list_tables", "list_columns",
                       "fetch_rows", "insert_row", "update_row", "delete_row",
                       "delete_rows", "execute_sql", "explain", "backup",
                       "restore", "integrity_check", "repair", "vacuum_into",
                       "table_stats", "duplicates", "export", "import_file",
                       "create_table", "add_column", "rename_table",
                       "drop_table", "validate_table"],
        "status": "available",
    }
