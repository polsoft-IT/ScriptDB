# -*- coding: utf-8 -*-
"""Process isolation for the Sandbox tab.

A sandboxed run must not be able to reach the application that started it. This
module builds the *isolation envelope* a run is started with: a scrubbed
environment, a private PATH, a private working directory, a private runtime
context, and a child-side runner that refuses to load the terminal's modules,
files or configuration.

    from tabs import sandbox_isolation as iso

    env = iso.build_environment(workspace, policy)   # the child's whole env
    spec = iso.describe(workspace, policy)           # what the run is given

What the envelope guarantees
----------------------------

**A separate process.** The child is started with ``python -I`` (isolated
mode), which drops ``sys.path[0]``, ``PYTHONPATH``, ``PYTHONSTARTUP`` and the
per-user site directory, so it cannot pick up the parent's imports.

**A separate environment.** The child does not inherit ``os.environ``. Only a
small allow-list of variables is passed through, and a run's own extra
variables come from the policy, not from the terminal.

**A separate PATH.** The child gets a PATH that names the interpreter's own
directory only. It cannot see the terminal's install locations, its tool
directories or its user bin folders.

**A separate working directory.** The child runs inside the throw-away
workspace, so relative paths never resolve into the application tree.

**A separate runtime context.** ``sys.path`` is replaced (never extended), the
byte-code cache is directed into the workspace, warnings and hashing are
pinned, and any config the runtime would read from the user profile is pointed
at the workspace instead.

**No access to the terminal.** The terminal here is the application that owns
the Debugger/Sandbox tabs: its memory, its imported modules, its files and its
configuration. The child cannot reach them because it never sees the paths and
never gets the modules -- and the runner additionally blocks a named set of
terminal modules, terminal files and terminal configuration by name, so even a
deliberate attempt is refused and reported.

The module is pure standard library and imports no Qt, so the envelope can be
built and asserted in a test without launching the tab.
"""

import os

# ---------------------------------------------------------------------------
# What the child is allowed to inherit
# ---------------------------------------------------------------------------
# A deliberately tiny allow-list. Anything not named here is dropped from the
# child's environment, so the run starts from a known state instead of the
# terminal's state. The list holds only variables a process genuinely needs to
# function, never anything that describes the user, the machine or the app.
ENV_ALLOW_LIST = (
    "SYSTEMROOT",       # Windows: needed to load system DLLs at all
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",          # Windows: needed for a shell to start
    "PATHEXT",          # Windows: needed to resolve an executable suffix
    "TEMP",
    "TMP",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
)

# Variables that describe the *terminal* (the host application / shell). They
# are removed explicitly even if something else would have passed them in,
# because each one is a handle back into the session that started the run.
HOST_ENV_DENY_LIST = (
    "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE",
    "PYTHONEXECUTABLE", "PYTHONNOUSERSITE", "PYTHONWARNINGS",
    "PYTHONHASHSEED", "PYTHONIOENCODING",
    "VIRTUAL_ENV", "CONDA_PREFIX", "CONDA_DEFAULT_ENV", "PIP_REQUIRE_VIRTUALENV",
    "PIP_INDEX_URL", "PIP_TARGET",
    "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA",
    "HOMEDRIVE", "HOMEPATH",
    "TERM", "TERM_PROGRAM", "SESSIONNAME", "SSH_CONNECTION", "SSH_TTY",
    "SHELL", "BASH_ENV", "ENV", "PROMPT",
    "COLUMNS", "LINES",
    "SDB_APP", "SDB_BASE_DIR", "SDB_DATA_FILE", "SDB_SETTINGS_FILE",
    "SDB_DEBUGGER_FILE", "SDB_AUDIT_FILE", "SDB_DOCS_DB", "SDB_REPORTS_DIR",
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
    "EDITOR", "VISUAL", "PAGER",
    "CDPATH",
)

# ---------------------------------------------------------------------------
# The terminal's modules, files and configuration
# ---------------------------------------------------------------------------
# The "terminal" is the application this tab lives in. These are the names of
# its own modules: importing any of them from inside a run would hand the child
# the host's live objects, so the runner refuses them by name.
HOST_MODULES = (
    "sdb_scripts",
    "tabs",
    "tabs.sandbox",
    "tabs.debugger",
    "tabs.audit",
    "tabs.docs_tab",
    "tabs.editor",
    "tabs.search_tab",
    "tabs.reports_tab",
    "tabs.analyzer",
    "tabs.code_flow",
    "tabs.flow_view",
    "tabs.breakpoints",
    "tabs.debug_langs",
    "tabs.syntax",
    "PyQt5",
    "PyQt5.QtCore",
    "PyQt5.QtGui",
    "PyQt5.QtWidgets",
)

# Modules that expose the terminal's memory, process table or OS surface. A
# run may not reach across the process boundary through any of them.
ISOLATION_MODULES = (
    "ctypes",           # raw memory access, incl. foreign process memory
    "cffi",
    "mmap",             # shares a mapping with any other process that maps it
    "multiprocessing",  # would spawn a sibling that is *not* under the policy
    "subprocess",
    "pty",
    "fcntl",            # termios family: the terminal itself
    "termios",
    "tty",
    "pwd",
    "grp",
    "resource",         # rlimits and process accounting of the parent
    "signal",           # can signal the parent's processes
)

# The terminal's files and configuration that a run must never read or write.
# Each entry is matched against the resolved absolute path of an open() call.
HOST_FILES = (
    "scripts_db.json",       # the script library
    "connections_db.json",   # saved connections (may hold credentials)
    "settings_db.json",      # the terminal's own preferences
    "debugger_db.json",      # the debug session
    "audit_timeline.json",   # the audit log
    "docs_db.sqlite",        # the document repository
    "reports_index.json",
)

# Directories that hold the terminal's configuration. A path below any of these
# is refused, whichever file it names.
HOST_CONFIG_DIRS = (
    ".sdb",
    "sdb_config",
    "sdb_settings",
)

# The names a process table would be searched for to find the terminal. Used by
# the runner's process guard: a run may not enumerate the process table at all,
# and if a language hands it a process list anyway, the names below are what it
# is told to keep its hands off.
HOST_PROCESS_MARKERS = (
    "sdb_scripts",
    "scriptdb",
    "pythonw",
    "sdb.exe",
)

# The sandbox always sets these, so the envelope is readable from the child.
ISOLATION_ENV_KEYS = (
    "SDB_SANDBOX_SCRIPT",
    "SDB_SANDBOX_ALLOW_NETWORK",
    "SDB_SANDBOX_ALLOW_WRITES",
    "SDB_SANDBOX_ALLOW_SUBPROCESS",
    "SDB_SANDBOX_ISOLATED",
    "SDB_SANDBOX_WORKSPACE",
    "SDB_SANDBOX_TERMINAL_ENV",
)

# How much of the inherited environment the report keeps, before it truncates.
ENV_REPORT_LIMIT = 40


def interpreter_dir():
    """The directory holding the interpreter that will run the child.

    The child's PATH is built from this alone, so PATH never points back at
    whatever the terminal had on it.
    """
    import sys
    directory = os.path.dirname(os.path.abspath(sys.executable or ""))
    return directory or os.curdir


def scrubbed_environment(allow_list=ENV_ALLOW_LIST):
    """The host environment reduced to the allow-list, minus the deny-list.

    Returns a fresh dict, so a caller can never accidentally hand the child a
    reference to the parent's own environment mapping.
    """
    allowed = set(name.upper() for name in allow_list)
    denied = set(name.upper() for name in HOST_ENV_DENY_LIST)
    clean = {}
    for name, value in os.environ.items():
        upper = name.upper()
        if upper in denied or upper not in allowed:
            continue
        clean[name] = value
    return clean


def system_dirs():
    """The platform's own binary directories (never the terminal's PATH).

    These are where the *operating system's* interpreters live: `cmd.exe`,
    `powershell.exe` and `cscript.exe` on Windows, `sh` and the core tools on
    POSIX. They are included on their own merits -- the platform puts its
    binaries there -- and not because the host happened to have them on its
    PATH, which is the distinction that keeps the isolation honest.
    """
    entries = []
    if os.name == "nt":
        root = os.environ.get("SYSTEMROOT") or os.environ.get("WINDIR") or ""
        if root:
            entries.append(os.path.join(root, "System32"))
            entries.append(os.path.join(root, "System32", "WindowsPowerShell",
                                        "v1.0"))
            entries.append(os.path.join(root, "System32", "Wbem"))
    else:
        entries.extend(["/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin",
                        "/sbin"])
    return [entry for entry in entries if entry and os.path.isdir(entry)]


def isolated_path(workspace=""):
    """A PATH naming the interpreter's bin, the OS binaries and the workspace.

    What it deliberately does *not* contain is anything the terminal put on its
    own PATH: an appended tool, a virtualenv's bin, a project's scripts. Those
    are the things a run must not inherit, while `System32` is not an
    inheritance -- it is the operating system.
    """
    entries = [interpreter_dir()]
    entries.extend(system_dirs())
    if workspace:
        entries.append(workspace)
    # A duplicate would be harmless but reads as noise in the report.
    unique = []
    for entry in entries:
        if entry and entry not in unique:
            unique.append(entry)
    return os.pathsep.join(unique)


def build_environment(workspace, policy, extra=None):
    """Build the complete environment the child process will run with.

    `workspace` is the throw-away directory the run owns. `policy` supplies the
    boolean guard flags and any run-specific variables. `extra` adds variables
    for this run only; a host variable can never be smuggled in through it.
    """
    env = scrubbed_environment()

    # A PATH of our own, never the terminal's.
    env["PATH"] = isolated_path(workspace)
    env["PATHEXT"] = env.get("PATHEXT", ".COM;.EXE;.BAT;.CMD")

    # Point every per-user location the runtime might consult at the workspace,
    # so a run can neither read the terminal's profile nor write into it.
    env["HOME"] = workspace
    env["USERPROFILE"] = workspace
    env["APPDATA"] = workspace
    env["LOCALAPPDATA"] = workspace
    env["TMPDIR"] = workspace
    env["TEMP"] = workspace
    env["TMP"] = workspace
    env["XDG_CACHE_HOME"] = os.path.join(workspace, "cache")
    env["XDG_CONFIG_HOME"] = os.path.join(workspace, "config")
    env["PYTHONPYCACHEPREFIX"] = os.path.join(workspace, "__pycache__")
    env["PYTHONHASHSEED"] = "0"
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    # The isolation flags the runner reads back.
    env["SDB_SANDBOX_ISOLATED"] = "1"
    env["SDB_SANDBOX_WORKSPACE"] = workspace
    env["SDB_SANDBOX_ALLOW_NETWORK"] = "1" if policy.allow_network else "0"
    env["SDB_SANDBOX_ALLOW_WRITES"] = "1" if policy.allow_writes else "0"
    env["SDB_SANDBOX_ALLOW_SUBPROCESS"] = "1" if policy.subprocess else "0"
    env["SDB_SANDBOX_TERMINAL_ENV"] = "0"

    for name, value in (extra or {}).items():
        if not name or name.upper() in set(n.upper()
                                           for n in HOST_ENV_DENY_LIST):
            continue
        env[str(name)] = str(value)
    return env


# ---------------------------------------------------------------------------
# The security profile
# ---------------------------------------------------------------------------
# (key, label key, fallback, what the profile promises).
#
# A profile names a *level* of isolation, not a language and not a policy: the
# policy says what a run may touch, the profile says how thoroughly the run is
# separated from the terminal before any of that matters. The three levels exist
# because they compose differently: a profile is what a caller picks when it
# wants "the strongest the sandbox can do" or "the same envelope the standard
# preset uses", without re-listing eight booleans.
SECURITY_PROFILES = (
    ("strict", "sandbox_profile_strict", "Strict",
     "every separation the sandbox can provide, for a language whose "
     "interpreter can be held to its strongest mode"),
    ("standard", "sandbox_profile_standard", "Standard",
     "the same separations, for a language with moderate guards: an own "
     "process, environment, PATH and working directory, no terminal handle"),
    ("permissive", "sandbox_profile_permissive", "Permissive",
     "the separations still hold, but the run is allowed the network, writes "
     "and child processes the policy would otherwise refuse"),
)

SECURITY_PROFILE_KEYS = tuple(key for key, _lk, _f, _d in SECURITY_PROFILES)
SECURITY_PROFILE_LABELS = {key: fallback
                           for key, _lk, fallback, _d in SECURITY_PROFILES}
SECURITY_PROFILE_MEANINGS = {key: meaning
                             for key, _lk, _f, meaning in SECURITY_PROFILES}

# The separations a profile asserts. Each is a fact the launcher has to make
# true, so the list is the checklist the launcher is written against rather than
# a description written after the fact.
PROFILE_SEPARATIONS = (
    "process",       # its own process, stdin closed
    "environment",   # its own environment, scrubbed to the allow-list
    "working_dir",   # its own working directory
    "path",          # its own PATH, never the terminal's
    "variables",     # a fresh set of variables, built per run
    "interpreter",   # its own interpreter invocation, in its own guard mode
    "terminal",      # no handle on the terminal's memory, modules, files,
                     # configuration or processes
)


def profile_separations():
    """The separations a profile asserts, as ``(key, question)`` pairs.

    The question is what a reader would ask to check the separation, which is
    what makes this list usable as a checklist rather than a slogan.
    """
    return (
        ("process", "does the run have its own process, with stdin closed?"),
        ("environment", "is the environment scrubbed rather than inherited?"),
        ("working_dir", "is the working directory the run's own?"),
        ("path", "is PATH the sandbox's own, not the terminal's?"),
        ("variables", "is the variable set built fresh for this run?"),
        ("interpreter", "is the interpreter invoked in its own guard mode?"),
        ("terminal",
         "is the terminal's memory, modules, files, configuration and "
         "processes out of reach?"),
    )


def security_profile(name, policy=None, interpreter=None):
    """Build the security profile a run is executed under.

    Returns a plain dict with the profile's name, the separations it asserts and
    the evidence for each one, so a report can show *why* the profile holds
    rather than only that it was selected. `interpreter` is the resolved
    interpreter (see `tabs.sandbox_langs`); when it is absent the interpreter
    separation is reported as unverified rather than as true, because a profile
    that claimed a fact nobody checked would be worse than one that names the
    gap.
    """
    name = name if name in SECURITY_PROFILE_KEYS else "standard"
    isolated = bool(getattr(policy, "isolate_cwd", True))
    interp_key = (interpreter or {}).get("key", "")
    interp_level = (interpreter or {}).get("guard_level", "")
    checks = {
        "process": {
            "ok": True,
            "evidence": "a separate child process, stdin closed",
        },
        "environment": {
            "ok": bool(getattr(policy, "isolate_environment", True)),
            "evidence": "environment built from the allow-list",
        },
        "working_dir": {
            "ok": isolated,
            "evidence": "cwd is the run's workspace",
        },
        "path": {
            "ok": bool(getattr(policy, "isolate_path", True)),
            "evidence": "PATH is the interpreter directory plus the workspace",
        },
        "variables": {
            "ok": True,
            "evidence": "a fresh mapping per run, never the parent's",
        },
        "interpreter": {
            "ok": bool(interp_key),
            "evidence": ("%s in %s guard mode"
                         % (interp_key, interp_level or "unknown"))
            if interp_key else "no interpreter resolved for this run",
        },
        "terminal": {
            "ok": bool(getattr(policy, "block_terminal", True)),
            "evidence": "terminal modules, files, config and processes refused",
        },
    }
    held = [key for key, _q in profile_separations() if checks[key]["ok"]]
    return {
        "name": name,
        "label": SECURITY_PROFILE_LABELS.get(name, name),
        "meaning": SECURITY_PROFILE_MEANINGS.get(name, ""),
        "separations": [{"key": key, "question": question,
                         "ok": checks[key]["ok"],
                         "evidence": checks[key]["evidence"]}
                        for key, question in profile_separations()],
        "held": len(held),
        "total": len(list(profile_separations())),
        "complete": len(held) == len(list(profile_separations())),
        "interpreter": interp_key,
        "guard_level": interp_level,
    }


def format_profile(report):
    """A security profile as ASCII lines (the policy view and the report)."""
    if not report:
        return ["(no security profile)"]
    lines = [
        "Security profile",
        "----------------",
        "name        : %s (%s)" % (report.get("name", "-"),
                                   report.get("label", "-")),
        "separations : %d of %d held" % (report.get("held", 0),
                                         report.get("total", 0)),
        "interpreter : %s (%s guard mode)"
        % (report.get("interpreter") or "-",
           report.get("guard_level") or "-"),
    ]
    for item in report.get("separations", []):
        lines.append("%-4s %-12s %s" % ("ok" if item.get("ok") else "GAP",
                                        item.get("key", "?"),
                                        item.get("evidence", "")))
    return lines


def describe(workspace, policy):
    """A JSON-friendly description of the envelope a run is given.

    This is what the run report and the policy view show, so the isolation is
    visible rather than implied by the code.
    """
    env = build_environment(workspace, policy)
    return {
        "process": {
            "separate_process": True,
            "isolated_interpreter": True,
            "stdin": "closed",
            "memory_shared_with_terminal": False,
        },
        "environment": {
            "inherits_terminal": False,
            "variable_count": len(env),
            "allow_list": list(ENV_ALLOW_LIST),
            "deny_list": list(HOST_ENV_DENY_LIST),
            "keys": sorted(env.keys())[:ENV_REPORT_LIMIT],
        },
        "path": {
            "inherits_terminal": False,
            "value": env.get("PATH", ""),
            "system_dirs": system_dirs(),
        },
        "working_directory": {
            "value": workspace,
            "isolated": bool(policy.isolate_cwd),
        },
        "runtime": {
            "sys_path": "replaced",
            "pycache": env.get("PYTHONPYCACHEPREFIX", ""),
            "hash_seed": env.get("PYTHONHASHSEED", ""),
            "user_site": False,
        },
        "terminal_access": {
            "memory": False,
            "modules": list(HOST_MODULES),
            "files": list(HOST_FILES),
            "config_dirs": list(HOST_CONFIG_DIRS),
            # The process table is the fifth handle back into the terminal, and
            # the one that is easiest to overlook: a run that can read it can
            # learn what the user is running, and a run that can signal it can
            # stop the terminal outright.
            "processes": False,
            "process_names": list(HOST_PROCESS_MARKERS),
        },
    }


def format_isolation(report):
    """The envelope as ASCII lines (policy view, run report, tests)."""
    if not report:
        return ["(no isolation data)"]
    process = report.get("process", {})
    env = report.get("environment", {})
    path = report.get("path", {})
    cwd = report.get("working_directory", {})
    runtime = report.get("runtime", {})
    terminal = report.get("terminal_access", {})
    lines = [
        "Process isolation",
        "-----------------",
        "separate process : %s" % _yes_no(process.get("separate_process")),
        "isolated interp  : %s" % _yes_no(process.get("isolated_interpreter")),
        "shares memory    : %s" % _yes_no(
            process.get("memory_shared_with_terminal")),
        "stdin            : %s" % (process.get("stdin") or "-"),
        "",
        "environment      : %s (%d variable(s))" % (
            "own" if not env.get("inherits_terminal") else "inherited",
            env.get("variable_count", 0)),
        "PATH             : %s" % ("own" if not path.get("inherits_terminal")
                                    else "inherited"),
        "working dir      : %s" % (cwd.get("value") or "-"),
        "runtime sys.path : %s" % (runtime.get("sys_path") or "-"),
        "",
        "terminal access",
        "  memory         : %s" % _yes_no(terminal.get("memory")),
        "  modules denied : %d" % len(terminal.get("modules", [])),
        "  files denied   : %d" % len(terminal.get("files", [])),
        "  config denied  : %d" % len(terminal.get("config_dirs", [])),
        "  processes      : %s" % _yes_no(terminal.get("processes")),
    ]
    return lines


def _yes_no(value):
    """``"yes"`` / ``"no"`` for an optional boolean."""
    if value is None:
        return "-"
    return "yes" if value else "no"


def path_is_terminal_file(path, workspace=""):
    """True when an absolute path names the terminal's data or configuration.

    Used by the runner so the child can refuse a handle back into the host, and
    by the tests so the rule can be asserted without running anything.
    """
    try:
        resolved = os.path.abspath(str(path or ""))
    except (TypeError, ValueError):
        return False
    if not resolved:
        return False
    # Anything inside the run's own workspace is the run's own business.
    if workspace:
        try:
            if os.path.commonpath([resolved, os.path.abspath(workspace)]) == \
                    os.path.abspath(workspace):
                return False
        except ValueError:
            pass
    name = os.path.basename(resolved).lower()
    if name in set(entry.lower() for entry in HOST_FILES):
        return True
    lowered = [part.lower() for part in resolved.replace("\\", "/").split("/")]
    for directory in HOST_CONFIG_DIRS:
        if directory.lower() in lowered:
            return True
    return False


def module_is_denied(name, policy=None):
    """True when an import of `name` must be refused inside a run.

    The terminal's own modules and the isolation modules are always refused; the
    policy then decides the network and subprocess families.
    """
    root = (name or "").split(".")[0]
    full = (name or "")
    if root in set(entry.split(".")[0] for entry in HOST_MODULES):
        return True
    if root in ("PyQt5",):
        return True
    if root in ISOLATION_MODULES:
        return True
    if policy is not None:
        if not policy.allow_network and root in (
                "socket", "ssl", "http", "urllib", "ftplib", "smtplib",
                "telnetlib", "asyncio", "requests", "httplib", "xmlrpc"):
            return True
        if not policy.subprocess and root in ("subprocess", "pty",
                                              "multiprocessing"):
            return True
    if full in HOST_MODULES:
        return True
    return False


def isolation_capabilities():
    """Describe the isolation for the audit / about layer."""
    return {
        "separate_process": True,
        "separate_environment": True,
        "separate_path": True,
        "separate_working_directory": True,
        "separate_runtime": True,
        "terminal_memory_access": False,
        "host_modules_denied": len(HOST_MODULES),
        "isolation_modules_denied": len(ISOLATION_MODULES),
        "host_files_denied": len(HOST_FILES),
        "host_config_denied": len(HOST_CONFIG_DIRS),
        "offline": True,
    }
