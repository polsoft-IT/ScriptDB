# -*- coding: utf-8 -*-
"""Runtime monitor and resource limiter for the sandbox.

This module owns the *second* half of what makes a run safe. The isolation
layer (`tabs.sandbox_isolation`) and the wall layers (`tabs.sandbox_fs`,
`sandbox_net`, `sandbox_syscall`) decide what a run may *reach*; this module
decides what it may *consume* and records what it *did*.

Two questions, one table each:

    MONITOR_ITEMS   what a run is watched for: elapsed time, CPU, RAM, IO,
                    function calls, module calls, syscalls, variable changes,
                    exceptions, errors and warnings
    LIMIT_ITEMS     what a run is bounded by: timeout, CPU, RAM, IO, number of
                    files, processes, threads and log size

The split follows the rest of the sandbox: the *table* of names lives here, so
the parent can show and count them, while the *enforcement* runs inside the
child (embedded in the runner source in `tabs.sandbox`), because the child is
the only place a wall-clock budget, a memory ceiling or a captured exception
can actually be seen. The names below are the contract between the two sides.

Nothing in this module is Qt-dependent or touches the network: it is a plain
description of the run's telemetry and its budgets.
"""

import os

# ---------------------------------------------------------------------------
# What the run is monitored for
# ---------------------------------------------------------------------------
# (key, label key, fallback, unit)
#
# `unit` is what the value means when it is shown: a duration in seconds, a
# byte count, a plain event count or a free-form text. Keeping it here rather
# than in the UI is what lets the report, the policy view and a test agree on
# what a number means without three copies of the same knowledge.
MONITOR_ITEMS = (
    ("elapsed", "sandbox_mon_elapsed", "Elapsed time", "seconds"),
    ("cpu", "sandbox_mon_cpu", "CPU usage", "seconds"),
    ("ram", "sandbox_mon_ram", "RAM usage", "bytes"),
    ("io", "sandbox_mon_io", "IO usage", "bytes"),
    ("calls", "sandbox_mon_calls", "Function calls", "calls"),
    ("modules", "sandbox_mon_modules", "Module calls", "modules"),
    ("syscalls", "sandbox_mon_syscalls", "System calls", "calls"),
    ("variables", "sandbox_mon_variables", "Variable changes", "changes"),
    ("exceptions", "sandbox_mon_exceptions", "Exceptions", "events"),
    ("errors", "sandbox_mon_errors", "Errors", "events"),
    ("warnings", "sandbox_mon_warnings", "Warnings", "events"),
)

MONITOR_KEYS = tuple(key for key, _lk, _f, _u in MONITOR_ITEMS)

# ---------------------------------------------------------------------------
# What the run is bounded by
# ---------------------------------------------------------------------------
# (key, label key, fallback, unit, hard ceiling)
#
# The last entry is the absolute ceiling the parent clamps a policy to, so a
# hand-edited or deserialised policy can never ask for an unbounded run whatever
# the UI is willing to show. `log_bytes` intentionally caps the *stderr* stream
# (errors, warnings, tracebacks) rather than stdout: stdout is already bounded
# by the run's output cap, and the two caps protect two different things.
LIMIT_ITEMS = (
    ("timeout", "sandbox_limit_timeout", "Time limit", "seconds", 3600),
    ("cpu_seconds", "sandbox_limit_cpu", "CPU limit", "seconds", 3600),
    ("memory_mb", "sandbox_limit_ram", "RAM limit", "MB", 8192),
    ("io_bytes", "sandbox_limit_io", "IO limit", "bytes", 8 * 1024 ** 3),
    ("max_files", "sandbox_limit_files", "File limit", "files", 100000),
    ("max_processes", "sandbox_limit_processes", "Process limit", "processes", 1024),
    ("max_threads", "sandbox_limit_threads", "Thread limit", "threads", 1024),
    ("log_bytes", "sandbox_limit_logs", "Log limit", "bytes", 64 * 1024 ** 2),
)

LIMIT_KEYS = tuple(key for key, _lk, _f, _u, _cap in LIMIT_ITEMS)

# The ceiling for one limit key (or 0 when the key is unknown).
LIMIT_CEILINGS = {key: cap for key, _lk, _f, _u, cap in LIMIT_ITEMS}

# Keys that are switched *on* by an integer budget rather than by a flag. Zero
# means "no ceiling of this kind", which is why the ceilings above are all
# positive: a limit is never disabled by its own value, only by being zero.
DISABLED = 0

# The kinds a runtime event can have. `error` and `warning` are what the child
# reports for its own diagnostics; `exception` is an uncaught exception that
# ended the script; `resource` is a limit being hit.
EVENT_KINDS = ("warning", "error", "exception", "resource")

# Limit names that the *child* enforces, so the report can tell a limit the
# child hit from one the parent did (the timeout kills the child from outside,
# the rest are refused inside it). The distinction matters when reading a
# report: a timeout means the run was still working, the others mean it asked
# for something the policy would not give it.
CHILD_ENFORCED = ("cpu_seconds", "memory_mb", "io_bytes", "max_files",
                  "max_processes", "max_threads", "log_bytes")
PARENT_ENFORCED = ("timeout",)

# Spawn counts are read as plain integers: the child's own PID plus whatever the
# process table shows as its children, and the interpreter's active threads.
# `psutil` would be more precise and is not an option: the sandbox is
# standard-library only, on purpose, and a dependency would be one more thing
# the child could see that the host has and it does not.


# ---------------------------------------------------------------------------
# Reading a monitor record
# ---------------------------------------------------------------------------
def _bytes_to_text(value):
    """A byte count as a human-readable string (best effort)."""
    try:
        size = float(value or 0)
    except (TypeError, ValueError):
        return "-"
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return ("%.1f %s" % (size, unit)) if unit != "B" \
                else ("%d B" % int(size))
        size /= 1024
    return "-"


def _seconds_to_text(value):
    """A duration in seconds as a short string."""
    try:
        return "%.3fs" % float(value or 0)
    except (TypeError, ValueError):
        return "-"


def value_text(key, value):
    """One monitor value as it should be shown: ``(text, number)``.

    `number` is what a test or a comparison should use, while `text` is what a
    reader sees. Both come from the same value, so they can never drift.
    """
    if key in ("elapsed", "cpu"):
        return _seconds_to_text(value), float(value or 0)
    if key in ("ram", "io"):
        return _bytes_to_text(value), int(value or 0)
    if key in ("calls", "modules", "syscalls", "variables",
               "exceptions", "errors", "warnings"):
        try:
            count = int(value or 0)
        except (TypeError, ValueError):
            count = 0
        return str(count), count
    return str(value if value is not None else "-"), value


def normalise_monitor(monitor):
    """A monitor record with every known key present and numeric.

    The child reports only what it measured, and an older report may not carry
    a key at all, so the reader is filled in here rather than in every caller.
    An unknown key is passed through untouched: a new measurement should not
    need this function changed before it can be shown.
    """
    out = {}
    for key in MONITOR_KEYS:
        value = (monitor or {}).get(key)
        unit = unit_of(key)
        if unit in ("seconds", "events", "calls", "modules", "changes"):
            try:
                out[key] = float(value or 0)
            except (TypeError, ValueError):
                out[key] = 0.0
        else:
            try:
                out[key] = int(value or 0)
            except (TypeError, ValueError):
                out[key] = 0
    for key, value in (monitor or {}).items():
        if key not in out:
            out[key] = value
    return out


def normalise_limits(limits):
    """A limits record with every known key present (missing means 0/off)."""
    out = {}
    for key in LIMIT_KEYS:
        try:
            value = int((limits or {}).get(key, 0) or 0)
        except (TypeError, ValueError):
            value = 0
        ceiling = LIMIT_CEILINGS.get(key, 0)
        if ceiling:
            value = max(0, min(ceiling, value))
        out[key] = value
    for key, value in (limits or {}).items():
        if key not in out:
            out[key] = value
    return out


def unit_of(key):
    """The unit of one monitor key (or "" for an unknown key)."""
    for mon_key, _lk, _f, unit in MONITOR_ITEMS:
        if mon_key == key:
            return unit
    return ""


def label_of(key, use_limit=False):
    """The fallback label of a monitor or limit key (used by tests/reports)."""
    for entry in (LIMIT_ITEMS if use_limit else MONITOR_ITEMS):
        if entry[0] == key:
            return entry[2]
    return key


def monitor_value(key, monitor):
    """One numeric monitor value (0 when it was not measured)."""
    return normalise_monitor(monitor).get(key, 0)


def measured_for(key, monitor):
    """The measurement a limit's budget is compared against.

    A limit and its measurement do not always share a name: the CPU budget is
    written in seconds as `cpu_seconds` while the monitor counts `cpu`, and the
    RAM budget is written in MB as `memory_mb` while the run measures `ram` in
    bytes. The conversion belongs here, in one place, so the reader of a report
    and the child that enforced the ceiling cannot compare different things.
    """
    monitor = normalise_monitor(monitor)
    if key == "timeout":
        return monitor.get("elapsed", 0)
    if key == "cpu_seconds":
        return monitor.get("cpu", 0)
    if key == "memory_mb":
        try:
            return float(monitor.get("ram", 0)) / (1024 * 1024)
        except (TypeError, ValueError):
            return 0
    if key in monitor:
        return monitor.get(key, 0)
    return 0


def limits_hit(limits, monitor):
    """The limit keys whose budget the run reached (in table order).

    A limit counts as hit when the measured value is at or above the budget and
    the budget is switched on, which is the same comparison the child makes. The
    timeout is left to the parent, which knows whether it had to kill the run,
    because a run's elapsed time can sit just under the budget that killed
    it (the watchdog fires on a wall clock, the report reads a monotonic one).
    """
    monitor = normalise_monitor(monitor)
    limits = normalise_limits(limits)
    hit = []
    for key in LIMIT_KEYS:
        budget = limits.get(key, 0)
        if not budget or key == "timeout":
            continue
        measured = measured_for(key, monitor)
        try:
            reached = float(measured) >= float(budget)
        except (TypeError, ValueError):
            reached = False
        if reached:
            hit.append(key)
    return hit


def limit_states(limits, monitor=None):
    """``[(key, budget, measured, hit)]`` for every switched-on limit."""
    monitor = normalise_monitor(monitor or {})
    limits = normalise_limits(limits)
    states = []
    for key in LIMIT_KEYS:
        budget = limits.get(key, 0)
        if not budget:
            continue
        measured = measured_for(key, monitor)
        try:
            hit = float(measured) >= float(budget)
        except (TypeError, ValueError):
            hit = False
        states.append((key, budget, measured, hit))
    return states


def format_monitor(monitor):
    """The runtime monitor as ASCII lines (the run report)."""
    monitor = normalise_monitor(monitor)
    if not monitor:
        lines = ["Runtime monitor", "---------------",
                 "(no measurements: the run did not report any)"]
        return lines
    lines = ["Runtime monitor", "---------------"]
    for key, _label_key, fallback, _unit in MONITOR_ITEMS:
        text, _number = value_text(key, monitor.get(key))
        lines.append("%-11s: %s" % (key, text))
    events = monitor.get("events")
    if isinstance(events, (list, tuple)) and events:
        lines.append("events     : %d" % len(events))
    return lines


def _quantity_text(value, unit):
    # One budget or measurement as a short string in its own unit.
    if unit == "bytes":
        return _bytes_to_text(value)
    if unit == "seconds":
        try:
            return "%.2f" % float(value)
        except (TypeError, ValueError):
            return "-"
    try:
        return str(int(value))
    except (TypeError, ValueError):
        return str(value)


def format_limits(limits, monitor=None):
    """The resource limits as ASCII lines, marking the ones that were hit."""
    lines = ["Resource limits", "---------------"]
    states = limit_states(limits, monitor)
    if not states:
        return lines + ["(no limits switched on)"]
    for key, budget, measured, hit in states:
        unit = ""
        for entry in LIMIT_ITEMS:
            if entry[0] == key:
                unit = entry[3]
                break
        row = "%-11s: %s / %s" % (
            key,
            _quantity_text(measured, unit),
            _quantity_text(budget, unit),
        )
        if unit and unit != "bytes":
            row = row + " " + unit
        if hit:
            row = row + "   [LIMIT HIT]"
        lines.append(row)
    return lines


# ---------------------------------------------------------------------------
# The event log
# ---------------------------------------------------------------------------
def event_counts(events):
    """How many events of each kind the run produced."""
    counts = {kind: 0 for kind in EVENT_KINDS}
    for entry in events or ():
        kind = (entry or {}).get("kind") if isinstance(entry, dict) \
            else "error"
        counts[kind] = counts.get(kind, 0) + 1
    return counts


def format_events(events, limit=40):
    """The run's errors, warnings, exceptions and limit hits as lines."""
    entries = [entry for entry in (events or ()) if entry]
    if not entries:
        return ["(no errors, warnings or limit hits)"]
    lines = []
    for entry in entries[:limit]:
        if isinstance(entry, dict):
            lines.append("%-9s %s" % (entry.get("kind", "error"),
                                      entry.get("detail", "")))
        else:
            lines.append(str(entry))
    if len(entries) > limit:
        lines.append("... and %d more" % (len(entries) - limit))
    return lines


# ---------------------------------------------------------------------------
# The policy document
# ---------------------------------------------------------------------------
def describe(monitor=None, limits=None, events=None):
    """A JSON-friendly description of the monitor, the limits and the events."""
    monitor = normalise_monitor(monitor or {})
    limits = normalise_limits(limits or {})
    return {
        "monitor": monitor,
        "limits": limits,
        "limits_hit": limits_hit(limits, monitor),
        "enabled": [key for key in LIMIT_KEYS if limits.get(key)],
        "monitored": list(MONITOR_KEYS),
        "events": list(events or ()),
        "event_counts": event_counts(events or ()),
    }


def monitor_capabilities():
    """Describe the runtime monitor and the limits for the audit layer."""
    return {
        "monitor": list(MONITOR_KEYS),
        "limits": list(LIMIT_KEYS),
        "monitor_items": [key for key, _lk, _f, _u in MONITOR_ITEMS],
        "limit_items": [key for key, _lk, _f, _u, _c in LIMIT_ITEMS],
        "child_enforced": list(CHILD_ENFORCED),
        "parent_enforced": list(PARENT_ENFORCED),
        "event_kinds": list(EVENT_KINDS),
        "ceilings": dict(LIMIT_CEILINGS),
        "platform": os.name,
    }
