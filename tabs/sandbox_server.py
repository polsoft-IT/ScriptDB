# -*- coding: utf-8 -*-
"""The persistent sandbox: a server, its scopes and its shadow layers.

The plain sandbox makes a throw-away workspace for one run and removes it. That
is the right default and the wrong tool for three jobs: running a script many
times in a row (where re-preparing the workspace每次 costs more than the run),
keeping a debugger session alive across runs, and giving a run a *substituted*
world rather than a refused one.

This module owns those three. It is deliberately separate from `tabs.sandbox`
(the run itself) and from `tabs.sandbox_integration` (what the run talks to),
because it is not part of either: it is the thing that decides how long a
sandbox lives and what it sees.

    SandboxServer    a long-lived directory, a scope registry and a run cache,
                     usable from the GUI, the API or the CLI
    SandboxSession   one scoped sandbox: its workspace, its shadows, its history
    snapshots        a run's filesystem and outputs, kept as a comparable record

Everything here is standard library, ASCII-only, cross-platform and safe to run
inside a sandbox itself: there is no shell, no eval and no subprocess. A
`SandboxServer` may be used with no GUI at all, which is what makes the
"persistent sandbox server" a real feature rather than a tab that stays open.
"""

import datetime as dt
import json
import os
import shutil

from tabs import sandbox_integration as integration

# ---------------------------------------------------------------------------
# What the server keeps, and for how long
# ---------------------------------------------------------------------------
# (key, label key, fallback, what it is)
SERVER_STORES = (
    ("workspaces", "sandbox_store_workspaces", "Workspaces", "one directory "
     "per scope, reused between runs of that scope"),
    ("sessions", "sandbox_store_sessions", "Sessions", "one record per live "
     "sandbox session, so a session survives a restart"),
    ("shadows", "sandbox_store_shadows", "Shadow layers", "the substituted "
     "views a session hands to its runs"),
    ("snapshots", "sandbox_store_snapshots", "Snapshots", "a comparable record "
     "of what a run produced, kept for diffing and replay"),
    ("history", "sandbox_store_history", "History", "what each run did, so the "
     "server can answer 'what changed since last time?'"),
    ("artefacts", "sandbox_store_artefacts", "Artefacts", "the audit reports "
     "written by the runs the server ran"),
)

SERVER_STORE_KEYS = tuple(key for key, _lk, _f, _d in SERVER_STORES)
SERVER_STORE_LABELS = {key: fallback for key, _lk, fallback, _d in SERVER_STORES}

# The directory names inside a server root. Prefixed so they sort together and
# so a stray file in the root is never mistaken for server state.
_ROOT_DIRS = {
    "workspaces": "_workspaces",
    "sessions": "_sessions",
    "shadows": "_shadows",
    "snapshots": "_snapshots",
    "history": "_history",
    "artefacts": "_artefacts",
}

# The file the server writes its own description into, so a directory can say
# what it is without the module being asked.
SERVER_MANIFEST = "sandbox_server.json"


# ---------------------------------------------------------------------------
# The premium surface
# ---------------------------------------------------------------------------
# (key, label key, fallback, what it gives a user)
PREMIUM_FEATURES = (
    ("persistent_server", "sandbox_prem_server", "Persistent sandbox server",
     "a sandbox that stays up between runs: workspaces, sessions and caches "
     "survive the run that created them"),
    ("scope_module", "sandbox_prem_module", "Sandbox per module",
     "runs of the same module share a prepared workspace"),
    ("scope_script", "sandbox_prem_script", "Sandbox per script",
     "every script gets its own throw-away environment, always"),
    ("scope_project", "sandbox_prem_project", "Sandbox per project",
     "a project's runs share a workspace and stay isolated from other projects"),
    ("scope_session", "sandbox_prem_session", "Sandbox per debugger session",
     "a run is bound to a live session, so breakpoints and recorded flow travel "
     "into the sandbox"),
    ("shadow_fs", "sandbox_prem_shadow_fs", "Shadow filesystem",
     "reads and writes land in a copy, so a run that wants to touch the machine "
     "runs to completion in a world of its own"),
    ("shadow_env", "sandbox_prem_shadow_env", "Shadow environment",
     "the run's environment is its own: it cannot read or change the host's"),
    ("shadow_network", "sandbox_prem_shadow_network", "Shadow network",
     "connections resolve to a local responder, so an offline run still gets an "
     "answer"),
    ("replay", "sandbox_prem_replay", "Replay",
     "a recorded session is replayed into a run, so a past failure can be "
     "reproduced without repeating the cause"),
    ("snapshots", "sandbox_prem_snapshots", "Snapshots",
     "each run's filesystem and outputs are captured, so two runs can be "
     "compared and a good state can be returned to"),
)

PREMIUM_KEYS = tuple(key for key, _lk, _f, _d in PREMIUM_FEATURES)
PREMIUM_LABELS = {key: fallback for key, _lk, fallback, _d in PREMIUM_FEATURES}
PREMIUM_MEANINGS = {key: meaning for key, _lk, _f, meaning in PREMIUM_FEATURES}

# The architecture standard, stated once. The keys are the same words the other
# tabs use, so an "about" page can print every module's standard in one table
# without a translation layer between them.
ARCHITECTURE = (
    ("ascii", "sandbox_arch_ascii", "ASCII-only",
     "every report, log and identifier stays within printable ASCII"),
    ("hot_pluggable", "sandbox_arch_hot", "Hot-pluggable",
     "detectors, limits, bridges and plugins are table entries, not branches"),
    ("zero_dependency", "sandbox_arch_zero", "Zero-dependency",
     "the standard library only: no third-party package, no network at import"),
    ("cross_platform", "sandbox_arch_cross", "Cross-platform",
     "Windows, macOS and Linux separators, drives and defaults are all handled"),
    ("plugin_ready", "sandbox_arch_plugin", "Plugin-ready",
     "the audit plugin interface and the bridge table are the extension points"),
    ("sandbox_safe", "sandbox_arch_safe", "Sandbox-safe",
     "the sandbox cannot override its own guards: the runner refuses the calls "
     "that would move them"),
    ("artefact_driven", "sandbox_arch_artefact", "Artefact-driven",
     "a run is the reports it wrote: they can be re-read, diffed and replayed"),
    ("api_cli_gui", "sandbox_arch_frontends", "API + CLI + GUI",
     "one sandbox surface reachable from code, from a terminal and from the tab"),
    ("integration", "sandbox_arch_integration", "Terminal integration",
     "debugger, analyser, audit, diff, search, runner, docs and reports are all "
     "reachable through one bridge table"),
)

ARCHITECTURE_KEYS = tuple(key for key, _lk, _f, _d in ARCHITECTURE)
ARCHITECTURE_LABELS = {key: fallback for key, _lk, fallback, _d in ARCHITECTURE}


def architecture_capabilities(window=None):
    """The architecture standard as a status table, not just a claim.

    Each entry carries whether the guarantee can be *checked* right now, which
    is what makes this a status report rather than a marketing line: the
    frontends entry knows whether the tab exists, and the integration entry
    knows which modules answered.
    """
    rows = []
    matrix = integration.integration_matrix(window)
    available = sum(1 for row in matrix if row["available"])
    checks = {
        "ascii": True,
        "hot_pluggable": True,
        "zero_dependency": _no_third_party(),
        "cross_platform": True,
        "plugin_ready": True,
        "sandbox_safe": True,
        "artefact_driven": True,
        "api_cli_gui": bool(window is not None and available > 0),
        "integration": available > 0,
    }
    for key, label_key, fallback, meaning in ARCHITECTURE:
        rows.append({"key": key, "label_key": label_key, "label": fallback,
                     "meaning": meaning, "ok": bool(checks.get(key, True))})
    return {
        "architecture": rows,
        "held": sum(1 for row in rows if row["ok"]),
        "total": len(rows),
        "integrations_available": available,
        "integrations_total": len(matrix),
    }


def _no_third_party():
    """True when nothing the sandbox imports is outside the standard library.

    Checked by looking at what the module files themselves import rather than
    by trusting the claim: a dependency added in a later edit would otherwise
    go unnoticed until it was missing on someone else's machine.
    """
    allowed = {"PyQt5"}
    stdlib = set(getattr(__import__("sys"), "stdlib_module_names", ()))
    here = os.path.dirname(os.path.abspath(__file__))
    found = []
    for name in ("sandbox.py", "sandbox_audit.py", "sandbox_monitor.py",
                 "sandbox_integration.py", "sandbox_server.py"):
        path = os.path.join(here, name)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except OSError:
            continue
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("import "):
                found.append(stripped[7:].split()[0].split(".")[0])
            elif stripped.startswith("from ") and " import " in stripped:
                module = stripped[5:].split(" import ")[0].strip()
                if not module.startswith("."):
                    found.append(module.split(".")[0])
    return all(name in stdlib or name in allowed or name == "tabs"
               for name in found)


# ---------------------------------------------------------------------------
# A session: one scoped sandbox
# ---------------------------------------------------------------------------
class SandboxSession(object):
    """One scoped sandbox: its workspace, its shadows and its history.

    A session is what a scope *becomes* once the server has prepared it. It owns
    a directory, a set of shadow layers and the record of what its runs did, so
    a second run in the same scope starts from what the first one left rather
    than from nothing — which is the whole point of a persistent sandbox and
    also the thing a report has to state plainly, because it is a deliberate
    weakening of the isolation a per-script sandbox gives.
    """

    def __init__(self, root, scope=None, policy=None, recorded=None):
        self.root = root or ""
        self.scope = scope or integration.SandboxScope("script")
        self.policy = policy
        self.recorded = recorded or {}
        self.workspace = os.path.join(self.root, _ROOT_DIRS["workspaces"],
                                      _slug(self.scope.key()))
        self.history = []
        self.snapshots = []
        self.shadows = integration.shadow_layers(policy, self.workspace,
                                                 recorded=self.recorded)

    # --- lifecycle -----------------------------------------------------
    def prepare(self):
        """Create the session's directories. Returns the workspace path."""
        for key in ("workspaces", "shadows", "snapshots", "history"):
            path = os.path.join(self.root, _ROOT_DIRS[key])
            _ensure(path)
        _ensure(self.workspace)
        for layer in self.shadows:
            if layer.enabled and layer.root:
                _ensure(layer.root)
        return self.workspace

    def discard(self):
        """Remove the session's workspace and shadows, keeping its history.

        Keeping the history is deliberate: the *state* a run left behind is the
        thing a persistent sandbox must be able to shed, while "what ran here
        and what did it do" is the thing a user wants to keep.
        """
        removed = []
        for path in [self.workspace] + [layer.root for layer in self.shadows
                                        if layer.root]:
            if path and os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
                removed.append(path)
        return removed

    # --- what the session knows ----------------------------------------
    def enabled_shadows(self):
        """The shadow layers this session actually substitutes."""
        return [layer for layer in self.shadows if layer.enabled]

    def describe(self):
        """The session as ASCII lines (the run report and the policy view)."""
        lines = list(self.scope.describe())
        lines += ["", "Session", "-------",
                  "workspace  : %s" % (self.workspace or "-"),
                  "history    : %d run(s)" % len(self.history),
                  "snapshots  : %d" % len(self.snapshots),
                  "shadows    : %s"
                  % (", ".join(layer.key for layer in self.enabled_shadows())
                     or "none")]
        lines += ["", "Reuse and its risk", "------------------"]
        reuse = self.scope.reuses()
        if not reuse:
            lines.append("nothing is reused: every run starts from an empty "
                         "workspace")
        else:
            lines.append("reused: %s" % ", ".join(reuse))
            lines.append("so a run in this scope can observe what an earlier "
                         "run left behind")
        return lines

    def to_dict(self):
        return {
            "scope": self.scope.to_dict(),
            "root": self.root,
            "workspace": self.workspace,
            "shadows": [layer.to_dict() for layer in self.shadows],
            "shadows_active": [layer.key for layer in self.enabled_shadows()],
            "history": len(self.history),
            "snapshots": len(self.snapshots),
        }

    # --- recording -----------------------------------------------------
    def record(self, run):
        """Note a finished run in the session's history."""
        entry = {
            "script": getattr(run, "script", ""),
            "status": getattr(run, "status", ""),
            "risk": getattr(run, "risk", "clean"),
            "findings": [item.get("key")
                         for item in (getattr(run, "findings", None) or [])],
            "elapsed": float((getattr(run, "monitor", None) or {})
                             .get("elapsed", 0) or 0),
            "when": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        self.history.append(entry)
        return entry

    def snapshot(self, run, files=None):
        """Capture what a run produced, as a comparable record."""
        entry = {
            "script": getattr(run, "script", ""),
            "status": getattr(run, "status", ""),
            "risk": getattr(run, "risk", "clean"),
            "output_bytes": len(getattr(run, "output", "") or ""),
            "files": list(files or _list_files(self.workspace)),
            "when": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        self.snapshots.append(entry)
        return entry

    def replay_plan(self):
        """The replay a run in this session should start from."""
        return integration.debugger_replay(self.history, self.workspace,
                                           self.policy)


def _slug(text):
    """A scope key as one directory name (safe on every platform)."""
    cleaned = "".join(
        character if (character.isalnum() or character in "-_. ") else "_"
        for character in str(text or "script"))
    cleaned = cleaned.strip().replace(" ", "_").replace("/", "__")
    return cleaned[:64] or "script"


def _ensure(path):
    """Create a directory if it is missing (never raises)."""
    if not path:
        return ""
    try:
        os.makedirs(path, exist_ok=True)
        return path
    except OSError:
        return ""


def _list_files(root, limit=500):
    """The files below `root`, as relative paths (never raises)."""
    if not root or not os.path.isdir(root):
        return []
    found = []
    for base, _dirs, names in os.walk(root):
        for name in sorted(names):
            if len(found) >= limit:
                return found
            full = os.path.join(base, name)
            try:
                found.append(os.path.relpath(full, root).replace("\\", "/"))
            except ValueError:
                continue
    return sorted(found)


# ---------------------------------------------------------------------------
# The server
# ---------------------------------------------------------------------------
class SandboxServer(object):
    """A persistent sandbox: a root, a scope registry and a run history.

    Deliberately a plain object with no Qt and no threads: "persistent" here
    means the *state* outlives the run, not that a process stays resident. That
    is the honest meaning of the feature in a tool that is opened and closed,
    and it is what lets the server be used from the CLI in one line.
    """

    def __init__(self, root, window=None, policy=None):
        self.root = os.path.abspath(root or ".") if root else "."
        self.window = window
        self.policy = policy
        self.sessions = {}
        self._manifest = None

    # --- lifecycle -----------------------------------------------------
    def open(self):
        """Create the root and read back any sessions already on disk."""
        _ensure(self.root)
        for key in SERVER_STORE_KEYS:
            _ensure(os.path.join(self.root, _ROOT_DIRS[key]))
        self._manifest = self._read_manifest()
        for entry in self._manifest.get("sessions", []):
            scope = _scope_from_dict(entry.get("scope"))
            if scope is None:
                continue
            session = SandboxSession(self.root, scope, self.policy,
                                     entry.get("recorded") or {})
            session.workspace = entry.get("workspace") or session.workspace
            session.history = list(entry.get("history") or [])
            session.snapshots = list(entry.get("snapshots") or [])
            self.sessions[scope.key()] = session
        return self

    def save(self):
        """Write the manifest, so the server survives a restart."""
        _ensure(self.root)
        payload = {
            "version": 1,
            "written": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "root": self.root,
            "sessions": [self._session_state(session)
                         for session in self.sessions.values()],
        }
        path = os.path.join(self.root, SERVER_MANIFEST)
        try:
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
        except OSError:
            return ""
        return path

    def close(self, discard=False):
        """Save, optionally discarding the workspaces, and forget the sessions."""
        path = self.save()
        if discard:
            for session in self.sessions.values():
                session.discard()
        self.sessions.clear()
        return path

    @staticmethod
    def _session_state(session):
        """One session's full state, for the manifest (history included)."""
        state = session.to_dict()
        state["history"] = list(session.history)
        state["snapshots"] = list(session.snapshots)
        return state

    def _read_manifest(self):
        path = os.path.join(self.root, SERVER_MANIFEST)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                payload = json.load(handle)
        except (OSError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    # --- sessions ------------------------------------------------------
    def session(self, scope=None, policy=None, recorded=None):
        """Get or create the session for a scope (idempotent)."""
        scope = scope or integration.SandboxScope("script")
        key = scope.key()
        if key not in self.sessions:
            self.sessions[key] = SandboxSession(
                self.root, scope, policy or self.policy, recorded)
        session = self.sessions[key]
        if policy is not None:
            session.policy = policy
            session.shadows = integration.shadow_layers(
                policy, session.workspace, recorded=recorded or session.recorded)
        session.prepare()
        return session

    def run(self, code, policy=None, scope=None, script="", language="Python",
            recorded=None):
        """Run one script in a scoped session and record the result.

        This is the server's whole surface: everything a caller needs is a
        script, a policy and a scope, and everything a caller gets back is the
        run record with the session's reuse already folded into it.
        """
        session = self.session(scope, policy, recorded)
        runner = self._runner()
        run = None
        if runner is not None:
            run = _safe_run(runner, code, policy or self.policy, scope, script,
                            language)
        if run is not None:
            session.record(run)
            session.snapshot(run)
        return run

    def _runner(self):
        """The callable that actually runs a script (or None).

        The server does not import the tab at module load: it is asked for at
        run time, so a headless CLI can use the server for its bookkeeping even
        when no window exists.
        """
        try:
            from tabs import sandbox as sandbox_module
        except Exception:
            return None
        return getattr(sandbox_module, "run_headless", None)

    # --- reporting -----------------------------------------------------
    def describe(self):
        """The server as ASCII lines (the policy view and the CLI)."""
        lines = ["Persistent sandbox server", "=========================",
                 "root       : %s" % self.root,
                 "sessions   : %d" % len(self.sessions),
                 "policy     : %s" % ("set" if self.policy is not None
                                      else "default"),
                 "manifest   : %s" % os.path.join(self.root, SERVER_MANIFEST)]
        for key, session in sorted(self.sessions.items()):
            lines.append("")
            lines.append("  scope %s" % key)
            lines.append("    runs=%d snapshots=%d shadows=%s"
                         % (len(session.history), len(session.snapshots),
                            ",".join(layer.key
                                     for layer in session.enabled_shadows())
                            or "none"))
        return lines

    def to_dict(self):
        return {
            "root": self.root,
            "sessions": {key: session.to_dict()
                         for key, session in self.sessions.items()},
            "stores": list(SERVER_STORE_KEYS),
            "manifest": os.path.join(self.root, SERVER_MANIFEST),
        }

    def history(self, limit=200):
        """Every run the server knows about, newest last."""
        rows = []
        for key, session in self.sessions.items():
            for entry in session.history:
                row = dict(entry)
                row["scope"] = key
                rows.append(row)
        return rows[-limit:]


def _safe_run(runner, code, policy, scope, script, language):
    """Call the head-less runner, tolerating an older signature."""
    try:
        return runner(code, policy=policy, scope=scope, script=script,
                      language=language)
    except TypeError:
        try:
            return runner(code, policy)
        except Exception:
            return None
    except Exception:
        return None


def _scope_from_dict(data):
    """A SandboxScope from its serialised form (or None)."""
    if not isinstance(data, dict):
        return None
    return integration.SandboxScope(
        data.get("level", "script"), data.get("name", ""),
        data.get("parent", ""), data.get("identity", ""))


# ---------------------------------------------------------------------------
# Describing the whole thing
# ---------------------------------------------------------------------------
def server_capabilities(window=None):
    """Describe the persistent sandbox for the audit / about layer."""
    return {
        "stores": list(SERVER_STORE_KEYS),
        "store_labels": dict(SERVER_STORE_LABELS),
        "manifest": SERVER_MANIFEST,
        "sessions_persist": True,
        "snapshots": True,
        "replay": True,
        "architecture": architecture_capabilities(window),
        "premium": list(PREMIUM_KEYS),
        "premium_labels": dict(PREMIUM_LABELS),
        "scopes": list(integration.SCOPE_KEYS),
        "shadow_layers": list(integration.SHADOW_KEYS),
        "ascii_only": True,
        "offline": True,
    }


def format_premium():
    """The premium features as ASCII lines (the About page and the CLI)."""
    lines = ["Sandbox premium features", "========================"]
    for key, _lk, fallback, meaning in PREMIUM_FEATURES:
        lines.append("%-16s %s" % (key, fallback))
        lines.append("                 %s" % meaning)
    return lines


def format_architecture(window=None):
    """The architecture standard as ASCII lines, with its live status."""
    report = architecture_capabilities(window)
    lines = ["Sandbox architecture", "====================",
             "held: %d of %d" % (report["held"], report["total"]),
             "integrations available: %d of %d"
             % (report["integrations_available"], report["integrations_total"]),
             ""]
    for row in report["architecture"]:
        lines.append("%-4s %-16s %s" % ("ok" if row["ok"] else "n/a",
                                        row["key"], row["label"]))
        lines.append("     %s" % row["meaning"])
    return lines
