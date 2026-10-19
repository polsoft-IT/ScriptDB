# -*- coding: utf-8 -*-
"""Verification of the sandbox's process-isolation guarantees.

Run:  python run_verifications.py isolation

Asserts the twelve things a script's isolation is supposed to mean, and proves
the ones that can be proved by running something rather than by restating the
design: a real run is started and its own view of the world is read back.
"""
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tabs import sandbox_isolation as iso
from tabs import sandbox_langs as langs
from tabs.sandbox import SandboxPolicy, run_headless

failures = []


def check(label, ok, detail=""):
    line = ("  OK   " if ok else "  FAIL ") + label + (
        "   <%s>" % detail if detail and not ok else "")
    print(str(line).encode("ascii", "replace").decode("ascii"))
    if not ok:
        failures.append(label)


print("1. process and its envelope")
policy = SandboxPolicy()
env = iso.build_environment("/tmp/ws", policy)
report = iso.describe("/tmp/ws", policy)
check("own process: stdin closed, no shared memory",
      report["process"]["separate_process"] is True
      and report["process"]["memory_shared_with_terminal"] is False)
check("own environment: not inherited from the terminal",
      all(name not in env for name in ("PYTHONPATH", "VIRTUAL_ENV", "SDB_APP")))
check("own environment: built from the allow-list only",
      report["environment"]["inherits_terminal"] is False)
check("own working directory: set to the workspace",
      report["working_directory"]["value"] == "/tmp/ws")
check("own PATH: never the terminal's",
      report["path"]["inherits_terminal"] is False
      and os.environ.get("PATH", "x") not in env["PATH"])
check("own variables: a fresh mapping per call",
      iso.build_environment("/tmp/ws", policy)
      is not iso.build_environment("/tmp/ws", policy))

print()
print("2. separate interpreter per language")
check("all seven languages are declared",
      set(langs.INTERPRETER_KEYS) == {"python", "node", "java", "cpp",
                                      "powershell", "batch", "vbs"})
for key in langs.INTERPRETER_KEYS:
    row = langs.resolve(key)
    check("  language '%s' resolves with a guard level" % key,
          row["key"] == key and row["guard_level"] in langs.GUARD_LEVELS)
check("an unknown language falls back to Python",
      langs.resolve("cobol")["key"] == "python")
check("Python is given the isolated-mode flags",
      langs.resolve("python")["guard_flags"] == ["-I", "-B"])
check("PowerShell is given the no-profile flags",
      "-NoProfile" in langs.resolve("powershell")["guard_flags"])
check("a weak language is marked as needing hosting",
      langs.needs_wrapper(langs.resolve("batch")) is True
      and langs.needs_wrapper(langs.resolve("python")) is False)
check("a command is built as an argv list, not a shell string",
      isinstance(langs.build_command(langs.resolve("python"), "/w/s.py"), list))

print()
print("3. security profile")
profile = iso.security_profile("strict", policy, langs.resolve("python"))
check("the profile names seven separations", profile["total"] == 7)
check("a complete profile holds every separation",
      profile["complete"] is True,
      str([s["key"] for s in profile["separations"] if not s["ok"]]))
check("every separation carries its evidence",
      all(item["evidence"] for item in profile["separations"]))
check("the profile records the interpreter and its guard mode",
      profile["interpreter"] == "python" and profile["guard_level"] == "strong")
check("an unresolved interpreter is a gap, not a false pass",
      iso.security_profile("strict", policy, {})["complete"] is False)
check("the profile renders as ASCII",
      iso.format_profile(profile)[0] == "Security profile")

print()
print("4. no access to the terminal")
terminal = report["terminal_access"]
check("memory: denied", terminal["memory"] is False)
check("modules: denied by name",
      "tabs" in terminal["modules"] and "PyQt5" in terminal["modules"])
check("files: denied by name", "scripts_db.json" in terminal["files"])
check("configuration: denied by directory",
      "sdb_config" in terminal["config_dirs"])
check("processes: denied", terminal["processes"] is False)
check("a terminal file path is refused",
      iso.path_is_terminal_file("C:/app/scripts_db.json") is True)
check("a sandbox path is not refused",
      iso.path_is_terminal_file("/tmp/ws/scripts_db.json", "/tmp/ws") is False)

print()
print("5. the guarantees, demonstrated by a real run")
WORKSPACE = tempfile.mkdtemp(prefix="verify-sandbox-")
PROBE = (
    "import os, sys\n"
    "print('PID', os.getpid())\n"
    "print('CWD', os.getcwd())\n"
    "print('PATH', os.environ.get('PATH', ''))\n"
    "print('HOME', os.environ.get('HOME', ''))\n"
    "print('PYTHONPATH', os.environ.get('PYTHONPATH', '(unset)'))\n"
    "print('NOSITE', str(bool(sys.flags.no_user_site)))\n"
    "print('ISOLATED', str(bool(sys.flags.isolated)))\n"
    "try:\n"
    "    import ctypes\n"
    "    print('CTYPES', 'REACHED')\n"
    "except ImportError:\n"
    "    print('CTYPES', 'blocked')\n"
    "try:\n"
    "    import mmap\n"
    "    print('MMAP', 'REACHED')\n"
    "except ImportError:\n"
    "    print('MMAP', 'blocked')\n"
    "try:\n"
    "    import tabs\n"
    "    print('TABS', 'REACHED')\n"
    "except ImportError:\n"
    "    print('TABS', 'blocked')\n"
    "try:\n"
    "    import signal\n"
    "    print('SIGNAL', 'REACHED')\n"
    "except ImportError:\n"
    "    print('SIGNAL', 'blocked')\n"
    "try:\n"
    "    open('scripts_db.json')\n"
    "    print('TERMFILE', 'REACHED')\n"
    "except Exception:\n"
    "    print('TERMFILE', 'blocked')\n"
    "try:\n"
    "    import subprocess\n"
    "    print('SUBPROC', 'REACHED')\n"
    "except ImportError:\n"
    "    print('SUBPROC', 'blocked')\n"
    "print('DONE')\n"
)

# The strict preset is used for the demonstration: the default policy
# deliberately *allows* child processes and the network, and those are policy
# choices rather than isolation promises.
strict = SandboxPolicy()
strict.apply_preset("strict")
strict = strict.clamped()
run = run_headless(PROBE, policy=strict, script="probe.py",
                   workspace=WORKSPACE)
seen = {}
for line in run.output.splitlines():
    parts = line.split(" ", 1)
    if len(parts) == 2:
        seen[parts[0]] = parts[1].strip()

check("the run completed", "DONE" in run.output, run.output[-300:])
check("child PID differs from this process",
      seen.get("PID") not in ("", str(os.getpid())))
check("child CWD is the workspace",
      seen.get("CWD") == WORKSPACE or "verify-sandbox-" in seen.get("CWD", ""))
check("child PATH is not the terminal's",
      os.environ.get("PATH", "x") not in seen.get("PATH", ""))
check("child HOME is the workspace", seen.get("HOME") == WORKSPACE)
check("child PYTHONPATH is unset", seen.get("PYTHONPATH") == "(unset)")
check("child runs in isolated mode", seen.get("ISOLATED") == "True")
check("child has no user site", seen.get("NOSITE") == "True")
check("terminal memory reach is blocked (ctypes)",
      seen.get("CTYPES") == "blocked")
check("terminal memory reach is blocked (mmap)", seen.get("MMAP") == "blocked")
check("terminal modules are blocked", seen.get("TABS") == "blocked")
check("terminal files are refused", seen.get("TERMFILE") == "blocked")
check("child processes are blocked", seen.get("SUBPROC") == "blocked")
check("signals are blocked", seen.get("SIGNAL") == "blocked")
check("the run recorded its interpreter",
      run.interpreter.get("key") == "python")
check("the run recorded its security profile",
      run.profile.get("total") == 7)
check("the recorded profile is complete", run.profile.get("complete") is True)

print()
print("6. a non-Python language is launched by its own interpreter")
node = langs.resolve("node")
if node["available"]:
    js = run_headless("console.log('hello from node')",
                      policy=SandboxPolicy(language="node"),
                      script="probe.js", language="node")
    check("a Node script runs under Node",
          js.interpreter.get("key") == "node"
          and "hello from node" in js.output, js.output[-200:])
else:
    check("Node unavailable is reported, not assumed",
          node["available"] is False and bool(node["searched"]))

batch = langs.resolve("batch")
if batch["available"]:
    result = run_headless("echo hello from batch",
                          policy=SandboxPolicy(language="batch"),
                          script="probe.bat", language="batch")
    check("a batch script runs under cmd.exe",
          result.interpreter.get("key") == "batch"
          and "hello from batch" in result.output, result.output[-200:])
else:
    check("cmd.exe unavailable is reported, not assumed",
          batch["available"] is False)

print()
print("%d failure(s)" % len(failures))
sys.exit(1 if failures else 0)
