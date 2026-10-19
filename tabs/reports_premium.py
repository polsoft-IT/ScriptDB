# -*- coding: utf-8 -*-
"""The premium report features: automatic generation, scheduling and the
three specialised report kinds.

`tabs/reports_tab.py` owns the report repository and `tabs/reports_analysis.py`
owns reading reports. This owns *producing* reports without a user asking:

    PREMIUM_FEATURES   the nine premium features, as a table
    TRIGGERS           the five automatic-generation events, as a table
    fire               record one trigger and say which generators it runs
    is_due / due_items the scheduler: which cyclic job is due at a moment
    comparative_report "what changed since the last run"
    env_diff_report    the environment diff between two captures
    security_report    the permissions audit
    run                dispatch one premium feature by name

Two decisions worth stating:

    * **A trigger is data, the running of it is not.** `TRIGGERS` says which
      generators an event runs and whether generation is automatic by default;
      the tab owns the QTimer that polls for file changes. That split is what
      lets the whole automation layer be tested from a shell with no Qt.
    * **A comparative report compares against a *named* baseline.** "What
      changed since the last run" is unanswerable without saying which run, so
      the generator takes a baseline report and a current state and writes the
      difference -- never "the previous file, whatever that means".
"""

import datetime as dt
import json
import os

# ---------------------------------------------------------------------------
# The premium features
# ---------------------------------------------------------------------------
# (key, label key, fallback, what it gives a user)
PREMIUM_FEATURES = (
    ("auto_generate", "reports_prem_auto", "Automatic report generation",
     "report artifacts written by the application itself when a watched event "
     "happens, with no button pressed"),
    ("scheduler", "reports_prem_scheduler", "Cyclic reports (scheduler)",
     "reports produced on a schedule -- hourly, daily or weekly -- and "
     "de-duplicated so a restart does not produce a burst of catch-up runs"),
    ("comparative", "reports_prem_comparative", "Comparative reports",
     "\"what changed since the last run\": a diff of the state between a "
     "named baseline and now"),
    ("env_diff", "reports_prem_env", "Environment reports (env diff)",
     "the difference between two environment captures: variables, paths, "
     "versions and platform"),
    ("security", "reports_prem_security", "Security reports (permissions)",
     "a permissions audit: file modes, owners, world-writable paths and "
     "sensitive files that are too open"),
    ("auto_change", "reports_prem_change", "Automatic report after file change",
     "a report written when a watched file changes on disk"),
    ("auto_error", "reports_prem_error", "Automatic report after an error",
     "a report written when a run fails, so the failure leaves an artifact"),
    ("auto_deploy", "reports_prem_deploy", "Automatic report after deploy",
     "a report written after a deployment, capturing what was deployed"),
    ("auto_build", "reports_prem_build", "Automatic report after build",
     "a report written after a build or compile step"),
)

PREMIUM_KEYS = tuple(key for key, _lk, _f, _d in PREMIUM_FEATURES)
PREMIUM_LABELS = {key: fallback for key, _lk, fallback, _d in PREMIUM_FEATURES}
PREMIUM_MEANINGS = {key: meaning for key, _lk, _f, meaning in PREMIUM_FEATURES}

# ---------------------------------------------------------------------------
# The automatic-generation triggers
# ---------------------------------------------------------------------------
# (key, label key, fallback, the generators it runs, whether it is on by
# default). The generators are the keys handled by `run`, so a trigger can
# only name a generator this module actually implements.
TRIGGERS = (
    ("after_build", "reports_trig_build", "After a build",
     ("build",), True),
    ("after_run", "reports_trig_run", "After a script runs",
     ("run",), True),
    ("after_error", "reports_trig_error", "After an error",
     ("error", "comparative"), True),
    ("after_deploy", "reports_trig_deploy", "After a deploy",
     ("deploy",), False),
    ("after_change", "reports_trig_change", "After a file change",
     ("change", "comparative"), False),
)

TRIGGER_KEYS = tuple(key for key, _lk, _f, _g, _d in TRIGGERS)
TRIGGER_LABELS = {key: fallback for key, _lk, fallback, _g, _d in TRIGGERS}
TRIGGER_GENERATORS = {key: generators for key, _lk, _f, generators, _d
                      in TRIGGERS}
TRIGGER_DEFAULT = {key: bool(default) for key, _lk, _f, _g, default
                   in TRIGGERS}

# The report kinds a trigger writes, so a generated artifact is classified by
# its own name without a registration step.
TRIGGER_KINDS = {
    "after_build": "build",
    "after_run": "summary",
    "after_error": "error",
    "after_deploy": "deploy",
    "after_change": "diff",
}

# ---------------------------------------------------------------------------
# The schedule cadences
# ---------------------------------------------------------------------------
# (key, label key, fallback, seconds between runs)
CADENCES = (
    ("hourly", "reports_cad_hourly", "Every hour", 3600),
    ("daily", "reports_cad_daily", "Every day", 86400),
    ("weekly", "reports_cad_weekly", "Every week", 604800),
)

CADENCE_KEYS = tuple(key for key, _lk, _f, _s in CADENCES)
CADENCE_LABELS = {key: fallback for key, _lk, fallback, _s in CADENCES}
CADENCE_SECONDS = {key: seconds for key, _lk, _f, seconds in CADENCES}

# The jobs the scheduler can run. Each names a generator this module owns.
SCHEDULE_JOBS = ("comparative", "env_diff", "security", "metrics")
SCHEDULE_JOB_LABELS = {
    "comparative": "Comparative (since last run)",
    "env_diff": "Environment diff",
    "security": "Permissions audit",
    "metrics": "Metrics snapshot",
}

# Something is "sensitive" when it is a name that usually holds a secret or a
# key, and "too open" when the mode lets group or world write it.
SENSITIVE_NAMES = (".env", "id_rsa", "id_dsa", "credentials", ".netrc",
                   "secret", "password", ".pem", ".key", "token")
OPEN_BITS = 0o022  # group-write or other-write


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------
def result(action, ok, detail="", path="", data=None):
    """One premium outcome, in the shape every function here returns."""
    return {
        "ok": bool(ok),
        "action": action,
        "detail": detail,
        "path": path,
        "data": dict(data or {}),
    }


def _now():
    return dt.datetime.now()


def _stamp(value=None):
    return (value or _now()).strftime("%Y-%m-%d %H:%M:%S")


def _slug(text):
    out = "".join(ch if ch.isalnum() else "_" for ch in str(text or "report"))
    return out.strip("_")[:48] or "report"


# ---------------------------------------------------------------------------
# Writing a report file (the one place anything is written)
# ---------------------------------------------------------------------------
def write_report(directory, name, lines, kind="summary", header=None):
    """Write one report artifact and return its path.

    The body is a list of lines (each already ASCII), a small header is added
    naming the kind and the generation time, and the file is written under
    `directory` (created when missing). This is the only function here that
    touches the disk, so every generated report has the same header shape and
    the Reports tab classifies it the same way.
    """
    if not directory:
        return result("write", False, "no report directory")
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError as exc:
        return result("write", False, str(exc))
    stamp = _stamp()
    head = ["# %s report" % kind, "generated: %s" % stamp, ""]
    if header:
        head += list(header) + [""]
    body = "\n".join(head + list(lines)) + "\n"
    path = os.path.join(directory, "%s_%s.txt"
                        % (_slug(name), stamp.replace(":", "").replace(" ", "_")))
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(body)
    except OSError as exc:
        return result("write", False, str(exc))
    return result("write", True, "wrote %s" % os.path.basename(path), path,
                  {"lines": len(body.splitlines()), "kind": kind})


# ---------------------------------------------------------------------------
# Comparative reports ("what changed since the last run")
# ---------------------------------------------------------------------------
def comparative_report(baseline, current, label="run"):
    """The difference between a baseline and the current state.

    Both are dicts of name -> value (a run summary, a metrics row, a set of
    file hashes). The result lists what appeared, what vanished and what
    changed value, so "what changed since the last run" is answered as data
    and not as prose.
    """
    baseline = baseline or {}
    current = current or {}
    added = {key: current[key] for key in current if key not in baseline}
    removed = {key: baseline[key] for key in baseline if key not in current}
    changed = {key: (baseline[key], current[key]) for key in current
               if key in baseline and baseline[key] != current[key]}
    return {
        "label": label,
        "added": added,
        "removed": removed,
        "changed": changed,
        "identical": not (added or removed or changed),
    }


def render_comparative(comparison):
    """A comparative analysis as report lines."""
    lines = ["Comparison: %s" % comparison.get("label", "run"), ""]
    if comparison.get("identical"):
        return lines + ["Nothing changed since the baseline."]
    for title, mapping in (("Added", comparison.get("added", {})),
                           ("Removed", comparison.get("removed", {})),
                           ("Changed", comparison.get("changed", {}))):
        if not mapping:
            continue
        lines.append("%s:" % title)
        for key in sorted(mapping, key=str):
            value = mapping[key]
            if title == "Changed":
                lines.append("  %-28s %s -> %s" % (key, value[0], value[1]))
            else:
                lines.append("  %-28s %s" % (key, value))
        lines.append("")
    return lines


def write_comparative(directory, baseline, current, label="run"):
    """Write a comparative report and return its path."""
    comparison = comparative_report(baseline, current, label)
    return write_report(directory, "comparative_%s" % label,
                        render_comparative(comparison), kind="diff",
                        header=["baseline: %s" % _stamp(),
                                "items: %d changed, %d added, %d removed"
                                % (len(comparison["changed"]),
                                   len(comparison["added"]),
                                   len(comparison["removed"]))])


# ---------------------------------------------------------------------------
# Environment reports (env diff)
# ---------------------------------------------------------------------------
def capture_environment(environ=None, versions=None):
    """A snapshot of the environment as a flat name -> value mapping.

    Reads the process environment, the Python version and the platform, then
    folds them into one mapping with a stable prefix, so two captures compare
    key by key. The default is the real environment; a caller may pass one in
    (which is what makes the diff testable without mutating anything).
    """
    import platform
    import sys
    environ = dict(os.environ if environ is None else environ)
    versions = dict(versions or {})
    capture = {"platform": platform.platform(),
               "python": sys.version.split()[0],
               "machine": platform.machine()}
    capture.update(versions)
    for key in sorted(environ):
        capture["env:%s" % key] = environ[key]
    return capture


def env_diff(baseline, current):
    """The difference between two environment captures."""
    return comparative_report(baseline, current, "environment")


def render_env_diff(diff):
    """An environment diff as report lines."""
    return render_comparative(diff)


def write_env_diff(directory, baseline, current=None):
    """Write an env-diff report and return its path."""
    current = capture_environment() if current is None else current
    diff = env_diff(baseline, current)
    return write_report(directory, "env_diff", render_env_diff(diff),
                        kind="environment",
                        header=["variables compared: %d" % len(current)])


# ---------------------------------------------------------------------------
# Security reports (permissions audit)
# ---------------------------------------------------------------------------
def _mode_text(mode):
    """A permission mode as the familiar rwxr-xr-x string (best effort)."""
    flags = "rwxrwxrwx"
    return "".join(flags[index] if mode & (1 << (8 - index)) else "-"
                   for index in range(9))


def audit_permissions(root, limit=2000):
    """A permissions audit of a directory tree.

    Every entry under `root` is described by its mode; an entry is flagged
    'world_writable' when either the group or the other write bit is set, and
    'sensitive' when its name looks like a secret. Returns a list of findings,
    most severe first -- the two flags are what a reader wants at the top, not
    the alphabetical order of the tree.
    """
    findings = []
    if not root or not os.path.isdir(root):
        return findings
    count = 0
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", ".venv")]
        for name in sorted(files) + sorted(dirs):
            count += 1
            if count > limit:
                return findings
            path = os.path.join(base, name)
            try:
                stat = os.stat(path)
            except OSError:
                continue
            mode = stat.st_mode & 0o7777
            lowered = name.lower()
            sensitive = any(word in lowered for word in SENSITIVE_NAMES)
            world_writable = bool(mode & OPEN_BITS)
            if not (sensitive or world_writable):
                continue
            findings.append({
                "path": path,
                "mode": _mode_text(mode),
                "octal": oct(mode),
                "size": stat.st_size,
                "world_writable": world_writable,
                "sensitive": sensitive,
                "severity": "high" if (sensitive and world_writable)
                            else ("medium" if world_writable else "low"),
            })
    order = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda item: (order[item["severity"]], item["path"]))
    return findings


def render_security(findings):
    """A permissions audit as report lines."""
    lines = ["Permissions audit", "================="]
    if not findings:
        return lines + ["No open or sensitive file found."]
    counts = {}
    for finding in findings:
        counts[finding["severity"]] = counts.get(finding["severity"], 0) + 1
    lines.append("findings: %d (%s)" % (
        len(findings), ", ".join("%s=%d" % (k, counts[k])
                                 for k in sorted(counts))))
    lines.append("")
    for finding in findings:
        flags = []
        if finding["sensitive"]:
            flags.append("sensitive")
        if finding["world_writable"]:
            flags.append("world-writable")
        lines.append("%-8s %-10s %-40s [%s]"
                     % (finding["severity"], finding["mode"],
                        finding["path"][-40:], ",".join(flags)))
    return lines


def write_security(directory, root):
    """Write a permissions-audit report and return its path."""
    findings = audit_permissions(root)
    return write_report(directory, "security_permissions",
                        render_security(findings), kind="audit",
                        header=["root: %s" % root,
                                "findings: %d" % len(findings)])


# ---------------------------------------------------------------------------
# The other trigger generators
# ---------------------------------------------------------------------------
def _state_lines(payload):
    """A payload as report lines (alphabetical, one key per line)."""
    payload = payload or {}
    return ["%-28s %s" % (key, payload[key]) for key in sorted(payload,
                                                               key=str)]


def _trigger_report(directory, trigger, payload):
    """Write the report a trigger names, from the payload it was given."""
    lines = ["Trigger: %s" % trigger, "", "Details:", ""]
    lines += _state_lines(payload)
    return write_report(directory, trigger, lines,
                        kind=TRIGGER_KINDS.get(trigger, "summary"),
                        header=["trigger: %s" % trigger])


# ---------------------------------------------------------------------------
# The scheduler
# ---------------------------------------------------------------------------
def _parse_stamp(value):
    if isinstance(value, dt.datetime):
        return value
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(value)
    for fmt, length in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d", 10)):
        try:
            return dt.datetime.strptime(str(value)[:length], fmt)
        except (ValueError, TypeError):
            continue
    return None


def is_due(cadence, last_run, now=None):
    """Whether a cyclic job is due, given when it last ran.

    A job that has never run is due; otherwise it is due once its cadence has
    elapsed. Comparing timestamps rather than counting ticks is what keeps a
    restart from producing a burst of catch-up runs: the schedule is a fact
    about the clock, not about how many times the timer fired.
    """
    seconds = CADENCE_SECONDS.get(cadence)
    if seconds is None:
        return False
    now = now or _now()
    when = _parse_stamp(last_run)
    if when is None:
        return True
    return (now - when).total_seconds() >= seconds


def due_jobs(jobs, state, now=None):
    """The names of the jobs that are due, from a job -> cadence mapping.

    `jobs` is a mapping of job name to cadence; `state` maps a job name to the
    stamp it last ran at. Returns the due job names in a stable order.
    """
    now = now or _now()
    return [name for name in SCHEDULE_JOBS if name in jobs
            and is_due(jobs[name], (state or {}).get(name), now)]


def run_scheduled(directory, jobs, state, now=None, env_baseline=None,
                  baseline=None):
    """Run every due job, write its report, and return what was written.

    `jobs` maps a job name to a cadence and `state` is the last-run mapping
    (updated in place). The jobs are the ones in :data:`SCHEDULE_JOBS`; each
    writes its own artifact through the same writer the interactive features
    use, so a scheduled report and a manual one are indistinguishable on disk.
    """
    now = now or _now()
    written = []
    for name in due_jobs(jobs, state, now):
        if name == "comparative":
            outcome = write_comparative(directory, baseline or {}, {},
                                        label="scheduled")
        elif name == "env_diff":
            outcome = write_env_diff(directory, env_baseline
                                     or capture_environment())
        elif name == "security":
            outcome = write_security(directory, directory)
        else:  # metrics snapshot: a small stand-in the caller can read.
            outcome = write_report(directory, "metrics_snapshot",
                                   _state_lines({"job": name,
                                                 "at": _stamp(now)}),
                                   kind="metrics")
        if outcome.get("ok"):
            state[name] = now
            written.append(outcome)
    return {"written": written, "count": len(written), "at": _stamp(now)}


# ---------------------------------------------------------------------------
# Dispatching a premium feature
# ---------------------------------------------------------------------------
def run(key, directory="", payload=None, **kwargs):
    """Run one premium feature by name: ``{ok, action, detail, path, data}``.

    `key` is one of :data:`PREMIUM_KEYS` (or a trigger key, for the automatic
    ones). The generators are the only things that write; this dispatcher just
    decides which one a name means.
    """
    key = str(key or "").strip().lower()
    payload = dict(payload or {})
    if key in TRIGGER_KEYS or key in ("build", "run", "error", "deploy",
                                      "change"):
        trigger = key if key in TRIGGER_KEYS else "after_%s" % key
        wrote = []
        for generator in TRIGGER_GENERATORS.get(trigger, (key,)):
            if generator == "comparative":
                wrote.append(write_comparative(directory,
                                               payload.get("baseline") or {},
                                               payload.get("current") or {},
                                               label=trigger))
            else:
                wrote.append(_trigger_report(directory, trigger, payload))
        ok = all(item.get("ok") for item in wrote) if wrote else False
        return result(key, ok, "wrote %d report(s)" % sum(
            1 for item in wrote if item.get("ok")),
            wrote[0]["path"] if wrote else "",
            {"written": [item["path"] for item in wrote]})
    if key == "comparative":
        return write_comparative(directory, payload.get("baseline") or {},
                                 payload.get("current") or {},
                                 label=payload.get("label", "run"))
    if key == "env_diff":
        return write_env_diff(directory, payload.get("baseline")
                              or capture_environment(), payload.get("current"))
    if key == "security":
        return write_security(directory, payload.get("root") or directory)
    if key == "scheduler":
        plan = payload.get("jobs") or {name: "daily" for name in SCHEDULE_JOBS}
        return result("scheduler", True,
                      "due: %s" % ", ".join(due_jobs(plan, payload.get("state")
                                                     or {})) or "none",
                      data={"jobs": plan})
    return result(key, False, "unknown premium feature %r (known: %s)"
                  % (key, ", ".join(PREMIUM_KEYS)))


# ---------------------------------------------------------------------------
# Describing the surface
# ---------------------------------------------------------------------------
def format_premium():
    """The nine premium features as ASCII lines."""
    lines = ["Premium report features", "======================="]
    for key, _lk, fallback, meaning in PREMIUM_FEATURES:
        lines.append("%-14s %s" % (key, fallback))
        lines.append("               %s" % meaning)
    return lines


def format_triggers():
    """The five automatic-generation triggers as ASCII lines."""
    lines = ["Automatic generation", "===================="]
    for key, _lk, fallback, generators, default in TRIGGERS:
        lines.append("%-14s %-4s %s -> %s"
                     % (key, "on" if default else "off", fallback,
                        ", ".join(generators)))
    return lines


def premium_capabilities():
    """Describe the premium surface for the audit / about layer."""
    return {
        "premium": list(PREMIUM_KEYS),
        "premium_labels": dict(PREMIUM_LABELS),
        "triggers": list(TRIGGER_KEYS),
        "trigger_labels": dict(TRIGGER_LABELS),
        "trigger_generators": {key: list(value) for key, value
                               in TRIGGER_GENERATORS.items()},
        "trigger_default": dict(TRIGGER_DEFAULT),
        "cadences": list(CADENCE_KEYS),
        "cadence_labels": dict(CADENCE_LABELS),
        "cadence_seconds": dict(CADENCE_SECONDS),
        "schedule_jobs": list(SCHEDULE_JOBS),
        "schedule_job_labels": dict(SCHEDULE_JOB_LABELS),
        "offline": True,
        "ascii_only": True,
        "zero_dependency": True,
        "cross_platform": True,
    }
