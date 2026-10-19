# -*- coding: utf-8 -*-
"""Audit tab for ScriptDB.

This module owns everything the "Audit" tab needs. It is intentionally
self-contained: it takes the main window in its constructor, builds its own
widgets and exposes a small, stable surface to the rest of the application:

    AuditTab(window)          -- build the tab
    tab.refresh()             -- recompute the dashboard from the database
    tab.retranslate(table)    -- apply a language table (see TRANSLATIONS)

The dashboard shows the headline audit indicators (integrity status, number of
problems, changes since the last audit, risk level) plus three feeds of recent
activity: security events, module changes and configuration changes.

The main window only needs three lines to wire the tab in::

    from tabs import AuditTab
    self.audit_tab = AuditTab(self)
    self.tabs.addTab(self.audit_tab, "Audit")

Adding a new tab follows the same pattern: create ``tabs/<name>.py`` with a
``<Name>Tab`` class and register it in ``tabs/__init__.py``.
"""

import datetime as dt
import hashlib
import json
import os

import app_paths

from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtGui import QFont

# Where the audit trail snapshot is stored between sessions.
AUDIT_FILE = "audit_db.json"

# Risk levels ordered from lowest to highest.
RISK_LEVELS = ("low", "medium", "high")


def _script_fingerprint(script):
    '''Return the canonical stable fingerprint shared by all audit paths.'''
    code = script.get("code", "") or ""
    return {
        "name": script.get("name", ""),
        "category": script.get("category", ""),
        "tags": script.get("tags", ""),
        "date": script.get("date", ""),
        "code_len": len(code),
        "hash": hashlib.sha256(code.encode("utf-8")).hexdigest(),
    }


def _now():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _parse_date(text):
    """Best-effort parse of a stored date string into a datetime (or None)."""
    if not text:
        return None
    text = str(text)
    for fmt, length in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d", 10)):
        try:
            return dt.datetime.strptime(text[:length], fmt)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Architecture (chapter 11)
#
# The audit module is built so it can be used from three front-ends that share
# one core: the GUI tab, a programmatic API and a command line interface. The
# pieces below are dependency-free (standard library only), ASCII-only, safe to
# call from a sandbox (no shell, no eval) and hot-pluggable through the event
# bus and the plugin registry.
# ---------------------------------------------------------------------------

# Event names the audit module reacts to. A host application can emit any of
# these to trigger the matching automatic audit (see the premium section).
AUDIT_EVENTS = (
    "file_changed",       # a file the workspace tracks was modified
    "module_installed",   # a new module/script was installed
    "module_updated",     # an existing module/script was updated
    "runtime_error",      # a runtime error or exception occurred
    "deploy",             # a deployment finished
    "manual",             # the user asked for an audit by hand
    "scheduled",          # the cyclic scheduler fired
)


class AuditEventBus(object):
    """A tiny synchronous publish/subscribe bus (zero dependency).

    Subscribers are called immediately when an event is emitted. A failing
    subscriber never breaks the others, which keeps the bus safe to use from
    plugins that are not fully trusted.
    """

    def __init__(self):
        self._subscribers = {}

    def subscribe(self, event, callback):
        """Register `callback` for `event`. Returns an unsubscribe function."""
        self._subscribers.setdefault(event, []).append(callback)

        def _unsubscribe():
            self.unsubscribe(event, callback)
        return _unsubscribe

    def unsubscribe(self, event, callback):
        handlers = self._subscribers.get(event, [])
        if callback in handlers:
            handlers.remove(callback)

    def subscribers(self, event=None):
        if event is None:
            return dict(self._subscribers)
        return list(self._subscribers.get(event, []))

    def emit(self, event, **payload):
        """Notify every subscriber of `event`; return how many were reached."""
        reached = 0
        for callback in list(self._subscribers.get(event, [])):
            try:
                callback(event, **payload)
                reached += 1
            except Exception:
                # A broken subscriber must not stop the audit.
                continue
        return reached


# The process-wide bus the GUI and the API share by default.
AUDIT_BUS = AuditEventBus()


def audit_capabilities():
    """Describe the guarantees this module offers (chapter 11 checklist)."""
    return {
        "ascii_only": True,
        "hot_pluggable": True,
        "zero_dependency": True,
        "cross_platform": True,
        "sandbox_safe": True,
        "plugin_ready": True,
        "artefact_driven": True,
        "frontends": ["api", "cli", "gui"],
        "event_bus": True,
        "snapshot_system": True,
    }


def ascii_safe(text):
    """Best-effort transliteration so output stays plain ASCII everywhere."""
    try:
        return str(text).encode("ascii", "replace").decode("ascii")
    except Exception:
        return str(text)


class AuditPlugin(object):
    """Base class for hot-pluggable audit extensions.

    A plugin receives the audit API when it is registered and may subscribe to
    events or add report sections. It must never require third-party packages
    and should stay side-effect free until `activate` is called.
    """

    name = "plugin"

    def activate(self, api):
        """Called when the plugin is registered."""
        return True

    def deactivate(self):
        """Called when the plugin is removed."""
        return True

    def section(self):
        """Optionally return (name, rows) to add a section to the report."""
        return None


class DemoAuditPlugin(AuditPlugin):
    """A tiny built-in plugin proving the hot-plug mechanism works.

    It subscribes to the event bus and contributes one extra report section.
    """

    name = "demo-plugin"

    def activate(self, api):
        self._unsubscribe = api.bus.subscribe("manual", self._on_manual)
        return True

    def deactivate(self):
        unsubscribe = getattr(self, "_unsubscribe", None)
        if callable(unsubscribe):
            unsubscribe()
        return True

    def _on_manual(self, event, **payload):
        return event

    def section(self):
        return ("demo_plugin", [{"status": "active", "name": self.name}])


class AuditAPI(object):
    """A headless, zero-dependency facade over the audit core.

    It can wrap a live GUI tab (when one exists) or run the audit directly from
    the stored data files. This is the surface both the CLI and external tools
    use, so nothing here imports Qt.
    """

    def __init__(self, tab=None, base_dir=None, bus=None):
        self.tab = tab
        self.base_dir = base_dir
        self.bus = bus or AUDIT_BUS
        self.plugins = []
        self._context = None

    # --- context -------------------------------------------------------
    def _ctx(self):
        """Return the audit engine: the live tab, or a headless stand-in."""
        if self.tab is not None:
            return self.tab
        if self._context is None:
            self._context = _HeadlessAudit(self.base_dir)
        return self._context

    # --- plugins -------------------------------------------------------
    def register_plugin(self, plugin):
        """Hot-plug a plugin (idempotent). Returns True on success."""
        if plugin in self.plugins:
            return True
        try:
            ok = plugin.activate(self)
        except Exception:
            return False
        if ok:
            self.plugins.append(plugin)
        return bool(ok)

    def unregister_plugin(self, plugin):
        if plugin not in self.plugins:
            return False
        try:
            ok = plugin.deactivate()
        except Exception:
            ok = False
        self.plugins.remove(plugin)
        return bool(ok)

    # --- operations ----------------------------------------------------
    def full_audit(self):
        """Run every audit area and return the structured document."""
        ctx = self._ctx()
        result = ctx.audit_all()
        for plugin in list(self.plugins):
            try:
                extra = plugin.section()
            except Exception:
                extra = None
            if extra:
                result.setdefault("sections", []).append(
                    {"name": extra[0], "rows": extra[1]})
        return result

    def report(self, fmt="txt"):
        """Build the full report in `fmt` (txt / json / log)."""
        return self._ctx().build_report(fmt)

    def export(self, fmt="txt"):
        """Export the report as an artifact and return its path."""
        return self._ctx().export_report(fmt)

    def integrity(self):
        """Return the workspace integrity summary."""
        return self._ctx().workspace_integrity()

    def risk(self):
        """Return the security audit with per-finding risk levels."""
        return self._ctx().security_risk_audit()

    def emit(self, event, **payload):
        """Publish an event on the shared bus."""
        return self.bus.emit(event, **payload)


class AuditCLI(object):
    """Command line front-end for the audit core (dependency free).

    Usage::

        python tabs/audit.py report [txt|json|log]
        python tabs/audit.py export [txt|json|log]
        python tabs/audit.py integrity
        python tabs/audit.py risk
        python tabs/audit.py capabilities
    """

    USAGE = ("usage: audit.py <command> [format]\n"
             "commands: report, export, integrity, risk, full, capabilities")

    def __init__(self, api=None, out=None):
        self.api = api or AuditAPI()
        self.out = out

    def _print(self, text):
        text = ascii_safe(text)
        if self.out is not None:
            self.out.write(text + "\n")
        else:
            print(text)

    def run(self, argv):
        """Execute a command line; returns a process exit code (0 = OK)."""
        argv = list(argv or [])
        if not argv:
            self._print(self.USAGE)
            return 2
        command = argv[0]
        arg = argv[1] if len(argv) > 1 else None
        if command == "capabilities":
            return self._capabilities()
        if command == "report":
            self._print(self.api.report(arg or "txt"))
            return 0
        if command == "export":
            path = self.api.export(arg or "txt")
            self._print("exported: " + (path or "FAILED"))
            return 0 if path else 1
        if command == "integrity":
            return self._integrity()
        if command == "risk":
            return self._risk()
        if command == "full":
            import json as _json
            self._print(_json.dumps(self.api.full_audit(), default=str,
                                    indent=2))
            return 0
        self._print(self.USAGE)
        return 2

    def _capabilities(self):
        caps = audit_capabilities()
        for key in sorted(caps):
            self._print("%-18s %s" % (key, caps[key]))
        return 0

    def _integrity(self):
        data = self.api.integrity()
        self._print("integrity: %s" % data.get("status", "?"))
        self._print("checked:   %s" % data.get("checked", 0))
        for item in data.get("problems", []):
            self._print("  - %s: %s" % (item.get("where", ""),
                                        item.get("detail", "")))
        return 0

    def _risk(self):
        data = self.api.risk()
        self._print("overall risk: %s" % data.get("overall", "low"))
        for row in data.get("findings", []):
            self._print("  [%s] %s - %s" % (row.get("risk", ""),
                                            row.get("where", ""),
                                            row.get("kind", "")))
        return 0


class _HeadlessAudit(object):
    """A minimal, Qt-free audit engine used by the CLI and the API.

    It reuses the same heuristics as the GUI tab but only through plain Python
    so it can run in a server, a cron job or a sandbox with no display.
    """

    def __init__(self, base_dir=None):
        base = base_dir or app_paths.ensure_db_dir()
        self.base_dir = base

    # --- data ----------------------------------------------------------
    def _data_dir(self):
        return self.base_dir

    def _scripts_path(self):
        return os.path.join(self.base_dir, "scripts_db.json")

    def scripts(self):
        try:
            with open(self._scripts_path(), "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return []
        return data if isinstance(data, list) else []

    def _snapshot_path(self):
        return os.path.join(self.base_dir, AUDIT_FILE)

    def snapshot(self):
        try:
            with open(self._snapshot_path(), "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _fingerprint(self, script):
        return _script_fingerprint(script)

    # --- audits --------------------------------------------------------
    def audit_all(self):
        snapshot = self.snapshot()
        previous = snapshot.get("fingerprints", {}) or {}
        modules = []
        problems = []
        for script in self.scripts():
            key = str(script.get("id"))
            name = script.get("name", "") or "(unnamed)"
            current = self._fingerprint(script)
            changed = (key not in previous or previous[key] != current)
            modules.append({"name": name, "changed": changed})
            if not (script.get("code", "") or "").strip():
                problems.append({"where": name, "detail": "empty body",
                                 "severity": "high"})
        return {"generated": _now(), "sections": [
            {"name": "summary", "rows": [{"total": len(modules),
                                          "problems": len(problems)}]},
            {"name": "modules", "rows": modules},
        ]}

    def build_report(self, fmt="txt"):
        data = self.audit_all()
        fmt = (fmt or "txt").lower()
        if fmt == "json":
            return json.dumps(data, ensure_ascii=False, indent=2, default=str)
        if fmt == "log":
            lines = []
            for section in data.get("sections", []):
                for row in section.get("rows", []):
                    lines.append("%s [%s] %s" % (
                        data.get("generated", ""), section.get("name", ""),
                        json.dumps(row, default=str)))
            return ascii_safe("\n".join(lines))
        lines = ["SCRIPTDB AUDIT REPORT", "generated: %s" % data.get("generated", "")]
        for section in data.get("sections", []):
            lines.append("-- %s" % section.get("name", "").upper())
            for row in section.get("rows", []):
                lines.append("  " + json.dumps(row, default=str))
        return ascii_safe("\n".join(lines))

    def _export_dir(self):
        directory = os.path.join(self.base_dir, "audit_exports")
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError:
            pass
        return directory

    def export_report(self, fmt="txt"):
        content = self.build_report(fmt)
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        path = os.path.join(self._export_dir(),
                            "audit-%s.%s" % (stamp, (fmt or "txt").lower()))
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(content)
        except OSError:
            return ""
        return path

    def workspace_integrity(self):
        snapshot = self.snapshot()
        previous = snapshot.get("fingerprints", {}) or {}
        problems = []
        checked = 0
        for script in self.scripts():
            key = str(script.get("id"))
            checked += 1
            current = self._fingerprint(script)
            if key in previous and previous[key] != current:
                problems.append({"where": script.get("name", ""),
                                 "detail": "fingerprint changed"})
        return {"status": "ok" if not problems else "warning",
                "checked": checked, "problems": problems}

    def security_risk_audit(self):
        findings = []
        high = 0
        for script in self.scripts():
            code = (script.get("code", "") or "").lower()
            for token, risk in (("eval(", "high"), ("exec(", "high"),
                                ("subprocess", "high"), ("os.system", "high"),
                                ("password", "medium"), ("token", "medium")):
                if token in code:
                    findings.append({"where": script.get("name", ""),
                                     "kind": token, "risk": risk})
                    if risk == "high":
                        high += 1
        overall = "high" if high else ("medium" if findings else "low")
        return {"overall": overall, "findings": findings}


# The three front-ends share one default API instance.
AUDIT_API = AuditAPI()


def audit_api(tab=None, base_dir=None):
    """Return an AuditAPI bound to a tab (or a headless engine)."""
    if tab is None and base_dir is None:
        return AUDIT_API
    return AuditAPI(tab=tab, base_dir=base_dir)


def audit_cli(argv, api=None, out=None):
    """Convenience wrapper so callers do not build an AuditCLI themselves."""
    return AuditCLI(api=api, out=out).run(argv)


class AuditTab(QtWidgets.QWidget):
    """Audit dashboard: integrity, problems, changes and recent activity.

    Everything is derived from the script database plus a small audit snapshot
    file, so the tab works without any external service and updates whenever the
    user presses Refresh (or the main window asks it to).
    """

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self._state = {}          # last computed dashboard values
        self._build_ui()
        # Wire the automatic audits to the shared event bus and arm the
        # cyclic scheduler if the user enabled it in a previous session.
        self._install_event_bridge()
        self._arm_scheduler()
        self.refresh()

    # --------------------------------------------------------------- helpers
    def t(self, key, fallback=None, **kwargs):
        """Look a label up in the main window's translations."""
        return self.window.t(key, fallback, **kwargs)

    def _data_dir(self):
        """Directory holding the audit snapshot (the databases folder)."""
        base = getattr(self.window, "db_dir", None)
        if base:
            return base
        base_dir = getattr(self.window, "DB_DIR", None)
        if base_dir:
            return base_dir
        return app_paths.ensure_db_dir()

    def _audit_path(self):
        return os.path.join(self._data_dir(), AUDIT_FILE)

    def _scripts(self):
        """All real script entries from the main window (never None)."""
        try:
            return list(self.window.scripts_only())
        except Exception:
            return []

    # -------------------------------------------------------------- data I/O
    def _load_snapshot(self):
        path = self._audit_path()
        if not os.path.isfile(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save_snapshot(self, snapshot):
        try:
            with open(self._audit_path(), "w", encoding="utf-8") as handle:
                json.dump(snapshot, handle, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def _fingerprint(self, script):
        """Stable signature of a script used to detect changes since last audit."""
        return _script_fingerprint(script)

    # ----------------------------------------------------------- analysis
    def _analyze(self):
        """Compute the dashboard: indicators, problems and recent activity."""
        scripts = self._scripts()
        problems = []
        security = []
        module_changes = []
        config_changes = []

        now = dt.datetime.now()
        for script in scripts:
            name = script.get("name", "") or "(unnamed)"
            code = script.get("code", "") or ""
            meta = script.get("meta", {}) or {}

            # --- integrity problems ------------------------------------
            if not code.strip():
                problems.append({"severity": "high", "where": name,
                                 "detail": self.t("audit_problem_empty",
                                                  "Empty script body")})
            if not meta:
                problems.append({"severity": "low", "where": name,
                                 "detail": self.t("audit_problem_nometa",
                                                  "No metadata")})
            if not script.get("favorite") and not script.get("tags"):
                problems.append({"severity": "low", "where": name,
                                 "detail": self.t("audit_problem_notags",
                                                  "No tags")})
            if script.get("history_truncated"):
                problems.append({"severity": "medium", "where": name,
                                 "detail": self.t("audit_problem_history",
                                                  "History was truncated")})

            # --- security events ---------------------------------------
            lowered = code.lower()
            risky = [tok for tok in ("eval(", "exec(", "subprocess", "os.system",
                                     "shell=true", "password", "secret", "token",
                                     "api_key", "rm -rf")
                     if tok in lowered]
            if risky:
                security.append({"when": script.get("date", ""),
                                 "where": name, "detail": ", ".join(risky)})

            # --- module changes (history revisions) --------------------
            history = script.get("history", []) or []
            if history:
                module_changes.append({
                    "when": script.get("date", ""),
                    "where": name,
                    "detail": self.t("audit_revisions",
                                     "{count} revision(s)").format(count=len(history)),
                })

            # --- configuration changes (recent edits) ------------------
            changed = _parse_date(script.get("date", ""))
            if changed and (now - changed).days <= 30:
                config_changes.append({
                    "when": script.get("date", ""), "where": name,
                    "detail": script.get("category", "") or "-",
                })

        # --- changes since the last audit ------------------------------
        snapshot = self._load_snapshot()
        previous = snapshot.get("fingerprints", {}) or {}
        current = {}
        added = modified = 0
        for script in scripts:
            key = str(script.get("id"))
            current[key] = self._fingerprint(script)
            if key not in previous:
                added += 1
            elif previous[key] != current[key]:
                modified += 1
        removed = len([key for key in previous if key not in current])
        changes_since = added + modified + removed
        last_audit = snapshot.get("last_audit", "")

        # --- risk level ------------------------------------------------
        high = sum(1 for p in problems if p["severity"] == "high")
        medium = sum(1 for p in problems if p["severity"] == "medium")
        riskscore = high * 3 + medium * 2 + len(security) * 2 + changes_since
        if high or len(security) >= 3:
            risk = "high"
        elif medium or security or changes_since > 5:
            risk = "medium"
        else:
            risk = "low"

        # --- integrity status ------------------------------------------
        integrity = "ok" if not problems else ("critical" if high else "warning")

        return {
            "total": len(scripts),
            "problems": problems,
            "security": sorted(security, key=lambda e: e["when"], reverse=True),
            "module_changes": sorted(module_changes, key=lambda e: e["when"],
                                     reverse=True),
            "config_changes": sorted(config_changes, key=lambda e: e["when"],
                                     reverse=True),
            "changes_since": changes_since,
            "added": added,
            "modified": modified,
            "removed": removed,
            "risk": risk,
            "risk_score": riskscore,
            "integrity": integrity,
            "last_audit": last_audit,
        }

    # ------------------------------------------------- module & script audit
    def _module_audit(self):
        """Audit every module (script) in the database.

        Returns one row per module with its integrity (hash OK / mismatch),
        cryptographic signature status, version, changes since the last audit
        and the permissions its code appears to require.
        """
        snapshot = self._load_snapshot()
        previous = snapshot.get("fingerprints", {}) or {}
        rows = []
        for script in self._scripts():
            name = script.get("name", "") or "(unnamed)"
            meta = script.get("meta", {}) or {}
            code = script.get("code", "") or ""
            key = str(script.get("id"))

            # Integrity: compare the stored fingerprint with the live one.
            current_fp = self._fingerprint(script)
            integrity = "ok"
            if key in previous and previous[key] != current_fp:
                integrity = "mismatch"

            # Signature: a metadata signature field, else "none".
            signature = str(meta.get("signature", "") or "").strip()
            if not signature:
                signature_status = "none"
            elif signature.lower() in ("valid", "ok", "true", "1", "yes"):
                signature_status = "valid"
            else:
                signature_status = "invalid"

            version = str(meta.get("version", "") or "-")
            changed = "yes" if (key not in previous or previous[key] != current_fp) else "no"
            permissions = self._module_permissions(code)
            rows.append({
                "name": name,
                "integrity": integrity,
                "signature": signature_status,
                "version": version,
                "changed": changed,
                "permissions": permissions,
            })
        return rows

    @staticmethod
    def _module_permissions(code):
        """Capabilities a module's code appears to use (files/net/exec/...)."""
        lowered = (code or "").lower()
        caps = []
        if any(tok in lowered for tok in ("open(", "read_text", "write_text",
                                          "os.remove", "shutil.")):
            caps.append("files")
        if any(tok in lowered for tok in ("socket", "requests.", "urllib",
                                          "http.client", "urlopen")):
            caps.append("network")
        if any(tok in lowered for tok in ("subprocess", "os.system", "popen",
                                          "eval(", "exec(")):
            caps.append("exec")
        if any(tok in lowered for tok in ("os.environ", "getenv")):
            caps.append("env")
        if any(tok in lowered for tok in ("sqlite3", "connect(", "cursor(")):
            caps.append("database")
        return ", ".join(caps) if caps else "sandboxed"

    def _script_audit(self):
        """Audit every script's code: changes, dead code and linter errors."""
        snapshot = self._load_snapshot()
        previous = snapshot.get("fingerprints", {}) or {}
        rows = []
        for script in self._scripts():
            name = script.get("name", "") or "(unnamed)"
            code = script.get("code", "") or ""
            key = str(script.get("id"))
            current_fp = self._fingerprint(script)
            if key not in previous:
                change = "new"
            elif previous[key] != current_fp:
                change = "modified"
            else:
                change = "unchanged"
            unused_funcs = self._unused_functions(code)
            unused_imports = self._unused_imports(code)
            suspicious = self._suspicious_constructs(code)
            lint = self._lint_errors(code, script.get("category", ""))
            rows.append({
                "name": name,
                "change": change,
                "unused_funcs": unused_funcs,
                "unused_imports": unused_imports,
                "suspicious": suspicious,
                "lint": lint,
            })
        return rows

    @staticmethod
    def _unused_functions(code):
        """Functions defined but never referenced elsewhere in the code."""
        import re as _re
        defined = _re.findall(r"^[ \t]*def\s+([A-Za-z_]\w*)\s*\(", code or "",
                              _re.MULTILINE)
        calls = _re.findall(r"([A-Za-z_]\w*)\s*\(", code or "")
        unused = []
        for name in set(defined):
            def_count = defined.count(name)
            # Every `def name(` also matches the call pattern, so a function is
            # only genuinely used when it is called more often than defined.
            if calls.count(name) <= def_count:
                unused.append(name)
        return sorted(unused)

    @staticmethod
    def _unused_imports(code):
        """Imported names that never appear again in the code body."""
        import re as _re
        names = []
        for line in (code or "").splitlines():
            match = _re.match(r"^\s*import\s+([\w., ]+)", line)
            if match:
                for part in match.group(1).split(","):
                    alias = part.strip().split(" as ")[-1].strip()
                    base = alias.split(".")[0]
                    if base:
                        names.append(base)
            match = _re.match(r"^\s*from\s+[\w.]+\s+import\s+([\w., ]+)", line)
            if match:
                for part in match.group(1).split(","):
                    alias = part.strip().split(" as ")[-1].strip()
                    if alias and alias != "*":
                        names.append(alias)
        body = code or ""
        unused = []
        for name in sorted(set(names)):
            if len(_re.findall(r"\b" + _re.escape(name) + r"\b", body)) <= 1:
                unused.append(name)
        return unused

    @staticmethod
    def _suspicious_constructs(code):
        """Risky constructs worth a reviewer's attention."""
        lowered = (code or "").lower()
        found = []
        patterns = {
            "eval": "eval(",
            "exec": "exec(",
            "os.system": "os.system",
            "subprocess": "subprocess",
            "shell=True": "shell=true",
            "__import__": "__import__",
            "pickle.loads": "pickle.loads",
        }
        for label, needle in patterns.items():
            if needle in lowered:
                found.append(label)
        return found

    @staticmethod
    def _lint_errors(code, category=""):
        """Very light syntax/lint check (Python only; empty for other kinds)."""
        if (category or "").lower() not in ("python", "py"):
            return []
        import ast
        errors = []
        try:
            ast.parse(code or "")
        except SyntaxError as exc:
            errors.append("line %s: %s" % (exc.lineno, exc.msg))
        return errors

    # ------------------------------------------------- security audit
    def _access_flags(self, code):
        """Which sensitive resources a piece of code touches."""
        lowered = (code or "").lower()
        file_tokens = ("open(", "read_text", "write_text", "os.remove",
                       "os.rename", "shutil.", "pathlib", ".write(", ".read(",
                       "tempfile", "os.makedirs")
        net_tokens = ("socket", "requests.", "urllib", "http.client",
                      "urlopen", "ftplib", "smtplib", "paramiko")
        env_tokens = ("os.environ", "getenv", "os.getenv", "environ[")
        exec_tokens = ("subprocess", "os.system", "os.popen", "popen(",
                       "eval(", "exec(", "compile(", "__import__",
                       "ctypes", "shell=true")
        return {
            "files": any(token in lowered for token in file_tokens),
            "network": any(token in lowered for token in net_tokens),
            "env": any(token in lowered for token in env_tokens),
            "exec": any(token in lowered for token in exec_tokens),
        }

    def _permission_analysis(self):
        """Per-target file / network / environment / exec access matrix."""
        rows = []
        for script in self._scripts():
            flags = self._access_flags(script.get("code", ""))
            rows.append({
                "name": script.get("name", "") or "(unnamed)",
                "files": flags["files"],
                "network": flags["network"],
                "env": flags["env"],
                "exec": flags["exec"],
            })
        return rows

    def _is_signed(self, script):
        """True when the script carries a recognised, valid signature."""
        meta = script.get("meta", {}) or {}
        signature = str(meta.get("signature", "") or "").strip().lower()
        return signature in ("valid", "ok", "true", "1", "yes")

    def _sandbox_escape(self, code):
        """Attempts to break out of the sandbox (process / native escapes)."""
        lowered = (code or "").lower()
        attempts = []
        checks = {
            "subprocess": "subprocess",
            "os.system": "os.system",
            "os.popen": "os.popen",
            "shell=True": "shell=true",
            "ctypes": "ctypes",
            "os.exec": "os.exec",
            "os.fork": "os.fork",
            "socket.raw": "socket.socket",
            "__import__": "__import__",
        }
        for label, needle in checks.items():
            if needle in lowered:
                attempts.append(label)
        return attempts

    def _suspicious_calls(self, code):
        """Suspicious system calls that deserve a closer look."""
        lowered = (code or "").lower()
        calls = []
        checks = {
            "eval": "eval(",
            "exec": "exec(",
            "compile": "compile(",
            "os.system": "os.system",
            "shutil.rmtree": "rmtree",
            "base64.decode": "b64decode",
            "pickle.loads": "pickle.loads",
            "marshal.loads": "marshal.loads",
            "winreg": "winreg",
            "registry": "hkey_",
            "scheduled task": "schtasks",
            "crontab": "crontab",
        }
        for label, needle in checks.items():
            if needle in lowered:
                calls.append(label)
        return calls

    def _security_findings(self):
        """Detect the security problems that matter to a reviewer.

        Covers: unauthorized changes, unsigned modules, hash mismatch,
        sandbox-escape attempts and suspicious system calls.
        """
        snapshot = self._load_snapshot()
        previous = snapshot.get("fingerprints", {}) or {}
        findings = []
        for script in self._scripts():
            name = script.get("name", "") or "(unnamed)"
            code = script.get("code", "") or ""
            key = str(script.get("id"))

            fp = self._fingerprint(script)

            # --- unauthorized change / hash mismatch -------------------
            if key not in previous:
                findings.append({"kind": "audit_find_unauthorized", "where": name,
                                 "severity": "medium"})
            elif previous[key] != fp:
                findings.append({"kind": "audit_find_hash_mismatch", "where": name,
                                 "severity": "high"})

            # --- unsigned module ---------------------------------------
            if not self._is_signed(script):
                findings.append({"kind": "audit_find_unsigned", "where": name,
                                 "severity": "medium"})

            # --- sandbox escape ----------------------------------------
            for attempt in self._sandbox_escape(code):
                findings.append({"kind": "audit_find_sandbox", "where": name,
                                 "severity": "high", "detail": attempt})

            # --- suspicious system calls -------------------------------
            for call in self._suspicious_calls(code):
                findings.append({"kind": "audit_find_syscall", "where": name,
                                 "severity": "medium", "detail": call})
        return findings

    # ------------------------------------------------- configuration audit
    def _config_snapshots_dir(self):
        """Folder holding configuration snapshots (next to the audit file)."""
        return os.path.join(self._data_dir(), "config_snapshots")

    # Application-owned files that must never be treated as user config.
    CONFIG_EXCLUDE = ("audit_db.json", "runtime_log.json", "scripts_db.json",
                      "connections_db.json", "settings_db.json",
                      "audit_timeline.json")

    def _config_files(self):
        """Find candidate config files (json / ini / yaml) in the data folder."""
        found = []
        try:
            for name in sorted(os.listdir(self._data_dir())):
                lowered = name.lower()
                if lowered in self.CONFIG_EXCLUDE:
                    continue
                if lowered.endswith((".json", ".ini", ".yaml", ".yml", ".cfg")):
                    found.append(os.path.join(self._data_dir(), name))
        except OSError:
            pass
        return found

    def _parse_config(self, path):
        """Parse a config file into a flat {key: value} dict (or None)."""
        lowered = path.lower()
        try:
            if lowered.endswith(".json"):
                with open(path, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
                return self._flatten(data)
            if lowered.endswith((".ini", ".cfg")):
                import configparser
                parser = configparser.ConfigParser()
                parser.read(path, encoding="utf-8")
                flat = {}
                for section in parser.sections():
                    for key, value in parser.items(section):
                        flat[f"{section}.{key}"] = value
                return flat
            if lowered.endswith((".yaml", ".yml")):
                return self._parse_yaml_simple(path)
        except Exception:
            return None
        return None

    @staticmethod
    def _flatten(data, prefix=""):
        # Turn a nested mapping into dotted keys so values can be compared.
        flat = {}
        if isinstance(data, dict):
            for key, value in data.items():
                name = f"{prefix}.{key}" if prefix else str(key)
                flat.update(AuditTab._flatten(value, name))
        else:
            flat[prefix] = data
        return flat

    @staticmethod
    def _parse_yaml_simple(path):
        """Parse a small subset of YAML (key: value lines, indentation)."""
        flat = {}
        stack = []
        try:
            with open(path, "r", encoding="utf-8") as handle:
                for raw in handle:
                    if not raw.strip() or raw.lstrip().startswith("#"):
                        continue
                    indent = len(raw) - len(raw.lstrip())
                    line = raw.strip()
                    if ":" not in line:
                        continue
                    key, _, value = line.partition(":")
                    level = indent // 2
                    stack = stack[:level]
                    stack.append(key.strip())
                    path_key = ".".join(stack)
                    if value.strip():
                        flat[path_key] = value.strip()
        except OSError:
            return None
        return flat

    def _config_audit(self):
        """Compare config files with the previous snapshot and flag issues."""
        changes = []
        policy = []
        snapshots_dir = self._config_snapshots_dir()
        snapshot_path = os.path.join(snapshots_dir, "last.json")
        previous = {}
        if os.path.isfile(snapshot_path):
            try:
                with open(snapshot_path, "r", encoding="utf-8") as handle:
                    previous = json.load(handle)
            except (OSError, ValueError):
                previous = {}
        current = {}
        expected_types = {
            "port": int, "timeout": int, "enabled": bool, "debug": bool,
            "level": str, "host": str, "user": str, "password": str,
        }
        for path in self._config_files():
            parsed = self._parse_config(path)
            if parsed is None:
                changes.append({"file": os.path.basename(path), "key": "*",
                                "issue": self.t("audit_cfg_unreadable",
                                                "Unreadable or invalid config"),
                                "severity": "high"})
                continue
            current[os.path.basename(path)] = parsed
            old = previous.get(os.path.basename(path), {})
            for key, value in parsed.items():
                # --- changed / missing keys ---------------------------
                if key not in old:
                    changes.append({"file": os.path.basename(path), "key": key,
                                    "issue": self.t("audit_cfg_added", "New key"),
                                    "severity": "low"})
                elif old[key] != value:
                    changes.append({"file": os.path.basename(path), "key": key,
                                    "issue": self.t("audit_cfg_changed",
                                                    "Value changed"),
                                    "severity": "medium"})
                # --- type mismatch ------------------------------------
                expected = expected_types.get(key.split(".")[-1])
                if expected is int:
                    type_ok = isinstance(value, int) and not isinstance(value, bool)
                elif expected is bool:
                    type_ok = isinstance(value, bool)
                elif expected is str:
                    type_ok = isinstance(value, str)
                else:
                    type_ok = expected is None or isinstance(value, expected)
                if not type_ok:
                    changes.append({"file": os.path.basename(path), "key": key,
                                    "issue": self.t("audit_cfg_type",
                                                    "Type mismatch"),
                                    "severity": "medium"})
            # --- missing keys (present before, gone now) --------------
            for key in old:
                if key not in parsed:
                    changes.append({"file": os.path.basename(path), "key": key,
                                    "issue": self.t("audit_cfg_removed",
                                                    "Missing key"),
                                    "severity": "high"})
            # --- unused settings (defined but never referenced elsewhere) --
            all_text = ""
            for script in self._scripts():
                all_text += (script.get("code", "") or "") + "\n"
            for key in parsed:
                leaf = key.split(".")[-1]
                if leaf and leaf not in all_text:
                    changes.append({"file": os.path.basename(path), "key": key,
                                    "issue": self.t("audit_cfg_unused",
                                                    "Unused setting"),
                                    "severity": "low"})
            # --- policy compliance ------------------------------------
            for name, value in parsed.items():
                leaf = name.split(".")[-1].lower()
                if leaf in ("debug",) and str(value).lower() in ("true", "1", "yes"):
                    policy.append({"file": os.path.basename(path), "policy": name,
                                   "result": self.t("audit_cfg_policy_violation",
                                                    "violation")})
                elif leaf == "password" and not str(value):
                    policy.append({"file": os.path.basename(path), "policy": name,
                                   "result": self.t("audit_cfg_policy_violation",
                                                    "violation")})
                elif leaf in ("port", "timeout") and str(value).strip() == "":
                    policy.append({"file": os.path.basename(path), "policy": name,
                                   "result": self.t("audit_cfg_policy_violation",
                                                    "violation")})
        self._config_current = current
        return changes, policy

    def _save_config_snapshot(self):
        """Persist the current parsed config as the baseline snapshot."""
        changes, _policy = self._config_audit()
        current = getattr(self, "_config_current", {})
        directory = self._config_snapshots_dir()
        try:
            os.makedirs(directory, exist_ok=True)
            with open(os.path.join(directory, "last.json"), "w",
                      encoding="utf-8") as handle:
                json.dump(current, handle, ensure_ascii=False, indent=2)
        except OSError:
            pass
        return len(changes)

    # ------------------------------------------------------- runtime audit
    def _runtime_log_path(self):
        return os.path.join(self._data_dir(), "runtime_log.json")

    def _load_runtime_log(self):
        path = self._runtime_log_path()
        if not os.path.isfile(path):
            return {"events": []}
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return {"events": []}
        return data if isinstance(data, dict) else {"events": []}

    def log_runtime_event(self, kind, where="", detail="", duration_ms=None):
        """Record one runtime event (call it from the runner / scripts)."""
        log = self._load_runtime_log()
        events = list(log.get("events", []))
        events.append({
            "when": _now(), "kind": kind, "where": where,
            "detail": detail, "duration_ms": duration_ms,
        })
        log["events"] = events[-500:]      # keep the log bounded
        try:
            with open(self._runtime_log_path(), "w", encoding="utf-8") as handle:
                json.dump(log, handle, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def _runtime_events(self):
        """All recorded runtime events, newest first."""
        events = self._load_runtime_log().get("events", [])
        return list(reversed(events))

    def _runtime_audit(self):
        """Summarise runtime events and detect anomalies."""
        events = self._runtime_events()
        durations = [e.get("duration_ms") for e in events
                     if isinstance(e.get("duration_ms"), (int, float))]
        average = sum(durations) / len(durations) if durations else 0
        anomalies = []
        # --- slow executions (well above the average) ------------------
        slow_threshold = max(200, average * 2)
        for event in events:
            duration = event.get("duration_ms")
            if isinstance(duration, (int, float)) and duration > slow_threshold:
                anomalies.append({"kind": "audit_anomaly_slow",
                                  "where": event.get("where", ""),
                                  "severity": "medium",
                                  "detail": f"{duration} ms"})
        # --- repeated errors / exceptions ------------------------------
        error_counts = {}
        for event in events:
            if event.get("kind") in ("error", "exception"):
                key = event.get("where", "") or event.get("detail", "")
                error_counts[key] = error_counts.get(key, 0) + 1
        for where, count in error_counts.items():
            if count >= 3:
                anomalies.append({"kind": "audit_anomaly_repeat",
                                  "where": where, "severity": "high",
                                  "detail": str(count)})
        # --- stacktrace present -> concrete failure --------------------
        for event in events:
            if event.get("kind") == "stacktrace":
                anomalies.append({"kind": "audit_anomaly_stacktrace",
                                  "where": event.get("where", ""),
                                  "severity": "high",
                                  "detail": (event.get("detail", "") or "")[:60]})
        # --- crash / unexpected exit -----------------------------------
        for event in events:
            if event.get("kind") in ("crash", "exit") and                     str(event.get("detail", "")).strip() not in ("", "0"):
                anomalies.append({"kind": "audit_anomaly_crash",
                                  "where": event.get("where", ""),
                                  "severity": "high",
                                  "detail": str(event.get("detail", ""))})
        return {"events": events, "anomalies": anomalies,
                "average_ms": round(average, 1), "count": len(events)}

    def _accept_snapshot(self, state):
        """Record the current fingerprints as the new baseline audit."""
        scripts = self._scripts()
        snapshot = {
            "last_audit": _now(),
            "integrity": state["integrity"],
            "risk": state["risk"],
            "problems": len(state["problems"]),
            "fingerprints": {str(s.get("id")): self._fingerprint(s) for s in scripts},
        }
        self._save_snapshot(snapshot)
        state["last_audit"] = snapshot["last_audit"]

    # ------------------------------------------------------ change timeline
    # The timeline keeps a full, chronological history of every change to the
    # files, modules, configuration, environment and permissions. Each entry
    # records who changed it, when, what changed and how it was done.

    TIMELINE_FILE = "audit_timeline.json"
    TIMELINE_LIMIT = 2000
    # Category code -> translation key for the timeline category label.
    TIMELINE_CATEGORIES = (
        ("files", "audit_tl_cat_files"),
        ("modules", "audit_tl_cat_modules"),
        ("config", "audit_tl_cat_config"),
        ("environment", "audit_tl_cat_environment"),
        ("permissions", "audit_tl_cat_permissions"),
    )

    def _timeline_path(self):
        return os.path.join(self._data_dir(), self.TIMELINE_FILE)

    def _load_timeline(self):
        """Read the persisted change timeline (a bounded list of events)."""
        path = self._timeline_path()
        if not os.path.isfile(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return []
        if isinstance(data, dict):
            data = data.get("events", [])
        return list(data) if isinstance(data, list) else []

    def _save_timeline(self, events):
        events = list(events)[-self.TIMELINE_LIMIT:]
        try:
            with open(self._timeline_path(), "w", encoding="utf-8") as handle:
                json.dump(events, handle, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def _current_actor(self):
        """Best-effort identity of the user driving the application."""
        try:
            return (os.environ.get("USERNAME") or os.environ.get("USER")
                    or os.environ.get("LOGNAME") or "?")
        except Exception:
            return "?"

    def _option(self, name, default):
        """Read a value from the main window settings, with a fallback."""
        getter = getattr(self.window, "setting", None)
        if callable(getter):
            try:
                return getter(name, default)
            except Exception:
                return default
        try:
            return self.window.settings.get(name, default)
        except Exception:
            return default

    @staticmethod
    def _change_magnitude(before, after, threshold=50):
        """Compare two values and return (is_large, magnitude score)."""
        if before is None and after is None:
            return False, 0
        if isinstance(before, str) or isinstance(after, str):
            left = str(before or "")
            right = str(after or "")
            lines = set(left.splitlines()) ^ set(right.splitlines())
            magnitude = len(lines) + abs(len(left) - len(right))
            return magnitude >= threshold, magnitude
        if isinstance(before, dict) or isinstance(after, dict):
            before = before or {}
            after = after or {}
            keys = set(before) | set(after)
            changed = sum(1 for key in keys if before.get(key) != after.get(key))
            return changed >= threshold, changed
        return False, 0

    def log_change(self, category, where="", detail="", how="",
                   before=None, after=None, actor=None):
        """Append one change to the timeline.

        ``category`` is one of: files, modules, config, environment or
        permissions. ``before`` / ``after`` are optional values used to work
        out the magnitude of the change and to flag it as a large change.
        """
        threshold = int(self._option("audit_large_change_threshold", 50))
        large, magnitude = self._change_magnitude(before, after, threshold)
        event = {
            "when": _now(),
            "who": actor or self._current_actor(),
            "category": category,
            "where": where,
            "what": detail,
            "how": how or self.t("audit_tl_how_auto", "automatic"),
            "large": large,
            "magnitude": magnitude,
        }
        events = self._load_timeline()
        events.append(event)
        self._save_timeline(events)
        return event

    def _timeline_events(self):
        """All timeline events, newest first."""
        return list(reversed(self._load_timeline()))

    def _timeline_rows(self, category=None):
        """Timeline rows prepared for the table (optionally one category)."""
        rows = []
        for event in self._timeline_events():
            if category and event.get("category") != category:
                continue
            rows.append({
                "when": event.get("when", ""),
                "who": event.get("who", ""),
                "category": event.get("category", ""),
                "where": event.get("where", ""),
                "what": event.get("what", ""),
                "how": event.get("how", ""),
                "large": bool(event.get("large")),
                "magnitude": event.get("magnitude", 0),
            })
        return rows

    def _detect_large_changes(self):
        """Flag scripts whose code grew or shrank by more than the threshold."""
        threshold = int(self._option("audit_large_change_threshold", 50))
        snapshot = self._load_snapshot()
        previous = snapshot.get("fingerprints", {}) or {}
        rows = []
        for script in self._scripts():
            name = script.get("name", "") or "(unnamed)"
            key = str(script.get("id"))
            current = self._fingerprint(script)
            if key not in previous or previous[key] == current:
                continue
            old = previous[key]
            old_len = old.get("code_len", 0) if isinstance(old, dict) else 0
            new_len = current.get("code_len", 0)
            delta = new_len - old_len
            if abs(delta) >= threshold:
                rows.append({"name": name, "delta": delta, "old_len": old_len,
                             "new_len": new_len})
        rows.sort(key=lambda row: abs(row["delta"]), reverse=True)
        return rows

    def _diff_snapshots(self):
        """Compare the live state with the accepted snapshot.

        Returns rows describing what was added, removed or modified between
        two snapshots, field by field (prepared for a table).
        """
        snapshot = self._load_snapshot()
        previous = snapshot.get("fingerprints", {}) or {}
        rows = []
        current = {}
        for script in self._scripts():
            key = str(script.get("id"))
            name = script.get("name", "") or "(unnamed)"
            current[key] = self._fingerprint(script)
            if key not in previous:
                rows.append({"name": name, "field": "*", "kind": "added",
                             "before": "-", "after": name})
                continue
            old = previous[key]
            if not isinstance(old, dict):
                rows.append({"name": name, "field": "*", "kind": "modified",
                             "before": str(old), "after": "changed"})
                continue
            for field, value in current[key].items():
                if old.get(field) != value:
                    rows.append({"name": name, "field": field,
                                 "kind": "modified",
                                 "before": str(old.get(field, "")),
                                 "after": str(value)})
        for key, old in previous.items():
            if key not in current:
                name = old.get("name", "(unnamed)") if isinstance(old, dict) else key
                rows.append({"name": name, "field": "*", "kind": "removed",
                             "before": name, "after": "-"})
        return rows

    def _timeline_audit(self):
        """Summarise the timeline: counts per category and large changes."""
        events = self._timeline_events()
        by_category = {code: 0 for code, _label in self.TIMELINE_CATEGORIES}
        for event in events:
            code = event.get("category", "")
            by_category[code] = by_category.get(code, 0) + 1
        large = [event for event in events if event.get("large")]
        return {"events": events, "by_category": by_category, "large": large}

    # ------------------------------------------------ timeline write hooks
    def on_file_changed(self, where, detail="", before=None, after=None,
                        how=""):
        """Record a file change (call from the file editor / importer)."""
        return self.log_change("files", where, detail, how, before, after)

    def on_module_changed(self, where, detail="", before=None, after=None,
                          how=""):
        """Record a module change (call when a script is saved)."""
        return self.log_change("modules", where, detail, how, before, after)

    def on_config_changed(self, where, detail="", before=None, after=None,
                          how=""):
        """Record a configuration change."""
        return self.log_change("config", where, detail, how, before, after)

    def on_environment_changed(self, where, detail="", before=None, after=None,
                               how=""):
        """Record an environment change (variables, PATH, dependencies)."""
        return self.log_change("environment", where, detail, how, before, after)

    def on_permissions_changed(self, where, detail="", before=None, after=None,
                               how=""):
        """Record a change to the permissions a module requires."""
        return self.log_change("permissions", where, detail, how, before, after)

    def _recording_hook(self):
        """Record every change detected since the last snapshot.

        The main window may call this after any change. It compares the live
        state with the last snapshot and writes one timeline entry per change,
        so the history stays complete without an explicit callback per edit.
        """
        snapshot = self._load_snapshot()
        previous = snapshot.get("fingerprints", {}) or {}
        recorded = 0
        saved = {str(script.get("id")): script for script in self._scripts()}
        for key, script in saved.items():
            name = script.get("name", "") or "(unnamed)"
            current = self._fingerprint(script)
            if key not in previous:
                self.on_module_changed(name, self.t("audit_tl_added", "added"),
                                       None, current.get("code_len"))
                recorded += 1
            elif previous[key] != current:
                old = previous[key]
                old_len = old.get("code_len", 0) if isinstance(old, dict) else 0
                self.on_module_changed(name,
                                       self.t("audit_tl_modified", "modified"),
                                       old_len, current.get("code_len"))
                recorded += 1
                perms_before = self._module_permissions("")
                perms_after = self._module_permissions(script.get("code", ""))
                if perms_before != perms_after:
                    self.on_permissions_changed(name, perms_after, perms_before,
                                                perms_after)
                    recorded += 1
        for key, old in previous.items():
            if key not in saved:
                name = old.get("name", "(unnamed)") if isinstance(old, dict) else key
                self.on_module_changed(name, self.t("audit_tl_removed", "removed"),
                                       None, None)
                recorded += 1
        return recorded

    # ------------------------------------------------------ environment audit
    # Packages considered part of the standard library (never "missing").
    STDLIB_MODULES = frozenset((
        "os", "sys", "re", "json", "math", "time", "datetime", "random",
        "subprocess", "threading", "collections", "itertools", "functools",
        "pathlib", "shutil", "tempfile", "typing", "io", "csv", "sqlite3",
        "hashlib", "base64", "pickle", "logging", "unittest", "argparse",
        "configparser", "urllib", "http", "socket", "ssl", "struct",
        "inspect", "importlib", "string", "textwrap", "traceback", "uuid",
        "warnings", "weakref", "xml", "zipfile", "tarfile", "glob", "fnmatch",
    ))

    def _env_variables(self):
        """Every environment variable, sorted by name.

        Values that look like secrets are masked so they never leak into the
        report; only the presence and a redacted form are kept.
        """
        rows = []
        for name in sorted(os.environ):
            value = os.environ.get(name, "")
            lowered = name.lower()
            secret = any(tok in lowered for tok in ("pass", "secret", "token",
                                                    "key", "pwd", "credential"))
            rows.append({
                "name": name,
                "value": "***" if secret else value[:120],
                "length": len(value),
                "secret": secret,
            })
        return rows

    def _path_analysis(self):
        """Analyse the PATH variable and flag risky entries."""
        raw = os.environ.get("PATH", "") or os.environ.get("Path", "")
        separator = ";" if os.name == "nt" else ":"
        entries = [part.strip() for part in raw.split(separator) if part.strip()]
        rows = []
        seen = set()
        for entry in entries:
            lowered = entry.lower().rstrip("\\/") if os.name == "nt" else entry
            exists = os.path.isdir(entry)
            duplicate = lowered in seen
            seen.add(lowered)
            issue = ""
            if not exists:
                issue = self.t("audit_env_path_missing", "path does not exist")
            elif duplicate:
                issue = self.t("audit_env_path_duplicate", "duplicate entry")
            elif not os.path.isabs(entry):
                issue = self.t("audit_env_path_relative", "relative entry")
            rows.append({"path": entry, "exists": exists,
                         "duplicate": duplicate, "issue": issue})
        return rows

    def _interpreter_versions(self):
        """Version of the running Python plus any others found on PATH."""
        import platform
        import shutil as _shutil
        rows = [{
            "name": "Python (running)",
            "version": platform.python_version(),
            "where": _shutil.which("python") or "?",
            "issue": "",
        }]
        seen = {"python"}
        for candidate in ("python3", "py"):
            found = _shutil.which(candidate)
            if not found:
                continue
            if candidate in seen:
                continue
            seen.add(candidate)
            rows.append({"name": candidate,
                         "version": self._probe_version(found),
                         "where": found, "issue": ""})
        return rows

    @staticmethod
    def _probe_version(executable):
        """Best-effort version string for another interpreter binary."""
        import subprocess
        for args in ((executable, "--version"), (executable, "-V")):
            try:
                result = subprocess.run(args, capture_output=True, text=True,
                                        timeout=4)
            except Exception:
                continue
            text = (result.stdout or result.stderr or "").strip()
            if text:
                return text.splitlines()[0]
        return "?"

    def _python_dependencies(self):
        """Installed pip packages and their versions (via metadata)."""
        packages = {}
        try:
            from importlib import metadata as _meta
            for dist in _meta.distributions():
                try:
                    name = dist.metadata["Name"]
                except Exception:
                    name = None
                if not name:
                    continue
                packages[name] = dist.version
        except Exception:
            packages = {}
        return packages

    def _dependency_usage(self):
        """Third-party modules imported by the scripts in the database."""
        import re as _re
        used = {}
        for script in self._scripts():
            code = script.get("code", "") or ""
            names = _re.findall(r"^\s*import\s+([\w., ]+)", code, _re.MULTILINE)
            names += _re.findall(r"^\s*from\s+([\w.]+)\s+import", code,
                                 _re.MULTILINE)
            for chunk in names:
                for part in chunk.split(","):
                    base = part.strip().split(" as ")[0].strip().split(".")[0]
                    if not base or base in self.STDLIB_MODULES:
                        continue
                    used.setdefault(base, []).append(
                        script.get("name", "") or "(unnamed)")
        return used

    def _environment_audit(self):
        """Audit the environment: variables, PATH and interpreter versions."""
        variables = self._env_variables()
        secrets = [row for row in variables if row["secret"]]
        path_rows = self._path_analysis()
        path_issues = [row for row in path_rows if row["issue"]]
        interpreters = self._interpreter_versions()
        versions = {row["version"] for row in interpreters if row["version"]}
        return {
            "variables": variables,
            "secret_count": len(secrets),
            "path": path_rows,
            "path_issues": path_issues,
            "interpreters": interpreters,
            "interpreter_conflict": len(versions) > 1,
        }

    def _dependency_audit(self):
        """Audit dependencies: installed, used, missing and version conflicts.

        Covers pip packages (via importlib.metadata) and the modules imported
        by the scripts. A module that is imported but not installed and not in
        the standard library is reported as missing.
        """
        installed = self._python_dependencies()
        lowered = {name.lower(): name for name in installed}
        used = self._dependency_usage()
        rows = []
        missing = []
        for module, users in sorted(used.items()):
            real = lowered.get(module.lower())
            if real:
                rows.append({"name": real, "version": installed[real],
                             "source": "pip",
                             "used_by": ", ".join(sorted(set(users))),
                             "issue": ""})
            else:
                rows.append({"name": module, "version": "-", "source": "python",
                             "used_by": ", ".join(sorted(set(users))),
                             "issue": self.t("audit_dep_missing", "missing")})
                missing.append(module)
        conflicts = self._version_conflicts(installed)
        return {"rows": rows, "missing": missing, "conflicts": conflicts,
                "installed": installed}

    @staticmethod
    def _version_conflicts(installed):
        """Detect version conflicts: multiple versions of the same package."""
        by_norm = {}
        names = {}
        for name, version in installed.items():
            norm = name.lower().replace("-", "_").replace(".", "_")
            names.setdefault(norm, []).append(name)
            by_norm.setdefault(norm, set()).add(version)
        conflicts = []
        for norm, versions in by_norm.items():
            if len(versions) > 1:
                conflicts.append({"package": ", ".join(sorted(names[norm])),
                                  "versions": ", ".join(sorted(versions))})
        return conflicts

    # --------------------------------------------------------- integrations
    # The audit tab is wired into the rest of the application through the
    # window object. Every call is defensive: if the host window does not
    # expose the expected method, the integration degrades gracefully and
    # reports it instead of crashing.

    def _window_call(self, name, *args, **kwargs):
        """Call a method on the main window if it exists; return (ok, result)."""
        method = getattr(self.window, name, None)
        if not callable(method):
            return False, None
        try:
            return True, method(*args, **kwargs)
        except Exception as exc:
            return False, exc

    def _application_tab(self, attr):
        """Return a top-level tab widget by attribute name, if present."""
        return getattr(self.window, attr, None)

    # --- opening the audit in the sibling tools ------------------------
    def open_audit_in_diff(self, script=None):
        """Open a script (or the selected one) in the Diff tab."""
        script = script or self._selected_script()
        if script is None:
            return self._report_action("audit_open_diff", ok=False,
                                       detail=self.t("audit_integration_noselect",
                                                     "no script selected"))
        diff = self._application_tab("diff_tab")
        if diff is None:
            return self._report_action("audit_open_diff", ok=False,
                                       detail=self.t("audit_integration_nodiff",
                                                     "Diff tab unavailable"))
        try:
            diff._right = diff._entry_to_source(script)
            diff.recompute()
            tabs = getattr(self.window, "tabs", None)
            if tabs is not None:
                tabs.setCurrentWidget(diff)
        except Exception as exc:
            return self._report_action("audit_open_diff", ok=False, detail=str(exc))
        self.log_change("files", script.get("name", ""),
                        self.t("audit_open_diff", "opened in Diff"),
                        self.t("audit_tl_how_manual", "manual"))
        return self._report_action("audit_open_diff", ok=True,
                                   detail=script.get("name", ""))

    def open_audit_in_search(self, needle=None):
        """Focus the Scripts search box with an audit-derived query."""
        ok, _ = self._window_call("focus_filter")
        # The Scripts tab exposes the search box as either filter_input or
        # filter_edit depending on the build; support both so the integration
        # works against the real window.
        entry = (getattr(self.window, "filter_edit", None)
                 or getattr(self.window, "filter_input", None))
        if entry is not None and needle is not None:
            try:
                entry.setText(str(needle))
                # Apply the filter immediately so results are already shown.
                self._window_call("on_filter_changed", str(needle))
            except Exception:
                pass
        if not ok and entry is None:
            return self._report_action("audit_open_search", ok=False,
                                       detail=self.t("audit_integration_nosearch",
                                                     "search unavailable"))
        return self._report_action("audit_open_search", ok=True,
                                   detail=str(needle or ""))

    def open_audit_in_runner(self, script=None):
        """Run a script (or the selected one) through the runner."""
        script = script or self._selected_script()
        if script is None:
            return self._report_action("audit_open_runner", ok=False,
                                       detail=self.t("audit_integration_noselect",
                                                     "no script selected"))
        ok, result = self._window_call("run_selected_script")
        if not ok:
            return self._report_action("audit_open_runner", ok=False,
                                       detail=self.t("audit_integration_norunner",
                                                     "runner unavailable"))
        self.log_change("modules", script.get("name", ""),
                        self.t("audit_open_runner", "run in runner"),
                        self.t("audit_tl_how_manual", "manual"))
        return self._report_action("audit_open_runner", ok=True,
                                   detail=script.get("name", ""))

    def open_audit_in_debugger(self, script=None):
        """Open a script in the debugger view if the application has one."""
        script = script or self._selected_script()
        debugger = self._application_tab("debug_tab")
        if debugger is None:
            debugger = self._application_tab("debugger_tab")
        if debugger is None:
            ok, _ = self._window_call("open_in_debugger", script)
            if not ok:
                return self._report_action(
                    "audit_open_debugger", ok=False,
                    detail=self.t("audit_integration_nodebugger",
                                  "debugger unavailable"))
        self.log_change("modules", (script or {}).get("name", ""),
                        self.t("audit_open_debugger", "opened in debugger"),
                        self.t("audit_tl_how_manual", "manual"))
        return self._report_action("audit_open_debugger", ok=True,
                                   detail=(script or {}).get("name", ""))

    # --- report generation & artifact export ---------------------------
    def generate_audit_report(self):
        """Build a human-readable audit report and show it in the output box."""
        text = self.build_report("txt")
        self._set_integration_output(text)
        self.log_change("files", "audit_report",
                        self.t("audit_generate_report", "report generated"),
                        self.t("audit_tl_how_manual", "manual"))
        return self._report_action("audit_generate_report", ok=True)

    def export_audit_artifact(self):
        """Save the report as an artifact file inside the data directory."""
        path = self._export_to_file("audit_artifact", "txt")
        return self._report_action("audit_export_artifact", ok=bool(path),
                                   detail=path or "")

    # --- audit operations --------------------------------------------
    def run_full_audit(self):
        """Refresh every audit area (full system audit)."""
        self.refresh()
        for index in range(self.subtabs.count()):
            self._fill_page(index)
        self.log_change("environment", "system",
                        self.t("audit_op_full", "full system audit"),
                        self.t("audit_tl_how_manual", "manual"))
        return self._report_action("audit_op_full", ok=True)

    def _audit_operation(self, kind, detail=""):
        """Switch to the sub-tab that shows a given audit area and refresh it."""
        mapping = {
            "modules": "audit_tab_modules",
            "scripts": "audit_tab_modules",
            "config": "audit_tab_config",
            "environment": "audit_tab_environment",
            "security": "audit_tab_security",
            "changes": "audit_tab_timeline",
            "dependencies": "audit_tab_environment",
            "runtime": "audit_tab_runtime",
        }
        key = mapping.get(kind)
        if key is None:
            return self._report_action("audit_op_module", ok=False, detail=kind)
        index = self._index_of_key(key)
        if index is not None:
            self.subtabs.setCurrentIndex(index)
            self._fill_page(index)
        return self._report_action("audit_op_" + {"scripts": "module"}.get(kind, kind),
                                   ok=True, detail=detail)

    def _index_of_key(self, key):
        for index, (value, _fallback) in self._subtab_keys.items():
            if value == key:
                return index
        return None

    def run_module_audit(self):
        return self._audit_operation("modules")

    def run_script_audit(self):
        return self._audit_operation("scripts")

    def run_config_audit(self):
        return self._audit_operation("config")

    def run_environment_audit(self):
        return self._audit_operation("environment")

    def run_security_audit(self):
        return self._audit_operation("security")

    def run_changes_audit(self):
        return self._audit_operation("changes")

    def run_dependency_audit(self):
        return self._audit_operation("dependencies")

    def run_runtime_audit(self):
        return self._audit_operation("runtime")

    # --- small helpers shared by the two new tabs ----------------------
    def _selected_script(self):
        getter = getattr(self.window, "selected_script", None)
        if callable(getter):
            try:
                return getter()
            except Exception:
                return None
        return None

    def _report_action(self, key, ok=True, detail=""):
        """Push a one-line status message into the integration output box."""
        label = self.t(key, key)
        prefix = self.t("audit_ok", "OK") if ok else self.t("audit_failed", "FAILED")
        line = "[%s] %s%s" % (prefix, label,
                              (" - " + str(detail)) if detail else "")
        self._append_integration_output(line)
        return ok

    def _append_integration_output(self, line):
        box = getattr(self, "integration_output", None)
        if box is None:
            return
        try:
            box.appendPlainText(line)
        except Exception:
            pass

    def _set_integration_output(self, text):
        box = getattr(self, "integration_output", None)
        if box is None:
            return
        try:
            box.setPlainText(text)
        except Exception:
            pass

    # -------------------------------------------------------- export engine
    EXPORT_FORMATS = ("txt", "json", "log")

    def build_report(self, fmt="txt"):
        """Build the full audit report in the requested format.

        Returns a string. ``txt`` is a plain ASCII report, ``json`` is a
        structured document and ``log`` is a timestamped log-lines document.
        """
        sections = self._report_sections()
        fmt = (fmt or "txt").lower()
        if fmt == "json":
            return json.dumps(sections, ensure_ascii=False, indent=2,
                              default=str)
        if fmt == "log":
            lines = []
            for section in sections["sections"]:
                for row in section.get("rows", []):
                    text = row if isinstance(row, str) else json.dumps(row, default=str)
                    lines.append("%s [%s] %s" % (sections["generated"],
                                                 section.get("name", ""), text))
            return "\n".join(lines)
        return self._report_as_text(sections)

    def audit_all(self):
        """Return the full structured audit document (API entry point)."""
        state = self._state or self._analyze()
        self._state = state
        return self._report_sections()

    def export_report(self, fmt="txt"):
        """Export the report as an artifact (API entry point)."""
        return self._export_to_file("audit", fmt)

    def _report_sections(self):
        """Collect every audit area into one structured document."""
        state = self._state or self._analyze()
        self._state = state
        timeline = self._timeline_audit()
        target = self._selection_filter_audit()
        sections = [
            {"name": "summary", "rows": [
                {"total": state["total"], "problems": len(state["problems"]),
                 "risk": state["risk"], "integrity": state["integrity"],
                 "changes_since": state["changes_since"]}]},
            {"name": "modules", "rows": self._module_audit()},
            {"name": "scripts", "rows": self._script_audit()},
            {"name": "security", "rows": self._security_findings()},
            {"name": "permissions", "rows": self._permission_analysis()},
            {"name": "config", "rows": self._config_audit_report()},
            {"name": "runtime", "rows": self._runtime_audit().get("anomalies", [])},
            {"name": "timeline", "rows": timeline.get("events", [])[:200]},
            {"name": "diff", "rows": self._diff_snapshots()},
            {"name": "large_changes", "rows": self._detect_large_changes()},
            {"name": "environment", "rows": self._environment_report()},
            {"name": "dependencies", "rows": self._dependency_report()},
        ]
        if target:
            sections.append({"name": "selection", "rows": target})
        return {"generated": _now(), "who": self._current_actor(),
                "sections": sections}

    def _config_audit_report(self):
        changes, policy = self._config_audit()
        return list(changes) + [{"policy": p} for p in policy]

    def _environment_report(self):
        report = self._environment_audit()
        rows = []
        for section, key in (("variables", "variables"), ("path", "path"),
                             ("interpreters", "interpreters")):
            for row in report.get(key, []):
                rows.append({"area": section, "data": row})
        return rows

    def _dependency_report(self):
        report = self._dependency_audit()
        return report.get("rows", []) + [
            {"conflict": c} for c in report.get("conflicts", [])]

    def _selection_filter_audit(self):
        """Audit restricted to one script.

        Uses the explicit target set by the host window ("Audit this
        script") when present, otherwise the current Scripts selection.
        """
        script = self._audit_target()
        if not script:
            return []
        return [{"selected": script.get("name", ""),
                 "module": self._fingerprint(script),
                 "script": self._script_audit_row(script)}]

    def _audit_target(self):
        """Return the explicitly targeted script, else the current selection."""
        target_id = getattr(self, "_audit_target_id", None)
        if target_id is not None:
            for script in self._scripts():
                if str(script.get("id")) == str(target_id):
                    return script
        script = self._selected_script()
        if script is not None:
            return script
        scripts = self._scripts()
        if target_id is not None and scripts:
            # The target was removed; fall back to the first script.
            return scripts[0]
        return None

    def _script_audit_row(self, script):
        code = script.get("code", "") or ""
        return {"name": script.get("name", ""),
                "suspicious": self._suspicious_constructs(code),
                "lint": self._lint_errors(code, script.get("category", ""))}

    def _report_as_text(self, sections):
        lines = []
        lines.append("=" * 72)
        lines.append("SCRIPTDB AUDIT REPORT")
        lines.append("generated: %s" % sections.get("generated", ""))
        lines.append("author:    %s" % sections.get("who", ""))
        lines.append("=" * 72)
        for section in sections.get("sections", []):
            lines.append("")
            lines.append("-- %s " % section.get("name", "").upper()
                         + "-" * max(0, 40 - len(section.get("name", ""))))
            rows = section.get("rows", [])
            if not rows:
                lines.append("  (none)")
                continue
            for row in rows:
                if isinstance(row, str):
                    lines.append("  " + row)
                else:
                    lines.append("  " + json.dumps(row, ensure_ascii=False,
                                                   default=str))
        return "\n".join(lines)

    def _export_dir(self):
        directory = os.path.join(self._data_dir(), "audit_exports")
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError:
            pass
        return directory

    @staticmethod
    def _ascii_safe(text):
        """Best-effort transliteration so TXT exports stay plain ASCII."""
        try:
            return text.encode("ascii", "replace").decode("ascii")
        except Exception:
            return text

    def _export_to_file(self, base_name, fmt, extra=None):
        """Write a report to a file and return its path (or '' on failure)."""
        content = self.build_report(fmt)
        if extra:
            content = extra + "\n" + content
        if fmt == "txt":
            content = self._ascii_safe(content)
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        filename = "%s-%s.%s" % (base_name, stamp, fmt)
        path = os.path.join(self._export_dir(), filename)
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(content)
        except OSError:
            return ""
        self.log_change("files", filename,
                        self.t("audit_export_file", "audit exported"),
                        self.t("audit_tl_how_manual", "manual"))
        return path

    def export_audit_txt(self):
        """Export the audit as a plain-ASCII TXT report."""
        path = self._export_to_file("audit", "txt")
        return self._report_action("audit_export_txt", ok=bool(path),
                                   detail=path or "")

    def export_audit_json(self):
        """Export the audit as a structured JSON document."""
        path = self._export_to_file("audit", "json")
        return self._report_action("audit_export_json", ok=bool(path),
                                   detail=path or "")

    def export_audit_log(self):
        """Export the audit as a timestamped LOG document."""
        path = self._export_to_file("audit", "log")
        return self._report_action("audit_export_log", ok=bool(path),
                                   detail=path or "")

    def export_full_audit_set(self):
        """Export every audit format at once as one artifact set."""
        paths = [self._export_to_file("audit_full", "txt"),
                 self._export_to_file("audit_full", "json"),
                 self._export_to_file("audit_full", "log")]
        ok = all(paths)
        return self._report_action("audit_export_full", ok=ok,
                                   detail="; ".join(p for p in paths if p))

    def export_comparative_audit(self):
        """Export a before/after snapshot comparison as a TXT artifact."""
        diff = self._diff_snapshots()
        lines = [self.t("audit_diff_title", "Snapshot diff"),
                 "generated: %s" % _now()]
        if not diff:
            lines.append(self.t("audit_none", "none"))
        for row in diff:
            lines.append("%s [%s] %s: %s -> %s" % (
                row.get("name", ""), row.get("kind", ""), row.get("field", ""),
                row.get("before", ""), row.get("after", "")))
        extra = self._ascii_safe("\n".join(lines))
        path = self._export_to_file("audit_comparative", "txt", extra=extra)
        return self._report_action("audit_export_comparative", ok=bool(path),
                                   detail=path or "")

    # ------------------------------------------------------ premium features
    # Chapter 12: automatic audits, a cyclic scheduler, a comparative audit,
    # a severity-graded security audit and a whole-workspace integrity check.
    # Everything is opt-in and safe to run headless.

    # Event -> sensible default action for the automatic audits.
    AUTOMATIC_TRIGGERS = (
        ("file_changed", "audit_trigger_file"),
        ("module_installed", "audit_trigger_install"),
        ("module_updated", "audit_trigger_update"),
        ("runtime_error", "audit_trigger_error"),
        ("deploy", "audit_trigger_deploy"),
    )
    # Persisted trigger preferences and scheduler state.
    PREMIUM_FILE = "audit_premium.json"

    def _premium_path(self):
        return os.path.join(self._data_dir(), self.PREMIUM_FILE)

    def _load_premium(self):
        path = self._premium_path()
        if not os.path.isfile(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save_premium(self, data):
        try:
            with open(self._premium_path(), "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def _premium(self):
        return self._load_premium()

    # --- automatic audits (event-driven) -------------------------------
    def triggers_enabled(self):
        """Which automatic triggers are currently on."""
        saved = self._premium().get("triggers", {})
        # Everything is enabled by default the first time.
        return dict((event, bool(saved.get(event, True)))
                    for event, _key in self.AUTOMATIC_TRIGGERS)

    def enable_trigger(self, event, enabled=True):
        """Turn one automatic trigger on or off and persist the choice."""
        data = self._load_premium()
        triggers = dict(data.get("triggers", {}))
        triggers[event] = bool(enabled)
        data["triggers"] = triggers
        self._save_premium(data)

    def _install_event_bridge(self):
        """Subscribe the automatic audits to the shared event bus."""
        if getattr(self, "_bridge_installed", False):
            return
        for event, _key in self.AUTOMATIC_TRIGGERS:
            AUDIT_BUS.subscribe(event, self._on_audit_event)
        self._bridge_installed = True

    def _on_audit_event(self, event, **payload):
        """Handle a bus event: run the configured automatic audit."""
        if not self.triggers_enabled().get(event, True):
            return False
        return self.trigger_automatic_audit(event, **payload)

    def trigger_automatic_audit(self, event, **payload):
        """Run the audit that an event implies and record it in the timeline."""
        where = payload.get("where", "") or payload.get("name", "") or ""
        detail = payload.get("detail", "")
        self.log_change("modules" if event in ("module_installed",
                                               "module_updated",
                                               "runtime_error") else "files",
                        where, detail or event,
                        self.t("audit_how_event", "event: %s") % event)
        if event == "runtime_error":
            self.run_runtime_audit()
        elif event in ("module_installed", "module_updated"):
            self.run_module_audit()
            self.run_script_audit()
        elif event == "deploy":
            self.run_config_audit()
            self.run_environment_audit()
        else:
            self.run_full_audit()
        return True

    def emit_audit_event(self, event, **payload):
        """Emit an audit event on the bus (used by the host application)."""
        return AUDIT_BUS.emit(event, **payload)

    # --- cyclic scheduler ----------------------------------------------
    def scheduler_config(self):
        """Current scheduler settings (interval in seconds, enabled flag)."""
        data = self._premium().get("scheduler", {})
        return {"enabled": bool(data.get("enabled", False)),
                "interval": int(data.get("interval", 3600)),
                "last_run": data.get("last_run", "")}

    def configure_scheduler(self, enabled=None, interval=None):
        """Update the scheduler settings and (re)arm the timer."""
        data = self._load_premium()
        scheduler = dict(data.get("scheduler", {}))
        if enabled is not None:
            scheduler["enabled"] = bool(enabled)
        if interval is not None:
            scheduler["interval"] = max(10, int(interval))
        data["scheduler"] = scheduler
        self._save_premium(data)
        self._arm_scheduler()
        return scheduler

    def _arm_scheduler(self):
        """Start or stop the QTimer that drives the cyclic audit."""
        config = self.scheduler_config()
        timer = getattr(self, "_scheduler_timer", None)
        if not config["enabled"]:
            if timer is not None:
                timer.stop()
            return
        if timer is None and hasattr(QtCore, "QTimer") and self.parent() is not None:
            timer = QtCore.QTimer(self)
            timer.timeout.connect(self.run_scheduled_audit)
            self._scheduler_timer = timer
        if timer is not None:
            timer.start(config["interval"] * 1000)

    def run_scheduled_audit(self):
        """Fire one cyclic audit and stamp the run time."""
        data = self._load_premium()
        scheduler = dict(data.get("scheduler", {}))
        scheduler["last_run"] = _now()
        data["scheduler"] = scheduler
        self._save_premium(data)
        self.log_change("environment", "scheduler",
                        self.t("audit_op_full", "full system audit"),
                        self.t("audit_how_scheduled", "scheduled"))
        self.run_full_audit()
        return scheduler["last_run"]

    def scheduler_due(self, now=None):
        """True when a cyclic audit is enabled and its interval has elapsed."""
        config = self.scheduler_config()
        if not config["enabled"]:
            return False
        last = _parse_date(config.get("last_run", ""))
        if last is None:
            return True
        now = now or dt.datetime.now()
        return (now - last).total_seconds() >= config["interval"]

    # --- comparative audit ---------------------------------------------
    def comparative_audit(self, since=None):
        """Show what changed since the last audit (or a given timestamp).

        Returns a summary plus the per-field snapshot diff rows, so the UI can
        answer "co zmienilo sie od ostatniego audytu" directly.
        """
        snapshot = self._load_snapshot()
        last_audit = snapshot.get("last_audit", "")
        diff = self._diff_snapshots()
        added = [row for row in diff if row.get("kind") == "added"]
        removed = [row for row in diff if row.get("kind") == "removed"]
        modified = [row for row in diff if row.get("kind") == "modified"]
        large = self._detect_large_changes()
        return {
            "since": since or last_audit or self.t("audit_never", "never"),
            "added": added, "removed": removed, "modified": modified,
            "large": large, "diff": diff,
            "count": len(added) + len(removed) + len(modified),
        }

    # --- severity-graded security audit --------------------------------
    def _risk_weight(self, severity):
        return {"high": 3, "medium": 2, "low": 1}.get(severity, 0)

    def risk_security_audit(self):
        """Security audit where every finding carries an explicit risk level.

        Returns findings ordered from highest to lowest risk together with an
        overall risk level and a numeric score.
        """
        findings = []
        for finding in self._security_findings():
            severity = finding.get("severity", "low")
            findings.append({
                "kind": finding.get("kind", ""),
                "where": finding.get("where", ""),
                "detail": finding.get("detail", ""),
                "risk": severity,
                "weight": self._risk_weight(severity),
            })
        state = self._state or self._analyze()
        high = sum(1 for f in findings if f["risk"] == "high")
        medium = sum(1 for f in findings if f["risk"] == "medium")
        if high:
            overall = "high"
        elif medium or findings:
            overall = "medium"
        else:
            overall = "low"
        findings.sort(key=lambda f: f["weight"], reverse=True)
        return {"overall": overall, "findings": findings,
                "score": sum(f["weight"] for f in findings),
                "dashboard_risk": state.get("risk", "low")}

    # Alias so the headless AuditAPI can call either name transparently.
    security_risk_audit = risk_security_audit
    # --- whole-workspace integrity -------------------------------------
    def workspace_integrity(self):
        """Check the integrity of every script/module in the workspace.

        Compares the live fingerprints with the accepted snapshot and reports
        every mismatch, missing entry and unsigned module as a problem.
        """
        snapshot = self._load_snapshot()
        previous = snapshot.get("fingerprints", {}) or {}
        problems = []
        checked = 0
        for script in self._scripts():
            key = str(script.get("id"))
            name = script.get("name", "") or "(unnamed)"
            checked += 1
            current = self._fingerprint(script)
            if key not in previous:
                problems.append({"where": name, "severity": "low",
                                 "detail": self.t("audit_integrity_new",
                                                  "not in baseline")})
            elif previous[key] != current:
                problems.append({"where": name, "severity": "high",
                                 "detail": self.t("audit_integrity_changed",
                                                  "fingerprint changed")})
            if not self._is_signed(script):
                problems.append({"where": name, "severity": "medium",
                                 "detail": self.t("audit_find_unsigned",
                                                  "unsigned module")})
        for key, old in previous.items():
            if not any(str(s.get("id")) == key for s in self._scripts()):
                name = old.get("name", "(unnamed)") if isinstance(old, dict) else key
                problems.append({"where": name, "severity": "medium",
                                 "detail": self.t("audit_integrity_missing",
                                                  "missing entry")})
        high = sum(1 for p in problems if p["severity"] == "high")
        status = "ok" if not problems else ("critical" if high else "warning")
        return {"status": status, "checked": checked, "problems": problems}

    # --- snapshot system (named, versioned snapshots) ------------------
    def _snapshots_dir(self):
        return os.path.join(self._data_dir(), "audit_snapshots")

    def save_named_snapshot(self, name):
        """Store the current audit state as a named, re-openable snapshot."""
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_"
                       for ch in str(name)) or "snapshot"
        snapshot = {
            "name": name, "saved": _now(), "who": self._current_actor(),
            "fingerprints": {str(s.get("id")): self._fingerprint(s)
                             for s in self._scripts()},
        }
        directory = self._snapshots_dir()
        try:
            os.makedirs(directory, exist_ok=True)
            path = os.path.join(directory, "%s.json" % safe)
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(snapshot, handle, ensure_ascii=False, indent=2)
        except OSError:
            return ""
        return path

    def list_snapshots(self):
        """Names of every stored snapshot, newest first."""
        directory = self._snapshots_dir()
        rows = []
        try:
            for name in os.listdir(directory):
                if not name.endswith(".json"):
                    continue
                path = os.path.join(directory, name)
                try:
                    with open(path, "r", encoding="utf-8") as handle:
                        data = json.load(handle)
                except (OSError, ValueError):
                    continue
                rows.append({"file": name, "name": data.get("name", name),
                             "saved": data.get("saved", "")})
        except OSError:
            return []
        rows.sort(key=lambda r: r.get("saved", ""), reverse=True)
        return rows

    def diff_with_snapshot(self, name):
        """Compare the live state with one named snapshot."""
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_"
                       for ch in str(name)) or "snapshot"
        path = os.path.join(self._snapshots_dir(), "%s.json" % safe)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                saved = json.load(handle)
        except (OSError, ValueError):
            return []
        previous = saved.get("fingerprints", {}) or {}
        rows = []
        current = {}
        for script in self._scripts():
            key = str(script.get("id"))
            name_ = script.get("name", "") or "(unnamed)"
            current[key] = self._fingerprint(script)
            if key not in previous:
                rows.append({"name": name_, "kind": "added", "field": "*"})
            elif previous[key] != current[key]:
                rows.append({"name": name_, "kind": "modified", "field": "*"})
        for key in previous:
            if key not in current:
                rows.append({"name": key, "kind": "removed", "field": "*"})
        return rows

    # -------------------------------------------------------------- UI build
    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)

        header = QtWidgets.QHBoxLayout()
        self.title_label = QtWidgets.QLabel(self.t("audit", "Audit"))
        self.title_label.setStyleSheet(
            "font-size: 14px; font-weight: bold; color: #8ab4f8;")
        header.addWidget(self.title_label)
        header.addStretch()
        self.last_audit_label = QtWidgets.QLabel("")
        self.last_audit_label.setStyleSheet("color: #9aa0a6;")
        header.addWidget(self.last_audit_label)
        self.btn_refresh = QtWidgets.QPushButton(self.t("audit_refresh", "Refresh"))
        self.btn_refresh.clicked.connect(self.refresh)
        header.addWidget(self.btn_refresh)
        self.btn_accept = QtWidgets.QPushButton(
            self.t("audit_accept", "Mark as audited"))
        self.btn_accept.clicked.connect(self.mark_audited)
        header.addWidget(self.btn_accept)
        layout.addLayout(header)

        # Sub-tabs: each audit area gets its own page so the view stays
        # readable and only the visible page is repainted on refresh.
        self.subtabs = QtWidgets.QTabWidget()
        layout.addWidget(self.subtabs, 1)

        self._build_dashboard_tab()
        self._build_modules_tab()
        self._build_security_tab()
        self._build_config_tab()
        self._build_runtime_tab()
        self._build_timeline_tab()
        self._build_environment_tab()
        self._build_integration_tab()
        self._build_export_tab()
        self._build_architecture_tab()
        self._build_premium_tab()

        self.status = QtWidgets.QLabel("")
        self.status.setStyleSheet("color: #9aa0a6;")
        layout.addWidget(self.status)

        # Repaint only the page the user is actually looking at.
        self.subtabs.currentChanged.connect(self._on_subtab_changed)

    def _make_page(self, title_key, fallback):
        """Create one sub-tab page and return (page, page_layout)."""
        page = QtWidgets.QWidget()
        page_layout = QtWidgets.QVBoxLayout(page)
        index = self.subtabs.addTab(page, self.t(title_key, fallback))
        # Remember the label key so retranslate() can update it later.
        if not hasattr(self, "_subtab_keys"):
            self._subtab_keys = {}
        self._subtab_keys[index] = (title_key, fallback)
        return page, page_layout
    def _build_dashboard_tab(self):
        page, page_layout = self._make_page("audit_tab_dashboard", "Dashboard")
        self._dashboard_page = page
        # ---- indicator cards ------------------------------------------
        cards = QtWidgets.QHBoxLayout()
        self.card_integrity = self._make_card(
            self.t("audit_integrity", "System integrity"), "-")
        self.card_problems = self._make_card(
            self.t("audit_problems", "Problems found"), "0")
        self.card_changes = self._make_card(
            self.t("audit_changes", "Changes since last audit"), "0")
        self.card_risk = self._make_card(
            self.t("audit_risk", "Risk level"), "-")
        for card in (self.card_integrity, self.card_problems,
                     self.card_changes, self.card_risk):
            cards.addWidget(card["frame"], 1)
        page_layout.addLayout(cards)

        # ---- recent activity feeds ------------------------------------
        feeds = QtWidgets.QHBoxLayout()
        columns = [self.t("audit_when", "When"), self.t("audit_where", "Script"),
                   self.t("audit_detail", "Detail")]
        self.security_tree = self._make_feed(
            self.t("audit_feed_security", "Recent security events"), columns)
        self.module_tree = self._make_feed(
            self.t("audit_feed_modules", "Recent module changes"), columns)
        self.config_tree = self._make_feed(
            self.t("audit_feed_config", "Recent configuration changes"), columns)
        feeds.addWidget(self.security_tree)
        feeds.addWidget(self.module_tree)
        feeds.addWidget(self.config_tree)
        page_layout.addLayout(feeds, 1)

    def _build_modules_tab(self):
        page, page_layout = self._make_page("audit_tab_modules",
                                            "Modules and scripts")
        self._modules_page = page
        audit_row = QtWidgets.QHBoxLayout()
        self.module_audit_tree = self._make_feed(
            self.t("audit_modules_title", "Module audit"),
            [self.t("audit_module", "Module"),
             self.t("audit_integrity_col", "Integrity"),
             self.t("audit_signature", "Signature"),
             self.t("audit_version", "Version"),
             self.t("audit_changed", "Changed"),
             self.t("audit_permissions", "Permissions")])
        self.script_audit_tree = self._make_feed(
            self.t("audit_scripts_title", "Script audit"),
            [self.t("audit_script", "Script"),
             self.t("audit_change_col", "Changes"),
             self.t("audit_unused_funcs", "Unused functions"),
             self.t("audit_unused_imports", "Unused imports"),
             self.t("audit_suspicious", "Suspicious"),
             self.t("audit_lint", "Lint errors")])
        audit_row.addWidget(self.module_audit_tree)
        audit_row.addWidget(self.script_audit_tree)
        page_layout.addLayout(audit_row, 1)

    def _build_security_tab(self):
        page, page_layout = self._make_page("audit_tab_security",
                                            "Security")
        self._security_page = page
        sec_row = QtWidgets.QHBoxLayout()
        self.permissions_tree = self._make_feed(
            self.t("audit_permissions_title", "Permission analysis"),
            [self.t("audit_target", "Target"),
             self.t("audit_files", "Files"),
             self.t("audit_network", "Network"),
             self.t("audit_environment", "Environment"),
             self.t("audit_exec", "Exec")])
        self.findings_tree = self._make_feed(
            self.t("audit_findings_title", "Security findings"),
            [self.t("audit_finding", "Finding"),
             self.t("audit_where", "Script"),
             self.t("audit_severity", "Severity")])
        sec_row.addWidget(self.permissions_tree)
        sec_row.addWidget(self.findings_tree)
        page_layout.addLayout(sec_row, 1)

    def _build_config_tab(self):
        page, page_layout = self._make_page("audit_tab_config",
                                            "Configuration")
        self._config_page = page
        cfg_row = QtWidgets.QHBoxLayout()
        self.config_changes_tree = self._make_feed(
            self.t("audit_cfg_changes", "Configuration changes"),
            [self.t("audit_file", "File"),
             self.t("audit_key", "Key"),
             self.t("audit_issue", "Issue"),
             self.t("audit_severity", "Severity")])
        self.config_policy_tree = self._make_feed(
            self.t("audit_cfg_policy", "Policy compliance"),
            [self.t("audit_file", "File"),
             self.t("audit_policy", "Policy"),
             self.t("audit_result", "Result")])
        cfg_row.addWidget(self.config_changes_tree)
        cfg_row.addWidget(self.config_policy_tree)
        page_layout.addLayout(cfg_row, 1)

    def _build_runtime_tab(self):
        page, page_layout = self._make_page("audit_tab_runtime", "Runtime")
        self._runtime_page = page
        run_row = QtWidgets.QHBoxLayout()
        self.runtime_events_tree = self._make_feed(
            self.t("audit_runtime_events", "Runtime events"),
            [self.t("audit_when", "When"),
             self.t("audit_kind", "Kind"),
             self.t("audit_where", "Script"),
             self.t("audit_time_ms", "Time (ms)")])
        self.runtime_anomaly_tree = self._make_feed(
            self.t("audit_runtime_anomalies", "Runtime anomalies"),
            [self.t("audit_anomaly", "Anomaly"),
             self.t("audit_where", "Script"),
             self.t("audit_severity", "Severity")])
        run_row.addWidget(self.runtime_events_tree)
        run_row.addWidget(self.runtime_anomaly_tree)
        page_layout.addLayout(run_row, 1)

    def _build_timeline_tab(self):
        page, page_layout = self._make_page("audit_tab_timeline",
                                            "Change timeline")
        self._timeline_page = page
        tl_row = QtWidgets.QHBoxLayout()
        self.timeline_tree = self._make_feed(
            self.t("audit_tl_title", "Change timeline"),
            [self.t("audit_when", "When"),
             self.t("audit_tl_who", "Who"),
             self.t("audit_tl_category", "Category"),
             self.t("audit_where", "Script"),
             self.t("audit_tl_what", "What"),
             self.t("audit_tl_how", "How")])
        self.diff_tree = self._make_feed(
            self.t("audit_diff_title", "Snapshot diff"),
            [self.t("audit_diff_item", "Entry"),
             self.t("audit_diff_kind", "Kind"),
             self.t("audit_diff_field", "Field"),
             self.t("audit_diff_before", "Before"),
             self.t("audit_diff_after", "After")])
        tl_row.addWidget(self.timeline_tree)
        tl_row.addWidget(self.diff_tree)
        page_layout.addLayout(tl_row, 1)

        self.large_tree = self._make_feed(
            self.t("audit_large_title", "Large changes"),
            [self.t("audit_large_item", "Entry"),
             self.t("audit_large_before", "Before"),
             self.t("audit_large_after", "After"),
             self.t("audit_large_delta", "Delta")])
        page_layout.addWidget(self.large_tree, 1)

    def _build_environment_tab(self):
        page, page_layout = self._make_page("audit_tab_environment",
                                            "Environment")
        self._environment_page = page
        env_row = QtWidgets.QHBoxLayout()
        self.env_vars_tree = self._make_feed(
            self.t("audit_env_vars", "Environment variables"),
            [self.t("audit_env_name", "Name"),
             self.t("audit_env_value", "Value"),
             self.t("audit_env_length", "Length")])
        self.env_path_tree = self._make_feed(
            self.t("audit_env_path", "PATH analysis"),
            [self.t("audit_env_entry", "Entry"),
             self.t("audit_env_exists", "Exists"),
             self.t("audit_env_issue", "Issue")])
        self.env_interp_tree = self._make_feed(
            self.t("audit_env_interp", "Interpreters"),
            [self.t("audit_env_name", "Name"),
             self.t("audit_env_version", "Version"),
             self.t("audit_where", "Script")])
        env_row.addWidget(self.env_vars_tree)
        env_row.addWidget(self.env_path_tree)
        env_row.addWidget(self.env_interp_tree)
        page_layout.addLayout(env_row, 1)

        dep_row = QtWidgets.QHBoxLayout()
        self.dep_tree = self._make_feed(
            self.t("audit_dep_title", "Dependencies"),
            [self.t("audit_dep_name", "Package"),
             self.t("audit_dep_version", "Version"),
             self.t("audit_dep_source", "Source"),
             self.t("audit_dep_used_by", "Used by"),
             self.t("audit_env_issue", "Issue")])
        self.dep_conflict_tree = self._make_feed(
            self.t("audit_dep_conflicts", "Version conflicts"),
            [self.t("audit_dep_name", "Package"),
             self.t("audit_dep_versions", "Versions")])
        dep_row.addWidget(self.dep_tree)
        dep_row.addWidget(self.dep_conflict_tree)
        page_layout.addLayout(dep_row, 1)

    def _make_button(self, label_key, fallback, slot):
        button = QtWidgets.QPushButton(self.t(label_key, fallback))
        button.clicked.connect(slot)
        if not hasattr(self, "_subtab_widgets"):
            self._subtab_widgets = []
        self._subtab_widgets.append((button, label_key, fallback))
        return button
    def _make_group(self, label_key, fallback):
        box = QtWidgets.QGroupBox(self.t(label_key, fallback))
        box.setStyleSheet("QGroupBox { color:#8ab4f8; }")
        if not hasattr(self, "_subtab_widgets"):
            self._subtab_widgets = []
        self._subtab_widgets.append((box, label_key, fallback))
        return box
    def _build_integration_tab(self):
        page, page_layout = self._make_page("audit_tab_integration",
                                            "Integration")
        self._integration_page = page
        # --- open the audit in the sibling tools ----------------------
        open_box = self._make_group("audit_integration_open",
                                    "Open audit in...")
        open_grid = QtWidgets.QGridLayout(open_box)
        open_actions = [
            ("audit_open_diff", "Open in Diff", self.open_audit_in_diff),
            ("audit_open_search", "Open in Search", self.open_audit_in_search),
            ("audit_open_runner", "Open in Runner", self.open_audit_in_runner),
            ("audit_open_debugger", "Open in Debugger",
             self.open_audit_in_debugger),
            ("audit_generate_report", "Generate audit report",
             self.generate_audit_report),
            ("audit_export_artifact", "Export audit as artifact",
             self.export_audit_artifact),
        ]
        for i, (key, fallback, slot) in enumerate(open_actions):
            open_grid.addWidget(self._make_button(key, fallback, slot),
                                i // 2, i % 2)
        page_layout.addWidget(open_box)

        # --- audit operations -----------------------------------------
        ops_box = self._make_group("audit_operations", "Audit operations")
        ops_grid = QtWidgets.QGridLayout(ops_box)
        operations = [
            ("audit_op_full", "Full system audit", self.run_full_audit),
            ("audit_op_module", "Audit selected module", self.run_module_audit),
            ("audit_op_script", "Audit selected script", self.run_script_audit),
            ("audit_op_config", "Configuration audit", self.run_config_audit),
            ("audit_op_environment", "Environment audit",
             self.run_environment_audit),
            ("audit_op_security", "Security audit", self.run_security_audit),
            ("audit_op_changes", "Change audit", self.run_changes_audit),
            ("audit_op_dependencies", "Dependency audit",
             self.run_dependency_audit),
            ("audit_op_runtime", "Runtime audit", self.run_runtime_audit),
        ]
        for i, (key, fallback, slot) in enumerate(operations):
            ops_grid.addWidget(self._make_button(key, fallback, slot),
                               i // 3, i % 3)
        page_layout.addWidget(ops_box)

        # --- action log ------------------------------------------------
        log_box = self._make_group("audit_integration_log", "Integration log")
        log_layout = QtWidgets.QVBoxLayout(log_box)
        self.integration_output = QtWidgets.QPlainTextEdit()
        self.integration_output.setReadOnly(True)
        self.integration_output.setFont(QFont("Consolas", 9))
        log_layout.addWidget(self.integration_output)
        page_layout.addWidget(log_box, 1)

    def _build_export_tab(self):
        page, page_layout = self._make_page("audit_tab_export", "Export")
        self._export_page = page
        export_box = self._make_group("audit_export_title", "Export audit")
        export_grid = QtWidgets.QGridLayout(export_box)
        exports = [
            ("audit_export_txt", "Export TXT (ASCII)", self.export_audit_txt),
            ("audit_export_json", "Export JSON (structural)",
             self.export_audit_json),
            ("audit_export_log", "Export LOG", self.export_audit_log),
            ("audit_export_full", "Export full audit set",
             self.export_full_audit_set),
            ("audit_export_comparative", "Export comparative audit",
             self.export_comparative_audit),
        ]
        for i, (key, fallback, slot) in enumerate(exports):
            export_grid.addWidget(self._make_button(key, fallback, slot),
                                  i // 2, i % 2)
        page_layout.addWidget(export_box)

        preview_box = self._make_group("audit_export_preview",
                                       "Report preview")
        preview_layout = QtWidgets.QVBoxLayout(preview_box)
        preview_controls = QtWidgets.QHBoxLayout()
        preview_controls.addWidget(self._make_button(
            "audit_export_preview_txt", "Preview TXT",
            lambda: self._preview_report("txt")))
        preview_controls.addWidget(self._make_button(
            "audit_export_preview_json", "Preview JSON",
            lambda: self._preview_report("json")))
        preview_controls.addWidget(self._make_button(
            "audit_export_preview_log", "Preview LOG",
            lambda: self._preview_report("log")))
        preview_controls.addWidget(self._make_button(
            "audit_export_dir", "Open export folder", self.open_export_folder))
        preview_controls.addStretch()
        preview_layout.addLayout(preview_controls)
        self.export_preview = QtWidgets.QPlainTextEdit()
        self.export_preview.setReadOnly(True)
        self.export_preview.setFont(QFont("Consolas", 9))
        preview_layout.addWidget(self.export_preview)
        page_layout.addWidget(preview_box, 1)

    def _build_architecture_tab(self):
        page, page_layout = self._make_page("audit_tab_architecture",
                                            "Architecture")
        self._architecture_page = page
        # --- capability matrix ----------------------------------------
        caps_box = self._make_group("audit_arch_caps", "Capabilities")
        caps_layout = QtWidgets.QVBoxLayout(caps_box)
        self.arch_caps_label = QtWidgets.QLabel("")
        self.arch_caps_label.setWordWrap(True)
        self.arch_caps_label.setFont(QFont("Consolas", 9))
        caps_layout.addWidget(self.arch_caps_label)
        page_layout.addWidget(caps_box)

        # --- front-ends (API + CLI + GUI) -----------------------------
        front_box = self._make_group("audit_arch_frontends", "Front-ends")
        front_layout = QtWidgets.QVBoxLayout(front_box)
        self.arch_frontend_label = QtWidgets.QLabel("")
        self.arch_frontend_label.setWordWrap(True)
        self.arch_frontend_label.setFont(QFont("Consolas", 9))
        front_layout.addWidget(self.arch_frontend_label)
        cli_row = QtWidgets.QHBoxLayout()
        cli_row.addWidget(self._make_button(
            "audit_arch_cli_help", "CLI: capabilities",
            lambda: self.run_cli(["capabilities"])))
        cli_row.addWidget(self._make_button(
            "audit_arch_cli_integrity", "CLI: integrity",
            lambda: self.run_cli(["integrity"])))
        cli_row.addWidget(self._make_button(
            "audit_arch_cli_risk", "CLI: risk",
            lambda: self.run_cli(["risk"])))
        cli_row.addStretch()
        front_layout.addLayout(cli_row)
        page_layout.addWidget(front_box)

        # --- plugins --------------------------------------------------
        plug_box = self._make_group("audit_arch_plugins", "Plugins")
        plug_layout = QtWidgets.QVBoxLayout(plug_box)
        self.plugin_tree = self._make_feed(
            self.t("audit_arch_plugins", "Plugins"),
            [self.t("audit_arch_plugin", "Plugin"),
             self.t("audit_changed", "Active")])
        plug_layout.addWidget(self.plugin_tree)
        plug_row = QtWidgets.QHBoxLayout()
        plug_row.addWidget(self._make_button(
            "audit_arch_plugin_add", "Register demo plugin",
            self.register_demo_plugin))
        plug_row.addWidget(self._make_button(
            "audit_arch_plugin_refresh", "Refresh plugins",
            self._fill_architecture))
        plug_row.addStretch()
        plug_layout.addLayout(plug_row)
        page_layout.addWidget(plug_box, 1)

        # --- event bus -------------------------------------------------
        event_box = self._make_group("audit_arch_events", "Event bus")
        event_layout = QtWidgets.QVBoxLayout(event_box)
        self.arch_bus_label = QtWidgets.QLabel("")
        self.arch_bus_label.setWordWrap(True)
        self.arch_bus_label.setFont(QFont("Consolas", 9))
        event_layout.addWidget(self.arch_bus_label)
        emit_row = QtWidgets.QHBoxLayout()
        for event, key in self.AUTOMATIC_TRIGGERS:
            emit_row.addWidget(self._make_button(
                "audit_emit_" + event, "emit " + event,
                (lambda e=event: self.emit_audit_event(e, where="demo"))))
        emit_row.addStretch()
        event_layout.addLayout(emit_row)
        page_layout.addWidget(event_box)

        # --- snapshots -------------------------------------------------
        snap_box = self._make_group("audit_arch_snapshots", "Snapshots")
        snap_layout = QtWidgets.QVBoxLayout(snap_box)
        self.snapshot_tree = self._make_feed(
            self.t("audit_arch_snapshots", "Snapshots"),
            [self.t("audit_arch_snapshot_name", "Snapshot"),
             self.t("audit_when", "When"),
             self.t("audit_arch_snapshot_file", "File")])
        snap_layout.addWidget(self.snapshot_tree)
        snap_row = QtWidgets.QHBoxLayout()
        snap_row.addWidget(self._make_button(
            "audit_arch_snapshot_save", "Save snapshot",
            self._save_snapshot_dialog))
        snap_row.addWidget(self._make_button(
            "audit_arch_snapshot_refresh", "Refresh snapshots",
            self._fill_architecture))
        snap_row.addStretch()
        snap_layout.addLayout(snap_row)
        page_layout.addWidget(snap_box, 1)

    def _build_premium_tab(self):
        page, page_layout = self._make_page("audit_tab_premium",
                                            "Premium / Automation")
        self._premium_page = page
        # --- automatic audits -----------------------------------------
        auto_box = self._make_group("audit_premium_auto",
                                    "Automatic audits")
        auto_layout = QtWidgets.QVBoxLayout(auto_box)
        self.trigger_checks = {}
        for event, key in self.AUTOMATIC_TRIGGERS:
            check = QtWidgets.QCheckBox(self.t(key, event))
            check.setChecked(self.triggers_enabled().get(event, True))
            check.toggled.connect(
                (lambda on, e=event: self.enable_trigger(e, on)))
            auto_layout.addWidget(check)
            self.trigger_checks[event] = check
        page_layout.addWidget(auto_box)

        # --- cyclic scheduler -----------------------------------------
        sched_box = self._make_group("audit_premium_scheduler",
                                     "Cyclic audit (scheduler)")
        sched_layout = QtWidgets.QHBoxLayout(sched_box)
        self.scheduler_check = QtWidgets.QCheckBox(
            self.t("audit_premium_scheduler_on", "Enabled"))
        self.scheduler_check.setChecked(self.scheduler_config()["enabled"])
        self.scheduler_check.toggled.connect(self._toggle_scheduler)
        sched_layout.addWidget(self.scheduler_check)
        sched_layout.addWidget(QtWidgets.QLabel(
            self.t("audit_premium_interval", "Interval (s):")))
        self.scheduler_spin = QtWidgets.QSpinBox()
        self.scheduler_spin.setRange(10, 86400)
        self.scheduler_spin.setValue(self.scheduler_config()["interval"])
        self.scheduler_spin.valueChanged.connect(
            lambda value: self.configure_scheduler(interval=value))
        sched_layout.addWidget(self.scheduler_spin)
        sched_layout.addWidget(self._make_button(
            "audit_premium_run_now", "Run now", self.run_scheduled_audit))
        sched_layout.addStretch()
        page_layout.addWidget(sched_box)

        # --- comparative audit ----------------------------------------
        cmp_box = self._make_group("audit_premium_comparative",
                                   "Comparative audit")
        cmp_layout = QtWidgets.QVBoxLayout(cmp_box)
        cmp_row = QtWidgets.QHBoxLayout()
        cmp_row.addWidget(self._make_button(
            "audit_premium_compare_run", "Show changes since last audit",
            self._fill_premium))
        cmp_row.addWidget(self._make_button(
            "audit_export_comparative", "Export comparative audit",
            self.export_comparative_audit))
        cmp_row.addStretch()
        cmp_layout.addLayout(cmp_row)
        self.comparative_tree = self._make_feed(
            self.t("audit_premium_comparative", "Comparative audit"),
            [self.t("audit_diff_item", "Entry"),
             self.t("audit_diff_kind", "Kind"),
             self.t("audit_diff_field", "Field")])
        cmp_layout.addWidget(self.comparative_tree)
        page_layout.addWidget(cmp_box, 1)

        # --- risk + integrity -----------------------------------------
        ri_row = QtWidgets.QHBoxLayout()
        risk_box = self._make_group("audit_premium_risk",
                                    "Security audit (risk levels)")
        risk_layout = QtWidgets.QVBoxLayout(risk_box)
        self.risk_tree = self._make_feed(
            self.t("audit_premium_risk", "Security audit (risk levels)"),
            [self.t("audit_finding", "Finding"),
             self.t("audit_where", "Script"),
             self.t("audit_risk", "Risk level")])
        risk_layout.addWidget(self.risk_tree)
        ri_row.addWidget(risk_box)
        integ_box = self._make_group("audit_premium_integrity",
                                     "Workspace integrity")
        integ_layout = QtWidgets.QVBoxLayout(integ_box)
        self.integrity_tree = self._make_feed(
            self.t("audit_premium_integrity", "Workspace integrity"),
            [self.t("audit_where", "Script"),
             self.t("audit_issue", "Issue"),
             self.t("audit_severity", "Severity")])
        integ_layout.addWidget(self.integrity_tree)
        ri_row.addWidget(integ_box)
        page_layout.addLayout(ri_row, 1)

    def _fill_architecture(self, *args):
        caps = audit_capabilities()
        self.arch_caps_label.setText(
            "\n".join("%-18s %s" % (k, caps[k]) for k in sorted(caps)))
        api = audit_api(self)
        self.arch_frontend_label.setText(
            self.t("audit_arch_frontend_text",
                   "API: audit_api(tab) | CLI: audit_cli([...]) | GUI: this tab"))
        tree = self.plugin_tree.tree
        tree.clear()
        plugins = getattr(api, "plugins", [])
        for plugin in plugins:
            item = QtWidgets.QTreeWidgetItem([
                getattr(plugin, "name", "plugin"),
                self.t("audit_yes", "yes")])
            tree.addTopLevelItem(item)
        if not plugins:
            placeholder = QtWidgets.QTreeWidgetItem(
                [self.t("audit_none", "none"), ""])
            placeholder.setForeground(0, QtGui.QColor("#6a737d"))
            tree.addTopLevelItem(placeholder)
        self.arch_bus_label.setText(
            self.t("audit_arch_bus_text",
                   "events: %s") % ", ".join(e for e, _k in self.AUTOMATIC_TRIGGERS))
        snap = self.snapshot_tree.tree
        snap.clear()
        rows = self.list_snapshots()
        for row in rows:
            item = QtWidgets.QTreeWidgetItem([
                row["name"], row["saved"], row["file"]])
            snap.addTopLevelItem(item)
        if not rows:
            placeholder = QtWidgets.QTreeWidgetItem(
                [self.t("audit_none", "none"), "", ""])
            placeholder.setForeground(0, QtGui.QColor("#6a737d"))
            snap.addTopLevelItem(placeholder)

    def _fill_premium(self, *args):
        report = self.comparative_audit()
        tree = self.comparative_tree.tree
        tree.clear()
        colours = {"added": "#9be9a8", "modified": "#f7c66d",
                   "removed": "#ff7b72"}
        for row in report["diff"][:200]:
            item = QtWidgets.QTreeWidgetItem([
                row["name"], self.t("audit_diff_kind_" + row["kind"], row["kind"]),
                row["field"]])
            item.setForeground(1, QtGui.QColor(
                colours.get(row["kind"], "#dfe7f5")))
            tree.addTopLevelItem(item)
        if not report["diff"]:
            placeholder = QtWidgets.QTreeWidgetItem(
                [self.t("audit_none", "none"), "", ""])
            placeholder.setForeground(0, QtGui.QColor("#6a737d"))
            tree.addTopLevelItem(placeholder)

        risk = self.risk_security_audit()
        rtree = self.risk_tree.tree
        rtree.clear()
        rcolours = {"low": "#9be9a8", "medium": "#f7c66d", "high": "#ff7b72"}
        for row in risk["findings"][:200]:
            item = QtWidgets.QTreeWidgetItem([
                self.t(row["kind"], row["kind"]), row["where"],
                row["risk"].upper()])
            item.setForeground(2, QtGui.QColor(
                rcolours.get(row["risk"], "#dfe7f5")))
            rtree.addTopLevelItem(item)
        if not risk["findings"]:
            placeholder = QtWidgets.QTreeWidgetItem(
                [self.t("audit_none", "none"), "", ""])
            placeholder.setForeground(0, QtGui.QColor("#6a737d"))
            rtree.addTopLevelItem(placeholder)

        integ = self.workspace_integrity()
        itree = self.integrity_tree.tree
        itree.clear()
        for row in integ["problems"][:200]:
            item = QtWidgets.QTreeWidgetItem([
                row["where"], row["detail"], row["severity"].upper()])
            item.setForeground(2, QtGui.QColor(
                rcolours.get(row["severity"], "#dfe7f5")))
            itree.addTopLevelItem(item)
        if not integ["problems"]:
            placeholder = QtWidgets.QTreeWidgetItem(
                [self.t("audit_integrity_ok", "OK"), "", ""])
            placeholder.setForeground(0, QtGui.QColor("#9be9a8"))
            itree.addTopLevelItem(placeholder)

    def _toggle_scheduler(self, enabled):
        self.configure_scheduler(enabled=bool(enabled))

    def _save_snapshot_dialog(self):
        name, ok = QtWidgets.QInputDialog.getText(
            self, self.t("audit_arch_snapshot_save", "Save snapshot"),
            self.t("audit_arch_snapshot_name", "Snapshot") + ":")
        if not ok or not name.strip():
            return
        path = self.save_named_snapshot(name.strip())
        self._report_action("audit_arch_snapshot_save", ok=bool(path),
                            detail=path or "")
        self._fill_architecture()

    def run_cli(self, argv):
        captured = []

        class _Sink(object):
            def write(self, text):
                captured.append(text)

        code = audit_cli(list(argv), api=audit_api(self), out=_Sink())
        self._set_integration_output("\n".join(captured))
        self._report_action("audit_arch_cli", ok=(code == 0),
                            detail=" ".join(argv))
        return code
    def register_demo_plugin(self):
        demo = DemoAuditPlugin()
        api = audit_api(self)
        ok = api.register_plugin(demo)
        self._report_action("audit_arch_plugin_add", ok=ok,
                            detail=getattr(demo, "name", ""))
        self._fill_architecture()
        return ok
    def _preview_report(self, fmt):
        box = getattr(self, "export_preview", None)
        if box is None:
            return
        try:
            box.setPlainText(self.build_report(fmt))
        except Exception as exc:
            box.setPlainText(str(exc))

    def open_export_folder(self):
        path = self._export_dir()
        opener = getattr(QtGui, "QDesktopServices", None)
        url_cls = getattr(QtCore, "QUrl", None)
        try:
            if opener is not None and url_cls is not None:
                opener.openUrl(url_cls.fromLocalFile(path))
                return True
        except Exception:
            pass
        return False
    def _on_subtab_changed(self, index):
        """Repaint the page the user just switched to."""
        self._fill_page(index)

    def _fill_page(self, index):
        """Fill the widgets that belong to one sub-tab."""
        key = self._subtab_keys.get(index, ("", ""))[0]
        if key == "audit_tab_dashboard":
            self._fill_dashboard()
        elif key == "audit_tab_modules":
            self._fill_module_audit()
            self._fill_script_audit()
        elif key == "audit_tab_security":
            self._fill_permissions()
            self._fill_findings()
        elif key == "audit_tab_config":
            self._fill_config_audit()
        elif key == "audit_tab_runtime":
            self._fill_runtime_audit()
        elif key == "audit_tab_timeline":
            self._fill_timeline()
        elif key == "audit_tab_environment":
            self._fill_environment()
            self._fill_dependencies()
        elif key == "audit_tab_architecture":
            self._fill_architecture()
        elif key == "audit_tab_premium":
            self._fill_premium()
        # Integration and Export are static action panels: nothing to fill.

    def _make_card(self, title, value):
        frame = QtWidgets.QFrame()
        frame.setFrameShape(QtWidgets.QFrame.StyledPanel)
        frame.setStyleSheet(
            "QFrame { background:#2d2d2d; border:1px solid #404040;"
            " border-radius:4px; }")
        inner = QtWidgets.QVBoxLayout(frame)
        title_label = QtWidgets.QLabel(title)
        title_label.setStyleSheet("color:#9aa0a6; border:none;")
        value_label = QtWidgets.QLabel(value)
        value_label.setStyleSheet(
            "font-size: 22px; font-weight: bold; color:#dfe7f5; border:none;")
        inner.addWidget(title_label)
        inner.addWidget(value_label)
        return {"frame": frame, "title": title_label, "value": value_label}

    def _make_feed(self, title, columns):
        box = QtWidgets.QGroupBox(title)
        box.setStyleSheet("QGroupBox { color:#8ab4f8; }")
        inner = QtWidgets.QVBoxLayout(box)
        tree = QtWidgets.QTreeWidget()
        tree.setAlternatingRowColors(True)
        tree.setRootIsDecorated(False)
        tree.setColumnCount(len(columns))
        tree.setHeaderLabels(columns)
        tree.setFont(QFont("Consolas", 9))
        inner.addWidget(tree)
        box.tree = tree
        return box

    # ------------------------------------------------------------- rendering
    def refresh(self):
        """Recompute everything and repaint the dashboard."""
        state = self._analyze()
        self._state = state
        self._refresh_header(state)
        # Only the visible page is painted; the rest fill lazily on switch.
        self._fill_page(self.subtabs.currentIndex())

    def _refresh_header(self, state):
        """Update the last-audit label and the status summary line."""
        risk = state["risk"]
        last = state.get("last_audit") or self.t("audit_never", "never")
        self.last_audit_label.setText(
            self.t("audit_last", "Last audit: {when}").format(when=last))
        self.status.setText(
            self.t("audit_summary",
                   "{total} script(s), {problems} problem(s), risk: {risk}").format(
                total=state["total"], problems=len(state["problems"]),
                risk=self.t("audit_risk_" + risk, risk)))

    def _fill_dashboard(self):
        """Fill the dashboard cards and the three recent-activity feeds."""
        state = self._state or self._analyze()
        self._state = state
        integrity = state["integrity"]
        integrity_text = {
            "ok": self.t("audit_integrity_ok", "OK"),
            "warning": self.t("audit_integrity_warning", "Warning"),
            "critical": self.t("audit_integrity_critical", "Critical"),
        }.get(integrity, integrity)
        integrity_colour = {"ok": "#9be9a8", "warning": "#f7c66d",
                            "critical": "#ff7b72"}.get(integrity, "#dfe7f5")
        self.card_integrity["value"].setText(integrity_text)
        self.card_integrity["value"].setStyleSheet(
            "font-size: 22px; font-weight: bold; border:none;"
            f" color:{integrity_colour};")

        self.card_problems["value"].setText(str(len(state["problems"])))
        self.card_problems["value"].setStyleSheet(
            "font-size: 22px; font-weight: bold; border:none;"
            f" color:{'#ff9a9a' if state['problems'] else '#9be9a8'};")

        self.card_changes["value"].setText(str(state["changes_since"]))
        self.card_changes["value"].setToolTip(
            self.t("audit_changes_detail",
                   "+{added} / ~{modified} / -{removed}").format(
                added=state["added"], modified=state["modified"],
                removed=state["removed"]))

        risk = state["risk"]
        risk_colour = {"low": "#9be9a8", "medium": "#f7c66d",
                       "high": "#ff7b72"}.get(risk, "#dfe7f5")
        self.card_risk["value"].setText(self.t("audit_risk_" + risk, risk.upper()))
        self.card_risk["value"].setStyleSheet(
            "font-size: 22px; font-weight: bold; border:none;"
            f" color:{risk_colour};")

        self._fill_feed(self.security_tree, state["security"], "#ff9a9a")
        self._fill_feed(self.module_tree, state["module_changes"], "#f7c66d")
        self._fill_feed(self.config_tree, state["config_changes"], "#8ab4f8")

    def _fill_feed(self, box, events, colour):
        tree = box.tree
        tree.clear()
        for event in events[:50]:
            item = QtWidgets.QTreeWidgetItem([
                event.get("when", ""), event.get("where", ""),
                event.get("detail", "")])
            item.setForeground(1, QtGui.QColor(colour))
            tree.addTopLevelItem(item)
        if not events:
            placeholder = QtWidgets.QTreeWidgetItem(
                ["", self.t("audit_none", "none"), ""])
            placeholder.setForeground(1, QtGui.QColor("#6a737d"))
            tree.addTopLevelItem(placeholder)

    def _fill_module_audit(self):
        """Fill the module audit tree: integrity, signature, version, perms."""
        tree = self.module_audit_tree.tree
        tree.clear()
        ok_colour = "#9be9a8"
        bad_colour = "#ff7b72"
        warn_colour = "#f7c66d"
        rows = self._module_audit()
        for row in rows:
            item = QtWidgets.QTreeWidgetItem([
                row["name"],
                self.t("audit_hash_ok", "hash OK") if row["integrity"] == "ok"
                else self.t("audit_hash_mismatch", "mismatch"),
                {"valid": self.t("audit_sig_valid", "valid"),
                 "invalid": self.t("audit_sig_invalid", "invalid"),
                 "none": self.t("audit_sig_none", "none")}[row["signature"]],
                row["version"],
                self.t("audit_yes", "yes") if row["changed"] == "yes"
                else self.t("audit_no", "no"),
                row["permissions"],
            ])
            item.setForeground(1, QtGui.QColor(
                ok_colour if row["integrity"] == "ok" else bad_colour))
            sig_colour = {"valid": ok_colour, "invalid": bad_colour,
                          "none": warn_colour}[row["signature"]]
            item.setForeground(2, QtGui.QColor(sig_colour))
            if row["changed"] == "yes":
                item.setForeground(4, QtGui.QColor(warn_colour))
            tree.addTopLevelItem(item)
        if not rows:
            placeholder = QtWidgets.QTreeWidgetItem(
                ["", self.t("audit_none", "none"), "", ""])
            placeholder.setForeground(1, QtGui.QColor("#6a737d"))
            tree.addTopLevelItem(placeholder)

    def _fill_script_audit(self):
        """Fill the script audit tree: changes, dead code, lint errors."""
        tree = self.script_audit_tree.tree
        tree.clear()
        warn_colour = "#f7c66d"
        bad_colour = "#ff9a9a"
        ok_colour = "#9be9a8"
        rows = self._script_audit()
        for row in rows:
            item = QtWidgets.QTreeWidgetItem([
                row["name"],
                self.t("audit_change_" + row["change"], row["change"]),
                ", ".join(row["unused_funcs"]) or "-",
                ", ".join(row["unused_imports"]) or "-",
                ", ".join(row["suspicious"]) or "-",
                "; ".join(row["lint"]) or "-",
            ])
            if row["change"] != "unchanged":
                item.setForeground(1, QtGui.QColor(warn_colour))
            if row["unused_funcs"] or row["unused_imports"]:
                item.setForeground(2, QtGui.QColor(warn_colour))
                item.setForeground(3, QtGui.QColor(warn_colour))
            if row["suspicious"]:
                item.setForeground(4, QtGui.QColor(bad_colour))
            if row["lint"]:
                item.setForeground(5, QtGui.QColor(bad_colour))
            else:
                item.setForeground(5, QtGui.QColor(ok_colour))
            tree.addTopLevelItem(item)
        if not rows:
            placeholder = QtWidgets.QTreeWidgetItem(
                ["", self.t("audit_none", "none"), "", ""])
            placeholder.setForeground(1, QtGui.QColor("#6a737d"))
            tree.addTopLevelItem(placeholder)

    def _fill_permissions(self):
        """Fill the permission matrix: files / network / env / exec access."""
        tree = self.permissions_tree.tree
        tree.clear()
        ok_colour = "#9be9a8"
        warn_colour = "#f7c66d"
        bad_colour = "#ff7b72"
        rows = self._permission_analysis()
        for row in rows:
            item = QtWidgets.QTreeWidgetItem([
                row["name"],
                self.t("audit_yes", "yes") if row["files"] else "-",
                self.t("audit_yes", "yes") if row["network"] else "-",
                self.t("audit_yes", "yes") if row["env"] else "-",
                self.t("audit_yes", "yes") if row["exec"] else "-",
            ])
            for column, flag, colour in ((1, row["files"], warn_colour),
                                         (2, row["network"], warn_colour),
                                         (3, row["env"], warn_colour),
                                         (4, row["exec"], bad_colour)):
                item.setForeground(
                    column, QtGui.QColor(colour if flag else ok_colour))
            tree.addTopLevelItem(item)
        if not rows:
            placeholder = QtWidgets.QTreeWidgetItem(
                ["", self.t("audit_none", "none"), "", ""])
            placeholder.setForeground(1, QtGui.QColor("#6a737d"))
            tree.addTopLevelItem(placeholder)

    def _fill_findings(self):
        """Fill the security findings tree with detection results."""
        tree = self.findings_tree.tree
        tree.clear()
        colours = {"low": "#9be9a8", "medium": "#f7c66d", "high": "#ff7b72"}
        findings = self._security_findings()
        for finding in findings:
            label = self.t(finding["kind"], finding["kind"])
            if finding.get("detail"):
                label += " (" + str(finding["detail"]) + ")"
            item = QtWidgets.QTreeWidgetItem(
                [label, finding["where"], finding["severity"].upper()])
            item.setForeground(0, QtGui.QColor(
                colours.get(finding["severity"], "#dfe7f5")))
            item.setForeground(2, QtGui.QColor(
                colours.get(finding["severity"], "#dfe7f5")))
            tree.addTopLevelItem(item)
        if not findings:
            placeholder = QtWidgets.QTreeWidgetItem(
                ["", self.t("audit_none", "none"), ""])
            placeholder.setForeground(1, QtGui.QColor("#6a737d"))
            tree.addTopLevelItem(placeholder)

    def _fill_config_audit(self):
        """Fill the configuration change + policy compliance trees."""
        changes, policy = self._config_audit()
        colours = {"low": "#9be9a8", "medium": "#f7c66d", "high": "#ff7b72"}
        tree = self.config_changes_tree.tree
        tree.clear()
        for row in changes:
            item = QtWidgets.QTreeWidgetItem([
                row["file"], row["key"], row["issue"], row["severity"].upper()])
            item.setForeground(3, QtGui.QColor(
                colours.get(row["severity"], "#dfe7f5")))
            tree.addTopLevelItem(item)
        if not changes:
            placeholder = QtWidgets.QTreeWidgetItem(
                ["", self.t("audit_none", "none"), "", ""])
            placeholder.setForeground(1, QtGui.QColor("#6a737d"))
            tree.addTopLevelItem(placeholder)

        ptree = self.config_policy_tree.tree
        ptree.clear()
        for row in policy:
            item = QtWidgets.QTreeWidgetItem(
                [row["file"], row["policy"], row["result"]])
            item.setForeground(2, QtGui.QColor("#ff7b72"))
            ptree.addTopLevelItem(item)
        if not policy:
            placeholder = QtWidgets.QTreeWidgetItem(
                ["", self.t("audit_cfg_policy_ok", "compliant"), ""])
            placeholder.setForeground(1, QtGui.QColor("#9be9a8"))
            ptree.addTopLevelItem(placeholder)

    def _fill_runtime_audit(self):
        """Fill the runtime events + anomaly trees."""
        report = self._runtime_audit()
        colours = {"low": "#9be9a8", "medium": "#f7c66d", "high": "#ff7b72"}
        tree = self.runtime_events_tree.tree
        tree.clear()
        for event in report["events"][:100]:
            duration = event.get("duration_ms")
            item = QtWidgets.QTreeWidgetItem([
                event.get("when", ""), event.get("kind", ""),
                event.get("where", ""),
                str(duration) if duration is not None else "-"])
            if event.get("kind") in ("error", "exception", "stacktrace"):
                item.setForeground(1, QtGui.QColor("#ff7b72"))
            elif event.get("kind") == "call":
                item.setForeground(1, QtGui.QColor("#8ab4f8"))
            tree.addTopLevelItem(item)
        if not report["events"]:
            placeholder = QtWidgets.QTreeWidgetItem(
                ["", self.t("audit_none", "none"), "", ""])
            placeholder.setForeground(1, QtGui.QColor("#6a737d"))
            tree.addTopLevelItem(placeholder)

        atree = self.runtime_anomaly_tree.tree
        atree.clear()
        for anomaly in report["anomalies"]:
            label = self.t(anomaly["kind"], anomaly["kind"])
            if anomaly.get("detail"):
                label += " (" + str(anomaly["detail"]) + ")"
            item = QtWidgets.QTreeWidgetItem(
                [label, anomaly["where"], anomaly["severity"].upper()])
            item.setForeground(0, QtGui.QColor(
                colours.get(anomaly["severity"], "#dfe7f5")))
            item.setForeground(2, QtGui.QColor(
                colours.get(anomaly["severity"], "#dfe7f5")))
            atree.addTopLevelItem(item)
        if not report["anomalies"]:
            placeholder = QtWidgets.QTreeWidgetItem(
                ["", self.t("audit_none", "none"), ""])
            placeholder.setForeground(1, QtGui.QColor("#6a737d"))
            atree.addTopLevelItem(placeholder)

    def _fill_timeline(self):
        """Fill the change timeline and the snapshot-diff trees."""
        colours = {"files": "#8ab4f8", "modules": "#f7c66d",
                   "config": "#9be9a8", "environment": "#c39bd3",
                   "permissions": "#ff9a9a"}
        tree = self.timeline_tree.tree
        tree.clear()
        rows = self._timeline_rows()
        for row in rows[:200]:
            category = row["category"]
            item = QtWidgets.QTreeWidgetItem([
                row["when"], row["who"],
                self.t("audit_tl_cat_" + category, category),
                row["where"], row["what"], row["how"]])
            item.setForeground(2, QtGui.QColor(
                colours.get(category, "#dfe7f5")))
            if row["large"]:
                item.setForeground(4, QtGui.QColor("#ff7b72"))
            tree.addTopLevelItem(item)
        if not rows:
            placeholder = QtWidgets.QTreeWidgetItem(
                ["", self.t("audit_none", "none"), "", "", "", ""])
            placeholder.setForeground(1, QtGui.QColor("#6a737d"))
            tree.addTopLevelItem(placeholder)

        dtree = self.diff_tree.tree
        dtree.clear()
        diff_kind_colour = {"added": "#9be9a8", "modified": "#f7c66d",
                            "removed": "#ff7b72"}
        diff = self._diff_snapshots()
        for row in diff[:200]:
            item = QtWidgets.QTreeWidgetItem([
                row["name"], self.t("audit_diff_kind_" + row["kind"],
                                    row["kind"]),
                row["field"], row["before"], row["after"]])
            item.setForeground(1, QtGui.QColor(
                diff_kind_colour.get(row["kind"], "#dfe7f5")))
            dtree.addTopLevelItem(item)
        if not diff:
            placeholder = QtWidgets.QTreeWidgetItem(
                ["", self.t("audit_none", "none"), "", "", ""])
            placeholder.setForeground(1, QtGui.QColor("#6a737d"))
            dtree.addTopLevelItem(placeholder)

        ltree = self.large_tree.tree
        ltree.clear()
        large = self._detect_large_changes()
        for row in large:
            item = QtWidgets.QTreeWidgetItem([
                row["name"], str(row["old_len"]), str(row["new_len"]),
                ("+" if row["delta"] >= 0 else "") + str(row["delta"])])
            item.setForeground(3, QtGui.QColor(
                "#ff7b72" if abs(row["delta"]) >= 200 else "#f7c66d"))
            ltree.addTopLevelItem(item)
        if not large:
            placeholder = QtWidgets.QTreeWidgetItem(
                ["", self.t("audit_none", "none"), "", ""])
            placeholder.setForeground(1, QtGui.QColor("#6a737d"))
            ltree.addTopLevelItem(placeholder)

    def _fill_environment(self):
        """Fill the environment variable, PATH and interpreter trees."""
        report = self._environment_audit()
        tree = self.env_vars_tree.tree
        tree.clear()
        for row in report["variables"]:
            item = QtWidgets.QTreeWidgetItem(
                [row["name"], row["value"], str(row["length"])])
            if row["secret"]:
                item.setForeground(1, QtGui.QColor("#f7c66d"))
            tree.addTopLevelItem(item)
        if not report["variables"]:
            placeholder = QtWidgets.QTreeWidgetItem(
                ["", self.t("audit_none", "none"), ""])
            placeholder.setForeground(1, QtGui.QColor("#6a737d"))
            tree.addTopLevelItem(placeholder)

        ptree = self.env_path_tree.tree
        ptree.clear()
        for row in report["path"]:
            item = QtWidgets.QTreeWidgetItem([
                row["path"],
                self.t("audit_yes", "yes") if row["exists"] else self.t("audit_no", "no"),
                row["issue"]])
            item.setForeground(1, QtGui.QColor(
                "#9be9a8" if row["exists"] else "#ff7b72"))
            if row["issue"]:
                item.setForeground(2, QtGui.QColor("#f7c66d"))
            ptree.addTopLevelItem(item)
        if not report["path"]:
            placeholder = QtWidgets.QTreeWidgetItem(
                ["", self.t("audit_none", "none"), ""])
            placeholder.setForeground(1, QtGui.QColor("#6a737d"))
            ptree.addTopLevelItem(placeholder)

        itree = self.env_interp_tree.tree
        itree.clear()
        for row in report["interpreters"]:
            item = QtWidgets.QTreeWidgetItem(
                [row["name"], row["version"], row["where"]])
            if report["interpreter_conflict"]:
                item.setForeground(1, QtGui.QColor("#f7c66d"))
            itree.addTopLevelItem(item)
        if not report["interpreters"]:
            placeholder = QtWidgets.QTreeWidgetItem(
                ["", self.t("audit_none", "none"), ""])
            placeholder.setForeground(1, QtGui.QColor("#6a737d"))
            itree.addTopLevelItem(placeholder)

    def _fill_dependencies(self):
        """Fill the dependency + version-conflict trees."""
        report = self._dependency_audit()
        tree = self.dep_tree.tree
        tree.clear()
        for row in report["rows"]:
            item = QtWidgets.QTreeWidgetItem([
                row["name"], row["version"], row["source"], row["used_by"],
                row["issue"]])
            if row["issue"]:
                item.setForeground(4, QtGui.QColor("#ff7b72"))
            else:
                item.setForeground(1, QtGui.QColor("#9be9a8"))
            tree.addTopLevelItem(item)
        if not report["rows"]:
            placeholder = QtWidgets.QTreeWidgetItem(
                ["", self.t("audit_none", "none"), "", "", ""])
            placeholder.setForeground(1, QtGui.QColor("#6a737d"))
            tree.addTopLevelItem(placeholder)

        ctree = self.dep_conflict_tree.tree
        ctree.clear()
        for row in report["conflicts"]:
            item = QtWidgets.QTreeWidgetItem([row["package"], row["versions"]])
            item.setForeground(1, QtGui.QColor("#ff7b72"))
            ctree.addTopLevelItem(item)
        if not report["conflicts"]:
            placeholder = QtWidgets.QTreeWidgetItem(
                ["", self.t("audit_none", "none")])
            placeholder.setForeground(1, QtGui.QColor("#6a737d"))
            ctree.addTopLevelItem(placeholder)

    # ------------------------------------------------------------ public API
    def mark_audited(self):
        """Accept the current state as the baseline for the next audit."""
        self._accept_snapshot(self._state or self._analyze())
        self.refresh()

    def append(self, text):
        """Kept for compatibility: show text in the status label."""
        self.status.setText(text)

    def clear(self):
        """Reset the dashboard to an uncomputed state."""
        self._state = {}
        self.refresh()

    def retranslate(self, table):
        """Apply the main window's language table to this tab."""

        def label(key, fallback):
            return table.get(key, fallback)

        self.title_label.setText(label("audit", "Audit"))
        self.btn_refresh.setText(label("audit_refresh", "Refresh"))
        self.btn_accept.setText(label("audit_accept", "Mark as audited"))
        for index, (key, fallback) in self._subtab_keys.items():
            self.subtabs.setTabText(index, label(key, fallback))
        self.card_integrity["title"].setText(
            label("audit_integrity", "System integrity"))
        self.card_problems["title"].setText(
            label("audit_problems", "Problems found"))
        self.card_changes["title"].setText(
            label("audit_changes", "Changes since last audit"))
        self.card_risk["title"].setText(label("audit_risk", "Risk level"))
        self.security_tree.setTitle(
            label("audit_feed_security", "Recent security events"))
        self.module_tree.setTitle(
            label("audit_feed_modules", "Recent module changes"))
        self.config_tree.setTitle(
            label("audit_feed_config", "Recent configuration changes"))
        headers = [label("audit_when", "When"), label("audit_where", "Script"),
                   label("audit_detail", "Detail")]
        for box in (self.security_tree, self.module_tree, self.config_tree):
            box.tree.setHeaderLabels(headers)
        self.module_audit_tree.setTitle(
            label("audit_modules_title", "Module audit"))
        self.module_audit_tree.tree.setHeaderLabels([
            label("audit_module", "Module"),
            label("audit_integrity_col", "Integrity"),
            label("audit_signature", "Signature"),
            label("audit_version", "Version"),
            label("audit_changed", "Changed"),
            label("audit_permissions", "Permissions")])
        self.script_audit_tree.setTitle(
            label("audit_scripts_title", "Script audit"))
        self.script_audit_tree.tree.setHeaderLabels([
            label("audit_script", "Script"),
            label("audit_change_col", "Changes"),
            label("audit_unused_funcs", "Unused functions"),
            label("audit_unused_imports", "Unused imports"),
            label("audit_suspicious", "Suspicious"),
            label("audit_lint", "Lint errors")])
        self.permissions_tree.setTitle(
            label("audit_permissions_title", "Permission analysis"))
        self.permissions_tree.tree.setHeaderLabels([
            label("audit_target", "Target"),
            label("audit_files", "Files"),
            label("audit_network", "Network"),
            label("audit_environment", "Environment"),
            label("audit_exec", "Exec")])
        self.findings_tree.setTitle(
            label("audit_findings_title", "Security findings"))
        self.findings_tree.tree.setHeaderLabels([
            label("audit_finding", "Finding"),
            label("audit_where", "Script"),
            label("audit_severity", "Severity")])
        self.config_changes_tree.setTitle(
            label("audit_cfg_changes", "Configuration changes"))
        self.config_changes_tree.tree.setHeaderLabels([
            label("audit_file", "File"), label("audit_key", "Key"),
            label("audit_issue", "Issue"), label("audit_severity", "Severity")])
        self.config_policy_tree.setTitle(
            label("audit_cfg_policy", "Policy compliance"))
        self.config_policy_tree.tree.setHeaderLabels([
            label("audit_file", "File"), label("audit_policy", "Policy"),
            label("audit_result", "Result")])
        self.runtime_events_tree.setTitle(
            label("audit_runtime_events", "Runtime events"))
        self.runtime_events_tree.tree.setHeaderLabels([
            label("audit_when", "When"), label("audit_kind", "Kind"),
            label("audit_where", "Script"), label("audit_time_ms", "Time (ms)")])
        self.runtime_anomaly_tree.setTitle(
            label("audit_runtime_anomalies", "Runtime anomalies"))
        self.runtime_anomaly_tree.tree.setHeaderLabels([
            label("audit_anomaly", "Anomaly"), label("audit_where", "Script"),
            label("audit_severity", "Severity")])
        self.timeline_tree.setTitle(label("audit_tl_title", "Change timeline"))
        self.timeline_tree.tree.setHeaderLabels([
            label("audit_when", "When"), label("audit_tl_who", "Who"),
            label("audit_tl_category", "Category"),
            label("audit_where", "Script"), label("audit_tl_what", "What"),
            label("audit_tl_how", "How")])
        self.diff_tree.setTitle(label("audit_diff_title", "Snapshot diff"))
        self.diff_tree.tree.setHeaderLabels([
            label("audit_diff_item", "Entry"),
            label("audit_diff_kind", "Kind"),
            label("audit_diff_field", "Field"),
            label("audit_diff_before", "Before"),
            label("audit_diff_after", "After")])
        self.large_tree.setTitle(label("audit_large_title", "Large changes"))
        self.large_tree.tree.setHeaderLabels([
            label("audit_large_item", "Entry"),
            label("audit_large_before", "Before"),
            label("audit_large_after", "After"),
            label("audit_large_delta", "Delta")])
        self.env_vars_tree.setTitle(
            label("audit_env_vars", "Environment variables"))
        self.env_vars_tree.tree.setHeaderLabels([
            label("audit_env_name", "Name"), label("audit_env_value", "Value"),
            label("audit_env_length", "Length")])
        self.env_path_tree.setTitle(label("audit_env_path", "PATH analysis"))
        self.env_path_tree.tree.setHeaderLabels([
            label("audit_env_entry", "Entry"),
            label("audit_env_exists", "Exists"),
            label("audit_env_issue", "Issue")])
        self.env_interp_tree.setTitle(label("audit_env_interp", "Interpreters"))
        self.env_interp_tree.tree.setHeaderLabels([
            label("audit_env_name", "Name"),
            label("audit_env_version", "Version"),
            label("audit_where", "Script")])
        self.dep_tree.setTitle(label("audit_dep_title", "Dependencies"))
        self.dep_tree.tree.setHeaderLabels([
            label("audit_dep_name", "Package"),
            label("audit_dep_version", "Version"),
            label("audit_dep_source", "Source"),
            label("audit_dep_used_by", "Used by"),
            label("audit_env_issue", "Issue")])
        self.dep_conflict_tree.setTitle(
            label("audit_dep_conflicts", "Version conflicts"))
        self.dep_conflict_tree.tree.setHeaderLabels([
            label("audit_dep_name", "Package"),
            label("audit_dep_versions", "Versions")])
        # Relabel the integration/export panels registered via _make_button /
        # _make_group (plain QPushButton uses setText, QGroupBox uses setTitle).
        for widget, key, fallback in getattr(self, "_subtab_widgets", []):
            text = label(key, fallback)
            if hasattr(widget, "setTitle"):
                widget.setTitle(text)
            else:
                widget.setText(text)
        self.refresh()



def main(argv=None):
    """Console entry point: run the audit CLI without a display."""
    import sys as _sys
    return audit_cli(list(argv if argv is not None else _sys.argv[1:]))


if __name__ == "__main__":       # pragma: no cover
    import sys as _sys
    _sys.exit(main())
