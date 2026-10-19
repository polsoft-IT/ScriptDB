# -*- coding: utf-8 -*-
"""Syscall filtering for the Sandbox tab (a mini-seccomp for Python / Node).

A sandbox has to stop a run at the *operation* level, not only at the path
level: deleting a file and opening one are different syscalls, and so are
`exec` and `chmod`. This module names the syscalls a run may not make, groups
them, and decides each one. It is the policy the child runner enforces.

    from tabs import sandbox_syscall as sc

    verdict = sc.check("execve", policy)
    verdict.allowed          # False
    verdict.reason           # "exec is blocked"
    verdict.category         # "exec"

The twelve guarded syscalls, in the groups the policy switches on:

    process    exec    spawn a program (os.exec*, os.system, a shell)
               fork    duplicate the process (os.fork, multiprocessing)
               spawn   start a child (subprocess, posix_spawn, spawn*)
               kill    signal another process (os.kill, os.killpg)
    system     mount   mount / unmount a filesystem
               chmod   change a file's mode
               chown   change a file's owner
    files      open    open a path outside the whitelist
               write   write a path outside the whitelist
    network    socket  create a socket
               bind    bind a socket to an address
               connect a socket to an address

How it is enforced
------------------

Python has no `seccomp`, so the filter is a *guarded entry point*: every
module-level function that reaches one of those syscalls is replaced with a
wrapper that consults this module first. The wiring lives in the child runner
(`tabs/sandbox.py::_RUNNER_SOURCE`), which calls :func:`is_blocked` for each
guarded name. That is the same shape a seccomp profile has -- a list of
operations, each allowed or denied -- with the check made in the interpreter
instead of the kernel.

**Where this is weaker than real seccomp, said plainly.** A kernel filter sees
the actual syscall and cannot be bypassed from the process. This filter sees
the *Python* entry point, so it can be bypassed by anything that reaches the
same syscall another way: a C extension the sandbox did not block, a module
that was already imported before the guard was installed, or a direct
`ctypes` call. That is why the guard is deliberately layered *on top of* the
harder blocks -- the module deny-list (which refuses `ctypes`, `mmap`, `fcntl`
and the rest), the separate process, and the scrubbed environment. Each layer
covers the previous one's escape hatch, and this module is the layer that names
the operations and reports them.

What this module *can* guarantee, and what the tests assert, is that every
guarded entry point in the runner refuses its syscall, that the refusal is
recorded with the syscall name, and that the decision is the policy`a` job
rather than the wrapper's.

The module is pure standard library and imports no Qt, so the whole table can
be asserted without starting the tab or a child process.
"""

# ---------------------------------------------------------------------------
# The syscall table
# ---------------------------------------------------------------------------
# Each syscall: the name, the group that owns it, the label shown in the UI and
# the entry points in the standard library that reach it. The entry points are
# what the runner actually replaces.
SYSCALLS = (
    # --- process control ---------------------------------------------------
    ("exec", "process", "Exec",
     ("os.execv", "os.execve", "os.execl", "os.execle", "os.execlp",
      "os.execvp", "os.execvpe", "os.system", "os.spawnv", "os.spawnl",
      "os.posix_spawn", "os.posix_spawnp", "pty.spawn")),
    ("fork", "process", "Fork",
     ("os.fork", "os.forkpty", "multiprocessing.Process")),
    ("spawn", "process", "Spawn",
     ("subprocess.Popen", "subprocess.run", "subprocess.call",
      "subprocess.check_call", "subprocess.check_output",
      "subprocess.getoutput", "subprocess.getstatusoutput")),
    ("kill", "process", "Kill",
     ("os.kill", "os.killpg", "signal.raise_signal")),

    # --- system ------------------------------------------------------------
    ("mount", "system", "Mount",
     ("os.mount", "os.umount", "os.chroot", "os.chdir_root")),
    ("chmod", "system", "Chmod",
     ("os.chmod", "os.lchmod", "os.fchmod", "os.chflags", "Path.chmod")),
    ("chown", "system", "Chown",
     ("os.chown", "os.lchown", "os.fchown", "Path.chown")),

    # --- filesystem --------------------------------------------------------
    ("open", "files", "Open",
     ("builtins.open", "io.open", "os.open", "pathlib.Path.open",
      "codecs.open")),
    ("write", "files", "Write",
     ("os.write", "os.pwrite", "os.truncate", "os.ftruncate")),

    # --- network -----------------------------------------------------------
    ("socket", "network", "Socket",
     ("socket.socket", "socket.create_connection", "socket.fromfd",
      "socket.socketpair")),
    ("bind", "network", "Bind",
     ("socket.socket.bind", "socket.socket.bind_ex")),
    ("connect", "network", "Connect",
     ("socket.socket.connect", "socket.socket.connect_ex",
      "socket.create_connection")),
)

# The syscall names, in table order.
SYSCALL_NAMES = tuple(entry[0] for entry in SYSCALLS)

# The groups, in the order the policy view lists them.
SYSCALL_GROUPS = ("process", "system", "files", "network")

# The policy attribute that switches one group on.
GROUP_POLICY = {
    "process": "block_exec",
    "system": "block_system",
    "files": "block_files",
    "network": "block_network",
}

# The label shown for a syscall that is blocked.
REASONS = {
    "exec": "exec is blocked",
    "fork": "fork is blocked",
    "spawn": "spawn is blocked",
    "kill": "kill is blocked",
    "mount": "mount is blocked",
    "chmod": "chmod is blocked",
    "chown": "chown is blocked",
    "open": "open is blocked outside the whitelist",
    "write": "write is blocked outside the whitelist",
    "socket": "socket is blocked",
    "bind": "bind is blocked",
    "connect": "connect is blocked",
}

# The syscalls that are refused even when their group is switched on. `mount`
# and `kill` are not things a script in a sandbox ever has a reason to do, so
# they stay on a second list the policy cannot relax.
ALWAYS_BLOCKED = ("mount", "kill")


class SyscallVerdict(object):
    """The decision for one syscall.

    ``allowed`` is the decision, ``name`` the syscall, ``category`` its group,
    and ``reason`` why it was refused (empty when allowed).
    """

    __slots__ = ("allowed", "name", "category", "reason", "detail")

    def __init__(self, allowed, name, category="", reason="", detail=""):
        self.allowed = bool(allowed)
        self.name = name or ""
        self.category = category or ""
        self.reason = reason or ""
        self.detail = detail or ""

    def to_dict(self):
        return {
            "allowed": self.allowed,
            "syscall": self.name,
            "category": self.category,
            "reason": self.reason,
            "detail": self.detail,
        }

    def __bool__(self):
        return self.allowed

    def __repr__(self):
        state = "allow" if self.allowed else "deny"
        return "SyscallVerdict(%s %s)" % (state, self.name)


def _allow(name, category):
    return SyscallVerdict(True, name, category)


def _deny(name, category, reason):
    return SyscallVerdict(False, name, category, reason)


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------
def category_of(name):
    """The group a syscall belongs to (or "" when the name is not one)."""
    lowered = (name or "").strip().lower()
    for syscall, category, _label, _entries in SYSCALLS:
        if syscall == lowered:
            return category
    return ""


def entry_points(name):
    """The standard-library entry points that reach a syscall."""
    lowered = (name or "").strip().lower()
    for syscall, _category, _label, entries in SYSCALLS:
        if syscall == lowered:
            return tuple(entries)
    return ()


def syscall_for_entry(entry):
    """The syscall a qualified entry point reaches (or "").

    A lookup by the longest suffix wins, so `os.fchmod` resolves to `chmod`
    rather than to any shorter name that happens to be a suffix.
    """
    text = (entry or "").strip()
    if not text:
        return ""
    best, best_length = "", 0
    for syscall, _category, _label, entries in SYSCALLS:
        for candidate in entries:
            if text == candidate or text.endswith("." + candidate) \
                    or candidate.endswith("." + text):
                if len(candidate) > best_length:
                    best, best_length = syscall, len(candidate)
    return best


def label_of(name):
    """The visible label of a syscall (or the name itself)."""
    lowered = (name or "").strip().lower()
    for syscall, _category, label, _entries in SYSCALLS:
        if syscall == lowered:
            return label
    return name or ""


def describe():
    """The whole table, for a report or a test."""
    return [
        {"syscall": syscall, "category": category, "label": label,
         "entries": list(entries), "reason": REASONS.get(syscall, "")}
        for syscall, category, label, entries in SYSCALLS
    ]


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------
def group_is_blocked(category, policy):
    """True when the policy blocks a group of syscalls.

    With no policy at all every group is blocked, which is the safe default: a
    caller that forgets to pass one gets the closed configuration rather than
    the open one.
    """
    if policy is None:
        return True
    attribute = GROUP_POLICY.get(category)
    if not attribute:
        return True
    return bool(getattr(policy, attribute, True))


def is_blocked(name, policy=None):
    """``(blocked, reason)`` for one syscall.

    `mount` and `kill` are always blocked; the rest follow their group's
    switch. An unknown name is not blocked here, because the path-level rules
    and the module deny-list are what govern anything this table does not name.
    """
    lowered = (name or "").strip().lower()
    if lowered in ALWAYS_BLOCKED:
        return True, REASONS.get(lowered, "%s is blocked" % lowered)
    category = category_of(lowered)
    if not category:
        return False, ""
    if group_is_blocked(category, policy):
        return True, REASONS.get(lowered, "%s is blocked" % lowered)
    return False, ""


def check(name, policy=None, detail=""):
    """Decide one syscall and return a `SyscallVerdict`.

    An unknown syscall is allowed: this table is the *named* operations, and
    something outside it is the business of the path rules and the module
    deny-list rather than of a filter that does not know what it is.
    """
    lowered = (name or "").strip().lower()
    category = category_of(lowered)
    if not category:
        return _allow(lowered, "")
    blocked, reason = is_blocked(lowered, policy)
    if blocked:
        return SyscallVerdict(False, lowered, category, reason, detail)
    return _allow(lowered, category)


def check_entry(entry, policy=None):
    """Decide a *qualified entry point* (`os.execv`, `socket.socket.bind`)."""
    name = syscall_for_entry(entry)
    if not name:
        return _allow(entry, "")
    return check(name, policy, detail=entry)


# ---------------------------------------------------------------------------
# Counting and reporting
# ---------------------------------------------------------------------------
class SyscallCounter(object):
    """How many times each syscall was decided, and how many were refused.

    The runner keeps one of these and writes it out at the end of a run, so the
    report shows what a script *tried* -- not only what it succeeded at.
    """

    def __init__(self):
        self.calls = {}
        self.denied = {}

    def record(self, verdict):
        """Count one verdict; returns it so a caller can chain."""
        if verdict is None:
            return None
        self.calls[verdict.name] = self.calls.get(verdict.name, 0) + 1
        if not verdict.allowed:
            self.denied[verdict.name] = self.denied.get(verdict.name, 0) + 1
        return verdict

    def total(self):
        return sum(self.calls.values())

    def refused(self):
        return sum(self.denied.values())

    def summary(self):
        """The counts as a JSON-friendly dict."""
        return {
            "decisions": self.total(),
            "refused": self.refused(),
            "by_syscall": dict(sorted(self.denied.items(),
                                      key=lambda item: -item[1])),
        }

    def format_report(self):
        """The counts as ASCII lines (run report and tests)."""
        lines = ["Syscall filter",
                 "--------------",
                 "decisions : %d (%d refused)" % (self.total(), self.refused())]
        if not self.calls:
            lines.append("  (no guarded syscall was reached)")
            return lines
        for name in SYSCALL_NAMES:
            calls = self.calls.get(name, 0)
            if not calls:
                continue
            lines.append("  %-9s : %2d call(s), %2d refused" % (
                name, calls, self.denied.get(name, 0)))
        return lines


def profile(policy=None):
    """The effective seccomp-style profile: every syscall and its decision."""
    profile = []
    for syscall, category, label, entries in SYSCALLS:
        blocked, reason = is_blocked(syscall, policy)
        profile.append({
            "syscall": syscall,
            "category": category,
            "label": label,
            "action": "deny" if blocked else "allow",
            "reason": reason,
            "entry_points": list(entries),
        })
    return profile


def format_profile(policy=None):
    """The profile as ASCII lines, the way a seccomp dump reads."""
    lines = ["Syscall filter profile",
             "----------------------",
             "%-9s %-9s %-6s %s" % ("syscall", "group", "action", "reason")]
    lines.append("-" * 62)
    for entry in profile(policy):
        lines.append("%-9s %-9s %-6s %s" % (
            entry["syscall"], entry["category"], entry["action"],
            entry["reason"] or "-"))
    blocked = sum(1 for entry in profile(policy)
                  if entry["action"] == "deny")
    lines.append("-" * 62)
    lines.append("%d denied, %d allowed" % (blocked,
                                            len(SYSCALLS) - blocked))
    return lines


def syscall_capabilities():
    """Describe the syscall filter for the audit / about layer."""
    return {
        "syscalls": list(SYSCALL_NAMES),
        "groups": list(SYSCALL_GROUPS),
        "always_blocked": list(ALWAYS_BLOCKED),
        "entry_points": sum(len(entries) for _s, _c, _l, entries in SYSCALLS),
        "kernel_enforced": False,
        "guarded_entry_points": True,
        "offline": True,
    }
