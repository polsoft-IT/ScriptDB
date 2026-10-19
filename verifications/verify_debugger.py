# -*- coding: utf-8 -*-
"""Verification of the debugger's stepping engine and its added features.

Run:  python run_verifications.py debugger

Exercises the five step modes, conditional breakpoints, hit counts, snapshots
and their diff, replay in three modes, and the head-less API and CLI.
"""
import json
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5 import QtWidgets

from tabs import code_flow, debug_steps, debugger
from tabs.debugger import DebuggerTab

failures = []


def check(label, ok, detail=""):
    line = ("  OK   " if ok else "  FAIL ") + label + (
        "   <%s>" % detail if detail and not ok else "")
    print(str(line).encode("ascii", "replace").decode("ascii"))
    if not ok:
        failures.append(label)


SCRIPT = {
    "name": "demo.py",
    "category": "Python",
    "code": (
        "import os\n"
        "\n"
        "def load(path):\n"
        "    data = open(path).read()\n"
        "    return data\n"
        "\n"
        "def run():\n"
        "    text = load('a.txt')\n"
        "    for line in text.splitlines():\n"
        "        print(line)\n"
        "    return len(text)\n"
        "\n"
        "run()\n"
    ),
}


class FakeWindow(QtWidgets.QWidget):
    def __init__(self, base):
        super().__init__()
        self.base_dir = base
        self._scripts = [dict(SCRIPT)]
        self.events = []

    def scripts_only(self):
        return list(self._scripts)

    def notify_audit(self, event, **payload):
        self.events.append((event, payload))
        return 1

    def t(self, key, fallback=None, **kwargs):
        text = fallback or key
        if kwargs:
            try:
                return text.format(**kwargs)
            except (KeyError, IndexError, ValueError):
                return text
        return text


print("1. the stepping engine")
recording = code_flow.record(SCRIPT)
events = recording.events
check("the recorder produced a timeline", len(events) > 0)

runner = debug_steps.SessionRunner(events=events)
first = runner.position()
runner.step("into")
second = runner.position()
check("step into reaches the very next event",
      second["index"] == first["index"] + 1,
      "%s -> %s" % (first["index"], second["index"]))
check("step over moves", runner.step("over") is not None)
before_out = runner.position()
runner.step("out")
after_out = runner.position()
check("step out moves shallower, never deeper",
      after_out["depth"] <= before_out["depth"],
      "%s -> %s" % (before_out["depth"], after_out["depth"]))

print()
print("2. conditional breakpoints")
EVENT = {"line": 42, "depth": 2, "kind": "call", "name": "load", "block": "if"}
cases = (("line > 40", True), ("line < 40", False), ("depth >= 2", True),
         ("kind == call", True), ("kind == loop", False),
         ("name == load", True), ("line > 40 and depth == 2", True),
         ("line > 99 and depth == 2", False), ("line > 99 or depth == 2", True),
         ("true", True), ("false", False), ("kind", True), ("", True))
for expression, expected in cases:
    ok, _why = debug_steps.eval_condition(expression, EVENT)
    check("condition %r evaluates to %s" % (expression, expected),
          ok == expected)
ok, why = debug_steps.eval_condition("nonsense ?? 1", EVENT)
check("an unparseable condition stops rather than silently passing",
      ok is True and "stops" in why)
check("the evaluator never calls eval",
      "eval(" not in open(os.path.join(
          os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
          "tabs", "debug_steps.py"), encoding="utf-8").read())

print()
print("3. hit counts and stopping")
bp = {"script": "demo.py", "kind": "line", "line": events[3].line,
      "condition": "depth >= 0", "enabled": True}
runner = debug_steps.SessionRunner(events=events, breakpoints=[bp])
runner.rewind()
check("the breakpoint is a stop", runner.stops_at(3))
landed = None
for _ in range(len(events)):
    event = runner.step("into")
    if event is None:
        break
    if runner.position()["line"] == events[3].line:
        landed = runner.position()
        break
check("stepping onto the breakpoint lands on it", landed is not None)
check("the hit is counted once", sum(runner.hits.values()) == 1, str(runner.hits))
runner2 = debug_steps.SessionRunner(
    events=events, breakpoints=[dict(bp, enabled=False)])
runner2.rewind()
check("a disabled breakpoint does not stop",
      runner2.run_to_breakpoint(limit=50) is None)
runner3 = debug_steps.SessionRunner(
    events=events, breakpoints=[dict(bp, condition="line < 0")])
runner3.rewind()
check("a false condition does not stop",
      runner3.run_to_breakpoint(limit=50) is None)

print()
print("4. snapshots")
runner = debug_steps.SessionRunner(events=events)
runner.step("into")
position_at_snapshot = runner.position()
snap = runner.snapshot("first")
check("the snapshot recorded the cursor it was taken at",
      snap.index == position_at_snapshot["index"],
      "%s != %s" % (snap.index, position_at_snapshot["index"]))
runner.step("into")
runner.step("into")
check("the cursor moved after the snapshot",
      runner.position()["index"] != position_at_snapshot["index"])
runner.restore(snap)
check("restoring returns the cursor exactly",
      runner.position()["index"] == position_at_snapshot["index"])
later = runner.snapshot("later")
moved, _hits, _watches = debug_steps.diff_snapshots(snap, later)
check("the snapshot diff reports the move", "from_line" in moved)
check("a snapshot serialises and rebuilds",
      debug_steps.SessionSnapshot.from_dict(snap.to_dict()).index == snap.index)

print()
print("5. replay")
frames = debug_steps.replay(runner, "step", limit=5)
check("a step replay produces frames", len(frames) == 5, str(len(frames)))
check("a transcript replay covers the steps",
      len(debug_steps.replay(runner, "transcript")) >= 1)
check("a backwards replay produces frames",
      len(debug_steps.replay(runner, "backwards", limit=3)) == 3)
check("a replay leaves the caller's cursor where it was",
      runner.position()["index"] == later.index)
for mode in debug_steps.REPLAY_MODE_KEYS:
    check("the replay mode %r is reachable" % mode,
          isinstance(debug_steps.replay(runner, mode, limit=2), list))

print()
print("6. the tables and the head-less surface")
check("five step modes are declared", len(debug_steps.STEP_MODES) == 5)
check("three replay modes are declared", len(debug_steps.REPLAY_MODES) == 3)
check("the architecture standard has nine entries",
      len(debugger.DEBUGGER_ARCHITECTURE) == 9)
check("the architecture uses the house keys",
      set(debugger.DEBUGGER_ARCHITECTURE_KEYS) >= {
          "ascii", "hot_pluggable", "zero_dependency", "cross_platform",
          "plugin_ready", "sandbox_safe", "artefact_driven", "api_cli_gui",
          "integration"})
check("eight premium features are declared",
      len(debugger.DEBUGGER_PREMIUM) == 8)
check("eight integrations are declared",
      len(debugger.DEBUGGER_INTEGRATIONS) == 8)
check("the integration matrix covers every module",
      [row["key"] for row in debugger.debugger_integration_matrix()]
      == list(debugger.DEBUGGER_INTEGRATION_KEYS))

record = debugger.debugger_session_record(SCRIPT)
check("a head-less session record is built", record["events"] > 0)
check("it reports the step modes", len(record["step_modes"]) == 5)
api = debugger.debugger_api()
check("the API describes the frontends",
      api["frontends"] == ["api", "cli", "gui"])
check("the API lists the step modes",
      api["step_modes"] == list(debug_steps.STEP_MODE_KEYS))
for command in ("capabilities", "api", "architecture", "premium",
                "integrations", "steps", "replay", "conditions", "languages"):
    output = debugger.debugger_cli([command])
    check("the CLI runs '%s'" % command,
          isinstance(output, str) and bool(output.strip()))
check("the CLI api is valid JSON",
      json.loads(debugger.debugger_cli(["api"]))["name"] == "Debugger")
check("the CLI reports an unknown command",
      "unknown command" in debugger.debugger_cli(["nope"]))
check("the CLI session needs a file",
      "usage" in debugger.debugger_cli(["session"]))

print()
print("7. the tab, end to end")
with tempfile.TemporaryDirectory() as tmp:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = FakeWindow(tmp)
    tab = DebuggerTab(window)

    runner = tab.start_session()
    check("a session can be started from the tab", runner is not None)
    check("the stepping pane shows the position",
          "Session position" in tab.step_view.toPlainText())

    tab.line_edit.setText("4")
    tab.add_breakpoint()
    stopped = tab.run_to_breakpoint()
    check("run to breakpoint stops in the tab", stopped is not None)

    snap = tab.take_snapshot()
    check("a snapshot can be taken from the tab", snap is not None)
    tab.step("into")
    restored = tab.restore_snapshot()
    check("a snapshot can be restored from the tab",
          restored is not None and restored.index == snap.index)

    tab.take_snapshot()
    check("two snapshots can be compared", len(tab.snapshot_diff()) > 0)
    check("the tab can replay a session",
          isinstance(tab.replay_session("transcript"), list))
    check("the tab can rewind a session",
          tab.rewind_session().cursor == 0)
    check("the session was announced to the audit bus",
          any(e[0].startswith("debugger_") for e in window.events))
    check("every step mode has a button",
          set(tab.step_buttons) == set(debug_steps.STEP_MODE_KEYS))
    check("the capabilities advertise the new surface",
          set(debugger.debugger_capabilities()["step_modes"])
          == set(debug_steps.STEP_MODE_KEYS))

print()
print("%d failure(s)" % len(failures))
sys.exit(1 if failures else 0)
