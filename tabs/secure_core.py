# -*- coding: utf-8 -*-
"""Script security core for ScriptDB.

This is the engine behind the "Security" tab (the *Script securing* tab).  It is
the toolkit that hardens the scripts the database holds, layer by layer, and it
is deliberately standard library only -- nothing here needs a third-party
package, so the same code protects a source checkout and a frozen build.

The layers, in the order the tab presents them, are:

    digests       SHA-256 / SHA-512 / SHA-3 / BLAKE2 fingerprints of a script
    hmac          a keyed integrity tag (HMAC-SHA256) so a digest cannot be
                  forged without the key
    kdf           PBKDF2-HMAC-SHA256 (and scrypt, and a pure-Python bcrypt-free
                  salted digest) to turn a passphrase into key material
    signatures    a per-script signature over the fingerprint, keyed with the
                  master key, compared in constant time
    integrity     a hash registry: every script's fingerprint recorded so a
                  later change is detectable
    tamper        a comparison of the registry against the files, with an alarm
                  and an optional vault lock when they disagree
    crypto        AES-free, dependency-free symmetric sealing (HMAC-SHA256
                  keystream + integrity tag) reused from the DOCs crypto, plus
                  per-file keys and one-time "shadow" keys
    sandbox       a policy description for isolating a run (fs / net / syscall
                  allow-lists, separate env, separate logs, separate process)
    ids           failed-attempt limit, cooldown and anomaly detection
    keys          master key, per-file keys, session keys, one-time keys
    backups       timestamped snapshots of a script and time-travel restore
    versioning    an append-only version history per script
    reports       integrity / change / access / error / warning / risk reports
    export        JSON export of a report, of the registry and of the config
    import        import of a config / registry / exported bundle

Everything that touches key material lives on :class:`SecurityCore`; the tab
never sees a raw key, only the outcome dictionaries, so the policy sits in one
place instead of being spread across the widgets.

    SecurityCore                  -- policy, keys, records, no UI
    SecurityRegistry              -- the hash registry file (integrity records)
    security_report(kind, ...)    -- the six reports, as plain text
"""

import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import tempfile
import zlib


# ---------------------------------------------------------------------------
# Names and defaults
# ---------------------------------------------------------------------------
SECURITY_DIR = "security"
SECURITY_CONFIG_FILE = "security.json"
SECURITY_REGISTRY_FILE = "registry.json"
SECURITY_BACKUP_DIR = "backups"
SECURITY_REPORT_DIR = "reports"

# The script extensions the tab knows how to secure.  The Scripts tab already
# lists these kinds; keeping the list here means the security layer agrees with
# it without importing the whole tab.
SECURABLE_EXTENSIONS = (
    ".bat", ".cmd", ".ps1", ".psm1", ".py", ".pyw", ".js", ".mjs", ".ts",
    ".vbs", ".vbe", ".reg", ".sh", ".rb", ".pl", ".sql", ".au3",
)

# Hash algorithms offered by the digest layer.  Each entry is (key, label key,
# fallback label, what it is).  ``hashlib.new`` accepts every one of them.
DIGEST_ALGORITHMS = (
    ("sha256", "sec_digest_sha256", "SHA-256", "the default 256-bit digest"),
    ("sha512", "sec_digest_sha512", "SHA-512", "a 512-bit digest"),
    ("sha3_256", "sec_digest_sha3", "SHA3-256", "the SHA-3 sponge digest"),
    ("blake2b", "sec_digest_blake2b", "BLAKE2b", "the fast BLAKE2 digest"),
)

DIGEST_KEYS = tuple(key for key, _lk, _f, _d in DIGEST_ALGORITHMS)
DIGEST_LABELS = {key: fallback for key, _lk, fallback, _d in DIGEST_ALGORITHMS}

# Key-derivation functions (KDF) offered.  Each entry is (key, label key,
# fallback, human description).
KDF_FUNCTIONS = (
    ("pbkdf2", "sec_kdf_pbkdf2", "PBKDF2-HMAC-SHA256",
     "the portable password hardening function"),
    ("scrypt", "sec_kdf_scrypt", "scrypt", "a memory-hard KDF"),
    ("sha256-salted", "sec_kdf_salted", "Salted SHA-256 (iterated)",
     "a dependency-free iterated digest for weak contexts"),
)

KDF_KEYS = tuple(key for key, _lk, _f, _d in KDF_FUNCTIONS)
KDF_LABELS = {key: fallback for key, _lk, fallback, _d in KDF_FUNCTIONS}

# The isolation policy for a sandboxed run of a secured script.  These are the
# options the tab toggles; the engine turns them into a description that the
# Sandbox tab understands.
SANDBOX_POLICY = (
    ("isolate_filesystem", "sec_sb_fs", "Isolate the filesystem", True),
    ("isolate_network", "sec_sb_net", "Block the network", True),
    ("isolate_syscalls", "sec_sb_syscalls", "Restrict syscalls", True),
    ("isolate_environment", "sec_sb_env", "Scrub the environment", True),
    ("separate_process", "sec_sb_process", "Run in a separate process", True),
    ("separate_logs", "sec_sb_logs", "Separate logs", True),
    ("shadow_fs", "sec_sb_shadow_fs", "Shadow filesystem", False),
    ("shadow_env", "sec_sb_shadow_env", "Shadow environment", False),
    ("shadow_net", "sec_sb_shadow_net", "Shadow network", False),
)

SANDBOX_POLICY_KEYS = tuple(key for key, _lk, _f, _d in SANDBOX_POLICY)
SANDBOX_POLICY_LABELS = {key: fallback for key, _lk, fallback, _d
                         in SANDBOX_POLICY}

# Authorization policy: the controls that guard the tab itself.
AUTH_POLICY = (
    ("require_password", "sec_auth_password", "Require a passphrase", True),
    ("require_token", "sec_auth_token", "Require a token (2nd factor)", False),
    ("max_attempts", "sec_auth_attempts", "Failed attempts allowed", 5),
    ("cooldown_seconds", "sec_auth_cooldown", "Cooldown after failures (s)", 60),
    ("auto_lock_seconds", "sec_auth_autolock", "Auto-lock (seconds)", 300),
    ("high_security", "sec_auth_high", "High-security mode", False),
    ("sign_scripts", "sec_auth_sign", "Sign every script", True),
    ("tamper_action", "sec_auth_tamper", "On tamper: lock the vault", True),
)

AUTH_POLICY_KEYS = tuple(key for key, _lk, _f, _d in AUTH_POLICY)
AUTH_POLICY_LABELS = {key: fallback for key, _lk, fallback, _d in AUTH_POLICY}

# The reports the tab can produce.  (key, label key, fallback).
SECURITY_REPORTS = (
    ("integrity", "sec_report_integrity", "Integrity report"),
    ("changes", "sec_report_changes", "Change report"),
    ("access", "sec_report_access", "Access report"),
    ("errors", "sec_report_errors", "Error report"),
    ("warnings", "sec_report_warnings", "Warning report"),
    ("risk", "sec_report_risk", "Risk report"),
)

SECURITY_REPORT_KEYS = tuple(key for key, _lk, _f in SECURITY_REPORTS)
SECURITY_REPORT_LABELS = {key: fallback for key, _lk, fallback in SECURITY_REPORTS}

# The event names every security action is recorded under, so the access report
# and the audit timeline can never disagree about what happened.
SECURITY_EVENTS = (
    "sec_created", "sec_opened", "sec_locked", "sec_unlocked",
    "sec_password_changed", "sec_unlock_failed", "sec_cooldown",
    "sec_high_security", "sec_token_issued", "sec_token_redeemed",
    "sec_fingerprinted", "sec_signed", "sec_verified", "sec_tamper",
    "sec_intrusion", "sec_sealed", "sec_unsealed", "sec_key_issued",
    "sec_key_revoked", "sec_backup_taken", "sec_backup_restored",
    "sec_version_recorded", "sec_report_written", "sec_exported",
    "sec_imported", "sec_sandbox_prepared",
)


# ---------------------------------------------------------------------------
# Small result helpers (the same shape the vault core uses)
# ---------------------------------------------------------------------------
def _ok(detail, **extra):
    out = {"ok": True, "detail": detail}
    out.update(extra)
    return out


def _fail(code, detail):
    return {"ok": False, "code": code, "detail": detail}


def _now():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _stamp():
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def _slug(text, fallback="item"):
    out = re.sub(r"[^A-Za-z0-9_.-]+", "-", (text or "").strip()).strip("-")
    return (out or fallback)[:64]


def _is_hex(value):
    return bool(value) and all(c in "0123456789abcdefABCDEF" for c in value)


# ---------------------------------------------------------------------------
# Layer 1 -- digests
# ---------------------------------------------------------------------------
def digest_bytes(data, algorithm="sha256"):
    """Return the hex digest of ``data`` under ``algorithm``.

    Every algorithm named in :data:`DIGEST_ALGORITHMS` is available in the
    standard library, so this never needs a fallback implementation.
    """
    algorithm = algorithm if algorithm in DIGEST_KEYS else "sha256"
    if algorithm == "sha512":
        return hashlib.sha512(data).hexdigest()
    if algorithm == "sha3_256":
        return hashlib.sha3_256(data).hexdigest()
    if algorithm == "blake2b":
        return hashlib.blake2b(data).hexdigest()
    return hashlib.sha256(data).hexdigest()


def digest_text(text, algorithm="sha256"):
    """The hex digest of a string, encoded UTF-8 first."""
    return digest_bytes((text or "").encode("utf-8"), algorithm)


def all_digests(text):
    """Every digest algorithm applied to one string, as {key: hex}."""
    return {key: digest_text(text, key) for key in DIGEST_KEYS}


def fingerprint(script, algorithm="sha256"):
    """The cryptographic fingerprint of a script mapping or a string.

    A script may arrive as a plain string (the usual case) or as the dict the
    Scripts tab stores; only the ``code`` field matters for the fingerprint, so
    the same script always fingerprints the same regardless of its metadata.
    """
    if isinstance(script, dict):
        body = script.get("code", "") or ""
        name = script.get("name", "")
    else:
        body = script or ""
        name = ""
    # The name is folded in so two identical bodies under different names do not
    # collide in the registry where the name is the key.
    payload = (name + "\x00" + body).encode("utf-8")
    return digest_bytes(payload, algorithm)


# ---------------------------------------------------------------------------
# Layer 2 -- keyed integrity (HMAC) and signatures
# ---------------------------------------------------------------------------
def hmac_tag(key, message):
    """HMAC-SHA256 of ``message`` under ``key`` (bytes in, hex out)."""
    if isinstance(message, str):
        message = message.encode("utf-8")
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def compare(a, b):
    """Constant-time string comparison (resistant to timing attacks)."""
    return hmac.compare_digest((a or "").encode("utf-8"),
                               (b or "").encode("utf-8"))


def sign_fingerprint(key, fingerprint_hex, name=""):
    """Sign a fingerprint with the master key.

    The signature covers the fingerprint *and* the script name, so a signature
    cannot be lifted from one script and pasted onto another.
    """
    return hmac_tag(key, "scriptdb-sign:" + (name or "") + ":" + (
        fingerprint_hex or ""))


def verify_signature(key, signature, fingerprint_hex, name=""):
    """True when ``signature`` is the one this key produces for the script."""
    expected = sign_fingerprint(key, fingerprint_hex, name)
    return compare(expected, signature or "")

# ---------------------------------------------------------------------------
# Layer 3 -- key derivation (KDF)
# ---------------------------------------------------------------------------
def derive(password, salt, function="pbkdf2", iterations=200000, length=32,
           memory=16384):
    """Derive a key from a password with the named KDF.

    ``pbkdf2`` uses the standard library's PBKDF2-HMAC-SHA256; ``scrypt`` uses
    ``hashlib.scrypt``; ``sha256-salted`` is a dependency-free iterated digest
    that stays usable when a context cannot afford the memory-hard ones.  The
    function never raises: an unsupported option falls back to the portable
    choice.
    """
    if isinstance(password, str):
        password = password.encode("utf-8")
    if isinstance(salt, str):
        salt = salt.encode("utf-8")
    salt = salt or b"scriptdb-salt"
    if function == "scrypt":
        try:
            return hashlib.scrypt(password, salt=salt, n=max(2 ** 14, 1 << 14),
                                  r=8, p=1, dklen=length,
                                  maxmem=memory * 1024 + 64 * 1024)
        except (ValueError, MemoryError):
            function = "pbkdf2"
    if function == "sha256-salted":
        block = hashlib.sha256(salt + password).digest()
        for _ in range(max(1, int(iterations)) // 1000 or 1):
            block = hashlib.sha256(block + salt + password).digest()
        return (block * ((length // len(block)) + 1))[:length]
    return hashlib.pbkdf2_hmac("sha256", password, salt,
                               max(1, int(iterations)), dklen=length)


def password_strength(password):
    """A 0-4 strength score plus notes, using only local heuristics.

    This is a report aid, never a gate: the length and variety are counted, and
    a repeated or sequential run is penalised.
    """
    text = password or ""
    notes = []
    score = 0
    if len(text) >= 8:
        score += 1
    else:
        notes.append("shorter than 8 characters")
    if len(text) >= 14:
        score += 1
    if any(c.islower() for c in text) and any(c.isupper() for c in text):
        score += 1
    else:
        notes.append("uses only one letter case")
    if any(c.isdigit() for c in text) and any(not c.isalnum() for c in text):
        score += 1
    else:
        notes.append("add a digit and a symbol")
    if text and len(set(text)) < 5:
        score = max(0, score - 1)
        notes.append("too few distinct characters")
    if re.search(r"(0123|1234|abcd|qwer|pass|admin)", text.lower()):
        score = max(0, score - 1)
        notes.append("contains a common sequence")
    return {"score": min(4, score), "notes": notes}


# ---------------------------------------------------------------------------
# Layer 4 -- symmetric sealing (encryption) and envelope helpers
# ---------------------------------------------------------------------------
# The sealing construction is the one the DOCs repository already uses: an
# HMAC-SHA256 keystream XORed over the payload, with the HMAC of the ciphertext
# carried as the integrity tag.  Reusing it means a sealed script and a sealed
# document are protected the same way, and the module stays standard library
# only.  The per-file key and the one-time (shadow) key are both produced here.

def _keystream(key, nonce, length):
    """Expand ``key`` and ``nonce`` into ``length`` bytes of keystream."""
    blocks = []
    counter = 0
    produced = 0
    while produced < length:
        blocks.append(hmac.new(key, nonce + counter.to_bytes(8, "big"),
                               hashlib.sha256).digest())
        produced += 32
        counter += 1
    return b"".join(blocks)[:length]


def _xor(data, stream):
    return bytes(a ^ b for a, b in zip(data, stream))


def seal_text(text, key):
    """Encrypt a string under ``key``; returns a printable envelope.

    The envelope is ``base64(nonce || ciphertext || tag)`` where the tag is the
    HMAC of ``nonce || ciphertext``, so any change to the payload is caught when
    it is opened.
    """
    nonce = os.urandom(16)
    payload = (text or "").encode("utf-8")
    stream = _keystream(key, nonce, len(payload))
    body = _xor(payload, stream)
    tag = hmac.new(key, nonce + body, hashlib.sha256).digest()
    return base64.b64encode(nonce + body + tag).decode("ascii")


def unseal_text(envelope, key):
    """Reverse :func:`seal_text`; refuses a tampered or foreign envelope."""
    try:
        raw = base64.b64decode((envelope or "").encode("ascii"))
    except Exception:
        return _fail("bad_envelope", "the envelope is not valid base64")
    if len(raw) < 48:
        return _fail("bad_envelope", "the envelope is too short")
    nonce, body, tag = raw[:16], raw[16:-32], raw[-32:]
    expected = hmac.new(key, nonce + body, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, tag):
        return _fail("tampered", "the integrity tag does not match")
    try:
        text = _xor(body, _keystream(key, nonce, len(body))).decode("utf-8")
    except UnicodeDecodeError:
        return _fail("tampered", "the payload does not decode")
    return _ok("unsealed", text=text)


def per_file_key(master_key, name):
    """Derive a unique key for one file from the master key.

    Each secured script gets its own key, so a key recovered for one file cannot
    open another, even though both come from the one master password.
    """
    return hmac.new(master_key, b"file-key:" + (name or "").encode("utf-8"),
                    hashlib.sha256).digest()


def one_time_key(label="", length=32):
    """A single-use session/project key (the "shadow" key)."""
    material = secrets.token_bytes(length)
    return {"token": secrets.token_urlsafe(24),
            "material": base64.b64encode(material).decode("ascii"),
            "label": label or "one-time"}


def export_key_bundle(master_key, secret, wrapped=True):
    """Export key material with a second layer of protection.

    The material is sealed under the ``secret`` supplied by the caller (a second
    password or the session key), so an exported bundle is not usable with the
    master password alone.  With ``wrapped`` off the bundle is still base64 and
    is clearly marked as unprotected.
    """
    raw = base64.b64encode(master_key).decode("ascii")
    if not wrapped:
        return {"protected": False, "material": raw}
    return {"protected": True, "material": seal_text(raw, secret)}


def import_key_bundle(bundle, secret):
    """Reverse :func:`export_key_bundle`; returns the raw key bytes."""
    if not isinstance(bundle, dict):
        return _fail("bad_bundle", "the bundle is not a dictionary")
    material = bundle.get("material", "")
    if not bundle.get("protected"):
        try:
            return _ok("imported", key=base64.b64decode(material.encode("ascii")))
        except Exception:
            return _fail("bad_bundle", "the material is not valid base64")
    outcome = unseal_text(material, secret)
    if not outcome["ok"]:
        return outcome
    try:
        return _ok("imported", key=base64.b64decode(outcome["text"]))
    except Exception:
        return _fail("bad_bundle", "the material is not valid base64")


# ---------------------------------------------------------------------------
# Layer 5 -- integrity registry (hash registry + manipulation detection)
# ---------------------------------------------------------------------------
class SecurityRegistry(object):
    """The hash registry: one integrity record per secured script.

    A record remembers the fingerprint under every algorithm, the signature, the
    optional sealed body, and the version count.  The registry is a plain JSON
    file so it is readable, diffable and portable, and it never holds a key.
    """

    def __init__(self, path=None):
        self.path = path or ""
        self.records = {}
        self.load()

    # ------------------------------------------------------------ persistence
    def load(self):
        if not self.path or not os.path.isfile(self.path):
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError):
            return {}
        records = payload.get("scripts") if isinstance(payload, dict) else None
        self.records = records if isinstance(records, dict) else {}
        return self.records

    def save(self):
        if not self.path:
            return False
        payload = {
            "format": "scriptdb-security-registry",
            "version": 1,
            "updated": _now(),
            "scripts": self.records,
        }
        _atomic_write_json(self.path, payload)
        return True

    # ---------------------------------------------------------------- records
    def get(self, name):
        return self.records.get(name or "")

    def names(self):
        return sorted(self.records)

    def record_script(self, script, master_key=None, algorithm="sha256",
                      sealed=None, sign=True):
        """Write (or refresh) the integrity record for one script.

        Returns the record.  ``sealed`` is the already-sealed body when the tab
        chose to encrypt it; it is stored alongside the digests so a restore can
        decrypt without ever holding the plaintext in the registry.
        """
        name = (script.get("name", "") if isinstance(script, dict)
                else "") or "script"
        body = (script.get("code", "") if isinstance(script, dict)
                else (script or ""))
        # The fingerprint folds in the name the same way SecurityRegistry.check
        # recomputes it, so a record taken here always verifies against the same
        # construction that reads it back.
        digests = all_digests(body)
        fold = (name + "\x00" + (body or "")).encode("utf-8")
        folded = {key: digest_bytes(fold, key) for key in DIGEST_KEYS}
        fp = folded.get(algorithm, folded.get("sha256"))
        record = {
            "name": name,
            "category": (script.get("category", "") if isinstance(script, dict)
                         else ""),
            "algorithm": algorithm,
            "fingerprint": fp,
            "digests": folded,
            "body_digests": digests,
            "size": len((body or "").encode("utf-8")),
            "digest_sha256": folded.get("sha256", ""),
            "recorded": _now(),
            "version": 1,
            "signature": "",
            "sealed": sealed or "",
            "sealed_with": "per-file" if sealed else "",
            "versions": [],
        }
        previous = self.records.get(name)
        if previous:
            # Keep the version history and count growing rather than resetting.
            record["version"] = int(previous.get("version", 1)) + 1
            record["versions"] = list(previous.get("versions", []))
        if master_key is not None and sign:
            record["signature"] = sign_fingerprint(master_key, fp, name)
        self.records[name] = record
        return record

    def push_version(self, name, digest, note="", sealed=""):
        """Append a version entry to a record's history (time travel)."""
        record = self.records.get(name or "")
        if not record:
            return None
        entry = {"time": _now(), "digest": digest, "note": note,
                 "sealed": sealed}
        versions = list(record.get("versions", []))
        versions.append(entry)
        # Keep the history bounded so the registry cannot grow without limit.
        record["versions"] = versions[-100:]
        return entry

    def remove(self, name):
        return self.records.pop(name or "", None)

    # --------------------------------------------------------------- checking
    def check(self, script, master_key=None):
        """Compare one script against its record; returns a state dict.

        The states are ``intact`` (everything matches), ``changed`` (the body no
        longer matches its fingerprint), ``unsigned`` (a record with no
        signature), ``foreign`` (a valid signature that belongs to another name)
        and ``missing`` (no record at all).
        """
        name = (script.get("name", "") if isinstance(script, dict) else "") or ""
        record = self.records.get(name)
        if not record:
            return {"name": name, "state": "missing",
                    "detail": "no integrity record"}
        body = (script.get("code", "") if isinstance(script, dict)
                else (script or ""))
        algorithm = record.get("algorithm", "sha256")
        found = digest_bytes((name + "\x00" + (body or "")).encode("utf-8"),
                             algorithm)
        expected = record.get("fingerprint", "")
        if not compare(expected, found):
            return {"name": name, "state": "changed", "expected": expected,
                    "found": found, "detail": "content changed"}
        signature = record.get("signature", "")
        if not signature:
            return {"name": name, "state": "unsigned", "expected": expected,
                    "found": found, "detail": "no signature"}
        if master_key is not None:
            if not verify_signature(master_key, signature, expected, name):
                return {"name": name, "state": "foreign", "expected": expected,
                        "found": found,
                        "detail": "the signature is not valid for this key"}
        return {"name": name, "state": "intact", "expected": expected,
                "found": found, "detail": "record matches"}

    def verify_all(self, scripts, master_key=None):
        """Check every script; returns a list of state dicts."""
        return [self.check(script, master_key) for script in (scripts or [])]

    def summary(self, states):
        """Count the states into the buckets the reports use."""
        out = {"intact": 0, "changed": 0, "unsigned": 0, "foreign": 0,
               "missing": 0, "total": 0}
        for state in states or []:
            key = state.get("state", "missing")
            out[key] = out.get(key, 0) + 1
            out["total"] += 1
        return out


# ---------------------------------------------------------------------------
# Layer 6 -- sandbox policy description
# ---------------------------------------------------------------------------
def sandbox_description(policy=None, workspace=""):
    """Turn the isolation policy into a description the Sandbox tab can read.

    This never launches anything: it produces the plan -- which shadows are on,
    which allow-lists apply, where the logs and the workspace live -- so the
    run itself is still made by the Sandbox tab under its own hardening.
    """
    policy = dict(policy or {})
    enabled = [key for key in SANDBOX_POLICY_KEYS if policy.get(key)]
    blocked = []
    if policy.get("isolate_filesystem"):
        blocked.append("writes outside the workspace")
    if policy.get("isolate_network"):
        blocked.append("network sockets")
    if policy.get("isolate_syscalls"):
        blocked.append("process and registry syscalls")
    if policy.get("isolate_environment"):
        blocked.append("inherited environment variables")
    shadows = [key for key in ("shadow_fs", "shadow_env", "shadow_net")
               if policy.get(key)]
    return {
        "workspace": workspace,
        "enabled": enabled,
        "blocked": blocked,
        "shadows": shadows,
        "separate_process": bool(policy.get("separate_process", True)),
        "separate_logs": bool(policy.get("separate_logs", True)),
        "description": "; ".join(blocked) if blocked else "no isolation",
        "isolated": bool(policy.get("isolate_filesystem")
                         or policy.get("isolate_network")
                         or policy.get("isolate_syscalls")),
    }


# ---------------------------------------------------------------------------
# Layer 7 -- the core
# ---------------------------------------------------------------------------
class SecurityCore(object):
    """Policy, key material and session state for the Security tab (no UI).

    The core owns the derived master key, the rotating session key, the token
    counter, the attempt counter and the cooldown clock.  The tab asks it
    questions and never touches a key, which keeps every guard in one place.
    """

    #: How long a derived key may live before it is recomputed.
    SESSION_KEY_LIFETIME = 900

    def __init__(self, path=None):
        self.path = path or ""
        self.policy = {key: default for key, _lk, _f, default in AUTH_POLICY}
        self.sandbox = {key: default for key, _lk, _f, default in SANDBOX_POLICY}
        self.salt = None
        self.verifier = None             # proves a password without the key
        self._key = None                 # the master key, derived on unlock
        self._session_key = None         # rotates on every unlock
        self._session_started = None
        self._last_activity = None
        self.attempts = 0
        self.cooldown_until = None
        self.locked = True
        self.events = []                 # the access record, newest first
        self.tokens = {}                 # issued second-factor tokens
        self.intrusions = []             # detected anomalies
        self.manipulations = []          # detected tamper records
        self.alarms = []                 # raised alarms (tamper, intrusion)
        self.load()

    # --------------------------------------------------------------- policy
    def set_policy(self, changes):
        """Update the authorization policy; returns the changed keys."""
        changed = []
        for key, value in (changes or {}).items():
            if key in AUTH_POLICY_KEYS and self.policy.get(key) != value:
                self.policy[key] = value
                changed.append(key)
        if changed:
            self.save()
        return changed

    def set_sandbox(self, changes):
        """Update the sandbox policy; returns the changed keys."""
        changed = []
        for key, value in (changes or {}).items():
            if key in SANDBOX_POLICY_KEYS and self.sandbox.get(key) != value:
                self.sandbox[key] = value
                changed.append(key)
        if changed:
            self.save()
        return changed

    # -------------------------------------------------------------- lifetime
    def has_key_material(self):
        return bool(self.salt and self.verifier)

    def create(self, password, confirm=None):
        """Create the key material for a new master passphrase."""
        if not password or len(password) < 8:
            return _fail("password_too_short",
                         "the passphrase needs at least 8 characters")
        if confirm is not None and password != confirm:
            return _fail("password_mismatch", "the two passphrases differ")
        self.salt = os.urandom(16)
        key = derive(password, self.salt, "pbkdf2", self._iterations())
        self._key = key
        self.verifier = self.verifier_for(key)
        self._session_key = None
        self.locked = True
        self.attempts = 0
        self.cooldown_until = None
        self.record("sec_created", "key material created")
        self.save()
        return _ok("key material created")

    def change_password(self, old, new):
        outcome = self.verify(old)
        if not outcome["ok"]:
            return outcome
        return self.create(new)

    def unlock(self, password, token=None):
        """Unlock the tab, honouring the attempt limit and cooldown policy."""
        if self.is_in_cooldown():
            return _fail("cooldown", "too many failures; %d second(s) left"
                         % self.cooldown_remaining())
        if not self.has_key_material():
            return _fail("not_created", "there is no key material yet")
        candidate = derive(password, self.salt, "pbkdf2", self._iterations())
        if not self.check_password(candidate):
            return self._failed_attempt()
        if self.policy.get("require_token"):
            if not token:
                challenge = self.issue_token()
                return _fail("needs_token",
                             "a token is required (challenge %s)"
                             % challenge.get("token", ""))
            if not self.redeem_token(token)["ok"]:
                return self._failed_attempt()
        return self._grant(candidate)

    @staticmethod
    def verifier_for(key):
        # The raw HMAC digest that proves a password without the key.
        return hmac.new(key, b"scriptdb-security-verifier",
                        hashlib.sha256).digest()

    def check_password(self, candidate):
        """True when a derived key matches the stored verifier.

        The check is made against the verifier -- an HMAC of a fixed label under
        the master key -- and not against the key itself, so locking can drop
        the key from memory while the verifier stays available to prove a
        password.  The verifier cannot decrypt anything on its own.
        """
        if not self.verifier or not candidate:
            return False
        return hmac.compare_digest(self.verifier_for(candidate), self.verifier)

    def verify(self, password):
        """Check a password without touching the session state."""
        if not self.salt or not password:
            return _fail("no_password", "no passphrase given")
        candidate = derive(password, self.salt, "pbkdf2", self._iterations())
        if not self.check_password(candidate):
            return _fail("bad_password", "the passphrase does not match")
        return _ok("passphrase accepted")

    def _grant(self, candidate):
        if candidate is not None:
            self._key = candidate
        self.locked = False
        self.attempts = 0
        self.cooldown_until = None
        self._session_key = os.urandom(32)
        self._session_started = dt.datetime.now()
        self._last_activity = dt.datetime.now()
        self.record("sec_unlocked", "session opened")
        return _ok("unlocked")

    def _failed_attempt(self):
        self.attempts += 1
        allowed = int(self.policy.get("max_attempts") or 5)
        self.record("sec_unlock_failed",
                    "failed attempt %d of %d" % (self.attempts, allowed))
        if self.attempts >= allowed:
            seconds = int(self.policy.get("cooldown_seconds") or 60)
            self.cooldown_until = dt.datetime.now() + dt.timedelta(
                seconds=seconds)
            self.record("sec_cooldown", "cooling down for %d second(s)"
                        % seconds)
            self.detect_intrusion("%d failed unlock attempts" % self.attempts)
            return _fail("cooldown", "cooling down for %d second(s)" % seconds)
        remaining = allowed - self.attempts
        return _fail("bad_password",
                     "wrong passphrase; %d attempt(s) left" % remaining)

    def lock(self, reason="locked"):
        """Close the session and drop every key held in memory."""
        self.locked = True
        self._session_key = None
        self._session_started = None
        self._key = None
        self.record("sec_locked", reason)
        return _ok(reason)

    # ------------------------------------------------------------- sessions
    def touch(self):
        self._last_activity = dt.datetime.now()
        return self._last_activity

    def idle_seconds(self):
        if self._last_activity is None:
            return 0
        return (dt.datetime.now() - self._last_activity).total_seconds()

    def should_auto_lock(self):
        if self.locked:
            return False
        limit = int(self.policy.get("auto_lock_seconds") or 0)
        if limit <= 0:
            return False
        # High-security mode halves the idle window before the tab re-locks.
        if self.policy.get("high_security"):
            limit = max(30, limit // 2)
        return self.idle_seconds() >= limit

    def check_auto_lock(self):
        if self.should_auto_lock():
            self.lock("auto-lock after inactivity")
            return True
        return False

    def session_key_expired(self):
        if self._session_started is None:
            return True
        age = (dt.datetime.now() - self._session_started).total_seconds()
        return age >= self.SESSION_KEY_LIFETIME

    def rotate_session_key(self):
        if self.locked:
            return _fail("locked", "the tab is locked")
        self._session_key = os.urandom(32)
        self._session_started = dt.datetime.now()
        self.record("sec_opened", "session key rotated")
        return _ok("session key rotated")

    def is_in_cooldown(self):
        if self.cooldown_until is None:
            return False
        if dt.datetime.now() >= self.cooldown_until:
            self.cooldown_until = None
            self.attempts = 0
            return False
        return True

    def cooldown_remaining(self):
        if self.cooldown_until is None:
            return 0
        left = (self.cooldown_until - dt.datetime.now()).total_seconds()
        return max(0, int(left))

    def enable_high_security(self, active=True):
        """Turn high-security mode on or off (it tightens the guards)."""
        self.policy["high_security"] = bool(active)
        self.save()
        self.record("sec_high_security",
                    "high-security mode %s" % ("on" if active else "off"))
        return _ok("high-security mode %s" % ("on" if active else "off"))

    # --------------------------------------------------------- tokens (2FA)
    def issue_token(self, label=""):
        """Mint a short second-factor token for the current challenge."""
        token = secrets.token_hex(3).upper()
        self.tokens[token] = {"used": False, "created": _now(), "label": label}
        self.record("sec_token_issued", "token issued for %r" % label)
        return {"ok": True, "token": token, "detail": "token issued"}

    def redeem_token(self, token):
        entry = self.tokens.get((token or "").strip().upper())
        if entry is None:
            return _fail("unknown_token", "that token was never issued")
        if entry.get("used"):
            self.detect_intrusion("a second-factor token was replayed")
            return _fail("spent", "that token has already been used")
        entry["used"] = True
        self.save()
        self.record("sec_token_redeemed", "token accepted")
        return _ok("token accepted")

    # ----------------------------------------------------------- detection
    def detect_intrusion(self, detail, severity="high"):
        record = {"time": _now(), "detail": detail, "severity": severity}
        self.intrusions.insert(0, record)
        self.alarms.insert(0, {"kind": "intrusion", "time": record["time"],
                               "detail": detail})
        self.record("sec_intrusion", detail)
        return record

    def detect_manipulation(self, detail, expected=None, found=None):
        record = {"time": _now(), "detail": detail, "expected": expected,
                  "found": found}
        self.manipulations.insert(0, record)
        self.alarms.insert(0, {"kind": "tamper", "time": record["time"],
                               "detail": detail})
        self.record("sec_tamper", detail)
        return record

    def scan_intrusions(self, events=None):
        """Look for anomalies in the access record that normal use cannot cause."""
        events = events if events is not None else self.events
        found = []
        failed = [e for e in events if e.get("event") == "sec_unlock_failed"]
        if len(failed) >= 3:
            found.append({"detail": "%d failed unlock attempts" % len(failed),
                          "severity": "high"})
        replays = [e for e in events if "replayed" in (e.get("detail") or "")]
        for _ in replays:
            found.append({"detail": "a one-time token was replayed",
                          "severity": "high"})
        if any(e.get("event") == "sec_tamper" for e in events):
            found.append({"detail": "content no longer matches its record",
                          "severity": "high"})
        for record in found:
            self.detect_intrusion(record["detail"], record["severity"])
        return found

    # ---------------------------------------------------------------- crypto
    def key(self):
        """The raw master key (only available while unlocked)."""
        return self._key

    def hash_script(self, script, algorithm="sha256"):
        """Hash one script under one algorithm; returns the digest dict."""
        body = (script.get("code", "") if isinstance(script, dict)
                else (script or ""))
        digests = all_digests(body)
        return _ok("hashed", algorithm=algorithm,
                   digest=digests.get(algorithm, digests["sha256"]),
                   digests=digests)

    def hmac_script(self, script):
        """A keyed integrity tag for one script (needs an unlocked session)."""
        if self._key is None:
            return _fail("locked", "the tab is locked")
        if isinstance(script, dict):
            name = script.get("name", "")
            body = script.get("code", "") or ""
        else:
            name, body = "", script or ""
        tag = hmac_tag(self._key, "scriptdb-hmac:" + name + "\x00" + body)
        return _ok("tagged", tag=tag)

    def sign(self, script):
        """Sign a script: fingerprint it, then HMAC the fingerprint."""
        if self._key is None:
            return _fail("locked", "the tab is locked")
        name = script.get("name", "") if isinstance(script, dict) else ""
        fp = fingerprint(script, "sha256")
        signature = sign_fingerprint(self._key, fp, name)
        return _ok("signed", fingerprint=fp, signature=signature, name=name)

    def verify_signature(self, script, signature):
        if self._key is None:
            return _fail("locked", "the tab is locked")
        name = script.get("name", "") if isinstance(script, dict) else ""
        fp = fingerprint(script, "sha256")
        good = verify_signature(self._key, signature, fp, name)
        return _ok("signature valid" if good else "signature invalid",
                   valid=good, expected=sign_fingerprint(self._key, fp, name))

    def seal_script(self, script, use_session=False):
        """Encrypt a script body with its own per-file key.

        With ``use_session`` the per-file key is wrapped again under the rotating
        session key, so a captured key alone is not enough to read the payload
        within the same session (the double-layer protection).
        """
        if self._key is None:
            return _fail("locked", "the tab is locked")
        name = (script.get("name", "") if isinstance(script, dict)
                else "script")
        body = (script.get("code", "") if isinstance(script, dict)
                else (script or ""))
        key = per_file_key(self._key, name)
        envelope = seal_text(body, key)
        if use_session:
            if self._session_key is None:
                return _fail("locked", "no session key")
            envelope = seal_text(envelope, self._session_key)
        return _ok("sealed", sealed=envelope, key_label=name,
                   layered=bool(use_session))

    def unseal_script(self, name, envelope, layered=False):
        """Reverse :meth:`seal_script` (session layer first when layered)."""
        if self._key is None:
            return _fail("locked", "the tab is locked")
        candidate = envelope
        if layered:
            if self._session_key is None:
                return _fail("locked", "no session key")
            outcome = unseal_text(candidate, self._session_key)
            if not outcome["ok"]:
                return outcome
            candidate = outcome["text"]
        key = per_file_key(self._key, name)
        outcome = unseal_text(candidate, key)
        if not outcome["ok"]:
            return outcome
        return _ok("unsealed", text=outcome["text"])

    def issue_shadow_key(self, label=""):
        """Mint a one-time / session / project key (the "shadow" key)."""
        if self.policy.get("high_security") and self._key is None:
            return _fail("locked", "issue keys only while unlocked")
        entry = one_time_key(label)
        entry["created"] = _now()
        self.tokens["KEY-" + entry["token"]] = {
            "used": False, "created": entry["created"], "label": label,
            "material": entry["material"]}
        self.record("sec_key_issued", "shadow key issued for %r" % label)
        self.save()
        return _ok("shadow key issued", key=entry)

    def revoke_shadow_key(self, token):
        entry = self.tokens.get("KEY-" + (token or ""))
        if entry is None:
            return _fail("unknown_key", "that key was never issued")
        entry["used"] = True
        self.record("sec_key_revoked", "shadow key revoked")
        self.save()
        return _ok("shadow key revoked")

    # ------------------------------------------------------------ persistence
    def config_path(self):
        if not self.path:
            return ""
        return os.path.join(self.path, SECURITY_CONFIG_FILE)

    def to_dict(self):
        """The state safe to write to disk (never a key)."""
        return {
            "format": "scriptdb-security",
            "version": 1,
            "salt": base64.b64encode(self.salt).decode("ascii")
            if self.salt else "",
            "verifier": base64.b64encode(self.verifier).decode("ascii")
            if self.verifier else "",
            "policy": self.policy,
            "sandbox": self.sandbox,
            "events": self.events[:500],
            "tokens": self.tokens,
            "intrusions": self.intrusions[:100],
            "manipulations": self.manipulations[:100],
            "alarms": self.alarms[:100],
            "updated": _now(),
        }

    def save(self):
        if not self.config_path():
            return False
        _atomic_write_json(self.config_path(), self.to_dict())
        return True

    def load(self):
        path = self.config_path()
        if not path or not os.path.isfile(path):
            return False
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError):
            return False
        try:
            self.salt = (base64.b64decode(payload["salt"].encode("ascii"))
                         if payload.get("salt") else None)
            self.verifier = (base64.b64decode(payload["verifier"]
                                              .encode("ascii"))
                             if payload.get("verifier") else None)
        except Exception:
            self.salt = self.verifier = None
        for key, value in (payload.get("policy") or {}).items():
            if key in AUTH_POLICY_KEYS:
                self.policy[key] = value
        for key, value in (payload.get("sandbox") or {}).items():
            if key in SANDBOX_POLICY_KEYS:
                self.sandbox[key] = value
        self.events = list(payload.get("events") or [])
        self.tokens = dict(payload.get("tokens") or {})
        self.intrusions = list(payload.get("intrusions") or [])
        self.manipulations = list(payload.get("manipulations") or [])
        self.alarms = list(payload.get("alarms") or [])
        return True

    def record(self, event, detail=""):
        """Append one event to the access record (newest first)."""
        if event not in SECURITY_EVENTS:
            return None
        entry = {"time": _now(), "event": event, "detail": detail}
        self.events.insert(0, entry)
        self.events = self.events[:1000]
        return entry

    def access_rows(self, limit=200):
        return [(e.get("time", ""), e.get("event", ""), e.get("detail", ""))
                for e in self.events[:limit]]

    @staticmethod
    def _iterations():
        return 200000


# ---------------------------------------------------------------------------
# Backups and versioning (file level)
# ---------------------------------------------------------------------------
def backup_script(script, directory, note=""):
    """Write a timestamped backup of one script; returns the backup path.

    The backup holds the plaintext body plus the record that was taken with it,
    so a restore can put the content back and re-verify the fingerprint.
    """
    name = ((script.get("name", "") if isinstance(script, dict) else "")
            or "script")
    os.makedirs(directory, exist_ok=True)
    stem, ext = os.path.splitext(name)
    path = os.path.join(directory, "%s.%s%s.bak" % (_slug(stem, "script"),
                                                    _stamp(), ext or ".txt"))
    body = (script.get("code", "") if isinstance(script, dict) else (script or ""))
    payload = {
        "format": "scriptdb-security-backup",
        "name": name,
        "note": note,
        "taken": _now(),
        "digest": digest_text(body, "sha256"),
        "body": body,
    }
    _atomic_write_json(path, payload)
    return path


def list_backups(directory):
    """The backups on disk, newest first, as light dicts."""
    if not directory or not os.path.isdir(directory):
        return []
    out = []
    for entry in os.listdir(directory):
        if not entry.endswith(".bak"):
            continue
        path = os.path.join(directory, entry)
        try:
            info = _read_json(path)
        except Exception:
            continue
        out.append({"file": entry, "path": path,
                    "name": info.get("name", ""),
                    "taken": info.get("taken", ""),
                    "note": info.get("note", ""),
                    "digest": info.get("digest", ""),
                    "modified": os.path.getmtime(path)})
    out.sort(key=lambda row: row.get("modified", 0), reverse=True)
    return out


def read_backup(path):
    """Load one backup file; returns its payload or None."""
    try:
        return _read_json(path)
    except Exception:
        return None


def restore_backup(path, expected_digest=None):
    """Read a backup and check it against its own recorded digest."""
    payload = read_backup(path)
    if not payload:
        return _fail("missing", "the backup could not be read")
    body = payload.get("body", "")
    digest = digest_text(body, "sha256")
    if expected_digest and not compare(digest, expected_digest):
        return _fail("tampered", "the backup does not match its digest")
    if payload.get("digest") and not compare(digest, payload["digest"]):
        return _fail("tampered", "the backup content does not match its digest")
    return _ok("backup restored", name=payload.get("name", ""), body=body,
               digest=digest)


def diff_scripts(left, right):
    """A very small structural diff of two script bodies (line counts + first
    differing line), enough for a change report without pulling in difflib."""
    left_lines = (left or "").splitlines()
    right_lines = (right or "").splitlines()
    first = None
    for index in range(max(len(left_lines), len(right_lines))):
        a = left_lines[index] if index < len(left_lines) else ""
        b = right_lines[index] if index < len(right_lines) else ""
        if a != b:
            first = {"line": index + 1, "left": a, "right": b}
            break
    return {"left_lines": len(left_lines), "right_lines": len(right_lines),
            "changed": first is not None, "first_change": first}


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------
def security_report(kind, core, registry, states=None, extra=None):
    """One of the six security reports, as plain text.

    Every report is built from the same two sources -- the core's policy and
    records, and the registry's integrity states -- so a report can never claim
    a protection the tab is not running.
    """
    extra = extra or {}
    states = states or []
    lines = ["ScriptDB security report",
             "kind: %s" % SECURITY_REPORT_LABELS.get(kind, kind),
             "generated: %s" % _now(),
             "registry: %s" % (getattr(registry, "path", "") or "n/a"),
             ""]
    if kind == "integrity":
        lines += _integrity_lines(registry, states)
    elif kind == "changes":
        lines += _changes_lines(registry)
    elif kind == "access":
        lines += _access_lines(core)
    elif kind == "errors":
        lines += _errors_lines(core, registry, states)
    elif kind == "warnings":
        lines += _warnings_lines(core, states)
    elif kind == "risk":
        lines += _risk_lines(core, registry, states)
    else:
        lines.append("unknown report kind")
    return "\n".join(lines) + "\n"


def _policy_block(core):
    lines = ["Policy"]
    for key, _lk, fallback, _default in AUTH_POLICY:
        lines.append("  %-28s %s" % (fallback + ":", core.policy.get(key)))
    lines.append("")
    lines.append("Sandbox")
    for key, _lk, fallback, _default in SANDBOX_POLICY:
        lines.append("  %-28s %s" % (fallback + ":", core.sandbox.get(key)))
    return lines


def _integrity_lines(registry, states):
    summary = registry.summary(states)
    lines = ["Scripts recorded: %d" % len(registry.names()),
             "Checked now:      %d" % summary.get("total", 0),
             "",
             "  intact:     %d" % summary.get("intact", 0),
             "  changed:    %d" % summary.get("changed", 0),
             "  unsigned:   %d" % summary.get("unsigned", 0),
             "  foreign:    %d" % summary.get("foreign", 0),
             "  missing:    %d" % summary.get("missing", 0),
             ""]
    changed = [s for s in states if s.get("state") == "changed"]
    if changed:
        lines.append("Changed scripts")
        for state in changed:
            lines.append("  %-30s expected %s" % (state.get("name"),
                                                  (state.get("expected") or "")[:16]))
            lines.append("  %-30s found    %s" % ("",
                                                  (state.get("found") or "")[:16]))
    else:
        lines.append("No script matches a changed state.")
    return lines


def _changes_lines(registry):
    lines = ["Registry entries: %d" % len(registry.names()), ""]
    for name in registry.names():
        record = registry.get(name)
        versions = record.get("versions", [])
        lines.append("  %-30s v%d  %s" % (name, record.get("version", 1),
                                          record.get("recorded", "")))
        for entry in versions[-5:]:
            lines.append("      %s  %s  %s" % (entry.get("time", ""),
                                               (entry.get("digest") or "")[:16],
                                               entry.get("note", "")))
    return lines


def _access_lines(core):
    rows = core.access_rows()
    lines = ["Entries: %d" % len(rows), ""]
    for time, event, detail in rows:
        lines.append("  %s  %-22s %s" % (time, event, detail))
    return lines


def _errors_lines(core, registry, states):
    lines = ["Failed unlocks: %d" % sum(
        1 for e in core.events if e.get("event") == "sec_unlock_failed"),
        "Cool-downs:     %d" % sum(
            1 for e in core.events if e.get("event") == "sec_cooldown"),
        "Tamper events:  %d" % len(core.manipulations),
        ""]
    changed = [s for s in states if s.get("state") in ("changed", "foreign")]
    if changed:
        lines.append("Errors found")
        for state in changed:
            lines.append("  %-30s %s" % (state.get("name"),
                                         state.get("detail", "")))
    else:
        lines.append("No errors recorded.")
    return lines


def _warnings_lines(core, states):
    lines = []
    if not core.has_key_material():
        lines.append("  no key material: the tab has no master passphrase yet")
    if not core.policy.get("require_password"):
        lines.append("  passphrase protection is switched off")
    if not core.policy.get("require_token"):
        lines.append("  no second factor is required")
    if not core.policy.get("sign_scripts"):
        lines.append("  scripts are not signed automatically")
    unsigned = [s for s in states if s.get("state") == "unsigned"]
    if unsigned:
        lines.append("  %d record(s) carry no signature" % len(unsigned))
    missing = [s for s in states if s.get("state") == "missing"]
    if missing:
        lines.append("  %d script(s) have no integrity record" % len(missing))
    if not lines:
        lines.append("  no warnings")
    return lines


def _risk_lines(core, registry, states):
    summary = registry.summary(states)
    score = 0
    score += 3 * summary.get("changed", 0)
    score += 2 * summary.get("foreign", 0)
    score += 1 * summary.get("unsigned", 0)
    score += 1 * summary.get("missing", 0)
    score += 2 * len(core.intrusions)
    score += 2 * len(core.manipulations)
    if not core.has_key_material():
        score += 5
    if not core.policy.get("require_password"):
        score += 3
    level = ("low" if score <= 2 else "moderate" if score <= 6
             else "high" if score <= 12 else "critical")
    lines = ["Risk score: %d (%s)" % (score, level),
             "",
             "Drivers"]
    if summary.get("changed"):
        lines.append("  changed scripts:    %d" % summary["changed"])
    if summary.get("foreign"):
        lines.append("  foreign signatures: %d" % summary["foreign"])
    if summary.get("missing"):
        lines.append("  missing records:    %d" % summary["missing"])
    if core.manipulations:
        lines.append("  tamper events:      %d" % len(core.manipulations))
    if core.intrusions:
        lines.append("  intrusions:         %d" % len(core.intrusions))
    if not core.has_key_material():
        lines.append("  no master key")
    if not summary.get("changed") and not core.manipulations:
        lines.append("  nothing anomalous recorded")
    return lines


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------
def _read_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _atomic_write_json(path, payload):
    """Write JSON so a crash can never leave a half-written file."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=directory, prefix=".sec-",
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


def export_report(kind, core, registry, states=None, directory=""):
    """Write one report to a JSON file; returns the path."""
    text = security_report(kind, core, registry, states)
    if not directory:
        return ""
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, "security-%s-%s.json" % (kind, _stamp()))
    payload = {"format": "scriptdb-security-report", "kind": kind,
               "generated": _now(), "text": text,
               "summary": registry.summary(states or [])}
    _atomic_write_json(path, payload)
    return path


def is_securable(name):
    """True when a filename carries an extension this layer secures."""
    return os.path.splitext(name or "")[1].lower() in SECURABLE_EXTENSIONS


def make_directory(path, private=True):
    """Create a directory; restrict its permissions where the OS allows it."""
    os.makedirs(path, exist_ok=True)
    if private and os.name == "posix":
        try:
            os.chmod(path, stat.S_IRWXU)
        except OSError:
            pass
    return path


def crc32_of(text):
    """A cheap non-cryptographic checksum, for quick change detection only."""
    return zlib.crc32((text or "").encode("utf-8")) & 0xffff
