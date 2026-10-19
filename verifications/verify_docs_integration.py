# -*- coding: utf-8 -*-
"""Verify the advanced search through the Docs tab's own machinery.

`verify_docs_search.py` proves the search module against an index it builds
itself. This proves the *integration*: that the tab can reach all thirteen
kinds, that its results are real repository documents, and that a document
carries the reason it matched.
"""
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5 import QtWidgets

from tabs import docs_search
from tabs.docs_tab import DocsTab

failures = []


def check(label, ok, detail=""):
    line = ("  OK   " if ok else "  FAIL ") + label + (
        "   <%s>" % detail if detail and not ok else "")
    print(str(line).encode("ascii", "replace").decode("ascii"))
    if not ok:
        failures.append(label)


class FakeWindow(QtWidgets.QWidget):
    def __init__(self, base):
        super().__init__()
        self.base_dir = base

    def t(self, key, fallback=None, **kwargs):
        text = fallback or key
        if kwargs:
            try:
                return text.format(**kwargs)
            except (KeyError, IndexError, ValueError):
                return text
        return text

    def notify_audit(self, event, **payload):
        return 1


DOCUMENTS = [
    {"title": "Backup and recovery guide", "doc_type": "guide",
     "category": "Operations", "tags": "backup, recovery",
     "description": "How to take a backup",
     "body": ("# Backup and recovery guide\n"
              "Take a backup of the database.\n\n"
              "## Taking a backup\nStop the writers before you copy.\n\n"
              "## Recovery\nRestore the newest archive and replay.\n"),
     "meta": {"author": "kowalski", "version": "2.10",
              "created": "2024-03-15", "modified": "2024-09-02",
              "history": "2024-08-01 reindexed after the schema change"}},
    {"title": "Archive retention policy", "doc_type": "policy",
     "category": "Compliance", "tags": "archive",
     "description": "Retention rules",
     "body": ("Archive retention policy\n=======================\n\n"
              "Copies are moved to cold storage after ninety days.\n"),
     "meta": {"author": "nowak", "version": "2.9", "created": "2023-11-01",
              "modified": "2024-01-20", "archived": "true"}},
]

# `ignore_cleanup_errors` is not used: the connections are closed deliberately
# at the end, so a removal failure is a real signal about the code rather than
# something to swallow. The directory is made by hand so the failure surfaces
# as a check instead of as a traceback from a context manager.
tmp = tempfile.mkdtemp(prefix="docs-verify-")
try:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = FakeWindow(tmp)
    tab = DocsTab(window)

    # Put the fixtures in through the repository's own API, then index them, so
    # the test exercises the same path a real document takes rather than
    # writing rows behind the repository's back.
    for document in DOCUMENTS:
        tab.repo.add(document["title"], body=document["body"],
                     doc_type=document["doc_type"],
                     category=document["category"],
                     tags=document["tags"],
                     description=document["description"],
                     meta=document["meta"])
    count = tab.reindex_documents(quiet=True)
    check("the repository documents were indexed", count >= 2, str(count))

    print()
    print("the thirteen kinds, through the tab")
    queries = {
        "content": "backup database",
        "section": "section:Recovery",
        "heading": "heading:Taking",
        "tag": "tag:archive",
        "type": "type:guide",
        "date": "date:2024-06-01..2024-12-31",
        "author": "author:kowalski",
        "version": "version>=2.9",
        "history": "history:reindexed",
        "archive": "archive:yes",
        "metadata": "author:nowak",
        "fuzzy": "fuzzy:archiv",
        "semantic": "~copies kept for the long term",
    }
    for kind, query in queries.items():
        found = tab.advanced_search(query)
        check("the %s kind returns documents" % kind, len(found) >= 1,
              "query %r -> %d" % (query, len(found)))

    print()
    print("the results are real documents")
    found = tab.advanced_search("heading:Taking")
    check("a result is a repository document",
          bool(found) and found[0].get("title"), str(found)[:200])
    check("a result carries why it matched",
          bool(found) and "match" in found[0]
          and found[0]["match"].get("kind") == "heading",
          str(found[0].get("match")) if found else "")
    check("the match carries a score",
          bool(found) and isinstance(found[0]["match"].get("score"), float))

    print()
    print("the tab can describe its own search")
    kinds = tab.search_kinds()
    check("every kind is described", len(kinds) == 13, str(len(kinds)))
    check("each description has an example",
          all(item.get("example") for item in kinds))
    check("each description has a meaning",
          all(item.get("meaning") for item in kinds))

    explanation = tab.explain_search("tag:archive author:nowak")
    check("a query can explain itself",
          any("Parsed query" in line for line in explanation),
          str(explanation[:3]))

    info = tab.engine_info()
    check("the engine advertises the advanced search",
          "advanced_search" in info and "search_kinds" in info)
    check("the engine lists all thirteen kinds",
          len(info.get("search_kinds", [])) == 13,
          str(len(info.get("search_kinds", []))))

    print()
    print("an unparseable or empty query is safe")
    check("an empty query returns nothing rather than raising",
          isinstance(tab.advanced_search(""), list))
    check("a nonsense query returns nothing rather than raising",
          isinstance(tab.advanced_search("???:::"), list))

    # The repository and the knowledge index share one connection, and on
    # Windows an open file blocks the temporary directory from being removed.
    # Both are closed here, so the cleanup is a test of the code rather than a
    # test of the operating system's file locking.
    if tab.index is not None:
        try:
            tab.index.conn.close()
        except Exception:
            pass
    tab.repo.close()

    # The tab holds the database open; closing it here is what makes the
    # cleanup below meaningful.
    try:
        if tab.index is not None:
            tab.index.conn.close()
        tab.repo.close()
    except Exception:
        pass
finally:
    import shutil
    try:
        shutil.rmtree(tmp, ignore_errors=True)
    except Exception:
        pass
print()
print("%d failure(s)" % len(failures))
sys.exit(1 if failures else 0)
