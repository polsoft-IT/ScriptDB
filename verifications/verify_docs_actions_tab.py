# -*- coding: utf-8 -*-
"""Verify the document operations and integrations through the Docs tab.

Run:  python run_verifications.py docs_actions_tab

`verify_docs_actions.py` proves the head-less modules. This proves the
*integration*: that the tab's own methods reach them, that an operation
refreshes what it changed, and that a generated report lands in the repository
as a document -- which is what makes it visible to the Reports tab.
"""
import os
import shutil
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5 import QtCore, QtWidgets

from tabs import docs_actions, docs_integration
from tabs.docs_tab import DocsTab

failures = []


def check(label, ok, detail=""):
    line = ("  OK   " if ok else "  FAIL ") + label + (
        "   <%s>" % detail if detail and not ok else "")
    print(str(line).encode("ascii", "replace").decode("ascii"))
    if not ok:
        failures.append(label)


class RecordingTab:
    """Stands in for a tab, remembering what a bridge handed it."""

    def __init__(self):
        self.calls = []
        self.query_edit = _Text()

    def load_script(self, entry):
        self.calls.append(("load_script", entry))

    def log_change(self, *args):
        self.calls.append(("log_change",) + args)

    def run_selected(self):
        self.calls.append(("run_selected",))

    def recompute(self):
        self.calls.append(("recompute",))

    def set_sources(self, left, right):
        self.calls.append(("set_sources", left, right))

    def _entry_to_source(self, entry):
        return {"name": entry.get("name", ""), "text": entry.get("code", "")}


class _Text:
    def __init__(self):
        self.value = ""

    def setText(self, text):
        self.value = text


class FakeTabs:
    def __init__(self):
        self.current = None

    def setCurrentWidget(self, widget):
        self.current = widget


class FakeWindow(QtWidgets.QWidget):
    def __init__(self, base):
        super().__init__()
        self.base_dir = base
        self.tabs = FakeTabs()
        self.events = []
        self.diff_tab = RecordingTab()
        self.search_tab = RecordingTab()
        self.audit_tab = RecordingTab()
        self.analyzer_tab = RecordingTab()
        self.runner_tab = RecordingTab()
        self.debugger_tab = RecordingTab()

    def notify_audit(self, event, **payload):
        self.events.append((event, payload))
        return 1

    def t(self, key, fallback=None, **kwargs):
        text = fallback or key
        if kwargs:
            try:
                return text.format(**kwargs)
            except (KeyError, IndexError, ValueError):
                return text
        return text


tmp = tempfile.mkdtemp(prefix="docs-tab-verify-")
try:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = FakeWindow(tmp)
    tab = DocsTab(window)

    for title, body, kind, category, tags, meta in (
            ("Backup and recovery guide",
             "# Backup and recovery guide\n"
             "Stop the writers before you copy the database files.\n\n"
             "## Recovery\nRestore the newest archive and replay.\n",
             "guide", "Operations", "backup, recovery",
             {"author": "kowalski", "version": "2.10"}),
            ("Archive retention policy",
             "Copies are moved to cold storage after ninety days.\n",
             "policy", "Compliance", "archive", {})):
        tab.repo.add(title, body=body, doc_type=kind, category=category,
                     tags=tags, meta=meta)
    tab.reindex_documents(quiet=True)
    tab.refresh()

    def select_document(wanted):
        """Select one document by its id, the way clicking a row does.

        Selecting "the first row" would be a different test every time an
        operation re-sorts the list, so the selection is made by identity: a
        user clicks a document, not a position in a list.
        """
        for index in range(tab.tree.topLevelItemCount()):
            item = tab.tree.topLevelItem(index)
            if item.data(0, QtCore.Qt.UserRole) == wanted:
                tab.tree.setCurrentItem(item)
                return True
        return False

    guide = [item for item in tab.repo.list_documents()
             if item.get("doc_type") == "guide"][0]
    doc_id = guide.get("id")
    check("the tree lists the documents", tab.tree.topLevelItemCount() >= 2,
          str(tab.tree.topLevelItemCount()))
    check("a document can be selected", select_document(doc_id))
    doc = tab._selected_document()
    check("the selection is the document asked for",
          doc is not None and doc.get("id") == doc_id, str(doc)[:120])

    print()
    print("1. the operations, through the tab")
    got = tab.document_action("open")
    check("open runs from the tab", got["ok"], str(got))

    got = tab.edit_document_field("title", "Backup guide (edited)")
    check("edit runs from the tab", got["ok"], str(got))
    check("the edit reached the repository",
          tab.repo.get(doc_id)["title"] == "Backup guide (edited)")

    got = tab.move_document_to("Runbooks")
    check("move runs from the tab",
          got["ok"] and tab.repo.get(doc_id)["category"] == "Runbooks")

    got = tab.copy_document_as("Backup guide copy")
    copy_id = got.get("doc_id")
    check("copy runs from the tab and makes a document",
          got["ok"] and copy_id not in (None, doc_id), str(got))

    got = tab.toggle_important()
    check("the important flag toggles", got["ok"], str(got))

    got = tab.pin_document_to("runbooks")
    check("pinning runs from the tab",
          got["ok"] and docs_actions.projects_of(tab.repo.get(doc_id))
          == ["runbooks"], str(got))

    got = tab.tag_document(add=["urgent"], remove=["recovery"])
    tags = docs_actions._tags_of(tab.repo.get(doc_id))
    check("tagging runs from the tab", got["ok"] and "urgent" in tags,
          str(tags))

    got = tab.link_document_to("script", "deploy.sh")
    check("linking runs from the tab",
          got["ok"] and docs_actions.links_of(tab.repo.get(doc_id))
          .get("script") == ["deploy.sh"], str(got))
    check("the link table is readable from the tab",
          tab.document_link_table().get("script") == ["deploy.sh"])

    for fmt in docs_actions.EXPORT_FORMATS:
        path = os.path.join(tmp, "doc." + fmt)
        got = tab.export_document_as(fmt, path)
        check("export %s runs from the tab" % fmt.upper(),
              got["ok"] and os.path.isfile(path), str(got))

    print()
    print("2. operations refresh what they changed")
    before = tab.tree.topLevelItemCount()
    tab.run_document_action("move", doc_id=doc_id, category="Moved")
    check("the tree is still populated after an operation",
          tab.tree.topLevelItemCount() > 0)
    check("the audit was told about the operation",
          any(event[0].startswith("docs_") for event in window.events),
          str(window.events[-3:]))

    print()
    print("3. the six integrations, through the tab")
    for target in docs_integration.INTEGRATION_KEYS:
        if target == "diff":
            got = tab.open_in_diff()
        elif target == "audit":
            got = tab.open_in_audit()
        else:
            got = tab.open_in_module(target)
        check("the tab can open a document in %s" % target,
              got["ok"] or "not open" not in got.get("detail", ""),
              str(got))

    check("the diff tab received a source",
          any(call[0] in ("set_sources", "recompute")
              for call in window.diff_tab.calls), str(window.diff_tab.calls))
    check("the search tab received a query",
          "writers" in window.search_tab.query_edit.value,
          window.search_tab.query_edit.value)
    check("the analyser received the script",
          any(call[0] == "load_script" for call in window.analyzer_tab.calls))
    check("the runner received the script",
          any(call[0] == "load_script" for call in window.runner_tab.calls))
    check("the debugger received the script",
          any(call[0] == "load_script" for call in window.debugger_tab.calls))
    check("the audit tab received a change entry",
          any(call[0] == "log_change" for call in window.audit_tab.calls))

    print()
    print("4. reports from a document")
    for kind in docs_integration.REPORT_KIND_KEYS:
        record = tab.generate_report(kind, store=True)
        check("the %s report is generated and stored" % kind,
              bool(record) and record.get("doc_id") is not None,
              str(record)[:200])
    stored = [item for item in tab.repo.list_documents()
              if item.get("doc_type") == "report"]
    check("the reports are documents in the repository",
          len(stored) >= len(docs_integration.REPORT_KIND_KEYS),
          str(len(stored)))
    check("a stored report is findable by its kind",
          any("integrity" in (item.get("tags") or "") for item in stored))
    text = tab.report_from_document("links")
    check("a report can be read without storing it",
          isinstance(text, str) and len(text) > 20, text[:80])
    unknown = tab.generate_report("nonsense", store=False)
    check("an unknown report kind is refused", unknown is None)

    print()
    print("5. a tab with nothing selected is safe")
    tab.tree.setCurrentItem(None) if hasattr(tab.tree, "setCurrentItem") else None
    got = tab.run_document_action("open", doc_id=None)
    check("an operation with no selection fails cleanly",
          got["ok"] is False and "no document selected" in got["detail"],
          str(got))
    check("an integration with no selection fails cleanly",
          tab.open_in_module("search")["ok"] is False)
    check("a report with no selection yields nothing",
          tab.generate_report("document", store=False) is None)
finally:
    # The repository holds the database open; releasing it keeps the cleanup a
    # test of the code rather than of the operating system's file lock.
    try:
        tab.repo.close()
    except Exception:
        pass
    shutil.rmtree(tmp, ignore_errors=True)

print()
print("%d failure(s)" % len(failures))
sys.exit(1 if failures else 0)
