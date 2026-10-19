# -*- coding: utf-8 -*-
"""The interpreters a sandboxed script can run under.

The sandbox has to run scripts in more than one language, and that is not a
detail of the launcher: each language brings its own way of being started, its
own way of being kept on a short leash, and its own way of quietly reaching
back into the host. This module owns all three.

    INTERPRETERS     one table entry per supported language: how it is found,
                     how it is started, and what its environment must set
    resolve          pick the entry for a script's language and find its binary
    build_command    the argv for one run, with the language's guard flags

Two things make this safe rather than merely convenient:

    * every entry declares its own `guard_flags`, which are the arguments that
      put *that* interpreter into a restricted mode (`-I -B` for Python,
      `-NoProfile -NonInteractive` for PowerShell, and so on). A language whose
      guards are weaker says so in the table instead of being quietly treated
      like the others.
    * a language whose binary is missing is not a failure: `resolve` reports it
      as unavailable and the caller decides, because "there is no Java here" is
      a fact about the machine, not an error in the sandbox.

The module is standard library, ASCII-only and cross-platform, and it never
executes anything: it describes how a run *would* be started, so the report and
the launcher cannot disagree about the command.
"""

import os
import sys

# ---------------------------------------------------------------------------
# How strictly each language can be constrained
# ---------------------------------------------------------------------------
# The sandbox is only as strong as the interpreter's own guards. Rather than
# pretend otherwise, each entry carries the level it can actually be held to:
#
#   "strong"   the interpreter starts in an isolated mode that drops the user's
#              paths, caches and startup files, and the sandbox's in-process
#              guards cover the language's own escape hatches
#   "moderate" the interpreter can be started without a profile or interactive
#              input, and its environment can be replaced, but it has no mode
#              that removes the user's configuration entirely
#   "weak"     the language has no isolation mode at all (a shell or a batch
#              file is a program *made of* host commands); the sandbox stops it
#              by refusing the files, the network and the child processes it
#              would need, rather than by containing the interpreter
GUARD_LEVELS = ("strong", "moderate", "weak")

# ---------------------------------------------------------------------------
# The languages
# ---------------------------------------------------------------------------
# (key, label, aliases, candidates, guard_flags, script_suffix, guard_level,
#  how the source file is written)
#
# `candidates` are the executable names to look for, in order. They are resolved
# against the isolated PATH (never the host's), so a run cannot pick up a
# binary the terminal happens to have first on its own PATH.
INTERPRETERS = (
    ("python", "Python", ("python", "python3", "py"),
     ("python.exe", "python3.exe", "python3", "python"), ("-I", "-B"),
     ".py", "strong",
     "the script is executed directly by the isolated interpreter"),

    ("node", "Node.js", ("node", "nodejs", "javascript", "js"),
     ("node.exe", "node"), ("--no-warnings",),
     ".js", "moderate",
     "Node has no isolated mode; the run gets a private HOME and NODE_PATH "
     "and is refused the modules it would need to reach the host"),

    ("java", "Java", ("java", "jar"),
     ("java.exe", "java"), (),
     ".java", "moderate",
     "Java is started with the source file, so no classpath from the host is "
     "used; the run is refused the network and the files outside its workspace"),

    ("cpp", "C++", ("cpp", "c++", "c", "gcc", "clang"),
     ("g++.exe", "clang++.exe", "g++", "clang++"), (),
     ".cpp", "weak",
     "C++ has to be compiled before it runs, which is itself a child process; "
     "the run may compile and execute only inside its workspace, and the "
     "compiler is reached through the isolated PATH alone"),

    ("powershell", "PowerShell", ("powershell", "pwsh", "ps1"),
     ("powershell.exe", "pwsh.exe", "pwsh"), (
         "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass"),
     ".ps1", "moderate",
     "PowerShell runs without a profile and without an interactive prompt, so "
     "the user's own configuration is never loaded; the policy still decides "
     "what the script may touch"),

    ("batch", "Batch", ("batch", "bat", "cmd"),
     ("cmd.exe", "cmd"), ("/D", "/Q", "/C"),
     ".bat", "weak",
     "a batch file *is* a sequence of host commands, so it has no guards of "
     "its own: it is contained by the files, the network and the children the "
     "policy refuses, never by the interpreter"),

    ("vbs", "VBScript", ("vbs", "vbscript", "vbs"),
     ("cscript.exe", "wscript.exe"), ("//Nologo", "//B"),
     ".vbs", "weak",
     "VBScript is run by the script host with banners and prompts off; as with "
     "batch, the wall is the policy rather than the interpreter"),
)

INTERPRETER_KEYS = tuple(key for key, _l, _a, _c, _g, _s, _gl, _n
                         in INTERPRETERS)
INTERPRETER_LABELS = {key: label for key, label, _a, _c, _g, _s, _gl, _n
                      in INTERPRETERS}
INTERPRETER_GUARD = {key: level for key, _l, _a, _c, _g, _s, level, _n
                     in INTERPRETERS}
INTERPRETER_SUFFIX = {key: suffix for key, _l, _a, _c, _g, suffix, _gl, _n
                      in INTERPRETERS}
INTERPRETER_NOTES = {key: note for key, _l, _a, _c, _g, _s, _gl, note
                     in INTERPRETERS}

# The interpreter used when a script's language is unknown or absent. Python is
# the sandbox's own language: the in-process guards (the import filter, the
# path rules, the syscall filter) are written for it, which makes it the
# strongest default rather than merely the most available one.
DEFAULT_INTERPRETER = "python"


def _alias_map():
    """``{alias: key}`` for every spelling a language may arrive under."""
    mapping = {}
    for key, _label, aliases, _candidates, _guards, _suffix, _level, _note \
            in INTERPRETERS:
        mapping[key] = key
        for alias in aliases:
            mapping[alias.lower()] = key
    return mapping


ALIASES = _alias_map()


def resolve(language, which=None):
    """The interpreter entry for a script's language, with its binary found.

    Returns a dict so a caller can serialise it into a report without knowing
    the table's shape::

        {key, label, language, binary, available, guard_flags, guard_level,
         script_suffix, notes, searched}

    An unknown language resolves to the default rather than failing, and a
    language whose binary is missing comes back with ``available: False`` and
    the names that were searched, so a report can say *why* it was unavailable.
    """
    key = ALIASES.get(str(language or "").strip().lower(), DEFAULT_INTERPRETER)
    entry = next(item for item in INTERPRETERS if item[0] == key)
    _key, label, _aliases, candidates, guards, suffix, level, note = entry

    binary, searched = _find(candidates, which)
    return {
        "key": key,
        "label": label,
        "language": str(language or ""),
        "binary": binary,
        "available": bool(binary),
        "guard_flags": list(guards),
        "guard_level": level,
        "script_suffix": suffix,
        "notes": note,
        "searched": searched,
    }


def _find(candidates, which):
    """The first existing binary among `candidates`, and what was searched.

    `which` is injectable so a test can assert the search without a particular
    interpreter being installed, which is the difference between a test that
    works everywhere and one that works on the machine it was written on.
    """
    searched = []
    finder = which or _default_which
    for name in candidates:
        searched.append(name)
        found = finder(name)
        if found:
            return found, searched
    return "", searched


def _default_which(name):
    """Locate one executable on the isolated PATH (never the host's PATH).

    The isolated PATH is used deliberately: resolving against the terminal's
    PATH would let a run pick up a binary the user happens to have, which is
    exactly the kind of inheritance the isolation is meant to remove.
    """
    from tabs import sandbox_isolation
    search = sandbox_isolation.isolated_path("")
    existing = os.environ.get("PATH", "")
    try:
        os.environ["PATH"] = search
        return _which(name)
    finally:
        os.environ["PATH"] = existing


def _which(name):
    """A tiny `which`, written by hand so PATH never leaks into a shell."""
    if not name:
        return ""
    if os.path.isabs(name) or os.sep in name:
        return name if os.path.isfile(name) else ""
    path = os.environ.get("PATH", "")
    extensions = [""]
    if os.name == "nt":
        extensions += [".exe", ".cmd", ".bat"]
    for directory in path.split(os.pathsep):
        if not directory:
            continue
        for extension in extensions:
            candidate = os.path.join(directory, name + extension)
            if os.path.isfile(candidate):
                return candidate
    return ""


def build_command(run, script_path, isolated=True):
    """The argv for one run, given a resolved interpreter and a script path.

    `run` is the dict :func:`resolve` returns. The guard flags come *before*
    the script, which is where every one of these interpreters expects them, and
    the script path is quoted nowhere: the command is an argv list, so a path
    with a space needs no escaping and a shell is never involved.
    """
    binary = (run or {}).get("binary") or ""
    if not binary:
        return []
    if not isolated:
        return [binary, script_path]
    return [binary] + list((run or {}).get("guard_flags") or []) + [script_path]


def needs_wrapper(run):
    """True when the language cannot be guarded by its own interpreter alone.

    A `weak` language is started under the sandbox's own runner wherever one is
    available, so the in-process guards (the import filter, the path rules, the
    syscall filter) still apply to the process that launches it. Where no
    wrapper exists, the run proceeds and the report says the guard level, which
    is the honest outcome: the policy still bounds it, but the interpreter does
    not help.
    """
    return (run or {}).get("guard_level") == "weak"


def format_interpreters(rows):
    """Resolved interpreters as ASCII lines (the report and the CLI)."""
    lines = ["Interpreters", "------------"]
    if not rows:
        return lines + ["(none resolved)"]
    for row in rows:
        lines.append("%-11s %-10s %s" % (
            row.get("key", "?"),
            row.get("guard_level", "?"),
            row.get("binary") or "(not found: %s)"
            % ", ".join(row.get("searched", [])[:3])))
        lines.append("            %s" % row.get("notes", ""))
    return lines


def interpreter_capabilities():
    """Describe the supported languages for the audit / about layer."""
    return {
        "languages": list(INTERPRETER_KEYS),
        "labels": dict(INTERPRETER_LABELS),
        "guard_levels": dict(INTERPRETER_GUARD),
        "guard_level_names": list(GUARD_LEVELS),
        "default": DEFAULT_INTERPRETER,
        "suffixes": dict(INTERPRETER_SUFFIX),
        "cross_platform": True,
        "zero_dependency": True,
        "ascii_only": True,
        "offline": True,
    }
