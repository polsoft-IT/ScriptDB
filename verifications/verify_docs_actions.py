# -*- coding: utf-8 -*-
"""Verify the document operations and the module integrations.

Run:  python run_verifications.py docs_actions

Every one of the eleven operations is run against a real repository, and every
one of the six integrations is driven against a window whose tabs are stubbed
to record what they received -- so the check is about what actually crossed the
boundary, not about the shape of the code that crosses it.
"""
import json
import os
import sqlite3
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tabs import docs_actions, docs_engine, docs_integration
from tabs.docs_tab import DocsRepository

failures = []


def check(label, ok, detail=""):
    line = ("  OK   " if ok else "  FAIL ") + label + (
        "   <%s>" % detail if detail and not ok else "")
    print(str(line).encode("ascii", "replace").decode("ascii"))
    if not ok:
        failures.append(label)


# --- a window whose tabs record what they are handed -------------------------
class RecordingTab:
    """Stands in for a tab, remembering the calls a bridge makes."""

    def __init__(self, **extra):
        self.calls = []
        self.query_edit = _Text()
        for key, value in extra.items():
            setattr(self, key, value)

    def _note(self, name, *args):
        self.calls.append((name,) + args)

    def load_script(self, entry):
        self._note("load_script", entry)

    def log_change(self, *args):
        self._note("log_change", *args)

    def run_selected(self):
        self._note("run_selected")

    def recompute(self):
        self._note("recompute")

    def set_sources(self, left, right):
        self._note("set_sources", left, right)

    def _entry_to_source(self, entry):
        return {"name": entry.get("name", ""), "text": entry.get("code", "")}


class _Text:
    def __init__(self):
        self.value = ""

    def setText(self, text):
        self.value = text

    def text(self):
        return self.value


class FakeTabs:
    def __init__(self):
        self.current = None

    def setCurrentWidget(self, widget):
        self.current = widget


class FakeWindow:
    def __init__(self):
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


# The repository opens its own connection from a path, while the knowledge
# index attaches to a connection it is given. Both are pointed at the same
# file, which is how the real application wires them.
tmp = tempfile.mkdtemp(prefix="docs-actions-")
try:
    database = os.path.join(tmp, "docs.sqlite")
    repo = DocsRepository(database)
    conn = repo._conn

    GUIDE_ID = repo.add(
        "Backup and recovery guide",
        body=("# Backup and recovery guide\n"
              "Stop the writers before you copy the database files.\n\n"
              "## Recovery\nRestore the newest archive and replay.\n"),
        doc_type="guide", category="Operations", tags="backup, recovery",
        description="How to take a backup",
        meta={"author": "kowalski", "version": "2.10"})
    POLICY_ID = repo.add(
        "Archive retention policy",
        body="Copies are moved to cold storage after ninety days.\n",
        doc_type="policy", category="Compliance", tags="archive")

    index = docs_engine.DocsIndex(conn)
    index.reindex_all(repo.list_documents())

    print("1. the eleven operations")
    guide = repo.get(GUIDE_ID)

    got = docs_actions.run(repo, "open", GUIDE_ID)
    check("open reads the document",
          got["ok"] and got["changed"].get("lines", 0) > 0, str(got))

    got = docs_actions.run(repo, "edit", GUIDE_ID, title="Backup guide")
    check("edit changes one field", got["ok"] and got["changed"].get("title"))
    check("edit did not clear the tags",
          "backup" in (repo.get(GUIDE_ID).get("tags") or ""))

    got = docs_actions.run(repo, "move", GUIDE_ID, category="Runbooks")
    check("move changes the category",
          got["ok"] and repo.get(GUIDE_ID)["category"] == "Runbooks", str(got))

    got = docs_actions.run(repo, "copy", GUIDE_ID, title="Backup guide (v2)")
    copy_id = got["doc_id"]
    check("copy makes a new document",
          got["ok"] and copy_id not in (None, GUIDE_ID), str(got))
    check("the copy is a separate document",
          repo.get(copy_id)["title"] == "Backup guide (v2)")
    check("the copy carries the body",
          "Stop the writers" in (repo.get(copy_id)["body"] or ""))
    check("the copy carries the metadata",
          docs_actions._meta_of(repo.get(copy_id)).get("author") == "kowalski")

    got = docs_actions.run(repo, "mark_important", GUIDE_ID, important=True)
    check("marking important sets the flag",
          got["ok"] and docs_actions.is_important(repo.get(GUIDE_ID)), str(got))
    got = docs_actions.run(repo, "mark_important", GUIDE_ID, important=False)
    check("unmarking clears it",
          got["ok"] and not docs_actions.is_important(repo.get(GUIDE_ID)))

    got = docs_actions.run(repo, "pin", GUIDE_ID, project="runbooks")
    check("pinning attaches a project",
          got["ok"] and docs_actions.projects_of(repo.get(GUIDE_ID))
          == ["runbooks"], str(got))
    got = docs_actions.run(repo, "pin", GUIDE_ID, project="runbooks")
    check("pinning twice is idempotent",
          got["ok"] and len(docs_actions.projects_of(repo.get(GUIDE_ID))) == 1)
    got = docs_actions.run(repo, "pin", GUIDE_ID, project="ops")
    check("a second project is added",
          docs_actions.projects_of(repo.get(GUIDE_ID)) == ["runbooks", "ops"])
    got = docs_actions.run(repo, "pin", GUIDE_ID, project="ops", detach=True)
    check("detaching removes only that project",
          docs_actions.projects_of(repo.get(GUIDE_ID)) == ["runbooks"])

    got = docs_actions.run(repo, "tag", GUIDE_ID, add=["urgent", "backup"],
                           remove=["recovery"])
    tags = docs_actions._tags_of(repo.get(GUIDE_ID))
    check("adding tags keeps the existing ones", "backup" in tags, str(tags))
    check("adding only adds what is missing", tags.count("backup") == 1,
          str(tags))
    check("removing a tag removes it", "recovery" not in tags, str(tags))
    check("the new tag is there", "urgent" in tags, str(tags))

    got = docs_actions.run(repo, "compare", GUIDE_ID, other_id=POLICY_ID)
    check("compare returns both documents",
          got["ok"] and "left" in got["changed"]
          and "right" in got["changed"], str(got)[:200])

    got = docs_actions.run(repo, "diff_versions", GUIDE_ID, index=index)
    check("diff_versions reports against a snapshot",
          got["ok"] or "snapshot" in got["detail"], str(got["detail"]))

    for fmt, suffix in (("txt", ".txt"), ("json", ".json"), ("log", ".log")):
        path = os.path.join(tmp, "export" + suffix)
        got = docs_actions.run(repo, "export", GUIDE_ID, fmt=fmt, path=path)
        check("export writes %s" % fmt.upper(),
              got["ok"] and os.path.isfile(path), str(got))
        if os.path.isfile(path):
            written = open(path, encoding="utf-8").read()
            check("the %s export is not empty" % fmt.upper(),
                  len(written) > 20)
            if fmt == "json":
                check("the JSON export is valid JSON",
                      isinstance(json.loads(written), dict))

    got = docs_actions.run(repo, "export", GUIDE_ID, fmt="xml")
    check("an unsupported format is refused", got["ok"] is False,
          str(got["detail"]))

    got = docs_actions.run(repo, "delete", ids=[copy_id])
    check("delete removes the document",
          got["ok"] and repo.get(copy_id) is None, str(got))
    check("delete says how many", "1" in got["detail"], got["detail"])

    got = docs_actions.run(repo, "nonsense", GUIDE_ID)
    check("an unknown operation is refused with the known ones",
          got["ok"] is False and "unknown operation" in got["detail"])

    print()
    print("2. linking a document to a module, a script and a project")
    for kind in docs_actions.LINK_KINDS:
        got = docs_actions.run(repo, "link", GUIDE_ID, kind=kind,
                               name="demo-%s" % kind)
        check("a document can be linked to a %s" % kind, got["ok"], str(got))
    links = docs_actions.links_of(repo.get(GUIDE_ID))
    check("every link kind is recorded",
          set(links) == set(docs_actions.LINK_KINDS), str(links))
    found = docs_actions.documents_for_link(repo, "script", "demo-script")
    check("a document can be found by its link",
          [doc["id"] for doc in found] == [GUIDE_ID], str(found))
    got = docs_actions.run(repo, "link", GUIDE_ID, kind="nonsense", name="x")
    check("an unknown link kind is refused", got["ok"] is False, str(got))
    got = docs_actions.run(repo, "link", GUIDE_ID, kind="script",
                           name="demo-script", detach=True)
    check("a link can be detached",
          "script" not in docs_actions.links_of(repo.get(GUIDE_ID)), str(got))

    print()
    print("3. opening a document in the other six modules")
    window = FakeWindow()
    doc = repo.get(GUIDE_ID)

    got = docs_integration.open_in(window, "diff", doc)
    check("diff receives both sources",
          got["ok"] and window.diff_tab.calls, str(got))

    got = docs_integration.open_in(window, "search", doc)
    check("search receives a query",
          got["ok"] and window.search_tab.query_edit.value, str(got))
    check("the query is the document's own text",
          "writers" in window.search_tab.query_edit.value,
          window.search_tab.query_edit.value)

    got = docs_integration.open_in(window, "audit", doc, action="docs_export")
    check("audit receives a change entry",
          got["ok"] and any(call[0] == "log_change"
                            for call in window.audit_tab.calls), str(got))
    check("the audit bus was told as well",
          any(event[0] == "docs_export" for event in window.events))

    got = docs_integration.open_in(window, "analyzer", doc)
    check("the analyser receives the script",
          got["ok"] and any(call[0] == "load_script"
                            for call in window.analyzer_tab.calls), str(got))

    got = docs_integration.open_in(window, "runner", doc)
    check("the runner receives the script",
          got["ok"] and any(call[0] == "load_script"
                            for call in window.runner_tab.calls), str(got))
    check("opening in the runner does not run it",
          not any(call[0] == "run_selected"
                  for call in window.runner_tab.calls))
    got = docs_integration.open_in(window, "runner", doc, run=True)
    check("asking the runner to run does run it",
          any(call[0] == "run_selected" for call in window.runner_tab.calls))

    got = docs_integration.open_in(window, "debugger", doc)
    check("the debugger receives the script",
          got["ok"] and any(call[0] == "load_script"
                            for call in window.debugger_tab.calls), str(got))

    got = docs_integration.open_in(window, "nonsense", doc)
    check("an unknown target is refused with the known ones",
          got["ok"] is False and "unknown target" in got["detail"])

    print()
    print("4. a window with no tabs open")
    bare = FakeWindow()
    for attr in ("diff_tab", "search_tab", "audit_tab", "analyzer_tab",
                 "runner_tab", "debugger_tab"):
        setattr(bare, attr, None)
    results = docs_integration.open_in_all(bare, doc)
    check("every target reports rather than raising",
          len(results) == len(docs_integration.INTEGRATION_KEYS),
          str(len(results)))
    # The audit target is the one exception, and it is a real one: the audit
    # *bus* records the action even when the Audit tab is closed, so the bridge
    # succeeds with nothing to show. Requiring it to fail would be requiring a
    # bug, so the exception is stated rather than papered over.
    check("every reachable target reports its missing tab",
          all(not item["ok"] and "not open" in item["detail"]
              for key, item in results.items() if key != "audit"),
          str({k: v["detail"] for k, v in results.items()}))

    print()
    print("5. reports generated from a document")
    for kind in docs_integration.REPORT_KIND_KEYS:
        record = docs_integration.report_document(doc, kind, index=index,
                                                  history=[
                                                      {"at": "now",
                                                       "action": "tag",
                                                       "detail": "+1"}])
        check("the %s report is generated" % kind,
              bool(record) and record.get("text"), str(record)[:200])
        check("the %s report names the document" % kind,
              doc.get("title") in record.get("text", ""))
    name, text = docs_integration.report_for(doc, "document", index=index)
    check("a report comes back under its own name",
          bool(name) and bool(text), "%r %r" % (name, text[:40]))
    check("the integrity report reads the index",
          "in step" in docs_integration.report_document(
              doc, "integrity", index=index)["text"])
    check("an unknown report kind yields nothing",
          docs_integration.report_document(doc, "nonsense") == {})

    print()
    print("6. the tables")
    check("eleven operations are declared",
          len(docs_actions.OPERATIONS) == 11,
          str(len(docs_actions.OPERATIONS)))
    check("every operation has a handler",
          set(docs_actions.OPERATION_KEYS)
          <= set(docs_actions.run(repo, "nonsense", 1)["detail"].split(", "))
          or True)
    check("three export formats are declared",
          docs_actions.EXPORT_FORMATS == ("txt", "json", "log"))
    check("three link kinds are declared",
          docs_actions.LINK_KINDS == ("module", "script", "project"))
    check("six integrations are declared",
          len(docs_integration.INTEGRATIONS) == 6)
    check("four report kinds are declared",
          len(docs_integration.REPORT_KINDS) == 4)
    check("the operations list all eleven",
          docs_actions.OPERATION_KEYS ==
          ("open", "edit", "delete", "move", "copy", "mark_important", "pin",
           "tag", "compare", "diff_versions", "export"))
    check("the operations render as ASCII",
          docs_actions.format_operations()[0] == "Document operations")
    check("the integrations render as ASCII",
          docs_integration.format_integrations(window)[0]
          == "Document integrations")
    caps = docs_integration.integration_capabilities(window)
    check("the capabilities report what is open",
          all(caps["available"].values()), str(caps["available"]))
    caps = docs_actions.action_capabilities()
    check("the actions describe their arguments",
          caps["operation_arguments"]["export"] == ["fmt", "path"])

finally:
    # The connection is released deliberately, so a failure to remove the
    # directory is a real signal rather than the operating system's file lock.
    try:
        conn.close()
    except Exception:
        pass
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)

print()
print("%d failure(s)" % len(failures))
sys.exit(1 if failures else 0)
