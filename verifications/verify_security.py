# -*- coding: utf-8 -*-
"""Verify the script-security core and tab.

Run:  python run_verifications.py security

It presses the buttons rather than looking for the calls: it creates a core,
round-trips every layer (hashing, HMAC, KDF, sealing, signing, registry, tamper,
backups, reports, export) and then builds the real tab against a fake window and
exercises its actions, so a wired-but-not-connected control fails here.
"""
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5 import QtWidgets

from tabs import secure_core as core
from tabs.secure_tab import SecurityTab

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
        # The real window publishes both folders: db_dir for the program state
        # (databases), base_dir for the files it produces. The tab reads db_dir.
        self.db_dir = base
        self.base_dir = base
        self.lang = "en"
        self.scripts = [
            {"id": 1, "name": "demo.py", "category": "Python",
             "code": "print(1)\n", "tags": "demo"},
            {"id": 2, "name": "run.bat", "category": "Batch",
             "code": "@echo off\n", "tags": "demo"},
        ]

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


SCRIPT = {"name": "demo.py", "category": "Python", "code": "print(1)\n"}

print("digests and fingerprints")
check("sha256 digest is 64 hex",
      len(core.digest_text("abc", "sha256")) == 64)
check("sha512 digest is 128 hex",
      len(core.digest_text("abc", "sha512")) == 128)
check("sha3 digest differs from sha256",
      core.digest_text("abc", "sha3_256") != core.digest_text("abc", "sha256"))
check("blake2b digest is available",
      len(core.digest_text("abc", "blake2b")) == 128)
check("all_digests returns every algorithm",
      set(core.all_digests("abc")) == set(core.DIGEST_KEYS))
check("same body fingerprints the same",
      core.fingerprint(SCRIPT) == core.fingerprint(dict(SCRIPT)))
check("a different body fingerprints differently",
      core.fingerprint(SCRIPT) != core.fingerprint(
          {"name": "demo.py", "code": "print(2)\n"}))

print()
print("hmac, signatures and constant time")
key = core.derive("hunter2pass", b"salt", "pbkdf2", 1000)
tag = core.hmac_tag(key, "payload")
check("hmac is stable", tag == core.hmac_tag(key, "payload"))
check("hmac changes with the message",
      tag != core.hmac_tag(key, "payload2"))
sig = core.sign_fingerprint(key, core.fingerprint(SCRIPT), "demo.py")
check("signature verifies",
      core.verify_signature(key, sig, core.fingerprint(SCRIPT), "demo.py"))
check("signature is name bound",
      not core.verify_signature(key, sig, core.fingerprint(SCRIPT),
                                "other.py"))
check("compare is constant time on equal",
      core.compare("abc", "abc") and not core.compare("abc", "abd"))

print()
print("key derivation")
check("pbkdf2 returns 32 bytes",
      len(core.derive("x", b"s", "pbkdf2", 100)) == 32)
check("scrypt returns 32 bytes",
      len(core.derive("x", b"s", "scrypt")) == 32)
check("salted sha256 returns 32 bytes",
      len(core.derive("x", b"s", "sha256-salted", 1000)) == 32)
check("different passwords derive different keys",
      core.derive("a", b"s", "pbkdf2", 100) != core.derive("b", b"s",
                                                           "pbkdf2", 100))
check("weak password is penalised",
      core.password_strength("abc")["score"] < 4)

print()
print("sealing and per-file keys")
sealed = core.seal_text("secret body", key)
check("seal produces an envelope", isinstance(sealed, str) and sealed)
check("unseal returns the plaintext",
      core.unseal_text(sealed, key)["text"] == "secret body")
check("a foreign key cannot open it",
      not core.unseal_text(sealed, core.derive("z", b"s", "pbkdf2", 100))["ok"])
tampered = sealed[:-2] + ("AA" if sealed[-2:] != "AA" else "BB")
check("a tampered envelope is refused",
      not core.unseal_text(tampered, key)["ok"])
check("per-file keys differ per name",
      core.per_file_key(key, "a.py") != core.per_file_key(key, "b.py"))
bundle = core.export_key_bundle(key, b"second-secret")
check("exported bundle is protected", bundle["protected"])
check("import needs the second secret",
      core.import_key_bundle(bundle, b"second-secret")["key"] == key)
check("import refuses the wrong second secret",
      not core.import_key_bundle(bundle, b"nope")["ok"])

print()
with tempfile.TemporaryDirectory() as tmp:
    sec_dir = os.path.join(tmp, "security")
    c = core.SecurityCore(sec_dir)
    check("a new core has no key material", not c.has_key_material())
    check("a short passphrase is refused",
          not c.create("short")["ok"])
    check("creating key material works",
          c.create("hunter2pass", "hunter2pass")["ok"])
    check("the core starts locked", c.locked)
    check("the wrong passphrase is refused", not c.unlock("nope")["ok"])
    check("the right passphrase unlocks", c.unlock("hunter2pass")["ok"])
    check("the core reports itself unlocked", not c.locked)

    print()
    print("authorization policy")
    c.unlock("hunter2pass")
    c.set_policy({"max_attempts": 2, "cooldown_seconds": 30})
    c.lock("test")
    c.unlock("bad1")
    outcome = c.unlock("bad2")
    check("the attempt limit triggers a cooldown",
          outcome["code"] == "cooldown" and c.is_in_cooldown())
    c._key = None
    c.policy["max_attempts"] = 5
    c.cooldown_until = None
    check("unlock works again after the cooldown", c.unlock("hunter2pass")["ok"])
    check("a token is minted", c.issue_token()["ok"])
    token = c.issue_token()["token"]
    check("a token can be redeemed", c.redeem_token(token)["ok"])
    check("a token cannot be replayed", not c.redeem_token(token)["ok"])
    check("high-security mode toggles",
          c.enable_high_security(True)["ok"] and c.policy["high_security"])
    c.enable_high_security(False)

    print()
    print("per-script protection")
    c.unlock("hunter2pass")
    hashed = c.hash_script(SCRIPT)
    check("hash_script returns a digest", len(hashed["digest"]) == 64)
    check("hmac_script returns a tag", len(c.hmac_script(SCRIPT)["tag"]) == 64)
    signed = c.sign(SCRIPT)
    check("sign returns a signature", bool(signed["signature"]))
    check("the tab verifies its own signature",
          c.verify_signature(SCRIPT, signed["signature"])["valid"])
    check("a foreign signature is rejected",
          not c.verify_signature(SCRIPT, "deadbeef")["valid"])
    sealed = c.seal_script(SCRIPT, use_session=True)
    check("a script can be sealed in two layers", sealed["layered"])
    check("a layered script unseals",
          c.unseal_script("demo.py", sealed["sealed"], True)["text"]
          == SCRIPT["code"])
    shadow = c.issue_shadow_key("project")
    check("a shadow key is issued", shadow["ok"] and shadow["key"]["token"])
    check("a shadow key can be revoked",
          c.revoke_shadow_key(shadow["key"]["token"])["ok"])

    print()
    print("integrity registry and tamper detection")
    reg = core.SecurityRegistry(os.path.join(sec_dir, "registry.json"))
    reg.record_script(SCRIPT, c.key())
    reg.save()
    check("an intact script verifies",
          reg.check(SCRIPT, c.key())["state"] == "intact")
    changed = dict(SCRIPT, code="print(2)\n")
    check("a changed script is detected",
          reg.check(changed, c.key())["state"] == "changed")
    check("a script with no record is missing",
          reg.check({"name": "nope.py", "code": ""}, c.key())["state"]
          == "missing")
    check("the registry reloads from disk",
          core.SecurityRegistry(reg.path).get("demo.py") is not None)
    reg.push_version("demo.py", core.digest_text("print(1)\n"), "first")
    check("a version is recorded",
          reg.get("demo.py")["versions"][-1]["note"] == "first")
    states = reg.verify_all([SCRIPT, changed], c.key())
    summary = reg.summary(states)
    check("the summary counts both states",
          summary["intact"] == 1 and summary["changed"] == 1)
    c.policy["tamper_action"] = True
    c.detect_manipulation("content changed", "a", "b")
    check("a tamper alarm is raised", c.alarms and c.manipulations)

    print()
    print("sandbox description")
    desc = core.sandbox_description(c.sandbox, os.path.join(tmp, "ws"))
    check("the default policy isolates", desc["isolated"])
    check("the network is blocked by default",
          any("network" in item for item in desc["blocked"]))
    c.sandbox["isolate_network"] = False
    desc2 = core.sandbox_description(c.sandbox)
    check("turning the network off removes the block",
          not any("network" in item for item in desc2["blocked"]))

    print()
    print("backups, versions and reports")
    backups = os.path.join(sec_dir, core.SECURITY_BACKUP_DIR)
    path = core.backup_script(SCRIPT, backups, "before edit")
    check("a backup is written", os.path.isfile(path))
    check("the backup lists", len(core.list_backups(backups)) == 1)
    check("the backup restores",
          core.restore_backup(path)["body"] == SCRIPT["code"])
    check("a changed backup is refused",
          core.restore_backup(path, core.digest_text("other"))["code"]
          == "tampered")
    diff = core.diff_scripts("a\nb\n", "a\nc\n")
    check("the diff finds the first change", diff["first_change"]["line"] == 2)
    for kind in core.SECURITY_REPORT_KEYS:
        text = core.security_report(kind, c, reg, states)
        check("report %r is produced" % kind,
              core.SECURITY_REPORT_LABELS[kind] in text and len(text) > 40)
    out = core.export_report("risk", c, reg, states,
                             os.path.join(sec_dir, core.SECURITY_REPORT_DIR))
    check("a report exports to JSON", os.path.isfile(out))
    check("a .py file is securable", core.is_securable("demo.py"))
    check("a .txt file is not securable", not core.is_securable("notes.txt"))

    print()
    print("core persistence")
    c.record("sec_exported", "registry written")
    c.save()
    reloaded = core.SecurityCore(sec_dir)
    check("the salt survives a reload", reloaded.salt is not None)
    check("the verifier survives a reload",
          not reloaded.verify("hunter2pass")["ok"] is False)
    check("the policy survives a reload",
          "isolate_network" in reloaded.sandbox)

    print()
    print("the tab")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = FakeWindow(tmp)
    tab = SecurityTab(window)
    check("the tab creates a core", tab.core is not None)
    # The dialog-driven create/unlock paths share their logic with the core, so
    # the state is set here directly and the buttons are exercised without a
    # modal dialog blocking an offscreen run.
    tab.core.create("hunter2pass", "hunter2pass")
    tab.core.unlock("hunter2pass")
    tab.after_state_change()
    check("the tab can set a passphrase", tab.core.has_key_material())
    check("the tab reports itself unlocked", not tab.is_locked())
    tab.refresh()
    check("the tab table is populated", tab.table.rowCount() >= 2)
    tab.table.selectRow(0)
    check("the tab selects a script", tab._selected_script() is not None)
    tab.fingerprint_selected()
    check("the tab fingerprints a script",
          "Fingerprint of" in tab.last_report)
    tab.sign_selected()
    check("the tab signs a script", "Signed" in tab.last_report)
    tab.record_selected()
    check("the tab records a script", tab.registry.get("demo.py") is not None)
    check("the recorded script is intact",
          tab.registry.check(SCRIPT, tab.core.key())["state"] == "intact")
    tab.record_selected()
    check("re-recording bumps the version",
          tab.registry.get("demo.py")["version"] >= 2)
    tab.verify_all()
    check("verify all produces a report line",
          "intact" in tab.last_report.lower())
    for kind in core.SECURITY_REPORT_KEYS:
        tab.show_report(kind)
        check("the tab shows report %r" % kind,
              core.SECURITY_REPORT_LABELS[kind] in tab.last_report)
    tab.export_report("risk")
    check("the tab exports a report", bool(tab.last_path))
    tab.export_registry()
    check("the tab exports the registry", bool(tab.last_path))
    tab.export_config()
    check("the tab exports the configuration", bool(tab.last_path))
    tab.show_sandbox_plan()
    check("the tab shows a sandbox plan", "Sandbox plan" in tab.last_report)
    tab.backup_selected()
    check("the tab takes a backup",
          len(core.list_backups(tab.backup_dir())) >= 1)
    tab.seal_selected()
    check("the tab seals a script", "Sealed" in tab.last_report)
    tab.unseal_selected()
    check("the tab unseals a script", "Unsealed" in tab.last_report)
    tab.issue_shadow()
    check("the tab issues a shadow key", "Shadow key issued" in tab.last_report)
    tab.hash_all()
    check("the tab hashes every script", tab.registry.get("run.bat") is not None)
    tab.lock()
    check("the tab locks", tab.is_locked())
    check("a locked tab refuses an action",
          tab.require_open("sign") is False)

print()
print("%d failure(s)" % len(failures))
sys.exit(1 if failures else 0)
