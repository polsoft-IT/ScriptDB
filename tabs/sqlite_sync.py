# -*- coding: utf-8 -*-
"""Sync Layer for the SQLite tab.

This module joins the two data layers. It is the only place that knows about
both an offline database and an online one, which is what keeps each of them
ignorant of the other and independently testable.

    SyncEngine.compare()                offline vs online, row by row
    SyncEngine.plan(mode)               what a sync would change, changing nothing
    SyncEngine.run(mode)                perform it
    SyncEngine.resolve(conflict, side)  decide one conflict
    SyncEngine.change_report()          the result, as plain text

The four modes, and what each one may do:

    upload      offline -> online       bring the online copy up to date
    download    online -> offline       bring the offline copy up to date
    bidirectional                       both directions, newest write wins
    manual                              nothing is applied without a decision

A conflict is one row that both sides changed. It is never resolved silently: it
is either decided by the policy the caller chose, or it is handed back for a
person to decide. Every decision is written into the log, which is what makes
the result auditable after the fact.

The engine never writes anything by itself. It builds a plan, and the plan is
applied through the methods of the layer that owns each side. That keeps every
write inside the layer that owns the data, and it means a dry run uses exactly
the same code path as the real thing.
"""

import datetime as dt
import json


def _now():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


#: The sync modes, as (key, label key, fallback, what it does).
SYNC_MODES = (
    ("upload", "sqlite_sync_upload", "Offline to online",
     "send the offline rows to the server"),
    ("download", "sqlite_sync_download", "Online to offline",
     "bring the offline rows down from the server"),
    ("bidirectional", "sqlite_sync_both", "Bidirectional",
     "reconcile both sides, newest write wins"),
    ("manual", "sqlite_sync_manual", "Manual (decide each change)",
     "report the differences and apply nothing"),
)

SYNC_MODE_KEYS = tuple(key for key, _lk, _f, _d in SYNC_MODES)
SYNC_MODE_LABELS = {key: fallback for key, _lk, fallback, _d in SYNC_MODES}

#: How the engine decides a difference it was not told how to handle.
CONFLICT_POLICIES = (
    ("newest", "sqlite_conflict_newest", "Newest write wins"),
    ("offline", "sqlite_conflict_offline", "Offline wins"),
    ("online", "sqlite_conflict_online", "Online wins"),
    ("ask", "sqlite_conflict_ask", "Ask for every change"),
)

CONFLICT_POLICY_KEYS = tuple(key for key, _lk, _f in CONFLICT_POLICIES)
CONFLICT_POLICY_LABELS = {key: fallback for key, _lk, fallback
                          in CONFLICT_POLICIES}

#: What a difference between the two sides can be.
DIFF_KINDS = ("added_offline", "added_online", "changed_both", "identical")

#: The differences that need a decision before anything is written.
CONFLICT_KINDS = ("changed_both",)


def ok(detail="", **extra):
    out = {"ok": True, "detail": detail}
    out.update(extra)
    return out


def fail(code, detail):
    return {"ok": False, "code": code, "detail": detail}


def row_key(row, key_column):
    """The value a row is identified by, as a string (never None)."""
    if row is None:
        return ""
    if key_column and key_column in row:
        value = row.get(key_column)
        # A key that is present but None reads as "" rather than as the text
        # "None", so the row is counted as unkeyed instead of colliding with
        # every other row that has no key.
        return "" if value is None else str(value)
    return ""


def _normalise(value):
    """One value in a form both sides write the same way.

    A number that arrived as 1 over JSON and as text from a text column must
    compare equal, or every row would look changed. A whole float is folded onto
    its integer, and every remaining scalar becomes a string, so the comparison
    never depends on how the value was transported.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value)
    if isinstance(value, int):
        return str(value)
    return str(value)


def row_signature(row, columns=None):
    """A stable, comparable signature of the contents of a row.

    The values are normalised and sorted by column, so a row that came back from
    JSON and the same row read from a database compare equal.
    """
    if row is None:
        return ""
    names = columns or sorted(row)
    return json.dumps({str(name): _normalise(row.get(name))
                       for name in names}, sort_keys=True)


def row_stamp(row):
    """A comparable timestamp for a row, or an empty string when it has none.

    Several spellings are accepted because the two sides are written by
    different programs, and a sync that cannot read the timestamp cannot decide
    which of two writes is newer.
    """
    if not isinstance(row, dict):
        return ""
    for name in ("modified", "updated_at", "updated", "changed", "date",
                 "timestamp", "mtime"):
        value = row.get(name)
        if value:
            return str(value)
    return ""


class SyncEngine(object):
    """Reconcile an offline database with an online one.

    The engine owns no data of its own: it holds a reference to each side and
    decides what should change. Every write it performs goes through the layer
    that owns that side, so a dry run and a real run share one code path.
    """

    def __init__(self, offline=None, online=None):
        self.offline = offline
        self.online = online
        self.policy = "newest"
        self.key_column = "id"
        self.table = ""
        self.log = []

    # --------------------------------------------------------------- setup
    def modes(self):
        return tuple(SYNC_MODES)

    def configure(self, table="", key_column=None, policy=None):
        """Point the engine at a table and choose how conflicts are handled."""
        changes = {}
        if table:
            self.table = table
            changes["table"] = table
        if key_column:
            self.key_column = key_column
            changes["key_column"] = key_column
        if policy:
            if policy not in CONFLICT_POLICY_KEYS:
                return fail("bad_policy", "policy must be one of %s"
                            % ", ".join(CONFLICT_POLICY_KEYS))
            self.policy = policy
            changes["policy"] = policy
        return ok("configured", **changes)

    def is_ready(self):
        """True when both sides are open enough to be compared."""
        problems = []
        if self.offline is None:
            problems.append("no offline database is configured")
        elif not self.offline.is_open():
            problems.append("no offline database is open")
        if self.online is None:
            problems.append("no online connection is configured")
        elif not self.online.is_signed_in():
            problems.append("the online session is not signed in")
        if not self.table:
            problems.append("no table has been chosen")
        if problems:
            return fail("not_ready", "; ".join(problems))
        return ok("ready")

    # -------------------------------------------------------------- reading
    def _offline_rows(self):
        if not self.offline or not self.table:
            return []
        if not self.offline.is_open():
            return []
        if self.table not in self.offline.list_tables():
            return []
        return self.offline.fetch_rows(self.table)

    def _online_rows(self):
        if not self.online or not self.table:
            return []
        if not self.online.is_signed_in():
            return []
        outcome = self.online.fetch_rows(self.table, limit=10000)
        if not outcome.get("ok"):
            return []
        return outcome.get("rows") or []

    def _infer_key(self, offline_rows, online_rows):
        """The column both sides carry, preferring the configured one."""
        sample = (offline_rows or online_rows or [{}])[0]
        if self.key_column and self.key_column in sample:
            return self.key_column
        for candidate in ("id", "key", "uuid", "name", "title"):
            if candidate in sample:
                return candidate
        return self.key_column

    # ------------------------------------------------------------- compare
    def compare(self):
        """Work out the difference between the two sides, writing nothing.

        Rows are matched by the key column. A row present on one side only is an
        addition; a row that differs on both sides is a conflict.
        """
        readiness = self.is_ready()
        if not readiness["ok"]:
            return readiness
        # Rows arrive from two different layers and are only usable when they
        # are mappings: a list or a scalar cannot be matched by a column, and
        # handing one to _classify would raise rather than report a difference.
        # Anything that is not a mapping is counted as unkeyed, which is the
        # bucket the report already has for rows it could not match.
        offline_raw = self._offline_rows()
        online_raw = self._online_rows()
        offline_rows = [row for row in offline_raw if isinstance(row, dict)]
        online_rows = [row for row in online_raw if isinstance(row, dict)]
        undictable = (len(offline_raw) - len(offline_rows)) + \
            (len(online_raw) - len(online_rows))
        key = self._infer_key(offline_rows, online_rows)

        left = {row_key(row, key): row for row in offline_rows}
        right = {row_key(row, key): row for row in online_rows}
        unkeyed = undictable + \
            len([row for row in offline_rows if not row_key(row, key)]) + \
            len([row for row in online_rows if not row_key(row, key)])

        differences = []
        for identifier in sorted(set(left) | set(right)):
            if not identifier:
                continue
            differences.append(self._classify(identifier, left.get(identifier),
                                              right.get(identifier)))

        summary = {}
        for item in differences:
            summary[item["kind"]] = summary.get(item["kind"], 0) + 1
        conflicts = [item for item in differences
                     if item["kind"] in CONFLICT_KINDS]
        return ok("%d difference(s), %d conflict(s)"
                  % (len(differences), len(conflicts)),
                  table=self.table, key=key, differences=differences,
                  conflicts=conflicts, summary=summary,
                  offline_rows=len(offline_rows), online_rows=len(online_rows),
                  unkeyed=unkeyed)

    @staticmethod
    def _classify(identifier, here, there):
        """One difference, classified."""
        item = {"key": identifier}
        if here is not None and there is None:
            item["kind"] = "added_offline"
            item["offline"] = here
            return item
        if here is None and there is not None:
            item["kind"] = "added_online"
            item["online"] = there
            return item
        columns = sorted(set(here) | set(there))
        if row_signature(here, columns) == row_signature(there, columns):
            item["kind"] = "identical"
            return item
        item["kind"] = "changed_both"
        item["offline"] = here
        item["online"] = there
        item["columns"] = [name for name in columns
                           if _normalise(here.get(name))
                           != _normalise(there.get(name))]
        item["offline_stamp"] = row_stamp(here)
        item["online_stamp"] = row_stamp(there)
        return item

    # ---------------------------------------------------------------- plan
    def plan(self, mode="bidirectional", policy=None):
        """What a sync in this mode would do, without doing any of it.

        Each step names the side it writes to and the action it takes, so the
        plan can be inspected and shown to a person before anything runs.
        """
        if mode not in SYNC_MODE_KEYS:
            return fail("bad_mode", "mode must be one of %s"
                        % ", ".join(SYNC_MODE_KEYS))
        chosen = policy or self.policy
        if chosen not in CONFLICT_POLICY_KEYS:
            return fail("bad_policy", "unknown conflict policy: %s" % chosen)
        compared = self.compare()
        if not compared["ok"]:
            return compared

        steps = []
        skipped = []
        for item in compared["differences"]:
            kind = item["kind"]
            if kind == "identical":
                continue
            if mode == "manual":
                skipped.append(self._skip(item, "manual mode applies nothing"))
                continue
            if mode == "upload":
                if kind in ("added_offline", "changed_both"):
                    steps.append(self._step("create", "online", item))
                else:
                    skipped.append(self._skip(item, "only the server has it"))
                continue
            if mode == "download":
                if kind in ("added_online", "changed_both"):
                    steps.append(self._step("create", "offline", item))
                else:
                    skipped.append(self._skip(item, "only this side has it"))
                continue
            # bidirectional
            if kind == "added_offline":
                steps.append(self._step("create", "online", item))
            elif kind == "added_online":
                steps.append(self._step("create", "offline", item))
            else:
                winner = self._winner(item, chosen)
                if winner is None:
                    skipped.append(self._skip(item, "needs a decision"))
                else:
                    steps.append(self._step("update", winner, item))
        return ok("%d step(s), %d skipped" % (len(steps), len(skipped)),
                  mode=mode, policy=chosen, table=self.table, steps=steps,
                  skipped=skipped, compared=compared)

    @staticmethod
    def _step(action, target, item):
        row = item.get("offline") if target == "online" else item.get("online")
        return {"action": action, "target": target, "key": item["key"],
                "kind": item["kind"], "row": row}

    @staticmethod
    def _skip(item, reason):
        return {"key": item["key"], "kind": item["kind"], "reason": reason}

    def _winner(self, item, policy):
        """Which side should win a conflicting row, or None when undecided."""
        if policy == "offline":
            return "offline"
        if policy == "online":
            return "online"
        if policy == "ask":
            return None
        here = item.get("offline_stamp") or ""
        there = item.get("online_stamp") or ""
        if here and there and here != there:
            return "offline" if here > there else "online"
        # With no usable timestamp, the local copy wins: it is the one the
        # person doing the sync can actually see.
        return "offline"

    # ----------------------------------------------------------------- run
    def run(self, mode="bidirectional", policy=None, decisions=None, limit=None):
        """Perform a sync and report exactly what happened.

        A step that fails is recorded and the sync carries on, so one bad row
        does not abandon the rest, and the outcome says which steps failed.
        """
        planned = self.plan(mode, policy)
        if not planned["ok"]:
            return planned
        decisions = decisions or {}
        applied = []
        failed = []
        steps = planned["steps"]
        if limit is not None:
            # A limit that cannot be read as a number is a bad argument, not a
            # crash: it is reported in the same shape every other guard here
            # uses, so the caller sees a failure rather than a traceback.
            try:
                steps = steps[:int(limit)]
            except (TypeError, ValueError):
                return fail("bad_limit", "limit must be a whole number: %r"
                            % (limit,))
        for step in steps:
            target = decisions.get(str(step["key"]), step["target"])
            outcome = self._apply(step["action"], target, step)
            record = {"key": step["key"], "action": step["action"],
                      "target": target, "kind": step["kind"],
                      "ok": bool(outcome.get("ok")),
                      "detail": outcome.get("detail", "")}
            if outcome.get("ok"):
                applied.append(record)
            else:
                failed.append(record)
        self._record("sync", "mode %s: %d applied, %d failed"
                     % (mode, len(applied), len(failed)))
        return ok("%d applied, %d failed" % (len(applied), len(failed)),
                  mode=mode, policy=planned["policy"], applied=applied,
                  failed=failed, skipped=planned["skipped"], table=self.table)

    def _apply(self, action, target, step):
        """One write, through the layer that owns that side."""
        if target == "online":
            return self._apply_online(action, step)
        return self._apply_offline(action, step)

    def _apply_online(self, action, step):
        if not self.online or not self.online.is_signed_in():
            return fail("no_session", "the online session is not open")
        if action == "create":
            return self.online.crud(self.table, "create", step.get("row"))
        return self.online.crud(self.table, "update", step.get("row"),
                                key=step["key"])

    def _apply_offline(self, action, step):
        if not self.offline or not self.offline.is_open():
            return fail("no_database", "no offline database is open")
        if self.offline.read_only:
            return fail("read_only", "the offline database is open read-only")
        key = self._offline_key()
        if action == "create":
            payload = {name: value for name, value in (step.get("row")
                                                       or {}).items()
                       if name != key}
            return self.offline.insert_row(self.table, payload)
        return self.offline.update_row(self.table, step["key"],
                                       step.get("row") or {}, key_column=key)

    def _offline_key(self):
        if self.offline and self.table:
            return self.offline.primary_key(self.table) or self.key_column
        return self.key_column

    # ------------------------------------------------------------- resolving
    def resolve(self, conflict, side):
        """Decide one conflict and apply it, for the manual mode.

        The decision is logged with the key it was made for, so a sync steered
        by hand can still be explained afterwards.
        """
        if side not in ("offline", "online", "skip"):
            return fail("bad_side", "side must be offline, online or skip")
        if side == "skip":
            self._record("resolve", "key %s skipped" % conflict.get("key"))
            return ok("skipped", key=conflict.get("key"))
        action = "create" if conflict["kind"] in ("added_offline",
                                                  "added_online") else "update"
        row = conflict.get(side)
        if row is None:
            return fail("no_row", "that side has no row for this key")
        outcome = self._apply(action, side, {"key": conflict.get("key"),
                                             "row": row})
        self._record("resolve", "key %s -> %s (%s)"
                     % (conflict.get("key"), side, outcome.get("detail", "")))
        return outcome

    # ------------------------------------------------------------ reporting
    def compare_rows(self, key):
        """Both sides of one record, side by side, for the diff panel."""
        compared = self.compare()
        if not compared["ok"]:
            return compared
        for item in compared["differences"]:
            if str(item.get("key")) == str(key):
                return ok("record %s" % key, item=item,
                          columns=sorted(set(item.get("offline") or {})
                                         | set(item.get("online") or {})))
        return fail("no_row", "no record with key %s" % key)

    def change_report(self, comparison=None):
        """The comparison as plain text, for the panel and for a report file."""
        compared = comparison or self.compare()
        if not compared.get("ok"):
            return "Sync comparison unavailable: %s\n" % compared.get(
                "detail", "")
        lines = ["ScriptDB sync report",
                 "generated: %s" % _now(),
                 "table: %s" % compared.get("table", ""),
                 "key: %s" % compared.get("key", ""),
                 "policy: %s" % CONFLICT_POLICY_LABELS.get(self.policy,
                                                           self.policy),
                 "",
                 "offline rows: %s" % compared.get("offline_rows", 0),
                 "online rows:  %s" % compared.get("online_rows", 0),
                 ""]
        summary = compared.get("summary") or {}
        listed = 0
        if summary:
            lines.append("Differences")
            for kind in DIFF_KINDS:
                if summary.get(kind):
                    lines.append("  %-18s %d" % (kind, summary[kind]))
            lines.append("")
        for item in compared.get("differences") or []:
            if item["kind"] == "identical":
                continue
            listed += 1
            lines.append("* %s  [%s]" % (item.get("key"), item["kind"]))
            for column in item.get("columns") or []:
                lines.append("    %-16s offline=%r  online=%r"
                             % (column,
                                (item.get("offline") or {}).get(column),
                                (item.get("online") or {}).get(column)))
        if compared.get("unkeyed"):
            lines += ["", "%d row(s) have no usable key and were not matched"
                      % compared["unkeyed"]]
        if not listed:
            lines.append("No differences: the two sides match.")
        lines += ["", compared.get("detail", "")]
        return "\n".join(lines) + "\n"

    def _record(self, action, detail):
        self.log.insert(0, {"time": _now(), "action": action, "detail": detail})
        del self.log[500:]
        return self.log[0]

    def log_rows(self, limit=200):
        return list(self.log[:limit])

    def describe(self):
        return {"table": self.table, "key_column": self.key_column,
                "policy": self.policy,
                "policy_label": CONFLICT_POLICY_LABELS.get(self.policy,
                                                           self.policy),
                "ready": self.is_ready()["ok"],
                "has_offline": bool(self.offline and self.offline.is_open()),
                "has_online": bool(self.online and self.online.is_signed_in())}


def capabilities():
    """The sync layer as a plugin-safe catalogue."""
    return {
        "modes": {key: SYNC_MODE_LABELS[key] for key in SYNC_MODE_KEYS},
        "policies": {key: CONFLICT_POLICY_LABELS[key]
                     for key in CONFLICT_POLICY_KEYS},
        "operations": ["configure", "compare", "plan", "run", "resolve",
                       "compare_rows", "change_report"],
        "status": "available",
    }
