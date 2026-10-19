# -*- coding: utf-8 -*-
"""Vault tab for ScriptDB -- an encrypted, isolated private safe.

The tab owns the "Vault" surface: a private store that cannot be opened, read,
exported or run without authentication. It reuses the parts of the application
that already exist instead of duplicating them:

    tabs/docs_tab.py        the crypto (PBKDF2 + authenticated keystream),
                            the encrypted SQLite repository pattern, atomic
                            writes, export/import
    tabs/docs_actions.py    the one-result-per-operation shape
    tabs/sandbox_isolation  the isolated workspace and scrubbed environment
    tabs/analysis helpers   the static script analysis already used elsewhere

Everything the vault holds is encrypted at rest with the master key; the key
itself is never written to disk.
"""

import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import shutil
import app_paths
from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtGui import QFont

from .docs_tab import _atomic_write_json           # atomic, fsync'd writes
from .vault_core import (VaultSecurityCore, VaultStore, vault_report,
                         analyse_document, analyse_script, analyse_risk,
                         change_history, flags_text, VAULT_SECTIONS,
                         VAULT_SECTION_KEYS, VAULT_SECTION_LABELS,
                         VAULT_FLAGS, VAULT_FLAG_KEYS, VAULT_FLAG_LABELS,
                         VAULT_POLICY, VAULT_POLICY_KEYS, VAULT_POLICY_LABELS,
                         VAULT_REPORTS, VAULT_REPORT_KEYS, VAULT_REPORT_LABELS,
                         VAULT_DIR, VAULT_DB_FILE, VAULT_CONFIG_FILE)

VAULT_FONT = "Consolas"
VAULT_FONT_SIZE = 10

# The search aspects the vault offers.  Each entry is (key, label key,
# fallback): what the search should look at.
VAULT_SEARCH_FIELDS = (
    ("name", "vault_search_name", "By name"),
    ("content", "vault_search_content", "By content"),
    ("tags", "vault_search_tags", "By tags"),
    ("type", "vault_search_type", "By type"),
    ("date", "vault_search_date", "By date"),
    ("meta", "vault_search_meta", "By metadata"),
)

VAULT_SEARCH_FIELD_KEYS = tuple(key for key, _lk, _f in VAULT_SEARCH_FIELDS)

# The analysis views offered per resource.  (key, label key, fallback).
VAULT_ANALYSIS_KINDS = (
    ("structure", "vault_analysis_structure", "Document structure"),
    ("integrity", "vault_analysis_integrity", "Script integrity"),
    ("risk", "vault_analysis_risk", "Security risk"),
    ("changes", "vault_analysis_changes", "Change history"),
)

VAULT_ANALYSIS_KEYS = tuple(key for key, _lk, _f in VAULT_ANALYSIS_KINDS)

# The integration targets.  (key, label key, fallback, window attribute).
VAULT_INTEGRATIONS = (
    ("editor", "vault_int_editor", "Open in Editor", "editor_tab"),
    ("debugger", "vault_int_debugger", "Open in Debugger", "debugger_tab"),
    ("runner", "vault_int_runner", "Run in Sandbox", "sandbox_tab"),
    ("audit", "vault_int_audit", "Open in Audit", "audit_tab"),
    ("reports", "vault_int_reports", "Generate a report", "reports_tab"),
    ("diff", "vault_int_diff", "Compare in Diff", "diff_tab"),
    ("snapshots", "vault_int_snapshots", "Take a snapshot", None),
)

VAULT_INTEGRATION_KEYS = tuple(key for key, _lk, _f, _a in VAULT_INTEGRATIONS)

# The status the header reports, as (key, label key, fallback, colour).
VAULT_STATES = (
    ("locked", "vault_state_locked", "LOCKED", "#f7768e"),
    ("unlocked", "vault_state_unlocked", "UNLOCKED", "#9ece6a"),
    ("hidden", "vault_state_hidden", "HIDDEN MODE", "#bb9af7"),
    ("readonly", "vault_state_readonly", "READ-ONLY", "#f7c66d"),
)


def _now():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _fmt_seconds(value):
    """A short, ASCII duration: 90 -> '1m 30s'."""
    value = max(0, int(value or 0))
    if value < 60:
        return "%ds" % value
    if value < 3600:
        return "%dm %ds" % (value // 60, value % 60)
    return "%dh %dm" % (value // 3600, (value % 3600) // 60)
class VaultUnlockAnimation(QtWidgets.QWidget):
    """A short, ASCII-only 'unlocking' indicator.

    It is drawn with plain rectangles and text so it needs no images and works
    in low-graphics mode, and it is deliberately brief: a lock screen that
    lingers is a lock screen people click through without reading.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._progress = 0
        self.setFixedHeight(28)
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._tick)
        self.setStyleSheet("color: #9ece6a;")

    def start(self, label="unlocking"):
        self._label = label
        self._progress = 0
        self._timer.start(40)
        self.show()

    def stop(self):
        self._timer.stop()
        self.hide()

    def _tick(self):
        self._progress = min(100, self._progress + 10)
        self.update()
        if self._progress >= 100:
            self._timer.stop()

    def paintEvent(self, _event):
        painter = QtGui.QPainter(self)
        try:
            bar = self.rect().adjusted(0, 8, 0, -8)
            painter.fillRect(bar, QtGui.QColor("#2a2d2e"))
            filled = QtCore.QRect(bar.left(), bar.top(),
                                  int(bar.width() * self._progress / 100.0),
                                  bar.height())
            painter.fillRect(filled, QtGui.QColor("#9ece6a"))
            painter.setPen(QtGui.QColor("#dfe7f5"))
            painter.drawText(self.rect(), QtCore.Qt.AlignCenter,
                             "%s  %d%%" % (getattr(self, "_label", "working"),
                                           self._progress))
        finally:
            painter.end()


class VaultPasswordDialog(QtWidgets.QDialog):
    """The master-password prompt.

    It doubles as the "create" dialog and the "unlock" dialog: the mode decides
    whether a confirmation field and the strength meter are shown.  The password
    is only ever read once, at accept time, and never stored on the widget.
    """

    def __init__(self, parent, title="Vault", mode="unlock", policy=None):
        super().__init__(parent)
        self.mode = mode
        self.policy = policy or {}
        self.setWindowTitle(title)
        self.setModal(True)
        self.resize(420, 260 if mode == "unlock" else 320)

        layout = QtWidgets.QVBoxLayout(self)
        self.heading = QtWidgets.QLabel(
            "Enter the master password" if mode == "unlock"
            else "Choose a master password")
        self.heading.setStyleSheet(
            "font-size: 13px; font-weight: bold; color: #8ab4f8;")
        layout.addWidget(self.heading)

        self.hint = QtWidgets.QLabel("")
        self.hint.setStyleSheet("color: #9aa0a6;")
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)

        form = QtWidgets.QFormLayout()
        self.password_edit = QtWidgets.QLineEdit()
        self.password_edit.setEchoMode(QtWidgets.QLineEdit.Password)
        self.password_edit.returnPressed.connect(self.try_accept)
        form.addRow("Password:", self.password_edit)

        self.confirm_edit = None
        if mode != "unlock":
            self.confirm_edit = QtWidgets.QLineEdit()
            self.confirm_edit.setEchoMode(QtWidgets.QLineEdit.Password)
            self.confirm_edit.returnPressed.connect(self.try_accept)
            form.addRow("Confirm:", self.confirm_edit)
        layout.addLayout(form)

        self.strength = QtWidgets.QProgressBar()
        self.strength.setRange(0, 100)
        self.strength.setTextVisible(True)
        if mode != "unlock":
            layout.addWidget(self.strength)
            self.password_edit.textChanged.connect(self._on_password)

        self.challenge_row = QtWidgets.QWidget()
        challenge_layout = QtWidgets.QHBoxLayout(self.challenge_row)
        challenge_layout.setContentsMargins(0, 0, 0, 0)
        self.challenge_label = QtWidgets.QLabel("")
        self.challenge_edit = QtWidgets.QLineEdit()
        self.challenge_edit.setPlaceholderText("two-factor code")
        challenge_layout.addWidget(self.challenge_label)
        challenge_layout.addWidget(self.challenge_edit, 1)
        layout.addWidget(self.challenge_row)
        self.challenge_row.setVisible(False)

        self.message = QtWidgets.QLabel("")
        self.message.setStyleSheet("color: #f7768e;")
        self.message.setWordWrap(True)
        layout.addWidget(self.message)

        self.animation = VaultUnlockAnimation(self)
        layout.addWidget(self.animation)
        layout.addStretch()

        buttons = QtWidgets.QHBoxLayout()
        self.btn_cancel = QtWidgets.QPushButton("Cancel")
        self.btn_cancel.clicked.connect(self.reject)
        self.btn_ok = QtWidgets.QPushButton(
            "Unlock" if mode == "unlock" else "Create")
        self.btn_ok.setDefault(True)
        self.btn_ok.clicked.connect(self.try_accept)
        buttons.addStretch()
        buttons.addWidget(self.btn_cancel)
        buttons.addWidget(self.btn_ok)
        layout.addLayout(buttons)

    def _on_password(self, text):
        score, label, colour = password_strength(text)
        self.strength.setValue(score)
        self.strength.setFormat("%s (%d)" % (label, score))
        self.strength.setStyleSheet(
            "QProgressBar::chunk { background-color: %s; }" % colour)

    def show_challenge(self, challenge):
        """Reveal the two-factor row instead of rejecting the whole dialog."""
        self.challenge_row.setVisible(True)
        self.challenge_label.setText("Code %s:" % challenge)
        self.challenge_edit.setFocus()
        self.btn_ok.setText("Confirm")

    def set_locked_out(self, seconds):
        """Disable the buttons while the lockout clock runs."""
        self.btn_ok.setEnabled(False)
        self.password_edit.setEnabled(False)
        self.animation.start("locked")
        self._unlock_timer = QtCore.QTimer(self)
        self._unlock_timer.timeout.connect(
            lambda: self._countdown(seconds))
        self._remaining = seconds
        self._unlock_timer.start(1000)

    def _countdown(self, total):
        self._remaining -= 1
        self.message.setText("Too many attempts. Try again in %s."
                             % _fmt_seconds(max(0, self._remaining)))
        if self._remaining <= 0:
            self._unlock_timer.stop()
            self.animation.stop()
            self.btn_ok.setEnabled(True)
            self.password_edit.setEnabled(True)
            self.password_edit.clear()
            self.message.setText("")

    def password(self):
        return self.password_edit.text()

    def confirmation(self):
        return self.confirm_edit.text() if self.confirm_edit is not None else ""

    def try_accept(self):
        if not self.password():
            self.message.setText("The password cannot be empty.")
            return
        if self.mode != "unlock" and self.password() != self.confirmation():
            self.message.setText("The two passwords differ.")
            return
        self.accept()


def password_strength(text):
    """A 0-100 strength score with its label and bar colour.

    The score rewards length first (it is the property that actually matters),
    then variety, and it takes a clear penalty for the patterns that make a
    password guessable.
    """
    text = text or ""
    score = min(50, len(text) * 5)
    classes = 0
    for pattern in (r"[a-z]", r"[A-Z]", r"[0-9]", r"[^0-9A-Za-z]"):
        if re.search(pattern, text):
            classes += 1
    score += classes * 10
    if re.search(r"(.)\1{2,}", text):
        score -= 10
    if re.search(r"(password|secret|admin|123456|qwerty)", text, re.I):
        score -= 25
    score = max(0, min(100, score))
    if score >= 80:
        return score, "strong", "#9ece6a"
    if score >= 55:
        return score, "good", "#7dcfff"
    if score >= 30:
        return score, "weak", "#f7c66d"
    return score, "very weak", "#f7768e"
class VaultResourceDialog(QtWidgets.QDialog):
    """Add or edit one vault resource.

    The dialog is the single place a resource's fields are assembled, so the
    add path and the edit path cannot drift apart.  For a password entry the
    body is presented as the secret itself, with a generator next to it.
    """

    def __init__(self, parent, section="scripts", record=None, title="Resource"):
        super().__init__(parent)
        self.section = section
        self.record = record or {}
        self.setWindowTitle(title)
        self.setModal(True)
        self.resize(560, 460)

        layout = QtWidgets.QVBoxLayout(self)
        form = QtWidgets.QFormLayout()

        self.title_edit = QtWidgets.QLineEdit(self.record.get("title", ""))
        form.addRow("Name:", self.title_edit)

        self.section_combo = QtWidgets.QComboBox()
        for key, _lk, fallback, _detail in VAULT_SECTIONS:
            self.section_combo.addItem(fallback, key)
        index = self.section_combo.findData(
            self.record.get("section") or section)
        self.section_combo.setCurrentIndex(max(0, index))
        form.addRow("Section:", self.section_combo)

        self.tags_edit = QtWidgets.QLineEdit(self.record.get("tags", ""))
        form.addRow("Tags:", self.tags_edit)

        self.desc_edit = QtWidgets.QLineEdit(self.record.get("description", ""))
        form.addRow("Description:", self.desc_edit)
        layout.addLayout(form)

        self.flags = {}
        flag_row = QtWidgets.QHBoxLayout()
        known = self.record.get("flags") or {}
        for key, _lk, fallback in VAULT_FLAGS:
            box = QtWidgets.QCheckBox(fallback)
            box.setChecked(bool(known.get(key)))
            self.flags[key] = box
            flag_row.addWidget(box)
        flag_row.addStretch()
        layout.addLayout(flag_row)

        # ---- the body, with a secret generator for password entries -------
        body_row = QtWidgets.QHBoxLayout()
        body_row.addWidget(QtWidgets.QLabel("Content:"))
        body_row.addStretch()
        self.btn_generate = QtWidgets.QPushButton("Generate secret")
        self.btn_generate.clicked.connect(self.generate_secret)
        self.btn_reveal = QtWidgets.QPushButton("Reveal")
        self.btn_reveal.setCheckable(True)
        self.btn_reveal.toggled.connect(self.toggle_reveal)
        body_row.addWidget(self.btn_generate)
        body_row.addWidget(self.btn_reveal)
        layout.addLayout(body_row)

        self.body_edit = QtWidgets.QPlainTextEdit(self.record.get("body", ""))
        self.body_edit.setFont(QFont(VAULT_FONT, VAULT_FONT_SIZE))
        layout.addWidget(self.body_edit, 1)

        self.length_spin = QtWidgets.QSpinBox()
        self.length_spin.setRange(8, 128)
        self.length_spin.setValue(24)
        length_row = QtWidgets.QHBoxLayout()
        length_row.addWidget(QtWidgets.QLabel("Secret length:"))
        length_row.addWidget(self.length_spin)
        length_row.addStretch()
        layout.addLayout(length_row)

        buttons = QtWidgets.QHBoxLayout()
        self.btn_cancel = QtWidgets.QPushButton("Cancel")
        self.btn_cancel.clicked.connect(self.reject)
        self.btn_ok = QtWidgets.QPushButton("Save")
        self.btn_ok.setDefault(True)
        self.btn_ok.clicked.connect(self.try_accept)
        buttons.addStretch()
        buttons.addWidget(self.btn_cancel)
        buttons.addWidget(self.btn_ok)
        layout.addLayout(buttons)

    def toggle_reveal(self, shown):
        self.body_edit.setVisible(True)
        if shown:
            self.btn_reveal.setText("Hide")
        else:
            self.btn_reveal.setText("Reveal")

    def generate_secret(self):
        """Fill the body with a strong random secret."""
        import secrets
        import string
        alphabet = (string.ascii_letters + string.digits + "!@#$%^&*-_=+")
        length = max(8, int(self.length_spin.value()))
        while True:
            value = "".join(secrets.choice(alphabet) for _ in range(length))
            # Guarantee the variety the strength meter rewards.
            if (any(c.islower() for c in value)
                    and any(c.isupper() for c in value)
                    and any(c.isdigit() for c in value)
                    and any(c in "!@#$%^&*-_=+" for c in value)):
                break
        self.body_edit.setPlainText(value)

    def try_accept(self):
        if not self.title_edit.text().strip():
            self.title_edit.setFocus()
            return
        self.accept()

    def values(self):
        """The edited resource, ready for the store."""
        return {
            "title": self.title_edit.text().strip(),
            "section": self.section_combo.currentData(),
            "tags": self.tags_edit.text().strip(),
            "description": self.desc_edit.text().strip(),
            "body": self.body_edit.toPlainText(),
            "flags": {key: box.isChecked() for key, box in self.flags.items()},
        }
class VaultTab(QtWidgets.QWidget):
    """The encrypted, isolated private safe.

    The tab owns one :class:`VaultSecurityCore` and one :class:`VaultStore`.
    Nothing in the store is reachable until the core reports an open session, and
    every action that could move data out of the vault (export, integration)
    re-checks the session first -- so a widget that was left enabled cannot leak
    anything on its own.
    """

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.core = None
        self.store = None
        self._current_id = None
        self._hidden_mode = False
        self._read_only = False
        self._search_fields = set()
        # The section names as last translated, so a count refresh keeps them.
        self._section_labels = {}
        self._build_ui()
        self.open_vault(self._default_dir(), silent=True)

    # ------------------------------------------------------------- helpers
    def t(self, key, fallback=None, **kwargs):
        """Look a label up in the main window's translations."""
        try:
            return self.window.t(key, fallback, **kwargs)
        except Exception:
            return fallback or key

    def _default_dir(self):
        # The vault store is an encrypted database, so it lives in db\vault.
        base = (getattr(self.window, "db_dir", None)
                or getattr(self.window, "DB_DIR", None)
                or app_paths.ensure_db_dir())
        return os.path.join(base, VAULT_DIR)

    def _announce(self, detail):
        """Record a vault action on the shared audit timeline."""
        try:
            self.core.record("vault_item_read", detail)
        except Exception:
            pass
        audit = getattr(self.window, "audit_tab", None)
        if audit is not None and hasattr(audit, "log_change"):
            try:
                audit.log_change("modules", "vault", detail, "vault")
                return
            except Exception:
                pass
        notify = getattr(self.window, "notify_audit", None)
        if callable(notify):
            try:
                notify("module_updated", where="vault", detail=detail)
            except Exception:
                pass

    def _status(self, message):
        self.status.setText(message)
        return message

    # ------------------------------------------------------------- lifecycle
    def open_vault(self, directory, silent=False):
        """Open (or create) a vault directory and load its configuration."""
        try:
            self.store = VaultStore(directory)
        except Exception as exc:
            if not silent:
                self._status(str(exc))
            return None
        self.core = VaultSecurityCore(directory)
        self.directory_label.setText(os.path.basename(directory))
        self.directory_label.setToolTip(os.path.abspath(directory))
        if not self.core.has_key_material():
            # A brand new vault: it exists, but it is locked and empty until a
            # master password is chosen.
            self._status(self.t("vault_new", "New vault. Set a master password."))
        self.after_state_change()
        return self.core

    def needs_creation(self):
        return bool(self.core) and not self.core.has_key_material()

    def is_open(self):
        return bool(self.core) and not self.core.locked

    def require_open(self, action="this action"):
        """Guard for an action that needs an authenticated session."""
        if self.is_open():
            return True
        self._status(self.t("vault_locked_action",
                            "Locked. Unlock the vault to {action}.").format(
            action=action))
        return False
    # ------------------------------------------------------------------- UI
    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)

        # ---- header: title, store, state ---------------------------------
        header = QtWidgets.QHBoxLayout()
        self.title_label = QtWidgets.QLabel(self.t("vault", "VAULT"))
        self.title_label.setStyleSheet(
            "font-size: 14px; font-weight: bold; color: #8ab4f8;")
        header.addWidget(self.title_label)
        self.directory_label = QtWidgets.QLabel("")
        self.directory_label.setStyleSheet("color: #9aa0a6;")
        header.addWidget(self.directory_label)
        header.addStretch()
        self.state_label = QtWidgets.QLabel("")
        self.state_label.setStyleSheet("font-weight: bold; color: #f7768e;")
        header.addWidget(self.state_label)
        self.count_label = QtWidgets.QLabel("")
        self.count_label.setStyleSheet("color: #9aa0a6;")
        header.addWidget(self.count_label)
        layout.addLayout(header)

        # ---- security row -------------------------------------------------
        security_row = QtWidgets.QHBoxLayout()
        self.btn_create = QtWidgets.QPushButton(
            self.t("vault_create", "Create vault..."))
        self.btn_create.clicked.connect(self.create_vault)
        self.btn_unlock = QtWidgets.QPushButton(
            self.t("vault_unlock", "Unlock..."))
        self.btn_unlock.clicked.connect(self.unlock_vault)
        self.btn_lock = QtWidgets.QPushButton(self.t("vault_lock", "Lock"))
        self.btn_lock.clicked.connect(self.lock_vault)
        self.btn_change_password = QtWidgets.QPushButton(
            self.t("vault_change_password", "Change password..."))
        self.btn_change_password.clicked.connect(self.change_password)
        self.btn_policy = QtWidgets.QPushButton(
            self.t("vault_policy", "Security policy..."))
        self.btn_policy.clicked.connect(self.edit_policy)
        self.btn_open_dir = QtWidgets.QPushButton(
            self.t("vault_open_dir", "Open vault folder"))
        self.btn_open_dir.clicked.connect(self.open_vault_folder)
        for button in (self.btn_create, self.btn_unlock, self.btn_lock,
                       self.btn_change_password, self.btn_policy,
                       self.btn_open_dir):
            security_row.addWidget(button)
        security_row.addStretch()
        self.idle_label = QtWidgets.QLabel("")
        self.idle_label.setStyleSheet("color: #9aa0a6;")
        security_row.addWidget(self.idle_label)
        layout.addLayout(security_row)

        self.animation = VaultUnlockAnimation(self)
        self.animation.hide()
        layout.addWidget(self.animation)

        # ---- resource row -------------------------------------------------
        resource_row = QtWidgets.QHBoxLayout()
        self.btn_add = QtWidgets.QPushButton(self.t("vault_add", "Add"))
        self.btn_add.clicked.connect(self.add_resource)
        self.btn_edit = QtWidgets.QPushButton(self.t("vault_edit", "Edit"))
        self.btn_edit.clicked.connect(self.edit_resource)
        self.btn_duplicate = QtWidgets.QPushButton(
            self.t("vault_duplicate", "Copy"))
        self.btn_duplicate.clicked.connect(self.duplicate_resource)
        self.btn_move = QtWidgets.QPushButton(self.t("vault_move", "Move..."))
        self.btn_move.clicked.connect(self.move_resource)
        self.btn_delete = QtWidgets.QPushButton(self.t("vault_delete", "Delete"))
        self.btn_delete.clicked.connect(self.delete_resources)

        self.btn_flags = QtWidgets.QToolButton()
        self.btn_flags.setText(self.t("vault_flags_menu", "Flags..."))
        self.btn_flags.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.flags_menu = QtWidgets.QMenu(self.btn_flags)
        self.flag_actions = {}
        for key, label_key, fallback in VAULT_FLAGS:
            action = self.flags_menu.addAction(self.t(label_key, fallback))
            action.setCheckable(True)
            action.triggered.connect(
                lambda checked, k=key: self.toggle_flag(k, checked))
            self.flag_actions[key] = action
        self.btn_flags.setMenu(self.flags_menu)

        self.btn_hidden_mode = QtWidgets.QPushButton(
            self.t("vault_hidden_mode", "Hidden mode"))
        self.btn_hidden_mode.setCheckable(True)
        self.btn_hidden_mode.toggled.connect(self.toggle_hidden_mode)
        self.btn_readonly = QtWidgets.QPushButton(
            self.t("vault_readonly_lock", "Read-only lock"))
        self.btn_readonly.setCheckable(True)
        self.btn_readonly.toggled.connect(self.toggle_read_only)

        for button in (self.btn_add, self.btn_edit, self.btn_duplicate,
                       self.btn_move, self.btn_delete, self.btn_flags,
                       self.btn_hidden_mode, self.btn_readonly):
            resource_row.addWidget(button)

        # ---- import / export controls ------------------------------------
        self.btn_import = QtWidgets.QPushButton(
            self.t("vault_import", "Import..."))
        self.btn_import.clicked.connect(self.import_resources)
        self.btn_export = QtWidgets.QToolButton()
        self.btn_export.setText(self.t("vault_export", "Export..."))
        self.btn_export.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.export_menu = QtWidgets.QMenu(self.btn_export)
        self.act_export_vault = self.export_menu.addAction(
            self.t("vault_export_vault", "Export the vault (re-authenticate)"))
        self.act_export_section = self.export_menu.addAction(
            self.t("vault_export_section", "Export the open section (inside the vault)"))
        self.act_export_snapshots = self.export_menu.addAction(
            self.t("vault_export_snapshots", "Export the snapshots"))
        self.act_export_config = self.export_menu.addAction(
            self.t("vault_export_config", "Export the vault configuration"))
        self.act_export_vault.triggered.connect(
            lambda: self.export_vault(allow_outside=True))
        self.act_export_section.triggered.connect(
            lambda: self.export_vault(allow_outside=False))
        self.act_export_snapshots.triggered.connect(self.export_snapshots)
        self.act_export_config.triggered.connect(self.export_configuration)
        self.btn_export.setMenu(self.export_menu)

        self.btn_reports = QtWidgets.QToolButton()
        self.btn_reports.setText(self.t("vault_reports_menu", "Reports..."))
        self.btn_reports.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.reports_menu = QtWidgets.QMenu(self.btn_reports)
        self.report_actions = {}
        for key, label_key, fallback in VAULT_REPORTS:
            action = self.reports_menu.addAction(self.t(label_key, fallback))
            action.triggered.connect(
                lambda _checked=False, k=key: self.show_report(k))
            self.report_actions[key] = action
        self.btn_reports.setMenu(self.reports_menu)

        self.btn_integrate = QtWidgets.QToolButton()
        self.btn_integrate.setText(self.t("vault_integrate_menu", "Send to..."))
        self.btn_integrate.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.integrate_menu = QtWidgets.QMenu(self.btn_integrate)
        self.integrate_actions = {}
        for key, label_key, fallback, _attr in VAULT_INTEGRATIONS:
            action = self.integrate_menu.addAction(self.t(label_key, fallback))
            action.triggered.connect(
                lambda _checked=False, k=key: self.run_integration(k))
            self.integrate_actions[key] = action
        self.btn_integrate.setMenu(self.integrate_menu)

        for widget in (self.btn_import, self.btn_export, self.btn_reports,
                       self.btn_integrate):
            resource_row.addWidget(widget)
        resource_row.addStretch()
        layout.addLayout(resource_row)

        # ---- search row ---------------------------------------------------
        search_row = QtWidgets.QHBoxLayout()
        self.search_edit = QtWidgets.QLineEdit()
        self.search_edit.setPlaceholderText(
            self.t("vault_search_hint", "Search the vault..."))
        self.search_edit.returnPressed.connect(self.run_search)
        search_row.addWidget(self.search_edit, 1)
        self.search_field_combo = QtWidgets.QComboBox()
        self.search_field_combo.addItem(
            self.t("vault_search_all", "Every aspect"), "")
        for key, label_key, fallback in VAULT_SEARCH_FIELDS:
            self.search_field_combo.addItem(self.t(label_key, fallback), key)
        search_row.addWidget(self.search_field_combo)
        self.btn_search = QtWidgets.QPushButton(self.t("vault_search", "Search"))
        self.btn_search.clicked.connect(self.run_search)
        self.btn_clear_search = QtWidgets.QPushButton(
            self.t("vault_search_clear", "Clear"))
        self.btn_clear_search.clicked.connect(self.clear_search)
        search_row.addWidget(self.btn_search)
        search_row.addWidget(self.btn_clear_search)
        layout.addLayout(search_row)

        # ---- the five internal tabs --------------------------------------
        self.section_tabs = QtWidgets.QTabWidget()
        self.trees = {}
        for key, label_key, fallback, _detail in VAULT_SECTIONS:
            tree = self._build_section_page(key, self.t(label_key, fallback))
            self.trees[key] = tree
        layout.addWidget(self.section_tabs, 1)

        self.status = QtWidgets.QLabel("")
        self.status.setStyleSheet("color: #9aa0a6;")
        layout.addWidget(self.status)

        # The auto-lock clock: a single timer that asks the core whether the
        # session has gone idle, so the rule lives in one place.
        self._clock = QtCore.QTimer(self)
        self._clock.timeout.connect(self._on_clock)
        self._clock.start(1000)

    def _build_section_page(self, section, title):
        """One internal tab: a filtered tree of that section's resources."""
        page = QtWidgets.QWidget()
        page_layout = QtWidgets.QVBoxLayout(page)
        tree = QtWidgets.QTreeWidget()
        tree.setColumnCount(5)
        tree.setHeaderLabels([
            self.t("vault_col_name", "Name"),
            self.t("vault_col_flags", "Flags"),
            self.t("vault_col_tags", "Tags"),
            self.t("vault_col_size", "Size"),
            self.t("vault_col_modified", "Modified"),
        ])
        tree.setRootIsDecorated(False)
        tree.setAlternatingRowColors(True)
        tree.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        tree.itemSelectionChanged.connect(self.on_selection_changed)
        tree.itemDoubleClicked.connect(self.on_double_click)
        page_layout.addWidget(tree)
        self.section_tabs.addTab(page, title)
        return tree
    # ------------------------------------------------------------- security
    def create_vault(self):
        """Choose the master password and key the new vault."""
        if self.core is None:
            return None
        if self.core.has_key_material():
            answer = QtWidgets.QMessageBox.question(
                self, self.t("vault", "VAULT"),
                self.t("vault_rekey_warning",
                       "The vault already has a master password. "
                       "Replacing it re-encrypts everything. Continue?"),
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No)
            if answer != QtWidgets.QMessageBox.Yes:
                return None
        dialog = VaultPasswordDialog(self, self.t("vault_create", "Create vault..."),
                                     mode="create")
        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            return None
        outcome = self.core.create(dialog.password(), dialog.confirmation())
        if not outcome["ok"]:
            self._status(outcome["detail"])
            return outcome
        # Key the repository with the same password, so the store is encrypted
        # at rest with exactly what the user just chose.
        try:
            if not self.store.repo.is_encrypted():
                self.store.repo.set_password(dialog.password())
        except Exception as exc:
            self._status(str(exc))
        self._announce("vault created")
        self._status(self.t("vault_created", "Vault created."))
        self.after_state_change()
        return outcome

    def unlock_vault(self):
        """Prompt for the master password and open a session."""
        if self.core is None:
            return None
        if not self.core.has_key_material():
            self._status(self.t("vault_no_password",
                                "No master password is set yet."))
            return None
        dialog = VaultPasswordDialog(self, self.t("vault_unlock", "Unlock..."),
                                     mode="unlock")
        if self.core.is_locked_out():
            dialog.set_locked_out(self.core.lockout_remaining())
        while dialog.exec_() == QtWidgets.QDialog.Accepted:
            self.animation.start("unlocking")
            self.animation.repaint()
            if dialog.challenge_row.isVisible():
                outcome = self.core.confirm_challenge(dialog.challenge_edit.text())
            else:
                outcome = self.core.unlock(dialog.password())
            self.animation.stop()
            if outcome["ok"]:
                self._open_store_session(dialog.password())
                self._announce("vault unlocked")
                self._status(self.t("vault_unlocked", "Vault unlocked."))
                self.after_state_change()
                return outcome
            if outcome.get("code") == "needs_2fa":
                challenge = self.core.open_challenge()["challenge"]
                dialog.show_challenge(challenge)
                continue
            if outcome.get("code") == "locked_out":
                dialog.set_locked_out(self.core.lockout_remaining())
                continue
            dialog.message.setText(outcome["detail"])
        self.after_state_change()
        return None

    def _open_store_session(self, password):
        """Unlock the repository with the same master password."""
        try:
            if self.store.repo.is_encrypted():
                if not self.store.repo.unlock(password):
                    self._status(self.t("vault_store_mismatch",
                                        "Store and vault passwords differ."))
            else:
                self.store.repo.set_password(password)
        except Exception as exc:
            self._status(str(exc))

    def lock_vault(self):
        """Close the session: the key is dropped from memory."""
        if self.core is None:
            return None
        outcome = self.core.lock(self.t("vault_locked", "locked"))
        self._announce("vault locked")
        self._status(self.t("vault_locked_msg", "Vault locked."))
        self.after_state_change()
        return outcome

    def change_password(self):
        """Re-key the vault after confirming the current password."""
        if not self.require_open("change the password"):
            return None
        dialog = VaultPasswordDialog(self, self.t("vault_change_password",
                                                  "Change password..."),
                                     mode="create")
        dialog.heading.setText(self.t("vault_new_password",
                                      "Choose the new master password"))
        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            return None
        outcome = self.core.create(dialog.password(), dialog.confirmation())
        if not outcome["ok"]:
            self._status(outcome["detail"])
            return outcome
        try:
            self.store.repo.set_password(dialog.password())
        except Exception as exc:
            self._status(str(exc))
        self._announce("vault password changed")
        self._status(self.t("vault_password_changed", "Master password changed."))
        return outcome

    def edit_policy(self):
        """A dialog over every policy field, so the rules are visible."""
        if self.core is None:
            return None
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle(self.t("vault_policy", "Security policy"))
        dialog.resize(520, 480)
        layout = QtWidgets.QVBoxLayout(dialog)
        form = QtWidgets.QFormLayout()
        widgets = {}
        for key, label_key, fallback, default in VAULT_POLICY:
            value = self.core.policy.get(key, default)
            if isinstance(default, bool):
                widget = QtWidgets.QCheckBox()
                widget.setChecked(bool(value))
            else:
                widget = QtWidgets.QSpinBox()
                widget.setRange(0, 86400)
                widget.setValue(int(value or 0))
            widgets[key] = widget
            form.addRow(self.t(label_key, fallback), widget)
        layout.addLayout(form)
        note = QtWidgets.QLabel(self.t(
            "vault_policy_note",
            "Auto-lock drops the key from memory after inactivity. "
            "Auto-destroy removes the key material after the lockout limit."))
        note.setWordWrap(True)
        note.setStyleSheet("color: #9aa0a6;")
        layout.addWidget(note)
        layout.addStretch()
        buttons = QtWidgets.QHBoxLayout()
        save = QtWidgets.QPushButton(self.t("vault_policy_save", "Apply"))
        save.clicked.connect(dialog.accept)
        close = QtWidgets.QPushButton(self.t("vault_close", "Close"))
        close.clicked.connect(dialog.reject)
        buttons.addStretch()
        buttons.addWidget(close)
        buttons.addWidget(save)
        layout.addLayout(buttons)
        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            return None
        changes = {}
        for key, widget in widgets.items():
            changes[key] = widget.isChecked() if isinstance(
                widget, QtWidgets.QCheckBox) else widget.value()
        changed = self.core.set_policy(changes)
        self._announce("vault policy changed: %s" % ", ".join(changed))
        self._status(self.t("vault_policy_saved",
                            "Policy applied ({count} change(s)).").format(
            count=len(changed)))
        self.after_state_change()
        return changed

    def toggle_hidden_mode(self, enabled):
        """Hide every flagged-hidden resource from the lists."""
        self._hidden_mode = bool(enabled)
        if enabled and not self.require_open("use hidden mode"):
            self.btn_hidden_mode.setChecked(False)
            self._hidden_mode = False
            return
        self.refresh()
        self._status(self.t("vault_hidden_on" if enabled else "vault_hidden_off",
                            "Hidden mode on" if enabled
                            else "Hidden mode off"))

    def toggle_read_only(self, enabled):
        """A lock that stops every write, whoever is signed in."""
        self._read_only = bool(enabled)
        self.refresh()
        self._status(self.t("vault_readonly_on" if enabled
                            else "vault_readonly_off",
                            "Read-only lock on" if enabled
                            else "Read-only lock off"))
    # --------------------------------------------------- resource management
    def current_section(self):
        """The section whose internal tab is open."""
        index = self.section_tabs.currentIndex()
        if 0 <= index < len(VAULT_SECTION_KEYS):
            return VAULT_SECTION_KEYS[index]
        return VAULT_SECTION_KEYS[0]

    def selected_ids(self):
        """The ids of every selected resource in the open section."""
        tree = self.trees.get(self.current_section())
        if tree is None:
            return []
        return [item.data(0, QtCore.Qt.UserRole)
                for item in tree.selectedItems()
                if item.data(0, QtCore.Qt.UserRole) is not None]

    def _guard_write(self, action="change the vault"):
        """Every write passes through here: open session and not read-only."""
        if not self.require_open(action):
            return False
        if self._read_only:
            self._status(self.t("vault_readonly_blocked",
                                "The read-only lock is on. Turn it off first."))
            return False
        return True

    def add_resource(self):
        if not self._guard_write("add a resource"):
            return None
        section = self.current_section()
        dialog = VaultResourceDialog(
            self, section, None,
            self.t("vault_add_title", "Add a resource"))
        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            return None
        values = dialog.values()
        doc_id = self.store.add(values["section"], values["title"],
                                values["body"], values["tags"],
                                values["description"], None, values["flags"])
        self._announce("vault item added: %s" % values["title"])
        self._status(self.t("vault_added", "Added {name}.").format(
            name=values["title"]))
        self.refresh()
        return doc_id

    def edit_resource(self):
        if not self._guard_write("edit a resource"):
            return None
        ids = self.selected_ids()
        if not ids:
            self._status(self.t("vault_select_first",
                                "Select a resource first."))
            return None
        record = self.store.get(ids[0])
        if record is None:
            return None
        if record["flags"].get("readonly") and not self._read_only:
            self._status(self.t("vault_item_readonly",
                                "That resource is marked read-only."))
            return None
        dialog = VaultResourceDialog(self, record.get("section"),
                                     record,
                                     self.t("vault_edit_title", "Edit resource"))
        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            return None
        values = dialog.values()
        self.store.update(ids[0], title=values["title"], body=values["body"],
                          tags=values["tags"],
                          description=values["description"],
                          section=values["section"], flags=values["flags"])
        self._announce("vault item updated: %s" % values["title"])
        self._status(self.t("vault_updated", "Updated {name}.").format(
            name=values["title"]))
        self.refresh()
        return ids[0]

    def duplicate_resource(self):
        if not self._guard_write("copy a resource"):
            return None
        ids = self.selected_ids()
        if not ids:
            self._status(self.t("vault_select_first", "Select a resource first."))
            return None
        record = self.store.get(ids[0])
        if record is None:
            return None
        new_id = self.store.add(record["section"],
                                record["title"] + " (copy)",
                                record["body"], record["tags"],
                                record["description"], None, record["flags"])
        self._announce("vault item copied: %s" % record["title"])
        self.refresh()
        return new_id

    def move_resource(self):
        if not self._guard_write("move a resource"):
            return None
        ids = self.selected_ids()
        if not ids:
            self._status(self.t("vault_select_first", "Select a resource first."))
            return None
        labels = [fallback for _key, _lk, fallback, _d in VAULT_SECTIONS]
        choice, accepted = QtWidgets.QInputDialog.getItem(
            self, self.t("vault_move", "Move..."),
            self.t("vault_move_to", "Move to section:"), labels, 0, False)
        if not accepted:
            return None
        target = VAULT_SECTION_KEYS[labels.index(choice)]
        moved = 0
        for doc_id in ids:
            if self.store.update(doc_id, section=target):
                moved += 1
        self._announce("vault items moved: %d" % moved)
        self._status(self.t("vault_moved", "Moved {count} resource(s).").format(
            count=moved))
        self.refresh()
        return moved

    def delete_resources(self):
        if not self._guard_write("delete a resource"):
            return None
        ids = self.selected_ids()
        if not ids:
            self._status(self.t("vault_select_first", "Select a resource first."))
            return None
        # Deleting is irreversible and holds secrets, so it is confirmed and
        # the dialog names what is about to go.
        names = []
        for doc_id in ids:
            record = self.store.get(doc_id)
            if record:
                names.append(record.get("title", ""))
        answer = QtWidgets.QMessageBox.question(
            self, self.t("vault_delete", "Delete"),
            self.t("vault_delete_confirm",
                   "Delete {count} resource(s)? This cannot be undone.\n\n"
                   "{names}").format(count=len(ids),
                                     names="\n".join(names[:20])),
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No)
        if answer != QtWidgets.QMessageBox.Yes:
            return None
        removed = self.store.delete(ids)
        self._announce("vault items deleted: %d" % removed)
        self._status(self.t("vault_deleted", "Deleted {count} resource(s).").format(
            count=removed))
        self.refresh()
        return removed

    def toggle_flag(self, flag, enabled):
        """Set one flag on every selected resource."""
        if not self._guard_write("change flags"):
            return None
        ids = self.selected_ids()
        if not ids:
            self._status(self.t("vault_select_first", "Select a resource first."))
            return None
        changed = 0
        for doc_id in ids:
            record = self.store.get(doc_id)
            if record is None:
                continue
            flags = dict(record["flags"])
            flags[flag] = bool(enabled)
            if self.store.update(doc_id, flags=flags):
                changed += 1
        self._status(self.t("vault_flags_set",
                            "{flag}: {state} on {count} resource(s)").format(
            flag=VAULT_FLAG_LABELS.get(flag, flag),
            state="on" if enabled else "off", count=changed))
        self.refresh()
        return changed

    def on_selection_changed(self):
        """Reflect the selection's flags in the menu check marks."""
        ids = self.selected_ids()
        flags = {}
        if ids:
            record = self.store.get(ids[0]) if self.store else None
            flags = (record or {}).get("flags") or {}
        for key, action in self.flag_actions.items():
            action.blockSignals(True)
            action.setChecked(bool(flags.get(key)))
            action.blockSignals(False)

    def on_double_click(self, item, _column):
        """Double-clicking opens the resource for editing."""
        if item.data(0, QtCore.Qt.UserRole) is not None:
            self.edit_resource()
    # ------------------------------------------------------------ refreshing
    def _on_clock(self):
        """The one timer: auto-lock, the idle readout and the session key."""
        if self.core is None:
            return
        if self.core.check_auto_lock():
            self._announce("vault auto-locked")
            self._status(self.t("vault_autolocked",
                                "Vault locked itself after inactivity."))
            self.after_state_change()
            return
        if self.core.session_key_expired() and not self.core.locked:
            self.core.rotate_session_key()
        if self.is_open():
            self.idle_label.setText(self.t(
                "vault_idle", "idle {time}").format(
                time=_fmt_seconds(self.core.idle_seconds())))

    def after_state_change(self):
        """Bring every widget in line with the core's current state."""
        locked = self.core is None or self.core.locked
        for widget in (self.btn_lock, self.btn_change_password,
                       self.btn_add, self.btn_edit, self.btn_duplicate,
                       self.btn_move, self.btn_delete, self.btn_flags,
                       self.btn_import, self.btn_export, self.btn_reports,
                       self.btn_integrate, self.btn_search,
                       self.btn_hidden_mode, self.btn_readonly):
            widget.setEnabled(not locked)
        self.btn_create.setEnabled(self.core is not None
                                   and not self.core.has_key_material())
        self.btn_unlock.setEnabled(self.core is not None
                                   and self.core.has_key_material() and locked)
        if locked:
            # A locked vault shows nothing: the lists are cleared so no title
            # stays on screen after the session ends.
            for tree in self.trees.values():
                tree.clear()
            self.state_label.setText(self.t("vault_state_locked", "LOCKED"))
            self.state_label.setStyleSheet(
                "font-weight: bold; color: #f7768e;")
            self.count_label.setText("")
            self.idle_label.setText("")
            return
        if self._hidden_mode:
            self.state_label.setText(self.t("vault_state_hidden", "HIDDEN MODE"))
            self.state_label.setStyleSheet(
                "font-weight: bold; color: #bb9af7;")
        elif self._read_only:
            self.state_label.setText(self.t("vault_state_readonly", "READ-ONLY"))
            self.state_label.setStyleSheet(
                "font-weight: bold; color: #f7c66d;")
        else:
            self.state_label.setText(self.t("vault_state_unlocked", "UNLOCKED"))
            self.state_label.setStyleSheet(
                "font-weight: bold; color: #9ece6a;")
        self.refresh()

    def refresh(self):
        """Reload every section tree from the store."""
        if self.store is None or self.core is None:
            return
        if self.core.locked:
            for tree in self.trees.values():
                tree.clear()
            return
        needle = self.search_edit.text().strip() if hasattr(self, "search_edit") \
            else ""
        field = self.search_field_combo.currentData() \
            if hasattr(self, "search_field_combo") else ""
        for section, tree in self.trees.items():
            if needle:
                records = self.store.search(
                    needle, section=section,
                    fields={field} if field else None,
                    include_hidden=self._hidden_mode)
            else:
                records = self.store.list(section=section,
                                          include_hidden=self._hidden_mode)
            self._fill_tree(tree, records)
        self._refresh_counts()

    def _selected_in(self, tree):
        """The ids selected in one tree (never None)."""
        return [item.data(0, QtCore.Qt.UserRole)
                for item in tree.selectedItems()
                if item.data(0, QtCore.Qt.UserRole) is not None]

    def _refresh_counts(self):
        """Update the header count and the five section labels."""
        counts = self.store.counts()
        shown = sum(len(self.store.list(section=section,
                                        include_hidden=self._hidden_mode))
                    for section in counts)
        self.count_label.setText(self.t(
            "vault_counts", "{shown} shown / {total} stored").format(
            shown=shown, total=sum(counts.values())))
        # The label text stays whatever retranslate() last set, so a count
        # refresh never silently reverts the section names to English.
        for index, (key, label_key, fallback, _detail) in enumerate(VAULT_SECTIONS):
            name = self._section_labels.get(key) or self.t(label_key, fallback)
            total = len(self.store.snapshots()) if key == "snapshots" \
                else counts.get(key, 0)
            self.section_tabs.setTabText(index, "%s (%d)" % (name, total))

    def _fill_tree(self, tree, records):
        """One resource per row, newest change first.

        The rows are rebuilt on every refresh, so the selection is carried
        over by id: without that, a refresh after an action would drop the
        selection and the next action would have nothing to work on.
        """
        keep = set(self._selected_in(tree))
        restored = []
        tree.clear()
        for record in sorted(records,
                             key=lambda item: item.get("modified") or "",
                             reverse=True):
            body = record.get("body") or ""
            item = QtWidgets.QTreeWidgetItem([
                record.get("title", ""),
                flags_text(record.get("flags")),
                record.get("tags", ""),
                "%d B" % len(body),
                record.get("modified", ""),
            ])
            item.setData(0, QtCore.Qt.UserRole, record.get("id"))
            item.setToolTip(0, record.get("description", ""))
            if record["flags"].get("critical"):
                item.setForeground(0, QtGui.QColor("#f7768e"))
            elif record["flags"].get("pinned"):
                item.setForeground(0, QtGui.QColor("#8ab4f8"))
            if record["flags"].get("hidden"):
                item.setForeground(3, QtGui.QColor("#bb9af7"))
            tree.addTopLevelItem(item)
            if record.get("id") in keep:
                restored.append(item)
        for item in restored:
            item.setSelected(True)

    # ---------------------------------------------------------------- search
    def run_search(self):
        """Filter every section by the search box."""
        if not self.require_open("search the vault"):
            return None
        needle = self.search_edit.text().strip()
        if not needle:
            self.refresh()
            self._status(self.t("vault_search_empty", "Showing everything."))
            return None
        field = self.search_field_combo.currentData()
        # A search always looks at hidden resources too: a hidden secret that
        # cannot be found is a secret that has been lost.  The rows it returns
        # still carry their Hidden flag, and the list itself stays filtered.
        found = self.store.search(needle, fields={field} if field else None,
                                  include_hidden=True)
        self.refresh()
        hidden = sum(1 for doc in found if doc["flags"].get("hidden"))
        message = self.t("vault_search_found",
                         "Found {count} resource(s).").format(count=len(found))
        if hidden and not self._hidden_mode:
            message += "" + self.t(
                "vault_search_hidden",
                "({count} hidden; turn hidden mode on to see them)").format(
                count=hidden)
        self._status(message)
        self._announce("vault search: %s" % needle)
        return found

    def clear_search(self):
        self.search_edit.clear()
        self.refresh()
        self._status(self.t("vault_search_empty", "Showing everything."))
    # -------------------------------------------------------------- analysis
    def analyse_resource(self, kind=None):
        """One analysis view over the selected resource."""
        if not self.require_open("analyse the vault"):
            return None
        ids = self.selected_ids()
        if not ids:
            self._status(self.t("vault_select_first", "Select a resource first."))
            return None
        record = self.store.get(ids[0])
        if record is None:
            return None
        # The caller may name the view; otherwise the combo decides.  The two
        # are spelled out because a bare conditional expression here binds the
        # wrong way round and would silently always analyse the structure.
        if not kind:
            kind = (self.analysis_combo.currentData()
                    if hasattr(self, "analysis_combo") else "structure")
        if kind == "structure":
            result = analyse_document(record)
            text = "\n".join("%-18s %s" % ("%s:" % key, value)
                             for key, value in result.items())
        elif kind == "integrity":
            result = analyse_script(record.get("body", ""))
            text = "\n".join("%-18s %s" % ("%s:" % key, value)
                             for key, value in result.items())
            recorded = record.get("digest") or ""
            found = VaultSecurityCore.digest(record.get("body") or "")
            text += "\n%-18s %s" % ("digest_matches:",
                                    hmac.compare_digest(recorded, found))
            if not hmac.compare_digest(recorded, found):
                self.core.detect_manipulation(
                    "%s does not match its recorded digest"
                    % record.get("title", ""), recorded, found)
        elif kind == "risk":
            result = analyse_risk(record)
            lines = ["%-18s %s" % ("level:", result["level"]),
                     "%-18s %s" % ("score:", result["score"]), ""]
            for item in result["findings"]:
                lines.append("  %-8s %-14s %s" % (item["severity"],
                                                  item["needle"],
                                                  item["reason"]))
            text = "\n".join(lines) or "no findings"
        else:
            payload = self._snapshot_records()
            rows = change_history(record, payload)
            text = "\n".join("  %s  %s  %d line(s)"
                             % (row.get("time", ""), row.get("digest", "")[:16],
                                row.get("lines", 0)) for row in rows)
        self._show_text(self.t("vault_analysis", "Analysis"), text)
        self._announce("vault analysis: %s" % kind)
        return text

    def _snapshot_records(self):
        """Every item of every snapshot, for the change history."""
        records = []
        for snapshot in self.store.snapshots():
            try:
                with open(snapshot["path"], "r", encoding="utf-8") as handle:
                    payload = json.load(handle)
            except (OSError, ValueError):
                continue
            records.extend(payload.get("items") or [])
        return records

    # --------------------------------------------------------------- reports
    def show_report(self, kind):
        """Render one of the five vault reports in a dialog."""
        if not self.require_open("generate a report"):
            return None
        text = vault_report(kind, self.core, self.store)
        self._show_text(self.t(
            "vault_report_" + kind, VAULT_REPORT_LABELS.get(kind, kind)), text)
        self._announce("vault report: %s" % kind)
        return text

    def save_report(self, kind, directory=None):
        """Write a report to disk so the Reports tab can index it.

        Only ever called with a path the caller chose, and the report itself
        carries no resource bodies -- it names resources, it does not copy them.
        """
        if not self.require_open("save a report"):
            return None
        text = vault_report(kind, self.core, self.store)
        root = directory or os.path.join(self.core.path or self._default_dir(),
                                         "reports")
        try:
            if not os.path.isdir(root):
                os.makedirs(root)
            path = os.path.join(root, "vault_%s_%s.md"
                                % (kind, _now().replace(":", "-")
                                   .replace(" ", "_")))
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(text)
        except OSError as exc:
            self._status(str(exc))
            return None
        self._status(self.t("vault_report_saved", "Report saved: {path}").format(
            path=path))
        reports = getattr(self.window, "reports_tab", None)
        if reports is not None and hasattr(reports, "refresh"):
            try:
                reports.refresh()
            except Exception:
                pass
        return path

    # ----------------------------------------------------------- integration
    def run_integration(self, key):
        """Send the selected resource to another part of the application.

        The vault only hands out content through this method, and it checks the
        session first, so a target that is reached from elsewhere cannot pull
        content out on its own.
        """
        if not self.require_open("send a resource elsewhere"):
            return None
        if key == "snapshots":
            return self.take_snapshot()
        ids = self.selected_ids()
        if not ids:
            self._status(self.t("vault_select_first", "Select a resource first."))
            return None
        record = self.store.get(ids[0])
        if record is None:
            return None
        entry = {"id": None, "name": record.get("title", "vault-item"),
                 "category": self._category_for(record),
                 "code": record.get("body", ""), "history": []}
        window = self.window
        try:
            if key == "editor":
                tab = getattr(window, "editor_tab", None)
                if tab is None or not hasattr(tab, "load_script"):
                    return self._unavailable("Editor")
                tab.load_script(entry)
                window.tabs.setCurrentWidget(tab)
            elif key == "debugger":
                tab = getattr(window, "debugger_tab", None)
                method = getattr(window, "open_in_debugger", None)
                if callable(method):
                    method(entry)
                elif tab is not None and hasattr(tab, "load_script"):
                    tab.load_script(entry)
                    window.tabs.setCurrentWidget(tab)
                else:
                    return self._unavailable("Debugger")
            elif key == "runner":
                tab = getattr(window, "sandbox_tab", None)
                if tab is None:
                    return self._unavailable("Sandbox")
                window.tabs.setCurrentWidget(tab)
            elif key == "audit":
                method = getattr(window, "audit_script", None)
                if callable(method):
                    method(entry)
                else:
                    return self._unavailable("Audit")
            elif key == "reports":
                return self.save_report("security")
            elif key == "diff":
                tab = getattr(window, "diff_tab", None)
                if tab is None or not hasattr(tab, "_entry_to_source"):
                    return self._unavailable("Diff")
                tab._left = tab._entry_to_source(entry)
                tab._right = tab._entry_to_source(entry)
                tab.recompute()
                window.tabs.setCurrentWidget(tab)
            else:
                return None
        except Exception as exc:
            self._status("Integration failed: %s" % exc)
            return None
        self._announce("vault resource sent: %s -> %s"
                       % (record.get("title", ""), key))
        self._status(self.t("vault_sent", "Sent {name} to {target}.").format(
            name=record.get("title", ""), target=key))
        return key

    @staticmethod
    def _category_for(record):
        """Which editor language a vault resource should open as."""
        section = record.get("section")
        if section == "scripts":
            return "Python"
        if section == "passwords":
            return "Other"
        title = (record.get("title") or "").lower()
        if title.endswith(".json"):
            return "JSON"
        return "Other"

    def _unavailable(self, target):
        return self._status(self.t("vault_target_unavailable",
                                   "{target} is not available.").format(
            target=target))
    # --------------------------------------------------------- import/export
    def import_resources(self):
        """Import files into the vault, encrypting them on the way in."""
        if not self._guard_write("import into the vault"):
            return None
        paths, _filter = QtWidgets.QFileDialog.getOpenFileNames(
            self, self.t("vault_import", "Import..."), "",
            self.t("vault_import_filter",
                   "Vault exports and text (*.json *.txt *.md *.py *.cfg *.ini);;"
                   "All files (*.*)"))
        if not paths:
            return None
        section = self.current_section()
        added = 0
        for path in paths:
            if path.lower().endswith(".json"):
                outcome = self.store.import_file(path, section)
                if outcome.get("ok"):
                    added += outcome.get("count", 0)
                    continue
            # Everything else is stored as its own text, with the file name as
            # the title -- and it is encrypted by the store on the way in.
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as handle:
                    body = handle.read()
            except OSError as exc:
                self._status(str(exc))
                continue
            self.store.add(section, os.path.basename(path), body,
                           tags="imported", description=path)
            added += 1
        self._announce("vault items imported: %d" % added)
        self._status(self.t("vault_imported",
                            "Imported {count} resource(s), encrypted.").format(
            count=added))
        self.refresh()
        return added

    def export_vault(self, allow_outside=False, section=None):
        """Export the vault (or one section) to a JSON file.

        An export that is allowed to leave the vault directory re-asks for the
        master password first -- that is the "export only after authentication"
        rule, and it is enforced here rather than trusted to the caller.
        """
        if not self.require_open("export"):
            return None
        section = section or (None if allow_outside else self.current_section())
        if allow_outside:
            dialog = VaultPasswordDialog(self, self.t("vault_export", "Export..."),
                                         mode="unlock")
            dialog.heading.setText(self.t(
                "vault_export_confirm",
                "Confirm the master password to export outside the vault"))
            if dialog.exec_() != QtWidgets.QDialog.Accepted:
                return None
            outcome = self.core.verify(dialog.password())
            if not outcome["ok"]:
                self._status(outcome["detail"])
                return outcome
            suggested = os.path.join(
                self._default_dir(), "vault_export.json")
        else:
            suggested = os.path.join(self.core.path or self._default_dir(),
                                     "export_%s.json" % self.current_section())
        path, _filter = QtWidgets.QFileDialog.getSaveFileName(
            self, self.t("vault_export", "Export..."), suggested,
            "JSON (*.json)")
        if not path:
            return None
        outcome = self.store.export(path, section=section,
                                    allow_outside=allow_outside)
        if not outcome.get("ok"):
            self._status(outcome.get("detail", ""))
            return outcome
        self.core.record("vault_item_exported", "exported to %s" % path)
        self._announce("vault exported: %s" % path)
        self._status(self.t("vault_exported",
                            "Exported {count} resource(s) to {path}.").format(
            count=outcome["count"], path=path))
        return outcome

    def export_snapshots(self):
        """Copy the snapshots out, after re-authenticating."""
        if not self.require_open("export the snapshots"):
            return None
        directory = QtWidgets.QFileDialog.getExistingDirectory(
            self, self.t("vault_export_snapshots", "Export the snapshots"))
        if not directory:
            return None
        copied = 0
        for snapshot in self.store.snapshots():
            target = os.path.join(directory, snapshot["name"])
            try:
                shutil.copyfile(snapshot["path"], target)
                copied += 1
            except OSError as exc:
                self._status(str(exc))
        self.core.record("vault_item_exported", "exported %d snapshot(s)" % copied)
        self._status(self.t("vault_snapshots_exported",
                            "Exported {count} snapshot(s).").format(count=copied))
        return copied

    def export_configuration(self):
        """Write the vault configuration (policy and records, never the key)."""
        if not self.require_open("export the configuration"):
            return None
        path, _filter = QtWidgets.QFileDialog.getSaveFileName(
            self, self.t("vault_export_config", "Export the vault configuration"),
            os.path.join(self._default_dir(), "vault_configuration.json"),
            "JSON (*.json)")
        if not path:
            return None
        try:
            _atomic_write_json(path, self.core.to_dict())
        except OSError as exc:
            self._status(str(exc))
            return None
        self._status(self.t("vault_config_exported",
                            "Configuration written to {path}.").format(path=path))
        return path

    # -------------------------------------------------------------- snapshots
    def take_snapshot(self, label=""):
        """Snapshot the vault, keeping the option to leave regions out."""
        if not self._guard_write("take a snapshot"):
            return None
        outcome = self._snapshot_choice()
        if outcome is None:
            return None
        selected, password = outcome
        if selected is None:
            return None
        path = self.store.snapshot(label or _now())
        self.core.record("vault_snapshot_taken", path)
        self._announce("vault snapshot taken: %s" % path)
        self._status(self.t("vault_snapshot_taken", "Snapshot written: {path}").format(
            path=path))
        self.refresh()
        return path

    def _snapshot_choice(self):
        """Ask which sections to include; returns (set or None, password).

        A snapshot of a section that holds secrets can be restricted the same
        way an export is: the vault asks for the password again when the
        selected set is narrower than the whole vault.
        """
        labels = [fallback for _k, _lk, fallback, _d in VAULT_SECTIONS]
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle(self.t("vault_snapshot", "Snapshot the vault"))
        layout = QtWidgets.QVBoxLayout(dialog)
        boxes = {}
        for index, (key, _lk, fallback, _detail) in enumerate(VAULT_SECTIONS):
            box = QtWidgets.QCheckBox(fallback)
            box.setChecked(True)
            boxes[key] = box
            layout.addWidget(box)
        note = QtWidgets.QLabel(self.t(
            "vault_snapshot_note",
            "Every section is included by default. Unchecking any of them "
            "requires the master password again."))
        note.setWordWrap(True)
        note.setStyleSheet("color: #9aa0a6;")
        layout.addWidget(note)
        buttons = QtWidgets.QHBoxLayout()
        ok = QtWidgets.QPushButton(self.t("vault_snapshot_take", "Take snapshot"))
        ok.clicked.connect(dialog.accept)
        cancel = QtWidgets.QPushButton(self.t("vault_close", "Close"))
        cancel.clicked.connect(dialog.reject)
        buttons.addStretch()
        buttons.addWidget(cancel)
        buttons.addWidget(ok)
        layout.addLayout(buttons)
        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            return None
        selected = {key for key, box in boxes.items() if box.isChecked()}
        if not selected:
            self._status(self.t("vault_snapshot_empty",
                                "Select at least one section."))
            return None
        if selected == set(VAULT_SECTION_KEYS):
            return selected, ""
        dialog = VaultPasswordDialog(self, self.t("vault_snapshot", "Snapshot"),
                                     mode="unlock")
        dialog.heading.setText(self.t(
            "vault_snapshot_confirm",
            "A partial snapshot: confirm the master password"))
        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            return None
        if not self.core.verify(dialog.password())["ok"]:
            self._status(self.t("vault_bad_password", "Wrong password."))
            return None
        return selected, dialog.password()

    def restore_snapshot(self, path):
        """Merge a snapshot back into the vault."""
        if not self._guard_write("restore a snapshot"):
            return None
        added = self.store.restore(path)
        if added < 0:
            self._status(self.t("vault_restore_refused",
                                "That snapshot cannot be restored."))
            return None
        self.core.record("vault_snapshot_restored", path)
        self._announce("vault snapshot restored: %s" % path)
        self._status(self.t("vault_restored",
                            "Restored {count} resource(s).").format(count=added))
        self.refresh()
        return added

    # ---------------------------------------------------------------- dialogs
    def _show_text(self, title, text):
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle(title)
        dialog.resize(780, 540)
        layout = QtWidgets.QVBoxLayout(dialog)
        view = QtWidgets.QPlainTextEdit(text)
        view.setReadOnly(True)
        view.setFont(QFont(VAULT_FONT, VAULT_FONT_SIZE))
        layout.addWidget(view, 1)
        buttons = QtWidgets.QHBoxLayout()
        copy = QtWidgets.QPushButton(self.t("vault_copy", "Copy"))
        copy.clicked.connect(
            lambda: QtWidgets.QApplication.clipboard().setText(text))
        close = QtWidgets.QPushButton(self.t("vault_close", "Close"))
        close.clicked.connect(dialog.close)
        buttons.addStretch()
        buttons.addWidget(copy)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        dialog.exec_()
        return dialog

    def open_vault_folder(self):
        """Show the vault directory in the desktop's file manager."""
        if not self.require_open("open the vault folder"):
            return None
        path = self.store.directory
        if not os.path.isdir(path):
            return None
        try:
            QtGui.QDesktopServices.openUrl(
                QtCore.QUrl.fromLocalFile(path))
        except Exception:
            pass
        self._status(self.t("vault_folder_opened", "Opened {path}.").format(
            path=path))
        return path

    # ------------------------------------------------------------ translation
    def retranslate(self, table):
        """Apply the main window's language table to this tab."""
        def label(key, fallback):
            return table.get(key, fallback)

        self.title_label.setText(label("vault", "VAULT"))
        self.btn_create.setText(label("vault_create", "Create vault..."))
        self.btn_unlock.setText(label("vault_unlock", "Unlock..."))
        self.btn_lock.setText(label("vault_lock", "Lock"))
        self.btn_change_password.setText(
            label("vault_change_password", "Change password..."))
        self.btn_policy.setText(label("vault_policy", "Security policy..."))
        self.btn_open_dir.setText(label("vault_open_dir", "Open vault folder"))
        self.btn_add.setText(label("vault_add", "Add"))
        self.btn_edit.setText(label("vault_edit", "Edit"))
        self.btn_duplicate.setText(label("vault_duplicate", "Copy"))
        self.btn_move.setText(label("vault_move", "Move..."))
        self.btn_delete.setText(label("vault_delete", "Delete"))
        self.btn_flags.setText(label("vault_flags_menu", "Flags..."))
        self.btn_hidden_mode.setText(label("vault_hidden_mode", "Hidden mode"))
        self.btn_readonly.setText(label("vault_readonly_lock", "Read-only lock"))
        self.btn_import.setText(label("vault_import", "Import..."))
        self.btn_export.setText(label("vault_export", "Export..."))
        self.btn_reports.setText(label("vault_reports_menu", "Reports..."))
        self.btn_integrate.setText(label("vault_integrate_menu", "Send to..."))
        self.btn_search.setText(label("vault_search", "Search"))
        self.btn_clear_search.setText(label("vault_search_clear", "Clear"))
        self.btn_snapshot_take = getattr(self, "btn_snapshot_take", None)
        for key, action in self.flag_actions.items():
            action.setText(label("vault_flag_" + key,
                                 VAULT_FLAG_LABELS.get(key, key)))
        for key, action in self.report_actions.items():
            action.setText(label("vault_report_" + key,
                                 VAULT_REPORT_LABELS.get(key, key)))
        for key, action in self.integrate_actions.items():
            action.setText(label("vault_int_" + key, key))
        self.act_export_vault.setText(label(
            "vault_export_vault", "Export the vault (re-authenticate)"))
        self.act_export_section.setText(label(
            "vault_export_section", "Export the open section (inside the vault)"))
        self.act_export_snapshots.setText(label(
            "vault_export_snapshots", "Export the snapshots"))
        self.act_export_config.setText(label(
            "vault_export_config", "Export the vault configuration"))
        self.search_edit.setPlaceholderText(
            label("vault_search_hint", "Search the vault..."))
        self.search_field_combo.setItemText(
            0, label("vault_search_all", "Every aspect"))
        for index, (key, label_key, fallback) in enumerate(
                VAULT_SEARCH_FIELDS, start=1):
            self.search_field_combo.setItemText(index,
                                                table.get(label_key, fallback))
        for key, label_key, fallback, _detail in VAULT_SECTIONS:
            self._section_labels[key] = table.get(label_key, fallback)
        self.after_state_change()
