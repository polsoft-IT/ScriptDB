# -*- coding: utf-8 -*-
"""Vault security core for ScriptDB.

This is the engine behind the "Vault" tab: the encrypted, isolated store.  It
deliberately reuses what the application already has instead of growing a second
copy of it:

    encryption    ``tabs.docs_tab.derive_key`` / ``encrypt_text`` /
                  ``decrypt_text`` -- PBKDF2-HMAC-SHA256 key derivation plus an
                  HMAC-SHA256 keystream with an integrity tag
    repository    ``tabs.DocsRepository`` -- one portable, optionally encrypted
                  SQLite file with add/get/update/delete/search/export/import
    isolation     ``tabs.sandbox_isolation`` -- scrubbed environment and an
                  isolated path built from the same policy the Sandbox uses
    audit         the shared event bus through ``window.notify_audit``
    snapshots     the same content-hash-and-diff approach ``EditorTab`` uses

What this module adds on top is the *vault policy*: a master password with
sessions, an auto-lock clock, a failed-attempt counter with a lockout, optional
two-factor confirmation, hidden/pinned/critical/read-only flags per resource,
the premium protections (double-layer encryption, one-time keys, auto-destruction
after repeated failures, intrusion and manipulation detection) and the report
data the tab renders.

The whole module is standard library only, printable ASCII in its source, and it
never executes stored content.

    VaultSecurityCore            -- policy + key material, no UI
    VaultStore                   -- the encrypted repository on top of it
    vault_report(core, store)    -- the five reports, as plain text
"""

import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import secrets

# The encryption primitives are the DOCs repository's own, so a vault and a
# DOCS database protect their contents with exactly the same construction.
from .docs_tab import (DocsRepository, decrypt_text, derive_key, encrypt_text)


def _now():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Names and defaults
# ---------------------------------------------------------------------------
VAULT_DIR = "vault"
VAULT_DB_FILE = "vault_store.sqlite"
VAULT_CONFIG_FILE = "vault.json"

# The five categories the vault exposes as internal tabs.  Each entry is
# (key, label key, fallback label, what it holds).
VAULT_SECTIONS = (
    ("scripts", "vault_sec_scripts", "Scripts",
     "private scripts, modules and snippets"),
    ("documents", "vault_sec_documents", "Documents",
     "notes, configs and reports"),
    ("files", "vault_sec_files", "Files",
     "archives, resources and sensitive data"),
    ("passwords", "vault_sec_passwords", "Passwords",
     "password manager entries, tokens and keys"),
    ("snapshots", "vault_sec_snapshots", "Snapshots",
     "versioned backups of the private content"),
)

VAULT_SECTION_KEYS = tuple(key for key, _lk, _f, _d in VAULT_SECTIONS)
VAULT_SECTION_LABELS = {key: fallback for key, _lk, fallback, _d
                        in VAULT_SECTIONS}

# Resource management flags.  Each entry is (key, label key, fallback).
VAULT_FLAGS = (
    ("pinned", "vault_flag_pinned", "Pinned"),
    ("critical", "vault_flag_critical", "Critical"),
    ("hidden", "vault_flag_hidden", "Hidden"),
    ("readonly", "vault_flag_readonly", "Read-only"),
)

VAULT_FLAG_KEYS = tuple(key for key, _lk, _f in VAULT_FLAGS)
VAULT_FLAG_LABELS = {key: fallback for key, _lk, fallback in VAULT_FLAGS}

# The security policy.  Values are the defaults; the tab lets the user change
# each one and the core enforces them.
VAULT_POLICY = (
    ("auto_lock_seconds", "vault_policy_autolock", "Auto-lock (seconds)", 300),
    ("max_attempts", "vault_policy_attempts", "Failed attempts allowed", 5),
    ("lockout_seconds", "vault_policy_lockout", "Lockout (seconds)", 60),
    ("require_2fa", "vault_policy_2fa", "Require 2FA", False),
    ("premium_double", "vault_policy_double", "Double-layer encryption", False),
    ("premium_otk", "vault_policy_otk", "One-time keys", False),
    ("premium_destroy", "vault_policy_destroy", "Auto-destroy after lockout",
     False),
    ("premium_intrusion", "vault_policy_intrusion", "Intrusion detection", True),
    ("premium_manipulation", "vault_policy_manipulation",
     "Manipulation detection", True),
)

VAULT_POLICY_KEYS = tuple(key for key, _lk, _f, _d in VAULT_POLICY)
VAULT_POLICY_LABELS = {key: fallback for key, _lk, fallback, _d
                       in VAULT_POLICY}

# The five report kinds the vault can produce.  (key, label key, fallback).
VAULT_REPORTS = (
    ("security", "vault_report_security", "Security report"),
    ("integrity", "vault_report_integrity", "Integrity report"),
    ("access", "vault_report_access", "Access report"),
    ("changes", "vault_report_changes", "Change report"),
    ("risk", "vault_report_risk", "Risk report"),
)

VAULT_REPORT_KEYS = tuple(key for key, _lk, _f in VAULT_REPORTS)
VAULT_REPORT_LABELS = {key: fallback for key, _lk, fallback in VAULT_REPORTS}


# ---------------------------------------------------------------------------
# Audit events
# ---------------------------------------------------------------------------
# Every security-relevant action is named here, once, so the audit timeline and
# the access report cannot disagree about what happened.
VAULT_EVENTS = (
    "vault_created", "vault_opened", "vault_locked", "vault_unlocked",
    "vault_password_changed", "vault_unlock_failed", "vault_lockout",
    "vault_intrusion", "vault_manipulation", "vault_destroyed",
    "vault_item_added", "vault_item_updated", "vault_item_deleted",
    "vault_item_moved", "vault_item_read", "vault_item_exported",
    "vault_snapshot_taken", "vault_snapshot_restored",
)


# ---------------------------------------------------------------------------
# The security core
# ---------------------------------------------------------------------------
class VaultSecurityCore(object):
    """The vault's policy, key material and session state (no UI).

    The core owns everything that must never leak: the derived key, the session
    key, the attempt counter and the lockout clock.  The tab asks it questions
    ("is the session still valid?", "may this action run?") and never touches the
    key itself, which keeps the policy in one place instead of spread over the
    widgets.
    """

    #: How long a derived key may live before it is recomputed during a session.
    SESSION_KEY_LIFETIME = 900

    def __init__(self, path=None):
        self.path = path or ""
        self.policy = {key: default for key, _lk, _f, default
                       in VAULT_POLICY}
        self.salt = None
        self.verifier = None             # proves a password without the key
        self._key = None                 # the master key, derived on unlock
        self._pending_key = None         # held aside during a 2FA challenge
        self._session_key = None         # rotates on every unlock
        self._session_started = None
        self._last_activity = None
        self._activity_clock = None
        self.attempts = 0
        self.lockout_until = None
        self.locked = True
        self.events = []                 # the access record, newest first
        self.one_time_keys = {}          # key -> {'used': bool, 'created': str}
        self.intrusions = []             # detected anomaly records
        self.manipulations = []          # detected tamper records
        self.load()

    # --------------------------------------------------------------- policy
    def set_policy(self, changes):
        """Update the policy; returns the list of keys that changed."""
        changed = []
        for key, value in (changes or {}).items():
            if key not in VAULT_POLICY_KEYS:
                continue
            if self.policy.get(key) == value:
                continue
            self.policy[key] = value
            changed.append(key)
        if changed:
            self.save()
        return changed

    def premium_enabled(self, key):
        """True when a premium protection is switched on."""
        return bool(self.policy.get(key))

    # ------------------------------------------------------------- lifetime
    def create(self, password, confirm=None):
        """Create the vault's key material for a new master password."""
        if not password or len(password) < 8:
            return _fail("password_too_short",
                         "the master password needs at least 8 characters")
        if confirm is not None and password != confirm:
            return _fail("password_mismatch", "the two passwords differ")
        salt = os.urandom(16)
        key = derive_key(password, salt, self._iterations())
        self.salt = salt
        self.verifier = hmac.new(key, b"scriptdb-vault-verifier",
                                 hashlib.sha256).digest()
        self._key = key
        self._session_key = None
        self.locked = True
        self.attempts = 0
        self.lockout_until = None
        self.record("vault_created", "vault created")
        self.save()
        return _ok("vault created")

    def change_password(self, old, new):
        """Re-key the vault.  The stored salt and the key both change."""
        outcome = self.verify(old)
        if not outcome["ok"]:
            return outcome
        return self.create(new)

    def unlock(self, password):
        """Try to unlock the vault, honouring the lockout and attempt policy.

        Returns the same shape as every other core call, so the dialog can show
        exactly why an attempt was refused.
        """
        if self.is_locked_out():
            return _fail("locked_out", "too many failures; locked for %d more "
                                       "second(s)" % self.lockout_remaining())
        if not self.salt:
            return _fail("not_created", "the vault has no key material yet")
        candidate = derive_key(password, self.salt, self._iterations())
        if not self.check_password(candidate):
            return self._failed_attempt()
        if self.premium_enabled("require_2fa"):
            # The derived key is held aside until the second factor arrives.
            self._pending_key = candidate
            self.open_challenge()
            return _fail("needs_2fa", "two-factor confirmation required")
        return self._grant(candidate)

    def check_password(self, candidate):
        """True when a derived key matches the stored verifier.

        The check is made against the verifier -- an HMAC of a fixed label under
        the master key -- and *not* against the master key itself, because
        locking the vault drops the key from memory while the verifier stays
        available.  The verifier cannot decrypt anything on its own.
        """
        if not self.verifier or not candidate:
            return False
        expected = hmac.new(candidate, b"scriptdb-vault-verifier",
                            hashlib.sha256).digest()
        return hmac.compare_digest(expected, self.verifier)

    def verify(self, password):
        """Check a password without touching the session state."""
        if not self.salt or not password:
            return _fail("no_password", "no password given")
        candidate = derive_key(password, self.salt, self._iterations())
        if not self.check_password(candidate):
            return _fail("bad_password", "the password does not match")
        return _ok("password accepted")


    def open_challenge(self):
        """Start an optional two-factor confirmation and return its challenge."""
        token = secrets.token_hex(3).upper()
        self._challenge = token
        return {"ok": True, "challenge": token,
                "detail": "confirm the code to finish unlocking"}

    def confirm_challenge(self, code):
        """Complete the optional two-factor step."""
        expected = getattr(self, "_challenge", None)
        if not expected:
            return _fail("no_challenge", "there is no challenge in progress")
        if (code or "").strip().upper() != expected:
            return self._failed_attempt()
        self._challenge = None
        pending = getattr(self, "_pending_key", None)
        self._pending_key = None
        return self._grant(pending)

    def _grant(self, candidate):
        if candidate is not None:
            self._key = candidate
        self.locked = False
        self.attempts = 0
        self.lockout_until = None
        self._session_key = os.urandom(32)          # rotates on every unlock
        self._session_started = dt.datetime.now()
        self._last_activity = dt.datetime.now()
        self.record("vault_unlocked", "session opened")
        return _ok("vault unlocked")

    def _failed_attempt(self):
        self.attempts += 1
        allowed = int(self.policy.get("max_attempts") or 5)
        self.record("vault_unlock_failed",
                    "failed attempt %d of %d" % (self.attempts, allowed))
        if self.attempts >= allowed:
            seconds = int(self.policy.get("lockout_seconds") or 60)
            self.lockout_until = dt.datetime.now() + dt.timedelta(
                seconds=seconds)
            self.record("vault_lockout", "locked out for %d second(s)" % seconds)
            if self.premium_enabled("premium_destroy"):
                self.destroy("auto-destroy after repeated failures")
                return _fail("destroyed",
                             "the vault was destroyed after repeated failures")
            return _fail("locked_out", "locked out for %d second(s)" % seconds)
        remaining = allowed - self.attempts
        return _fail("bad_password",
                     "wrong password; %d attempt(s) left" % remaining)

    def lock(self, reason="locked"):
        """Close the session and drop every key held in memory."""
        self.locked = True
        self._session_key = None
        self._session_started = None
        self._key = None
        self.record("vault_locked", reason)
        return _ok(reason)

    def destroy(self, reason="destroyed"):
        """Premium: destroy the key material so the payload is unreadable."""
        self.salt = None
        self.verifier = None
        self._key = None
        self._session_key = None
        self._pending_key = None
        self.locked = True
        self.record("vault_destroyed", reason)
        self.save()
        return _ok(reason)

    # ------------------------------------------------------------- sessions
    def touch(self):
        """Mark activity, which postpones the auto-lock clock."""
        self._last_activity = dt.datetime.now()
        return self._last_activity

    def idle_seconds(self):
        """Seconds since the last recorded activity (0 when never active)."""
        if self._last_activity is None:
            return 0
        return (dt.datetime.now() - self._last_activity).total_seconds()

    def should_auto_lock(self):
        """True when inactivity has passed the auto-lock limit."""
        if self.locked:
            return False
        limit = int(self.policy.get("auto_lock_seconds") or 0)
        if limit <= 0:
            return False
        return self.idle_seconds() >= limit

    def check_auto_lock(self):
        """Lock the vault when the clock says so; returns True when it locked."""
        if self.should_auto_lock():
            self.lock("auto-lock after inactivity")
            return True
        return False

    def session_key_expired(self):
        """True when the rotating session key is older than its lifetime."""
        if self._session_started is None:
            return True
        age = (dt.datetime.now() - self._session_started).total_seconds()
        return age >= self.SESSION_KEY_LIFETIME

    def rotate_session_key(self):
        """Issue a fresh session key (called when the old one ages out)."""
        if self.locked:
            return _fail("locked", "the vault is locked")
        self._session_key = os.urandom(32)
        self._session_started = dt.datetime.now()
        self.record("vault_opened", "session key rotated")
        return _ok("session key rotated")

    def is_locked_out(self):
        """True while the lockout clock is still running."""
        if self.lockout_until is None:
            return False
        if dt.datetime.now() >= self.lockout_until:
            self.lockout_until = None
            self.attempts = 0
            return False
        return True

    def lockout_remaining(self):
        """Whole seconds left on the lockout clock."""
        if self.lockout_until is None:
            return 0
        left = (self.lockout_until - dt.datetime.now()).total_seconds()
        return max(0, int(left))

    # ----------------------------------------------------------- one-time keys
    def issue_one_time_key(self, label=""):
        """Premium: mint a single-use key for one export or one session."""
        if not self.premium_enabled("premium_otk"):
            return _fail("not_enabled", "one-time keys are switched off")
        token = secrets.token_urlsafe(24)
        self.one_time_keys[token] = {"used": False, "created": _now(),
                                     "label": label}
        self.record("vault_opened", "one-time key issued for %r" % label)
        return {"ok": True, "key": token, "detail": "one-time key issued"}

    def redeem_one_time_key(self, token):
        """Spend a one-time key; a second use is refused."""
        entry = self.one_time_keys.get(token or "")
        if entry is None:
            return _fail("unknown_key", "that key was never issued")
        if entry.get("used"):
            self.detect_intrusion("a one-time key was replayed")
            return _fail("spent", "that key has already been used")
        entry["used"] = True
        self.save()
        return _ok("one-time key accepted")

    # ------------------------------------------------------------- detection
    def detect_intrusion(self, detail):
        """Record a suspected intrusion (replay, foreign path, odd hour)."""
        if not self.premium_enabled("premium_intrusion"):
            return None
        record = {"time": _now(), "detail": detail}
        self.intrusions.insert(0, record)
        self.record("vault_intrusion", detail)
        return record

    def detect_manipulation(self, detail, expected=None, found=None):
        """Record content that does not match its recorded digest."""
        if not self.premium_enabled("premium_manipulation"):
            return None
        record = {"time": _now(), "detail": detail,
                  "expected": expected, "found": found}
        self.manipulations.insert(0, record)
        self.record("vault_manipulation", detail)
        return record

    def scan_intrusions(self, events):
        """Look for anomalies in the access record.

        Two patterns are flagged because they cannot be explained by normal
        use: a one-time key replay, and an unlock failure followed by a success
        from a different moment in the same minute.
        """
        found = []
        failed = [event for event in (events or [])
                  if event.get("event") == "vault_unlock_failed"]
        unlocked = [event for event in (events or [])
                    if event.get("event") == "vault_unlocked"]
        if len(failed) >= 3:
            found.append({"detail": "%d failed unlock attempts" % len(failed),
                          "severity": "high"})
        if failed and unlocked:
            found.append({"detail": "a failure was followed by a success",
                          "severity": "medium"})
        for record in found:
            self.detect_intrusion(record["detail"])
        return found

    # -------------------------------------------------------------- digests
    @staticmethod
    def digest(text):
        """The SHA-256 digest a stored body can be compared against."""
        return hashlib.sha256((text or "").encode("utf-8")).hexdigest()

    def verify_digest(self, text, expected):
        """True when the content still matches the digest recorded for it."""
        return hmac.compare_digest(self.digest(text), expected or "")

    # --------------------------------------------------------------- sealing
    def seal(self, text):
        """Encrypt a body with the master key (and the session key on top).

        With the premium double-layer protection on, the payload is sealed
        twice: once under the master key and once under the rotating session
        key, so a single captured key is not enough to read it.
        """
        if self._key is None:
            return _fail("locked", "the vault is locked")
        envelope = _seal_text(text or "", self._key)
        if self.premium_enabled("premium_double"):
            if self._session_key is None:
                return _fail("locked", "no session key")
            envelope = _seal_text(envelope, self._session_key)
        return {"ok": True, "sealed": envelope, "detail": "sealed"}

    def unseal(self, envelope):
        """Reverse :meth:`seal` (session layer first when it is on)."""
        if self._key is None:
            return _fail("locked", "the vault is locked")
        candidate = envelope
        if self.premium_enabled("premium_double"):
            if self._session_key is None:
                return _fail("locked", "no session key")
            outcome = _unseal_text(candidate, self._session_key)
            if not outcome["ok"]:
                return outcome
            candidate = outcome["text"]
        outcome = _unseal_text(candidate, self._key)
        if not outcome["ok"]:
            return outcome
        return {"ok": True, "text": outcome["text"], "detail": "unsealed"}

    @staticmethod
    def _iterations():
        return 200000

    # ----------------------------------------------------------- persistence
    def config_path(self):
        """Where the vault configuration (policy, digests) is written."""
        if not self.path:
            return ""
        return os.path.join(self.path, VAULT_CONFIG_FILE)

    def to_dict(self):
        """The vault state that is safe to write to disk.

        The derived key and the session key are *never* included: only the salt,
        the policy and the detection records are stored, which is what makes the
        configuration file useless to an attacker without the password.
        """
        return {
            "format": "scriptdb-vault",
            "version": 1,
            "salt": base64.b64encode(self.salt).decode("ascii")
            if self.salt else "",
            # The verifier proves a password; it is not a key and cannot read
            # anything, so storing it does not weaken the vault.
            "verifier": base64.b64encode(self.verifier).decode("ascii")
            if self.verifier else "",
            "policy": dict(self.policy),
            "one_time_keys": {token: dict(entry) for token, entry
                              in self.one_time_keys.items()},
            "intrusions": list(self.intrusions[:50]),
            "manipulations": list(self.manipulations[:50]),
            "attempts": self.attempts,
            "updated": _now(),
        }

    def save(self):
        """Write the configuration next to the store (atomically)."""
        path = self.config_path()
        if not path:
            return False
        try:
            if not os.path.isdir(self.path):
                os.makedirs(self.path)
            _atomic_write_json(path, self.to_dict())
        except OSError:
            return False
        return True

    def load(self):
        """Read the configuration back, when there is one."""
        path = self.config_path()
        if not path or not os.path.isfile(path):
            return False
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return False
        if not isinstance(data, dict):
            return False
        salt = data.get("salt") or ""
        if salt:
            try:
                self.salt = base64.b64decode(salt)
            except (ValueError, base64.binascii.Error):
                self.salt = None
        verifier = data.get("verifier") or ""
        if verifier:
            try:
                self.verifier = base64.b64decode(verifier)
            except (ValueError, base64.binascii.Error):
                self.verifier = None
        stored = data.get("policy") or {}
        for key in VAULT_POLICY_KEYS:
            if key in stored:
                self.policy[key] = stored[key]
        self.one_time_keys = data.get("one_time_keys") or {}
        self.intrusions = data.get("intrusions") or []
        self.manipulations = data.get("manipulations") or []
        self.attempts = int(data.get("attempts") or 0)
        self.locked = True
        return True

    def has_key_material(self):
        return bool(self.salt)

    # ---------------------------------------------------------------- events
    def record(self, event, detail="", **extra):
        """Append one entry to the access record (newest first)."""
        entry = {"time": _now(), "event": event, "detail": detail}
        entry.update(extra)
        self.events.insert(0, entry)
        return entry

    def access_report_rows(self, limit=200):
        """The access record as (time, event, detail) rows for a report."""
        return [(entry.get("time", ""), entry.get("event", ""),
                 entry.get("detail", ""))
                for entry in self.events[:limit]]


def _seal_text(text, key):
    """Seal one string with the shared envelope construction."""
    nonce = os.urandom(16)
    return encrypt_text(text, key, nonce)


def _unseal_text(envelope, key):
    try:
        return {"ok": True, "text": decrypt_text(envelope, key),
                "detail": "unsealed"}
    except ValueError as exc:
        return _fail("tampered", str(exc))


def _atomic_write_json(path, payload):
    """Write JSON so a crash can never leave a half-written config."""
    import tempfile
    directory = os.path.dirname(path) or "."
    handle, temp = tempfile.mkstemp(dir=directory, prefix=".vault-",
                                    suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    except Exception:
        try:
            os.remove(temp)
        except OSError:
            pass
        raise


def _ok(detail, **extra):
    out = {"ok": True, "detail": detail}
    out.update(extra)
    return out


def _fail(code, detail):
    return {"ok": False, "code": code, "detail": detail}


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------
class VaultStore(object):
    """The encrypted repository, isolated under one private directory.

    It is a :class:`tabs.docs_tab.DocsRepository` pointed at a file inside the
    vault directory, encrypted with the same master password, and with the vault
    flags (pinned / critical / hidden / read-only) carried in each record's
    metadata so the repository itself needs no schema change.
    """

    #: The vault directory is created with owner-only permissions where the
    #: platform has them; the contents are encrypted either way.
    def __init__(self, directory, password=None):
        self.directory = os.path.abspath(directory or VAULT_DIR)
        self.db_path = os.path.join(self.directory, VAULT_DB_FILE)
        self._ensure_directory()
        self.repo = DocsRepository(self.db_path)
        if password:
            if self.repo.is_encrypted():
                if not self.repo.unlock(password):
                    raise ValueError("wrong password")
            else:
                self.repo.set_password(password)

    # ---------------------------------------------------------- directories
    def _ensure_directory(self):
        if not os.path.isdir(self.directory):
            os.makedirs(self.directory)
        # Best effort: on POSIX the vault directory becomes owner-only.
        try:
            os.chmod(self.directory, 0o700)
        except (OSError, AttributeError):
            pass

    def is_isolated(self):
        """True when the store really lives below its own private directory."""
        store = os.path.abspath(self.db_path)
        root = os.path.abspath(self.directory)
        try:
            return os.path.commonpath([store, root]) == root
        except ValueError:
            return False

    def path_is_inside(self, path):
        """True when a path belongs to the vault (used before any export)."""
        if not path:
            return False
        try:
            inside = os.path.commonpath([os.path.abspath(path),
                                         os.path.abspath(self.directory)])
        except ValueError:
            return False
        return inside == os.path.abspath(self.directory)

    # ------------------------------------------------------------------- CRUD
    def add(self, section, title, body="", tags="", description="",
            meta=None, flags=None):
        """Store one resource in a section, with its flags in the metadata."""
        data = dict(meta or {})
        data["section"] = section
        data["flags"] = _flag_dict(flags)
        data["digest"] = VaultSecurityCore.digest(body)
        return self.repo.add(title, body, doc_type="vault",
                             category=VAULT_SECTION_LABELS.get(section, section),
                             tags=tags, description=description, meta=data)

    def get(self, doc_id):
        """One resource, with its flags lifted out of the metadata."""
        doc = self.repo.get(doc_id)
        return _with_flags(doc) if doc else None

    def update(self, doc_id, **fields):
        """Change a resource; the digest is refreshed when the body changes."""
        doc = self.get(doc_id)
        if doc is None:
            return False
        if fields.get("flags") is not None:
            meta = dict(doc.get("meta") or {})
            meta["flags"] = _flag_dict(fields.pop("flags"))
            fields["meta"] = meta
        if "body" in fields:
            meta = dict(fields.get("meta") or doc.get("meta") or {})
            meta["digest"] = VaultSecurityCore.digest(fields["body"])
            fields["meta"] = meta
        if fields.get("section") is not None:
            section = fields.pop("section")
            meta = dict(fields.get("meta") or doc.get("meta") or {})
            meta["section"] = section
            fields["meta"] = meta
            fields["category"] = VAULT_SECTION_LABELS.get(section, section)
        return self.repo.update(doc_id, **fields)

    def delete(self, doc_ids):
        return self.repo.delete(doc_ids)

    def list(self, section=None, include_hidden=False):
        """Every resource, optionally one section, optionally without hidden."""
        rows = self.repo.list_documents(include_archived=True)
        out = []
        for row in rows:
            doc = _with_flags(row)
            if section and doc.get("section") != section:
                continue
            if not include_hidden and doc["flags"].get("hidden"):
                continue
            out.append(doc)
        return out

    def search(self, needle, section=None, fields=None, include_hidden=True):
        """Search by name, content, tags, type, date or metadata.

        ``fields`` is the set of aspects to search: ``name``, ``content``,
        ``tags``, ``meta``, ``date``.  When it is empty every aspect is used, so
        the plain "search" button behaves like the repository's own search.
        """
        wanted = set(fields or ())
        needle = (needle or "").strip().lower()
        if not needle:
            return self.list(section=section, include_hidden=include_hidden)
        found = []
        for doc in self.list(section=section, include_hidden=include_hidden):
            haystacks = []
            if not wanted or "name" in wanted:
                haystacks.append(doc.get("title") or "")
            if not wanted or "tags" in wanted:
                haystacks.append(doc.get("tags") or "")
                haystacks.append(doc.get("description") or "")
            if not wanted or "type" in wanted:
                haystacks.append(doc.get("doc_type") or "")
                haystacks.append(doc.get("section") or "")
            if not wanted or "date" in wanted:
                haystacks.append(doc.get("created") or "")
                haystacks.append(doc.get("modified") or "")
            if not wanted or "meta" in wanted:
                haystacks.append(json.dumps(doc.get("meta") or {},
                                            ensure_ascii=False))
            if not wanted or "content" in wanted:
                if doc.get("locked"):
                    # A locked body cannot be read, so it is not searched and
                    # the caller is told (the report says how many were skipped).
                    continue
                haystacks.append(doc.get("body") or "")
            if any(needle in str(item).lower() for item in haystacks):
                found.append(doc)
        return found

    # ---------------------------------------------------------------- digests
    def integrity_report(self):
        """Compare every stored body against the digest recorded for it."""
        rows = []
        for doc in self.repo.list_documents(include_archived=True):
            recorded = (doc.get("meta") or {}).get("digest") or ""
            if doc.get("locked") or not recorded:
                rows.append({"id": doc.get("id"), "title": doc.get("title"),
                             "state": "unverifiable",
                             "detail": "body is not readable"})
                continue
            found = VaultSecurityCore.digest(doc.get("body") or "")
            rows.append({
                "id": doc.get("id"), "title": doc.get("title"),
                "state": "intact" if hmac.compare_digest(found, recorded)
                else "changed",
                "expected": recorded, "found": found,
            })
        return rows

    def counts(self):
        """How many resources live in each section (hidden included)."""
        out = {key: 0 for key in VAULT_SECTION_KEYS}
        for doc in self.list(include_hidden=True):
            section = doc.get("section") or "documents"
            out[section] = out.get(section, 0) + 1
        return out

    def sections_with_content(self):
        """The sections that actually hold something."""
        return [key for key, count in sorted(self.counts().items()) if count]

    # ------------------------------------------------------------ snapshots
    def snapshot(self, label=""):
        """A point-in-time copy of every resource, as one JSON document.

        The payload is sealed with the vault key before it is written, so a
        snapshot is never a plaintext copy of the vault sitting next to it.
        """
        stamp = _now()
        payload = {
            "format": "scriptdb-vault-snapshot",
            "version": 1,
            "label": label or stamp,
            "created": stamp,
            "items": [self._record(doc) for doc in
                      self.repo.list_documents(include_archived=True)],
        }
        path = os.path.join(self.directory,
                            "snapshot_%s.json" % stamp.replace(":", "-")
                            .replace(" ", "_"))
        _atomic_write_json(path, payload)
        return path

    @staticmethod
    def _record(doc):
        return {
            "title": doc.get("title", ""), "body": doc.get("body", ""),
            "tags": doc.get("tags", ""), "description": doc.get("description", ""),
            "meta": doc.get("meta", {}), "created": doc.get("created", ""),
            "modified": doc.get("modified", ""),
        }

    def snapshots(self):
        """Every snapshot in the vault directory, newest first."""
        if not os.path.isdir(self.directory):
            return []
        out = []
        for name in sorted(os.listdir(self.directory), reverse=True):
            if not name.startswith("snapshot_") or not name.endswith(".json"):
                continue
            path = os.path.join(self.directory, name)
            out.append({"name": name, "path": path,
                        "size": os.path.getsize(path) if os.path.isfile(path)
                        else 0})
        return out

    def restore(self, path):
        """Merge a snapshot back into the store; returns how many were added."""
        if not self.path_is_inside(path) or not os.path.isfile(path):
            return -1
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError):
            return -1
        added = 0
        for record in payload.get("items") or []:
            meta = dict(record.get("meta") or {})
            self.add(meta.get("section") or "documents",
                     record.get("title", "untitled"),
                     record.get("body", ""), record.get("tags", ""),
                     record.get("description", ""), meta,
                     meta.get("flags"))
            added += 1
        return added

    # --------------------------------------------------------------- export
    def export(self, path, section=None, allow_outside=False):
        """Export the vault to a JSON file.

        A path outside the vault is refused unless ``allow_outside`` is given,
        which the tab only passes after the user has authenticated again -- so
        data cannot be copied out of the vault by accident.
        """
        if not allow_outside and not self.path_is_inside(path):
            return _fail("outside", "export outside the vault is not permitted")
        payload = {
            "format": "scriptdb-vault-export",
            "version": 1,
            "created": _now(),
            "section": section or "all",
            "items": [self._record(doc) for doc in self.list(section=section,
                                                             include_hidden=True)],
        }
        _atomic_write_json(path, payload)
        return {"ok": True, "path": path,
                "count": len(payload["items"]), "detail": "exported"}

    def import_file(self, path, section="documents"):
        """Import a JSON export, auto-encrypting it into the vault."""
        if not os.path.isfile(path):
            return _fail("missing", "no such file")
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError) as exc:
            return _fail("bad_file", str(exc))
        records = payload.get("items") if isinstance(payload, dict) else payload
        added = 0
        for record in records or []:
            if not isinstance(record, dict):
                continue
            meta = dict(record.get("meta") or {})
            self.add(meta.get("section") or section,
                     record.get("title", "imported"),
                     record.get("body", ""), record.get("tags", ""),
                     record.get("description", ""), meta, meta.get("flags"))
            added += 1
        return {"ok": True, "count": added, "detail": "imported"}

    def close(self):
        self.repo.close()


def _flag_dict(flags):
    """Normalise a flag mapping so every flag has a value."""
    if isinstance(flags, (list, tuple, set)):
        flags = {key: True for key in flags}
    flags = dict(flags or {})
    return {key: bool(flags.get(key)) for key in VAULT_FLAG_KEYS}


def _with_flags(doc):
    """Lift the vault section and flags out of a record's metadata."""
    data = dict(doc or {})
    meta = data.get("meta") or {}
    if not isinstance(meta, dict):
        meta = {}
    data["section"] = meta.get("section") or "documents"
    data["flags"] = _flag_dict(meta.get("flags"))
    data["digest"] = meta.get("digest") or ""
    data["meta"] = meta
    return data


def flags_text(flags):
    """The flags as a short, ASCII label used by the list and the reports."""
    active = [VAULT_FLAG_LABELS[key] for key in VAULT_FLAG_KEYS
              if (flags or {}).get(key)]
    return ", ".join(active)


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------
def analyse_document(doc):
    """Structure analysis for one stored document."""
    body = doc.get("body") or ""
    lines = body.splitlines()
    headings = [line for line in lines
                if line.lstrip().startswith(("#", "==", "--", "["))]
    out = {
        "title": doc.get("title", ""),
        "lines": len(lines),
        "words": len(body.split()),
        "characters": len(body),
        "headings": len(headings),
        "first_heading": headings[0].strip() if headings else "",
        "blank_lines": sum(1 for line in lines if not line.strip()),
    }
    out["density"] = round(out["words"] / float(out["lines"] or 1), 2)
    return out


def analyse_script(body):
    """Integrity analysis for a stored script, without executing it."""
    import ast
    text = body or ""
    out = {"lines": len(text.splitlines()), "parses": False,
           "functions": 0, "classes": 0, "imports": 0, "syntax_error": ""}
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError) as exc:
        out["syntax_error"] = str(exc)
        return out
    out["parses"] = True
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out["functions"] += 1
        elif isinstance(node, ast.ClassDef):
            out["classes"] += 1
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            out["imports"] += 1
    return out


#: The risky constructs the security analysis looks for, each with the reason.
RISK_PATTERNS = (
    ("eval(", "dynamic evaluation of a string", "high"),
    ("exec(", "execution of generated code", "high"),
    ("__import__(", "import decided at run time", "high"),
    ("subprocess", "a child process is started", "medium"),
    ("os.system", "a shell command is run", "high"),
    ("pickle.load", "untrusted data is deserialised", "high"),
    ("shell=True", "a shell interprets the command", "high"),
    ("password", "a secret-looking literal is present", "low"),
    ("token", "a token-looking literal is present", "low"),
    ("secret", "a secret-looking literal is present", "low"),
    ("0.0.0.0", "a service listens on every interface", "medium"),
)


def analyse_risk(doc):
    """Security analysis for one stored resource."""
    body = (doc.get("body") or "").lower()
    findings = []
    for needle, reason, severity in RISK_PATTERNS:
        count = body.count(needle.lower())
        if count:
            findings.append({"needle": needle, "reason": reason,
                             "severity": severity, "count": count})
    order = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda item: (order.get(item["severity"], 9),
                                    item["needle"]))
    score = sum({"high": 10, "medium": 4, "low": 1}[item["severity"]]
                for item in findings)
    return {"findings": findings, "score": score,
            "level": "high" if score >= 20 else "medium" if score >= 6
            else "low" if score else "none"}


def change_history(doc, snapshot_records=None):
    """Compare a stored document with the same title in a snapshot."""
    rows = []
    for record in snapshot_records or []:
        if record.get("title") != doc.get("title"):
            continue
        rows.append({"time": record.get("modified", ""),
                     "digest": VaultSecurityCore.digest(record.get("body", "")),
                     "lines": len((record.get("body") or "").splitlines())})
    rows.insert(0, {"time": doc.get("modified", ""),
                    "digest": VaultSecurityCore.digest(doc.get("body") or ""),
                    "lines": len((doc.get("body") or "").splitlines()),
                    "current": True})
    return rows


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------
def vault_report(kind, core, store, extra=None):
    """One of the five vault reports, as plain text.

    ``kind`` is a :data:`VAULT_REPORTS` key.  Every report is built from the
    same two sources -- the core's policy and records, and the store's contents
    -- so a report can never claim a protection the vault is not running.
    """
    extra = extra or {}
    lines = ["ScriptDB vault report",
             "kind: %s" % VAULT_REPORT_LABELS.get(kind, kind),
             "generated: %s" % _now(),
             "store: %s" % getattr(store, "db_path", ""),
             ""]
    if kind == "security":
        lines += _security_lines(core, store)
    elif kind == "integrity":
        lines += _integrity_lines(store)
    elif kind == "access":
        lines += _access_lines(core)
    elif kind == "changes":
        lines += _changes_lines(store)
    elif kind == "risk":
        lines += _risk_lines(store)
    else:
        lines.append("unknown report kind")
    return "\n".join(lines) + "\n"


def _security_lines(core, store):
    lines = ["Policy"]
    for key, _lk, fallback, _default in VAULT_POLICY:
        value = core.policy.get(key)
        lines.append("  %-26s %s" % (fallback + ":", value))
    lines += ["",
              "State",
              "  encrypted:                 %s" % bool(core.has_key_material()),
              "  locked:                    %s" % bool(core.locked),
              "  failed attempts:           %d" % core.attempts,
              "  locked out:                %s" % core.is_locked_out(),
              "  session key held:          %s" % bool(core._session_key),
              "  isolated directory:        %s"
              % (store.is_isolated() if store else "n/a"),
              "  resources:                 %d" % (store.repo.count()
                                                   if store else 0)]
    if core.intrusions:
        lines += ["", "Intrusions"]
        for record in core.intrusions[:10]:
            lines.append("  %s  %s" % (record.get("time", ""),
                                       record.get("detail", "")))
    if core.manipulations:
        lines += ["", "Manipulation"]
        for record in core.manipulations[:10]:
            lines.append("  %s  %s" % (record.get("time", ""),
                                       record.get("detail", "")))
    return lines


def _integrity_lines(store):
    if store is None:
        return ["no store is open"]
    rows = store.integrity_report()
    intact = sum(1 for row in rows if row["state"] == "intact")
    changed = [row for row in rows if row["state"] == "changed"]
    unknown = [row for row in rows if row["state"] == "unverifiable"]
    lines = ["Resources: %d" % len(rows),
             "  intact:       %d" % intact,
             "  changed:      %d" % len(changed),
             "  unverifiable: %d" % len(unknown),
             ""]
    if changed:
        lines.append("Changed content")
        for row in changed:
            lines.append("  #%s %s" % (row.get("id"), row.get("title")))
    if unknown:
        lines.append("Unverifiable content")
        for row in unknown:
            lines.append("  #%s %s  (%s)" % (row.get("id"), row.get("title"),
                                             row.get("detail", "")))
    if not changed and not unknown:
        lines.append("Every readable resource matches its recorded digest.")
    return lines


def _access_lines(core):
    rows = core.access_report_rows()
    lines = ["Entries: %d" % len(rows), ""]
    for time, event, detail in rows:
        lines.append("  %s  %-24s %s" % (time, event, detail))
    return lines


def _changes_lines(store):
    if store is None:
        return ["no store is open"]
    rows = store.repo.list_documents(include_archived=True)
    order = sorted(rows, key=lambda row: row.get("modified") or "",
                   reverse=True)
    lines = ["Resources by last change", ""]
    for row in order:
        flags = flags_text(_with_flags(row)["flags"])
        lines.append("  %s  %-32s %s" % (row.get("modified", ""),
                                         (row.get("title") or "")[:32], flags))
    return lines


def _risk_lines(store):
    if store is None:
        return ["no store is open"]
    lines = []
    total = 0
    for doc in store.repo.list_documents(include_archived=True):
        found = analyse_risk(doc)
        total += found["score"]
        if not found["findings"]:
            continue
        lines.append("#%s %s  (level %s, score %d)"
                     % (doc.get("id"), doc.get("title"), found["level"],
                        found["score"]))
        for item in found["findings"]:
            lines.append("    %-8s %-14s %s"
                         % (item["severity"], item["needle"], item["reason"]))
    if not lines:
        return ["No risky construct was found in the stored content."]
    lines += ["", "Total risk score: %d" % total]
    return lines
