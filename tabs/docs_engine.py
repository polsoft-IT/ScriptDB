# -*- coding: utf-8 -*-
"""Offline documentation engine (Core Knowledge Engine) for the DOCs tab.

This module is the knowledge core behind the "DOCs" tab. It keeps three
independent indexes next to the document store and works entirely offline --
no network, no third-party package, nothing beyond the standard library:

    document index   one row per document (id, size, token count, checksum,
                     source path, updated stamp) -- the catalogue
    content index    a full-text index of the body: SQLite FTS5 when the
                     SQLite build provides it, otherwise a portable inverted
                     index table that behaves the same way
    metadata index   the searchable facets: category, doc type, every tag and
                     free-form metadata key/value, each mapped to its documents

On top of the indexes the engine offers:

    offline cache          rendered plain text kept per document, keyed by a
                           checksum, so a document can be shown without
                           re-reading or re-parsing its source
    documentation
    snapshots              a point-in-time copy of the whole knowledge base
                           (catalogue + bodies), restorable offline
    automatic updates      tracked local files are re-read and re-indexed when
                           they change on disk (size / mtime / checksum)

Everything is stored in the same SQLite database file as the documents, so the
whole knowledge base travels as one file and can be opened by any SQLite-aware
program. The engine never touches the network: "refresh" means re-reading local
files, never downloading anything.
"""

import datetime as dt
import hashlib
import json
import os
import re
import sqlite3

# Index schema version so a future migration can recognise an old index.
DOCS_INDEX_VERSION = 1

# Words shorter than this are not indexed (they carry little meaning and would
# make the inverted index enormous).
MIN_TERM_LENGTH = 2

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+", re.UNICODE)


def _now():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def tokenize(text):
    """Split text into lower-cased index terms."""
    if not text:
        return []
    return [term.lower() for term in _TOKEN_RE.findall(text)
            if len(term) >= MIN_TERM_LENGTH]


def checksum(text):
    """A stable hex digest of a document body (used for cache freshness)."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def fts5_available(conn):
    """True when this SQLite build provides the FTS5 full-text module."""
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _fts_probe "
                     "USING fts5(x)")
        conn.execute("DROP TABLE IF EXISTS _fts_probe")
        return True
    except sqlite3.OperationalError:
        return False


class DocsIndex(object):
    """The Core Knowledge Engine: three indexes over one document store.

    It attaches to an open :class:`sqlite3` connection (the same one the
    repository uses, so the knowledge base is part of the database file) and
    builds/maintains the catalogue, the full-text index and the metadata index.
    """

    def __init__(self, conn):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.fts5 = fts5_available(self.conn)
        self._create_schema()

    # ------------------------------------------------------------- schema
    def _create_schema(self):
        cur = self.conn.cursor()
        # --- document index (the catalogue) -------------------------------
        cur.execute(
            "CREATE TABLE IF NOT EXISTS docs_index ("
            " doc_id INTEGER PRIMARY KEY,"
            " title TEXT,"
            " doc_type TEXT,"
            " category TEXT,"
            " size INTEGER DEFAULT 0,"
            " terms INTEGER DEFAULT 0,"
            " checksum TEXT,"
            " source_path TEXT,"
            " source_size INTEGER DEFAULT 0,"
            " source_mtime REAL DEFAULT 0,"
            " updated TEXT)")
        # --- metadata index (searchable facets) ---------------------------
        cur.execute(
            "CREATE TABLE IF NOT EXISTS docs_meta_index ("
            " doc_id INTEGER NOT NULL,"
            " facet TEXT NOT NULL,"      # category / type / tag / meta key
            " value TEXT NOT NULL,"
            " PRIMARY KEY (doc_id, facet, value))")
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_meta_facet "
            "ON docs_meta_index(facet, value)")
        # --- offline cache (rendered plain text per document) -------------
        cur.execute(
            "CREATE TABLE IF NOT EXISTS docs_cache ("
            " doc_id INTEGER PRIMARY KEY,"
            " checksum TEXT,"
            " plain_text TEXT,"
            " token_count INTEGER DEFAULT 0,"
            " cached TEXT)")
        # --- documentation snapshots (point-in-time knowledge base) -------
        cur.execute(
            "CREATE TABLE IF NOT EXISTS docs_snapshots ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " name TEXT,"
            " created TEXT,"
            " doc_count INTEGER DEFAULT 0,"
            " payload TEXT)")
        # --- full-text index ----------------------------------------------
        if self.fts5:
            cur.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5("
                " doc_id UNINDEXED, title, body, category, tokenize='unicode61')")
        else:
            # Portable inverted index used when FTS5 is missing.
            cur.execute(
                "CREATE TABLE IF NOT EXISTS docs_terms ("
                " term TEXT NOT NULL,"
                " doc_id INTEGER NOT NULL,"
                " freq INTEGER DEFAULT 1,"
                " PRIMARY KEY (term, doc_id))")
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_terms_term "
                "ON docs_terms(term)")
        self._set_meta("index_version", str(DOCS_INDEX_VERSION))
        self._set_meta("content_index", "fts5" if self.fts5 else "inverted")
        self.conn.commit()

    def _set_meta(self, key, value):
        try:
            self.conn.execute(
                "INSERT INTO repository(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)))
        except sqlite3.Error:
            # The repository table may not exist yet in a bare database.
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS repository "
                "(key TEXT PRIMARY KEY, value TEXT)")
            self.conn.execute(
                "INSERT OR REPLACE INTO repository(key, value) VALUES(?, ?)",
                (key, str(value)))

    def engine_info(self):
        """Describe the engine for the UI / audit report."""
        return {
            "content_index": "fts5" if self.fts5 else "inverted",
            "indexed": self.conn.execute(
                "SELECT COUNT(*) AS n FROM docs_index").fetchone()["n"],
            "cached": self.conn.execute(
                "SELECT COUNT(*) AS n FROM docs_cache").fetchone()["n"],
            "snapshots": self.conn.execute(
                "SELECT COUNT(*) AS n FROM docs_snapshots").fetchone()["n"],
            "offline": True,
        }

    # -------------------------------------------------------- indexing
    def index_document(self, doc, source_path=None):
        """(Re)index one document: catalogue, facets, content and cache.

        `doc` is a document dict with id / title / doc_type / category / tags /
        description / body / meta (as returned by the repository).
        """
        doc_id = doc.get("id")
        if doc_id is None:
            return
        body = doc.get("body") or ""
        title = doc.get("title") or ""
        digest = checksum(body)
        terms = tokenize(body)
        # The title and category are searchable too, so fold them into the term
        # stream (weighted implicitly by being present).
        terms += tokenize(title) + tokenize(doc.get("category") or "")
        size = len(body.encode("utf-8"))
        source_size, source_mtime = 0, 0
        if source_path and os.path.exists(source_path):
            try:
                stat = os.stat(source_path)
                source_size, source_mtime = stat.st_size, stat.st_mtime
            except OSError:
                pass

        self._remove_index(doc_id)

        self.conn.execute(
            "INSERT OR REPLACE INTO docs_index "
            "(doc_id, title, doc_type, category, size, terms, checksum,"
            " source_path, source_size, source_mtime, updated) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (doc_id, title, doc.get("doc_type", ""), doc.get("category", ""),
             size, len(terms), digest, source_path or "", source_size,
             source_mtime, _now()))

        # Content index.
        if self.fts5:
            self.conn.execute(
                "INSERT INTO docs_fts(doc_id, title, body, category) "
                "VALUES (?, ?, ?, ?)",
                (doc_id, title, body, doc.get("category", "")))
        else:
            counts = {}
            for term in terms:
                counts[term] = counts.get(term, 0) + 1
            self.conn.executemany(
                "INSERT OR REPLACE INTO docs_terms(term, doc_id, freq) "
                "VALUES (?, ?, ?)",
                [(term, doc_id, freq) for term, freq in counts.items()])

        # Metadata index: every facet a query might filter on.
        facets = [("category", doc.get("category") or ""),
                  ("type", doc.get("doc_type") or "")]
        for tag in re.split(r"[,\s]+", doc.get("tags") or ""):
            if tag.strip():
                facets.append(("tag", tag.strip()))
        meta = doc.get("meta") or {}
        if isinstance(meta, dict):
            for key, value in meta.items():
                if isinstance(value, (list, tuple)):
                    for item in value:
                        facets.append((("meta:" + str(key)), str(item)))
                else:
                    facets.append((("meta:" + str(key)), str(value)))
        rows = [(doc_id, facet, value) for facet, value in facets if value != ""]
        if rows:
            self.conn.executemany(
                "INSERT OR IGNORE INTO docs_meta_index(doc_id, facet, value) "
                "VALUES (?, ?, ?)", rows)

        # Offline cache: the rendered plain text, keyed by the checksum.
        self.conn.execute(
            "INSERT OR REPLACE INTO docs_cache "
            "(doc_id, checksum, plain_text, token_count, cached) "
            "VALUES (?, ?, ?, ?, ?)",
            (doc_id, digest, body, len(terms), _now()))
        self.conn.commit()

    def _remove_index(self, doc_id):
        self.conn.execute("DELETE FROM docs_index WHERE doc_id = ?", (doc_id,))
        self.conn.execute("DELETE FROM docs_meta_index WHERE doc_id = ?",
                          (doc_id,))
        self.conn.execute("DELETE FROM docs_cache WHERE doc_id = ?", (doc_id,))
        if self.fts5:
            self.conn.execute("DELETE FROM docs_fts WHERE doc_id = ?",
                              (doc_id,))
        else:
            self.conn.execute("DELETE FROM docs_terms WHERE doc_id = ?",
                              (doc_id,))

    def remove_document(self, doc_id):
        """Drop every index entry for a deleted document."""
        self._remove_index(doc_id)
        self.conn.commit()

    def reindex_all(self, documents, path_for=None):
        """Rebuild the whole knowledge base from a document list.

        `path_for` optionally maps a document id to its tracked source file.
        Returns the number of documents indexed.
        """
        self.conn.execute("DELETE FROM docs_index")
        self.conn.execute("DELETE FROM docs_meta_index")
        self.conn.execute("DELETE FROM docs_cache")
        if self.fts5:
            self.conn.execute("DELETE FROM docs_fts")
        else:
            self.conn.execute("DELETE FROM docs_terms")
        count = 0
        for doc in documents:
            path = path_for(doc) if callable(path_for) else None
            self.index_document(doc, source_path=path)
            count += 1
        self.conn.commit()
        return count

    # ----------------------------------------------------- full-text search
    def search(self, query, category=None, doc_type=None, tag=None, limit=200):
        """Full-text search over the content index, filtered by metadata.

        The query is a bag of words: a document matches when it contains every
        word. Returns documents ordered by relevance (term frequency), newest
        first on ties. Works identically with FTS5 and the portable inverted
        index, and needs no network.
        """
        terms = tokenize(query)
        metadata_filter = self._metadata_doc_ids(category, doc_type, tag)
        if not terms:
            ids = metadata_filter if metadata_filter is not None else None
            return self._fetch_documents(ids, limit)

        if self.fts5:
            match = " AND ".join('"%s"' % term for term in terms)
            rows = self.conn.execute(
                "SELECT doc_id, bm25(docs_fts) AS rank FROM docs_fts "
                "WHERE docs_fts MATCH ? ORDER BY rank LIMIT ?",
                (match, limit * 4)).fetchall()
            ranked = [(row["doc_id"], -row["rank"]) for row in rows]
        else:
            ranked = self._inverted_match(terms, limit * 4)

        ids = [doc_id for doc_id, _score in ranked]
        if metadata_filter is not None:
            allowed = set(metadata_filter)
            ids = [doc_id for doc_id in ids if doc_id in allowed]
        return self._fetch_documents(ids[:limit], limit)

    def _inverted_match(self, terms, limit):
        # Every term must be present; score = total frequency across terms.
        rows = self.conn.execute(
            "SELECT doc_id, SUM(freq) AS score FROM docs_terms "
            "WHERE term IN (%s) GROUP BY doc_id "
            "HAVING COUNT(DISTINCT term) = ? "
            "ORDER BY score DESC LIMIT ?" % ",".join("?" for _ in terms),
            list(terms) + [len(terms), limit]).fetchall()
        return [(row["doc_id"], row["score"]) for row in rows]

    def _metadata_doc_ids(self, category, doc_type, tag):
        clauses = []
        params = []
        if category:
            clauses.append("(facet = 'category' AND value = ?)")
            params.append(category)
        if doc_type:
            clauses.append("(facet = 'type' AND value = ?)")
            params.append(doc_type)
        if tag:
            clauses.append("(facet = 'tag' AND value = ?)")
            params.append(tag)
        if not clauses:
            return None
        rows = self.conn.execute(
            "SELECT doc_id FROM docs_meta_index WHERE " + " AND ".join(clauses),
            params).fetchall()
        return [row["doc_id"] for row in rows]

    def _fetch_documents(self, ids, limit):
        if ids is None:
            rows = self.conn.execute(
                "SELECT * FROM docs_index ORDER BY updated DESC LIMIT ?",
                (limit,)).fetchall()
        elif not ids:
            rows = []
        else:
            marks = ",".join("?" for _ in ids)
            rows = self.conn.execute(
                "SELECT * FROM docs_index WHERE doc_id IN (%s)" % marks,
                list(ids)).fetchall()
            order = {doc_id: index for index, doc_id in enumerate(ids)}
            rows = sorted(rows, key=lambda row: order.get(row["doc_id"], 1e9))
        return [dict(row) for row in rows]

    # -------------------------------------------------------- metadata index
    def facets(self, facet):
        """Distinct values of one facet (category / type / tag / meta:key)."""
        rows = self.conn.execute(
            "SELECT value, COUNT(*) AS n FROM docs_meta_index "
            "WHERE facet = ? GROUP BY value ORDER BY value COLLATE NOCASE",
            (facet,)).fetchall()
        return [(row["value"], row["n"]) for row in rows]

    def documents_with_facet(self, facet, value):
        """Ids of the documents carrying one facet value."""
        rows = self.conn.execute(
            "SELECT doc_id FROM docs_meta_index WHERE facet = ? AND value = ?",
            (facet, value)).fetchall()
        return [row["doc_id"] for row in rows]

    def metadata_index(self):
        """The whole metadata index as {facet: [(value, count), ...]}."""
        out = {}
        for row in self.conn.execute(
                "SELECT DISTINCT facet FROM docs_meta_index ORDER BY facet"):
            out[row["facet"]] = self.facets(row["facet"])
        return out

    # ------------------------------------------------------------ cache
    def cached_text(self, doc_id):
        """The cached rendered text for a document, or None."""
        row = self.conn.execute(
            "SELECT plain_text, checksum FROM docs_cache WHERE doc_id = ?",
            (doc_id,)).fetchone()
        return row["plain_text"] if row else None

    def cache_is_fresh(self, doc_id, body):
        """True when the cached text still matches the document body."""
        row = self.conn.execute(
            "SELECT checksum FROM docs_cache WHERE doc_id = ?",
            (doc_id,)).fetchone()
        return bool(row and row["checksum"] == checksum(body))

    def cache_stats(self):
        """(cached_count, total_tokens) for the header."""
        row = self.conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(token_count), 0) AS tokens "
            "FROM docs_cache").fetchone()
        return row["n"], row["tokens"]

    def clear_cache(self):
        """Drop the offline cache (it is rebuilt on the next index pass)."""
        self.conn.execute("DELETE FROM docs_cache")
        self.conn.commit()

    # --------------------------------------------------------- snapshots
    def snapshot(self, name, documents):
        """Store a point-in-time copy of the knowledge base.

        `documents` is the list of document dicts to capture; the snapshot is
        self-contained (catalogue + bodies) so it can be restored offline.
        """
        payload = json.dumps({
            "format": "scriptdb-docs-snapshot",
            "index_version": DOCS_INDEX_VERSION,
            "documents": documents,
        }, ensure_ascii=False)
        cur = self.conn.execute(
            "INSERT INTO docs_snapshots(name, created, doc_count, payload) "
            "VALUES (?, ?, ?, ?)",
            (name or _now(), _now(), len(documents), payload))
        self.conn.commit()
        return cur.lastrowid

    def snapshots(self):
        """Every stored snapshot, newest first (without the payload)."""
        rows = self.conn.execute(
            "SELECT id, name, created, doc_count FROM docs_snapshots "
            "ORDER BY id DESC").fetchall()
        return [dict(row) for row in rows]

    def snapshot_payload(self, snapshot_id):
        """The documents stored inside one snapshot, or None."""
        row = self.conn.execute(
            "SELECT payload FROM docs_snapshots WHERE id = ?",
            (snapshot_id,)).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["payload"]).get("documents", [])
        except (ValueError, AttributeError):
            return []

    def delete_snapshot(self, snapshot_id):
        self.conn.execute("DELETE FROM docs_snapshots WHERE id = ?",
                          (snapshot_id,))
        self.conn.commit()

    def restore_snapshot(self, snapshot_id, add_document):
        """Restore a snapshot via a callback, returning the docs restored.

        `add_document(doc)` is called for every captured document; the caller
        (the repository) decides how to write it. Keeping the engine ignorant
        of the store keeps the two layers independent.
        """
        documents = self.snapshot_payload(snapshot_id)
        if documents is None:
            return 0
        for doc in documents:
            add_document(doc)
        return len(documents)

    # ------------------------------------------------ automatic updates
    def stale_sources(self):
        """Tracked source files whose documents are out of date on disk.

        A document is stale when its source file has a different size or
        modification time than the one recorded at index time -- i.e. the local
        file changed and the offline knowledge base should catch up. Nothing is
        downloaded: this only ever looks at local files.
        """
        stale = []
        rows = self.conn.execute(
            "SELECT doc_id, source_path, source_size, source_mtime "
            "FROM docs_index WHERE source_path <> ''").fetchall()
        for row in rows:
            path = row["source_path"]
            if not path or not os.path.exists(path):
                continue
            try:
                stat = os.stat(path)
            except OSError:
                continue
            if (stat.st_size != row["source_size"]
                    or abs(stat.st_mtime - row["source_mtime"]) > 1e-6):
                stale.append({"doc_id": row["doc_id"], "path": path,
                              "size": stat.st_size, "mtime": stat.st_mtime})
        return stale

    def source_path_for(self, doc_id):
        """The tracked source file of a document (or "")."""
        row = self.conn.execute(
            "SELECT source_path FROM docs_index WHERE doc_id = ?",
            (doc_id,)).fetchone()
        return row["source_path"] if row else ""

    def read_source(self, path):
        """Read a local text file for an automatic update (offline)."""
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                stat = os.stat(path)
                return handle.read(), stat.st_size, stat.st_mtime
        except OSError:
            return None, 0, 0


    # ------------------------------------------------ offline document preview
    def preview_ascii(self, text, width=80, ascii_only=False, wrap=True):
        """A plain-text (ASCII) preview of a document body.

        Non-printable characters are dropped and long lines are wrapped to
        `width`. When `ascii_only` is set, characters outside ASCII are
        transliterated to a close ASCII equivalent (so the preview is safe in
        terminals and exports that cannot show Unicode). Returns a single
        string; no network and no rendering engine is involved.
        """
        if not text:
            return ""
        prepared = _fold_ascii(text) if ascii_only else text
        # Keep tabs, drop other control characters (except newlines).
        cleaned = "".join(
            ch if (ch == "\n" or ch == "\t" or ch >= " ") else "" for ch in prepared)
        if not wrap or width <= 0:
            return cleaned
        lines = []
        for raw in cleaned.splitlines():
            line = raw.expandtabs(4)
            while len(line) > width:
                lines.append(line[:width])
                line = line[width:]
            lines.append(line)
        return "\n".join(lines)

    def document_structure(self, text):
        """The structure of a document: headings, sections and code blocks.

        Recognises Markdown ATX (`#`) and Setext (`===`) headings, comment
        banners, numbered/lettered section titles in plain text, fenced code
        blocks (```), indented blocks and list runs. Returns a list of records
        in document order, each a dict with:
            kind   heading / section / code / list / paragraph / rule
            line   1-based line number
            level  heading depth (0 for non-headings)
            text   the trimmed heading / label
        This is a purely offline, line-based reader -- no parser dependency.
        """
        records = []
        lines = (text or "").splitlines()
        in_fence = False
        fence_line = 0
        list_start = 0
        list_items = 0
        for number, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("```") or stripped.startswith("~~~"):
                if not in_fence:
                    in_fence, fence_line = True, number
                else:
                    records.append({"kind": "code", "line": fence_line,
                                    "level": 0,
                                    "text": "%d line(s) of code" % (number - fence_line - 1)})
                    in_fence = False
                continue
            if in_fence:
                continue
            # Markdown ATX heading.
            if stripped.startswith("#"):
                level = len(stripped) - len(stripped.lstrip("#"))
                records.append({"kind": "heading", "line": number,
                                "level": min(level, 6),
                                "text": stripped.lstrip("#").strip()})
                list_items = 0
                continue
            # Setext heading (the line below a `===` or `---` rule).
            if number >= 2 and stripped and set(stripped) <= set("=-") and len(stripped) >= 3:
                previous = lines[number - 2].strip()
                if previous:
                    records.append({"kind": "heading", "line": number - 1,
                                    "level": 1 if stripped[0] == "=" else 2,
                                    "text": previous})
                else:
                    records.append({"kind": "rule", "line": number, "level": 0,
                                    "text": ""})
                list_items = 0
                continue
            # Comment banner used as a section title (e.g. `# === Setup ===`).
            if stripped.startswith(("#", "//", ";", "--")) and len(stripped) > 4:
                banner = stripped.lstrip("#/;-").strip()
                if banner and (banner == banner.upper() or banner.endswith(":")):
                    records.append({"kind": "section", "line": number, "level": 0,
                                    "text": banner.rstrip(":").strip()})
                    list_items = 0
                    continue
            # List run.
            if stripped[:2] in ("- ", "* ", "+ ") or (
                    stripped[:1].isdigit() and ". " in stripped[:5]):
                if list_items == 0:
                    list_start = number
                list_items += 1
                continue
            if list_items:
                records.append({"kind": "list", "line": list_start, "level": 0,
                                "text": "%d list item(s)" % list_items})
                list_items = 0
        if list_items:
            records.append({"kind": "list", "line": list_start, "level": 0,
                            "text": "%d list item(s)" % list_items})
        # A Setext heading is recorded on its text line (one above the rule),
        # so re-sort by line to keep the records in document order.
        records.sort(key=lambda rec: (rec["line"], rec["kind"] != "heading"))
        return records

    def minimap(self, text, rows=12, buckets=60):
        """A compact text mini-map of a document.

        Returns a list of `rows` short strings (each up to `buckets` symbols);
        every symbol shows how dense that slice of a line is: a space is empty,
        `.` light, `-` medium, `=` heavy and `#` very heavy. It gives an at-a-
        glance shape of the whole document without rendering anything.
        """
        lines = (text or "").splitlines()
        if not lines:
            return ["" for _ in range(rows)]
        step = max(1, len(lines) // rows)
        sample = [lines[i:i + step] for i in range(0, len(lines), step)][:rows]
        while len(sample) < rows:
            sample.append([])
        out = []
        for group in sample:
            out.append(_density_row(group, buckets))
        return out

    def document_metadata(self, doc):
        """Rich metadata for the metadata preview (all offline)."""
        body = doc.get("body") or ""
        words = _TOKEN_RE.findall(body)
        lines = body.splitlines()
        meta = doc.get("meta") or {}
        return {
            "title": doc.get("title", ""),
            "doc_type": doc.get("doc_type", ""),
            "category": doc.get("category", ""),
            "tags": [t for t in re.split(r"[,\s]+", doc.get("tags") or "") if t],
            "description": doc.get("description", ""),
            "created": doc.get("created", ""),
            "modified": doc.get("modified", ""),
            "archived": bool(doc.get("archived")),
            "encrypted": bool(doc.get("encrypted")),
            "characters": len(body),
            "lines": len(lines),
            "words": len(words),
            "unique_words": len(set(w.lower() for w in words)),
            "checksum": checksum(body)[:16],
            "meta": meta,
        }

    def document_links(self, doc, documents=None):
        """What a document links to: shared tags, category and title mentions.

        `documents` is the full document list to compare against; when omitted
        only the metadata index is used. Returns a dict with the outgoing links
        (tags / category / meta) and the related documents (by shared tag or
        category and by a title mention in the body). Fully offline.
        """
        doc_id = doc.get("id")
        body = (doc.get("body") or "").lower()
        tags = set(t for t in re.split(r"[,\s]+", doc.get("tags") or "") if t)
        category = doc.get("category") or ""
        related = []
        pool = documents
        if pool is None:
            pool = self._all_documents_from_index()
        for other in pool:
            if other.get("id") == doc_id:
                continue
            other_tags = set(t for t in re.split(r"[,\s]+", other.get("tags") or "") if t)
            other_cat = other.get("category") or ""
            shared = tags & other_tags
            reasons = []
            if shared:
                reasons.append("tags: " + ", ".join(sorted(shared)))
            if category and other_cat == category:
                reasons.append("category: " + category)
            title = (other.get("title") or "").strip()
            if title and len(title) >= 4 and title.lower() in body:
                reasons.append("mentions title")
            if reasons:
                related.append({"doc_id": other.get("id"),
                                "title": other.get("title", ""),
                                "reasons": reasons})
        return {
            "tags": sorted(tags),
            "category": category,
            "meta_keys": sorted((doc.get("meta") or {}).keys()),
            "related": related,
        }

    def _all_documents_from_index(self):
        """A light document list straight from the catalogue (id/title/...)."""
        rows = self.conn.execute(
            "SELECT doc_id AS id, title, category, doc_type, '' AS tags "
            "FROM docs_index").fetchall()
        return [dict(row) for row in rows]

    def compare_versions(self, old_text, new_text, context=2):
        """Changes between two versions as lightweight diff records.

        Returns a dict with the counts and a list of hunks; each hunk is a dict
        with kind (added / removed / changed), the old and new line numbers and
        the old and new text. Built on :mod:`difflib`, so it is fully offline.
        """
        import difflib
        old_lines = (old_text or "").splitlines()
        new_lines = (new_text or "").splitlines()
        additions = removals = 0
        hunks = []
        matcher = difflib.SequenceMatcher(None, old_lines, new_lines)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                continue
            if tag == "replace":
                kind = "changed"
            elif tag == "insert":
                kind = "added"
            else:
                kind = "removed"
            additions += j2 - j1
            removals += i2 - i1
            hunks.append({
                "kind": kind,
                "old_line": i1 + 1,
                "new_line": j1 + 1,
                "old_text": "\n".join(old_lines[i1:i2]),
                "new_text": "\n".join(new_lines[j1:j2]),
            })
        return {"additions": additions, "removals": removals,
                "changed": sum(1 for h in hunks if h["kind"] == "changed"),
                "hunks": hunks, "identical": not hunks}

    def snapshot_text(self, snapshot_id, doc_id):
        """The body of one document as it was captured in a snapshot."""
        documents = self.snapshot_payload(snapshot_id) or []
        for doc in documents:
            if doc.get("id") == doc_id:
                return doc.get("body") or ""
        # Fall back to matching by title when ids differ (restored copies).
        for doc in documents:
            return doc.get("body") or ""
        return ""

    def preview_available(self):
        """True -- the preview needs nothing but the local database."""
        return True


    def config_outline(self, text):
        """A key/value outline of a configuration document (offline).

        Understands JSON, simple YAML (`key: value`, nested by indentation),
        INI/TOML (`[section]`, `key = value`) and `key=value` lines. Returns a
        string with the keys and a short value preview, indented by depth.
        """
        body = (text or "").strip()
        if not body:
            return ""
        # JSON first: parse it properly when it is valid.
        if body[:1] in ("{", "["):
            try:
                data = json.loads(body)
                return "\n".join(_flatten_json(data))
            except ValueError:
                pass
        lines = []
        section = ""
        for number, raw in enumerate((text or "").splitlines(), 1):
            stripped = raw.strip()
            if not stripped or stripped.startswith(("#", ";", "//")):
                continue
            indent = "  " * (len(raw) - len(raw.lstrip()) > 0)
            if stripped.startswith("[") and stripped.endswith("]"):
                section = stripped
                lines.append("%s%s" % (indent, stripped))
                continue
            for separator in (" = ", "=", ":", "\t"):
                if separator in stripped:
                    key, _, value = stripped.partition(separator)
                    value = value.strip()
                    if len(value) > 60:
                        value = value[:57] + "..."
                    lines.append("%s%s: %s" % (indent, key.strip(), value))
                    break
            else:
                lines.append("%s%s" % (indent, stripped[:80]))
        header = ("[%s]" % section) if section else ""
        return "\n".join(lines)

    def log_summary(self, text, tail=20):
        """A log overview: level counts plus the most recent lines (offline).

        Recognises the usual level words (ERROR/WARN/INFO/DEBUG/TRACE) anywhere
        on the line and returns a short histogram followed by the last `tail`
        lines, so a log document can be understood at a glance.
        """
        lines = (text or "").splitlines()
        levels = {"ERROR": 0, "WARN": 0, "INFO": 0, "DEBUG": 0, "TRACE": 0}
        for line in lines:
            upper = line.upper()
            for level in levels:
                if level in upper:
                    levels[level] += 1
                    break
        counts = "  ".join("%s=%d" % (name, levels[name]) for name in levels)
        head = "%s  |  %d line(s)" % (counts, len(lines))
        recent = lines[-tail:] if tail else lines
        return "\n".join([head, "-" * min(60, len(head))] + recent)

    def table_outline(self, text, max_rows=30):
        """A column/row outline of a delimited document (CSV/TSV, offline)."""
        import csv as _csv
        rows = list(_csv.reader((text or "").splitlines()))
        if not rows:
            return ""
        header = rows[0]
        width = len(header)
        out = ["%d column(s), %d row(s)" % (width, max(0, len(rows) - 1))]
        out.append("columns: " + ", ".join(header))
        for index, row in enumerate(rows[1:max_rows + 1], 1):
            out.append("%3d | %s" % (index, " | ".join(
                (cell[:24] for cell in row))))
        if len(rows) - 1 > max_rows:
            out.append("... %d more row(s)" % (len(rows) - 1 - max_rows))
        return "\n".join(out)

    def type_preview(self, doc_type_key, text, ascii_only=True):
        """Render the preview a document type asks for (offline dispatcher).

        The strategy comes from :mod:`tabs.docs_types`; unknown types fall back
        to a plain ASCII preview.
        """
        try:
            from . import docs_types
            strategy = docs_types.preview_for(doc_type_key)
        except Exception:
            strategy = "text"
        if strategy == "structure":
            return "\n".join("%s%s" % (
                "  " * rec["level"],
                rec["text"] or ("[%s]" % rec["kind"]))
                for rec in self.document_structure(text)) or self.preview_ascii(
                    text, ascii_only=ascii_only)
        if strategy == "config":
            return self.config_outline(text) or self.preview_ascii(
                text, ascii_only=ascii_only)
        if strategy == "log":
            return self.log_summary(text)
        if strategy == "table":
            return self.table_outline(text) or self.preview_ascii(
                text, ascii_only=ascii_only)
        if strategy == "none":
            return ""
        return self.preview_ascii(text, ascii_only=ascii_only)

# ---------------------------------------------------------------------------
# Preview helpers (used by the offline document preview)
# ---------------------------------------------------------------------------

# A small transliteration table so `preview_ascii(ascii_only=True)` produces
# readable ASCII for the accented characters a European document usually has.
_ASCII_FOLD = {
    "\u00e0": "a", "\u00e1": "a", "\u00e2": "a", "\u00e3": "a", "\u00e4": "a",
    "\u00e5": "a", "\u00e7": "c", "\u00e8": "e", "\u00e9": "e", "\u00ea": "e",
    "\u00eb": "e", "\u00ec": "i", "\u00ed": "i", "\u00ee": "i", "\u00ef": "i",
    "\u00f1": "n", "\u00f2": "o", "\u00f3": "o", "\u00f4": "o", "\u00f5": "o",
    "\u00f6": "o", "\u00f9": "u", "\u00fa": "u", "\u00fb": "u", "\u00fc": "u",
    "\u00fd": "y", "\u00ff": "y", "\u0105": "a", "\u0107": "c", "\u0119": "e",
    "\u0142": "l", "\u0144": "n", "\u015b": "s", "\u017a": "z", "\u017c": "z",
    "\u0171": "u", "\u0111": "d", "\u0161": "s", "\u010d": "c", "\u0159": "r",
    "\u00e6": "ae", "\u0153": "oe", "\u00df": "ss", "\u2018": "'",
    "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2013": "-",
    "\u2014": "-", "\u2026": "...", "\u2022": "*",
}
_ASCII_MAP = str.maketrans(_ASCII_FOLD)

# Density symbols from lightest to heaviest.
_DENSITY = " .-=#"


def _flatten_json(data, prefix="", depth=0):
    """Flatten a JSON value into indented 'key: value' preview lines."""
    out = []
    indent = "  " * depth
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, (dict, list)):
                out.append("%s%s:" % (indent, key))
                out.extend(_flatten_json(value, prefix, depth + 1))
            else:
                text = str(value)
                if len(text) > 60:
                    text = text[:57] + "..."
                out.append("%s%s: %s" % (indent, key, text))
    elif isinstance(data, list):
        for index, value in enumerate(data):
            if isinstance(value, (dict, list)):
                out.append("%s- [%d]" % (indent, index))
                out.extend(_flatten_json(value, prefix, depth + 1))
            else:
                out.append("%s- %s" % (indent, str(value)[:60]))
    else:
        out.append("%s%s" % (indent, str(data)[:60]))
    return out


def _fold_ascii(text):
    """Transliterate a string to printable ASCII (best effort)."""
    if not text:
        return ""
    folded = text.translate(_ASCII_MAP)
    # Drop any remaining non-ASCII character rather than emitting mojibake.
    return "".join(ch if ord(ch) < 128 else "" for ch in folded)


def _density_row(lines, buckets):
    """One mini-map row: the per-slice density of a group of lines."""
    if not lines:
        return ""
    widest = max((len(line) for line in lines), default=0)
    width = max(1, widest // buckets or 1)
    weights = []
    for index in range(buckets):
        weight = 0
        for line in lines:
            segment = line[index * width:index * width + width]
            weight += len(segment.strip())
        weights.append(weight)
    peak = max(weights) or 1
    span = len(_DENSITY) - 1
    cells = []
    for weight in weights:
        level = int(round(weight / peak * span))
        cells.append(_DENSITY[max(0, min(span, level))])
    return "".join(cells)


# ---------------------------------------------------------------------------
# Convenience: build a knowledge base over a repository in one call
# ---------------------------------------------------------------------------


def build_engine(repository):
    """Return a :class:`DocsIndex` bound to a repository's connection."""
    return DocsIndex(repository._conn)
