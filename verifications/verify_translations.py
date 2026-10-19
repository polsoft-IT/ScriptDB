# -*- coding: utf-8 -*-
"""Verify that retranslate reaches every control on both tabs.

Run:  python run_verifications.py translations

This presses the button rather than looking for the call: it applies a table
that renames every label, then asserts no control still carries its English
fallback. A `setText` call that exists but assigns the wrong widget passes a
greppable check and fails this one -- which is exactly how two missing
translations were found.
"""
import os
import re
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5 import QtWidgets

from tabs import (debug_steps, sandbox_audit, sandbox_integration,
                  sandbox_monitor)
from tabs.debugger import DebuggerTab
from tabs.sandbox import SandboxTab

failures = []


def check(label, ok, detail=""):
    line = ("  OK   " if ok else "  FAIL ") + label + (
        "   <%s>" % detail if detail and not ok else "")
    print(str(line).encode("ascii", "replace").decode("ascii"))
    if not ok:
        failures.append(label)


class FakeWindow(QtWidgets.QWidget):
    def __init__(self, base):
        super().__init__()
        self.base_dir = base
        self.scripts = [{"name": "demo.py", "category": "Python",
                         "code": "x = 1\n"}]

    def scripts_only(self):
        return list(self.scripts)

    def notify_audit(self, event, **payload):
        return 1

    def t(self, key, fallback=None, **kwargs):
        text = fallback or key
        if kwargs:
            try:
                return text.format(**kwargs)
            except (KeyError, IndexError, ValueError):
                return text
        return text


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

with tempfile.TemporaryDirectory() as tmp:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = FakeWindow(tmp)

    keys = set()
    for path in ("tabs/debugger.py", "tabs/sandbox.py"):
        with open(os.path.join(ROOT, path), "r", encoding="utf-8") as handle:
            text = handle.read()
        keys.update(re.findall(r'label\("([a-z0-9_]+)"', text))
        keys.update(re.findall(r'self\.t\("([a-z0-9_]+)"', text))

    # The keys the *tables* name. They never appear as a literal, so a table
    # built from the literals alone would miss them and report a working
    # translation as a failure.
    for _key, label_key, _f, _m in debug_steps.STEP_MODES:
        keys.add(label_key)
    for _key, label_key, _f, _m in debug_steps.REPLAY_MODES:
        keys.add(label_key)
    for _key, label_key, _f, _m in sandbox_integration.SCOPES:
        keys.add(label_key)
    for _key, label_key, _f, _m in sandbox_integration.SHADOW_LAYERS:
        keys.add(label_key)
    for _key, label_key, _f, _m, _x in sandbox_integration.INTEGRATIONS:
        keys.add(label_key)
    for _key, label_key, _f, _d in sandbox_audit.REPORTS:
        keys.add(label_key)
    for _key, label_key, _f, _u, _c in sandbox_monitor.LIMIT_ITEMS:
        keys.add(label_key)
    for _key, label_key, _f, _u in sandbox_monitor.MONITOR_ITEMS:
        keys.add(label_key)
    for widget in ("flow_save", "rewind", "snapshot", "restore",
                   "snapshot_diff", "replay", "start_session"):
        keys.add("debugger_" + widget)
    for widget in ("show_report", "save_audit", "integrations", "premium",
                   "persistent", "shadow_env_chk", "shadow_net_chk",
                   "monitor_runtime", "scope", "scope_name"):
        keys.add("sandbox_" + widget)

    # A table that renames every label it is given, so any control left at its
    # English fallback is one the tab never reached.
    table = {key: "XX_" + key for key in keys}

    debugger_tab = DebuggerTab(window)
    sandbox_tab = SandboxTab(window)
    debugger_tab.retranslate(table)
    sandbox_tab.retranslate(table)

    def reached(widget):
        """True when a widget shows a renamed label rather than its fallback."""
        try:
            text = widget.text()
        except AttributeError:
            return False
        return "XX_" in text

    print("debugger tab")
    for name in ("btn_start_session", "btn_rewind", "btn_snapshot",
                 "btn_restore", "btn_snapshot_diff", "btn_replay",
                 "btn_flow_save", "btn_refresh", "btn_clear", "btn_add_bp",
                 "btn_validate", "btn_record"):
        widget = getattr(debugger_tab, name, None)
        if widget is None:
            check("  %s exists" % name, False)
            continue
        check("  %s was retranslated" % name, reached(widget), widget.text())
    for mode, button in debugger_tab.step_buttons.items():
        check("  step button %r was retranslated" % mode,
              "XX_" in button.text(), button.text())
    for index in range(debugger_tab.replay_combo.count()):
        check("  replay %d was retranslated" % index,
              "XX_" in debugger_tab.replay_combo.itemText(index),
              debugger_tab.replay_combo.itemText(index))

    print()
    print("sandbox tab")
    for name in ("btn_run", "btn_scan", "btn_save_report", "btn_show_report",
                 "btn_save_audit", "btn_integrations", "btn_premium"):
        widget = getattr(sandbox_tab, name, None)
        if widget is None:
            check("  %s exists" % name, False)
            continue
        check("  %s was retranslated" % name, reached(widget), widget.text())
    for combo_name, combo in (("scope_combo", sandbox_tab.scope_combo),
                              ("report_combo", sandbox_tab.report_combo)):
        for index in range(combo.count()):
            check("  %s item %d was retranslated" % (combo_name, index),
                  "XX_" in combo.itemText(index), combo.itemText(index))

    # The tabs hold session and repository files open; releasing them keeps the
    # cleanup a test of the code rather than of the operating system.
    try:
        debugger_tab.session.path = ""
    except Exception:
        pass

print()
print("%d failure(s)" % len(failures))
sys.exit(1 if failures else 0)
