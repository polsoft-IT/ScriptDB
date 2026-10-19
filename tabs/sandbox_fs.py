# -*- coding: utf-8 -*-
"""Filesystem isolation for the Sandbox tab (FS Sandbox).

A sandboxed run must only touch what it is meant to touch. This module decides
*which paths a run may reach*, refuses the operations that would damage a
machine, and gives the run a private view of the filesystem to work in.

    from tabs import sandbox_fs

    verdict = sandbox_fs.check("/etc/passwd", "read", workspace, policy)
    verdict.allowed          # False
    verdict.reason           # "outside the sandbox"

What it enforces
----------------

**A path whitelist.** Only paths below an allowed root may be reached. The
run's own workspace is always allowed; the policy may add more roots (a
read-only asset directory, an output directory), and the whitelist is what
decides, not the platform.

**A path blacklist.** Named paths and patterns are refused even if a whitelist
entry would otherwise cover them, so a broad allow rule can still protect a
specific file.

**A read wall and a write wall.** Reading outside the sandbox and writing
outside the sandbox are separate decisions: `allow_read_outside` and
`allow_writes` gate them independently, and a write is never allowed merely
because a read was.

**A destructive-operation block.** `remove`, `unlink`, `rmdir`, `rmtree`,
`rename`, `truncate`, `chmod`, `chown` and their relatives are refused by name
when `block_destructive` is on — the classic way a script destroys a machine is
not by writing a file, it is by deleting one.

**A recursive-operation block.** `rmtree`, `walk` over a huge tree, `copytree`
and `glob('**')` are refused when `block_recursive` is on, because a recursive
operation is the one that turns a small mistake into an unbounded one.

**A system-directory block.** The operating system's own directories are
refused by name on every platform (`/etc`, `/usr`, `/bin`, `C:\\Windows`,
`C:\\Program Files`, the drive root...), whatever the whitelist says.

**A pseudo-filesystem.** The run can be given a private tree that *looks* like
a root: `pseudo_fs()` maps a virtual path onto the workspace, so a script that
insists on writing to `/tmp/x` or `C:\\data\\x` writes into the sandbox
instead of failing.

**A shadow copy.** `shadow_copy()` takes a copy of a real file into the
workspace and returns the copy's path, so a run may "edit" a file without the
original ever being at risk.

**A filesystem snapshot.** `snapshot()` records the size, mtime and hash of
every file in a tree, so a run's real effect on disk can be proved afterwards
by comparing two snapshots.

Everything here is pure standard library and imports no Qt, so the whole policy
can be asserted in a test without starting the tab, and the child process can
use the same rules as the parent.
"""

import hashlib
import os
import re

# ---------------------------------------------------------------------------
# Operations a path can be checked for
# ---------------------------------------------------------------------------
READ_OPS = ("read", "list", "stat", "exists")
WRITE_OPS = ("write", "append", "create", "truncate", "rename")
DESTRUCTIVE_OPS = ("remove", "unlink", "rmdir", "rmtree", "chmod", "chown",
                   "truncate", "rename", "replace", "move")
RECURSIVE_OPS = ("rmtree", "copytree", "walk", "glob_recursive", "move_tree")

# The operations that are refused when `block_destructive` is on. `chmod` and
# `chown` are in here on purpose: they do not destroy data, but they are the
# two calls that quietly remove the owner's ability to recover it.
DESTRUCTIVE_CALLS = (
    "os.remove", "os.unlink", "os.rmdir", "os.removedirs", "os.rename",
    "os.replace", "os.truncate", "os.chmod", "os.chown", "os.lchmod",
    "os.lchown", "shutil.rmtree", "shutil.move", "shutil.copytree",
    "pathlib.Path.unlink", "pathlib.Path.rmdir", "pathlib.Path.chmod",
    "pathlib.Path.rename", "pathlib.Path.replace", "pathlib.Path.touch",
)

# The recursive calls, refused when `block_recursive` is on.
RECURSIVE_CALLS = (
    "shutil.rmtree", "shutil.copytree", "os.walk", "os.removedirs",
    "pathlib.Path.glob", "pathlib.Path.rglob", "glob.glob",
)

# ---------------------------------------------------------------------------
# The directories that belong to the operating system
# ---------------------------------------------------------------------------
# Refused on every platform. A run has no business here, and each of these is a
# directory whose loss or corruption makes the machine unusable.
SYSTEM_DIRS_POSIX = (
    "/etc", "/usr", "/bin", "/sbin", "/lib", "/lib64", "/boot", "/dev",
    "/proc", "/sys", "/var/lib", "/var/log", "/root", "/opt",
)

# Windows directories, compared case-insensitively after resolving.
SYSTEM_DIRS_WINDOWS = (
    "windows", "program files", "program files (x86)", "programdata",
    "system32", "syswow64", "winsxs", "perflogs", "$recycle.bin",
    "recovery", "boot",
)

# A path segment that names the user profile itself (the home directory). The
# profile root is refused; a directory *inside* it is governed by the ordinary
# whitelist and blacklist rules.
PROFILE_MARKERS = (".ssh", ".gnupg", ".aws", ".kube", ".docker", "cookies",
                   "login data", "keychain", "credentials")

# Longest path a single check will resolve, so a hand-crafted 100k-character
# path cannot turn a check into a denial of service.
MAX_PATH_LENGTH = 4096

# The hash chunk size used by the snapshot writer.
HASH_CHUNK = 65536


class FsVerdict(object):
    """The result of one filesystem check.

    ``allowed`` is the decision, ``reason`` says why it was refused (empty when
    allowed) and ``rule`` is the rule that made the decision, so the report can
    count refusals by cause.
    """

    __slots__ = ("allowed", "reason", "rule", "path", "operation")

    def __init__(self, allowed, path, operation, reason="", rule=""):
        self.allowed = bool(allowed)
        self.path = path or ""
        self.operation = operation or ""
        self.reason = reason or ""
        self.rule = rule or ""

    def to_dict(self):
        return {
            "allowed": self.allowed,
            "path": self.path,
            "operation": self.operation,
            "reason": self.reason,
            "rule": self.rule,
        }

    def __bool__(self):
        return self.allowed

    def __repr__(self):
        state = "allow" if self.allowed else "deny"
        return "FsVerdict(%s %s %r)" % (state, self.operation, self.path)


def _deny(path, operation, reason, rule):
    return FsVerdict(False, path, operation, reason, rule)


def _allow(path, operation):
    return FsVerdict(True, path, operation)


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------
def normalise(path):
    """The absolute, case-folded form a comparison uses (or "" when unusable).

    A path longer than `MAX_PATH_LENGTH` is rejected outright: resolving it
    would itself be the attack.
    """
    text = str(path or "")
    if not text or len(text) > MAX_PATH_LENGTH:
        return ""
    try:
        return os.path.normcase(os.path.abspath(text))
    except (TypeError, ValueError, OSError):
        return ""


def is_below(path, root):
    """True when `path` is `root` itself or sits below it."""
    resolved = normalise(path)
    base = normalise(root)
    if not resolved or not base:
        return False
    if resolved == base:
        return True
    # `commonpath` is the only correct way to do this: a plain startswith would
    # let "/tmp/sandbox-evil" pass as a child of "/tmp/sandbox".
    try:
        return os.path.commonpath([resolved, base]) == base
    except (ValueError, OSError):
        return False


def path_parts(path):
    """The lower-cased segments of a path, for the name-based rules."""
    resolved = normalise(path)
    if not resolved:
        return []
    return [part.lower() for part in resolved.replace("\\", "/").split("/")
            if part]


def system_dir_reason(path):
    """Why a path belongs to the operating system (or "" when it does not)."""
    resolved = normalise(path)
    if not resolved:
        return ""
    parts = path_parts(path)
    if not parts:
        return ""
    # A bare POSIX root is the operating system's tree as a whole.
    if resolved in (os.sep, "/"):
        return "system directory: /"
    if os.name == "nt":
        # A bare drive root, e.g. "C:\\".
        drive, tail = os.path.splitdrive(resolved)
        if drive and tail in ("\\", "/", ""):
            return "system directory: drive root"
        for marker in SYSTEM_DIRS_WINDOWS:
            if marker in parts:
                return "system directory: %s" % marker
        for name in ("pagefile.sys", "hiberfil.sys", "bootmgr", "ntldr"):
            if name in parts:
                return "system file: %s" % name
        return ""
    for marker in SYSTEM_DIRS_POSIX:
        target = marker.strip("/").lower()
        if target and target in parts:
            return "system directory: %s" % marker
    return ""


def profile_reason(path):
    """Why a path belongs to the user profile's secrets (or "")."""
    parts = path_parts(path)
    for marker in PROFILE_MARKERS:
        if marker in parts:
            return "credentials path: %s" % marker
    return ""


def matched_pattern(path, patterns):
    """The first glob-ish pattern that matches a path (or "").

    A pattern is matched against the whole path and against its base name, so
    both `*.pem` and `/etc/*` behave the way a reader expects.
    """
    resolved = normalise(path)
    if not resolved:
        return ""
    name = os.path.basename(resolved).lower()
    for pattern in patterns or ():
        text = str(pattern or "").strip()
        if not text:
            continue
        candidate = normalise(text)
        if candidate and (resolved == candidate or is_below(resolved, candidate)):
            return text
        if _glob_match(name, text.lower()) or _glob_match(resolved, text.lower()):
            return text
    return ""


def _glob_match(text, pattern):
    """A tiny glob matcher supporting `*` and `?` only.

    Written by hand rather than with `fnmatch` so a pattern can never reach the
    filesystem (no `[]` character classes that a caller might turn into a path
    probe), and so the behaviour is identical on every platform.
    """
    if not pattern:
        return False
    expression = ["^"]
    for character in pattern:
        if character == "*":
            expression.append(".*")
        elif character == "?":
            expression.append(".")
        else:
            expression.append(re.escape(character))
    expression.append("$")
    try:
        return re.match("".join(expression), text) is not None
    except re.error:
        return False


# ---------------------------------------------------------------------------
# The check itself
# ---------------------------------------------------------------------------
def check(path, operation, workspace="", policy=None):
    """Decide whether one filesystem operation on `path` is allowed.

    `operation` is one of READ_OPS, WRITE_OPS, DESTRUCTIVE_OPS or RECURSIVE_OPS.
    The rules are applied in a fixed order, and the first one that has an
    opinion wins, so a refusal can always be explained by a single rule:

      1. an unusable path is refused,
      2. a named blacklist entry is refused,
      3. a matched blacklist pattern is refused,
      4. an operating-system directory is refused,
      5. a credentials path is refused,
      6. a destructive operation is refused (when the policy blocks them),
      7. a recursive operation is refused (when the policy blocks them),
      8. a path below the workspace is allowed,
      9. a write outside the workspace is refused (unless writes are open),
     10. a read outside the workspace is refused (unless reads are open),
     11. a path below an extra allowed root is allowed,
     12. anything else is refused.
    """
    operation = (operation or "read").lower()

    resolved = normalise(path)
    if not resolved:
        return _deny(path, operation, "unusable path", "invalid")

    # --- 2/3: the blacklist, checked before anything may allow the path ------
    blacklist = list(getattr(policy, "fs_blacklist", ()) or ())
    hit = matched_pattern(resolved, blacklist)
    if hit:
        return _deny(path, operation, "on the blacklist", "blacklist:%s" % hit)

    # --- 4: the operating system's own directories --------------------------
    if getattr(policy, "block_system_dirs", True):
        reason = system_dir_reason(resolved)
        if reason:
            return _deny(path, operation, reason, "system_dir")

    # --- 5: credentials never, whatever else says otherwise -----------------
    reason = profile_reason(resolved)
    if reason:
        return _deny(path, operation, reason, "credentials")

    # --- 6: destructive operations ------------------------------------------
    if operation in DESTRUCTIVE_OPS and getattr(policy, "block_destructive",
                                                True):
        return _deny(path, operation,
                     "destructive operation blocked", "destructive")

    # --- 7: recursive operations --------------------------------------------
    if operation in RECURSIVE_OPS and getattr(policy, "block_recursive", True):
        return _deny(path, operation,
                     "recursive operation blocked", "recursive")

    # --- 8: the run's own workspace is always its own ------------------------
    if workspace and is_below(resolved, workspace):
        return _allow(path, operation)

    # --- 11/12: an explicitly whitelisted root, which may be read-only -------
    whitelist = list(getattr(policy, "fs_whitelist", ()) or ())
    for root in whitelist:
        if not root:
            continue
        if not is_below(resolved, root):
            continue
        if operation in WRITE_OPS or operation in DESTRUCTIVE_OPS \
                or operation in RECURSIVE_OPS:
            if getattr(policy, "whitelist_read_only", True):
                return _deny(path, operation,
                             "whitelisted path is read-only", "whitelist:ro")
            if not getattr(policy, "allow_writes", False):
                return _deny(path, operation,
                             "writes are blocked", "writes")
        return _allow(path, operation)

    # --- 9/10: outside everything, the walls decide --------------------------
    if operation in WRITE_OPS or operation in DESTRUCTIVE_OPS \
            or operation in RECURSIVE_OPS:
        if not getattr(policy, "allow_writes", False):
            return _deny(path, operation, "writes are blocked outside the "
                                          "sandbox", "write_wall")
        return _deny(path, operation, "outside the sandbox", "write_wall")
    if not getattr(policy, "allow_read_outside", False):
        return _deny(path, operation, "outside the sandbox", "read_wall")
    return _allow(path, operation)


def check_call(target, policy=None, workspace=""):
    """Check a *named call* (``os.remove``, ``shutil.rmtree``) rather than a path.

    The runner uses this to refuse the dangerous functions before they are even
    reached, which is what makes the block work for a call whose argument the
    guard cannot see (for example one built at run time).
    """
    name = (target or "").strip()
    if getattr(policy, "block_destructive", True):
        for call in DESTRUCTIVE_CALLS:
            if name.endswith(call) or name == call:
                return _deny(workspace, name,
                             "destructive call blocked", "destructive:%s" % call)
    if getattr(policy, "block_recursive", True):
        for call in RECURSIVE_CALLS:
            if name.endswith(call) or name == call:
                return _deny(workspace, name,
                             "recursive call blocked", "recursive:%s" % call)
    return FsVerdict(True, workspace, name)


# ---------------------------------------------------------------------------
# A pseudo filesystem, a shadow copy and a snapshot
# ---------------------------------------------------------------------------
def pseudo_fs(workspace, roots=None):
    """A private tree that looks like a filesystem root.

    A script that insists on writing to `/tmp/out.txt` or `C:\\data\\out.txt`
    is given a real directory inside the workspace instead, so the write
    succeeds without ever leaving the sandbox. Returns the mapping, which the
    runner installs when `pseudo_root` is on.
    """
    mapping = {}
    base = os.path.join(workspace, "_pseudo")
    for root in roots or ("/tmp", "/var/tmp", "/data", "C:\\data", "C:\\temp"):
        # The virtual path becomes a real directory name, with the separators
        # and the drive colon folded out so any virtual root is representable.
        slug = re.sub(r"[^A-Za-z0-9]+", "_", str(root)).strip("_") or "root"
        mapping[str(root)] = os.path.join(base, slug)
    return mapping


def map_pseudo_path(path, mapping):
    """The real path a virtual path resolves to (or "" for no mapping).

    The longest matching virtual root wins, so `/var/tmp` is preferred over
    `/var` when both are mapped.
    """
    text = str(path or "")
    if not text or not mapping:
        return ""
    best = ""
    for virtual in mapping:
        if text == virtual or text.startswith(virtual.rstrip("/\\") +
                                              os.sep) or \
                text.startswith(virtual + "/") or text.startswith(virtual + "\\"):
            if len(virtual) > len(best):
                best = virtual
    if not best:
        return ""
    tail = text[len(best):].lstrip("/\\")
    return os.path.join(mapping[best], tail.replace("\\", os.sep)
                        .replace("/", os.sep))


def shadow_copy(path, workspace, keep_name=True):
    """Copy a real file into the workspace and return the copy's path.

    This is how a run is allowed to *edit* a file it may only read: the run
    edits the copy, and the original is never open for writing. Returns ``""``
    when the source cannot be copied, so a caller can fall back.
    """
    resolved = str(path or "")
    if not resolved or not os.path.isfile(resolved):
        return ""
    shadow_dir = os.path.join(workspace, "_shadow")
    try:
        os.makedirs(shadow_dir, exist_ok=True)
    except OSError:
        return ""
    name = os.path.basename(resolved) if keep_name else "copy"
    # Keep the tail of the path so two files with the same base name do not
    # overwrite each other's shadow.
    stem = re.sub(r"[^A-Za-z0-9]+", "_", os.path.dirname(resolved))[-40:]
    target = os.path.join(shadow_dir, (stem + "_" + name).strip("_"))
    try:
        with open(resolved, "rb") as source:
            data = source.read()
        with open(target, "wb") as handle:
            handle.write(data)
    except OSError:
        return ""
    return target


def file_digest(path):
    """The SHA-256 of a file (or "" when it cannot be read)."""
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(HASH_CHUNK), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except (OSError, ValueError):
        return ""


def snapshot(root, limit=20000):
    """Record size, mtime and hash for every file below `root`.

    Comparing two snapshots taken around a run is what *proves* the run's
    effect on disk, rather than trusting that the guards were hit.
    """
    entries = {}
    if not root or not os.path.isdir(root):
        return entries
    for base, _dirs, files in os.walk(root):
        for name in files:
            if len(entries) >= limit:
                return entries
            full = os.path.join(base, name)
            try:
                stats = os.stat(full)
            except OSError:
                continue
            entries[os.path.relpath(full, root).replace("\\", "/")] = {
                "size": stats.st_size,
                "mtime": int(stats.st_mtime),
                "hash": file_digest(full),
            }
    return entries


def compare_snapshots(before, after):
    """What changed between two snapshots: ``(added, removed, changed)``."""
    before = before or {}
    after = after or {}
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(
        name for name in set(before) & set(after)
        if before[name].get("hash") != after[name].get("hash")
        or before[name].get("size") != after[name].get("size"))
    return added, removed, changed


# ---------------------------------------------------------------------------
# Describing the rules
# ---------------------------------------------------------------------------
def describe(workspace, policy):
    """A JSON-friendly description of the filesystem rules a run obeys."""
    whitelist = [workspace] + [root for root in
                               (getattr(policy, "fs_whitelist", ()) or [])
                               if root]
    return {
        "workspace": workspace,
        "whitelist": whitelist,
        "blacklist": list(getattr(policy, "fs_blacklist", ()) or []),
        "read_wall": not getattr(policy, "allow_read_outside", False),
        "write_wall": not getattr(policy, "allow_writes", False),
        "block_destructive": bool(getattr(policy, "block_destructive", True)),
        "block_recursive": bool(getattr(policy, "block_recursive", True)),
        "block_system_dirs": bool(getattr(policy, "block_system_dirs", True)),
        "whitelist_read_only": bool(
            getattr(policy, "whitelist_read_only", True)),
        "blocked_calls": list(DESTRUCTIVE_CALLS),
        "recursive_calls": list(RECURSIVE_CALLS),
        "system_dirs": list(SYSTEM_DIRS_WINDOWS if os.name == "nt"
                            else SYSTEM_DIRS_POSIX),
        "pseudo_root": bool(getattr(policy, "pseudo_root", False)),
        "shadow_copy": bool(getattr(policy, "shadow_copy", False)),
        "snapshot_fs": bool(getattr(policy, "snapshot_fs", False)),
    }


def format_fs_rules(report):
    """The filesystem rules as ASCII lines (policy view and run report)."""
    if not report:
        return ["(no filesystem rules)"]
    flags = (
        ("read wall", report.get("read_wall")),
        ("write wall", report.get("write_wall")),
        ("destructive ops", report.get("block_destructive")),
        ("recursive ops", report.get("block_recursive")),
        ("system dirs", report.get("block_system_dirs")),
        ("pseudo FS", report.get("pseudo_root")),
        ("shadow copy", report.get("shadow_copy")),
        ("FS snapshot", report.get("snapshot_fs")),
    )
    lines = ["Filesystem isolation", "---------------------"]
    lines.append("workspace  : %s" % (report.get("workspace") or "-"))
    lines.append("whitelist  : %d root(s)" % len(report.get("whitelist", [])))
    for root in report.get("whitelist", [])[1:]:
        lines.append("             + %s" % root)
    lines.append("blacklist  : %d entr(ies)" % len(report.get("blacklist", [])))
    for name, value in flags:
        lines.append("%-11s: %s" % (name, "blocked" if value else "off"))
    lines.append("blocked calls: %d" % len(report.get("blocked_calls", [])))
    lines.append("system dirs  : %d" % len(report.get("system_dirs", [])))
    return lines


def check_script(paths, operations, workspace="", policy=None):
    """Run a batch of checks; returns ``(allowed, refused)`` verdict lists.

    Handy for a test or a report that wants to show the wall from both sides
    without repeating the policy setup.
    """
    allowed, refused = [], []
    for path, operation in zip(paths, operations):
        verdict = check(path, operation, workspace, policy)
        (allowed if verdict.allowed else refused).append(verdict)
    return allowed, refused


def fs_capabilities():
    """Describe the filesystem isolation for the audit / about layer."""
    return {
        "whitelist": True,
        "blacklist": True,
        "read_wall": True,
        "write_wall": True,
        "block_destructive": True,
        "block_recursive": True,
        "block_system_dirs": True,
        "pseudo_fs": True,
        "shadow_copy": True,
        "snapshot": True,
        "destructive_calls_blocked": len(DESTRUCTIVE_CALLS),
        "recursive_calls_blocked": len(RECURSIVE_CALLS),
        "system_dirs_blocked": len(SYSTEM_DIRS_WINDOWS if os.name == "nt"
                                   else SYSTEM_DIRS_POSIX),
        "offline": True,
    }
