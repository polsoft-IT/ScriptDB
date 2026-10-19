# -*- coding: utf-8 -*-
"""Integration between the sandbox and the rest of the terminal.

The sandbox is not an island: a run is worth doing because its result reaches
the other tabs, and a tab is worth opening because it can hand the sandbox
something to run. This module is that wiring, and nothing else: it holds no
detection logic, no policy and no UI, so it can be read as the answer to one
question — *what does the sandbox exchange with each module, and through which
call?*

    INTEGRATIONS   the eight modules and what each one exchanges
    bridges        one function per module, each returning plain data
    SandboxScope   what a sandbox is bound to (a script, a module, a project,
                   a debugger session)
    ShadowLayer    a substituted view of one resource (files, env, network)

Every bridge follows the same three rules, which is what makes the module
hot-pluggable rather than a pile of special cases:

    1. it never imports the module it bridges to at module load, only inside
       the call, so one missing tab cannot break the sandbox;
    2. it returns plain data (dicts, lists, strings), never a widget, so the
       caller decides what to do with it;
    3. it never raises: a bridge that cannot reach its module returns an empty
       result and says why, because a sandbox that fails to run because the
       Dash tab is closed would be worse than useless.

The standard the rest of the terminal follows applies here too: ASCII-only,
zero-dependency, cross-platform, sandbox-safe (this module never executes a
line of the script it is describing) and API + CLI + GUI reachable.
"""

import datetime as dt
import os

# ---------------------------------------------------------------------------
# Which modules the sandbox talks to, and over what
# ---------------------------------------------------------------------------
# (key, label key, fallback, direction, what is exchanged)
#
# `direction` is `read` when the sandbox consumes something from the module,
# `write` when it produces something for it, and `both` when the exchange goes
# each way. It is here rather than in the code because a reader should be able
# to see the whole shape of the integration in one screen.
INTEGRATIONS = (
    ("debugger", "sandbox_int_debugger", "Debugger", "both",
     "a run can be started as a debugger session, and a stopped session can "
     "be replayed in the sandbox; breakpoints and watches carry over"),
    ("analyzer", "sandbox_int_analyzer", "Script analyser", "read",
     "the analyser's findings become the sandbox's pre-run risk scan, so a "
     "script is judged once and acted on twice"),
    ("audit", "sandbox_int_audit", "Audit", "write",
     "every run is announced on the audit bus and its security report is "
     "offered to the audit as a section"),
    ("diff", "sandbox_int_diff", "Diff", "write",
     "two sandbox runs can be compared: their reports, their findings and "
     "their resource use"),
    ("search", "sandbox_int_search", "Search", "read",
     "the sandbox's report files are indexed by content, so a finding can be "
     "found again without knowing which run produced it"),
    ("runner", "sandbox_int_runner", "Script runner", "both",
     "a script the runner holds can be handed to the sandbox, and a sandbox "
     "run can be handed back as a script to run normally"),
    ("docs", "sandbox_int_docs", "Documentation", "write",
     "the sandbox's own capabilities become a documentation article, so the "
     "sandbox documents itself from the same tables it enforces"),
    ("reports", "sandbox_int_reports", "Reports", "write",
     "every audit report is written as an artefact the Reports tab can index, "
     "preview and search"),
)

INTEGRATION_KEYS = tuple(key for key, _lk, _f, _d, _w in INTEGRATIONS)
INTEGRATION_LABELS = {key: fallback for key, _lk, fallback, _d, _w in INTEGRATIONS}
INTEGRATION_DIRECTIONS = {key: direction
                          for key, _lk, _f, direction, _w in INTEGRATIONS}

# The two directions an exchange can go, spelled out for a reader of a report.
DIRECTIONS = ("read", "write", "both")

# The name every sandbox artefact starts with. Keeping it in one place is what
# lets the Reports tab, the Search index and the diff pick the sandbox's own
# files out of a directory without a naming convention being repeated in four
# modules.
ARTEFACT_PREFIX = "sandbox_"
AUDIT_PREFIX = "sandbox_audit_"


# ---------------------------------------------------------------------------
# The scope a sandbox is bound to
# ---------------------------------------------------------------------------
# (key, label key, fallback, what it means for isolation and reuse)
#
# The scope answers "how long does this sandbox live, and what shares it?".
# The four levels are cumulative: a project sandbox may contain module
# sandboxes, a module sandbox may contain script sandboxes, and a debugger
# session may borrow any of them. What changes is what is reused between runs —
# the workspace, the cache, the environment — and so what a leak from one run
# can reach.
SCOPES = (
    ("script", "sandbox_scope_script", "Per script", "one run, one throw-away "
     "workspace; nothing is reused and nothing survives"),
    ("module", "sandbox_scope_module", "Per module", "runs of the same module "
     "share a workspace cache, so a header or a fixture is prepared once"),
    ("project", "sandbox_scope_project", "Per project", "runs of the same "
     "project share a workspace and an environment, and are isolated from "
     "other projects"),
    ("session", "sandbox_scope_session", "Per debugger session", "the run is "
     "bound to a live debugger session: its breakpoints, watches and recorded "
     "flow travel into the sandbox with it"),
)

SCOPE_KEYS = tuple(key for key, _lk, _f, _d in SCOPES)
SCOPE_LABELS = {key: fallback for key, _lk, fallback, _d in SCOPES}
SCOPE_MEANINGS = {key: meaning for key, _lk, _f, meaning in SCOPES}

# The level each scope sits at. A higher level may *contain* a lower one, never
# the reverse, which is what makes the reuse rules one-directional and simple.
SCOPE_LEVELS = {"script": 0, "module": 1, "project": 2, "session": 3}


class SandboxScope(object):
    """What a sandbox is bound to, and what that means for what is reused.

    A plain holder: it can be built by the UI, carried in a report, or
    serialised, and it never touches Qt or the filesystem. What it decides is
    only *identity* — the key a workspace, a cache or a session is filed under —
    which is what lets a policy be applied per script, per module, per project
    or per debugger session without the policy engine learning about any of it.
    """

    def __init__(self, level="script", name="", parent="", identity=""):
        self.level = level if level in SCOPE_KEYS else "script"
        self.name = name or ""
        # The scope this one sits inside (a script inside a module inside a
        # project). Empty means "top level".
        self.parent = parent or ""
        # An extra discriminator, such as a debugger session id. Kept separate
        # from `name` because two sessions may share a project but not state.
        self.identity = identity or ""

    @property
    def level_rank(self):
        """How broad this scope is (0 = narrowest)."""
        return SCOPE_LEVELS.get(self.level, 0)

    def contains(self, other):
        """True when `other` is at or below this scope's level."""
        if other is None:
            return False
        return self.level_rank >= SCOPE_LEVELS.get(other.level, 0)

    def key(self):
        """The stable key this scope's state is filed under.

        Only the parts that are *set* take part, so an unset identity does not
        produce a key with a trailing separator that would file every run under
        a different name.
        """
        parts = [self.level]
        if self.name:
            parts.append(str(self.name))
        if self.parent:
            parts.append(str(self.parent))
        if self.identity:
            parts.append(str(self.identity))
        return "/".join(parts)

    def to_dict(self):
        return {"level": self.level, "name": self.name,
                "parent": self.parent, "identity": self.identity,
                "key": self.key(), "reuses": self.reuses()}

    def reuses(self):
        """What this scope carries from one run to the next.

        This is the honest description of the risk a broader scope carries: a
        per-script sandbox reuses nothing, a per-project one reuses both a
        workspace and an environment, so a run in it can observe what an
        earlier run left behind. That is the point of the feature and also its
        hazard, and the report says both.
        """
        reuse = []
        if self.level_rank >= SCOPE_LEVELS["module"]:
            reuse.append("workspace cache")
        if self.level_rank >= SCOPE_LEVELS["project"]:
            reuse.append("environment")
        if self.level == "session":
            reuse.append("debugger session")
        return reuse

    def describe(self):
        """The scope as ASCII lines (the run report and the policy view)."""
        lines = ["Sandbox scope", "-------------",
                 "level      : %s (%s)" % (self.level,
                                           SCOPE_LABELS.get(self.level, self.level)),
                 "name       : %s" % (self.name or "-"),
                 "parent     : %s" % (self.parent or "-"),
                 "identity   : %s" % (self.identity or "-"),
                 "key        : %s" % self.key(),
                 "reuses     : %s" % (", ".join(self.reuses()) or "nothing"),
                 "means      : %s" % SCOPE_MEANINGS.get(self.level, "")]
        return lines


def scope_for(level="script", name="", parent="", identity=""):
    """Build a scope (a thin factory so callers never need the class)."""
    return SandboxScope(level, name, parent, identity)


def scope_from_script(script):
    """The scope implied by a script entry from the main window.

    The main window already knows which module and project a script belongs to,
    so the sandbox does not have to be told twice: this reads what the entry
    has and falls back to a per-script scope when it says nothing.
    """
    if not isinstance(script, dict):
        return SandboxScope("script", str(script or ""))
    level = script.get("scope") or script.get("level") or "script"
    name = script.get("name") or script.get("script") or ""
    parent = script.get("module") or script.get("category") or ""
    project = script.get("project") or ""
    scope = SandboxScope(level, name, parent, project)
    return scope


# ---------------------------------------------------------------------------
# The shadow layers
# ---------------------------------------------------------------------------
# (key, label key, fallback, what the run sees instead)
#
# A shadow layer is a substituted view of one resource. The point is that a run
# which *believes* it is writing to /etc or reading a real API sees something
# consistent, so it runs to completion and produces a usable report, while the
# machine outside is untouched. It is the difference between refusing a run and
# letting it run in a world of its own.
SHADOW_LAYERS = (
    ("shadow_fs", "sandbox_shadow_fs", "Shadow filesystem",
     "reads are served from a copy, writes land in the copy, and the copy is "
     "discarded; the real tree is never touched"),
    ("shadow_env", "sandbox_shadow_env", "Shadow environment",
     "the run gets its own environment variables; setting or clearing one "
     "cannot reach the terminal or the machine"),
    ("shadow_network", "sandbox_shadow_network", "Shadow network",
     "connections resolve to a local responder that answers from a recorded "
     "or synthetic response, so a run that needs a reply gets one offline"),
    ("shadow_replay", "sandbox_shadow_replay", "Replay",
     "a recorded session is replayed into the run, so a past failure can be "
     "reproduced without repeating the cause"),
    ("shadow_snapshot", "sandbox_shadow_snapshot", "Snapshots",
     "the run's filesystem and outputs are captured as a snapshot that can be "
     "diffed against another run"),
)

SHADOW_KEYS = tuple(key for key, _lk, _f, _d in SHADOW_LAYERS)
SHADOW_LABELS = {key: fallback for key, _lk, fallback, _d in SHADOW_LAYERS}
SHADOW_MEANINGS = {key: meaning for key, _lk, _f, meaning in SHADOW_LAYERS}


class ShadowLayer(object):
    """A substituted view of one resource, as a plain description.

    The layer does not itself copy anything: it describes *what* is substituted
    and *where the substitute lives*, and the run gets that description through
    its environment, exactly as it gets the policy. Keeping the description and
    the mechanism apart is what lets a report say "shadow FS at <dir>" and lets
    a test assert the same string without a filesystem being touched.
    """

    def __init__(self, key, root="", recorded=None, enabled=True):
        self.key = key if key in SHADOW_KEYS else "shadow_fs"
        self.root = root or ""
        # For the replay and network layers: what the substitute answers with,
        # which is a recorded session or a synthetic responder.
        self.recorded = recorded or {}
        self.enabled = bool(enabled)

    def to_dict(self):
        return {"key": self.key, "enabled": self.enabled, "root": self.root,
                "recorded": sorted(self.recorded) if self.recorded else [],
                "means": SHADOW_MEANINGS.get(self.key, "")}

    def describe(self):
        """The layer as one ASCII line."""
        return "%-16s %s%s" % (
            self.key, "on" if self.enabled else "off",
            (" (%s)" % self.root) if self.root else "")


def shadow_layers(policy=None, workspace="", recorded=None):
    """The shadow layers a policy asks for, as descriptions (never None).

    A layer that the policy does not request comes back disabled rather than
    absent, so a report shows the full set and a reader can see what was *not*
    substituted as easily as what was.
    """
    def asked(name):
        return bool(getattr(policy, name, False)) if policy is not None else False

    return [
        ShadowLayer(key, root=os.path.join(workspace, "_shadow_%s" % key[7:])
                    if workspace else "",
                    recorded=recorded or {}, enabled=asked(key))
        for key in SHADOW_KEYS
    ]


def format_shadows(layers):
    """The shadow layers as ASCII lines (the run report)."""
    lines = ["Shadow layers", "-------------"]
    if not layers:
        return lines + ["(none)"]
    for layer in layers:
        lines.append(layer.describe() if hasattr(layer, "describe")
                     else ShadowLayer("shadow_fs").describe())
    active = [layer for layer in layers
              if getattr(layer, "enabled", False)]
    lines.append("active     : %d of %d" % (len(active), len(layers)))
    return lines


# ---------------------------------------------------------------------------
# The bridges
# ---------------------------------------------------------------------------
def _safe(call, fallback=None):
    """Run a bridge body, returning `fallback` if anything at all goes wrong.

    One wrapper rather than a try/except in every bridge: the rule is the same
    for all of them, and stating it once is what keeps a bridge short enough to
    read.
    """
    try:
        return call()
    except Exception:
        return fallback


# --- debugger --------------------------------------------------------------
def debugger_session(tab=None, name="", scope=None):
    """What the sandbox needs to run a script as a debugger session.

    Reads the live session when there is one and otherwise reports that the
    debugger is not open, rather than failing: a run without breakpoints is a
    valid run, and the report says the difference.
    """
    def build():
        if tab is None:
            return {"available": False,
                    "reason": "the debugger tab is not open"}
        session = getattr(tab, "session", None)
        if session is None:
            return {"available": False,
                    "reason": "the debugger has no session"}
        describe = getattr(session, "summary", None)
        summary = describe() if callable(describe) else None
        return {"available": True, "name": name or "session",
                "breakpoints": list(getattr(session, "breakpoints", []) or []),
                "watches": list(getattr(session, "watches", []) or []),
                "summary": summary or {},
                "scope": (scope or SandboxScope("session", name)).to_dict(),
                "replay": bool(getattr(session, "recorded", None))}
    return _safe(build, {"available": False, "reason": "debugger bridge failed"})


def debugger_replay(snapshots, workspace="", policy=None):
    """A replay plan: the recorded state a run should start from.

    Returns plain data rather than replaying anything itself, because the replay
    happens inside the child (only it can be re-entered) and the parent's job is
    to say what should be replayed.
    """
    def build():
        entries = list(snapshots or [])
        return {
            "frames": len(entries),
            "workspace": workspace,
            "shadow": [layer.to_dict()
                       for layer in shadow_layers(policy, workspace,
                                                  {"replay": len(entries)})],
            "steps": [{"index": index,
                       "name": str((entry or {}).get("name", ""))[:80]}
                      for index, entry in enumerate(entries[:200])],
        }
    return _safe(build, {"frames": 0, "steps": []})


# --- analyzer --------------------------------------------------------------
def analyzer_findings(tab=None, script=None):
    """The analyser's findings for a script, as the sandbox's pre-run scan.

    The two tabs judge the same script from different angles: the analyser
    statically, the sandbox dynamically. Rather than scanning twice, the sandbox
    reads the analyser's result when it is there and falls back to its own
    static scan when it is not.
    """
    def build():
        if tab is None:
            return {"available": False,
                    "reason": "the analyser tab is not open",
                    "findings": []}
        api = getattr(tab, "describe", None)
        described = api() if callable(api) else {}
        report = ""
        builder = getattr(tab, "build_report", None)
        if callable(builder):
            report = builder("json") or ""
        return {"available": True,
                "findings": (described or {}).get("findings", []),
                "summary": described or {},
                "report_bytes": len(report),
                "script": (script or {}).get("name", "")
                if isinstance(script, dict) else str(script or "")}
    return _safe(build, {"available": False, "findings": [],
                         "reason": "analyzer bridge failed"})


# --- audit -----------------------------------------------------------------
def audit_event(run, bus=None, event="runtime_error"):
    """Announce a sandbox run on the audit bus and return what was sent.

    The event name is chosen by the risk, not by the caller: a run that detected
    something dangerous is a `runtime_error`, because that is what the audit's
    own automatic-audit table reacts to, while a clean run is a plain
    announcement. Picking it here rather than at the call site is what lets the
    sandbox be wired to the audit with one line that cannot drift.
    """
    def build():
        risk = getattr(run, "risk", "clean")
        findings = list(getattr(run, "findings", None) or [])
        name = event
        if risk == "dangerous" or findings:
            name = "runtime_error"
        payload = {
            "where": getattr(run, "script", "") or "-",
            "detail": "%s (risk=%s, %d finding(s))"
                      % (getattr(run, "status", ""), risk, len(findings)),
            "risk": risk,
            "findings": [item.get("key") for item in findings],
            "report": "sandbox_security",
        }
        reached = 0
        if bus is not None and hasattr(bus, "emit"):
            reached = bus.emit(name, **payload)
        return {"event": name, "payload": payload, "reached": reached}
    return _safe(build, {"event": event, "payload": {}, "reached": 0})


def audit_section(run, findings=None):
    """The sandbox's security report as an audit section (name, rows).

    The shape is the one the audit's plugin interface uses, so the sandbox can
    be registered as a plugin and contribute a section without the audit
    learning anything about sandboxes.
    """
    def build():
        from tabs import sandbox_audit
        items = (sandbox_audit.detect(run) if findings is None else findings)
        rows = [{"key": "risk",
                 "value": sandbox_audit.risk_level(items)}]
        rows.append({"key": "findings", "value": str(len(items))})
        for item in items[:20]:
            rows.append({"key": item.get("key", "?"),
                         "value": item.get("detail", "")})
        if getattr(run, "limits_hit", None):
            rows.append({"key": "limits_hit",
                         "value": ", ".join(run.limits_hit)})
        return ("sandbox", rows)
    return _safe(build, ("sandbox", []))


class SandboxAuditPlugin(object):
    """The sandbox as a hot-pluggable audit plugin.

    It follows the audit's plugin contract exactly (`activate`, `deactivate`,
    `section`), so registering it needs no change in either module: the audit
    already knows how to call a plugin, and the sandbox already knows how to
    describe a run. That symmetry is the whole point of the plugin interface.
    """

    name = "sandbox"

    def __init__(self, run=None):
        self.run = run
        self._unsubscribe = None

    def activate(self, api):
        self.api = api
        bus = getattr(api, "bus", None)
        if bus is not None and hasattr(bus, "subscribe"):
            self._unsubscribe = bus.subscribe("sandbox_run", self._on_run)
        return True

    def deactivate(self):
        if callable(self._unsubscribe):
            self._unsubscribe()
            self._unsubscribe = None
        return True

    def _on_run(self, event, **payload):
        # Keep the last run so a later `section()` has something to describe.
        self.last_event = payload
        return event

    def section(self):
        if self.run is None:
            return None
        return audit_section(self.run)


# --- diff ------------------------------------------------------------------
def diff_runs(left, right, findings_left=None, findings_right=None):
    """Compare two sandbox runs: what changed between them.

    Deliberately built from the reports rather than from the raw runs, so two
    runs made on different machines (or in different sessions, months apart) can
    still be compared: a report is the artefact, and an artefact is what
    survives.
    """
    def build():
        from tabs import sandbox_audit

        def snapshot(run, findings):
            items = (sandbox_audit.detect(run) if findings is None else findings)
            monitor = dict(getattr(run, "monitor", None) or {})
            return {
                "script": getattr(run, "script", ""),
                "status": getattr(run, "status", ""),
                "risk": sandbox_audit.risk_level(items),
                "findings": sorted(item.get("key") for item in items),
                "limits_hit": sorted(getattr(run, "limits_hit", None) or []),
                "elapsed": float(monitor.get("elapsed", 0) or 0),
                "cpu": float(monitor.get("cpu", 0) or 0),
                "ram": int(monitor.get("ram", 0) or 0),
                "calls": int(monitor.get("calls", 0) or 0),
            }

        before = snapshot(left, findings_left)
        after = snapshot(right, findings_right)
        appeared = sorted(set(after["findings"]) - set(before["findings"]))
        vanished = sorted(set(before["findings"]) - set(after["findings"]))
        new_limits = sorted(set(after["limits_hit"])
                            - set(before["limits_hit"]))
        return {
            "left": before,
            "right": after,
            "findings_appeared": appeared,
            "findings_resolved": vanished,
            "limits_new": new_limits,
            "changed": {
                key: {"from": before[key], "to": after[key]}
                for key in ("status", "risk", "elapsed", "cpu", "ram", "calls")
                if before[key] != after[key]
            },
        }
    return _safe(build, {"left": {}, "right": {}, "changed": {}})


def format_diff(comparison):
    """A run comparison as ASCII lines (the Diff tab and the report)."""
    if not comparison or not comparison.get("left"):
        return ["(nothing to compare)"]
    lines = ["Sandbox run comparison", "======================="]
    left, right = comparison["left"], comparison["right"]
    lines.append("%-12s %-24s %s" % ("field", str(left.get("script", ""))[:24],
                                     str(right.get("script", ""))[:24]))
    for key in ("status", "risk", "elapsed", "cpu", "ram", "calls"):
        if left.get(key) != right.get(key):
            lines.append("%-12s %-24s %s" % (key, left.get(key), right.get(key)))
    for label, key in (("appeared", "findings_appeared"),
                       ("resolved", "findings_resolved"),
                       ("new limits", "limits_new")):
        values = comparison.get(key) or []
        if values:
            lines.append("%-12s %s" % (label, ", ".join(values)))
    if len(lines) == 3:
        lines.append("(the two runs are identical in every measured way)")
    return lines


# --- search ----------------------------------------------------------------
def search_query(run=None, key="security"):
    """The query that finds a run's report in the search engine.

    A report file is found by its own name, so the work is naming the artefact
    the same way in both modules: this returns the query and the expected name
    together, so a caller cannot build one without the other.
    """
    def build():
        marker = "%s%s" % (AUDIT_PREFIX, key)
        return {
            "query": marker,
            "artefact": marker,
            "prefix": ARTEFACT_PREFIX,
            "hint": "search the reports directory for '%s'" % marker,
            "script": getattr(run, "script", "") if run is not None else "",
        }
    return _safe(build, {"query": ARTEFACT_PREFIX, "artefact": ARTEFACT_PREFIX})


def search_reports(needle="", root=""):
    """Search the sandbox's own report artefacts for `needle`.

    Uses the search engine when it is importable and otherwise does its own
    read, because a sandbox that could not report its own history when the
    Search tab is missing would be the wrong kind of dependency.
    """
    def build():
        directory = root or ""
        if not directory or not os.path.isdir(directory):
            return {"root": directory, "hits": [], "engine": "none"}
        try:
            from tabs import search_engine
            documents = []
            for name in sorted(os.listdir(directory)):
                if not name.startswith(ARTEFACT_PREFIX):
                    continue
                path = os.path.join(directory, name)
                try:
                    with open(path, "r", encoding="utf-8",
                              errors="replace") as handle:
                        documents.append((name, handle.read(), "text"))
                except OSError:
                    continue
            hits = search_engine.search_text(documents, needle) \
                if needle else []
            return {"root": directory, "hits": hits, "engine": "search_engine"}
        except Exception:
            return {"root": directory, "hits": [], "engine": "unavailable"}
    return _safe(build, {"root": root, "hits": [], "engine": "failed"})


# --- runner ----------------------------------------------------------------
def runner_handoff(run, keep_output=True):
    """A finished sandbox run as something the runner can re-run.

    The point of the handoff is that a sandbox run is a *candidate*: once a
    script has behaved under the policy, the user often wants to run it for real.
    The handoff carries the script name and the command shape, not the code, so
    the runner still owns where the code comes from.
    """
    def build():
        return {
            "name": getattr(run, "script", "") or "sandboxed",
            "language": getattr(run, "language", "") or "Python",
            "status": getattr(run, "status", ""),
            "risk": getattr(run, "risk", "clean"),
            "output": (getattr(run, "output", "") or "") if keep_output else "",
            "exit_code": getattr(run, "exit_code", None),
            "command": ["sandbox-run", getattr(run, "script", "") or "-"],
            "reason": "ran in the sandbox; re-run here to run it for real",
        }
    return _safe(build, {"name": "sandboxed", "command": []})


def runner_script(script):
    """A runner entry as a script the sandbox can execute (or None).

    Accepts the shapes the runner and the script database use (a dict with
    `code`, or an object with `text`), because the handoff goes in both
    directions and neither side should have to learn the other's spelling.
    """
    def build():
        if script is None:
            return None
        if isinstance(script, dict):
            code = script.get("code") or script.get("text") or ""
            return {"name": script.get("name", "(runner)"),
                    "category": script.get("category", "Python"),
                    "code": code}
        code = getattr(script, "code", None) or getattr(script, "text", None)
        if code is None:
            return None
        return {"name": getattr(script, "name", "(runner)"),
                "category": getattr(script, "category", "Python"),
                "code": code}
    return _safe(build, None)


# --- docs ------------------------------------------------------------------
def documentation_article(capabilities=None):
    """The sandbox's own capabilities as a documentation article.

    Generated from the capability tables rather than written by hand, so the
    documentation cannot describe a sandbox that no longer exists: add a rule
    and it appears in the article, remove one and it disappears.
    """
    def build():
        from tabs import sandbox_audit, sandbox_monitor
        caps = capabilities or {}
        sections = []
        sections.append(("overview", [
            "The sandbox runs a script in a separate process, in a throw-away",
            "workspace, under an explicit policy, with a hard time budget."]))
        monitor = caps.get("monitor") or sandbox_monitor.monitor_capabilities()
        sections.append(("monitoring", [
            "Monitored: " + ", ".join(monitor.get("monitor", []))]))
        sections.append(("limits", [
            "Bounded: " + ", ".join(monitor.get("limits", []))]))
        audit = caps.get("audit") or sandbox_audit.audit_capabilities()
        sections.append(("detection", [
            "Detected: " + ", ".join(audit.get("behaviours", []))]))
        sections.append(("reports", [
            "Reports: " + ", ".join(audit.get("reports", []))]))
        sections.append(("integration", [
            "Talks to: " + ", ".join(INTEGRATION_KEYS)]))
        sections.append(("scopes", [
            "Scopes: " + ", ".join(SCOPE_KEYS)]))
        sections.append(("shadow", [
            "Shadow layers: " + ", ".join(SHADOW_KEYS)]))
        return {"title": "Sandbox", "sections": sections,
                "line_count": sum(len(body) for _name, body in sections)}
    return _safe(build, {"title": "Sandbox", "sections": []})


# --- reports ---------------------------------------------------------------
def artefact_name(key, run=None, fmt="ascii", stamp=None):
    """The file name one audit report is written under.

    One function rather than a format string at each call site: the Reports tab
    finds sandbox artefacts by prefix and the diff finds them by stamp, and both
    would break silently if one caller spelled the name differently.
    """
    when = stamp or dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    script = ""
    if run is not None:
        script = str(getattr(run, "script", "") or "")
    slug = "".join(character if character.isalnum() else "_"
                   for character in script)[:32].strip("_")
    parts = [AUDIT_PREFIX.rstrip("_"), key]
    if slug:
        parts.append(slug)
    parts.append(when)
    return "%s.%s" % ("_".join(parts), fmt)


def report_metadata(path):
    """What the Reports tab needs to index one sandbox artefact."""
    def build():
        from tabs import reports_tab
        return reports_tab.report_metadata(
            reports_tab.Report(path) if hasattr(reports_tab, "Report") else path)
    return _safe(build, {"path": path})


# ---------------------------------------------------------------------------
# The integration as a whole
# ---------------------------------------------------------------------------
def integration_matrix(window=None):
    """``[{key, label, direction, exchange, available}]`` for every module.

    `available` says whether the other module could actually be reached, which
    is what turns the matrix from a description of intent into a live status
    report: a reader can see at a glance which tabs the sandbox is wired to
    *right now*.
    """
    probes = {
        "debugger": lambda: _module_present("tabs", "debugger"),
        "analyzer": lambda: _module_present("tabs", "analyzer"),
        "audit": lambda: _module_present("tabs", "audit"),
        "search": lambda: _module_present("tabs", "search_engine"),
        "docs": lambda: _module_present("tabs", "docs_engine"),
        "reports": lambda: _module_present("tabs", "reports_tab"),
        # Diff and the runner are not modules of their own: they are tabs the
        # main window builds, so the only honest probe for them is the window.
        "diff": lambda: _tab_present(window, "diff_tab"),
        "runner": lambda: _tab_present(window, "runner_tab", "run_tab"),
    }
    rows = []
    for key, label_key, fallback, direction, exchange in INTEGRATIONS:
        probe = probes.get(key)
        available = bool(probe and _safe(probe, False))
        rows.append({"key": key, "label_key": label_key, "label": fallback,
                     "direction": direction, "exchange": exchange,
                     "available": available})
    return rows


def _module_present(package, name):
    """True when a terminal module can be imported right now."""
    try:
        __import__("%s.%s" % (package, name))
        return True
    except Exception:
        return False


def _tab_present(window, *names):
    """True when the main window has one of the named tabs built.

    A tab that exists only as an attribute (Diff, the runner) is probed this
    way rather than by import: there is no module to import, and reporting the
    integration missing because the window has not been built yet would be a
    statement about the caller rather than about the sandbox.
    """
    if window is None:
        return False
    return any(getattr(window, name, None) is not None for name in names)


def integration_capabilities(window=None):
    """Describe the integration for the audit / about layer."""
    return {
        "integrations": list(INTEGRATION_KEYS),
        "directions": list(DIRECTIONS),
        "scopes": list(SCOPE_KEYS),
        "shadow_layers": list(SHADOW_KEYS),
        "artefact_prefix": ARTEFACT_PREFIX,
        "audit_prefix": AUDIT_PREFIX,
        "ascii_only": True,
        "hot_pluggable": True,
        "zero_dependency": True,
        "cross_platform": True,
        "sandbox_safe": True,
        "plugin_ready": True,
        "artefact_driven": True,
        "frontends": ["api", "cli", "gui"],
        "available": {row["key"]: row["available"]
                      for row in integration_matrix(window)},
    }
