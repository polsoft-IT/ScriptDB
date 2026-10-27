# -*- coding: utf-8 -*-
"""Dangerous-behaviour detection and audit reporting for the sandbox.

This module owns the third question a sandbox has to answer. The walls
(`tabs.sandbox_fs`, `sandbox_net`, `sandbox_syscall`) decide what a run may
*reach*; the monitor (`tabs.sandbox_monitor`) decides what it may *consume* and
records what it did. This module decides what it was *doing* — the shapes of
misbehaviour that can be recognised from outside a run even when no rule was
broken, and turns everything the run produced into reports.

Two halves:

    BEHAVIOURS      what a run is checked for: an infinite loop, a recursion
                    overflow, a memory leak, a deadlock, a race condition, and
                    the four attempts to leave the sandbox (filesystem, network,
                    syscall and a deliberate bypass)
    REPORTS         the ten documents an audit needs, each renderable as ASCII
                    or as JSON, all built from one run record

The split is deliberate. `detect` reads a run and returns findings; `render`
turns a report into bytes. Neither knows about Qt, and neither touches the
network, so the same code answers the Audit tab, the Logs tab and a test.

A note on what "detection" can mean here. The sandbox is standard-library only
and runs on any platform, so nothing in this file reads a debugger, a kernel
trace or the process table. Detection is therefore *evidence-based*: a run
leaves traces — a hit CPU budget with no output, a `RecursionError`, a peak
memory that never comes down, a lock that was held when the budget ended, a
refused syscall — and each pattern below is a statement about those traces. It
says what the run did, never why; that last step is the reader's.
"""

import datetime as dt
import json

# ---------------------------------------------------------------------------
# What a run is checked for
# ---------------------------------------------------------------------------
# (key, label key, fallback, severity, what it means)
#
# `severity` orders the findings in a report and decides which ones a reader is
# shown first: `critical` is a run that left the allowed domain or wedged the
# process, `high` is a run that consumed its way into a limit, `medium` is one
# whose shape suggests a bug that has not bitten yet.
BEHAVIOURS = (
    ("infinite_loop", "sandbox_beh_infinite_loop", "Infinite loop", "high",
     "the run kept working until its time or CPU budget stopped it, and "
     "produced nothing that would end on its own"),
    ("recursion_overflow", "sandbox_beh_recursion", "Recursion overflow",
     "high",
     "the call stack was exhausted: recursion with no base case, or a cycle "
     "between two functions"),
    ("memory_leak", "sandbox_beh_memory_leak", "Memory leak", "medium",
     "memory only ever grew: the run held every allocation instead of "
     "releasing it"),
    ("deadlock", "sandbox_beh_deadlock", "Deadlock", "critical",
     "the run stopped making progress while a lock was still held"),
    ("race_condition", "sandbox_beh_race", "Race condition", "high",
     "several threads touched the same state at once, so the result depends "
     "on their timing"),
    ("fs_unauthorised", "sandbox_beh_fs", "Unauthorised filesystem access",
     "critical",
     "a filesystem operation was attempted outside the paths the policy "
     "allows"),
    ("net_unauthorised", "sandbox_beh_net", "Unauthorised network access",
     "critical",
     "a network operation was attempted against a host, port or protocol "
     "the policy refuses"),
    ("syscall_unauthorised", "sandbox_beh_syscall",
     "Unauthorised system call", "critical",
     "a system call the syscall filter refuses was attempted"),
    ("sandbox_bypass", "sandbox_beh_bypass", "Sandbox bypass attempt",
     "critical",
     "the run reached for the guards themselves, or for the terminal's own "
     "state, rather than for the operation they protect"),
    ("process_explosion", "sandbox_beh_process", "Process or thread explosion",
     "high",
     "the run spawned processes or threads until a budget stopped it, which "
     "is how a fork bomb begins"),
)

BEHAVIOUR_KEYS = tuple(key for key, _lk, _f, _sev, _d in BEHAVIOURS)
BEHAVIOUR_LABELS = {key: fallback for key, _lk, fallback, _sev, _d in BEHAVIOURS}
BEHAVIOUR_SEVERITY = {key: sev for key, _lk, _f, sev, _d in BEHAVIOURS}
BEHAVIOUR_MEANING = {key: meaning for key, _lk, _f, _sev, meaning in BEHAVIOURS}

# The order findings are sorted in: the worst first, then by the table order so
# two findings of the same severity are always shown the same way round.
_SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}
_BEHAVIOUR_RANK = {key: index for index, key in enumerate(BEHAVIOUR_KEYS)}

# The evidence a finding can be based on, so a report can show why a run was
# flagged rather than only that it was. One finding carries a list of these.
EVIDENCE_KINDS = ("violation", "event", "limit", "monitor", "pattern", "stack")

# ---------------------------------------------------------------------------
# The ten reports an audit needs
# ---------------------------------------------------------------------------
# (key, label key, fallback, one-line description)
#
# `security` and `runtime` are roll-ups; the rest each own one dimension of the
# run. Keeping them separate (rather than one big document) is what makes `logs`
# useful: each report can be filed, diffed and read on its own.
REPORTS = (
    ("security", "sandbox_rep_security", "Security report",
     "the dangerous behaviours detected, worst first, with their evidence"),
    ("calls", "sandbox_rep_calls", "Call report",
     "every call the run made through a guarded entry point"),
    ("errors", "sandbox_rep_errors", "Error report",
     "the errors and uncaught exceptions of the run"),
    ("warnings", "sandbox_rep_warnings", "Warning report",
     "the warnings the run produced or that it triggered"),
    ("runtime", "sandbox_rep_runtime", "Runtime report",
     "how long the run took, how it ended and what it measured"),
    ("resources", "sandbox_rep_resources", "Resource report",
     "each budget in the policy, what the run used and what it reached"),
    ("filesystem", "sandbox_rep_fs", "Filesystem report",
     "the filesystem rules the run obeyed and the operations they refused"),
    ("network", "sandbox_rep_net", "Network report",
     "the network rules the run obeyed and the operations they refused"),
    ("syscalls", "sandbox_rep_syscalls", "Syscall report",
     "how often each guarded syscall was reached and which ones were refused"),
    ("behaviour", "sandbox_rep_behaviour", "Behaviour report",
     "the traces of misbehaviour, one line each, in the order they happened"),
)

REPORT_KEYS = tuple(key for key, _lk, _f, _d in REPORTS)
REPORT_LABELS = {key: fallback for key, _lk, fallback, _d in REPORTS}
REPORT_DESCRIPTIONS = {key: desc for key, _lk, _f, desc in REPORTS}

# The two formats every report can be written in.
FORMATS = ("ascii", "json")

# The line width the ASCII renderer wraps free text at. A report that is meant
# to live in a file or a terminal is read at a glance, and a 400-character line
# defeats that.
ASCII_WIDTH = 78


# ---------------------------------------------------------------------------
# Reading one run
# ---------------------------------------------------------------------------
def _get(run, name, default=None):
    """One attribute of a run, whether it is a SandboxRun or a plain dict."""
    if run is None:
        return default
    if isinstance(run, dict):
        return run.get(name, default)
    return getattr(run, name, default)


def _violations(run):
    entries = _get(run, "violations") or []
    return [entry for entry in entries if isinstance(entry, dict)]


def _events(run):
    entries = _get(run, "events") or []
    return [entry for entry in entries if isinstance(entry, dict)]


def _monitor(run):
    monitor = _get(run, "monitor") or {}
    return monitor if isinstance(monitor, dict) else {}


def _limits(run):
    policy = _get(run, "policy")
    if policy is not None and hasattr(policy, "limits"):
        try:
            return policy.limits()
        except Exception:
            return {}
    limits = _get(run, "limits") or {}
    return limits if isinstance(limits, dict) else {}


def _number(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _kinds(run, prefix):
    """The entries of one violation family, e.g. every `fs:` refusal."""
    return [entry for entry in _violations(run)
            if str(entry.get("kind", "")).startswith(prefix)]


def _counts_kinds(run):
    """``{kind: count}`` for the run's violations, whatever the family."""
    counts = {}
    for entry in _violations(run):
        kind = str(entry.get("kind", ""))
        counts[kind] = counts.get(kind, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------
def _finding(key, detail, evidence=()):
    """One detected behaviour, with the evidence that raised it."""
    return {
        "key": key,
        "label": BEHAVIOUR_LABELS.get(key, key),
        "severity": BEHAVIOUR_SEVERITY.get(key, "medium"),
        "detail": detail,
        "meaning": BEHAVIOUR_MEANING.get(key, ""),
        "evidence": [dict(item) for item in evidence],
    }


def _detect_infinite_loop(run, monitor, limits, hits):
    """A run that consumed its time or CPU budget and could not end alone."""
    findings = []
    timed_out = bool(_get(run, "timed_out"))
    cpu_hit = "cpu_seconds" in hits
    if not (timed_out or cpu_hit):
        return findings
    # The distinction that makes this behavioural rather than merely a limit:
    # a run that finished its work and then idled is not in a loop, while one
    # that was still burning CPU when the budget ended is. A run that printed
    # something on the way is reported too, but as weaker evidence.
    elapsed = _number(monitor.get("elapsed"))
    cpu = _number(monitor.get("cpu"))
    output = str(_get(run, "output") or "")
    evidence = [
        {"kind": "limit", "detail": "timeout=%s cpu_seconds=%s"
         % (timed_out, cpu_hit)},
        {"kind": "monitor", "detail": "elapsed=%.3fs cpu=%.3fs"
         % (elapsed, cpu)},
    ]
    if output.strip():
        evidence.append({"kind": "monitor",
                         "detail": "%d char(s) of output before it stopped"
                                   % len(output)})
    # A busy loop with no output at all is the clearest case, and the one the
    # timeout watchdog was built for.
    if not output.strip():
        detail = ("the run reached its time budget after %.2fs of CPU with no "
                  "output at all" % cpu)
    else:
        detail = ("the run reached its time budget after %.2fs of CPU; it was "
                  "still working when it was stopped" % cpu)
    findings.append(_finding("infinite_loop", detail, evidence))
    return findings


def _detect_recursion(run, monitor, limits, hits):
    """A run whose stack was exhausted."""
    findings = []
    evidence = []
    for entry in _events(run):
        if str(entry.get("kind", "")) != "exception":
            continue
        detail = str(entry.get("detail", ""))
        if "RecursionError" in detail or "maximum recursion depth" in detail:
            evidence.append({"kind": "event", "detail": detail})
    for entry in _violations(run):
        if "RecursionError" in str(entry.get("detail", "")):
            evidence.append({"kind": "violation",
                             "detail": str(entry.get("detail", ""))[:120]})
    if evidence:
        findings.append(_finding(
            "recursion_overflow",
            "the call stack was exhausted after %d call(s)"
            % int(_number(monitor.get("calls"))), evidence))
    return findings


def _detect_memory_leak(run, monitor, limits, hits):
    """Memory that only ever grew: the run held every allocation."""
    findings = []
    peak = _number(monitor.get("ram"))
    if peak <= 0:
        return findings
    evidence = [{"kind": "monitor", "detail": "peak RAM %d byte(s)" % int(peak)}]
    # Two things together say "leak" rather than "big allocation": the peak is
    # substantial (a leak worth reporting leaks something), and the run never
    # told us it was releasing anything — either it grew into its budget, or it
    # allocated far more than the number of calls it made would explain.
    budget = _number((limits or {}).get("memory_mb")) * 1024 * 1024
    hit_budget = "memory_mb" in hits or "memory_bytes" in hits
    calls = _number(monitor.get("calls"))
    # A megabyte per guarded call is a very loose ceiling: ordinary code that
    # touches a few large buffers will not reach it, while a run that appends
    # to a list in a loop and never clears it will pass it easily.
    per_call = peak / calls if calls else peak
    substantial = peak >= 8 * 1024 * 1024
    if hit_budget:
        evidence.append({"kind": "limit",
                         "detail": "the RAM budget of %d MB was reached"
                                   % int(_number((limits or {}).get("memory_mb")))})
        detail = ("memory grew into its budget (%d byte(s) held at the peak)"
                  % int(peak))
    elif substantial and per_call >= 1024 * 1024:
        evidence.append({"kind": "monitor", "detail": "%d byte(s) held per call"
                                                     % int(per_call)})
        detail = ("memory grew to %d byte(s) across %d call(s) and was never "
                  "released" % (int(peak), int(calls)))
    else:
        return findings
    findings.append(_finding("memory_leak", detail, evidence))
    return findings


def _detect_deadlock(run, monitor, limits, hits):
    """A run that stopped making progress while holding a lock."""
    findings = []
    evidence = []
    # How a deadlock shows from outside: the run was killed for its time (it
    # was not going to finish), and it had asked for a lock at some point, and
    # it produced nothing while it waited. A run that never touched a lock
    # cannot deadlock on one, whatever else went wrong.
    timed_out = bool(_get(run, "timed_out"))
    output = str(_get(run, "output") or "")
    for entry in _violations(run):
        detail = str(entry.get("detail", ""))
        if "acquire" in detail or "lock" in detail.lower():
            evidence.append({"kind": "violation", "detail": detail[:120]})
    if not timed_out:
        return findings
    interesting = [entry for entry in _violations(run)
                   if "lock" in str(entry.get("detail", "")).lower()]
    if interesting:
        evidence.extend({"kind": "violation", "detail": str(entry.get("detail",
                                                                     ""))[:120]}
                        for entry in interesting[:4])
    if not evidence:
        return findings
    evidence.append({"kind": "monitor",
                     "detail": "the run was still waiting after %.2fs"
                               % _number(monitor.get("elapsed"))})
    if output.strip():
        evidence.append({"kind": "monitor",
                         "detail": "output stopped at %d char(s)"
                                   % len(output)})
    findings.append(_finding(
        "deadlock",
        "the run was stopped by its time budget while holding a lock",
        evidence))
    return findings


def _detect_race(run, monitor, limits, hits):
    """Several threads shared state, so the run's result is timing-dependent."""
    findings = []
    held = _number(monitor.get("threads"))
    evidence = []
    if "max_threads" in hits:
        evidence.append({"kind": "limit", "detail": "the thread budget was reached"})
    for entry in _violations(run):
        detail = str(entry.get("detail", ""))
        if "thread" in detail.lower():
            evidence.append({"kind": "violation", "detail": detail[:120]})
    if not evidence:
        return findings
    findings.append(_finding(
        "race_condition",
        "the run started threads that share its state; without a lock around "
        "each shared write the result depends on their timing", evidence))
    return findings


def _detect_process_explosion(run, monitor, limits, hits):
    """A run that spawned processes or threads until a budget stopped it."""
    if not ("max_processes" in hits or "max_threads" in hits):
        return []
    spawned = [entry for entry in _violations(run)
               if str(entry.get("kind", "")).startswith("sys:")]
    evidence = [{"kind": "limit", "detail": "processes=%s threads=%s"
                 % ("max_processes" in hits, "max_threads" in hits)}]
    evidence.extend({"kind": "violation", "detail": str(entry.get("detail",
                                                                  ""))[:120]}
                    for entry in spawned[:4])
    return [_finding(
        "process_explosion",
        "the run was stopped by its process or thread budget", evidence)]


def _detect_unauthorised(run):
    """The three walls, each reported as its own behaviour."""
    findings = []
    families = (
        ("fs_unauthorised", "fs:", "filesystem"),
        ("net_unauthorised", "net:", "network"),
        ("syscall_unauthorised", "sys:", "system call"),
    )
    for key, prefix, noun in families:
        entries = _kinds(run, prefix)
        if not entries:
            continue
        evidence = [{"kind": "violation",
                     "detail": "%s %s" % (entry.get("kind", ""),
                                          entry.get("detail", ""))[:120]}
                    for entry in entries[:6]]
        # Naming the distinct operations is what makes the finding actionable:
        # "11 attempts" says how bad, "open, write" says what to fix.
        operations = sorted({str(entry.get("kind", ""))[len(prefix):]
                             for entry in entries if entry.get("kind")})
        detail = ("%d refused %s operation(s): %s"
                  % (len(entries), noun, ", ".join(operations) or "-"))
        findings.append(_finding(key, detail, evidence))
    return findings


def _detect_bypass(run):
    """A run that reached for the guards rather than around them.

    Deliberately narrow. An ordinary wall hit ("writes are blocked by the
    policy") is not a bypass attempt even though the refusal came from the
    sandbox: a script that opens a file and is told no has learned nothing
    about the mechanism. A bypass attempt is a run that *named* the mechanism,
    and those cases carry their own marker rather than being inferred from the
    wording of an unrelated refusal.
    """
    findings = []
    evidence = []
    # The imports that are refused precisely because they are escape routes.
    guard_imports = ("ctypes", "cffi", "mmap", "fcntl", "resource", "signal",
                     "sdb", "sdb_scripts", "tabs", "pyqt5", "builtins")
    for entry in _violations(run):
        kind = str(entry.get("kind", ""))
        detail = str(entry.get("detail", ""))
        lowered = detail.lower()
        if kind.startswith("behaviour:bypass") or "bypass" in kind.lower() \
                or "bypass" in lowered:
            evidence.append({"kind": "violation", "detail": detail[:120] or kind})
            continue
        # An import refused for reaching across the process boundary is an
        # escape attempt by construction: nothing else refuses those names.
        if "blocked import" in lowered \
                and any(name in lowered for name in guard_imports):
            evidence.append({"kind": "violation", "detail": detail[:120]})
            continue
        # A run that reached for the sandbox's own bookkeeping is going for the
        # mechanism rather than for the operation it protects.
        if any(marker in lowered for marker in
               ("sdb_sandbox", "violations.log", "_sandbox_runner",
                "_monitor.json")):
            evidence.append({"kind": "violation", "detail": detail[:120]})
    for entry in _events(run):
        lowered = str(entry.get("detail", "")).lower()
        if "bypass" in lowered:
            evidence.append({"kind": "event",
                             "detail": str(entry.get("detail", ""))[:120]})
    if not evidence:
        return findings
    findings.append(_finding(
        "sandbox_bypass",
        "%d operation(s) reached for the sandbox's own guards or the "
        "terminal's state" % len(evidence), evidence[:8]))
    return findings


# The detectors that read the run's live measurements, in the order their
# findings are collected. The order matters only for the raw list; `detect`
# sorts the result by severity. The wall detectors are not here because they
# read the violations directly and need no monitor or limits argument.
_DETECTORS = (
    _detect_infinite_loop,
    _detect_recursion,
    _detect_memory_leak,
    _detect_deadlock,
    _detect_race,
    _detect_process_explosion,
)


def detect(run):
    """The dangerous behaviours a run shows, worst and most specific first.

    `run` is a `SandboxRun` or the dict it serialises to. Returns a list of
    findings, each ``{key, label, severity, detail, meaning, evidence}``.
    Sorted by severity, then by the table order in :data:`BEHAVIOURS`.
    """
    monitor = _monitor(run)
    limits = _limits(run)
    hits = set(_get(run, "limits_hit") or [])
    findings = []
    for detector in _DETECTORS:
        try:
            findings.extend(detector(run, monitor, limits, hits))
        except Exception:
            # A detector that cannot read an old report must not stop the
            # others: a missing measurement is an absent finding, not an error.
            continue
    findings.extend(_detect_unauthorised(run))
    findings.extend(_detect_bypass(run))
    findings.sort(key=lambda item: (_SEVERITY_RANK.get(item["severity"], 9),
                                    _BEHAVIOUR_RANK.get(item["key"], 99)))
    return findings


def behaviour_counts(findings):
    """``{behaviour: count}`` for a list of findings."""
    counts = {}
    for item in findings or ():
        key = (item or {}).get("key")
        if key:
            counts[key] = counts.get(key, 0) + 1
    return counts


def severity_counts(findings):
    """``{severity: count}`` for a list of findings."""
    counts = {}
    for item in findings or ():
        severity = (item or {}).get("severity", "medium")
        counts[severity] = counts.get(severity, 0) + 1
    return counts


def worst_severity(findings):
    """The worst severity among the findings (or "" when there are none)."""
    ranking = sorted((_SEVERITY_RANK.get((item or {}).get("severity"), 9),
                      (item or {}).get("severity", "medium"))
                     for item in findings or ())
    return ranking[0][1] if ranking else ""


def risk_level(findings):
    """The run's risk as one word, for a badge or a one-line summary.

    Deliberately blunt: a reader scanning a list of runs needs to see which ones
    to open, and a three-value scale does that better than the five severities.
    """
    worst = worst_severity(findings)
    if worst == "critical":
        return "dangerous"
    if worst in ("high", "medium"):
        return "suspicious"
    return "clean"


# ---------------------------------------------------------------------------
# The reports
# ---------------------------------------------------------------------------
def _header(title, run=None):
    """The banner every ASCII report starts with."""
    lines = [title, "=" * len(title)]
    if run is not None:
        lines.append("Script  : %s" % (_get(run, "script", "") or "-"))
        lines.append("Started : %s" % (_get(run, "started", "") or "-"))
        lines.append("Finished: %s" % (_get(run, "finished", "") or "-"))
        lines.append("Status  : %s" % (_get(run, "status", "") or "-"))
    return lines


def _section(lines, title, body):
    """Append one ``title`` + ``body`` section to an ASCII report."""
    lines.append("")
    lines.append(title)
    lines.append("-" * len(title))
    lines.extend(body or ["(none)"])
    return lines


def _wrap(text, width=ASCII_WIDTH, indent="  "):
    """Free text as a list of wrapped lines (no textwrap: explicit and stable)."""
    words = str(text or "").split()
    if not words:
        return [indent.rstrip()]
    lines, current = [], indent
    for word in words:
        if len(current) + len(word) + 1 > width and current.strip():
            lines.append(current.rstrip())
            current = indent + word + " "
        else:
            current += word + " "
    lines.append(current.rstrip())
    return lines


# --- each report's own content ---------------------------------------------
def _security_body(run, findings):
    body = []
    if not findings:
        return ["no dangerous behaviour detected"]
    for item in findings:
        body.append("%-9s %-22s %s" % (item["severity"], item["key"],
                                       item["detail"]))
        if item.get("meaning"):
            body.extend(_wrap(item["meaning"], indent="            "))
        for evidence in item.get("evidence", [])[:4]:
            body.append("            %-9s %s" % (evidence.get("kind", "?"),
                                                 evidence.get("detail", "")))
    return body


def _calls_body(run, monitor):
    syscalls = _get(run, "syscalls") or {}
    counts = _counts_kinds(run)
    by_syscall = (syscalls.get("by_syscall") or {}) if isinstance(syscalls, dict) else {}
    body = ["calls      : %d" % int(_number(monitor.get("calls"))),
            "modules    : %d" % int(_number(monitor.get("modules"))),
            "syscalls   : %d" % int(_number(monitor.get("syscalls"))),
            "refused    : %d" % int(_number((syscalls or {}).get("refused")))]
    reached = sorted(((name, count) for name, count in counts.items()
                      if name.startswith("sys:")),
                     key=lambda item: -item[1])
    if reached:
        body.append("")
        body.append("guarded calls reached:")
        for name, count in reached:
            body.append("  %-20s %d" % (name, count))
    if by_syscall:
        body.append("")
        body.append("refused by syscall:")
        for name, count in sorted(by_syscall.items(), key=lambda item: -item[1]):
            body.append("  %-20s %d" % (name, count))
    return body


def _errors_body(run, findings):
    body = []
    for entry in _events(run):
        kind = str(entry.get("kind", ""))
        if kind in ("error", "exception"):
            body.append("%-10s %s" % (kind, entry.get("detail", "")))
    for item in findings:
        if item["key"] == "recursion_overflow":
            body.append("%-10s %s" % ("recursion", item["detail"]))
    if _get(run, "error"):
        body.append("%-10s %s" % ("run", _get(run, "error")))
    return body


def _warnings_body(run, findings):
    body = ["%-10s %s" % (str(entry.get("kind", "")), entry.get("detail", ""))
            for entry in _events(run)
            if str(entry.get("kind", "")) == "warning"]
    # The behaviours that are warnings rather than errors: a leak or a race has
    # not necessarily broken this run, but it is the run's own smoke.
    for item in findings:
        if item["key"] in ("memory_leak", "race_condition", "process_explosion"):
            body.append("%-10s %s" % ("behaviour", item["detail"]))
    return body


def _runtime_body(run, monitor):
    return [
        "started    : %s" % (_get(run, "started", "") or "-"),
        "finished   : %s" % (_get(run, "finished", "") or "-"),
        "status     : %s" % (_get(run, "status", "") or "-"),
        "exit code  : %s" % (_get(run, "exit_code", None)
                             if _get(run, "exit_code") is not None else "-"),
        "elapsed    : %.3fs" % _number(monitor.get("elapsed")),
        "cpu        : %.3fs" % _number(monitor.get("cpu")),
        "timed out  : %s" % bool(_get(run, "timed_out")),
        "truncated  : %d char(s)" % int(_number(_get(run, "truncated"))),
        "workspace  : %s" % (_get(run, "workspace", "") or "-"),
    ]


def _resources_body(run, monitor):
    from tabs import sandbox_monitor as _mon
    limits = _limits(run)
    states = _mon.limit_states(limits, monitor)
    body = _mon.format_limits(limits, monitor)
    if not states:
        return body
    body.append("")
    body.append("usage against each budget:")
    for key, budget, measured, hit in states:
        body.append("  %-14s %12s / %-12s %s"
                    % (key, _mon.value_text(key, measured)[0],
                       _mon.value_text(key, budget)[0],
                       "[REACHED]" if hit else "ok"))
    return body


def _fs_body(run, policy):
    from tabs import sandbox_fs as _fs
    body = _fs.format_fs_rules(_fs.describe(_get(run, "workspace", ""), policy))
    refused = _kinds(run, "fs:")
    if refused:
        body.append("")
        body.append("refused operations:")
        body.extend("  %-12s %s" % (entry.get("kind", ""), entry.get("detail", ""))
                    for entry in refused[:40])
    return body


def _net_body(run, policy):
    from tabs import sandbox_net as _net
    body = _net.format_net_rules(_net.describe(policy))
    refused = _kinds(run, "net:")
    if refused:
        body.append("")
        body.append("refused operations:")
        body.extend("  %-12s %s" % (entry.get("kind", ""), entry.get("detail", ""))
                    for entry in refused[:40])
    return body


def _syscalls_body(run, monitor):
    syscalls = _get(run, "syscalls") or {}
    body = ["decisions  : %d" % int(_number((syscalls or {}).get("decisions"))),
            "refused    : %d" % int(_number((syscalls or {}).get("refused"))),
            "syscalls   : %d (monitored)" % int(_number(monitor.get("syscalls")))]
    by_syscall = (syscalls.get("by_syscall") or {}) if isinstance(syscalls, dict) else {}
    if by_syscall:
        body.append("")
        body.append("refused by syscall:")
        for name, count in sorted(by_syscall.items(), key=lambda item: -item[1]):
            body.append("  %-14s %d refusal(s)" % (name, count))
    refusals = _kinds(run, "sys:")
    if refusals:
        body.append("")
        body.append("what was attempted:")
        body.extend("  %-12s %s" % (entry.get("kind", ""), entry.get("detail", ""))
                    for entry in refusals[:40])
    return body


def _behaviour_body(run, findings):
    if not findings:
        return ["no behaviour of note: the run neither looped, leaked, wedged "
                "itself nor reached past its walls"]
    body = []
    for item in findings:
        body.append("%-9s %s" % (item["severity"], item["label"]))
        body.extend(_wrap(item["detail"], indent="  "))
        body.append("")
    return body[:-1] if body and body[-1] == "" else body


def _security_document(run, findings):
    monitor = _monitor(run)
    return {
        "risk": risk_level(findings),
        "worst_severity": worst_severity(findings) or None,
        "finding_count": len(findings),
        "behaviour_counts": behaviour_counts(findings),
        "severity_counts": severity_counts(findings),
        "findings": findings,
        "script": _get(run, "script", ""),
        "status": _get(run, "status", ""),
        "elapsed": _number(monitor.get("elapsed")),
        "checked": list(BEHAVIOUR_KEYS),
    }


def _calls_document(run, monitor):
    counts = _counts_kinds(run)
    return {
        "calls": int(_number(monitor.get("calls"))),
        "modules": int(_number(monitor.get("modules"))),
        "syscalls": int(_number(monitor.get("syscalls"))),
        "refused": int(_number((_get(run, "syscalls") or {}).get("refused"))),
        "refused_by_kind": dict(sorted(counts.items())),
        "calls_by_syscall": dict(_get(run, "syscalls") or {}),
    }


def _errors_document(run, findings):
    entries = [entry for entry in _events(run)
               if str(entry.get("kind", "")) in ("error", "exception")]
    return {
        "error_count": len(entries),
        "exception_count": sum(1 for entry in entries
                               if str(entry.get("kind")) == "exception"),
        "entries": entries,
        "run_error": _get(run, "error", ""),
        "behaviours": [item["key"] for item in findings
                       if item["key"] == "recursion_overflow"],
    }


def _warnings_document(run, findings):
    entries = [entry for entry in _events(run)
               if str(entry.get("kind", "")) == "warning"]
    return {
        "warning_count": len(entries),
        "entries": entries,
        "behaviour_warnings": [
            {"key": item["key"], "detail": item["detail"]}
            for item in findings
            if item["key"] in ("memory_leak", "race_condition",
                               "process_explosion")],
    }


def _runtime_document(run, monitor):
    return {
        "started": _get(run, "started", ""),
        "finished": _get(run, "finished", ""),
        "status": _get(run, "status", ""),
        "exit_code": _get(run, "exit_code"),
        "elapsed": _number(monitor.get("elapsed")),
        "cpu": _number(monitor.get("cpu")),
        "timed_out": bool(_get(run, "timed_out")),
        "ok": bool(_get(run, "ok")),
        "truncated": int(_number(_get(run, "truncated"))),
        "workspace": _get(run, "workspace", ""),
        "language": _get(run, "language", ""),
        "monitor": monitor,
    }


def _resources_document(run, monitor):
    from tabs import sandbox_monitor as _mon
    limits = _limits(run)
    return {
        "limits": _mon.normalise_limits(limits),
        "usage": _mon.normalise_monitor(monitor),
        "limits_hit": list(_get(run, "limits_hit") or []),
        "states": [
            {"key": key, "budget": budget, "measured": measured, "hit": hit}
            for key, budget, measured, hit in _mon.limit_states(limits, monitor)
        ],
    }


def _fs_document(run, policy):
    from tabs import sandbox_fs as _fs
    try:
        described = _fs.describe(_get(run, "workspace", ""), policy)
    except Exception:
        described = {}
    return {
        "rules": described,
        "refused": [entry for entry in _kinds(run, "fs:")],
        "refused_count": len(_kinds(run, "fs:")),
    }


def _net_document(run, policy):
    from tabs import sandbox_net as _net
    try:
        described = _net.describe(policy)
    except Exception:
        described = {}
    return {
        "rules": described,
        "refused": [entry for entry in _kinds(run, "net:")],
        "refused_count": len(_kinds(run, "net:")),
    }


def _syscalls_document(run, monitor):
    return {
        "decisions": int(_number((_get(run, "syscalls") or {}).get("decisions"))),
        "refused": int(_number((_get(run, "syscalls") or {}).get("refused"))),
        "monitored": int(_number(monitor.get("syscalls"))),
        "by_syscall": dict((_get(run, "syscalls") or {}).get("by_syscall") or {}),
        "refusals": [entry for entry in _kinds(run, "sys:")],
    }


def _behaviour_document(run, findings):
    return {
        "checked": list(BEHAVIOUR_KEYS),
        "detected": [item["key"] for item in findings],
        "findings": findings,
        "risk": risk_level(findings),
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def _policy_of(run):
    policy = _get(run, "policy")
    if policy is not None and hasattr(policy, "limits"):
        return policy
    # A dict run (a loaded JSON report) has no policy object; the reports that
    # need one fall back to a default so they still render. The import is done
    # here rather than at module load because `tabs.sandbox` imports this
    # module, and a cycle at import time would break both.
    from tabs import sandbox as _sandbox
    return _sandbox.SandboxPolicy()


def report_body(key, run, findings=None, policy=None):
    """The body lines of one report (the part below its header)."""
    findings = detect(run) if findings is None else findings
    monitor = _monitor(run)
    policy = policy or _policy_of(run)
    if key == "security":
        return _security_body(run, findings)
    if key == "calls":
        return _calls_body(run, monitor)
    if key == "errors":
        return _errors_body(run, findings)
    if key == "warnings":
        return _warnings_body(run, findings)
    if key == "runtime":
        return _runtime_body(run, monitor)
    if key == "resources":
        return _resources_body(run, monitor)
    if key == "filesystem":
        return _fs_body(run, policy)
    if key == "network":
        return _net_body(run, policy)
    if key == "syscalls":
        return _syscalls_body(run, monitor)
    if key == "behaviour":
        return _behaviour_body(run, findings)
    return ["(unknown report: %s)" % key]


def report_document(key, run, findings=None, policy=None):
    """One report as a JSON-friendly dict."""
    findings = detect(run) if findings is None else findings
    monitor = _monitor(run)
    policy = policy or _policy_of(run)
    if key == "security":
        return _security_document(run, findings)
    if key == "calls":
        return _calls_document(run, monitor)
    if key == "errors":
        return _errors_document(run, findings)
    if key == "warnings":
        return _warnings_document(run, findings)
    if key == "runtime":
        return _runtime_document(run, monitor)
    if key == "resources":
        return _resources_document(run, monitor)
    if key == "filesystem":
        return _fs_document(run, policy)
    if key == "network":
        return _net_document(run, policy)
    if key == "syscalls":
        return _syscalls_document(run, monitor)
    if key == "behaviour":
        return _behaviour_document(run, findings)
    return {"error": "unknown report: %s" % key}


def render(key, run, fmt="ascii", findings=None, policy=None):
    """One report as text in ``fmt`` (``ascii`` or ``json``).

    The ASCII form carries a header naming the report and the run; the JSON form
    is the document on its own, because a JSON consumer already knows which file
    it opened and an envelope would only be one more thing to unwrap.
    """
    key = key if key in REPORT_KEYS else key
    findings = detect(run) if findings is None else findings
    if fmt == "json":
        return json.dumps(report_document(key, run, findings, policy),
                          indent=2, sort_keys=True,
                          default=str) + "\n"
    title = REPORT_LABELS.get(key, str(key))
    lines = _header("ScriptDB sandbox %s" % title, run)
    lines.append("Report  : %s" % key)
    lines.extend(_section(lines, "Findings" if key == "security" else "Detail",
                          report_body(key, run, findings, policy)))
    return "\n".join(lines) + "\n"


def render_all(run, fmt="ascii", findings=None):
    """Every report joined into one document, in the order of :data:`REPORTS`."""
    findings = detect(run) if findings is None else findings
    if fmt == "json":
        payload = {
            "run": {
                "script": _get(run, "script", ""),
                "started": _get(run, "started", ""),
                "finished": _get(run, "finished", ""),
                "status": _get(run, "status", ""),
            },
            "generated": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "risk": risk_level(findings),
            "reports": {key: report_document(key, run, findings)
                        for key in REPORT_KEYS},
        }
        return json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n"
    blocks = []
    for key in REPORT_KEYS:
        blocks.append(render(key, run, "ascii", findings))
    banner = "ScriptDB sandbox audit"
    blocks.insert(0, "%s\n%s\nReports: %d\nRisk   : %s\n"
                  % (banner, "=" * len(banner), len(REPORT_KEYS),
                     risk_level(findings)))
    return "\n".join(blocks)


# ---------------------------------------------------------------------------
# Describing the whole thing
# ---------------------------------------------------------------------------
def format_findings(findings, limit=20):
    """The findings as short ASCII lines (the tab's findings pane)."""
    if not findings:
        return ["(no dangerous behaviour detected)"]
    lines = []
    for item in findings[:limit]:
        lines.append("%-9s %-20s %s" % (item.get("severity", "?"),
                                        item.get("key", "?"),
                                        item.get("detail", "")))
    if len(findings) > limit:
        lines.append("... and %d more" % (len(findings) - limit))
    return lines


def audit_capabilities():
    """Describe the detectors and the reports for the audit / about layer."""
    return {
        "behaviours": list(BEHAVIOUR_KEYS),
        "behaviour_labels": dict(BEHAVIOUR_LABELS),
        "severities": ["critical", "high", "medium", "low"],
        "reports": list(REPORT_KEYS),
        "report_labels": dict(REPORT_LABELS),
        "formats": list(FORMATS),
        "evidence_kinds": list(EVIDENCE_KINDS),
        "ascii_width": ASCII_WIDTH,
        "offline": True,
    }
