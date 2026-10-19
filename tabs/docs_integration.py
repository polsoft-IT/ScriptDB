# -*- coding: utf-8 -*-
"""Opening a document in the other modules, and reporting from one.

`tabs/docs_actions.py` owns what can be done *to* a document. This owns where a
document can be *sent*, and it is a separate module for the same reason the
sandbox's bridges are: the Docs tab should not have to know how the Diff tab
wants its input, and the Diff tab should not have to know a document exists.

    INTEGRATIONS    the six targets, what each receives, and what it gives back
    open_in         dispatch one target by name
    report          generate a report *from* a document, for the Reports tab

Every bridge obeys the rules the rest of the terminal follows:

    1. the target is reached through the window (`getattr(window, "x_tab")`),
       never imported, so a closed tab cannot break the Docs tab;
    2. the result is plain data -- a status dict, never a widget -- so the
       caller decides what to show;
    3. nothing raises: an unreachable target returns ``ok: False`` and says
       which tab was missing, because "the Diff tab is not open" is a fact the
       user needs, not an exception they have to read.
"""

import datetime as dt
import os

# ---------------------------------------------------------------------------
# The six targets
# ---------------------------------------------------------------------------
# (key, label key, fallback, what the target receives, what it gives back)
INTEGRATIONS = (
    ("diff", "docs_int_diff", "Diff", "the document as the right-hand source, "
     "and its previous version as the left when one exists",
     "a side-by-side comparison"),
    ("search", "docs_int_search", "Search", "the document's first meaningful "
     "line as the query",
     "every script and document that contains it"),
    ("audit", "docs_int_audit", "Audit", "a change-log entry naming the "
     "document and what was done",
     "the action recorded on the audit timeline"),
    ("analyzer", "docs_int_analyzer", "Script analyser", "the document's body "
     "as the subject to analyse",
     "the analyser's findings for the text"),
    ("runner", "docs_int_runner", "Script runner", "the document's body, as a "
     "script the runner may execute",
     "a run, with its output"),
    ("debugger", "docs_int_debugger", "Debugger", "the document's body as the "
     "script to debug",
     "a session over it, with the document's lines as frames"),
)

INTEGRATION_KEYS = tuple(key for key, _lk, _f, _r, _g in INTEGRATIONS)
INTEGRATION_LABELS = {key: fallback for key, _lk, fallback, _r, _g
                      in INTEGRATIONS}
INTEGRATION_RECEIVES = {key: receives for key, _lk, _f, receives, _g
                        in INTEGRATIONS}
INTEGRATION_GIVES = {key: gives for key, _lk, _f, _r, gives in INTEGRATIONS}

# The window attribute each target lives under, in the order they are tried.
# Two names are listed for the tabs whose attribute has varied (`diff_tab` is
# the real one; `compare_tab` is accepted so an older window still works).
TARGET_ATTRS = {
    "diff": ("diff_tab", "compare_tab"),
    "search": ("search_tab",),
    "audit": ("audit_tab",),
    "analyzer": ("analyzer_tab", "script_analyzer_tab"),
    "runner": ("runner_tab", "run_tab"),
    "debugger": ("debugger_tab", "debug_tab"),
}

# ---------------------------------------------------------------------------
# The architecture standard
# ---------------------------------------------------------------------------
# The tab's standard, stated once. The first eight keys are the same words
# every other tab uses, so an "about" page can print the whole terminal's
# standards in one grid without a translation layer between them; the last
# three name the systems a document plugs into.
ARCHITECTURE = (
    ("ascii", "docs_arch_ascii", "ASCII-only",
     "every report, log line and identifier stays within printable ASCII"),
    ("hot_pluggable", "docs_arch_hot", "Hot-pluggable",
     "the six targets and the four report kinds are table entries, not "
     "branches, so a new one is one tuple"),
    ("zero_dependency", "docs_arch_zero", "Zero-dependency",
     "the standard library and the repository's own SQLite file only"),
    ("cross_platform", "docs_arch_cross", "Cross-platform",
     "Windows, macOS and Linux separators, encodings and defaults are all "
     "handled"),
    ("sandbox_safe", "docs_arch_sandbox", "Sandbox-safe",
     "a bridge reaches a tab through the window and never imports or executes "
     "its code"),
    ("plugin_ready", "docs_arch_plugin", "Plugin-ready",
     "the bridge table and the report table are the extension points"),
    ("artefact_driven", "docs_arch_artefact", "Artefact-driven",
     "a document is its report: a report is generated from it, stored and "
     "re-read rather than held in memory"),
    ("api_cli_gui", "docs_arch_frontends", "API + CLI + GUI",
     "one document surface reachable from code, from a terminal and from the "
     "tab"),
    ("events", "docs_arch_events", "Event system integration",
     "every action is announced on the shared audit event bus "
     "(window.notify_audit) so the timeline stays complete"),
    ("snapshots", "docs_arch_snapshots", "Snapshot system integration",
     "the engine's point-in-time snapshots are what a document is diffed "
     "against and what an integrity report checks"),
    ("audit_reports", "docs_arch_audit_reports", "Audit and reports "
     "integration", "every bridge can record on the audit timeline, and every "
     "document can be rendered as a report the Reports tab indexes"),
)

ARCHITECTURE_KEYS = tuple(key for key, _lk, _f, _d in ARCHITECTURE)
ARCHITECTURE_LABELS = {key: fallback for key, _lk, fallback, _d
                       in ARCHITECTURE}
ARCHITECTURE_MEANINGS = {key: meaning for key, _lk, _f, meaning in ARCHITECTURE}

# ---------------------------------------------------------------------------
# Results and helpers
# ---------------------------------------------------------------------------
def bridge_result(target, ok, detail="", data=None):
    """One bridge's outcome: ``{ok, target, detail, data}``."""
    return {"ok": bool(ok), "target": target, "detail": detail,
            "data": data if data is not None else {}}


def target_tab(window, target):
    """The tab a target lives under, or None when it is not open."""
    if window is None:
        return None
    for name in TARGET_ATTRS.get(target, ()):
        tab = getattr(window, name, None)
        if tab is not None:
            return tab
    return None


def focus(window, tab):
    """Bring a tab to the front (best effort, never raises)."""
    try:
        tabs = getattr(window, "tabs", None)
        if tabs is not None and tab is not None:
            tabs.setCurrentWidget(tab)
        return True
    except Exception:
        return False


def _as_entry(doc, name=""):
    """A document as the (name, code, category) entry the other tabs take.

    The other tabs were written before documents existed, so they accept the
    script shape: a name, the code, and a category. Making one here -- in one
    place -- means the two conventions live side by side in a single function
    instead of at six call sites.
    """
    return {
        "id": None,
        "name": name or doc.get("title") or "document",
        "category": doc.get("doc_type") or "Text",
        "tags": doc.get("tags") or "",
        "desc": doc.get("description") or "",
        "code": doc.get("body") or "",
        "date": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _same_opening(text, title):
    """Whether a body's first line is the document's title restated.

    Compared by their *common prefix* rather than by equality or by one
    containing the other: a document titled `Backup guide` whose first line
    reads `Backup and recovery guide` has already agreed on `backup` by the
    time the two diverge, and that agreement is the whole signal. Requiring
    one to contain the other would miss this case, which is exactly what an
    edit to the title produces -- the ordinary way the two come apart.
    """
    left = str(text or "").strip().lower()
    right = str(title or "").strip().lower()
    if not left or not right:
        return False
    shared = 0
    for index, character in enumerate(left):
        if index >= len(right) or right[index] != character:
            break
        shared += 1
    # Four characters is enough to be the same opening (`back`); a shorter
    # agreement (`b`) is a coincidence between unrelated words.
    return shared >= 4


def query_for(doc):
    """A search query that identifies a document: a line of its own prose.

    The title is skipped deliberately. A document almost always repeats its
    title in its first line, so searching for that line would look for the
    one string guaranteed not to be distinctive. A document whose body *is*
    only its title has nothing else to search for, so the title is then the
    last resort rather than the first choice.
    """
    title = (doc.get("title") or "").strip()
    first = True
    for line in (doc.get("body") or "").splitlines():
        text = line.strip().strip("#=*- ").strip()
        if not text:
            continue
        if first:
            # The opening line is a title by convention, so it is passed over
            # when it repeats the document's own title. The comparison is a
            # prefix test rather than equality: a document titled `Backup
            # guide` whose first line reads `Backup and recovery guide` is the
            # same opening line, and an edit to the title must not turn that
            # line into the search query.
            first = False
            if _same_opening(text, title):
                continue
        if len(text) >= 8:
            return text[:60]
    return title[:60]


def open_in_diff(window, doc, previous=None):
    """Open a document in the Diff tab, against its previous version.

    The Diff tab's own `_entry_to_source` is used rather than a hand-built
    source dict: the shape of a diff source belongs to the Diff tab, and
    duplicating it here would be a second copy to keep in step.
    """
    tab = target_tab(window, "diff")
    if tab is None:
        return bridge_result("diff", False, "the Diff tab is not open")
    entry = _as_entry(doc)
    try:
        left = None
        if previous is not None:
            left = previous
        converter = getattr(tab, "_entry_to_source", None)
        if callable(converter):
            right = converter(entry)
            if left is not None:
                tab.set_sources(converter(left) if isinstance(left, dict)
                                else left, right)
            else:
                # No previous version: show the document against nothing, which
                # the Diff tab renders as an all-additions comparison.
                tab._right = right
                if hasattr(tab, "recompute"):
                    tab.recompute()
        else:
            return bridge_result("diff", False, "the Diff tab has no source "
                                                "converter")
        focus(window, tab)
        return bridge_result("diff", True,
                             "opened %r in the Diff tab"
                             % doc.get("title", ""),
                             {"compared_with_previous": left is not None})
    except Exception as exc:
        return bridge_result("diff", False, "diff failed: %s" % exc)


def open_in_search(window, doc, needle=None):
    """Search for a document's own text."""
    tab = target_tab(window, "search")
    query = needle or query_for(doc)
    if tab is None:
        return bridge_result("search", False, "the Search tab is not open",
                             {"query": query})
    try:
        entry = getattr(tab, "query_edit", None)
        if entry is not None:
            entry.setText(query)
        else:
            return bridge_result("search", False,
                                 "the Search tab has no query box",
                                 {"query": query})
        focus(window, tab)
        return bridge_result("search", True, "searching for %r" % query,
                             {"query": query})
    except Exception as exc:
        return bridge_result("search", False, "search failed: %s" % exc,
                             {"query": query})


def open_in_audit(window, doc, action="docs_open", detail=""):
    """Record what was done with a document on the audit timeline."""
    tab = target_tab(window, "audit")
    entry = {
        "where": doc.get("title", ""),
        "detail": detail or action,
        "group": "docs",
    }
    # The window's own notifier is preferred when it exists, because the audit
    # tab may not be open while the audit bus still is.
    notifier = getattr(window, "notify_audit", None)
    if callable(notifier):
        try:
            notifier(action, **entry)
        except Exception:
            pass
    if tab is None:
        return bridge_result("audit", bool(callable(notifier)),
                             "recorded on the audit bus"
                             if callable(notifier)
                             else "the Audit tab is not open")
    logger = getattr(tab, "log_change", None)
    if callable(logger):
        try:
            logger("docs", doc.get("title", ""), action,
                   detail or "manual")
        except Exception:
            pass
    focus(window, tab)
    return bridge_result("audit", True, "recorded %r on the audit timeline"
                         % action)


def open_in_analyzer(window, doc):
    """Hand a document's text to the Script analyser."""
    tab = target_tab(window, "analyzer")
    if tab is None:
        return bridge_result("analyzer", False,
                             "the Script analyser tab is not open")
    entry = _as_entry(doc)
    try:
        loader = getattr(tab, "load_script", None)
        if callable(loader):
            loader(entry)
            focus(window, tab)
            return bridge_result("analyzer", True,
                                 "analysing %r" % doc.get("title", ""))
        # No loader: fall back to the analyser's own entry point, which takes
        # the text directly.
        analyse = getattr(tab, "analyse_text", None)
        if callable(analyse):
            result = analyse(doc.get("body") or "")
            focus(window, tab)
            return bridge_result("analyzer", True,
                                 "analysed %r" % doc.get("title", ""),
                                 {"result": result})
        return bridge_result("analyzer", False,
                             "the analyser tab takes no script from here")
    except Exception as exc:
        return bridge_result("analyzer", False, "analysis failed: %s" % exc)


def open_in_runner(window, doc, run=False):
    """Put a document in the runner, optionally running it at once.

    `run=False` by default: a document is usually a description *of* a script
    rather than a script, so running it is something the user asks for rather
    than something opening a document does behind their back.
    """
    tab = target_tab(window, "runner")
    entry = _as_entry(doc)
    if tab is None:
        # The runner may be a window call rather than a tab.
        setter = getattr(window, "load_script", None)
        if callable(setter):
            try:
                setter(entry)
                return bridge_result("runner", True,
                                     "loaded %r into the runner"
                                     % doc.get("title", ""))
            except Exception as exc:
                return bridge_result("runner", False, "runner failed: %s" % exc)
        return bridge_result("runner", False, "the runner tab is not open")
    try:
        loader = getattr(tab, "load_script", None)
        if callable(loader):
            loader(entry)
        elif hasattr(tab, "script_combo"):
            tab.script_combo.addItem(entry["name"])
            tab.script_combo.setCurrentIndex(tab.script_combo.count() - 1)
        else:
            return bridge_result("runner", False,
                                 "the runner tab takes no script from here")
        if run:
            starter = getattr(tab, "run_selected", None)
            if callable(starter):
                starter()
        focus(window, tab)
        return bridge_result("runner", True,
                             ("ran %r" if run else "loaded %r into the runner")
                             % doc.get("title", ""),
                             {"ran": bool(run)})
    except Exception as exc:
        return bridge_result("runner", False, "runner failed: %s" % exc)


def open_in_debugger(window, doc):
    """Open a document in the Debugger as the script to inspect."""
    tab = target_tab(window, "debugger")
    if tab is None:
        opener = getattr(window, "open_in_debugger", None)
        if callable(opener):
            try:
                opener(_as_entry(doc))
                return bridge_result("debugger", True,
                                     "opened %r in the debugger"
                                     % doc.get("title", ""))
            except Exception as exc:
                return bridge_result("debugger", False,
                                     "debugger failed: %s" % exc)
        return bridge_result("debugger", False,
                             "the Debugger tab is not open")
    try:
        loader = getattr(tab, "load_script", None)
        if callable(loader):
            loader(_as_entry(doc))
            focus(window, tab)
            return bridge_result("debugger", True,
                                 "opened %r in the debugger"
                                 % doc.get("title", ""))
        # No loader: put the text in the tab's editor and let the user start a
        # session, which is the debugger's own model (it prepares, then runs).
        editor = getattr(tab, "trace_edit", None) or getattr(
            tab, "cond_edit", None)
        if editor is not None:
            editor.setText((doc.get("body") or "")[:200])
            focus(window, tab)
            return bridge_result("debugger", True,
                                 "put %r in the debugger" % doc.get("title", ""),
                                 {"prepared_only": True})
        return bridge_result("debugger", False,
                             "the debugger tab takes no script from here")
    except Exception as exc:
        return bridge_result("debugger", False, "debugger failed: %s" % exc)


_BRIDGES = {
    "diff": open_in_diff,
    "search": open_in_search,
    "audit": open_in_audit,
    "analyzer": open_in_analyzer,
    "runner": open_in_runner,
    "debugger": open_in_debugger,
}


def open_in(window, target, doc, **kwargs):
    """Open a document in one named module.

    The kwargs are passed through untouched, so a caller can use the extras a
    single bridge accepts (`previous`, `needle`, `run`) without this dispatcher
    having to know about them.
    """
    target = str(target or "").strip().lower()
    bridge = _BRIDGES.get(target)
    if bridge is None:
        return bridge_result(target, False,
                             "unknown target %r (known: %s)"
                             % (target, ", ".join(INTEGRATION_KEYS)))
    try:
        return bridge(window, doc, **kwargs)
    except Exception as exc:
        return bridge_result(target, False, "bridge failed: %s" % exc)


def open_in_all(window, doc):
    """Try every target at once: ``{target: result}``.

    Useful for a report or a test, and honest by construction: every failure
    comes back as a result with its reason rather than being dropped, so the
    count of what worked is never quietly wrong.
    """
    return {target: open_in(window, target, doc) for target in INTEGRATION_KEYS}


# ---------------------------------------------------------------------------
# Reporting from a document
# ---------------------------------------------------------------------------
# (key, label key, fallback, what the report contains)
REPORT_KINDS = (
    ("document", "docs_rep_document", "Document report",
     "the document itself, its metadata and its structure"),
    ("operations", "docs_rep_operations", "Operations report",
     "what was done to the document in this session"),
    ("links", "docs_rep_links", "Links report",
     "the modules, scripts and projects the document is tied to"),
    ("integrity", "docs_rep_integrity", "Integrity report",
     "its checksum, size and whether the index agrees with the repository"),
)

REPORT_KIND_KEYS = tuple(key for key, _lk, _f, _d in REPORT_KINDS)
REPORT_KIND_LABELS = {key: fallback for key, _lk, fallback, _d in REPORT_KINDS}
REPORT_KIND_MEANINGS = {key: meaning for key, _lk, _f, meaning in REPORT_KINDS}


def report_document(doc, kind="document", index=None, history=None):
    """One document as a report, in the shape the Reports tab indexes."""
    kind = str(kind or "document").strip().lower()
    if kind not in REPORT_KIND_KEYS:
        return {}
    from . import docs_actions
    title = doc.get("title", "(untitled)")
    lines = []
    if kind == "document":
        lines = docs_actions.render_document(doc, "txt").splitlines()
        outline = _outline(doc)
        if outline:
            lines += ["", "Structure", "---------"]
            lines += outline
    elif kind == "operations":
        lines = ["Document operations", "==================",
                 "document: %s" % title, ""]
        for entry in history or ():
            lines.append("%-18s %-8s %s" % (entry.get("at", ""),
                                            entry.get("action", ""),
                                            entry.get("detail", "")))
        if not history:
            lines.append("(nothing recorded in this session)")
    elif kind == "links":
        links = docs_actions.links_of(doc)
        lines = ["Document links", "==============",
                 "document : %s" % title,
                 "important: %s" % docs_actions.is_important(doc),
                 "projects : %s" % (", ".join(docs_actions.projects_of(doc))
                                    or "-"),
                 ""]
        for link_kind in docs_actions.LINK_KINDS:
            names = links.get(link_kind) or []
            lines.append("%-8s: %s" % (link_kind, ", ".join(names) or "-"))
    elif kind == "integrity":
        lines = ["Document integrity", "==================",
                 "document : %s" % title,
                 "id       : %s" % doc.get("id"),
                 "size     : %d byte(s)" % len(doc.get("body") or ""),
                 "lines    : %d" % len((doc.get("body") or "").splitlines()),
                 "created  : %s" % doc.get("created", ""),
                 "modified : %s" % doc.get("modified", ""),
                 "archived : %s" % bool(doc.get("archived"))]
        lines += _integrity(doc, index)

    text = "\n".join(lines) + "\n"
    return {
        "name": "%s_%s" % (_slug(title), kind),
        "title": "%s (%s)" % (title, REPORT_KIND_LABELS[kind]),
        "kind": kind,
        "category": "Documentation",
        "doc_id": doc.get("id"),
        "text": text,
        "lines": len(lines),
        "generated": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _outline(doc):
    """The document's headings as report lines (or [])."""
    try:
        from . import docs_search
    except Exception:
        return []
    lines = []
    for entry in docs_search.document_outline(doc.get("body") or ""):
        lines.append("  %s%s  (line %d)" % ("  " * (entry["level"] - 1),
                                            entry["title"], entry["line"]))
    return lines


def _integrity(doc, index):
    """Whether the index's own record agrees with the document."""
    if index is None:
        return ["", "index    : not open"]
    try:
        rows = index.conn.execute(
            "SELECT checksum, size, terms FROM docs_index WHERE doc_id = ?",
            (doc.get("id"),)).fetchall()
    except Exception:
        return ["", "index    : unavailable"]
    if not rows:
        return ["", "index    : the document is not indexed"]
    row = rows[0]
    from . import docs_engine
    body = doc.get("body") or ""
    current = docs_engine.checksum(body)
    stored = str(row["checksum"] or "")
    return ["", "index    : %s" % ("in step" if stored == current
                                   else "OUT OF STEP (reindex needed)"),
            "checksum : %s" % current[:16],
            "indexed  : %s" % stored[:16],
            "terms    : %s" % row["terms"]]


def _slug(text):
    """A title as a safe report name."""
    out = "".join(character if character.isalnum() else "_"
                  for character in str(text or "document"))
    return out.strip("_")[:40] or "document"


def report_for(doc, kind="document", index=None, history=None):
    """The report's text under its own name: ``(name, text)``.

    The two-part return is deliberate: the Reports tab indexes the name and
    shows the text, and a caller that had to reach into a dict for one of them
    would sooner or later store the wrong one.
    """
    record = report_document(doc, kind, index, history)
    return record.get("name", "document"), record.get("text", "")


# ---------------------------------------------------------------------------
# Describing the surface
# ---------------------------------------------------------------------------
def format_integrations(window=None):
    """The six targets as ASCII lines, with their live availability."""
    lines = ["Document integrations", "====================="]
    for key, _lk, fallback, receives, gives in INTEGRATIONS:
        open_now = target_tab(window, key) is not None if window is not None \
            else False
        lines.append("%-10s %-4s %s" % (key, "open" if open_now else "n/a",
                                        fallback))
        lines.append("           receives: %s" % receives)
        lines.append("           gives   : %s" % gives)
    return lines


def integration_capabilities(window=None):
    """Describe the integrations and the reports for the audit layer."""
    return {
        "integrations": list(INTEGRATION_KEYS),
        "integration_labels": dict(INTEGRATION_LABELS),
        "reports": list(REPORT_KIND_KEYS),
        "report_labels": dict(REPORT_KIND_LABELS),
        "architecture": list(ARCHITECTURE_KEYS),
        "architecture_labels": dict(ARCHITECTURE_LABELS),
        "available": {key: target_tab(window, key) is not None
                      for key in INTEGRATION_KEYS},
        "offline": True,
        "ascii_only": True,
        "zero_dependency": True,
        "cross_platform": True,
    }


def format_architecture():
    """The tab's architecture standard as ASCII lines.

    Rendered from the table rather than written out, so the text a user reads
    and the table a caller inspects cannot drift apart.
    """
    lines = ["Document tab architecture (the standard)",
             "======================================"]
    for key, _lk, fallback, meaning in ARCHITECTURE:
        lines.append("%-18s %s" % (key, fallback))
        lines.append("                   %s" % meaning)
    return lines
