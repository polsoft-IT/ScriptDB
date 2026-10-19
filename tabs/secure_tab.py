# -*- coding: utf-8 -*-
"""Security tab for ScriptDB ("Script securing").

This module owns the "Security" tab: the toolkit that hardens the scripts the
database holds, layer by layer.  It is the interface to ``tabs.secure_core`` --
the engine does the cryptography and keeps the keys, and this tab only asks it
questions and shows the answers, so a widget that was left enabled can never
touch key material on its own.

The tab follows the same one-file-per-tab pattern as every other tab:

    SecurityTab(window)              -- build the tab
    tab.retranslate(table)           -- apply a language table (see TRANSLATIONS)
    tab.refresh()                    -- rebuild the table from the Scripts list

What it offers, grouped as the panel presents it:

    authorization   create / unlock / lock the passphrase, an optional token,
                    the attempt limit with a cool-down, auto-lock, high-security
                    mode (the policy dialog edits all of them)
    integrity       one row per script: its SHA-256 fingerprint, whether a
                    record exists, whether it is signed and whether it still
                    matches; fingerprint / sign / record / verify actions
    sealing         seal a script under its own per-file key, with a double
                    layer under the session key (unseal puts it back)
    backups         timestamped backups of a script, listed and restorable
                    (time travel), plus the version history a record keeps
    sandbox         the isolation description for a run (fs / net / syscalls /
                    env / process / logs), handed to the Sandbox tab
    reports         integrity / change / access / error / warning / risk, shown
                    as text and exportable to JSON
    keys            shadow keys: one-time / session / project keys, issuable and
                    revocable
    export/import   export the registry, the config and a report; import a
                    registry or a config bundle

All of it is offline and dependency free: the module uses only the standard
library and never sends anything anywhere.

The main window only needs a few lines to wire the tab in::

    from tabs import SecurityTab
    self.security_tab = SecurityTab(self)
    self.tabs.addTab(self.security_tab, "Security")
"""

import os

import app_paths

from PyQt5 import QtCore, QtWidgets

from . import secure_core as core


class SecurityTab(QtWidgets.QWidget):
    """The script-securing workspace.

    The tab owns one :class:`tabs.secure_core.SecurityCore` and one
    :class:`tabs.secure_core.SecurityRegistry`.  Nothing inside the registry is
    reachable until the core reports an unlocked session, and every action
    re-checks that session first -- so a stale widget cannot leak content.
    """

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.core = None
        self.registry = None
        self.last_report = ""
        self.last_path = ""
        self._rows = []
        self._last_selected = ""
        self.directory = self._default_dir()
        self._build_ui()
        self.open_security(self.directory, silent=True)

    # ------------------------------------------------------------- helpers
    def t(self, key, fallback=None, **kwargs):
        try:
            return self.window.t(key, fallback, **kwargs)
        except Exception:
            return fallback or key

    def _default_dir(self):
        # The integrity registry and the backups are program state (db\security).
        base = (getattr(self.window, "db_dir", None)
                or getattr(self.window, "DB_DIR", None)
                or app_paths.ensure_db_dir())
        return os.path.join(base, core.SECURITY_DIR)

    def backup_dir(self):
        return os.path.join(self.directory or self._default_dir(),
                            core.SECURITY_BACKUP_DIR)

    def report_dir(self):
        return os.path.join(self.directory or self._default_dir(),
                            core.SECURITY_REPORT_DIR)

    def _scripts(self):
        """Every script the main window holds, as a list of dicts."""
        getter = getattr(self.window, "scripts_only", None)
        if callable(getter):
            try:
                return list(getter())
            except Exception:
                return []
        return list(getattr(self.window, "scripts", []) or [])

    def _announce(self, detail):
        """Record a security action on the shared audit timeline."""
        try:
            if self.core is not None:
                self.core.record("sec_verified", detail)
        except Exception:
            pass
        audit = getattr(self.window, "audit_tab", None)
        if audit is not None and hasattr(audit, "log_change"):
            try:
                audit.log_change("modules", "security", detail, "security")
                return
            except Exception:
                pass
        notify = getattr(self.window, "notify_audit", None)
        if callable(notify):
            try:
                notify("module_updated", where="security", detail=detail)
            except Exception:
                pass

    def _status(self, message):
        self.status_label.setText(message)
        return message

    def is_locked(self):
        return bool(self.core) and self.core.locked

    def require_open(self, action="this action"):
        if not self.is_locked():
            return True
        self._status(self.t("sec_locked_action",
                            "Locked. Unlock to {action}.").format(action=action))
        return False

    def _selected_script(self):
        """The script the table row currently points at, or None."""
        row = self.table.currentRow()
        name = ""
        if 0 <= row < len(self._rows):
            name = self._rows[row].get("name", "")
        if not name:
            # The row highlight is lost when another widget takes focus (a
            # report, for instance), so fall back to the last picked name --
            # otherwise an action would silently do nothing.
            name = self._last_selected
        if not name:
            return None
        for script in self._scripts():
            if script.get("name") == name:
                return script
        return {"name": name, "code": ""}

    # ------------------------------------------------------------- lifecycle
    def open_security(self, directory, silent=False):
        """Open (or create) the security directory and load its state."""
        self.directory = os.path.abspath(directory or self._default_dir())
        core.make_directory(self.directory)
        core.make_directory(self.backup_dir())
        core.make_directory(self.report_dir())
        self.core = core.SecurityCore(self.directory)
        self.registry = core.SecurityRegistry(
            os.path.join(self.directory, core.SECURITY_REGISTRY_FILE))
        self.directory_label.setText(os.path.basename(self.directory))
        self.directory_label.setToolTip(self.directory)
        if not self.core.has_key_material() and not silent:
            self._status(self.t("sec_new",
                                "New security store. Set a passphrase."))
        self.after_state_change()
        return self.core

    def after_state_change(self):
        """Refresh everything that depends on the lock state."""
        unlocked = not self.is_locked()
        self.state_label.setText(self.t("sec_state_open", "UNLOCKED")
                                 if unlocked else
                                 self.t("sec_state_locked", "LOCKED"))
        self.state_label.setStyleSheet(
            "font-weight: bold; color: %s;" % ("#9ece6a" if unlocked
                                               else "#f7768e"))
        self.count_label.setText(
            self.t("sec_count", "{0} record(s)").format(
                len(self.registry.names()))
            if self.registry else "")
        for widget in (self.btn_lock, self.btn_policy, self.btn_fingerprint,
                       self.btn_sign, self.btn_record, self.btn_verify,
                       self.btn_seal, self.btn_unseal, self.btn_backup,
                       self.btn_restore, self.btn_shadow, self.btn_hash_all):
            widget.setEnabled(unlocked or widget in (self.btn_policy,))
        self.btn_create.setEnabled(not (self.core and
                                        self.core.has_key_material()))
        self.btn_unlock.setEnabled(self.core is not None and
                                   self.core.has_key_material())
        self.btn_high.setCheckable(True)
        self.btn_high.setChecked(bool(self.core and
                                      self.core.policy.get("high_security")))

    # ------------------------------------------------------------------- UI
    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)

        # ---- header -------------------------------------------------------
        header = QtWidgets.QHBoxLayout()
        self.title_label = QtWidgets.QLabel(self.t("sec_title", "SECURITY"))
        self.title_label.setStyleSheet(
            "font-size: 14px; font-weight: bold; color: #8ab4f8;")
        header.addWidget(self.title_label)
        self.directory_label = QtWidgets.QLabel("")
        self.directory_label.setStyleSheet("color: #9aa0a6;")
        header.addWidget(self.directory_label)
        header.addStretch()
        self.state_label = QtWidgets.QLabel("")
        header.addWidget(self.state_label)
        self.count_label = QtWidgets.QLabel("")
        self.count_label.setStyleSheet("color: #9aa0a6;")
        header.addWidget(self.count_label)
        layout.addLayout(header)

        # ---- authorization row --------------------------------------------
        auth_row = QtWidgets.QHBoxLayout()
        self.btn_create = QtWidgets.QPushButton(
            self.t("sec_create", "Set passphrase..."))
        self.btn_create.clicked.connect(self.create_passphrase)
        self.btn_unlock = QtWidgets.QPushButton(
            self.t("sec_unlock", "Unlock..."))
        self.btn_unlock.clicked.connect(self.unlock)
        self.btn_lock = QtWidgets.QPushButton(self.t("sec_lock", "Lock"))
        self.btn_lock.clicked.connect(self.lock)
        self.btn_policy = QtWidgets.QPushButton(
            self.t("sec_policy", "Policy..."))
        self.btn_policy.clicked.connect(self.edit_policy)
        self.btn_high = QtWidgets.QPushButton(
            self.t("sec_high", "High security"))
        self.btn_high.setCheckable(True)
        self.btn_high.toggled.connect(self.toggle_high_security)
        for button in (self.btn_create, self.btn_unlock, self.btn_lock,
                       self.btn_policy, self.btn_high):
            auth_row.addWidget(button)
        auth_row.addStretch()
        self.idle_label = QtWidgets.QLabel("")
        self.idle_label.setStyleSheet("color: #9aa0a6;")
        auth_row.addWidget(self.idle_label)

        # ---- protection row -----------------------------------------------
        protect_row = QtWidgets.QHBoxLayout()
        for name, label_key, fallback, slot in (
                ("btn_fingerprint", "sec_fingerprint", "Fingerprint",
                 self.fingerprint_selected),
                ("btn_sign", "sec_sign", "Sign", self.sign_selected),
                ("btn_record", "sec_record", "Record", self.record_selected),
                ("btn_verify", "sec_verify", "Verify", self.verify_all),
                ("btn_seal", "sec_seal", "Seal", self.seal_selected),
                ("btn_unseal", "sec_unseal", "Unseal", self.unseal_selected),
                ("btn_backup", "sec_backup", "Backup", self.backup_selected),
                ("btn_restore", "sec_restore", "Restore", self.restore_backup),
                ("btn_shadow", "sec_shadow", "Shadow key", self.issue_shadow),
                ("btn_hash_all", "sec_hash_all", "Hash all", self.hash_all)):
            button = QtWidgets.QPushButton(self.t(label_key, fallback))
            button.clicked.connect(slot)
            setattr(self, name, button)
            protect_row.addWidget(button)
        protect_row.addStretch()
        layout.addLayout(protect_row)

        # ---- report / export tools -----------------------------------------
        self.btn_reports = QtWidgets.QToolButton()
        self.btn_reports.setText(self.t("sec_reports", "Reports..."))
        self.btn_reports.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.report_menu = QtWidgets.QMenu(self.btn_reports)
        self.report_actions = {}
        for key, label_key, fallback in core.SECURITY_REPORTS:
            action = self.report_menu.addAction(self.t(label_key, fallback))
            action.triggered.connect(lambda _c=False, k=key:
                                     self.show_report(k))
            self.report_actions[key] = action
        self.btn_reports.setMenu(self.report_menu)

        self.btn_export = QtWidgets.QToolButton()
        self.btn_export.setText(self.t("sec_export", "Export..."))
        self.btn_export.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.export_menu = QtWidgets.QMenu(self.btn_export)
        self.export_menu.addAction(self.t("sec_export_report",
                                          "Report to JSON"),
                                   lambda: self.export_report("risk"))
        self.export_menu.addAction(self.t("sec_export_registry",
                                          "Registry to JSON"),
                                   self.export_registry)
        self.export_menu.addAction(self.t("sec_export_config",
                                          "Configuration"),
                                   self.export_config)
        self.btn_export.setMenu(self.export_menu)

        self.btn_import = QtWidgets.QPushButton(
            self.t("sec_import", "Import..."))
        self.btn_import.clicked.connect(self.import_bundle)
        self.btn_sandbox = QtWidgets.QPushButton(
            self.t("sec_sandbox", "Sandbox plan"))
        self.btn_sandbox.clicked.connect(self.show_sandbox_plan)

        # Keep security actions together: authorization stays on the left,
        # while report and integration tools occupy the opposite side.
        for button in (self.btn_reports, self.btn_export, self.btn_import,
                       self.btn_sandbox):
            auth_row.addWidget(button)
        layout.addLayout(auth_row)

        # The selected script path remains visible beside the report output.
        report_row = QtWidgets.QHBoxLayout()
        report_row.addStretch()
        self.path_label = QtWidgets.QLabel("")
        self.path_label.setStyleSheet("color: #9aa0a6;")
        report_row.addWidget(self.path_label)
        layout.addLayout(report_row)

        # ---- body: table + report view ------------------------------------
        splitter = QtWidgets.QSplitter(QtCore.Qt.Vertical)

        self.table = QtWidgets.QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels([
            self.t("sec_col_name", "Script"),
            self.t("sec_col_digest", "SHA-256"),
            self.t("sec_col_record", "Record"),
            self.t("sec_col_signed", "Signed"),
            self.t("sec_col_state", "State"),
            self.t("sec_col_detail", "Detail")])
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._on_selection)
        splitter.addWidget(self.table)

        self.report_view = QtWidgets.QPlainTextEdit()
        self.report_view.setReadOnly(True)
        self.report_view.setPlaceholderText(
            self.t("sec_report_hint", "A report or a plan appears here."))
        splitter.addWidget(self.report_view)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter)

        self.status_label = QtWidgets.QLabel("")
        self.status_label.setStyleSheet("color: #9aa0a6;")
        layout.addWidget(self.status_label)

    # ---------------------------------------------------------------- table
    def refresh(self):
        """Rebuild the table from the Scripts list and the registry."""
        scripts = self._scripts()
        self._rows = []
        self.table.setRowCount(0)
        for script in scripts:
            name = script.get("name", "")
            digests = core.all_digests(script.get("code", "") or "")
            record = self.registry.get(name) if self.registry else None
            if record and self.core.key():
                state = self.registry.check(script, self.core.key())
            elif record:
                state = self.registry.check(script)
            else:
                state = {"state": "missing", "detail": "no integrity record"}
            self._rows.append({
                "name": name,
                "digest": digests.get("sha256", ""),
                "record": self.t("sec_yes", "yes") if record
                else self.t("sec_no", "no"),
                "signed": self.t("sec_yes", "yes")
                if record and record.get("signature") else self.t("sec_no", "no"),
                "state": state.get("state", ""),
                "detail": state.get("detail", ""),
            })
        self.table.setRowCount(len(self._rows))
        for row, entry in enumerate(self._rows):
            for column, key in enumerate(("name", "digest", "record", "signed",
                                          "state", "detail")):
                item = QtWidgets.QTableWidgetItem(str(entry.get(key, "")))
                self.table.setItem(row, column, item)
        self.table.resizeColumnsToContents()
        if not self._rows:
            self._status(self.t("sec_no_scripts", "No scripts to secure."))
        else:
            self._status(self.t("sec_rows", "{0} script(s) listed.").format(
                len(self._rows)))
        self.after_state_change()
        return self._rows

    def _on_selection(self):
        row = self.table.currentRow()
        if 0 <= row < len(self._rows):
            self._last_selected = self._rows[row].get("name", "")
        script = self._selected_script()
        if script is not None:
            self.path_label.setText(script.get("name", ""))

    # -------------------------------------------------------------- actions
    def _report(self, text):
        self.last_report = text
        self.report_view.setPlainText(text)

    def create_passphrase(self):
        """Ask for a new master passphrase (twice) and create the key material."""
        first, ok = QtWidgets.QInputDialog.getText(
            self, self.t("sec_create", "Set passphrase..."),
            self.t("sec_passphrase_hint", "New passphrase (min 8 chars):"),
            QtWidgets.QLineEdit.Password)
        if not ok:
            return None
        second, ok = QtWidgets.QInputDialog.getText(
            self, self.t("sec_create", "Set passphrase..."),
            self.t("sec_passphrase_again", "Repeat the passphrase:"),
            QtWidgets.QLineEdit.Password)
        if not ok:
            return None
        outcome = self.core.create(first, second)
        self._status(outcome["detail"])
        if outcome["ok"]:
            # Unlock straight away so the user can start securing scripts.
            self.core.unlock(first)
            self._announce("key material created")
        self.after_state_change()
        return outcome

    def unlock(self):
        """Ask for the passphrase (and a token when the policy wants one)."""
        text, ok = QtWidgets.QInputDialog.getText(
            self, self.t("sec_unlock", "Unlock..."),
            self.t("sec_passphrase_prompt", "Passphrase:"),
            QtWidgets.QLineEdit.Password)
        if not ok:
            return None
        outcome = self.core.unlock(text)
        if not outcome["ok"] and outcome.get("code") == "needs_token":
            token, ok = QtWidgets.QInputDialog.getText(
                self, self.t("sec_unlock", "Unlock..."),
                self.t("sec_token_prompt", "Token: "))
            if ok:
                outcome = self.core.unlock(text, token)
        self._status(outcome["detail"])
        self.after_state_change()
        return outcome

    def lock(self):
        self.core.lock("locked by the user")
        self._status(self.t("sec_locked", "Locked."))
        self.after_state_change()
        return True

    def toggle_high_security(self, active):
        if self.core is None:
            return
        self.core.enable_high_security(active)
        self._status(self.t("sec_high_on", "High-security mode is on.")
                     if active else
                     self.t("sec_high_off", "High-security mode is off."))
        self.after_state_change()

    def edit_policy(self):
        dialog = SecurityPolicyDialog(self, self.core)
        if dialog.exec_():
            self.core.set_policy(dialog.policy_values())
            self.core.set_sandbox(dialog.sandbox_values())
            self._status(self.t("sec_policy_saved", "Policy updated."))
            self.after_state_change()

    def fingerprint_selected(self):
        script = self._selected_script()
        if script is None:
            return self._status(self.t("sec_pick", "Pick a script first."))
        outcome = self.core.hash_script(script)
        text = ["Fingerprint of %s" % script.get("name", ""), ""]
        for key in core.DIGEST_KEYS:
            text.append("  %-10s %s" % (core.DIGEST_LABELS[key],
                                        outcome["digests"][key]))
        self._report("\n".join(text))
        self._status(self.t("sec_fingerprinted", "Fingerprint computed."))
        return outcome

    def sign_selected(self):
        if not self.require_open("sign"):
            return None
        script = self._selected_script()
        if script is None:
            return self._status(self.t("sec_pick", "Pick a script first."))
        outcome = self.core.sign(script)
        if outcome["ok"]:
            self._report("Signed %s\n  fingerprint: %s\n  signature:   %s"
                         % (outcome["name"], outcome["fingerprint"],
                            outcome["signature"]))
            self._status(self.t("sec_signed", "Script signed."))
        return outcome

    def record_selected(self):
        """Write (or refresh) the integrity record for the selected script."""
        script = self._selected_script()
        if script is None:
            return self._status(self.t("sec_pick", "Pick a script first."))
        use_key = self.core.key() if self.core.policy.get("sign_scripts") else None
        sealed = ""
        if self.core.key() is not None:
            outcome = self.core.seal_script(script)
            if outcome["ok"]:
                sealed = outcome["sealed"]
        record = self.registry.record_script(script, use_key, sealed=sealed,
                                             sign=bool(use_key))
        self.registry.push_version(record["name"], record["fingerprint"],
                                   "recorded")
        self.registry.save()
        self._announce("recorded %s" % record["name"])
        self.refresh()
        self._status(self.t("sec_recorded", "Integrity record written."))
        return record

    def verify_all(self):
        """Check every script against the registry and report the result."""
        states = self.registry.verify_all(self._scripts(), self.core.key())
        summary = self.registry.summary(states)
        text = ["Verification", ""]
        for state in states:
            text.append("  %-24s %-10s %s" % (state.get("name", ""),
                                              state.get("state", ""),
                                              state.get("detail", "")))
        text += ["", "intact %d / changed %d / unsigned %d / foreign %d / "
                     "missing %d" % (summary["intact"], summary["changed"],
                                     summary["unsigned"], summary["foreign"],
                                     summary["missing"])]
        changed = [s for s in states if s.get("state") in ("changed", "foreign")]
        if changed:
            for state in changed:
                self.core.detect_manipulation(
                    "%s: %s" % (state.get("name"), state.get("detail")),
                    state.get("expected"), state.get("found"))
            if self.core.policy.get("tamper_action"):
                self.core.lock("tamper detection locked the session")
                text.append("")
                text.append("Tamper detected: %d script(s). The session was "
                            "locked (tamper action)." % len(changed))
        self._report("\n".join(text))
        self.refresh()
        self._status(self.t("sec_verified", "Verification complete."))
        return states

    def hash_all(self):
        """Fingerprint every script at once and record the digests."""
        if not self.require_open("hash"):
            return None
        script_list = self._scripts()
        for script in script_list:
            use_key = self.core.key() if self.core.policy.get("sign_scripts") \
                else None
            self.registry.record_script(script, use_key,
                                        sign=bool(use_key))
        self.registry.save()
        self.refresh()
        self._status(self.t("sec_hashed_all", "{0} script(s) recorded.").format(
            len(script_list)))
        return script_list

    def seal_selected(self):
        if not self.require_open("seal"):
            return None
        script = self._selected_script()
        if script is None:
            return self._status(self.t("sec_pick", "Pick a script first."))
        use_session = bool(self.core.policy.get("high_security"))
        outcome = self.core.seal_script(script, use_session=use_session)
        if outcome["ok"]:
            # Persist the envelope in the script's record so Unseal can find it;
            # the record stores only the ciphertext, never the key.
            name = script.get("name", "")
            record = self.registry.get(name) or \
                self.registry.record_script(script, self.core.key())
            record["sealed"] = outcome["sealed"]
            record["sealed_with"] = "layered" if outcome["layered"] \
                else "per-file"
            self.registry.save()
            self._report("Sealed %s\n  layered: %s\n  envelope: %s"
                         % (name, outcome["layered"], outcome["sealed"]))
            self._status(self.t("sec_sealed", "Script sealed."))
        return outcome

    def unseal_selected(self):
        if not self.require_open("unseal"):
            return None
        script = self._selected_script()
        name = (script or {}).get("name", "")
        record = self.registry.get(name)
        if not record or not record.get("sealed"):
            return self._status(self.t("sec_nothing_sealed",
                                       "That script was never sealed."))
        layered = record.get("sealed_with") == "layered"
        outcome = self.core.unseal_script(name, record["sealed"], layered=layered)
        if outcome["ok"]:
            self._report("Unsealed %s\n%s" % (name, outcome["text"]))
            self._status(self.t("sec_unsealed", "Script unsealed."))
        return outcome

    def backup_selected(self):
        script = self._selected_script()
        if script is None:
            return self._status(self.t("sec_pick", "Pick a script first."))
        path = core.backup_script(script, self.backup_dir(), "manual backup")
        if self.core.key():
            self.registry.push_version(script.get("name", ""),
                                       core.digest_text(script.get("code", "")),
                                       "backup", "")
            self.registry.save()
        self._announce("backup %s" % os.path.basename(path))
        self._status(self.t("sec_backed_up", "Backup written: {0}").format(
            os.path.basename(path)))
        return path

    def restore_backup(self):
        """Pick the newest backup and restore it (time travel)."""
        backups = core.list_backups(self.backup_dir())
        if not backups:
            return self._status(self.t("sec_no_backups", "No backups yet."))
        labels = ["%s  (%s)" % (row.get("name", "") or row.get("file", ""),
                                row.get("taken", "")) for row in backups]
        choice, ok = QtWidgets.QInputDialog.getItem(
            self, self.t("sec_restore", "Restore"),
            self.t("sec_restore_pick", "Restore which backup?"), labels, 0,
            False)
        if not ok:
            return None
        index = labels.index(choice)
        outcome = core.restore_backup(backups[index]["path"])
        if outcome["ok"]:
            self._report("Restored %s\n  digest: %s\n%s"
                         % (outcome["name"], outcome["digest"],
                            outcome["body"]))
            self._status(self.t("sec_restored", "Backup restored."))
        else:
            self._status(outcome["detail"])
        return outcome

    def issue_shadow(self):
        if not self.require_open("issue a key"):
            return None
        outcome = self.core.issue_shadow_key("tab")
        if outcome["ok"]:
            self._report("Shadow key issued\n  token:  %s\n  label:  %s"
                         % (outcome["key"]["token"], outcome["key"]["label"]))
            self._status(self.t("sec_shadow_issued", "Shadow key issued."))
        return outcome

    def show_sandbox_plan(self):
        script = self._selected_script()
        workspace = os.path.join(self.directory, "sandbox",
                                 core._slug((script or {}).get("name", "run")))
        desc = core.sandbox_description(self.core.sandbox, workspace)
        text = ["Sandbox plan", ""]
        if script is not None:
            text.append("  script:      %s" % script.get("name", ""))
        text.append("  workspace:   %s" % desc["workspace"])
        text.append("  isolated:    %s" % desc["isolated"])
        text.append("  blocked:     %s" % (desc["description"] or "nothing"))
        text.append("  shadows:     %s" % (", ".join(desc["shadows"]) or "none"))
        text.append("")
        text.append("Send this script to the Sandbox tab to run it under this "
                    "policy.")
        self._report("\n".join(text))
        self._status(self.t("sec_sandbox_plan", "Sandbox plan built."))
        return desc

    # ------------------------------------------------------------- reports
    def _states(self):
        return self.registry.verify_all(self._scripts(), self.core.key())

    def show_report(self, kind):
        text = core.security_report(kind, self.core, self.registry,
                                    self._states())
        self._report(text)
        self._status(self.t("sec_report_shown", "Report: {0}").format(
            core.SECURITY_REPORT_LABELS.get(kind, kind)))
        return text

    def export_report(self, kind="risk"):
        path = core.export_report(kind, self.core, self.registry,
                                  self._states(), self.report_dir())
        self.last_path = path
        self._status(self.t("sec_exported", "Report exported: {0}").format(
            os.path.basename(path)))
        return path

    def export_registry(self):
        """Write the registry to a JSON bundle (the records, never a key)."""
        payload = {"format": "scriptdb-security-registry-export",
                   "exported": core._now(),
                   "scripts": self.registry.records}
        path = os.path.join(self.report_dir(), "registry-%s.json"
                            % core._stamp())
        core._atomic_write_json(path, payload)
        self.last_path = path
        self._status(self.t("sec_exported_registry",
                            "Registry exported: {0}").format(
            os.path.basename(path)))
        return path

    def export_config(self):
        """Write the policy configuration to a JSON bundle."""
        payload = {"format": "scriptdb-security-config",
                   "exported": core._now(),
                   "policy": self.core.policy,
                   "sandbox": self.core.sandbox}
        path = os.path.join(self.report_dir(), "config-%s.json" % core._stamp())
        core._atomic_write_json(path, payload)
        self.last_path = path
        self._status(self.t("sec_exported_config",
                            "Configuration exported: {0}").format(
            os.path.basename(path)))
        return path

    def import_bundle(self):
        """Import a registry or a configuration bundle from a JSON file."""
        path, _filter = QtWidgets.QFileDialog.getOpenFileName(
            self, self.t("sec_import", "Import..."), self.directory,
            "JSON (*.json)")
        if not path:
            return None
        try:
            payload = core._read_json(path)
        except Exception as exc:
            self._status(str(exc))
            return None
        kind = payload.get("format", "")
        if kind == "scriptdb-security-registry-export":
            self.registry.records.update(payload.get("scripts") or {})
            self.registry.save()
            self.refresh()
            self._status(self.t("sec_imported_registry",
                                "Registry imported: {0} record(s)").format(
                len(payload.get("scripts") or {})))
        elif kind == "scriptdb-security-config":
            self.core.set_policy(payload.get("policy") or {})
            self.core.set_sandbox(payload.get("sandbox") or {})
            self.after_state_change()
            self._status(self.t("sec_imported_config", "Configuration imported."))
        else:
            self._status(self.t("sec_import_unknown",
                                "That file is not a security bundle."))
        return payload

    # ------------------------------------------------------------ translation
    def retranslate(self, table):
        """Apply the main window's language table to this tab."""
        def label(key, fallback):
            return table.get(key, fallback)

        self.title_label.setText(label("sec_title", "SECURITY"))
        self.btn_create.setText(label("sec_create", "Set passphrase..."))
        self.btn_unlock.setText(label("sec_unlock", "Unlock..."))
        self.btn_lock.setText(label("sec_lock", "Lock"))
        self.btn_policy.setText(label("sec_policy", "Policy..."))
        self.btn_high.setText(label("sec_high", "High security"))
        self.btn_fingerprint.setText(label("sec_fingerprint", "Fingerprint"))
        self.btn_sign.setText(label("sec_sign", "Sign"))
        self.btn_record.setText(label("sec_record", "Record"))
        self.btn_verify.setText(label("sec_verify", "Verify"))
        self.btn_seal.setText(label("sec_seal", "Seal"))
        self.btn_unseal.setText(label("sec_unseal", "Unseal"))
        self.btn_backup.setText(label("sec_backup", "Backup"))
        self.btn_restore.setText(label("sec_restore", "Restore"))
        self.btn_shadow.setText(label("sec_shadow", "Shadow key"))
        self.btn_hash_all.setText(label("sec_hash_all", "Hash all"))
        self.btn_reports.setText(label("sec_reports", "Reports..."))
        self.btn_export.setText(label("sec_export", "Export..."))
        self.btn_import.setText(label("sec_import", "Import..."))
        self.btn_sandbox.setText(label("sec_sandbox", "Sandbox plan"))
        for key, action in self.report_actions.items():
            action.setText(label("sec_report_" + key,
                                 core.SECURITY_REPORT_LABELS.get(key, key)))
        self.table.setHorizontalHeaderLabels([
            label("sec_col_name", "Script"),
            label("sec_col_digest", "SHA-256"),
            label("sec_col_record", "Record"),
            label("sec_col_signed", "Signed"),
            label("sec_col_state", "State"),
            label("sec_col_detail", "Detail")])
        self.report_view.setPlaceholderText(
            label("sec_report_hint", "A report or a plan appears here."))
        self.after_state_change()


class SecurityPolicyDialog(QtWidgets.QDialog):
    """Edit the authorization and sandbox policies in one place."""

    def __init__(self, parent, sec_core):
        super().__init__(parent)
        self.sec_core = sec_core
        self.setWindowTitle(parent.t("sec_policy", "Policy..."))
        self.checks = {}
        self.spins = {}
        layout = QtWidgets.QVBoxLayout(self)

        auth_box = QtWidgets.QGroupBox(parent.t("sec_group_auth",
                                                "Authorization"))
        auth_form = QtWidgets.QFormLayout(auth_box)
        for key, _lk, fallback, default in core.AUTH_POLICY:
            value = sec_core.policy.get(key, default)
            label = parent.t("sec_policy_" + key, fallback)
            if isinstance(default, bool):
                box = QtWidgets.QCheckBox(label)
                box.setChecked(bool(value))
                self.checks[key] = box
                auth_form.addRow(box)
            else:
                spin = QtWidgets.QSpinBox()
                spin.setRange(0, 86400)
                spin.setValue(int(value or 0))
                self.spins[key] = spin
                auth_form.addRow(label, spin)
        layout.addWidget(auth_box)

        sandbox_box = QtWidgets.QGroupBox(parent.t("sec_group_sandbox",
                                                   "Sandbox and isolation"))
        sandbox_layout = QtWidgets.QVBoxLayout(sandbox_box)
        for key, _lk, fallback, default in core.SANDBOX_POLICY:
            box = QtWidgets.QCheckBox(parent.t("sec_sandbox_" + key, fallback))
            box.setChecked(bool(sec_core.sandbox.get(key, default)))
            self.checks[key] = box
            sandbox_layout.addWidget(box)
        layout.addWidget(sandbox_box)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def policy_values(self):
        out = {}
        for key, box in self.checks.items():
            if key in core.AUTH_POLICY_KEYS:
                out[key] = box.isChecked()
        for key, spin in self.spins.items():
            out[key] = spin.value()
        return out

    def sandbox_values(self):
        return {key: box.isChecked() for key, box in self.checks.items()
                if key in core.SANDBOX_POLICY_KEYS}
