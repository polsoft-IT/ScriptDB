# -*- coding: utf-8 -*-
"""The eleven operations a document supports, as head-less functions.

`tabs/docs_tab.py` owns the repository; its `update`, `delete` and `get` are the
primitives. This module is the *operations* built on them -- open, edit, delete,
move, copy, mark important, pin to a project, tag, compare, diff versions,
export -- written so that each is one call with one shape, usable from the tab,
the API, the CLI and a test without a window existing.

     OPERATIONS      the eleven operations, as a table (with their arguments)
     run             dispatch one operation by name
     edit / move / copy / mark_important / pin / tag / compare / diff /
     export          one function each, all returning the same result shape

Every function returns ``{ok, action, doc_id, detail, changed}``. One shape for
all of them is what lets a UI bind eleven buttons to one handler, and it is why
a failure says *why* rather than only that it did not happen.

Two decisions worth stating:

    * **A copy is a new document, not a link.** Copying gives the copy its own
      id and a title that says it is a copy, because a "copy" that silently
      aliases the original would be a trap the first time either is edited.
    * **Pinning and tagging are metadata, never columns.** They live in the
      document's `meta` mapping, so a document gains a project or a tag without
      the schema changing and without a migration -- which is what keeps the
      feature usable on a repository someone already has.
"""

import datetime as dt
import json

# ---------------------------------------------------------------------------
# The operations
# ---------------------------------------------------------------------------
# (key, label key, fallback, arguments, what it does)
OPERATIONS = (
    ("open", "docs_op_open", "Open",
     (), "read the document and make it the current one"),
    ("edit", "docs_op_edit", "Edit",
     ("title", "body", "doc_type", "category", "tags", "description"),
     "change any of a document's editable fields"),
    ("delete", "docs_op_delete", "Delete",
     ("ids",), "remove one document or several"),
    ("move", "docs_op_move", "Move",
     ("category",), "change which category (folder) the document lives in"),
    ("copy", "docs_op_copy", "Copy",
     ("title",), "duplicate the document under a new id and title"),
    ("mark_important", "docs_op_important", "Mark as important",
     ("important",), "flag the document as important, or clear the flag"),
    ("pin", "docs_op_pin", "Pin to project",
     ("project",), "attach the document to a project, or detach it"),
    ("tag", "docs_op_tag", "Add tags",
     ("add", "remove"), "add and remove tags, keeping the rest"),
    ("compare", "docs_op_compare", "Compare with another document",
     ("other_id",), "put two documents side by side as a diff"),
    ("diff_versions", "docs_op_diff_versions", "Show version differences",
     ("snapshot_id",), "compare the document with an earlier snapshot"),
    ("export", "docs_op_export", "Export",
     ("fmt", "path"), "write the document as TXT, JSON or LOG"),
)

OPERATION_KEYS = tuple(key for key, _lk, _f, _a, _d in OPERATIONS)
OPERATION_LABELS = {key: fallback for key, _lk, fallback, _a, _d in OPERATIONS}
OPERATION_ARGUMENTS = {key: args for key, _lk, _f, args, _d in OPERATIONS}
OPERATION_MEANINGS = {key: meaning for key, _lk, _f, _a, meaning in OPERATIONS}

# The export formats the operation accepts, and the suffix each is written with.
EXPORT_FORMATS = ("txt", "json", "log")
EXPORT_SUFFIX = {"txt": ".txt", "json": ".json", "log": ".log"}

# The metadata key the important flag lives under. A leading underscore marks it
# as the repository's own bookkeeping rather than something a user typed.
IMPORTANT_KEY = "_important"

# The metadata key holding the projects a document is pinned to. A list, so a
# document may belong to more than one project without a second key appearing.
PROJECTS_KEY = "_projects"

# The metadata key holding the module or script a document documents. Kept as a
# list for the same reason as the projects.
LINKS_KEY = "_links"

# The kinds of thing a document can be linked to.
LINK_KINDS = ("module", "script", "project")


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------
def result(action, ok, doc_id=None, detail="", changed=None):
    """One operation's outcome, in the shape every operation returns."""
    return {
        "ok": bool(ok),
        "action": action,
        "doc_id": doc_id,
        "detail": detail,
        "changed": dict(changed or {}),
    }


def _meta_of(doc):
    """A document's meta mapping, whatever form it is stored in (never None)."""
    meta = (doc or {}).get("meta")
    if isinstance(meta, dict):
        return dict(meta)
    if isinstance(meta, str):
        try:
            loaded = json.loads(meta)
        except (ValueError, TypeError):
            return {}
        return dict(loaded) if isinstance(loaded, dict) else {}
    return {}


def _tags_of(doc):
    """A document's tags as an ordered list, from the comma/space string."""
    raw = (doc or {}).get("tags") or ""
    out = []
    for piece in str(raw).replace(",", " ").split():
        text = piece.strip()
        if text and text not in out:
            out.append(text)
    return out


def _need(repo, doc_id):
    """The document, or None when it is missing (so a caller can report it)."""
    if repo is None or doc_id is None:
        return None
    try:
        return repo.get(doc_id)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# The operations
# ---------------------------------------------------------------------------
def open_document(repo, doc_id):
    """Read one document (the operation a UI calls 'open')."""
    doc = _need(repo, doc_id)
    if doc is None:
        return result("open", False, doc_id, "document not found")
    return result("open", True, doc_id,
                  "opened %r" % doc.get("title", ""),
                  {"title": doc.get("title", ""),
                   "lines": len((doc.get("body") or "").splitlines())})


def edit_document(repo, doc_id, **fields):
    """Change any of a document's editable fields.

    Only the fields that were actually passed are written, so an edit to the
    body cannot silently clear the tags -- which is what a "save everything"
    call does when a caller forgets one of them.
    """
    doc = _need(repo, doc_id)
    if doc is None:
        return result("edit", False, doc_id, "document not found")
    wanted = {key: value for key, value in fields.items() if value is not None}
    if not wanted:
        return result("edit", False, doc_id, "nothing to change")
    try:
        ok = repo.update(doc_id, **wanted)
    except Exception as exc:
        return result("edit", False, doc_id, "update failed: %s" % exc)
    if not ok:
        return result("edit", False, doc_id, "no editable field was given")
    return result("edit", True, doc_id,
                  "changed " + ", ".join(sorted(wanted)), wanted)


def delete_documents(repo, ids):
    """Remove one document or several."""
    if isinstance(ids, int):
        ids = [ids]
    wanted = [doc_id for doc_id in (ids or []) if doc_id is not None]
    if not wanted:
        return result("delete", False, None, "no document given")
    try:
        removed = repo.delete(wanted)
    except Exception as exc:
        return result("delete", False, None, "delete failed: %s" % exc)
    count = removed if isinstance(removed, int) else len(wanted)
    return result("delete", bool(count), None,
                  "deleted %d document(s)" % count, {"ids": wanted})


def move_document(repo, doc_id, category):
    """Change the category (folder) a document lives in."""
    doc = _need(repo, doc_id)
    if doc is None:
        return result("move", False, doc_id, "document not found")
    target = str(category or "").strip()
    if not target:
        return result("move", False, doc_id, "no category given")
    if target == (doc.get("category") or ""):
        return result("move", True, doc_id, "already in %r" % target)
    try:
        repo.update(doc_id, category=target)
    except Exception as exc:
        return result("move", False, doc_id, "move failed: %s" % exc)
    return result("move", True, doc_id,
                  "moved from %r to %r" % (doc.get("category") or "", target),
                  {"from": doc.get("category") or "", "to": target})


def copy_document(repo, doc_id, title="", keep_links=True):
    """Duplicate a document under a new id.

    The copy is a *separate* document: it gets its own id and a title that says
    it is a copy, because a copy that aliased the original would surprise
    whoever edited one of them next. The metadata travels with it (a copy of a
    linked document is still about the same module), which is what `keep_links`
    controls.
    """
    doc = _need(repo, doc_id)
    if doc is None:
        return result("copy", False, doc_id, "document not found")
    new_title = str(title or "").strip() or ("%s (copy)"
                                            % doc.get("title", "document"))
    meta = _meta_of(doc)
    if not keep_links:
        meta.pop(LINKS_KEY, None)
    try:
        new_id = repo.add(new_title, body=doc.get("body") or "",
                          doc_type=doc.get("doc_type", "text"),
                          category=doc.get("category", "General"),
                          tags=doc.get("tags", ""),
                          description=doc.get("description", ""),
                          meta=meta)
    except Exception as exc:
        return result("copy", False, doc_id, "copy failed: %s" % exc)
    return result("copy", bool(new_id), new_id,
                  "copied %r to %r" % (doc.get("title", ""), new_title),
                  {"source": doc_id, "title": new_title})


def mark_important(repo, doc_id, important=True):
    """Flag a document as important, or clear the flag."""
    doc = _need(repo, doc_id)
    if doc is None:
        return result("mark_important", False, doc_id, "document not found")
    meta = _meta_of(doc)
    was = bool(meta.get(IMPORTANT_KEY))
    if bool(important):
        meta[IMPORTANT_KEY] = True
    else:
        meta.pop(IMPORTANT_KEY, None)
    if was == bool(important):
        return result("mark_important", True, doc_id,
                      "already %s" % ("important" if important else "not important"))
    try:
        repo.update(doc_id, meta=meta)
    except Exception as exc:
        return result("mark_important", False, doc_id,
                      "update failed: %s" % exc)
    return result("mark_important", True, doc_id,
                  "marked important" if important else "unmarked",
                  {"important": bool(important)})


def pin_document(repo, doc_id, project="", detach=False):
    """Attach a document to a project, or detach it from one.

    A document may belong to several projects, so the pins are a list: pinning
    twice is idempotent and detaching one project leaves the others alone.
    """
    doc = _need(repo, doc_id)
    if doc is None:
        return result("pin", False, doc_id, "document not found")
    name = str(project or "").strip()
    if not name:
        return result("pin", False, doc_id, "no project given")
    meta = _meta_of(doc)
    projects = list(meta.get(PROJECTS_KEY) or [])
    if detach:
        if name not in projects:
            return result("pin", True, doc_id, "not pinned to %r" % name)
        projects = [item for item in projects if item != name]
    else:
        if name in projects:
            return result("pin", True, doc_id, "already pinned to %r" % name)
        projects.append(name)
    if projects:
        meta[PROJECTS_KEY] = projects
    else:
        meta.pop(PROJECTS_KEY, None)
    try:
        repo.update(doc_id, meta=meta)
    except Exception as exc:
        return result("pin", False, doc_id, "update failed: %s" % exc)
    return result("pin", True, doc_id,
                  ("unpinned from %r" if detach else "pinned to %r") % name,
                  {"projects": projects})


def tag_document(repo, doc_id, add=(), remove=()):
    """Add and remove tags, keeping the ones that were already there.

    The result is a set of tags, so adding a tag the document already has is
    not an error and removing one it never had is not an error either: a tag
    operation says what the document's tags should *become*, and a caller that
    treats it as an append would double every tag on a retry.
    """
    doc = _need(repo, doc_id)
    if doc is None:
        return result("tag", False, doc_id, "document not found")
    wanted = [_text(item) for item in (add or ()) if _text(item)]
    unwanted = [_text(item) for item in (remove or ()) if _text(item)]
    if not wanted and not unwanted:
        return result("tag", False, doc_id, "no tag given")
    tags = _tags_of(doc)
    added, removed = [], []
    for tag in wanted:
        if tag not in tags:
            tags.append(tag)
            added.append(tag)
    for tag in unwanted:
        if tag in tags:
            tags.remove(tag)
            removed.append(tag)
    if not added and not removed:
        return result("tag", True, doc_id, "tags unchanged",
                      {"tags": tags})
    try:
        repo.update(doc_id, tags=", ".join(tags))
    except Exception as exc:
        return result("tag", False, doc_id, "update failed: %s" % exc)
    return result("tag", True, doc_id,
                  "+%d / -%d tag(s)" % (len(added), len(removed)),
                  {"tags": tags, "added": added, "removed": removed})


def compare_documents(repo, doc_id, other_id):
    """Put two documents side by side, as lines ready for a diff view."""
    left, right = _need(repo, doc_id), _need(repo, other_id)
    if left is None or right is None:
        return result("compare", False, doc_id, "one of the documents is missing")
    return result("compare", True, doc_id,
                  "comparing %r with %r" % (left.get("title", ""),
                                            right.get("title", "")),
                  {"left": {"id": doc_id, "title": left.get("title", ""),
                            "body": left.get("body") or ""},
                   "right": {"id": other_id, "title": right.get("title", ""),
                             "body": right.get("body") or ""}})


def diff_versions(repo, index, doc_id, snapshot_id=None):
    """Compare a document with an earlier snapshot of it.

    The index owns the snapshots, so this reads them through it rather than
    keeping a second history: two histories would disagree eventually, and the
    one a user could see would be the wrong one.
    """
    doc = _need(repo, doc_id)
    if doc is None:
        return result("diff_versions", False, doc_id, "document not found")
    if index is None:
        return result("diff_versions", False, doc_id,
                      "the knowledge index is not open")
    snapshots = []
    try:
        snapshots = index.snapshots()
    except Exception:
        snapshots = []
    if not snapshots:
        return result("diff_versions", False, doc_id,
                      "no snapshot to compare against")
    chosen = snapshot_id
    if chosen is None:
        chosen = snapshots[0]["id"]
    try:
        old_text = index.snapshot_text(chosen, doc_id)
        diff = index.compare_versions(old_text, doc.get("body") or "")
    except Exception as exc:
        return result("diff_versions", False, doc_id,
                      "comparison failed: %s" % exc)
    return result("diff_versions", bool(not diff.get("identical")), doc_id,
                  "+%d / -%d line(s), %d changed block(s)"
                  % (diff.get("additions", 0), diff.get("removals", 0),
                     diff.get("changed", 0)),
                  {"snapshot": chosen, "diff": diff})


def export_document(repo, doc_id, fmt="txt", path=""):
    """Write one document as TXT, JSON or LOG.

    Returns the path written. The three formats differ in what they are *for*
    rather than in what they contain: TXT is the document as a person reads it,
    JSON is the document as a program reads it (metadata included), and LOG is
    the document as a dated, appendable record.
    """
    doc = _need(repo, doc_id)
    if doc is None:
        return result("export", False, doc_id, "document not found")
    fmt = str(fmt or "txt").strip().lower()
    if fmt not in EXPORT_FORMATS:
        return result("export", False, doc_id,
                      "unsupported format %r (use %s)"
                      % (fmt, ", ".join(EXPORT_FORMATS)))
    text = render_document(doc, fmt)
    if not path:
        return result("export", True, doc_id, "rendered %d byte(s)" % len(text),
                      {"format": fmt, "text": text})
    try:
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
    except OSError as exc:
        return result("export", False, doc_id, "cannot write: %s" % exc)
    return result("export", True, doc_id, "written: %s" % path,
                  {"format": fmt, "path": path, "bytes": len(text)})


def render_document(doc, fmt="txt"):
    """One document as text in the requested format."""
    fmt = str(fmt or "txt").strip().lower()
    if fmt == "json":
        payload = {
            "id": doc.get("id"),
            "title": doc.get("title", ""),
            "doc_type": doc.get("doc_type", ""),
            "category": doc.get("category", ""),
            "tags": doc.get("tags", ""),
            "description": doc.get("description", ""),
            "created": doc.get("created", ""),
            "modified": doc.get("modified", ""),
            "meta": _meta_of(doc),
            "body": doc.get("body") or "",
        }
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if fmt == "log":
        lines = [
            "[%s] %s" % (doc.get("modified") or doc.get("created") or "",
                         doc.get("title", "(untitled)")),
            "[%s] type=%s category=%s archived=%s"
            % (dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               doc.get("doc_type", ""), doc.get("category", ""),
               bool(doc.get("archived"))),
        ]
        meta = _meta_of(doc)
        if meta:
            lines.append("[%s] meta=%s"
                         % (dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            json.dumps(meta, sort_keys=True)))
        lines.append("")
        lines.append(doc.get("body") or "")
        return "\n".join(lines)
    # TXT: a header a person can read, then the body unchanged.
    header = [
        doc.get("title", "(untitled)"),
        "=" * len(str(doc.get("title", "(untitled)"))),
        "Type    : %s" % doc.get("doc_type", ""),
        "Category: %s" % doc.get("category", ""),
        "Tags    : %s" % doc.get("tags", ""),
        "Created : %s" % doc.get("created", ""),
        "Modified: %s" % doc.get("modified", ""),
        "",
    ]
    return "\n".join(header) + (doc.get("body") or "") + "\n"


# ---------------------------------------------------------------------------
# Links: a document tied to a module, a script or a project
# ---------------------------------------------------------------------------
def link_document(repo, doc_id, kind, name, detach=False):
    """Attach a document to a module, a script or a project.

    The link lives in the document's own metadata, so it survives export, needs
    no schema change, and cannot be lost by a rebuild of the index -- the same
    reason the important flag and the pins live there.
    """
    kind = str(kind or "").strip().lower()
    if kind not in LINK_KINDS:
        return result("link", False, doc_id,
                      "unknown link kind %r (use %s)"
                      % (kind, ", ".join(LINK_KINDS)))
    doc = _need(repo, doc_id)
    if doc is None:
        return result("link", False, doc_id, "document not found")
    wanted = "%s:%s" % (kind, str(name or "").strip())
    if not str(name or "").strip():
        return result("link", False, doc_id, "no name given")
    meta = _meta_of(doc)
    links = list(meta.get(LINKS_KEY) or [])
    if detach:
        if wanted not in links:
            return result("link", True, doc_id, "not linked to %s" % wanted)
        links = [item for item in links if item != wanted]
    else:
        if wanted in links:
            return result("link", True, doc_id, "already linked to %s" % wanted)
        links.append(wanted)
    if links:
        meta[LINKS_KEY] = links
    else:
        meta.pop(LINKS_KEY, None)
    try:
        repo.update(doc_id, meta=meta)
    except Exception as exc:
        return result("link", False, doc_id, "update failed: %s" % exc)
    return result("link", True, doc_id,
                  ("unlinked %s" if detach else "linked %s") % wanted,
                  {"links": links, "kind": kind, "name": name})


def links_of(doc):
    """A document's links as ``{kind: [name]}`` (never None)."""
    out = {}
    for entry in _meta_of(doc).get(LINKS_KEY) or []:
        kind, _sep, name = str(entry).partition(":")
        if not kind or not name:
            continue
        out.setdefault(kind, []).append(name)
    return out


def documents_for_link(repo, kind, name):
    """Every document linked to one module, script or project."""
    wanted = "%s:%s" % (str(kind or "").lower(), str(name or "").strip())
    found = []
    for doc in _documents(repo):
        if wanted in (_meta_of(doc).get(LINKS_KEY) or []):
            found.append(doc)
    return found


def projects_of(doc):
    """The projects a document is pinned to (never None)."""
    return list(_meta_of(doc).get(PROJECTS_KEY) or [])


def is_important(doc):
    """Whether a document is flagged important."""
    return bool(_meta_of(doc).get(IMPORTANT_KEY))


def _documents(repo):
    """Every document in the repository (never raises, never None)."""
    if repo is None:
        return []
    for name in ("list_documents", "documents", "all"):
        getter = getattr(repo, name, None)
        if callable(getter):
            try:
                return list(getter())
            except TypeError:
                continue
            except Exception:
                return []
    return []


def _text(value):
    """One tag or name as a trimmed string."""
    return str(value or "").strip()


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
def run(repo, action, doc_id=None, index=None, **kwargs):
    """Run one operation by name; returns the shared result shape.

    The dispatch is a table rather than a chain of `if`s so the eleven
    operations stay one line apart in the source: a reader can see all of them
    at once, and adding a twelfth is one entry in two places.
    """
    action = str(action or "").strip().lower()
    handlers = {
        "open": lambda: open_document(repo, doc_id),
        "edit": lambda: edit_document(repo, doc_id, **_editable(kwargs)),
        "delete": lambda: delete_documents(repo, kwargs.get("ids", doc_id)),
        "move": lambda: move_document(repo, doc_id, kwargs.get("category", "")),
        "copy": lambda: copy_document(repo, doc_id, kwargs.get("title", "")),
        "mark_important": lambda: mark_important(
            repo, doc_id, kwargs.get("important", True)),
        "pin": lambda: pin_document(repo, doc_id, kwargs.get("project", ""),
                                    kwargs.get("detach", False)),
        "tag": lambda: tag_document(repo, doc_id, kwargs.get("add", ()),
                                    kwargs.get("remove", ())),
        "compare": lambda: compare_documents(repo, doc_id,
                                             kwargs.get("other_id")),
        "diff_versions": lambda: diff_versions(repo, index, doc_id,
                                               kwargs.get("snapshot_id")),
        "export": lambda: export_document(repo, doc_id, kwargs.get("fmt", "txt"),
                                          kwargs.get("path", "")),
        "link": lambda: link_document(repo, doc_id, kwargs.get("kind", ""),
                                      kwargs.get("name", ""),
                                      kwargs.get("detach", False)),
    }
    handler = handlers.get(action)
    if handler is None:
        return result(action, False, doc_id,
                      "unknown operation %r (known: %s)"
                      % (action, ", ".join(OPERATION_KEYS)))
    try:
        return handler()
    except Exception as exc:
        return result(action, False, doc_id, "operation failed: %s" % exc)


def _editable(kwargs):
    """The editable fields among a kwargs mapping."""
    return {key: value for key, value in kwargs.items()
            if key in ("title", "body", "doc_type", "category", "tags",
                       "description")}


# ---------------------------------------------------------------------------
# Describing the surface
# ---------------------------------------------------------------------------
def format_operations():
    """The eleven operations as ASCII lines (the About page and the CLI)."""
    lines = ["Document operations", "=================="]
    for key, _lk, fallback, arguments, meaning in OPERATIONS:
        lines.append("%-16s %s" % (key, fallback))
        lines.append("                 %s%s"
                     % (meaning, ("  args: " + ", ".join(arguments))
                        if arguments else ""))
    return lines


def action_capabilities():
    """Describe the operations and the links for the audit / about layer."""
    return {
        "operations": list(OPERATION_KEYS),
        "operation_labels": dict(OPERATION_LABELS),
        "operation_arguments": {key: list(value)
                                for key, value in OPERATION_ARGUMENTS.items()},
        "export_formats": list(EXPORT_FORMATS),
        "link_kinds": list(LINK_KINDS),
        "flags": {"important": IMPORTANT_KEY, "projects": PROJECTS_KEY,
                  "links": LINKS_KEY},
        "offline": True,
        "ascii_only": True,
        "zero_dependency": True,
        "cross_platform": True,
    }
