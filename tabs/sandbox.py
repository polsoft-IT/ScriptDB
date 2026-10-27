# -*- coding: utf-8 -*-
"""Sandbox tab for ScriptDB.

This module owns the "Sandbox" tab: an isolated environment for running a
script safely. It sits right after the Debugger tab. Every run happens in a
throw-away working directory, inside a separate child process, under a small,
explicit policy, with a hard timeout and a cap on how much output is captured,
so a runaway or destructive snippet cannot harm the machine or hang the
application.

The tab follows the same one-file-per-tab pattern as the other tabs:

    SandboxTab(window)           -- build the tab
    tab.retranslate(table)       -- apply a language table (see TRANSLATIONS)
    tab.run_sandboxed()          -- run the selected script in the sandbox

What it offers:

    policy      a set of toggles that decide what a sandboxed run may do:
                allow network, allow filesystem writes (only ever inside the
                sandbox dir), allow child processes, a wall-clock timeout and
                an output cap
    presets     named policy presets (strict / standard / permissive) so a
                sensible configuration is one click away
    workspace   a private, per-run temporary directory the script runs in;
                anything it writes stays there and is removed afterwards
    output      captured stdout / stderr, the exit status, the elapsed time
                and how much output was truncated
    report      a structured summary of the run (policy, limits hit), suitable
                for the Reports tab

All of it is offline and dependency free: the sandbox uses only the standard
library, and it never sends anything anywhere.

The main window only needs a few lines to wire the tab in::

    from tabs import SandboxTab
    self.sandbox_tab = SandboxTab(self)
    self.tabs.addTab(self.sandbox_tab, "Sandbox")
"""

import datetime as dt
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtGui import QFont
from . import (sandbox_audit, sandbox_fs, sandbox_integration,
               sandbox_isolation, sandbox_langs, sandbox_monitor,
               sandbox_net, sandbox_server, sandbox_syscall)
SANDBOX_FONT = "Consolas"
SANDBOX_FONT_SIZE = 10

# Hard ceilings the sandbox never exceeds, whatever the policy asks for.
SANDBOX_MAX_TIMEOUT = 120        # seconds
SANDBOX_MIN_TIMEOUT = 1
SANDBOX_DEFAULT_TIMEOUT = 15
SANDBOX_DEFAULT_OUTPUT_CAP = 200000        # characters
SANDBOX_ABSOLUTE_OUTPUT_CAP = 2000000

# Named policy presets: (key, label key, fallback, settings).
POLICY_PRESETS = (
    ("strict", "sandbox_preset_strict", "Strict",
     {"timeout": 5, "output_cap": 50000, "allow_network": False,
      "allow_writes": False, "subprocess": False,
      "cpu_seconds": 5, "memory_mb": 128, "io_bytes": 4 * 1024 * 1024,
      "max_files": 32, "max_processes": 0, "max_threads": 4,
      "log_bytes": 256 * 1024}),
    ("standard", "sandbox_preset_standard", "Standard",
     {"timeout": 15, "output_cap": 200000, "allow_network": False,
      "allow_writes": True, "subprocess": True,
      "cpu_seconds": 15, "memory_mb": 256, "io_bytes": 64 * 1024 * 1024,
      "max_files": 512, "max_processes": 16, "max_threads": 16,
      "log_bytes": 2 * 1024 * 1024}),
    ("permissive", "sandbox_preset_permissive", "Permissive",
     {"timeout": 60, "output_cap": 1000000, "allow_network": True,
      "allow_writes": True, "subprocess": True,
      "cpu_seconds": 60, "memory_mb": 1024, "io_bytes": 512 * 1024 * 1024,
      "max_files": 4096, "max_processes": 64, "max_threads": 64,
      "log_bytes": 16 * 1024 * 1024}),
)

# The isolation switches, shown as their own group in the policy view. Every
# one is on by default and none of them is loosened by a preset: isolation is
# what makes the tab a sandbox, not a convenience setting.
ISOLATION_RULES = (
    ("isolate_process", "sandbox_iso_process", "Separate process",
     "enforced"),
    ("isolate_environment", "sandbox_iso_environment", "Separate environment",
     "scrubbed"),
    ("isolate_path", "sandbox_iso_path", "Separate PATH", "own"),
    ("isolate_cwd", "sandbox_iso_cwd", "Separate working directory",
     "isolated"),
    ("isolate_runtime", "sandbox_iso_runtime", "Separate runtime context",
     "replaced"),
    ("block_terminal", "sandbox_iso_terminal", "Terminal access", "blocked"),
)

# The filesystem rules, shown as their own group in the policy view.
FS_RULES = (
    ("fs_whitelist", "sandbox_fs_whitelist", "Path whitelist",
     "1 root"),
    ("fs_blacklist", "sandbox_fs_blacklist", "Path blacklist", "0 entries"),
    ("allow_read_outside", "sandbox_fs_read", "Read outside sandbox",
     "blocked"),
    ("allow_writes", "sandbox_fs_write", "Write outside sandbox",
     "blocked"),
    ("block_destructive", "sandbox_fs_destructive", "Destructive ops",
     "blocked"),
    ("block_recursive", "sandbox_fs_recursive", "Recursive ops", "blocked"),
    ("block_system_dirs", "sandbox_fs_system", "System directories",
     "blocked"),
    ("pseudo_root", "sandbox_fs_pseudo", "Virtual filesystem", "off"),
    ("shadow_copy", "sandbox_fs_shadow", "Shadow copy", "off"),
    ("snapshot_fs", "sandbox_fs_snapshot", "FS snapshot", "off"),
)

# The syscall filter, shown as its own group in the policy view. These are
# the operations a run may not make, which is a narrower question than which
# paths it may reach.
SYSCALL_RULES = (
    ("block_exec", "sandbox_sys_exec", "Exec (execve)", "blocked"),
    ("block_exec", "sandbox_sys_fork", "Fork", "blocked"),
    ("block_exec", "sandbox_sys_spawn", "Spawn", "blocked"),
    ("block_exec", "sandbox_sys_kill", "Kill", "always"),
    ("block_system", "sandbox_sys_mount", "Mount", "always"),
    ("block_system", "sandbox_sys_chmod", "Chmod", "blocked"),
    ("block_system", "sandbox_sys_chown", "Chown", "blocked"),
    ("block_files", "sandbox_sys_open", "Open", "whitelist"),
    ("block_files", "sandbox_sys_write", "Write", "whitelist"),
    ("block_network", "sandbox_sys_socket", "Socket", "blocked"),
    ("block_network", "sandbox_sys_bind", "Bind", "blocked"),
    ("block_network", "sandbox_sys_connect", "Connect", "blocked"),
)

# The network rules, shown as their own group in the policy view.
NET_RULES = (
    ("allow_network", "sandbox_net_network", "Network access", "blocked"),
    ("net_whitelist", "sandbox_net_whitelist", "Host whitelist",
     "0 hosts"),
    ("net_allowed_ports", "sandbox_net_allowed_ports", "Allowed ports",
     "any"),
    ("net_blocked_ports", "sandbox_net_ports", "Blocked ports", "-"),
    ("net_blocked_protocols", "sandbox_net_protocols", "Blocked protocols",
     "-"),
    ("block_raw_sockets", "sandbox_net_raw", "Raw sockets", "blocked"),
    ("block_icmp", "sandbox_net_icmp", "ICMP / ping", "blocked"),
    ("block_dns", "sandbox_net_dns", "DNS", "blocked"),
    ("block_private_ranges", "sandbox_net_private", "Private ranges",
     "blocked"),
    ("block_admin_ports", "sandbox_net_admin", "Admin ports", "blocked"),
    ("allow_http", "sandbox_net_http", "HTTP", "blocked"),
    ("allow_https", "sandbox_net_https", "HTTPS", "allowed"),
    ("monitor_network", "sandbox_net_monitor", "Traffic monitor", "off"),
)

# The runtime monitor, shown as its own group. These are the measurements the
# child reports after a run: the twelve items of the monitoring contract in
# `tabs.sandbox_monitor`, each with the unit its number is in.
MONITOR_RULES = tuple(
    (key, label_key, fallback, unit)
    for key, label_key, fallback, unit in sandbox_monitor.MONITOR_ITEMS
)

# The resource limits, shown as their own group. Every one is an integer budget
# enforced inside the child (see the runner source below), except the
# wall-clock timeout, which is applied by the parent because a run that has
# stopped responding cannot be trusted to enforce its own time budget.
LIMIT_RULES = tuple(
    (key, label_key, fallback, unit)
    for key, label_key, fallback, unit, _ceiling in sandbox_monitor.LIMIT_ITEMS
)

# The dangerous behaviours, shown as their own group: (key, label key,
# fallback, severity). The table is the one in `tabs.sandbox_audit`, so the
# detectors and the view can never disagree about what is checked.
BEHAVIOUR_RULES = tuple(
    (key, label_key, fallback, severity)
    for key, label_key, fallback, severity, _meaning in sandbox_audit.BEHAVIOURS
)

# The terminal modules the sandbox talks to: (key, label key, fallback).
# The table is the one in `tabs.sandbox_integration`, so the view, the CLI and
# the bridges can never disagree about what is wired up.
INTEGRATION_RULES = tuple(
    (key, label_key, fallback, direction)
    for key, label_key, fallback, direction, _exchange
    in sandbox_integration.INTEGRATIONS
)

# The sandbox scopes: (key, label key, fallback, what it reuses).
SCOPE_RULES = tuple(
    (key, label_key, fallback,
     ",".join(sandbox_integration.SandboxScope(key).reuses()) or "nothing")
    for key, label_key, fallback, _meaning in sandbox_integration.SCOPES
)

# The shadow layers the sandbox can substitute: (key, label key, fallback,
# what the substitute is).
SHADOW_RULES = tuple(
    (key, label_key, fallback, meaning)
    for key, label_key, fallback, meaning in sandbox_integration.SHADOW_LAYERS
)

# The premium features: (key, label key, fallback, what it gives a user).
PREMIUM_RULES = tuple(
    (key, label_key, fallback, meaning)
    for key, label_key, fallback, meaning in sandbox_server.PREMIUM_FEATURES
)

# The reports offered by the report picker: (key, label key, fallback).
REPORT_RULES = tuple(
    (key, label_key, fallback)
    for key, label_key, fallback, _desc in sandbox_audit.REPORTS
)

# The rules shown in the policy list (key, label key, fallback, severity).
POLICY_RULES = (
    ("network", "sandbox_rule_network", "Network access", "blocked"),
    ("writes", "sandbox_rule_writes", "Filesystem writes", "sandbox only"),
    ("subprocess", "sandbox_rule_subprocess", "Child processes", "allowed"),
    ("timeout", "sandbox_rule_timeout", "Wall-clock limit", "enforced"),
    ("output", "sandbox_rule_output", "Output cap", "enforced"),
    ("cwd", "sandbox_rule_cwd", "Working directory", "isolated"),
)

# Patterns used by the static policy scan to flag risky constructs.
_RISKY_PATTERNS = (
    ("network", re.compile(
        r"\b(requests|urllib|httplib|http\.client|socket|ftplib|smtplib|"
        r"telnetlib|aiohttp)\b")),
    ("writes", re.compile(
        r"\b(open\s*\([^)]*['\"][wax]|shutil\.(copy|move|rmtree)|"
        r"os\.(remove|unlink|rmdir|rename|makedirs|mkdir)|"
        r"pathlib\.Path\([^)]*\)\.write)")),
    ("subprocess", re.compile(
        r"\b(subprocess|os\.system|os\.popen|pty\.spawn|popen)\b")),
    ("exec", re.compile(r"\b(eval|exec|compile)\s*\(")),
)

# The source of the child-process runner. It is written into the sandbox
# workspace and launched with the isolated interpreter (python -I). Keeping it
# as a string means the parent process never has to import the sandboxed code.
_RUNNER_SOURCE = '''# -*- coding: utf-8 -*-
# Sandbox runner: execute SDB_SANDBOX_SCRIPT under the loaded policy.

import builtins
import os
import runpy
import sys
import threading
import time
import traceback

def _truthy(name):
    return os.environ.get(name, "0") == "1"


def _limit(name, fallback=0):
    # One resource budget as an integer (0 means "no ceiling of this kind").
    raw = os.environ.get(name, "")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return fallback
    return value if value > 0 else 0


ALLOW_NETWORK = _truthy("SDB_SANDBOX_ALLOW_NETWORK")
ALLOW_WRITES = _truthy("SDB_SANDBOX_ALLOW_WRITES")
ALLOW_SUBPROCESS = _truthy("SDB_SANDBOX_ALLOW_SUBPROCESS")
ISOLATED = _truthy("SDB_SANDBOX_ISOLATED")
WORKSPACE = os.environ.get("SDB_SANDBOX_WORKSPACE", "")

# --- filesystem rules ----------------------------------------------------
READ_OUTSIDE = _truthy("SDB_SANDBOX_READ_OUTSIDE")
BLOCK_DESTRUCTIVE = _truthy("SDB_SANDBOX_BLOCK_DESTRUCTIVE")
BLOCK_RECURSIVE = _truthy("SDB_SANDBOX_BLOCK_RECURSIVE")
BLOCK_SYSTEM_DIRS = _truthy("SDB_SANDBOX_BLOCK_SYSTEM_DIRS")
PSEUDO_ROOT = _truthy("SDB_SANDBOX_PSEUDO_ROOT")


def _env_list(name):
    # A newline-separated environment variable as a list (never None).
    raw = os.environ.get(name, "")
    return [entry for entry in raw.split(chr(10)) if entry]


FS_WHITELIST = _env_list("SDB_SANDBOX_FS_WHITELIST")
FS_BLACKLIST = _env_list("SDB_SANDBOX_FS_BLACKLIST")

# --- network rules -------------------------------------------------------
ALLOW_HTTP = _truthy("SDB_SANDBOX_ALLOW_HTTP")
ALLOW_HTTPS = _truthy("SDB_SANDBOX_ALLOW_HTTPS")
BLOCK_ICMP = _truthy("SDB_SANDBOX_BLOCK_ICMP")
BLOCK_DNS = _truthy("SDB_SANDBOX_BLOCK_DNS")
BLOCK_RAW = _truthy("SDB_SANDBOX_BLOCK_RAW")
NET_WHITELIST = _env_list("SDB_SANDBOX_NET_WHITELIST")
NET_BLOCKED_PORTS = _env_list("SDB_SANDBOX_NET_BLOCKED_PORTS")
NET_BLOCKED_PROTOCOLS = _env_list("SDB_SANDBOX_NET_BLOCKED_PROTOCOLS")

# --- the syscall filter -----------------------------------------------------
# One switch per group, plus the two syscalls that are never allowed whatever
# the switch says. The guards below consult these; the table itself lives in
# tabs/sandbox_syscall.py, which this file mirrors by hand because the child
# cannot import the application.
BLOCK_EXEC = _truthy("SDB_SANDBOX_BLOCK_EXEC")
BLOCK_SYSTEM = _truthy("SDB_SANDBOX_BLOCK_SYSTEM")
BLOCK_FILES = _truthy("SDB_SANDBOX_BLOCK_FILES")
BLOCK_NETWORK = _truthy("SDB_SANDBOX_BLOCK_NETWORK")

# mount and kill are not operations a sandboxed script ever has a reason to
# make, so they are refused even when their group is open.
_ALWAYS_BLOCKED = ("mount", "kill")

# The terminal's own modules: importing any of them would hand this run the
# host's live objects instead of an isolated copy, so they are refused.
_TERMINAL_MODULES = {
    "sdb", "sdb_scripts", "tabs", "PyQt5",
}
# Modules that reach across the process boundary (memory, the process
# table, the terminal itself). A run may never use them.
_ISOLATION_MODULES = {
    "ctypes", "cffi", "mmap", "fcntl", "termios", "tty", "pwd",
    "grp", "resource", "signal",
}
# The terminal's own data and configuration files, by base name.
_TERMINAL_FILES = {
    "scripts_db.json", "connections_db.json", "settings_db.json",
    "debugger_db.json", "audit_timeline.json", "docs_db.sqlite",
    "reports_index.json",
}
_TERMINAL_CONFIG_DIRS = {".sdb", "sdb_config", "sdb_settings"}
_SYSTEM_DIRS_WINDOWS = (
    "windows", "program files", "program files (x86)",
    "programdata", "system32", "syswow64", "winsxs",
    "$recycle.bin", "recovery")
_SYSTEM_DIRS_POSIX = (
    "etc", "usr", "bin", "sbin", "lib", "lib64", "boot",
    "dev", "proc", "sys", "root", "opt")

_BLOCKED = set(_TERMINAL_MODULES) | set(_ISOLATION_MODULES)
if not ALLOW_NETWORK:
    _BLOCKED.update({"socket", "ssl", "http", "urllib", "ftplib",
                     "smtplib", "telnetlib", "asyncio", "requests",
                     "httplib", "xmlrpc"})
if not ALLOW_SUBPROCESS:
    _BLOCKED.update({"subprocess", "pty", "multiprocessing"})

# Every refusal is written here so the parent can show what a run tried to
# do rather than only what it succeeded at. The parent reads this file back
# after the run and folds it into the report.
VIOLATIONS_FILE = os.environ.get("SDB_SANDBOX_VIOLATIONS", "")
VIOLATIONS = []

_real_open = builtins.open


def _record_violation(kind, detail):
    # Note one refused operation (best effort, never raises).
    entry = "%s\t%s" % (kind, (detail or "")[:200])
    VIOLATIONS.append(entry)
    if not VIOLATIONS_FILE:
        return
    try:
        with _real_open(VIOLATIONS_FILE, "a", encoding="utf-8") as handle:
            handle.write(entry + chr(10))
    except Exception:
        pass


# --- the runtime monitor and the resource limits ----------------------------
# Two things happen here, and both have to happen inside the child rather than
# in the parent, because the parent can only see the child from outside: what
# the run *measured* while it ran (the monitoring contract in
# tabs/sandbox_monitor.py, which this file mirrors by hand because the child
# cannot import the application) and what it *consumed* against its budgets.
#
# A limit is hit by refusing the operation that would cross it, rather than by
# killing the process after the fact: an exception explains itself in the run's
# own output, while a SIGKILL is only visible to the parent as an exit code.
# The one exception is `cpu_seconds`, where a CPU-bound loop makes no calls to
# refuse, so the wall-clock budget becomes the ceiling that finally stops it.
# The books are opened before anything is measured: the counters a budget is
# checked against, the event log the report reads back, and the two streams the
# limits are applied to.
EVENTS = []
EVENT_KINDS = []
MONITOR_IO = 0
MONITOR_FILES = 0
MONITOR_SYSCALLS = 0
MONITOR_CALLS = 0
MONITOR_MODULES = 0
MONITOR_VARIABLES = 0
LOG_BYTES = 0
_START = time.monotonic()
_START_CPU = time.process_time()

# `tracemalloc` is what makes the RAM budget a ceiling on the *run* rather than
# on the interpreter: it measures the Python heap the script allocates, and the
# baseline is subtracted so the runtime's own footprint is not charged to it.
# It is optional: without it, memory is measured rather than enforced.
_tracemalloc = None
_tracemalloc_base = 0
try:
    import tracemalloc as _tracemalloc
    _tracemalloc.start()
    _tracemalloc_base = _tracemalloc.get_traced_memory()[0]
except Exception:
    _tracemalloc = None
    _tracemalloc_base = 0
# The budgets, read once from the policy the parent shipped in the environment.
LIMIT_CPU = _limit("SDB_SANDBOX_LIMIT_CPU")
LIMIT_MEMORY = _limit("SDB_SANDBOX_LIMIT_MEMORY")
LIMIT_IO = _limit("SDB_SANDBOX_LIMIT_IO")
LIMIT_FILES = _limit("SDB_SANDBOX_LIMIT_FILES")
LIMIT_PROCESSES = _limit("SDB_SANDBOX_LIMIT_PROCESSES")
LIMIT_THREADS = _limit("SDB_SANDBOX_LIMIT_THREADS")
LIMIT_LOG = _limit("SDB_SANDBOX_LIMIT_LOG")
MONITOR = _truthy("SDB_SANDBOX_MONITOR")
MONITOR_FILE = os.environ.get("SDB_SANDBOX_MONITOR_PATH", "")

# The stderr budget is applied to a stream of this run's own, so capping the
# log never touches the interpreter's stream and cannot break a traceback that
# the runner itself prints after the script has ended.
_real_stderr_write = sys.stderr.write
class _CappedStderr(object):
    # A stderr with a byte budget: past it, diagnostics are dropped and the
    # drop is recorded as an event. This is what stops a warning-message storm
    # from filling the pipe (and the parent's log) past the run's own limit.
    encoding = "utf-8"
    errors = "replace"

    def write(self, text):
        global LOG_BYTES
        data = str(text)
        size = len(data.encode("utf-8", "replace"))
        if LIMIT_LOG and LOG_BYTES + size > LIMIT_LOG:
            LOG_BYTES = LIMIT_LOG
            _record_violation("limit:log_bytes", "%d >= %d"
                              % (LOG_BYTES, LIMIT_LOG))
            _real_stderr_write(
                "sandbox: log limit reached, diagnostics dropped" + chr(10))
            return len(data)
        LOG_BYTES += size
        _real_stderr_write(data)
        return len(data)

    def flush(self):
        try:
            sys.stderr.flush()
        except Exception:
            pass
    def writable(self):
        return True
    def isatty(self):
        return False
    def fileno(self):
        return 2
# The two streams the budgets apply to. stdout is left alone because the
# parent already caps it as it reads it, and because a script's own output is
# what the user asked to see, not a diagnostic to be dropped.
sys.stderr = _CappedStderr()


def _elapsed():
    return time.monotonic() - _START

# The gauge that reads the current thread count. Deliberately a name in this
# namespace rather than a direct call to `threading`, so a script cannot make
# the thread limit unenforceable by swapping `threading` for a stub of its own.
_thread_counter = threading.active_count

def _resource_used():
    # The run's own memory: the peak the script reached, on top of what the
    # interpreter had already allocated before the first guarded call, so the
    # budget is about the run and not about being a Python process.
    #
    # The peak is read rather than the current size because a script that
    # allocates 200 MB in a loop and then frees each block still needs the
    # budget to have been the thing that stopped it; reading only the total at
    # the last call would let the flood through between two calls.
    if not _tracemalloc:
        return 0
    try:
        _current, peak = _tracemalloc.get_traced_memory()
        return max(0, peak - _tracemalloc_base)
    except Exception:
        return 0

def _record_event(kind, detail):
    # Note one runtime event: an error, a warning, an uncaught exception or a
    # limit being hit. Kept in the same list shape the parent reads back from
    # the violations log, so the report has one vocabulary rather than two.
    EVENT_KINDS.append(kind)
    EVENTS.append((kind, str(detail)[:200]))
    _record_violation("event:%s" % kind, detail)


def _warn(detail):
    # A diagnostic that did not stop the run.
    _record_event("warning", detail)


def _error(detail):
    # A diagnostic that did stop the operation, but not the run.
    _record_event("error", detail)


def _limit_hit(name, used, budget):
    # Refuse an operation that would cross a budget, recording the refusal and
    # the event, so the report can say which ceiling was reached and by what.
    EVENTS.append(("resource", "%s: %s >= %s" % (name, used, budget)))
    _record_violation("limit:%s" % name, "%s >= %s" % (used, budget))
    raise MemoryError(
        "sandbox resource limit reached: %s (%s >= %s)"
        % (name, used, budget))

def _memory_now():
    # One memory reading: what the run holds on top of what the interpreter had
    # already allocated before the first guarded call.
    if not _tracemalloc:
        return 0
    try:
        current, _peak = _tracemalloc.get_traced_memory()
        return max(0, current - _tracemalloc_base)
    except Exception:
        return 0


def _check_budgets():
    # The CPU and memory ceilings, checked wherever the run makes a call (the
    # guarded entry points below) and once at the end. The CPU check is what
    # turns a CPU-bound loop into a refusal instead of a timeout.
    if LIMIT_CPU:
        used = time.process_time() - _START_CPU
        if used >= LIMIT_CPU:
            _limit_hit("cpu_seconds", "%.3f" % used, LIMIT_CPU)
    if LIMIT_MEMORY:
        used = _memory_now()
        if used >= LIMIT_MEMORY:
            _limit_hit("memory_bytes", used, LIMIT_MEMORY)



def _in_workspace(path):
    if not WORKSPACE:
        return False
    try:
        return os.path.commonpath(
            [os.path.abspath(path), os.path.abspath(WORKSPACE)]
        ) == os.path.abspath(WORKSPACE)
    except (ValueError, OSError):
        return False


def _is_terminal_path(path):
    # True when a path names the terminal data or config.
    try:
        resolved = os.path.abspath(str(path))
    except (TypeError, ValueError):
        return False
    if _in_workspace(resolved):
        return False
    if os.path.basename(resolved).lower() in _TERMINAL_FILES:
        return True
    parts = [part.lower() for part in
             resolved.replace(chr(92), "/").split("/")]
    return any(name in parts for name in _TERMINAL_CONFIG_DIRS)


def _system_dir_reason(path):
    # The system directory a path names (or an empty string).
    resolved = os.path.abspath(path)
    parts = [part.lower() for part in
             resolved.replace(chr(92), "/").split("/") if part]
    if ISOLATED and os.name == "nt":
        drive, tail = os.path.splitdrive(resolved)
        if drive and tail in (chr(92), "/", ""):
            return "system drive root"
        for marker in _SYSTEM_DIRS_WINDOWS:
            if marker in parts:
                return "system directory: %s" % marker
        return ""
    for marker in _SYSTEM_DIRS_POSIX:
        if marker in parts:
            return "system directory: %s" % marker
    return ""


def _simple_glob(text, pattern):
    # A tiny glob matcher (* and ? only), so a blacklist entry can never
    # reach the filesystem and the behaviour is the same on every platform.
    if not pattern:
        return False
    import re as _re
    out = ["^"]
    for character in pattern:
        if character == "*":
            out.append(".*")
        elif character == "?":
            out.append(".")
        else:
            out.append(_re.escape(character))
    out.append("$")
    try:
        return _re.match("".join(out), text) is not None
    except _re.error:
        return False


def _fs_refuse(path, operation, reason):
    # Refuse a filesystem operation, recording it for the run report.
    _record_violation("fs:%s" % operation, "%s (%s)" % (path, reason))
    raise PermissionError(
        "sandbox FS sandbox blocked %s of %r: %s"
        % (operation, str(path), reason))


def _on_blacklist(resolved, operation):
    # Refuse a path the blacklist names, either as a literal prefix or through
    # the small glob matcher. A refusal is raised, so returning means the path
    # is not on the list.
    base = os.path.basename(resolved).lower()
    for entry in FS_BLACKLIST:
        text = str(entry).strip()
        if not text:
            continue
        candidate = os.path.abspath(text).lower()
        flattened = candidate.rstrip(chr(92) + chr(47)) + os.sep
        if resolved.lower() == candidate \
                or resolved.lower().startswith(flattened):
            _fs_refuse(resolved, operation, "on the blacklist")
        if _simple_glob(base, text.lower()) \
                or _simple_glob(resolved.lower(), text.lower()):
            _fs_refuse(resolved, operation, "on the blacklist")


def _on_whitelist(resolved, operation):
    # A whitelisted root that is not read-only is the one place outside the
    # workspace a run may write to, so a broad `allow_writes` never becomes
    # permission to write anywhere on the machine. True means the path was
    # whitelisted and is now allowed.
    for root in FS_WHITELIST:
        text = str(root or "").strip()
        if not text:
            continue
        candidate = os.path.abspath(text).lower()
        flattened = candidate.rstrip(chr(92) + chr(47)) + os.sep
        if resolved.lower() == candidate \
                or resolved.lower().startswith(flattened):
            if operation == "write":
                _fs_refuse(resolved, operation,
                           "whitelisted path is read-only")
            return True
    return False

def _check_path(path, operation):
    # Apply the blacklist, terminal, system-dir and wall rules. This mirrors
    # tabs.sandbox_fs.check, so a refusal inside the run and one predicted by
    # the report are the same decision.
    try:
        resolved = os.path.abspath(str(path))
    except (TypeError, ValueError):
        _fs_refuse(path, operation, "unusable path")
        return ""
    if WORKSPACE and _in_workspace(resolved):
        # The workspace is the run's own, but a policy that blocks writes
        # blocks them here too: the strict preset means no writes at all, not
        # "no writes except the ones that are convenient".
        if operation != "read" and not ALLOW_WRITES:
            _fs_refuse(resolved, operation,
                       "writes are blocked by the policy")
        return resolved
    _on_blacklist(resolved, operation)
    if _is_terminal_path(resolved):
        _fs_refuse(resolved, operation, "terminal data or configuration")
    if BLOCK_SYSTEM_DIRS:
        reason = _system_dir_reason(resolved)
        if reason:
            _fs_refuse(resolved, operation, reason)
    if _on_whitelist(resolved, operation):
        return resolved
    if operation == "write":
        _fs_refuse(resolved, operation,
                   "writes are blocked outside the sandbox")
    if operation == "read" and not READ_OUTSIDE:
        _fs_refuse(resolved, operation, "outside the sandbox")
    return resolved


def _map_pseudo(path):
    # Map a virtual path onto the workspace when the pseudo root is on, so a
    # script that insists on writing to /tmp/x still succeeds.
    if not PSEUDO_ROOT or not WORKSPACE:
        return ""
    text = str(path).replace(chr(92), "/")
    for virtual in ("/tmp", "/var/tmp", "/data",
                    "C:/data", "C:/temp"):
        if text == virtual or text.startswith(virtual + "/"):
            slug = virtual.replace(":", "").replace("/", "_").strip("_")
            tail = text[len(virtual):].lstrip("/")
            base = os.path.join(WORKSPACE, "_pseudo", slug or "root")
            return os.path.join(base, tail.replace("/", os.sep))
    return ""


# --- dangerous behaviour, detected from inside the run -----------------------
# The detectors here cover the four shapes of misbehaviour that can only be
# seen while a run is live: an infinite loop, a recursion overflow, a memory
# leak, a deadlock and a race. The four "left the sandbox" behaviours need no
# code here: every refusal the guards below record is already the evidence, and
# tabs/sandbox_audit.py reads it back.
#
# The same principle as the resource budgets applies: a *trace* is recorded
# here (a lock was taken, threads overlapped, a stack grew without bound) and
# the *verdict* is drawn by the parent, because a trace on its own is a fact
# and only the report can say what it means.

# The deepest call the run reached. `sys.setrecursionlimit` is left where the
# interpreter put it: raising it is the first thing a runaway recursion reaches
# for, and the guard below refuses it so the run meets the limit rather than
# moving it out of the way.
_recursion_depth = 0
# The depth at which a run is warned that its recursion is unusual rather than
# merely deep. Well below the interpreter's own limit (~1000), because the
# interesting case is a recursion that is *growing*, and a run that reaches
# this and then raises is reported as an overflow rather than as a warning.
_RECURSION_DEPTH_WARN = 64
# How many `behaviour:` traces have been recorded, so a report can say the run
# was flagged without the parent having to re-derive it.
_BEHAVIOUR_TRACES = 0

# Whether the run ever held a lock, and whether two threads overlapped on the
# same object. Both are traces, not verdicts: a script that locks correctly
# leaves the first trace and not the second.
_lock_taken = 0
_lock_released = 0
_shared_writes = 0
_watched_objects = {}

# The memory the run held at its lowest and highest, sampled at every guarded
# call, so a leak (monotonic growth) can be told from a spike (one big
# allocation that was freed).
_memory_samples = []


def _sample_memory():
    # One memory reading into a bounded ring of recent samples. The ring is what
    # lets the parent tell a leak (steady growth) from a spike (one allocation
    # that was then freed), which a single figure could not.
    if not _tracemalloc:
        return
    try:
        _current, _peak = _tracemalloc.get_traced_memory()
    except Exception:
        return
    value = max(0, _current - _tracemalloc_base)
    _memory_samples.append(value)
    if len(_memory_samples) > 256:
        del _memory_samples[0]


def _note_lock(acquired):
    # One lock being taken or given back.
    global _lock_taken, _lock_released
    if acquired:
        _lock_taken += 1
    else:
        _lock_released += 1


def _note_shared_write(obj_id):
    # Two threads reached the same object: the trace a race leaves behind.
    global _shared_writes
    owner = threading.current_thread().ident
    previous = _watched_objects.get(obj_id)
    if previous is not None and previous != owner:
        _shared_writes += 1
    _watched_objects[obj_id] = owner


def _is_recursive_call(frame):
    # True when `frame`'s code object appears again further up the stack, which
    # is what recursion looks like at the moment it happens. Bounded to a few
    # hundred frames: a stack deeper than that is already reported by the depth
    # counter, and unbounded walking would be its own denial of service.
    code = frame.f_code
    seen = 0
    current = frame.f_back
    while current is not None and seen < 400:
        if current.f_code is code:
            return True
        current = current.f_back
        seen += 1
    return False


def _recursion_trace(overflowed=False):
    # The trace of a stack that was exhausted. Called from `main` when the run
    # ended in a RecursionError, and only then: the depth itself is recorded
    # below whatever happens, because a report wants to say how deep the run
    # went even when going there was the point.
    _record_violation("behaviour:recursion",
                      "call depth %d at the deepest (interpreter limit %d)"
                      % (_recursion_depth, sys.getrecursionlimit()))


def _leak_trace():
    # The trace of memory that only ever grew.
    #
    # The signature is monotonic growth, not a high peak: a run that allocates
    # one large buffer and holds it grows once and then stays flat, which is a
    # big allocation, while a leak keeps climbing with every iteration. The
    # comparison is therefore between the last sample and the one a quarter of
    # the samples back, so a single early spike cannot be mistaken for a leak.
    samples = [value for value in _memory_samples if value]
    if len(samples) < 8:
        return
    window = samples[len(samples) // 4:]
    if len(window) < 4:
        return
    grew = window[-1] > window[0]
    climbed = sum(1 for index in range(len(window) - 1)
                  if window[index + 1] >= window[index])
    # Three quarters of the steps rising, and a substantial absolute increase:
    # that is a leak rather than noise from the allocator's own behaviour.
    if grew and climbed >= (len(window) - 1) * 0.75 \
            and window[-1] - window[0] > 2 * 1024 * 1024:
        EVENTS.append(("behaviour", "memory rose from %d to %d byte(s) and "
                                    "never came back down"
                                    % (window[0], window[-1])))
        _record_violation(
            "behaviour:memory_leak",
            "%d byte(s) held at the peak and never released" % window[-1])


def _concurrency_trace():
    # The traces a deadlock and a race leave behind.
    if _lock_taken > _lock_released:
        EVENTS.append(("behaviour", "%d lock(s) still held when the budget "
                                    "ended" % (_lock_taken - _lock_released)))
        _record_violation(
            "behaviour:deadlock",
            "lock held at the end of the run (%d taken, %d released)"
            % (_lock_taken, _lock_released))
    if _shared_writes:
        EVENTS.append(("behaviour",
                       "%d shared write(s) seen from more than one thread"
                       % _shared_writes))
        _record_violation(
            "behaviour:race_condition",
            "%d shared object(s) touched by more than one thread"
            % _shared_writes)


# The instruments. Each replaces a stdlib name with a wrapper that records the
# trace and then behaves exactly as before, so a run that does not misbehave
# cannot tell the difference.
_REAL_THREAD_WRITE_CALLS = {}
_real_lock = threading.Lock


class _InstrumentedLock(object):
    # A lock that counts being taken and being given back. The count is what
    # distinguishes a deadlock (taken and never released) from normal locking,
    # which a script may do as often as it likes.
    def __init__(self):
        self._lock = _real_lock()

    def acquire(self, *args, **kwargs):
        _note_lock(True)
        result = self._lock.acquire(*args, **kwargs)
        if result and LIMIT_CPU:
            _check_budgets()
        return result

    def release(self):
        _note_lock(False)
        return self._lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False

    def locked(self):
        return getattr(self._lock, "locked", lambda: False)()


threading.Lock = _InstrumentedLock
if hasattr(threading, "RLock"):
    _real_rlock = threading.RLock

    class _InstrumentedRLock(_InstrumentedLock):
        def __init__(self):
            self._lock = _real_rlock()
    threading.RLock = _InstrumentedRLock


def _setrecursionlimit_refuse(limit):
    # Moving the stack limit out of the way is an attempt to escape the one
    # guard a runaway recursion cannot otherwise pass.
    _record_violation("behaviour:bypass",
                      "attempted sys.setrecursionlimit(%r)" % (limit,))
    EVENTS.append(("behaviour", "sys.setrecursionlimit was refused"))
    raise RecursionError(
        "sandbox refused sys.setrecursionlimit(%r): the recursion guard is "
        "part of the sandbox" % (limit,))


sys.setrecursionlimit = _setrecursionlimit_refuse
# The recursion counter is driven by a trace function rather than by wrapping
# calls: the interpreter already reports every call and every return, and using
# that is both cheaper and total (it cannot be missed by a call made through a
# route the wrappers below do not cover). `sys.setprofile` is used rather than
# `sys.settrace` because only the call/return events are needed.
# Set by `main` when the run ended in a RecursionError, read by `_collect` so
# the depth that was reached is reported as an overflow rather than as an
# incidental measurement.
_overflowed = False


def _recursion_overflowed():
    return _overflowed


def _profile_calls(frame, event, arg):
    # The depth is tracked silently, per call and per return. Nothing is
    # *reported* here: a run that recurses 300 frames deep and finishes is a run
    # with a deep recursion, not a run with a bug, and writing an event for it
    # would flag every recursive algorithm as misbehaving. The depth is reported
    # only when it is the thing that ended the run (an actual overflow, caught
    # in `main`), so the finding is about the failure and not about the depth.
    global _recursion_depth
    if event == "call":
        _recursion_depth += 1
        # Memory is sampled on every call, not only on guarded ones: a leak is
        # a loop that allocates without ever touching a file or a socket, and
        # waiting for a guarded call would miss exactly the case that matters.
        if LIMIT_MEMORY:
            _sample_memory()
    elif event == "return":
        if _recursion_depth > 0:
            _recursion_depth -= 1
    return None

# --- the syscall filter -----------------------------------------------------
def _syscall_blocked(name):
    # (blocked, reason) for one syscall, read from the flags above. `mount` and
    # `kill` are refused whatever the group switch says.
    if name in _ALWAYS_BLOCKED:
        return True, "%s is blocked" % name
    group = _SYSCALL_GROUP.get(name)
    if group is None:
        return False, ""
    if group == "process":
        blocked = BLOCK_EXEC
    elif group == "system":
        blocked = BLOCK_SYSTEM
    elif group == "files":
        blocked = BLOCK_FILES
    else:
        blocked = BLOCK_NETWORK
    if blocked:
        return True, "%s is blocked" % name
    return False, ""


def _syscall_refuse(name, detail=""):
    # Refuse a guarded syscall, recording it for the run report. The recorded
    # kind is `sys:<name>`, so the parent can count refusals by syscall.
    MONITOR_SYSCALLS += 1
    reason = "%s is blocked" % name
    _record_violation("sys:%s" % name, detail or reason)
    raise PermissionError(
        "sandbox syscall filter blocked %s: %s" % (name, detail or reason))

# Every call that is monitored rather than refused is counted here: a script
# that never touches the filter is still watched, which is the point of the
# monitor. Counting happens on the wrapper rather than inside each function so
# there is exactly one place that knows how a call is attributed.
_MONITORED_CALLS = ("open", "write", "socket", "connect", "spawn", "fork")


def _guard_syscall(name, real, detail=""):
    # Wrap one entry point so the filter is consulted, the call is counted and
    # the budgets are checked *before* the operation happens rather than after.
    blocked, _reason = _syscall_blocked(name)
    counted = MONITOR and name in _MONITORED_CALLS
    if not blocked and not counted:
        return real
    def _wrapper(*args, **kwargs):
        global MONITOR_CALLS, MONITOR_SYSCALLS
        MONITOR_SYSCALLS += 1
        MONITOR_CALLS += 1
        _track_variables()
        _sample_memory()
        if LIMIT_FILES:
            target = args[0] if args else kwargs.get("file")
            if _looks_like_new_file(target, name):
                if MONITOR_FILES >= LIMIT_FILES:
                    _limit_hit("max_files", MONITOR_FILES + 1, LIMIT_FILES)
        evidence = detail or _describe_call(real, args, kwargs)
        if blocked:
            _syscall_refuse(name, evidence)
        _check_budgets()
        return real(*args, **kwargs)
    try:
        _wrapper.__name__ = getattr(real, "__name__", name)
        _wrapper.__doc__ = getattr(real, "__doc__", "")
    except (AttributeError, TypeError):
        pass
    return _wrapper

def _describe_call(real, args, kwargs):
    # A short description of the refused call, so the report says what was
    # attempted rather than only which syscall it was.
    try:
        rendered = ", ".join([repr(item)[:40] for item in args[:3]])
    except Exception:
        rendered = ""
    return "%s(%s)" % (getattr(real, "__name__", "?"), rendered)

_SYSCALL_GROUP = {
    "exec": "process", "fork": "process", "spawn": "process",
    "kill": "process",
    "mount": "system", "chmod": "system", "chown": "system",
    "open": "files", "write": "files",
    "socket": "network", "bind": "network", "connect": "network",
}

_real_remove = os.remove
_real_chmod = getattr(os, "chmod", None)
_real_chown = getattr(os, "chown", None)


def _guarded_remove(path, *args, **kwargs):
    global MONITOR_CALLS
    MONITOR_CALLS += 1
    _track_variables()
    if BLOCK_DESTRUCTIVE:
        _fs_refuse(path, "remove", "destructive operation blocked")
    _check_path(path, "remove")
    return _real_remove(path, *args, **kwargs)


_real_rmtree = None
try:
    import shutil as _shutil
    _real_rmtree = _shutil.rmtree
except ImportError:
    _shutil = None


def _guarded_rmtree(path, *args, **kwargs):
    if BLOCK_RECURSIVE:
        _fs_refuse(path, "rmtree", "recursive operation blocked")
    if not _in_workspace(os.path.abspath(str(path))):
        _fs_refuse(path, "rmtree", "outside the sandbox")
    return _real_rmtree(path, *args, **kwargs)


def _guarded_chmod(path, *args, **kwargs):
    if BLOCK_DESTRUCTIVE:
        _fs_refuse(path, "chmod", "destructive operation blocked")
    _check_path(path, "chmod")
    return _real_chmod(path, *args, **kwargs)


def _guarded_chown(path, *args, **kwargs):
    if BLOCK_DESTRUCTIVE:
        _fs_refuse(path, "chown", "destructive operation blocked")
    _check_path(path, "chown")
    return _real_chown(path, *args, **kwargs)

# chown does not exist on every platform. Where it is missing the name is
# published anyway, so a script that reaches for it meets the filter rather
# than an AttributeError that reads like "the call is not available here".
if _real_chown is None:
    def _missing_chown(path, *args, **kwargs):
        _syscall_refuse("chown", "os.chown")


def _looks_like_new_file(target, operation):
    # True when a call is about to *create* a file rather than touch one that
    # already exists, which is what the file-count budget is about. Removing a
    # file never crosses it, and re-opening one never counts twice.
    if operation in ("remove", "unlink", "rmdir"):
        return False
    try:
        path = str(target)
    except Exception:
        return False
    if not path or path in ("", "."):
        return False
    try:
        return not os.path.exists(path) and not os.path.isdir(path)
    except (OSError, ValueError):
        return False

def _is_own_file(file):
    # True for the sandbox's own runner and script, which the interpreter loads
    # from the workspace before the script runs. Refusing those reads would stop
    # the run before it started rather than sandbox it, so both the file-count
    # budget and the `open` switch exempt them.
    return str(file).endswith(("_sandbox_runner.py", "_sandbox_script.py"))


def _check_open_path(file, mode):
    # The rules an `open` call has to pass, in one place so the guarded entry
    # point and the syscall wrapper below can never disagree about them.
    #
    # `open` and `write` are separate syscalls with separate switches, so a
    # policy can refuse opening a file at all while still allowing a write to
    # an already-open handle. Only the read side is governed by the `open`
    # switch. A write is the `write` syscall, which has its own switch and its
    # own path rules, so a policy that blocks `open` does not also silence a
    # run's own output.
    #
    # The sandbox's own two files are exempt: the interpreter loads the runner
    # and the script from the workspace before the script runs, so refusing
    # those reads would stop the run before it started rather than sandbox it.
    global MONITOR_FILES
    write = any(flag in str(mode) for flag in ("w", "a", "x", "+"))
    own_file = _is_own_file(file)
    # Creating a file is what the file-count budget bounds, so it is checked
    # before the path rules rather than after: a run that has filled its
    # allowance is refused whether or not the path would have been allowed.
    if LIMIT_FILES and not own_file \
            and _looks_like_new_file(file, "write" if write else "read"):
        if MONITOR_FILES >= LIMIT_FILES:
            _limit_hit("max_files", MONITOR_FILES + 1, LIMIT_FILES)
        MONITOR_FILES += 1
    _check_budgets()
    if not write and not own_file:
        blocked, _reason = _syscall_blocked("open")
        # The `open` switch governs opening a path *outside the whitelist*,
        # which is what its reason says. A read inside the run's own workspace
        # is the path rules' business, not the syscall filter's, so it is
        # allowed here and decided by `_check_path` below.
        if blocked and not _in_workspace(os.path.abspath(str(file or "."))):
            _syscall_refuse("open", "open(%r, %r)" % (file, mode))
    return write

def _guarded_open(file, mode="r", *args, **kwargs):
    global MONITOR_FILES, MONITOR_CALLS, MONITOR_SYSCALLS
    MONITOR_CALLS += 1
    MONITOR_SYSCALLS += 1
    _track_variables()
    _sample_memory()
    write = _check_open_path(file, mode)
    target = _map_pseudo(file)
    if target:
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
        except OSError:
            pass
        file = target
    _check_path(file, "write" if write else "read")
    handle = _real_open(file, mode, *args, **kwargs)
    if LIMIT_IO and write:
        handle = _CountedWriter(handle)
    return handle
class _CountedWriter(object):
    # A write handle with an IO budget. It proxies the real file rather than
    # subclassing `io` (which is a C type that cannot be re-opened), so a
    # script sees an object whose `write`, `writelines` and `close` behave the
    # way they always did, with the running total added on top.
    def __init__(self, handle):
        self._handle = handle
    def write(self, data):
        global MONITOR_IO
        size = len(data) if isinstance(data, (bytes, bytearray)) else len(
            str(data).encode("utf-8", "replace"))
        if MONITOR_IO + size > LIMIT_IO:
            _limit_hit("io_bytes", MONITOR_IO + size, LIMIT_IO)
        MONITOR_IO += size
        _sample_memory()
        return self._handle.write(data)

    def writelines(self, lines):
        for line in lines:
            self.write(line)

    def __getattr__(self, name):
        return getattr(self._handle, name)

    def __iter__(self):
        return iter(self._handle)

    def __enter__(self):
        return self
    def __exit__(self, *exc):
        self.close()
        return False


def _host_matches(host, pattern):
    # A whitelist entry covers a host and its sub-domains:
    # example.com matches api.example.com but never notexample.com, because
    # the comparison is over dot-separated labels rather than raw text.
    host = (host or "").lower().rstrip(".")
    pattern = (pattern or "").lower().rstrip(".")
    if not host or not pattern:
        return False
    if host == pattern:
        return True
    return host.endswith("." + pattern)


def _net_refuse(host, port, protocol, reason):
    # Refuse a network operation, recording it for the run report.
    _record_violation("net:%s" % protocol,
                      "%s:%s (%s)" % (host, port, reason))
    raise OSError(
        "sandbox network isolation blocked %s to %s:%s: %s"
        % (protocol, host, port, reason))


def _check_connection(host, port, protocol):
    # Apply the network rules to one connection.
    name = (host or "").lower()
    port = int(port) if str(port).isdigit() else port
    if BLOCK_RAW and protocol in ("raw", "packet"):
        _net_refuse(name, port, protocol, "raw sockets are blocked")
    if BLOCK_ICMP and protocol in ("icmp", "icmpv6"):
        _net_refuse(name, port, protocol, "ICMP is blocked")
    if BLOCK_DNS and (protocol == "dns" or str(port) == "53"):
        _net_refuse(name, port, protocol, "DNS is blocked")
    if not ALLOW_NETWORK:
        _net_refuse(name, port, protocol, "network is blocked")
    local = (name in ("localhost", "127.0.0.1", "::1", "[::1]")
             or name.startswith("127.") or name.startswith("10.")
             or name.startswith("192.168.")
             or name.startswith("169.254."))
    if local:
        _net_refuse(name, port, protocol, "loopback/private address")
    if str(port) in NET_BLOCKED_PORTS:
        _net_refuse(name, port, protocol, "port is blocked")
    if protocol in NET_BLOCKED_PROTOCOLS \
            or str(port) in NET_BLOCKED_PROTOCOLS:
        _net_refuse(name, port, protocol, "protocol is blocked")
    if (protocol == "http" or str(port) == "80") and not ALLOW_HTTP:
        _net_refuse(name, port, protocol, "HTTP is blocked")
    if (protocol == "https" or str(port) == "443") and not ALLOW_HTTPS:
        _net_refuse(name, port, protocol, "HTTPS is blocked")
    for entry in NET_WHITELIST:
        if _host_matches(name, entry):
            return True
    _net_refuse(name, port, protocol, "host is not on the whitelist")


def _address_parts(address):
    # (host, port) from whatever shape an address argument has.
    if isinstance(address, tuple) and len(address) >= 2:
        return str(address[0]), address[1]
    if len(address) == 1 and isinstance(address[0], tuple) \
            and len(address[0]) >= 2:
        return str(address[0][0]), address[0][1]
    return (str(address[0]) if address else ""), 0


import socket as _socket_module

_real_socket = _socket_module.socket
_real_create_connection = _socket_module.create_connection


class _GuardedSocket(_real_socket):
    # A subclass, so isinstance() and the stdlib's own type checks keep
    # working for the wrappers that inspect the object they are given.
    def __init__(self, family=-1, type=-1, proto=-1, fileno=None):
        # A raw or packet socket sits below the transport layer: no host or
        # port rule can constrain what it does, so it is refused here, at the
        # point the socket is built, whatever the policy allows elsewhere.
        if BLOCK_RAW:
            raw_types = {int(_real_socket.SOCK_RAW)}
            for extra in ("SOCK_PACKET", "SOCK_RDM"):
                found = getattr(_real_socket, extra, None)
                if found is not None:
                    raw_types.add(int(found))
            try:
                if int(type) in raw_types:
                    _net_refuse("-", 0, "raw",
                                "raw sockets are always blocked")
            except (TypeError, ValueError):
                pass
        # The socket syscall itself is its own switch, so a policy can refuse
        # socket creation even when it allows a specific host to be reached.
        blocked, _reason = _syscall_blocked("socket")
        if blocked:
            _syscall_refuse("socket", "socket(%s, %s, %s)"
                            % (family, type, proto))
        _real_socket.__init__(self, family, type, proto, fileno)

    def bind(self, address):
        # bind decides what the socket *is* rather than who it talks to, so it
        # has its own switch: a run that may connect to a whitelisted host has
        # no reason to open a listening port on the machine.
        blocked, _reason = _syscall_blocked("bind")
        if blocked:
            _syscall_refuse("bind", "socket.bind(%r)" % (address,))
        host, port = _address_parts(address)
        _check_connection(host, port, "bind")
        return _real_socket.bind(self, address)

    def listen(self, *args, **kwargs):
        blocked, _reason = _syscall_blocked("bind")
        if blocked:
            _syscall_refuse("bind", "socket.listen")
        return _real_socket.listen(self, *args, **kwargs)

    def connect(self, address):
        host, port = _address_parts(address)
        blocked, _reason = _syscall_blocked("connect")
        if blocked:
            _syscall_refuse("connect",
                            "socket.connect(%s:%s)" % (host, port))
        _check_connection(host, port, "tcp")
        return _real_socket.connect(self, address)

    def connect_ex(self, address):
        host, port = _address_parts(address)
        blocked, _reason = _syscall_blocked("connect")
        if blocked:
            _syscall_refuse("connect",
                            "socket.connect_ex(%s:%s)" % (host, port))
        _check_connection(host, port, "tcp")
        return _real_socket.connect_ex(self, address)

    def sendto(self, data, *args):
        host, port = _address_parts(args) if args else ("", 0)
        _check_connection(host, port, "udp")
        return _real_socket.sendto(self, data, *args)


def _guarded_create_connection(address, *args, **kwargs):
    host, port = _address_parts(address)
    _check_connection(host, port, "tcp")
    return _real_create_connection(address, *args, **kwargs)


def _guarded_import(name, *args, **kwargs):
    global MONITOR_MODULES
    root = (name or "").split(".")[0]
    if root in _BLOCKED:
        # ImportError is what real code catches around an import; the message
        # still says the isolation, not the policy, refused it.
        raise ImportError("sandbox isolation blocked import of %r" % root)
    # Only a first import is counted: a module that is already in `sys.modules`
    # is a dictionary lookup, not a call into the runtime, and counting it would
    # make the number say more about how often a name is written than about what
    # the run loaded.
    if MONITOR and root not in _IMPORTED_BEFORE:
        _IMPORTED_BEFORE.add(root)
        if root not in sys.modules:
            MONITOR_MODULES += 1
    return _real_import(name, *args, **kwargs)

_real_import = builtins.__import__
_IMPORTED_BEFORE = set(sys.modules)
builtins.__import__ = _guarded_import
# --- variable change tracking ------------------------------------------------
# A script's globals are watched for rebinding between the calls it makes, so
# the report can say how much state the run moved rather than only what it
# called. The snapshot is taken here (globals at the moment the guards are
# installed) and refreshed by each guarded call, so the count is of changes
# that happened *during* the run.
_VARIABLE_SNAPSHOT = dict(globals())


def _track_variables():
    global MONITOR_VARIABLES
    if not MONITOR:
        return
    frame = sys._getframe(1)
    seen = 0
    while frame is not None and seen < 3:
        store = frame.f_globals
        for key, value in list(store.items()):
            previous = _VARIABLE_SNAPSHOT.get(key, _UNSET)
            if previous is _UNSET:
                _VARIABLE_SNAPSHOT[key] = value
                MONITOR_VARIABLES += 1
            elif previous is not value:
                _VARIABLE_SNAPSHOT[key] = value
                MONITOR_VARIABLES += 1
        frame = frame.f_back
        seen += 1
class _Unset(object):
    # The "this name was not here a moment ago" marker, so an assignment can be
    # told apart from a name that merely held None all along.
    __slots__ = ()

_UNSET = _Unset()

builtins.open = _guarded_open
# Every destructive entry point a script can reach is replaced, not only the
# one that is easiest to call: remove and rmtree are what a runaway script
# reaches for first, and chmod/chown are what quietly make a machine
# unrecoverable.
for _name in ("remove", "unlink", "rmdir", "removedirs", "rename",
             "replace", "truncate"):
    if hasattr(os, _name):
        setattr(os, _name, _guarded_remove)
if _real_chmod is not None:
    os.chmod = _guarded_chmod
if _real_chown is not None:
    os.chown = _guarded_chown
else:
    # A platform without chown still gets the name, so a script that reaches
    # for it meets the filter instead of an AttributeError.
    os.chown = _missing_chown
if _shutil is not None:
    _shutil.rmtree = _guarded_rmtree
_socket_module.socket = _GuardedSocket
_socket_module.create_connection = _guarded_create_connection
# --- install the syscall filter ---------------------------------------------
# Every entry point that reaches a guarded syscall is replaced, so the filter
# is consulted and the budgets are checked before the operation happens rather
# than after. The instrumentation that is not a refusal (threads, processes,
# files, IO and variable changes) is installed here too, for the same reason:
# the count has to come from the run itself, not be guessed afterwards.

_thread_start_real = threading.Thread.start


# --- the process guard ------------------------------------------------------
# The fifth handle back into the terminal is its process table. A run that can
# enumerate it learns what the user is running; a run that can signal it can
# stop the terminal outright. `signal` and `subprocess` are already refused at
# import, so the guard below closes the remaining routes: the process table
# itself, and any signal aimed at a process that is not this one.
try:
    import signal as _signal_module
except ImportError:
    _signal_module = None


def _own_pids():
    # The only process ids a run is allowed to name: itself, and the process
    # group it was started in (which the sandbox created for it).
    pids = set()
    for name in ("getpid", "getppid"):
        getter = getattr(os, name, None)
        if callable(getter):
            try:
                pids.add(int(getter()))
            except Exception:
                pass
    return pids


def _process_refuse(action, detail):
    # Refuse one process operation, recording it as its own behaviour so the
    # report can say the run reached for the terminal's process table.
    _record_violation("behaviour:process_access", "%s: %s" % (action, detail))
    raise PermissionError(
        "sandbox process isolation blocked %s: %s" % (action, detail))


def _guarded_kill(pid, *args, **kwargs):
    # A signal aimed at a process the run did not create is a signal aimed at
    # the terminal or at something the user is doing.
    try:
        target = int(pid)
    except (TypeError, ValueError):
        target = -1
    if target < 0 or target not in _own_pids():
        _process_refuse("os.kill", "pid %r" % (pid,))
    return _real_kill(pid, *args, **kwargs)


_real_kill = getattr(os, "kill", None)
if _real_kill is not None:
    os.kill = _guarded_kill
if _signal_module is not None:
    _real_signal = getattr(_signal_module, "signal", None)
    if _real_signal is not None:
        def _guarded_signal(sig, handler):
            # A run may install its own handler for its own signals; it may not
            # take over the ones the terminal relies on to shut down.
            if int(sig) in (getattr(_signal_module, "SIGTERM", 15),
                            getattr(_signal_module, "SIGINT", 2),
                            getattr(_signal_module, "SIGHUP", 1)):
                _process_refuse("signal.signal",
                                "signal %r" % (sig,))
            return _real_signal(sig, handler)
        _signal_module.signal = _guarded_signal


class _InstrumentedThread(threading.Thread):
    # The process-wide thread switch, installed as `threading.Thread`. The
    # check is in `start` rather than in `__init__`, because creating a thread
    # object costs nothing: it is starting it that puts a second thread on the
    # machine and so has to be bounded.

    def start(self):
        if LIMIT_THREADS and _thread_counter() >= LIMIT_THREADS:
            _limit_hit("max_threads", _thread_counter(), LIMIT_THREADS)
        return _thread_start_real(self)

threading.Thread = _InstrumentedThread

# exec: replacing the process image, or handing a string to a shell.
for _name in ("execv", "execve", "execl", "execle", "execlp", "execlpe",
              "execvp", "execvpe", "system", "spawnv", "spawnve", "spawnl",
              "spawnle", "spawnlp", "spawnlpe", "spawnvp", "spawnvpe",
              "posix_spawn", "posix_spawnp"):
    if hasattr(os, _name):
        setattr(os, _name,
                _guard_syscall("exec", getattr(os, _name), "os.%s" % _name))

# fork: a second copy of this process, outside the policy entirely. On a
# platform without fork the name is published rather than skipped, so a script
# that reaches for it meets the filter rather than an AttributeError that could
# be mistaken for the call not existing.
for _name in ("fork", "forkpty"):
    if hasattr(os, _name):
        setattr(os, _name,
                _guard_syscall("fork", getattr(os, _name), "os.%s" % _name))
    else:
        setattr(os, _name, _guard_syscall("fork", None, "os.%s" % _name))

# kill: signalling another process, including the parent.
for _name in ("kill", "killpg"):
    if hasattr(os, _name):
        setattr(os, _name,
                _guard_syscall("kill", getattr(os, _name), "os.%s" % _name))

# mount / chroot: changing what the filesystem tree even means.
for _name in ("mount", "umount", "chroot", "umount2"):
    if hasattr(os, _name):
        setattr(os, _name,
                _guard_syscall("mount", getattr(os, _name), "os.%s" % _name))

# chmod / chown are already replaced by the filesystem guards above, which
# consult the same rules; installing the syscall guard here would double-wrap
# them, so their names are recorded rather than re-wrapped.

# spawn: starting a child process. subprocess is normally refused at import
# when the policy blocks children, so the guards below are installed through a
# stub loader as well as on the real module: a script that reaches the module
# by any route still meets the filter.
_subprocess = None
try:
    import subprocess as _subprocess
except ImportError:
    _subprocess = None
class _BlockedSubprocess(object):
    # Stands in for the module when it is refused, so an attribute such as
    # `subprocess.Popen` raises the filter refusal instead of an
    # AttributeError that a script could mistake for the module being absent.
    def __getattr__(self, name):
        def _refused(*args, **kwargs):
            _syscall_refuse("spawn", "subprocess.%s" % name)
        return _refused
if _subprocess is None:
    import sys as _sys_module
    _sys_module.modules["subprocess"] = _BlockedSubprocess()
else:
    for _name in ("Popen", "run", "call", "check_call", "check_output",
                  "getoutput", "getstatusoutput"):
        if hasattr(_subprocess, _name):
            setattr(_subprocess, _name,
                    _guard_syscall("spawn", getattr(_subprocess, _name),
                                   "subprocess.%s" % _name))

# multiprocessing starts a sibling process that is not under any of these
# rules, so both its entry point and the class are covered.
_multiprocessing = None
try:
    import multiprocessing as _multiprocessing
except ImportError:
    _multiprocessing = None
if _multiprocessing is None:
    import sys as _sys_module2
    _sys_module2.modules["multiprocessing"] = _BlockedSubprocess()
else:
    if hasattr(_multiprocessing, "Process"):
        _real_process = _multiprocessing.Process
        def _guarded_process(*args, **kwargs):
            _syscall_refuse("fork", "multiprocessing.Process")
            return _real_process(*args, **kwargs)
        _multiprocessing.Process = _guarded_process
    # `multiprocessing` reaches fork through more than its Process class: the
    # pool, the context objects and the start helpers all spawn a sibling
    # process, so each entry point is guarded rather than only the class.
    for _name in ("Pool", "pool", "get_context", "set_start_method",
                  "freeze_support", "active_children"):
        if hasattr(_multiprocessing, _name):
            setattr(_multiprocessing, _name,
                    _guard_syscall("fork", getattr(_multiprocessing, _name),
                                   "multiprocessing.%s" % _name))
# Name resolution is a second way out of the sandbox: it can be used to reach a
# name without a connection, to enumerate the network, or to exfiltrate data in
# the labels of a lookup. `getaddrinfo` and its relatives are module functions
# rather than socket methods, so they are replaced here.
_real_getaddrinfo = _socket_module.getaddrinfo
_real_gethostbyname = getattr(_socket_module, "gethostbyname", None)


def _resolve_refuse(name):
    if BLOCK_DNS:
        _net_refuse(str(name), 53, "dns", "DNS is blocked")
    return True

def _guarded_getaddrinfo(host, port, *args, **kwargs):
    if isinstance(host, bytes):
        host = host.decode("utf-8", "replace")
    _resolve_refuse(host)
    return _real_getaddrinfo(host, port, *args, **kwargs)


def _guarded_gethostbyname(host):
    _resolve_refuse(host)
    return _real_gethostbyname(host)

_socket_module.getaddrinfo = _guarded_getaddrinfo
if _real_gethostbyname is not None:
    _socket_module.gethostbyname = _guarded_gethostbyname


def report_violations():
    # Print what the run was refused, so the parent can show it. Written to
    # stderr as a marked line the script cannot print over, followed by a
    # per-syscall count when the filter refused anything.
    if not VIOLATIONS:
        return
    sys.stderr.write("SDB_SANDBOX_VIOLATIONS " + str(len(VIOLATIONS)) + chr(10))
    for entry in VIOLATIONS:
        sys.stderr.write("  " + str(entry) + chr(10))
    # The syscall filter count, so the report shows how often each guarded
    # operation was reached as well as which ones were refused.
    counts = {}
    for entry in VIOLATIONS:
        kind = str(entry).split(chr(9))[0]
        if kind.startswith("sys:"):
            name = kind[4:]
            counts[name] = counts.get(name, 0) + 1
    if counts:
        summary = ", ".join("%s=%d" % (name, counts[name])
                             for name in sorted(counts))
        sys.stderr.write("SDB_SANDBOX_SYSCALLS " + summary + chr(10))


def _error_events(text):
    # The `error` events in a blob of text, one per non-empty line that names
    # an error. This is what lets a run that merely *printed* a problem be
    # counted alongside one that raised: the monitor is not limited to the
    # exceptions the interpreter itself sees.
    found = []
    for line in str(text or "").split(chr(10)):
        lowered = line.lower()
        if not line.strip():
            continue
        if lowered.startswith("traceback "):
            continue
        if " error" in lowered or lowered.startswith("error"):
            found.append(line)
    return found

def _collect():
    # The measurements and the events, from the runtime state this module has
    # been keeping all along plus the per-operation counters the guards
    # updated. `elapsed` and `cpu` are read last so they are as close to the
    # end of the run as this process can make them.
    _track_variables()
    # The behaviour traces are gathered now, at the end of the run: the samples
    # they read (memory, locks, threads) are only complete once the script has
    # stopped, and a half-finished run would otherwise be reported as a leak.
    _leak_trace()
    _concurrency_trace()
    if _recursion_overflowed():
        _recursion_trace(overflowed=True)
    events = list(EVENTS)
    measured = {
        "elapsed": _elapsed(),
        "cpu": time.process_time() - _START_CPU,
        "ram": _resource_used(),
        "io": MONITOR_IO,
        "calls": MONITOR_CALLS,
        "modules": MONITOR_MODULES,
        "syscalls": MONITOR_SYSCALLS,
        "variables": MONITOR_VARIABLES,
        "exceptions": sum(1 for kind, _d in EVENTS if kind == "exception"),
        "errors": sum(1 for kind, _d in EVENTS if kind == "error"),
        "warnings": sum(1 for kind, _d in EVENTS if kind == "warning"),
    }
    return measured, events

def report_monitor():
    # Publish the measurements and the events. The text goes to stderr as a
    # marked block (which the log budget bounds, so a huge report cannot fill
    # the pipe) and the JSON goes to the file the parent reads back, which is
    # the machine-readable copy of the same content.
    if not MONITOR:
        return
    measured, events = _collect()
    sys.stderr.write("SDB_SANDBOX_MONITOR " + str(len(measured)) + chr(10))
    for name in sorted(measured):
        sys.stderr.write("  %s\t%s" % (name, measured[name]) + chr(10))
    if events:
        sys.stderr.write("SDB_SANDBOX_EVENTS " + str(len(events)) + chr(10))
        for kind, detail in events:
            sys.stderr.write("  %s\t%s" % (kind, detail) + chr(10))
    for name, value in (("cpu", measured["cpu"]),
                        ("memory", measured["ram"])):
        budget = LIMIT_CPU if name == "cpu" else LIMIT_MEMORY
        if budget and value >= budget:
            _record_violation("limit:%s" % name, "%s >= %s" % (value, budget))
    if not MONITOR_FILE:
        return
    try:
        payload = {"monitor": measured,
                   "events": [{"kind": kind, "detail": detail}
                              for kind, detail in events],
                   "limits_hit": [kind.split(":", 1)[1] for kind in
                                  [entry.split(chr(9))[0]
                                   for entry in VIOLATIONS]
                                  if kind.startswith("limit:")]}
        import json as _json
        with _real_open(MONITOR_FILE, "w", encoding="utf-8") as handle:
            _json.dump(payload, handle, sort_keys=True)
            handle.write(chr(10))
    except Exception:
        # Writing the machine-readable copy is best effort: the same content
        # has already gone to stderr, and a run that could not write its own
        # report is not a reason to fail a run that otherwise succeeded.
        pass


def main():
    global _overflowed
    target = os.environ.get("SDB_SANDBOX_SCRIPT", "")
    if not target or not os.path.isfile(target):
        sys.stderr.write("sandbox: script not found" + chr(10))
        return 2
    try:
        # The call profiler is what turns "the stack got deep" into a number.
        # It is installed here, around the script only, so the runner's own
        # calls are not counted as the run's recursion.
        sys.setprofile(_profile_calls)
        runpy.run_path(target, run_name="__main__")
        status = 0
    except SystemExit as exc:
        code = exc.code
        if code is None:
            status = 0
        else:
            status = code if isinstance(code, int) else 1
    except BaseException:
        # An uncaught exception is a monitored event, not only an exit code:
        # the report says which one it was, and the run is marked as having
        # raised even when the exit status alone would not say so.
        formatted = traceback.format_exc().strip().split(chr(10))
        _record_event("exception", formatted[-1] if formatted else "exception")
        # A RecursionError from the interpreter is its own behaviour, and the
        # flag is what lets the collector report the depth the stack reached as
        # an overflow. The sandbox's own `setrecursionlimit` refusal raises the
        # same exception type as a way of stopping the call, so it is excluded
        # by its message: a refused limit is a bypass, not a stack overflow.
        joined = " ".join(formatted)
        if "maximum recursion depth" in joined \
                or ("RecursionError" in joined
                    and "setrecursionlimit" not in joined):
            _overflowed = True
        traceback.print_exc()
        status = 1
    try:
        sys.setprofile(None)
    except Exception:
        pass
    if LIMIT_CPU:
        # A CPU-bound loop reaches nothing that can refuse it, so the budget is
        # checked once more here, after the script has ended: `process_time`
        # carries the total the run really used, whether or not it was stopped.
        used = time.process_time() - _START_CPU
        if used >= LIMIT_CPU:
            EVENTS.append(("resource", "cpu_seconds: %.3f >= %s"
                           % (used, LIMIT_CPU)))
            _record_violation("limit:cpu_seconds", "%.3f >= %s"
                              % (used, LIMIT_CPU))
            if status == 0:
                status = 3
    # Report the refusals and the measurements last, so they cannot be lost
    # behind the script's own output however much of it there is.
    report_monitor()
    report_violations()
    return status


if __name__ == "__main__":
    sys.exit(main())
'''


# The source of the launcher used for a non-Python language. It is a Python
# program because the *runner* is Python: this file is what the sandboxed child
# executes, and it in turn starts the language's interpreter. That keeps the
# language's process inside the sandboxed process tree, with the in-process
# guards still above it, instead of handing the host a bare `node` or `cmd`.
_LAUNCHER_SOURCE = """# -*- coding: utf-8 -*-
# Sandbox launcher: start the language interpreter named in the environment.

import os
import subprocess
import sys

COMMAND = os.environ.get("SDB_SANDBOX_LAUNCH_COMMAND", "")
if not COMMAND:
    sys.stderr.write("sandbox: no launch command was given" + chr(10))
    sys.exit(2)

ARGS = COMMAND.split(chr(31))
if not os.path.isfile(ARGS[0]):
    sys.stderr.write("sandbox: interpreter not found: " + ARGS[0] + chr(10))
    sys.exit(127)

try:
    child = subprocess.Popen(
        ARGS,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
        cwd=os.environ.get("SDB_SANDBOX_WORKSPACE") or None,
    )
except OSError as exc:
    sys.stderr.write("sandbox: could not start the interpreter: "
                     + str(exc) + chr(10))
    sys.exit(126)

for line in iter(child.stdout.readline, ""):
    sys.stdout.write(line)
try:
    child.wait(timeout=float(os.environ.get("SDB_SANDBOX_LAUNCH_TIMEOUT",
                                            "0") or 0) or None)
except Exception:
    try:
        child.kill()
    except Exception:
        pass
sys.exit(child.returncode if child.returncode is not None else 1)
"""


def _now_stamp():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _stamp_for_file():
    # A timestamp safe to put in a file name on every platform (no colons).
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def _clamp_budget(key, value):
    """One resource budget forced inside its hard ceiling (0 means "off").

    The ceilings live in `tabs.sandbox_monitor` next to the names, so the UI,
    the child and this clamp all read the same number rather than each holding
    a copy that can fall out of step.
    """
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return 0
    if number <= 0:
        return 0
    ceiling = sandbox_monitor.LIMIT_CEILINGS.get(key, 0)
    return min(number, ceiling) if ceiling else number


def plan_launch(language, policy, workspace, script_path):
    """How a run in `language` should be started, and under which profile.

    Returns ``(argv, interpreter, profile, script_name, extra_env)``:

        argv         the full command, ready for Popen
        interpreter  the resolved entry from `tabs.sandbox_langs`
        profile      the security profile the run will be executed under
        script_name  the file name the script was written as (the interpreter's
                     own suffix, so Java and C++ see a name they accept)
        extra_env    the variables the launch needs (empty for Python)

    The one judgement here is the *hosting* of a weak language. C++, Batch and
    VBScript have no interpreter guard, so rather than run them bare the
    launcher hosts them: the interpreter becomes a child of a sandboxed child,
    which is the only way the in-process guards still apply to the process that
    starts them.
    """
    resolved = sandbox_langs.resolve(language)
    suffix = resolved.get("script_suffix") or ".py"
    script_name = "_sandbox_script%s" % suffix
    target = os.path.join(workspace, script_name)

    profile = sandbox_isolation.security_profile(
        getattr(policy, "security_profile", "standard"), policy, resolved)

    # Python is the sandbox's own language: it runs as the runner itself, which
    # is where the guards live, so nothing is delegated to a child here.
    if resolved.get("key") == "python":
        runner = os.path.join(workspace, "_sandbox_runner.py")
        return ([sys.executable, "-I", "-B", runner], resolved, profile,
                script_name, {"SDB_SANDBOX_SCRIPT": target})

    # Any other language: the runner's launcher starts it, so the language's
    # process is inside the sandboxed tree rather than beside it.
    launcher = os.path.join(workspace, "_sandbox_launcher.py")
    command = sandbox_langs.build_command(resolved, target, isolated=True)
    extra = {
        "SDB_SANDBOX_SCRIPT": target,
        # UNIT SEPARATOR (0x1f) joins the argv: it cannot appear in a path on
        # any platform the sandbox supports, so no quoting rule is needed and
        # a path with a space survives intact.
        "SDB_SANDBOX_LAUNCH_COMMAND": chr(31).join(command),
        "SDB_SANDBOX_LAUNCH_TIMEOUT": str(getattr(policy, "timeout", 0) or 0),
        "SDB_SANDBOX_LANGUAGE": resolved.get("key", ""),
    }
    return ([sys.executable, "-I", "-B", launcher], resolved, profile,
            script_name, extra)


def _policy_env(policy):
    """The filesystem and network rules as environment flags for the child.

    The runner applies exactly the rules the tab shows, because both sides are
    built from the same policy: the flags below are the child copy of it.
    A list-valued rule is newline-separated, which is what the runner helper
    splits back apart.
    """
    join = lambda values: chr(10).join(str(value) for value in values or ())  # noqa: E731
    return {
        "SDB_SANDBOX_READ_OUTSIDE": "1" if policy.allow_read_outside else "0",
        "SDB_SANDBOX_BLOCK_DESTRUCTIVE": "1" if policy.block_destructive else "0",
        "SDB_SANDBOX_BLOCK_RECURSIVE": "1" if policy.block_recursive else "0",
        "SDB_SANDBOX_BLOCK_SYSTEM_DIRS": "1" if policy.block_system_dirs else "0",
        "SDB_SANDBOX_PSEUDO_ROOT": "1" if policy.pseudo_root else "0",
        "SDB_SANDBOX_FS_WHITELIST": join(policy.fs_whitelist),
        "SDB_SANDBOX_FS_BLACKLIST": join(policy.fs_blacklist),
        "SDB_SANDBOX_ALLOW_HTTP": "1" if policy.allow_http else "0",
        "SDB_SANDBOX_ALLOW_HTTPS": "1" if policy.allow_https else "0",
        "SDB_SANDBOX_BLOCK_ICMP": "1" if policy.block_icmp else "0",
        "SDB_SANDBOX_BLOCK_DNS": "1" if policy.block_dns else "0",
        "SDB_SANDBOX_BLOCK_RAW": "1" if policy.block_raw_sockets else "0",
        "SDB_SANDBOX_NET_WHITELIST": join(policy.net_whitelist),
        "SDB_SANDBOX_NET_BLOCKED_PORTS": join(policy.net_blocked_ports),
        "SDB_SANDBOX_NET_BLOCKED_PROTOCOLS": join(
            policy.net_blocked_protocols),
        # The syscall filter switches, one per group.
        "SDB_SANDBOX_BLOCK_EXEC": "1" if policy.block_exec else "0",
        "SDB_SANDBOX_BLOCK_SYSTEM": "1" if policy.block_system else "0",
        "SDB_SANDBOX_BLOCK_FILES": "1" if policy.block_files else "0",
        "SDB_SANDBOX_BLOCK_NETWORK": "1" if policy.block_network else "0",
        # The runtime monitor and the resource budgets. `timeout` travels
        # separately (the parent needs it to arm its own watchdog); the rest are
        # integer budgets the child enforces from inside, where the operation
        # that would cross them can be seen and refused.
        "SDB_SANDBOX_MONITOR": "1" if policy.monitor_runtime else "0",
        "SDB_SANDBOX_LIMIT_CPU": str(policy.cpu_seconds),
        "SDB_SANDBOX_LIMIT_MEMORY": str(_megabytes(policy.memory_mb)),
        "SDB_SANDBOX_LIMIT_IO": str(policy.io_bytes),
        "SDB_SANDBOX_LIMIT_FILES": str(policy.max_files),
        "SDB_SANDBOX_LIMIT_PROCESSES": str(policy.max_processes),
        "SDB_SANDBOX_LIMIT_THREADS": str(policy.max_threads),
        "SDB_SANDBOX_LIMIT_LOG": str(policy.log_bytes),
    }


def _megabytes(value):
    """A RAM budget in MB as bytes, which is the unit the child measures in.

    The policy is written in MB because that is the only way a person can read
    it; the child compares against a byte count because that is what
    `tracemalloc` reports. The conversion happens once, here, so the two sides
    cannot disagree about what a budget means.
    """
    try:
        return max(0, int(value)) * 1024 * 1024
    except (TypeError, ValueError):
        return 0


def _summarise_syscalls(violations):
    """The syscall filter counts, from the entries the child reported.

    Each refusal is recorded as `sys:<name>`, so counting the entries gives the
    same picture the child printed without a second channel to keep in step.
    """
    refused = {}
    for entry in violations or ():
        kind = (entry or {}).get("kind", "")
        if not kind.startswith("sys:"):
            continue
        name = kind[4:]
        refused[name] = refused.get(name, 0) + 1
    return {
        "decisions": sum(refused.values()),
        "refused": sum(refused.values()),
        "by_syscall": dict(sorted(refused.items(), key=lambda item: -item[1])),
    }


def _read_violations(path):
    """The refused operations the child reported (never raises).

    Each line is `kind<TAB>detail`, so the report can group refusals by cause
    rather than only counting them.
    """
    if not path or not os.path.isfile(path):
        return []
    entries = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                text = line.strip()
                if not text:
                    continue
                kind, _tab, detail = text.partition("\t")
                entries.append({"kind": kind, "detail": detail})
    except OSError:
        return []
    return entries


def _read_monitor(path):
    """The runtime measurements the child reported (never raises).

    Read back from its own file rather than parsed out of the child's stderr:
    the stream is already carrying the script's output and the violation
    report, and a third channel is what makes the report robust against a
    script that writes something that looks like a measurement.
    """
    import json
    if not path or not os.path.isfile(path):
        return {}, []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return {}, []
    if not isinstance(payload, dict):
        return {}, []
    monitor = sandbox_monitor.normalise_monitor(payload.get("monitor") or {})
    events = [entry for entry in (payload.get("events") or [])
              if isinstance(entry, dict)]
    return monitor, events


def _events_from_violations(violations):
    """The runtime events folded out of the violations log (never raises).

    The child writes every event as `event:<kind>` and every limit as
    `limit:<name>` in the same log as the filesystem and network refusals, so
    the events survive even when the JSON payload could not be written (a full
    disk, a killed process, a script that filled the pipe). This is the second
    side of that one vocabulary.
    """
    events, limits = [], []
    for entry in violations or ():
        kind = (entry or {}).get("kind", "")
        detail = (entry or {}).get("detail", "")
        if kind.startswith("event:"):
            events.append({"kind": kind[6:], "detail": detail})
        elif kind.startswith("limit:"):
            limits.append(kind[6:])
    return events, limits


class SandboxPolicy(object):
    """The rules a sandboxed run obeys, with hard ceilings applied on top.

    The policy is a plain holder so it can be built by the UI, cloned for a
    preset or serialised into a run report without any Qt dependency.
    """

    def __init__(self, timeout=SANDBOX_DEFAULT_TIMEOUT,
                 output_cap=SANDBOX_DEFAULT_OUTPUT_CAP, allow_network=False,
                 allow_writes=True, subprocess=True, isolate_cwd=True,
                 isolate_process=True, isolate_environment=True,
                 isolate_path=True, isolate_runtime=True,
                 block_terminal=True,
                 fs_whitelist=(), fs_blacklist=(), allow_read_outside=False,
                 block_destructive=True, block_recursive=True,
                 block_system_dirs=True, whitelist_read_only=True,
                 pseudo_root=False, shadow_copy=False, snapshot_fs=False,
                 net_whitelist=(), net_allowed_ports=(), net_blocked_ports=(),
                 net_blocked_protocols=(), block_raw_sockets=True,
                 block_icmp=True, block_dns=True, block_private_ranges=True,
                 block_admin_ports=True, allow_http=False, allow_https=True,
                 monitor_network=False,
                 block_exec=True, block_system=True, block_files=True,
                 block_network=True, filter_syscalls=True,
                 monitor_runtime=True, cpu_seconds=0, memory_mb=0, io_bytes=0,
                 max_files=0, max_processes=0, max_threads=0, log_bytes=0,
                 scope_level="script", scope_name="", scope_parent="",
                 scope_identity="", persistent=False, shadow_env=False,
                 shadow_network=False, replay=False,
                 security_profile="standard", language=""):
        self.timeout = timeout
        self.output_cap = output_cap
        self.allow_network = allow_network
        self.allow_writes = allow_writes
        self.subprocess = subprocess
        self.isolate_cwd = isolate_cwd
        # Process isolation: the run gets its own process, environment, PATH,
        # working directory, runtime context, and no handle on the terminal.
        self.isolate_process = isolate_process
        self.isolate_environment = isolate_environment
        self.isolate_path = isolate_path
        self.isolate_runtime = isolate_runtime
        self.block_terminal = block_terminal
        # --- filesystem sandbox (see tabs/sandbox_fs.py) -------------------
        # The run's own workspace is always allowed. `fs_whitelist` adds more
        # roots it may read, and a whitelisted root is read-only by default so
        # a broad entry cannot become an accidental write permission.
        self.fs_whitelist = list(fs_whitelist or ())
        self.fs_blacklist = list(fs_blacklist or ())
        self.allow_read_outside = allow_read_outside
        self.block_destructive = block_destructive
        self.block_recursive = block_recursive
        self.block_system_dirs = block_system_dirs
        self.whitelist_read_only = whitelist_read_only
        self.pseudo_root = pseudo_root
        self.shadow_copy = shadow_copy
        self.snapshot_fs = snapshot_fs
        # --- network sandbox (see tabs/sandbox_net.py) ---------------------
        self.net_whitelist = list(net_whitelist or ())
        self.net_allowed_ports = list(net_allowed_ports or ())
        self.net_blocked_ports = list(net_blocked_ports or ())
        self.net_blocked_protocols = list(net_blocked_protocols or ())
        self.block_raw_sockets = block_raw_sockets
        self.block_icmp = block_icmp
        self.block_dns = block_dns
        self.block_private_ranges = block_private_ranges
        self.block_admin_ports = block_admin_ports
        self.allow_http = allow_http
        self.allow_https = allow_https
        self.monitor_network = monitor_network
        # --- syscall filter (see tabs/sandbox_syscall.py) ------------------
        # One switch per group. `mount` and `kill` are refused whatever these
        # say, because a sandboxed script has no reason to make either call.
        self.block_exec = block_exec
        self.block_system = block_system
        self.block_files = block_files
        self.block_network = block_network
        self.filter_syscalls = filter_syscalls
        # --- runtime monitor and resource limits (sandbox_monitor) ----------
        # The monitor is what the report shows; the budgets are what bounds the
        # run. A budget of zero is "no ceiling of this kind", which is the
        # default for everything but the wall-clock timeout: a sandbox that
        # refuses to run at all is not a useful default, and a ceiling that the
        # user did not ask for is one they cannot reason about.
        self.monitor_runtime = monitor_runtime
        # --- scope, persistence and shadow layers (sandbox_server) ----------
        # The scope decides how long a sandbox lives and what it reuses; the
        # shadow flags decide which resources it sees a *substitute* for rather
        # than a refusal. Both default to the safe answer: per script, not
        # persistent, no shadows.
        self.scope_level = scope_level
        self.scope_name = scope_name
        self.scope_parent = scope_parent
        self.scope_identity = scope_identity
        self.persistent = persistent
        self.shadow_env = shadow_env
        self.shadow_network = shadow_network
        self.replay = replay
        # The security profile names the isolation level (see
        # `sandbox_isolation`), and the language decides which interpreter is
        # launched. Both are part of the profile of a run, not of its wall.
        self.security_profile = security_profile
        self.language = language
        self.cpu_seconds = cpu_seconds
        self.memory_mb = memory_mb
        self.io_bytes = io_bytes
        self.max_files = max_files
        self.max_processes = max_processes
        self.max_threads = max_threads
        self.log_bytes = log_bytes

    def clamped(self):
        """Return the policy with its values forced inside the hard ceilings."""
        return SandboxPolicy(
            timeout=max(SANDBOX_MIN_TIMEOUT,
                        min(SANDBOX_MAX_TIMEOUT, int(self.timeout))),
            output_cap=max(1000, min(SANDBOX_ABSOLUTE_OUTPUT_CAP,
                                     int(self.output_cap))),
            allow_network=bool(self.allow_network),
            allow_writes=bool(self.allow_writes),
            subprocess=bool(self.subprocess),
            isolate_cwd=bool(self.isolate_cwd),
            isolate_process=bool(self.isolate_process),
            isolate_environment=bool(self.isolate_environment),
            isolate_path=bool(self.isolate_path),
            isolate_runtime=bool(self.isolate_runtime),
            block_terminal=bool(self.block_terminal),
            fs_whitelist=list(self.fs_whitelist),
            fs_blacklist=list(self.fs_blacklist),
            allow_read_outside=bool(self.allow_read_outside),
            block_destructive=bool(self.block_destructive),
            block_recursive=bool(self.block_recursive),
            block_system_dirs=bool(self.block_system_dirs),
            whitelist_read_only=bool(self.whitelist_read_only),
            pseudo_root=bool(self.pseudo_root),
            shadow_copy=bool(self.shadow_copy),
            snapshot_fs=bool(self.snapshot_fs),
            net_whitelist=list(self.net_whitelist),
            net_allowed_ports=list(self.net_allowed_ports),
            net_blocked_ports=list(self.net_blocked_ports),
            net_blocked_protocols=list(self.net_blocked_protocols),
            block_raw_sockets=bool(self.block_raw_sockets),
            block_icmp=bool(self.block_icmp),
            block_dns=bool(self.block_dns),
            block_private_ranges=bool(self.block_private_ranges),
            block_admin_ports=bool(self.block_admin_ports),
            allow_http=bool(self.allow_http),
            allow_https=bool(self.allow_https),
            monitor_network=bool(self.monitor_network),
            block_exec=bool(self.block_exec),
            block_system=bool(self.block_system),
            block_files=bool(self.block_files),
            block_network=bool(self.block_network),
            filter_syscalls=bool(self.filter_syscalls),
            monitor_runtime=bool(self.monitor_runtime),
            scope_level=self.scope_level,
            scope_name=self.scope_name,
            scope_parent=self.scope_parent,
            scope_identity=self.scope_identity,
            persistent=bool(self.persistent),
            shadow_env=bool(self.shadow_env),
            shadow_network=bool(self.shadow_network),
            replay=bool(self.replay),
            security_profile=self.security_profile,
            language=self.language,
            cpu_seconds=_clamp_budget("cpu_seconds", self.cpu_seconds),
            memory_mb=_clamp_budget("memory_mb", self.memory_mb),
            io_bytes=_clamp_budget("io_bytes", self.io_bytes),
            max_files=_clamp_budget("max_files", self.max_files),
            max_processes=_clamp_budget("max_processes", self.max_processes),
            max_threads=_clamp_budget("max_threads", self.max_threads),
            log_bytes=_clamp_budget("log_bytes", self.log_bytes),
        )

    def scope(self):
        """The scope this policy runs under (see sandbox_integration)."""
        return sandbox_integration.SandboxScope(
            self.scope_level, self.scope_name, self.scope_parent,
            self.scope_identity)

    def shadows(self, workspace=""):
        """The shadow layers this policy asks for (see sandbox_integration).

        The filesystem and snapshot layers are the ones the FS sandbox already
        had (`shadow_copy`, `snapshot_fs`); the rest are new here. One call
        gathers them all so a report states the whole substituted view rather
        than listing the layers from three different tables.
        """
        return sandbox_integration.shadow_layers(self, workspace,
                                                 {"replay": self.replay})

    def limits(self):
        """The resource budgets as a plain dict (see sandbox_monitor)."""
        values = {"timeout": self.timeout}
        for key in sandbox_monitor.LIMIT_KEYS:
            if key != "timeout":
                values[key] = getattr(self, key, 0)
        return sandbox_monitor.normalise_limits(values)

    def monitor(self):
        """The monitoring contract as a plain dict, with `timeout` folded in.

        The timeout is a limit the *parent* applies, so the child never
        measures it; it is put back here, from the run's own elapsed time, while
        the other budgets are the child's own numbers. A report that shows one
        without the other would read as if the wall-clock limit were off.
        """
        values = {key: 0 for key in sandbox_monitor.MONITOR_KEYS}
        values.update({"timeout": self.timeout, "enabled": True})
        return values
    def apply_preset(self, key):
        """Overwrite the policy from a named preset (unknown keys ignored)."""
        for preset_key, _lk, _f, settings in POLICY_PRESETS:
            if preset_key == key:
                for name, value in settings.items():
                    setattr(self, name, value)
                return True
        return False

    def to_dict(self):
        # The budgets are folded in alongside the flags: a policy that reported
        # `timeout` but not `memory_mb` would let a report show half a
        # configuration, and the half it hid would be the new one.
        values = {
            "timeout": self.timeout,
            "output_cap": self.output_cap,
            "allow_network": self.allow_network,
            "allow_writes": self.allow_writes,
            "subprocess": self.subprocess,
            "isolate_cwd": self.isolate_cwd,
            "monitor_runtime": self.monitor_runtime,
            "filter_syscalls": self.filter_syscalls,
            "scope_level": self.scope_level,
            "persistent": self.persistent,
            "shadow_env": self.shadow_env,
            "shadow_network": self.shadow_network,
            "replay": self.replay,
            "security_profile": self.security_profile,
            "language": self.language,
        }
        for key in sandbox_monitor.LIMIT_KEYS:
            if key != "timeout":
                values[key] = getattr(self, key, 0)
        return values

    def __repr__(self):
        return "SandboxPolicy(%r)" % (self.to_dict(),)


class SandboxRun(object):
    """The result of one sandboxed run (output, status, limits, report)."""

    __slots__ = ("script", "language", "ok", "status", "output", "error",
                 "truncated", "elapsed", "workspace", "timed_out",
                 "exit_code", "policy", "started", "finished", "isolation",
                 "violations", "syscalls", "monitor", "events", "limits_hit",
                 "findings", "risk", "reports", "scope", "shadows",
                 "interpreter", "profile", "language_engine")

    def __init__(self, script="", language="", policy=None):
        self.script = script
        self.language = language
        self.policy = policy or SandboxPolicy()
        # The isolation envelope the run was started with (see
        # `tabs.sandbox_isolation`), kept so the report can show it.
        self.isolation = {}
        # What the child reported being refused: the FS and network blocks it
        # hit, so the report shows what a run tried as well as what it did.
        self.violations = []
        # The syscall filter counts, keyed by syscall name, from the child.
        self.syscalls = {}
        # The runtime monitor: what the run measured (time, CPU, RAM, IO,
        # calls, modules, syscalls, variable changes), the events it produced
        # (exceptions, errors, warnings) and the budgets it reached.
        self.monitor = {}
        self.events = []
        self.limits_hit = []
        # The dangerous behaviours detected in the run (see
        # `tabs.sandbox_audit`), the risk they add up to, and the audit reports
        # built from them. Kept on the record so a report can be saved, audited
        # or re-rendered without re-running anything.
        self.findings = []
        self.risk = "clean"
        self.reports = {}
        # The scope the run happened in and the shadow layers it saw, so a
        # report can say what was reused and what was substituted rather than
        # only what was refused.
        self.scope = {}
        self.shadows = []
        # Which interpreter actually ran this script and which security
        # profile it ran under: the two things that make "a script runs in
        # isolation" a statement about *this* run rather than a claim.
        self.interpreter = {}
        self.profile = {}
        self.language_engine = ""
        self.ok = False
        self.status = "pending"
        self.output = ""
        self.error = ""
        self.truncated = 0
        self.elapsed = 0.0
        self.workspace = ""
        self.timed_out = False
        self.exit_code = None
        self.started = ""
        self.finished = ""

    def to_dict(self):
        return {
            "script": self.script,
            "language": self.language,
            "ok": self.ok,
            "status": self.status,
            "output": self.output,
            "error": self.error,
            "truncated": self.truncated,
            "elapsed": self.elapsed,
            "workspace": self.workspace,
            "timed_out": self.timed_out,
            "exit_code": self.exit_code,
            "policy": self.policy.to_dict(),
            "started": self.started,
            "finished": self.finished,
            "isolation": self.isolation,
            "violations": list(self.violations),
            "syscalls": self.syscalls,
            "monitor": self.monitor,
            "events": list(self.events),
            "limits_hit": list(self.limits_hit),
            "findings": list(self.findings),
            "risk": self.risk,
            "scope": self.scope,
            "shadows": list(self.shadows),
            "interpreter": self.interpreter,
            "profile": self.profile,
            "language_engine": self.language_engine,
        }


def scan_risky_operations(code):
    """Statically scan a script for constructs the sandbox must watch.

    Returns a list of ``(kind, line, text)`` tuples, one per match, where
    ``kind`` is one of the keys in :data:`POLICY_RULES`. This is advisory: it
    warns before a run, it does not by itself stop anything.
    """
    findings = []
    seen = set()
    for number, line in enumerate((code or "").splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        for kind, pattern in _RISKY_PATTERNS:
            if pattern.search(line):
                key = (kind, number)
                if key not in seen:
                    seen.add(key)
                    findings.append((kind, number, stripped[:120]))
    return findings


def format_run_report(run):
    """The run as an ASCII report (used by the pane and the Reports tab)."""
    lines = [
        "ScriptDB sandbox run",
        "====================",
        "Script  : %s" % run.script,
        "Language: %s" % run.language,
        "Started : %s" % run.started,
        "Finished: %s" % run.finished,
        "Status  : %s" % run.status,
        "Exit    : %s" % (run.exit_code if run.exit_code is not None else "-"),
        "Elapsed : %.2fs" % run.elapsed,
        "Workspace: %s" % run.workspace,
        "",
        "Policy",
        "------",
    ]
    for name, value in run.policy.to_dict().items():
        lines.append("  %-14s : %s" % (name, value))
    # The runtime monitor and the resource limits, so a report says what the run
    # measured and which budget it reached, not only whether it exited cleanly.
    monitor = getattr(run, "monitor", None) or {}
    limits = run.policy.limits()
    lines += ["", "  Runtime monitor", "  ---------------"]
    lines += ["  " + line for line in sandbox_monitor.format_monitor(monitor)]
    lines += ["", "  Resource limits", "  ---------------"]
    lines += ["  " + line for line in
              sandbox_monitor.format_limits(limits, monitor)]
    events = getattr(run, "events", None) or []
    lines += ["", "  Runtime events", "  --------------"]
    lines += ["  " + line for line in
              sandbox_monitor.format_events(events)]
    lines += ["", "Isolation", "---------"]
    lines += ["  " + line for line in
              sandbox_isolation.format_isolation(getattr(run, "isolation", {}))]
    # The filesystem and network rules the run actually obeyed.
    policy = getattr(run, "policy", None) or SandboxPolicy()
    lines += [""]
    lines += ["  " + line for line in
              sandbox_fs.format_fs_rules(sandbox_fs.describe(
                  getattr(run, "workspace", ""), policy))]
    lines += [""]
    lines += ["  " + line for line in
              sandbox_net.format_net_rules(sandbox_net.describe(policy))]
    # The syscall filter: the operations the run may not make at all.
    lines += [""]
    syscalls = getattr(run, "syscalls", None) or {}
    if syscalls:
        lines += ["", "  Syscall filter", "  --------------",
                  "  decisions : %d (%d refused)" % (
                      syscalls.get("decisions", 0),
                      syscalls.get("refused", 0))]
        for name, count in sorted((syscalls.get("by_syscall") or {}).items(),
                                  key=lambda item: -item[1]):
            lines.append("  %-9s : %d refusal(s)" % (name, count))
    # What the run was refused, from the violations the child reported.
    violations = getattr(run, "violations", None) or []
    if violations:
        lines += ["", "Refused operations", "------------------"]
        for entry in violations[:40]:
            if isinstance(entry, dict):
                lines.append("  %-14s %s" % (entry.get("kind", "?"),
                                              entry.get("detail", "")))
            else:
                lines.append("  " + str(entry))
        if len(violations) > 40:
            lines.append("  ... and %d more" % (len(violations) - 40))
    # The dangerous behaviours, worst first, with the evidence behind each one.
    findings = getattr(run, "findings", None) or []
    # The scope and the shadow layers: what the run reused and what it saw a
    # substitute for, which is the difference between this run and a throw-away
    # one on the same script.
    scope = getattr(run, "scope", None) or {}
    if scope:
        lines += ["", "Scope", "-----"]
        lines += ["  " + line for line in
                  sandbox_integration.SandboxScope(
                      scope.get("level", "script"), scope.get("name", ""),
                      scope.get("parent", ""),
                      scope.get("identity", "")).describe()]
    shadows = getattr(run, "shadows", None) or []
    if shadows:
        lines += [""]
        lines += ["  " + line for line in
                  sandbox_integration.format_shadows([
                      sandbox_integration.ShadowLayer(
                          item.get("key", "shadow_fs"), item.get("root", ""),
                          enabled=item.get("enabled", False))
                      for item in shadows])]
    lines += ["", "Dangerous behaviour", "-------------------",
              "  risk        : %s" % (getattr(run, "risk", "clean") or "clean"),
              "  findings    : %d" % len(findings)]
    lines += ["  " + line for line in sandbox_audit.format_findings(findings)]
    lines += ["", "Limits",
              "------",
              "  timeout     : %s" % ("HIT" if run.timed_out else "ok"),
              "  output cap  : %s" % ("HIT" if run.truncated else "ok"),
              "  truncated   : %d char(s)" % run.truncated,
              "", "Output", "------", run.output or "(no output)"]
    return "\n".join(lines)


def format_audit_report(run, fmt="ascii"):
    """The full ten-report audit for a run (ASCII or JSON)."""
    findings = getattr(run, "findings", None)
    return sandbox_audit.render_all(run, fmt, findings)


def report_keys():
    """The report keys the sandbox can render (for the UI and the tests)."""
    return list(sandbox_audit.REPORT_KEYS)


def format_single_report(run, key, fmt="ascii"):
    """One named audit report for a run (ASCII or JSON)."""
    findings = getattr(run, "findings", None)
    return sandbox_audit.render(key, run, fmt, findings)


def sandbox_capabilities(window=None):
    """Describe the sandbox for the audit / about layer.

    `window` is optional and only used to report which integrations are live
    right now: a head-less caller gets the same document with the availability
    map empty, rather than an error.
    """
    return {
        "presets": [key for key, _lk, _f, _s in POLICY_PRESETS],
        "rules": [key for key, _lk, _f, _sev in POLICY_RULES],
        "isolation_rules": [key for key, _lk, _f, _sev in ISOLATION_RULES],
        "fs_rules": [key for key, _lk, _f, _sev in FS_RULES],
        "net_rules": [key for key, _lk, _f, _sev in NET_RULES],
        "syscall_rules": [key for key, _lk, _f, _sev in SYSCALL_RULES],
        "syscalls": sandbox_syscall.syscall_capabilities(),
        "isolation": sandbox_isolation.isolation_capabilities(),
        "filesystem": sandbox_fs.fs_capabilities(),
        "network": sandbox_net.net_capabilities(),
        "monitor": sandbox_monitor.monitor_capabilities(),
        "behaviours": [key for key, _lk, _f, _sev, _d in sandbox_audit.BEHAVIOURS],
        "audit": sandbox_audit.audit_capabilities(),
        "integration": sandbox_integration.integration_capabilities(window),
        "server": sandbox_server.server_capabilities(window),
        "scopes": list(sandbox_integration.SCOPE_KEYS),
        "shadow_layers": list(sandbox_integration.SHADOW_KEYS),
        "max_timeout": SANDBOX_MAX_TIMEOUT,
        "absolute_output_cap": SANDBOX_ABSOLUTE_OUTPUT_CAP,
        "offline": True,
    }


# ---------------------------------------------------------------------------
# The head-less surface: API + CLI
#
# The tab is one front-end over the run above. These are the other two, and
# they exist so the sandbox can be used from a script, from a test and from a
# terminal with no window in sight. Nothing below imports Qt: the run itself
# never did, and keeping the front-ends beside it rather than inside the widget
# is what makes that visible.
# ---------------------------------------------------------------------------
def run_headless(code, policy=None, scope=None, script="", language="Python",
                 workspace="", keep_workspace=False):
    """Run `code` in the sandbox with no window and no tab (returns a run).

    `workspace` is the one thing a persistent caller wants and a throw-away
    one does not: when it is given, the run happens in that directory and the
    directory is *not* removed afterwards, which is what lets a server reuse a
    prepared workspace between runs.
    """
    policy = (policy or SandboxPolicy()).clamped()
    if scope is not None:
        policy.scope_level = scope.level
        policy.scope_name = scope.name
        policy.scope_parent = scope.parent
        policy.scope_identity = scope.identity
    run = SandboxRun(script=script or "(headless)", language=language,
                     policy=policy)
    run.started = _now_stamp()
    started = dt.datetime.now()

    owned = not workspace
    workspace = workspace or _headless_workspace(script or "headless")
    run.workspace = workspace
    run.scope = policy.scope().to_dict()
    run.shadows = [layer.to_dict()
                   for layer in policy.shadows(workspace)]

    cap = policy.output_cap
    violations_path = os.path.join(workspace, "_violations.log")
    monitor_path = os.path.join(workspace, "_monitor.json")

    # The language decides how the run is started (see `plan_launch`), and the
    # profile records what that launch guarantees. Both are settled before the
    # workspace is written, so a language whose interpreter is missing fails
    # with that reason rather than with a half-prepared directory.
    command, interpreter, profile, script_name, launch_env = plan_launch(
        policy.language or language, policy, workspace, "")
    runner_path = os.path.join(workspace, "_sandbox_runner.py")
    launcher_path = os.path.join(workspace, "_sandbox_launcher.py")
    script_path = os.path.join(workspace, script_name)
    run.interpreter = interpreter
    run.profile = profile
    run.language_engine = interpreter.get("key", "")
    try:
        with open(script_path, "w", encoding="utf-8") as handle:
            handle.write(code or "")
        with open(runner_path, "w", encoding="utf-8") as handle:
            handle.write(_RUNNER_SOURCE)
        if os.path.basename(command[-1]) == "_sandbox_launcher.py":
            with open(launcher_path, "w", encoding="utf-8") as handle:
                handle.write(_LAUNCHER_SOURCE)
    except OSError as exc:
        run.status = "error"
        run.error = "could not prepare the workspace: %s" % exc
        run.finished = _now_stamp()
        return run

    extra = {"SDB_SANDBOX_VIOLATIONS": violations_path,
             "SDB_SANDBOX_MONITOR_PATH": monitor_path}
    extra.update(launch_env)
    extra.update(_policy_env(policy))
    env = sandbox_isolation.build_environment(workspace, policy, extra=extra)
    run.isolation = sandbox_isolation.describe(workspace, policy)
    try:
        process = subprocess.Popen(
            command,
            cwd=workspace if policy.isolate_cwd else os.getcwd(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            env=env,
        )
    except OSError as exc:
        run.status = "error"
        run.error = "could not start sandbox process: %s" % exc
        run.finished = _now_stamp()
        run.elapsed = (dt.datetime.now() - started).total_seconds()
        _finish_headless(run, policy, violations_path, monitor_path, owned)
        return run

    # The same watchdog the tab uses, for the same reason: a silent script
    # blocks the read loop, so the kill has to come from another thread.
    killed = threading.Event()

    def _watchdog():
        if process.poll() is None:
            killed.set()
            try:
                process.kill()
            except Exception:
                pass

    killer = threading.Timer(policy.timeout, _watchdog)
    killer.daemon = True
    killer.start()
    timed_out = False
    captured, used = [], 0
    try:
        for line in iter(process.stdout.readline, ""):
            remaining = cap - used
            if remaining <= 0:
                run.truncated += len(line)
                continue
            piece = line[:remaining]
            if len(piece) < len(line):
                run.truncated += len(line) - len(piece)
            captured.append(piece)
            used += len(piece)
        try:
            process.wait(timeout=policy.timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
        if killed.is_set():
            timed_out = True
    finally:
        killer.cancel()
        if process.poll() is None:
            try:
                process.kill()
                process.wait(timeout=3)
            except Exception:
                pass
        try:
            process.stdout.close()
        except Exception:
            pass

    run.output = "".join(captured)
    run.exit_code = process.returncode
    if timed_out:
        run.timed_out = True
        run.status = "timeout"
    elif process.returncode == 0:
        run.status = "ok"
        run.ok = True
    else:
        run.status = "error"
    if run.truncated:
        run.error = "output truncated by the policy"
    run.elapsed = (dt.datetime.now() - started).total_seconds()
    run.finished = _now_stamp()
    _finish_headless(run, policy, violations_path, monitor_path, owned)
    return run


def _headless_workspace(script):
    """A private workspace for a head-less run (never raises)."""
    try:
        return tempfile.mkdtemp(prefix="sdb-sandbox-")
    except OSError:
        return os.path.abspath(".")


def _finish_headless(run, policy, violations_path, monitor_path, owned):
    """Collect a head-less run's refusals and measurements, and clean up.

    Shared by the success and failure paths so a run that never started still
    reports what it managed to write, which is the same rule the tab follows.
    """
    run.violations = _read_violations(violations_path)
    run.syscalls = _summarise_syscalls(run.violations)
    monitor, events = _read_monitor(monitor_path)
    folded, limits = _events_from_violations(run.violations)
    for entry in folded:
        if entry not in events:
            events.append(entry)
    monitor["elapsed"] = run.elapsed
    run.monitor = monitor
    run.events = events
    aliases = {"memory_bytes": "memory_mb", "memory": "memory_mb",
               "cpu": "cpu_seconds", "log": "log_bytes"}
    hit = set(sandbox_monitor.limits_hit(policy.limits(), monitor))
    for name in limits:
        hit.add(aliases.get(name, name))
    run.limits_hit = sorted(hit)
    if run.timed_out and "timeout" not in run.limits_hit:
        run.limits_hit.append("timeout")
    if run.limits_hit and run.status == "ok":
        run.status = "limited"
    run.findings = sandbox_audit.detect(run)
    run.risk = sandbox_audit.risk_level(run.findings)
    run.reports = {key: sandbox_audit.report_document(key, run, run.findings)
                   for key in sandbox_audit.REPORT_KEYS}
    if owned:
        shutil.rmtree(run.workspace, ignore_errors=True)
    return run


def sandbox_api(window=None, tab=None):
    """A machine-readable description of the sandbox (the API front-end).

    It describes the *sandbox*, not the tab: the same document comes back with
    or without a window, which is what lets a caller decide what to do with the
    sandbox before deciding whether to build one.
    """
    tab = tab or (getattr(window, "sandbox_tab", None)
                  if window is not None else None)
    live = tab is not None
    return {
        "name": "Sandbox",
        "available": True,
        "frontends": ["api", "cli", "gui"],
        "gui_open": live,
        "scopes": list(sandbox_integration.SCOPE_KEYS),
        "shadow_layers": list(sandbox_integration.SHADOW_KEYS),
        "premium": list(sandbox_server.PREMIUM_KEYS),
        "last_run": (tab.current_run().to_dict() if live
                     and hasattr(tab, "current_run")
                     and tab.current_run() is not None else None),
        "capabilities": sandbox_capabilities(window),
    }


def sandbox_cli(argv=None, window=None, out=None):
    """Head-less sandbox entry point (zero-dependency, ASCII output).

    Supported commands::

        capabilities                - the sandbox's tables, as ASCII
        api                         - the API description, as JSON
        architecture                - the architecture standard and its status
        premium                     - the premium features
        integrations                - which terminal modules are wired up
        scopes                      - the four sandbox scopes
        shadow                      - the five shadow layers
        reports                     - the ten audit reports
        run <file> [json|ascii]     - run a script file in the sandbox
        audit <file>                - run a script and print the full audit

    Returns a printable string (callers print it), so it can be used from a
    test as easily as from a terminal. It never touches Qt.
    """
    argv = list(argv or [])
    command = argv[0] if argv else "capabilities"
    argument = argv[1] if len(argv) > 1 else None

    if command in ("capabilities", "help", "-h", "--help"):
        caps = sandbox_capabilities(window)
        lines = ["ScriptDB sandbox capabilities"]
        lines.append("presets: " + ", ".join(caps["presets"]))
        lines.append("monitor: " + ", ".join(caps["monitor"]["monitor"]))
        lines.append("limits: " + ", ".join(caps["monitor"]["limits"]))
        lines.append("behaviours: " + ", ".join(caps["behaviours"]))
        lines.append("reports: " + ", ".join(caps["audit"]["reports"]))
        lines.append("scopes: " + ", ".join(caps["scopes"]))
        lines.append("shadow: " + ", ".join(caps["shadow_layers"]))
        lines.append("integrations: " + ", ".join(caps["integration"]["integrations"]))
        environment = caps["integration"]["available"]
        lines.append("wired now: " + ", ".join(
            key for key, ok in sorted(environment.items()) if ok) or "none")
        return chr(10).join(lines)

    if command == "api":
        import json as _json
        return _json.dumps(sandbox_api(window), indent=2, sort_keys=True,
                           default=str)

    if command == "architecture":
        return chr(10).join(sandbox_server.format_architecture(window))

    if command == "premium":
        return chr(10).join(sandbox_server.format_premium())

    if command == "integrations":
        rows = sandbox_integration.integration_matrix(window)
        lines = ["Sandbox integrations", "===================="]
        for row in rows:
            lines.append("%-9s %-4s %s" % (
                row["key"], "yes" if row["available"] else "no",
                row["label"]))
            lines.append("          %s" % row["exchange"])
        return chr(10).join(lines)

    if command == "scopes":
        lines = ["Sandbox scopes", "=============="]
        for key in sandbox_integration.SCOPE_KEYS:
            lines.append("%-9s %s" % (key, sandbox_integration.SCOPE_LABELS[key]))
            lines.append("          %s" % sandbox_integration.SCOPE_MEANINGS[key])
        return chr(10).join(lines)

    if command == "shadow":
        return chr(10).join(
            sandbox_integration.SHADOW_LABELS[key] for key
            in sandbox_integration.SHADOW_KEYS)

    if command == "reports":
        return chr(10).join(sandbox_audit.REPORT_KEYS)

    if command in ("run", "audit"):
        if not argument:
            return "usage: sandbox run <file> [json|ascii]"
        try:
            with open(argument, "r", encoding="utf-8", errors="replace") as h:
                code = h.read()
        except OSError as exc:
            return "cannot read %s: %s" % (argument, exc)
        run = run_headless(code, script=os.path.basename(argument))
        if command == "audit":
            fmt = "json" if argument.endswith(".json") else "ascii"
            return format_audit_report(run, fmt)
        return format_run_report(run)

    return "unknown command: %s" % command


# ---------------------------------------------------------------------------
# The Qt tab
# ---------------------------------------------------------------------------


class SandboxTab(QtWidgets.QWidget):
    """An isolated environment for running scripts with an explicit policy.

    The tab never runs anything implicitly: the user presses Run, and the run
    happens in a fresh temporary directory, inside a separate child process,
    under a hard timeout with a capped output buffer. Every control is offline
    and local.
    """

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.policy = SandboxPolicy()
        self._last_run = None
        self._running = False
        self._build_ui()
        self.refresh()

    # --------------------------------------------------------------- helpers
    def t(self, key, fallback=None, **kwargs):
        """Look a label up in the main window's translations."""
        try:
            return self.window.t(key, fallback, **kwargs)
        except Exception:
            return fallback or key

    def _base_dir(self):
        return (getattr(self.window, "base_dir", None)
                or getattr(self.window, "BASE_DIR", None)
                or os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    def _scripts(self):
        """All real script entries from the main window (never None)."""
        try:
            return list(self.window.scripts_only())
        except Exception:
            return list(getattr(self.window, "scripts", []) or [])

    def _current_script(self):
        """The script selected in the picker (or the first one)."""
        name = self.script_combo.currentText() if hasattr(
            self, "script_combo") else ""
        for script in self._scripts():
            if script.get("name") == name:
                return script
        scripts = self._scripts()
        return scripts[0] if scripts else None

    # ------------------------------------------------------------------- UI
    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)

        header = QtWidgets.QHBoxLayout()
        self.title_label = QtWidgets.QLabel(self.t("sandbox", "Sandbox"))
        self.title_label.setStyleSheet(
            "font-size: 14px; font-weight: bold; color: #8ab4f8;")
        header.addWidget(self.title_label)
        self.status_label = QtWidgets.QLabel(
            self.t("sandbox_ready", "isolated and ready"))
        self.status_label.setStyleSheet("color: #9aa0a6;")
        header.addWidget(self.status_label)
        header.addStretch()
        self.btn_refresh = QtWidgets.QPushButton(
            self.t("sandbox_refresh", "Refresh"))
        self.btn_refresh.clicked.connect(self.refresh)
        layout.addLayout(header)

        # ---- policy row: preset, limits --------------------------------
        header.addWidget(QtWidgets.QLabel(
            self.t("sandbox_preset", "Preset:")))
        self.preset_combo = QtWidgets.QComboBox()
        for key, label_key, fallback, _settings in POLICY_PRESETS:
            self.preset_combo.addItem(self.t(label_key, fallback), key)
        self.preset_combo.setCurrentIndex(1)
        self.preset_combo.currentIndexChanged.connect(self.on_preset_changed)
        header.addWidget(self.preset_combo)

        header.addWidget(QtWidgets.QLabel(
            self.t("sandbox_timeout", "Timeout (s):")))
        self.timeout_spin = QtWidgets.QSpinBox()
        self.timeout_spin.setRange(SANDBOX_MIN_TIMEOUT, SANDBOX_MAX_TIMEOUT)
        self.timeout_spin.setValue(self.policy.timeout)
        self.timeout_spin.valueChanged.connect(self._read_policy_from_ui)
        header.addWidget(self.timeout_spin)

        header.addWidget(QtWidgets.QLabel(
            self.t("sandbox_output_cap", "Output cap:")))
        self.output_spin = QtWidgets.QSpinBox()
        self.output_spin.setRange(1000, SANDBOX_ABSOLUTE_OUTPUT_CAP)
        self.output_spin.setSingleStep(10000)
        self.output_spin.setValue(self.policy.output_cap)
        self.output_spin.valueChanged.connect(self._read_policy_from_ui)
        header.addWidget(self.output_spin)

        self.chk_network = QtWidgets.QCheckBox(
            self.t("sandbox_allow_network", "Allow network"))
        self.chk_network.toggled.connect(self._read_policy_from_ui)
        header.addWidget(self.chk_network)
        self.chk_writes = QtWidgets.QCheckBox(
            self.t("sandbox_allow_writes", "Allow writes"))
        self.chk_writes.setChecked(True)
        self.chk_writes.toggled.connect(self._read_policy_from_ui)
        header.addWidget(self.chk_writes)
        self.chk_monitor = QtWidgets.QCheckBox(
            self.t("sandbox_monitor_runtime", "Monitor runtime"))
        self.chk_monitor.setChecked(True)
        self.chk_monitor.toggled.connect(self._read_policy_from_ui)
        header.addWidget(self.chk_monitor)
        header.addStretch()
        header.addWidget(self.btn_refresh)

        # ---- limits row: the resource budgets ---------------------------
        # One spin box per budget. Zero means "no ceiling of this kind", which
        # is what the default policy ships: a budget the user did not ask for
        # is one they cannot reason about.
        limits_row = QtWidgets.QHBoxLayout()
        limits_row.addWidget(QtWidgets.QLabel(
            self.t("sandbox_limits", "Limits:")))
        self.limit_spins = {}
        for key, label_key, fallback, _unit in LIMIT_RULES:
            limits_row.addWidget(QtWidgets.QLabel(
                self.t(label_key, fallback) + ":"))
            spin = QtWidgets.QSpinBox()
            spin.setRange(0, 10 ** 9)
            spin.setValue(int(getattr(self.policy, key, 0) or 0))
            spin.setToolTip(self.t(
                "sandbox_limit_zero", "0 = no ceiling of this kind"))
            spin.valueChanged.connect(self._read_policy_from_ui)
            limits_row.addWidget(spin)
            self.limit_spins[key] = spin
        limits_row.addStretch()
        layout.addLayout(limits_row)

        # ---- run row: script picker + buttons --------------------------
        run_row = QtWidgets.QHBoxLayout()
        run_row.addWidget(QtWidgets.QLabel(
            self.t("sandbox_script", "Script:")))
        self.script_combo = QtWidgets.QComboBox()
        self.script_combo.currentIndexChanged.connect(self.on_script_changed)
        run_row.addWidget(self.script_combo, 3)

        self.btn_run = QtWidgets.QPushButton(
            self.t("sandbox_run", "Run in sandbox"))
        self.btn_run.clicked.connect(self.run_sandboxed)
        run_row.addWidget(self.btn_run)
        self.btn_scan = QtWidgets.QPushButton(
            self.t("sandbox_scan", "Scan policy"))
        self.btn_scan.clicked.connect(self.scan_selected)
        run_row.addWidget(self.btn_scan)
        self.btn_save_report = QtWidgets.QPushButton(
            self.t("sandbox_save_report", "Save report"))
        self.btn_save_report.clicked.connect(self.save_report)
        run_row.addWidget(self.btn_save_report)
        layout.addLayout(run_row)

        # ---- audit row: which report, which format, and a way to save it ---
        audit_row = QtWidgets.QHBoxLayout()
        audit_row.addWidget(QtWidgets.QLabel(
            self.t("sandbox_report", "Report:")))
        self.report_combo = QtWidgets.QComboBox()
        for key, label_key, fallback in REPORT_RULES:
            self.report_combo.addItem(self.t(label_key, fallback), key)
        audit_row.addWidget(self.report_combo)
        audit_row.addWidget(QtWidgets.QLabel(
            self.t("sandbox_format", "Format:")))
        self.format_combo = QtWidgets.QComboBox()
        for fmt in sandbox_audit.FORMATS:
            self.format_combo.addItem(fmt.upper(), fmt)
        audit_row.addWidget(self.format_combo)
        self.btn_show_report = QtWidgets.QPushButton(
            self.t("sandbox_show_report", "Show report"))
        self.btn_show_report.clicked.connect(self.show_report)
        audit_row.addWidget(self.btn_show_report)
        self.btn_save_audit = QtWidgets.QPushButton(
            self.t("sandbox_save_audit", "Save audit"))
        self.btn_save_audit.clicked.connect(self.save_audit)
        audit_row.addWidget(self.btn_save_audit)
        audit_row.addStretch()
        layout.addLayout(audit_row)

        # ---- scope row: how long this sandbox lives and what it reuses -----
        # The scope is the feature that decides whether a run reuses anything:
        # per script reuses nothing, per project shares a workspace and an
        # environment. The row shows it plainly because it changes what a run
        # can observe, and a user should not have to read a report to know.
        scope_row = QtWidgets.QHBoxLayout()
        scope_row.addWidget(QtWidgets.QLabel(
            self.t("sandbox_scope", "Scope:")))
        self.scope_combo = QtWidgets.QComboBox()
        for key, label_key, fallback, _reuse in SCOPE_RULES:
            self.scope_combo.addItem(self.t(label_key, fallback), key)
        self.scope_combo.currentIndexChanged.connect(self._read_policy_from_ui)
        scope_row.addWidget(self.scope_combo)
        self.scope_name_edit = QtWidgets.QLineEdit()
        self.scope_name_edit.setPlaceholderText(
            self.t("sandbox_scope_name", "name (module, project, session)"))
        self.scope_name_edit.textChanged.connect(self._read_policy_from_ui)
        scope_row.addWidget(self.scope_name_edit, 2)
        self.chk_persistent = QtWidgets.QCheckBox(
            self.t("sandbox_persistent", "Persistent"))
        self.chk_persistent.setToolTip(self.t(
            "sandbox_persistent_hint",
            "keep the workspace between runs (the scope decides what is reused)"))
        self.chk_persistent.toggled.connect(self._read_policy_from_ui)
        scope_row.addWidget(self.chk_persistent)
        self.chk_shadow_env = QtWidgets.QCheckBox(
            self.t("sandbox_shadow_env_chk", "Shadow ENV"))
        self.chk_shadow_env.setToolTip(self.t(
            "sandbox_shadow_env_hint",
            "give the run its own environment instead of refusing it one"))
        self.chk_shadow_env.toggled.connect(self._read_policy_from_ui)
        scope_row.addWidget(self.chk_shadow_env)
        self.chk_shadow_net = QtWidgets.QCheckBox(
            self.t("sandbox_shadow_net_chk", "Shadow network"))
        self.chk_shadow_net.setToolTip(self.t(
            "sandbox_shadow_net_hint",
            "answer connections locally instead of refusing them"))
        self.chk_shadow_net.toggled.connect(self._read_policy_from_ui)
        scope_row.addWidget(self.chk_shadow_net)
        self.btn_integrations = QtWidgets.QPushButton(
            self.t("sandbox_integrations", "Integrations"))
        self.btn_integrations.clicked.connect(self.show_integrations)
        scope_row.addWidget(self.btn_integrations)
        self.btn_premium = QtWidgets.QPushButton(
            self.t("sandbox_premium", "Premium"))
        self.btn_premium.clicked.connect(self.show_premium)
        scope_row.addWidget(self.btn_premium)
        scope_row.addStretch()
        layout.addLayout(scope_row)

        # ---- split: policy rules | output ------------------------------
        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)

        rules_side = QtWidgets.QWidget()
        rules_layout = QtWidgets.QVBoxLayout(rules_side)
        rules_layout.addWidget(QtWidgets.QLabel(
            self.t("sandbox_rules", "Policy rules")))
        self.rules_tree = QtWidgets.QTreeWidget()
        self.rules_tree.setColumnCount(3)
        self.rules_tree.setHeaderLabels([
            self.t("sandbox_col_rule", "Rule"),
            self.t("sandbox_col_state", "State"),
            self.t("sandbox_col_detail", "Detail"),
        ])
        self.rules_tree.setAlternatingRowColors(True)
        self.rules_tree.setFont(QFont(SANDBOX_FONT, SANDBOX_FONT_SIZE))
        rules_layout.addWidget(self.rules_tree, 1)

        rules_layout.addWidget(QtWidgets.QLabel(
            self.t("sandbox_findings", "Scan findings")))
        self.findings_view = QtWidgets.QPlainTextEdit()
        self.findings_view.setReadOnly(True)
        self.findings_view.setFont(QFont(SANDBOX_FONT, SANDBOX_FONT_SIZE))
        panel_line = QtGui.QFontMetrics(
            QFont(SANDBOX_FONT, SANDBOX_FONT_SIZE)).lineSpacing()
        panel_height = 4 * panel_line + 8
        self.findings_view.setFixedHeight(panel_height)
        rules_layout.addWidget(self.findings_view)

        # The dangerous behaviours of the last run, so a run can be judged
        # without opening its full report.
        rules_layout.addWidget(QtWidgets.QLabel(
            self.t("sandbox_behaviour_findings", "Dangerous behaviour")))
        self.behaviour_view = QtWidgets.QPlainTextEdit()
        self.behaviour_view.setReadOnly(True)
        self.behaviour_view.setFont(QFont(SANDBOX_FONT, SANDBOX_FONT_SIZE))
        self.behaviour_view.setFixedHeight(panel_height)
        self.behaviour_view.setPlainText(self.t(
            "sandbox_no_run_yet", "(run something to detect behaviour)"))
        rules_layout.addWidget(self.behaviour_view)
        self.rules_tree.setMinimumHeight(
            self.rules_tree.minimumSizeHint().height() + 2 * panel_line)
        splitter.addWidget(rules_side)

        # The path and host lists are editable here, because a whitelist is
        # only useful if a rule can be typed: one entry per line, applied to
        # the policy on every run.
        lists_layout = QtWidgets.QHBoxLayout()
        self.list_editors = {}
        for key, title_key, fallback in (
                ("fs_whitelist", "sandbox_list_fs_whitelist",
                 "Allowed paths (one per line)"),
                ("net_whitelist", "sandbox_list_net_whitelist",
                 "Allowed hosts (one per line)")):
            box = QtWidgets.QGroupBox(self.t(title_key, fallback))
            box_layout = QtWidgets.QVBoxLayout(box)
            editor = QtWidgets.QPlainTextEdit()
            editor.setFont(QFont(SANDBOX_FONT, SANDBOX_FONT_SIZE))
            editor.setMaximumHeight(70)
            editor.textChanged.connect(self._read_lists_from_ui)
            box_layout.addWidget(editor)
            self.list_editors[key] = editor
            lists_layout.addWidget(box, 1)
        layout.addLayout(lists_layout)

        output_side = QtWidgets.QWidget()
        output_layout = QtWidgets.QVBoxLayout(output_side)
        self.result_label = QtWidgets.QLabel("")
        self.result_label.setStyleSheet("color: #9aa0a6;")
        output_layout.addWidget(self.result_label)
        self.output_view = QtWidgets.QPlainTextEdit()
        self.output_view.setReadOnly(True)
        self.output_view.setFont(QFont(SANDBOX_FONT, SANDBOX_FONT_SIZE))
        output_layout.addWidget(self.output_view, 1)
        splitter.addWidget(output_side)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        layout.addWidget(splitter, 1)

        self.status = QtWidgets.QLabel("")
        self.status.setStyleSheet("color: #9aa0a6;")
        layout.addWidget(self.status)

    # ------------------------------------------------------------- policy
    def _read_lists_from_ui(self, *_):
        """Copy the path and host lists from the editors into the policy."""
        for key, editor in getattr(self, "list_editors", {}).items():
            entries = [line.strip() for line in
                       editor.toPlainText().splitlines() if line.strip()]
            setattr(self.policy, key, entries)
        return self.policy

    def _write_lists_to_ui(self):
        """Push the policy path and host lists back into the editors."""
        for key, editor in getattr(self, "list_editors", {}).items():
            entries = getattr(self.policy, key, []) or []
            text = chr(10).join(str(entry) for entry in entries)
            if editor.toPlainText() != text:
                editor.blockSignals(True)
                editor.setPlainText(text)
                editor.blockSignals(False)

    def _read_policy_from_ui(self, *_):
        """Copy the current widget values into the policy (clamped)."""
        self.policy.timeout = self.timeout_spin.value()
        self.policy.output_cap = self.output_spin.value()
        self.policy.allow_network = self.chk_network.isChecked()
        self.policy.allow_writes = self.chk_writes.isChecked()
        self.policy.monitor_runtime = self.chk_monitor.isChecked()
        # The resource budgets, one spin box each. A value of zero is the
        # "no ceiling" setting rather than a budget of nothing.
        for key, spin in getattr(self, "limit_spins", {}).items():
            setattr(self.policy, key, spin.value())
        # The scope and the shadow switches: how long the sandbox lives and
        # which resources it sees a substitute for rather than a refusal.
        self.policy.scope_level = (
            self.scope_combo.currentData() if hasattr(self, "scope_combo")
            else "script")
        self.policy.scope_name = (
            self.scope_name_edit.text() if hasattr(self, "scope_name_edit")
            else "")
        self.policy.persistent = bool(
            self.chk_persistent.isChecked()
            if hasattr(self, "chk_persistent") else False)
        self.policy.shadow_env = bool(
            self.chk_shadow_env.isChecked()
            if hasattr(self, "chk_shadow_env") else False)
        self.policy.shadow_network = bool(
            self.chk_shadow_net.isChecked()
            if hasattr(self, "chk_shadow_net") else False)
        self._read_lists_from_ui()
        self.policy = self.policy.clamped()
        self._write_limits_to_ui()
        self.refresh_rules()
        return self.policy

    def _write_policy_to_ui(self):
        """Push the policy back into the widgets (after a preset change)."""
        for widget, value in ((self.timeout_spin, self.policy.timeout),
                              (self.output_spin, self.policy.output_cap)):
            widget.blockSignals(True)
            widget.setValue(value)
            widget.blockSignals(False)
        for widget, value in ((self.chk_network, self.policy.allow_network),
                              (self.chk_writes, self.policy.allow_writes),
                              (self.chk_monitor, self.policy.monitor_runtime)):
            widget.blockSignals(True)
            widget.setChecked(value)
            widget.blockSignals(False)
        self._write_limits_to_ui()
        self._write_scope_to_ui()

    def _write_scope_to_ui(self):
        # Push the scope and shadow settings back into their controls, so the
        # row always shows the policy that will actually be applied rather than
        # one a preset has just replaced.
        for widget, value in ((self.chk_persistent, self.policy.persistent),
                              (self.chk_shadow_env, self.policy.shadow_env),
                              (self.chk_shadow_net,
                               self.policy.shadow_network)):
            widget.blockSignals(True)
            widget.setChecked(bool(value))
            widget.blockSignals(False)
        if hasattr(self, "scope_combo"):
            index = self.scope_combo.findData(
                getattr(self.policy, "scope_level", "script"))
            if index >= 0:
                self.scope_combo.blockSignals(True)
                self.scope_combo.setCurrentIndex(index)
                self.scope_combo.blockSignals(False)

    def _write_limits_to_ui(self):
        # A budget is clamped by SandboxPolicy.clamped, so the box is corrected
        # after every read: a limit control must never show a number the run is
        # not actually bounded by.
        for key, spin in getattr(self, "limit_spins", {}).items():
            spin.blockSignals(True)
            spin.setValue(int(getattr(self.policy, key, 0) or 0))
            spin.blockSignals(False)

    def on_preset_changed(self, *_):
        """Apply the selected preset to the policy and the controls."""
        key = self.preset_combo.currentData()
        self.policy.apply_preset(key)
        self.policy = self.policy.clamped()
        self._write_policy_to_ui()
        self.refresh_rules()
        self.status.setText(self.t(
            "sandbox_preset_set", "preset applied: {name}").format(
            name=self.preset_combo.currentText()))

    def refresh_rules(self):
        """Show every policy rule with its current state."""
        tree = getattr(self, "rules_tree", None)
        if tree is None:
            return
        tree.clear()
        state = {
            "network": ("blocked" if not self.policy.allow_network
                        else "allowed"),
            "writes": ("sandbox only" if self.policy.allow_writes
                       else "blocked"),
            "subprocess": ("allowed" if self.policy.subprocess else "blocked"),
            "timeout": "%ds" % self.policy.timeout,
            "output": "%d chars" % self.policy.output_cap,
            "cwd": "isolated" if self.policy.isolate_cwd else "shared",
            "monitor": "on" if self.policy.monitor_runtime else "off",
        }
        colours = {"blocked": "#ff9a9a", "allowed": "#9be9a8",
                   "sandbox": "#f7c66d", "isolated": "#8ab4f8",
                   "enforced": "#9be9a8", "scrubbed": "#9be9a8",
                   "own": "#9be9a8", "replaced": "#8ab4f8"}
        for key, label_key, fallback, _severity in POLICY_RULES:
            value = state.get(key, "-")
            item = QtWidgets.QTreeWidgetItem([
                self.t(label_key, fallback), value, key])
            colour = colours.get(value.split(" ")[0])
            if colour:
                item.setForeground(1, QtGui.QColor(colour))
            tree.addTopLevelItem(item)

        # The isolation envelope gets its own group, so it is visible as the
        # property that makes this a sandbox rather than a run button.
        group = QtWidgets.QTreeWidgetItem([
            self.t("sandbox_iso_group", "Process isolation"),
            "%d/%d" % (sum(1 for key, _lk, _f, _sev in ISOLATION_RULES
                           if getattr(self.policy, key, True)),
                       len(ISOLATION_RULES)),
            "isolation"])
        group.setForeground(0, QtGui.QColor("#8ab4f8"))
        group.setForeground(1, QtGui.QColor("#8ab4f8"))
        for key, label_key, fallback, severity in ISOLATION_RULES:
            enabled = bool(getattr(self.policy, key, True))
            child = QtWidgets.QTreeWidgetItem([
                self.t(label_key, fallback),
                severity if enabled else "off", key])
            child.setForeground(
                1, QtGui.QColor("#9be9a8" if enabled else "#ff9a9a"))
            group.addChild(child)
        tree.addTopLevelItem(group)
        group.setExpanded(True)

        # The filesystem and network sandboxes get their own groups too, so the
        # rules a run obeys are visible rather than implied by the code.
        self._add_rule_group(tree, "sandbox_fs_group", "Filesystem sandbox",
                             FS_RULES, self._fs_rule_state)
        self._add_rule_group(tree, "sandbox_net_group", "Network sandbox",
                             NET_RULES, self._net_rule_state)
        self._add_rule_group(tree, "sandbox_sys_group", "Syscall filter",
                             SYSCALL_RULES, self._syscall_rule_state)
        # The runtime monitor and the resource limits, so what a run is watched
        # for and what it is bounded by are visible next to the rules that
        # decide what it may reach.
        self._add_rule_group(tree, "sandbox_mon_group", "Runtime monitor",
                             MONITOR_RULES, self._monitor_rule_state)
        self._add_rule_group(tree, "sandbox_limit_group", "Resource limits",
                             LIMIT_RULES, self._limit_rule_state)
        # What the sandbox is wired to and what it can be configured to do:
        # the integration is a live status, the scopes and shadows are the
        # policy's current state. All three come from the modules that own the
        # knowledge, so the view cannot drift from the behaviour.
        self._add_rule_group(tree, "sandbox_int_group", "Terminal integration",
                             INTEGRATION_RULES, self._integration_rule_state)
        self._add_rule_group(tree, "sandbox_scope_group", "Sandbox scope",
                             SCOPE_RULES, self._scope_rule_state)
        self._add_rule_group(tree, "sandbox_shadow_group", "Shadow layers",
                             SHADOW_RULES, self._shadow_rule_state)

        for column in range(tree.columnCount()):
            tree.resizeColumnToContents(column)

    def _add_rule_group(self, tree, title_key, fallback, rules, state_of):
        """Add one collapsible group of rules."""
        group = QtWidgets.QTreeWidgetItem([
            self.t(title_key, fallback),
            "%d rule(s)" % len(rules), "group"])
        group.setForeground(0, QtGui.QColor("#8ab4f8"))
        group.setForeground(1, QtGui.QColor("#8ab4f8"))
        for key, label_key, label_fallback, _severity in rules:
            text, colour = state_of(key)
            child = QtWidgets.QTreeWidgetItem([
                self.t(label_key, label_fallback), text, key])
            child.setForeground(1, QtGui.QColor(colour))
            group.addChild(child)
        tree.addTopLevelItem(group)
        group.setExpanded(True)
        return group
    def _fs_rule_state(self, key):
        """The shown state of one filesystem rule: ``(text, colour)``."""
        value = getattr(self.policy, key, None)
        blocked = "#9be9a8"
        open_colour = "#ff9a9a"
        off_colour = "#9aa0a6"
        if isinstance(value, (list, tuple)):
            return "%d entr(ies)" % len(value), (
                blocked if value else off_colour)
        if key in ("allow_read_outside", "allow_writes"):
            return ("allowed" if value else "blocked",
                    open_colour if value else blocked)
        return (("on" if value else "off"),
                blocked if value else off_colour)

    def _syscall_rule_state(self, key):
        """The shown state of one syscall group: ``(text, colour)``."""
        enabled = bool(getattr(self.policy, key, True))
        return (("blocked" if enabled else "allowed"),
                "#9be9a8" if enabled else "#ff9a9a")

    def _monitor_rule_state(self, key):
        """The shown state of one monitored item: ``(text, colour)``.

        A monitoring item has no on/off state of its own: it is either measured
        during the run or reported as zero. What the column shows is the unit
        the number will be in, which is what a reader needs in order to make
        sense of the value when it appears in the report.
        """
        enabled = bool(getattr(self.policy, "monitor_runtime", True))
        unit = ""
        for mon_key, _lk, _f, mon_unit in sandbox_monitor.MONITOR_ITEMS:
            if mon_key == key:
                unit = mon_unit
                break
        return (("tracked (%s)" % unit) if enabled else "off",
                "#9be9a8" if enabled else "#9aa0a6")

    def _limit_rule_state(self, key):
        """The shown state of one resource limit: ``(text, colour)``.

        A limit of zero is "no ceiling of this kind", shown as `off` rather
        than as `0`: the difference between a run that may allocate anything
        and one bounded by nothing is a decision, not a number.
        """
        value = getattr(self.policy, key, 0)
        try:
            number = int(value or 0)
        except (TypeError, ValueError):
            number = 0
        if number <= 0:
            return "off", "#9aa0a6"
        unit = ""
        for limit_key, _lk, _f, limit_unit, _cap in sandbox_monitor.LIMIT_ITEMS:
            if limit_key == key:
                unit = limit_unit
                break
        return "%d %s" % (number, unit), "#9be9a8"

    def _behaviour_rule_state(self, key):
        # The shown state of one detector, coloured by how bad the behaviour it
        # looks for would be. The state is what the detector *watches for*
        # rather than a per-run result: the result is in the findings pane, and
        # mixing the two in one column would make the policy view change shape
        # every time a script ran.
        severity = sandbox_audit.BEHAVIOUR_SEVERITY.get(key, "medium")
        colours = {"critical": "#ff9a9a", "high": "#f7c66d",
                   "medium": "#8ab4f8", "low": "#9aa0a6"}
        return severity, colours.get(severity, "#9aa0a6")


    def _integration_rule_state(self, key):
        """The shown state of one terminal bridge: ``(text, colour)``.

        The column shows whether the module can be reached *right now*, which
        is what makes the integration group a status report rather than a
        description of intent.
        """
        available = sandbox_integration.integration_capabilities(
            self.window).get("available", {}).get(key, False)
        return (("wired" if available else "not open"),
                "#9be9a8" if available else "#9aa0a6")

    def _scope_rule_state(self, key):
        """The shown state of one scope: ``(text, colour)``."""
        selected = getattr(self.policy, "scope_level", "script") == key
        reuses = sandbox_integration.SandboxScope(key).reuses()
        text = "selected" if selected else "available"
        if reuses:
            text += " (+%d reused)" % len(reuses)
        return (text, "#8ab4f8" if selected else "#9aa0a6")

    def _shadow_rule_state(self, key):
        """The shown state of one shadow layer: ``(text, colour)``.

        A layer that is off means the run is *refused* the resource rather
        than shown a substitute for it, which is the distinction the colour
        carries.
        """
        enabled = bool(getattr(self.policy, key, False))
        return (("shadowing" if enabled else "refused"),
                "#9be9a8" if enabled else "#f7c66d")


    def _net_rule_state(self, key):
        """The shown state of one network rule: ``(text, colour)``."""
        value = getattr(self.policy, key, None)
        blocked = "#9be9a8"
        open_colour = "#ff9a9a"
        off_colour = "#9aa0a6"
        if isinstance(value, (list, tuple)):
            return "%d entr(ies)" % len(value), (
                blocked if value else off_colour)
        if key in ("allow_network", "allow_http", "allow_https"):
            return ("allowed" if value else "blocked",
                    open_colour if value else blocked)
        return (("on" if value else "off"),
                blocked if value else off_colour)

    # ---------------------------------------------------------- repository
    def refresh(self):
        """Reload the script list and repaint the policy view."""
        self._reload_scripts()
        self._write_lists_to_ui()
        self.refresh_rules()
        self.status.setText(self.t("sandbox_ready", "isolated and ready"))

    def _reload_scripts(self):
        combo = getattr(self, "script_combo", None)
        if combo is None:
            return
        current = combo.currentText()
        combo.blockSignals(True)
        combo.clear()
        for script in self._scripts():
            combo.addItem(script.get("name", "(unnamed)"))
        index = combo.findText(current)
        if index >= 0:
            combo.setCurrentIndex(index)
        combo.blockSignals(False)

    def on_script_changed(self, *_):
        self.findings_view.clear()

    def current_run(self):
        return self._last_run

    # -------------------------------------------------------------- scanning
    def scan_selected(self):
        """Statically scan the selected script for risky constructs."""
        script = self._current_script()
        if script is None:
            self.findings_view.setPlainText(self.t(
                "sandbox_no_script", "(no script selected)"))
            return []
        findings = scan_risky_operations(script.get("code", ""))
        if not findings:
            self.findings_view.setPlainText(self.t(
                "sandbox_no_findings", "(no risky constructs found)"))
        else:
            lines = ["line %-4s %-11s %s" % (n, kind, text)
                     for kind, n, text in findings]
            self.findings_view.setPlainText("\n".join(lines))
        self.status.setText(self.t(
            "sandbox_scanned", "scan: {count} finding(s)").format(
            count=len(findings)))
        return findings

    # ---------------------------------------------------------------- running
    def run_sandboxed(self):
        """Run the selected script in an isolated child-process sandbox.

        The run is bounded by the policy timeout, so the UI is updated on the
        GUI thread while the child is killed if it overruns its time budget.
        """
        if self._running:
            return None
        script = self._current_script()
        if script is None:
            self.status.setText(self.t("sandbox_no_script",
                                       "(no script selected)"))
            return None
        self._read_policy_from_ui()
        self._running = True
        self.btn_run.setEnabled(False)
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.BusyCursor)
        try:
            run = self._execute(script.get("code", ""),
                                script.get("category", "Python"))
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
            self.btn_run.setEnabled(True)
            self._running = False
        self._last_run = run
        self._show_run(run)
        self.notify_audit(run)
        return run

    def _make_workspace(self):
        """A private temporary directory for the run (removed afterwards)."""
        return tempfile.mkdtemp(prefix="sdb-sandbox-")

    def _execute(self, code, language):
        """Run `code` in an isolated child process, bounded by the policy.

        A child process (rather than a thread) is used on purpose: it can be
        killed outright when the timeout elapses, and it cannot corrupt the
        parent's stdout, working directory or memory however the script
        misbehaves. The runner applies the same import / write guards inside
        the child, and the workspace is removed when the run ends.
        """
        policy = self.policy.clamped()
        run = SandboxRun(script=self.script_combo.currentText() if hasattr(
            self, "script_combo") else "", language=language, policy=policy)
        run.started = _now_stamp()
        started = dt.datetime.now()
        workspace = self._make_workspace()
        run.workspace = workspace
        cap = policy.output_cap
        # The language decides the launch (see `plan_launch`); the profile it
        # returns is what the report shows for this run.
        command, interpreter, profile, script_name, launch_env = plan_launch(
            policy.language or language, policy, workspace, "")
        runner_path = os.path.join(workspace, "_sandbox_runner.py")
        launcher_path = os.path.join(workspace, "_sandbox_launcher.py")
        script_path = os.path.join(workspace, script_name)
        run.interpreter = interpreter
        run.profile = profile
        run.language_engine = interpreter.get("key", "")
        with open(script_path, "w", encoding="utf-8") as handle:
            handle.write(code or "")
        with open(runner_path, "w", encoding="utf-8") as handle:
            handle.write(_RUNNER_SOURCE)
        if os.path.basename(command[-1]) == "_sandbox_launcher.py":
            with open(launcher_path, "w", encoding="utf-8") as handle:
                handle.write(_LAUNCHER_SOURCE)
        # The isolation envelope: a scrubbed environment, a private PATH, a
        # private runtime context and the terminal guards, all built by
        # `sandbox_isolation` so the child never sees the terminal's state.
        # The child reports every refusal to this file, which the parent reads
        # back after the run so the report can show what was blocked.
        violations_path = os.path.join(workspace, "_violations.log")
        # The runtime monitor writes its measurements to its own file, so they
        # cannot be confused with the script's output or with a refusal.
        monitor_path = os.path.join(workspace, "_monitor.json")
        extra = {"SDB_SANDBOX_VIOLATIONS": violations_path,
                 "SDB_SANDBOX_MONITOR_PATH": monitor_path}
        extra.update(launch_env)
        # The filesystem and network rules travel to the child as flags, so the
        # guards inside the run apply exactly the policy the tab shows.
        extra.update(_policy_env(policy))
        env = sandbox_isolation.build_environment(workspace, policy,
                                                  extra=extra)
        run.isolation = sandbox_isolation.describe(workspace, policy)
        # The scope and the shadow layers travel with the record, so a report
        # can say what this run reused and what it saw a substitute for --
        # the two things that make it different from a throw-away run.
        run.scope = policy.scope().to_dict()
        run.shadows = [layer.to_dict()
                       for layer in policy.shadows(workspace)]

        # `command` comes from `plan_launch`: for Python `-I` drops
        # sys.path[0], PYTHONPATH, PYTHONSTARTUP and the per-user site
        # directory (so the child cannot pick up the terminal's imports even by
        # accident) and `-B` stops it writing byte-code; for any other language
        # the launcher starts the interpreter with its own guard flags.

        process = None
        try:
            process = subprocess.Popen(
                command,
                cwd=workspace if policy.isolate_cwd else self._base_dir(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                env=env,
            )
        except OSError as exc:
            run.status = "error"
            run.ok = False
            run.error = "could not start sandbox process: %s" % exc
            run.finished = _now_stamp()
            run.elapsed = (dt.datetime.now() - started).total_seconds()
            shutil.rmtree(workspace, ignore_errors=True)
            return run

        # A watchdog kills the child when the budget runs out. It must run
        # with the parent blocked reading the pipe, otherwise a silent script
        # (one that never prints and never exits) would block readline()
        # forever and the timeout would never fire.
        killed_by_watchdog = threading.Event()

        def _watchdog():
            if process.poll() is None:
                killed_by_watchdog.set()
                try:
                    process.kill()
                except Exception:
                    pass
        killer = threading.Timer(policy.timeout, _watchdog)
        killer.daemon = True
        killer.start()
        timed_out = False
        captured = []
        used = 0
        try:
            for line in iter(process.stdout.readline, ""):
                remaining = cap - used
                if remaining <= 0:
                    run.truncated += len(line)
                    continue
                piece = line[:remaining]
                if len(piece) < len(line):
                    run.truncated += len(line) - len(piece)
                captured.append(piece)
                used += len(piece)
            try:
                process.wait(timeout=policy.timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
            if killed_by_watchdog.is_set():
                # The watchdog fired: the run exceeded its budget.
                timed_out = True
        finally:
            killer.cancel()
            if process.poll() is None:
                try:
                    process.kill()
                    process.wait(timeout=3)
                except Exception:
                    pass
            try:
                process.stdout.close()
            except Exception:
                pass
        run.output = "".join(captured)
        run.exit_code = process.returncode
        if timed_out:
            run.timed_out = True
            run.status = "timeout"
            run.ok = False
        elif process.returncode == 0:
            run.status = "ok"
            run.ok = True
        else:
            run.status = "error"
            run.ok = False
        if run.truncated:
            run.error = "output truncated by the policy"
        run.elapsed = (dt.datetime.now() - started).total_seconds()
        run.finished = _now_stamp()
        run.violations = _read_violations(violations_path)
        run.syscalls = _summarise_syscalls(run.violations)
        # --- the runtime monitor and the budgets that were reached ----------
        # The child's own measurements first, then the two things only the
        # parent knows: the wall-clock time the run really took (the child can
        # not be asked, it is gone) and whether its own watchdog had to kill it.
        monitor, events = _read_monitor(monitor_path)
        event_fold, limit_fold = _events_from_violations(run.violations)
        for entry in event_fold:
            if not any(existing.get("kind") == entry.get("kind")
                       and existing.get("detail") == entry.get("detail")
                       for existing in events):
                events.append(entry)
        # The wall-clock time is the parent's own reading, not the child's: the
        # child measured up to the moment it reported, and the difference is
        # exactly the part of the run that happened after it looked.
        monitor["elapsed"] = run.elapsed
        run.monitor = monitor
        run.events = events
        # What the child refused: its own names for the budget it hit, folded
        # back onto the policy keys so a report says `memory_mb` (the budget
        # the user set) rather than `memory_bytes` (the unit it enforces in).
        aliases = {"memory_bytes": "memory_mb", "memory": "memory_mb",
                   "cpu": "cpu_seconds", "log": "log_bytes"}
        hit = set(sandbox_monitor.limits_hit(policy.limits(), monitor))
        for name in limit_fold:
            hit.add(aliases.get(name, name))
        run.limits_hit = sorted(hit)
        if run.timed_out and "timeout" not in run.limits_hit:
            run.limits_hit.append("timeout")
        # A run that was stopped by a budget is reported as one, whatever the
        # exit status was: the status says how the process died, the limit says
        # why, and the second is the one a reader needs.
        if run.limits_hit and run.status == "ok":
            run.status = "limited"
        # --- dangerous behaviour -------------------------------------------
        # The detectors read the run as a whole, so they run here, once every
        # other part of the record has been filled in: a finding that quoted a
        # half-built run would be evidence of the wrong thing.
        run.findings = sandbox_audit.detect(run)
        run.risk = sandbox_audit.risk_level(run.findings)
        run.reports = {key: sandbox_audit.report_document(key, run,
                                                          run.findings)
                       for key in sandbox_audit.REPORT_KEYS}
        shutil.rmtree(workspace, ignore_errors=True)
        return run

    def _show_run(self, run):
        """Render a run into the output pane and the status line."""
        body = run.output
        if run.truncated:
            body += "\n[output truncated: +%d char(s)]" % run.truncated
        self.output_view.setPlainText(body or self.t(
            "sandbox_no_output", "(no output)"))
        monitor = getattr(run, "monitor", None) or {}
        limits = self.t(
            "sandbox_hit", "limits: {limits}").format(
            limits=", ".join(getattr(run, "limits_hit", []) or []) or "none")
        self.result_label.setText(self.t(
            "sandbox_result",
            "status: {status} | exit: {code} | {elapsed:.2f}s | "
            "{truncated} char(s) truncated | cpu {cpu} | ram {ram} | "
            "{limits}").format(
                status=run.status,
                code=run.exit_code if run.exit_code is not None else "-",
                elapsed=run.elapsed, truncated=run.truncated,
                cpu=sandbox_monitor.value_text(
                    "cpu", monitor.get("cpu"))[0],
                ram=sandbox_monitor.value_text(
                    "ram", monitor.get("ram"))[0],
                limits=limits))
        self.status.setText(self.t(
            "sandbox_done", "run finished: {status}").format(
            status=run.status))
        self._show_findings(run)

    def _show_findings(self, run):
        # The detected behaviours, worst first, into their own pane. A clean run
        # is stated as checked rather than merely unremarked, so the absence of
        # findings is visible as a result not as a missing feature.
        view = getattr(self, "behaviour_view", None)
        if view is None:
            return
        findings = getattr(run, "findings", None) or []
        risk = getattr(run, "risk", "clean") or "clean"
        lines = ["risk: %s | %d finding(s)" % (risk, len(findings))]
        lines += sandbox_audit.format_findings(findings)
        view.setPlainText(chr(10).join(lines))

    def _current_format(self):
        # The format the user picked in the format combo (ASCII by default).
        combo = getattr(self, "format_combo", None)
        if combo is None:
            return "ascii"
        return combo.currentData() or "ascii"

    def show_report(self):
        # Render the selected audit report into the output pane.
        if self._last_run is None:
            self.status.setText(self.t("sandbox_no_run",
                                       "run something first"))
            return ""
        key = (self.report_combo.currentData()
               if hasattr(self, "report_combo") else "security")
        text = format_single_report(self._last_run, key, self._current_format())
        self.output_view.setPlainText(text)
        self.status.setText(self.t(
            "sandbox_report_shown", "report shown: {name}").format(
            name=self.report_combo.currentText()))
        return text

    def save_audit(self):
        # Write the selected report (ASCII or JSON) next to the database.
        if self._last_run is None:
            self.status.setText(self.t("sandbox_no_run",
                                       "run something first"))
            return ""
        key = (self.report_combo.currentData()
               if hasattr(self, "report_combo") else "security")
        fmt = self._current_format()
        path = os.path.join(self._base_dir(),
                            "sandbox_%s_%s.%s"
                            % (key, _stamp_for_file(), fmt))
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(format_single_report(self._last_run, key, fmt))
        except OSError as exc:
            self.status.setText(str(exc))
            return ""
        self.status.setText(self.t(
            "sandbox_audit_saved", "audit written: {path}").format(path=path))
        return path

    def save_all_reports(self):
        # Write every audit report, JSON first then ASCII, as one pair of files.
        if self._last_run is None:
            self.status.setText(self.t("sandbox_no_run",
                                       "run something first"))
            return []
        written = []
        for fmt in sandbox_audit.FORMATS:
            path = os.path.join(self._base_dir(),
                                "sandbox_audit_%s.%s"
                                % (_stamp_for_file(), fmt))
            try:
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(format_audit_report(self._last_run, fmt))
            except OSError:
                continue
            written.append(path)
        if written:
            self.status.setText(self.t(
                "sandbox_audit_saved", "audit written: {path}").format(
                path=written[0]))
        return written

    def show_integrations(self):
        # Print the live integration matrix into the output pane, so the wiring
        # is visible rather than asserted: each row says whether the module can
        # be reached *now*.
        rows = sandbox_integration.integration_matrix(self.window)
        lines = ["Terminal integration", "===================="]
        for row in rows:
            lines.append("%-9s %-4s %s" % (
                row["key"], "wired" if row["available"] else "n/a",
                row["label"]))
            lines.append("          direction: %s" % row["direction"])
            lines.append("          %s" % row["exchange"])
        self.output_view.setPlainText(chr(10).join(lines))
        available = sum(1 for row in rows if row["available"])
        self.status.setText(self.t(
            "sandbox_integrations_status",
            "integration: {count}/{total} module(s) wired").format(
                count=available, total=len(rows)))
        return lines

    def show_premium(self):
        # Print the premium feature list and the architecture standard, both
        # generated from the tables rather than written out.
        lines = sandbox_server.format_premium()
        lines += [""] + sandbox_server.format_architecture(self.window)
        self.output_view.setPlainText(chr(10).join(lines))
        self.status.setText(self.t("sandbox_premium_shown",
                                   "premium features shown"))
        return lines

    def save_report(self):
        """Write the last run's report to a file next to the database."""
        if self._last_run is None:
            self.status.setText(self.t("sandbox_no_run",
                                       "run something first"))
            return ""
        path = os.path.join(self._base_dir(), "sandbox_report.txt")
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(format_run_report(self._last_run))
        except OSError as exc:
            self.status.setText(str(exc))
            return ""
        self.status.setText(self.t(
            "sandbox_report_saved", "report written: {path}").format(
                path=path))
        return path

    # ---------------------------------------------------------- cross-tab
    def notify_audit(self, run):
        """Announce a sandbox run on the shared audit bus (best effort)."""
        notifier = getattr(self.window, "notify_audit", None)
        if not callable(notifier):
            return
        try:
            notifier("sandbox_run", where=run.script,
                     detail="%s (%s), risk=%s, %d finding(s)"
                     % (run.status, run.language,
                        getattr(run, "risk", "clean"),
                        len(getattr(run, "findings", None) or [])))
        except Exception:
            pass

    # ------------------------------------------------------- translation
    def retranslate(self, table):
        """Apply the main window's language table to this tab."""
        def label(key, fallback):
            return table.get(key, fallback)

        self.title_label.setText(label("sandbox", "Sandbox"))
        self.btn_refresh.setText(label("sandbox_refresh", "Refresh"))
        self.btn_run.setText(label("sandbox_run", "Run in sandbox"))
        self.btn_scan.setText(label("sandbox_scan", "Scan policy"))
        self.btn_save_report.setText(
            label("sandbox_save_report", "Save report"))
        self.chk_network.setText(
            label("sandbox_allow_network", "Allow network"))
        self.chk_writes.setText(label("sandbox_allow_writes", "Allow writes"))
        self.chk_monitor.setText(
            label("sandbox_monitor_runtime", "Monitor runtime"))
        self.btn_show_report.setText(
            label("sandbox_show_report", "Show report"))
        self.btn_save_audit.setText(label("sandbox_save_audit", "Save audit"))
        self.btn_integrations.setText(
            label("sandbox_integrations", "Integrations"))
        self.btn_premium.setText(label("sandbox_premium", "Premium"))
        self.chk_persistent.setText(label("sandbox_persistent", "Persistent"))
        self.chk_shadow_env.setText(
            label("sandbox_shadow_env_chk", "Shadow ENV"))
        self.chk_shadow_net.setText(
            label("sandbox_shadow_net_chk", "Shadow network"))
        for index, (_key, label_key, fallback, _reuse) in enumerate(SCOPE_RULES):
            self.scope_combo.setItemText(index, label(label_key, fallback))
        for key, title_key, fallback in (
                ("fs_whitelist", "sandbox_list_fs_whitelist",
                 "Allowed paths (one per line)"),
                ("net_whitelist", "sandbox_list_net_whitelist",
                 "Allowed hosts (one per line)")):
            editor = getattr(self, "list_editors", {}).get(key)
            if editor is None:
                continue
            box = editor.parentWidget()
            if box is not None:
                box.setTitle(label(title_key, fallback))
        for index, (_key, label_key, fallback, _s) in enumerate(POLICY_PRESETS):
            self.preset_combo.setItemText(index, label(label_key, fallback))
        # The audit report and format pickers, which were built from their own
        # tables and left out of the language switch.
        for index, (_key, label_key, fallback) in enumerate(REPORT_RULES):
            self.report_combo.setItemText(index, label(label_key, fallback))
        for index, fmt in enumerate(sandbox_audit.FORMATS):
            self.format_combo.setItemText(index, fmt.upper())
        self.rules_tree.setHeaderLabels([
            label("sandbox_col_rule", "Rule"),
            label("sandbox_col_state", "State"),
            label("sandbox_col_detail", "Detail"),
        ])
        self.refresh_rules()
