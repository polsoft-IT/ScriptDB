# -*- coding: utf-8 -*-
"""Exercise the advanced documentation search against a real index.

Every one of the thirteen kinds is run over a small but realistic corpus, so the
result is evidence that the search works rather than a restatement of the
design. Nothing here is mocked: the index is built by `tabs.docs_engine` and
queried through `tabs.docs_search`.
"""
import os
import sqlite3
import sys
import tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tabs import docs_engine, docs_search

failures = []


def check(label, ok, detail=""):
    line = ("  OK   " if ok else "  FAIL ") + label + (
        "   <%s>" % detail if detail and not ok else "")
    print(str(line).encode("ascii", "replace").decode("ascii"))
    if not ok:
        failures.append(label)


GUIDE = {
    "id": 1, "title": "Backup and recovery guide", "doc_type": "guide",
    "category": "Operations", "tags": "backup, recovery, policy",
    "description": "How to take a backup and recover the database",
    "body": (
        "# Backup and recovery guide\n"
        "\n"
        "This guide explains how to take a backup safely.\n"
        "\n"
        "## Taking a backup\n"
        "\n"
        "Stop the writers before you copy the database files.\n"
        "\n"
        "## Recovery\n"
        "\n"
        "Restore the newest archive and replay the journal.\n"
    ),
    "meta": {"author": "kowalski", "version": "2.10", "created": "2024-03-15",
             "modified": "2024-09-02", "hash": "ab12cdef",
             "history": "2024-08-01 reindexed after the schema change"},
}

API = {
    "id": 2, "title": "Archive retention policy", "doc_type": "policy",
    "category": "Compliance", "tags": "archive, policy",
    "description": "Retention rules for archived copies",
    "body": (
        "Archive retention policy\n"
        "=======================\n"
        "\n"
        "Copies are moved to cold storage after ninety days.\n"
        "The archival copy is verified by its checksum.\n"
    ),
    "meta": {"author": "nowak", "version": "2.9", "created": "2023-11-01",
             "modified": "2024-01-20", "hash": "99887766",
             "archived": "true"},
}

MANUAL = {
    "id": 3, "title": "Installing the agent", "doc_type": "manual",
    "category": "Operations", "tags": "install",
    "description": "Step by step installation",
    "body": (
        "1.1) Installing the agent\n"
        "\n"
        "Run the installer and accept the defaults.\n"
        "\n"
        "1.2) Verifying the install\n"
        "\n"
        "Check the service is running.\n"
    ),
    "meta": {"author": "kowalski", "version": "1.4", "created": "2022-05-10",
             "modified": "2022-06-01"},
}

OLD = {
    "id": 4, "title": "Legacy notes", "doc_type": "note",
    "category": "Operations", "tags": "legacy",
    "description": "Old notes kept for reference",
    "body": "These notes describe the separate archive procedure.\n",
    "meta": {"author": "zielinski", "version": "1.0", "created": "2019-01-05",
             "modified": "2019-01-05", "archived": "true"},
}

DOCUMENTS = [GUIDE, API, MANUAL, OLD]


def ids(hits):
    return sorted(hit["doc_id"] for hit in hits)


with tempfile.TemporaryDirectory() as tmp:
    # The index attaches to a connection rather than opening one, because in
    # the application the knowledge base lives inside the repository's own
    # database file. A verifier has to give it a connection the same way.
    conn = sqlite3.connect(os.path.join(tmp, "docs.sqlite"))
    engine = docs_engine.DocsIndex(conn)
    count = engine.reindex_all(DOCUMENTS)
    check("the index built", count == len(DOCUMENTS), str(count))
    print()

    print("1. content")
    hits = docs_search.run(engine, "backup recovery", DOCUMENTS)
    check("content search finds the guide", 1 in ids(hits), str(ids(hits)))
    hits = docs_search.run(engine, "content retencja", DOCUMENTS)
    check("a word that is not present finds nothing", ids(hits) == [])

    print()
    print("2. sections")
    hits = docs_search.run(engine, "section:Recovery", DOCUMENTS)
    check("section search finds the section by name", 1 in ids(hits),
          str(ids(hits)))
    hits = docs_search.run(engine, "section:Verifying", DOCUMENTS)
    check("section search reads the identifier style", 3 in ids(hits),
          str(ids(hits)))

    print()
    print("3. headings")
    hits = docs_search.run(engine, "heading:Taking", DOCUMENTS)
    check("heading search finds the markdown heading", 1 in ids(hits),
          str(ids(hits)))
    hits = docs_search.run(engine, "heading:ninety", DOCUMENTS)
    check("a word only in the prose is not a heading hit", ids(hits) == [],
          str(ids(hits)))

    print()
    print("4. tags")
    hits = docs_search.run(engine, "tag:policy", DOCUMENTS)
    check("tag search finds both tagged documents", ids(hits) == [1, 2],
          str(ids(hits)))

    print()
    print("5. document type")
    hits = docs_search.run(engine, "type:guide", DOCUMENTS)
    check("type search finds the guide", ids(hits) == [1], str(ids(hits)))

    print()
    print("6. date")
    hits = docs_search.run(engine, "date:2024-01-01..2024-12-31", DOCUMENTS)
    check("a date range finds the 2024 documents", ids(hits) == [1, 2],
          str(ids(hits)))
    hits = docs_search.run(engine, "date:2022", DOCUMENTS)
    check("a bare year means the whole year", ids(hits) == [3],
          str(ids(hits)))
    hits = docs_search.run(engine, "after:2024-06-01", DOCUMENTS)
    check("'after' finds the later document", ids(hits) == [1], str(ids(hits)))
    hits = docs_search.run(engine, "before:2020-01-01", DOCUMENTS)
    check("'before' finds the older document", ids(hits) == [4], str(ids(hits)))

    print()
    print("7. author")
    hits = docs_search.run(engine, "author:kowalski", DOCUMENTS)
    check("author search finds both of one author's documents",
          ids(hits) == [1, 3], str(ids(hits)))
    hits = docs_search.run(engine, "author:kow", DOCUMENTS)
    check("author search is forgiving about a prefix", ids(hits) == [1, 3],
          str(ids(hits)))

    print()
    print("8. version")
    hits = docs_search.run(engine, "version:2.10", DOCUMENTS)
    check("an exact version is found", ids(hits) == [1], str(ids(hits)))
    hits = docs_search.run(engine, "version>=2.9", DOCUMENTS)
    check("a version comparison is ordered numerically", ids(hits) == [1, 2],
          str(ids(hits)))
    hits = docs_search.run(engine, "version<2.0", DOCUMENTS)
    check("a less-than comparison works", ids(hits) == [3, 4], str(ids(hits)))

    print()
    print("9. history")
    hits = docs_search.run(engine, "history:reindexed", DOCUMENTS)
    check("history search finds the recorded change", ids(hits) == [1],
          str(ids(hits)))
    hits = docs_search.run(engine, "changed:2024-08", DOCUMENTS)
    check("history search by month finds it too", ids(hits) == [1],
          str(ids(hits)))

    print()
    print("10. archive")
    hits = docs_search.run(engine, "archive:yes", DOCUMENTS)
    check("archive search finds the archived documents", ids(hits) == [2, 4],
          str(ids(hits)))
    hits = docs_search.run(engine, "archive:no", DOCUMENTS)
    check("and the live ones", ids(hits) == [1, 3], str(ids(hits)))

    print()
    print("11. metadata")
    hits = docs_search.run(engine, "hash:ab12cd", DOCUMENTS)
    check("metadata search finds a hash by prefix", ids(hits) == [1],
          str(ids(hits)))
    hits = docs_search.run(engine, "size>0", DOCUMENTS)
    check("a numeric metadata comparison works", len(ids(hits)) == len(DOCUMENTS),
          str(ids(hits)))
    hits = docs_search.run(engine, "author=nowak", DOCUMENTS)
    check("a metadata key outside the known set is still searchable",
          ids(hits) == [2], str(ids(hits)))

    print()
    print("12. fuzzy")
    # The misspelling has to be of a word the corpus actually contains: these
    # documents say "copy", not "receive", so misspelling "receive" would test
    # the fixture rather than the search.
    hits = docs_search.run(engine, "~copys", DOCUMENTS)
    check("a misspelling still finds the document", 2 in ids(hits),
          str(ids(hits)))
    hits = docs_search.run(engine, "fuzzy:instal", DOCUMENTS)
    check("fuzzy finds the manual", 3 in ids(hits), str(ids(hits)))
    distance = docs_search.edit_distance("recieve", "receive")
    check("the edit distance is measured", distance == 2, str(distance))
    check("a prefix of a longer word is matched",
          "installer" in
          [candidate for candidate in
           docs_search.fuzzy_terms(engine, "instal")])
    check("a short word is allowed no edits",
          docs_search.fuzzy_allowance("cat") == 0)

    print()
    print("13. semantic")
    hits = docs_search.run(engine, "~copies kept for the long term", DOCUMENTS)
    check("a semantic search reaches the archive policy", 2 in ids(hits),
          str(ids(hits)))
    check("a semantic hit carries its score",
          all(isinstance(hit["score"], float) for hit in hits))
    related = docs_search.semantic_related(engine, "archive")
    check("the corpus suggests related terms", isinstance(related, list))

    print()
    print("the query language")
    spec = docs_search.parse_query("tag:policy type:guide")
    check("two filters parse", len(spec["filters"]) == 2, str(spec["filters"]))
    spec = docs_search.parse_query("date:2024-01-01..2024-12-31")
    check("a range parses", "date" in spec["ranges"], str(spec["ranges"]))
    spec = docs_search.parse_query("backup copy")
    check("plain words stay plain terms",
          spec["terms"] == ["backup", "copy"] and spec["kind"] == "content")
    spec = docs_search.parse_query("~roughly this meaning")
    check("a leading tilde selects semantic", spec["kind"] == "semantic")
    spec = docs_search.parse_query("heading:install")
    check("a named kind becomes the query's kind", spec["kind"] == "heading")
    check("every kind is reachable",
          set(docs_search.SEARCH_KIND_KEYS) ==
          set(docs_search._RUNNERS))
    check("the thirteen kinds are all declared",
          len(docs_search.SEARCH_KINDS) == 13,
          str(len(docs_search.SEARCH_KINDS)))

    print()
    print("combined filters")
    hits = docs_search.run(engine, "copies tag:policy", DOCUMENTS)
    check("a term plus a filter narrows the result",
          all(hit["doc_id"] in (1, 2) for hit in hits), str(ids(hits)))

    print()
    print("the structure reader")
    outline = docs_search.document_outline(GUIDE["body"])
    headings = [item["title"] for item in outline]
    check("the markdown headings are read",
          "Taking a backup" in headings, str(headings))
    outline = docs_search.document_outline(API["body"])
    check("a banner title is read as a section",
          any(item["kind"] == "section" for item in outline),
          str([(i["kind"], i["title"]) for i in outline]))
    outline = docs_search.document_outline(MANUAL["body"])
    check("an identifier heading is read",
          any("Verifying" in item["title"] for item in outline),
          str([i["title"] for i in outline]))

    # The connection is closed before the temporary directory is removed, so
    # the cleanup is a real test of the engine rather than of the OS holding a
    # handle open.
    engine.conn.close()

print()
print("%d failure(s)" % len(failures))
sys.exit(1 if failures else 0)
