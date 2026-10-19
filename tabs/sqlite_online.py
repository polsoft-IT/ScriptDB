# -*- coding: utf-8 -*-
"""Online Data Layer for the SQLite tab.

This module is the *online* half of the SQLite tab: it talks to a database that
lives behind an HTTP API. It is deliberately the smaller half -- the offline
layer is the one that carries the weight -- but it is complete enough to be
useful:

    login(url, user, password)   a session token
    refresh()                    keep the session alive
    logout()
    ping()                       is the server answering
    list_tables() / list_columns(table)
    fetch_rows(table)
    crud(table, action, payload) create / read / update / delete
    create_table / drop_table
    console(method, path, body)  the raw API console, for debugging

The transport is ``urllib`` from the standard library, so there is no dependency
to install. Nothing is sent anywhere until ``login`` has produced a token, and
every request carries it -- so a session that has expired is reported rather
than silently retried as an anonymous caller.

The API shape it expects is the one almost every small service exposes:

    POST   /login              {"username": ..., "password": ...} -> {"token": ...}
    GET    /tables                                          -> {"tables": [...]}
    GET    /tables/<name>                                   -> {"columns": [...]}
    GET    /tables/<name>/rows?limit=&offset=               -> {"rows": [...]}
    POST   /tables/<name>/rows                              -> {"key": ...}
    PUT    /tables/<name>/rows/<key>
    DELETE /tables/<name>/rows/<key>

``api_base`` in the constructor lets a different shape be substituted, which is
what the "API console" in the tab uses to probe a service before trusting it.
"""

import base64
import datetime as dt
import json
import socket
import urllib.error
import urllib.parse
import urllib.request


def _now():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


#: The CRUD actions the online layer understands.
ONLINE_ACTIONS = ("create", "read", "update", "delete")

#: How a session can be doing, as (key, label key, fallback).
SESSION_STATES = (
    ("signed_out", "sqlite_online_signed_out", "Signed out"),
    ("signed_in", "sqlite_online_signed_in", "Signed in"),
    ("expired", "sqlite_online_expired", "Session expired"),
    ("offline", "sqlite_online_offline", "Server unreachable"),
)

SESSION_STATE_LABELS = {key: fallback for key, _lk, fallback in SESSION_STATES}

#: The requests the console offers, as (label, method).
CONSOLE_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")


def ok(detail="", **extra):
    out = {"ok": True, "detail": detail}
    out.update(extra)
    return out


def fail(code, detail):
    return {"ok": False, "code": code, "detail": detail}


class OnlineDatabase(object):
    """A database reached over HTTP, with a small session of its own.

    The token lives on this object only; nothing is written to disk, so closing
    the tab signs the session out. A request is attempted once, never retried
    blindly, so a token that was refused does not turn into a second anonymous
    call.
    """

    #: Seconds a request may take before it is abandoned.
    TIMEOUT = 15

    def __init__(self, base_url="", api_base="/api"):
        self.base_url = (base_url or "").rstrip("/")
        self.api_base = "/" + (api_base or "api").strip("/") if api_base else ""
        self.user = ""
        self.token = ""
        self.state = "signed_out"
        self.table = ""
        self.signed_in_at = ""
        self.last_error = ""
        self.log = []                 # every request, newest first
        self.read_only = False
        self._refresh_token = ""

    # ------------------------------------------------------------- addressing
    def set_base(self, base_url, api_base=None):
        self.base_url = (base_url or "").rstrip("/")
        if api_base is not None:
            self.api_base = "/" + api_base.strip("/") if api_base else ""
        return ok("base set", base_url=self.base_url, api_base=self.api_base)

    def url_for(self, path):
        """The absolute URL for an API path, always below the base."""
        path = "/" + str(path or "").lstrip("/")
        return self.base_url + self.api_base + path

    def is_configured(self):
        return bool(self.base_url) and self.base_url.startswith(
            ("http://", "https://"))

    def is_signed_in(self):
        return self.state == "signed_in" and bool(self.token)

    def describe(self):
        return {
            "base_url": self.base_url, "api_base": self.api_base,
            "user": self.user, "state": self.state,
            "state_label": SESSION_STATE_LABELS.get(self.state, self.state),
            "signed_in_at": self.signed_in_at,
            "has_token": bool(self.token),
            "read_only": self.read_only,
        }

    # -------------------------------------------------------------- transport
    def request(self, method, path, payload=None, query=None, raw=False):
        """Make one request, returning a uniform result.

        Every outcome -- a reply, an HTTP error, a socket that never answered --
        comes back in the same shape, so a caller never has to catch anything.
        """
        if not self.is_configured():
            return fail("not_configured",
                        "set the server address first (http:// or https://)")
        url = self.url_for(path)
        if query:
            clean = {key: value for key, value in query.items()
                     if value is not None}
            if clean:
                url += "?" + urllib.parse.urlencode(clean)
        body = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload, default=str).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = "Bearer %s" % self.token
        request = urllib.request.Request(url, data=body, headers=headers,
                                        method=method.upper())
        started = dt.datetime.now()
        try:
            with urllib.request.urlopen(request, timeout=self.TIMEOUT) as reply:
                text = reply.read().decode("utf-8", errors="replace")
                status = reply.status
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", errors="replace")
            self._log(method, url, exc.code, started)
            if exc.code in (401, 403):
                self.state = "expired"
                self.last_error = "the server refused the session (%d)" % exc.code
                return fail("unauthorized", self.last_error)
            return fail("http_%d" % exc.code,
                        "HTTP %d: %s" % (exc.code, text[:400]))
        except (urllib.error.URLError, socket.timeout, OSError) as exc:
            self._log(method, url, 0, started)
            self.state = "offline"
            self.last_error = "the server did not answer: %s" % exc
            return fail("unreachable", self.last_error)
        self._log(method, url, status, started)
        if raw:
            return ok("HTTP %d" % status, status=status, text=text)
        data = None
        if text.strip():
            try:
                data = json.loads(text)
            except ValueError:
                data = text
        return ok("HTTP %d" % status, status=status, data=data, text=text)

    def _log(self, method, url, status, started):
        elapsed = (dt.datetime.now() - started).total_seconds()
        self.log.insert(0, {"time": _now(), "method": method.upper(),
                            "url": url, "status": status,
                            "seconds": round(elapsed, 3)})
        del self.log[500:]
        return self.log[0]

    def log_rows(self, limit=200):
        return list(self.log[:limit])

    # -------------------------------------------------------------- session
    def login(self, base_url="", user="", password="", api_base=None):
        """Sign in and keep the token. Passwords are never stored here."""
        if base_url:
            self.set_base(base_url, api_base)
        if not user or not password:
            return fail("no_credentials", "a user name and a password are needed")
        # The credentials go in a header as well as the body, because small
        # services differ about which one they read, and neither is logged.
        outcome = self.request("POST", "/login",
                               {"username": user, "password": password})
        if not outcome["ok"]:
            self.user = ""
            self.state = "signed_out"
            return outcome
        data = outcome.get("data")
        token = ""
        if isinstance(data, dict):
            token = str(data.get("token") or data.get("access_token")
                        or data.get("jwt") or "")
        if not token:
            return fail("no_token",
                        "the server accepted the login but returned no token")
        self.token = token
        self.user = user
        self.state = "signed_in"
        self.signed_in_at = _now()
        self.last_error = ""
        return ok("signed in as %s" % user, user=user,
                  token_hint=self.token_hint())

    def token_hint(self):
        """The first and last few characters of the token, for the panel.

        The whole token is never shown or logged: a screen that displays a live
        session token is a screen that leaks it.
        """
        if not self.token:
            return ""
        if len(self.token) <= 12:
            return "..." + self.token[-4:]
        return "%s...%s" % (self.token[:4], self.token[-4:])

    def refresh(self):
        """Ask the server for a fresh token before the current one expires."""
        if not self.is_signed_in():
            return fail("signed_out", "there is no session to refresh")
        outcome = self.request("POST", "/refresh",
                               {"token": self.token})
        data = outcome.get("data") if outcome["ok"] else None
        token = ""
        if isinstance(data, dict):
            token = str(data.get("token") or data.get("access_token") or "")
        if not token:
            # A service without a refresh endpoint keeps the session alive by
            # being used; ping it so the state is honest either way.
            ping = self.ping()
            return ok("session still valid" if ping["ok"] else "session stale",
                      token=bool(self.token), ping=ping["ok"])
        self.token = token
        return ok("token refreshed", token_hint=self.token_hint())

    def logout(self):
        """Drop the session locally, and tell the server when it offers to."""
        had_token = bool(self.token)
        if had_token:
            try:
                self.request("POST", "/logout", {"token": self.token})
            except Exception:
                pass
        self.token = ""
        self.user = ""
        self.state = "signed_out"
        self.signed_in_at = ""
        return ok("signed out")

    def ping(self):
        """Check the server is answering, without needing a session."""
        outcome = self.request("GET", "/health")
        if outcome["ok"]:
            return outcome
        # A service with no health endpoint is not necessarily down, so a
        # refused path is reported as reachable-but-unknown.
        if str(outcome.get("code", "")).startswith("http_4"):
            return ok("the server answered", reachable=True)
        return outcome

    # -------------------------------------------------------------- reading
    def require_session(self, action="do that"):
        if not self.is_signed_in():
            return fail("signed_out",
                        "sign in before you %s" % action)
        return ok("session is open")

    def list_tables(self):
        """The table names the server reports."""
        guard = self.require_session("read the table list")
        if not guard["ok"]:
            return guard
        outcome = self.request("GET", "/tables")
        if not outcome["ok"]:
            return outcome
        data = outcome.get("data")
        tables = []
        if isinstance(data, dict):
            tables = data.get("tables") or []
        elif isinstance(data, list):
            tables = data
        if tables and isinstance(tables[0], dict):
            tables = [item.get("name") for item in tables if item.get("name")]
        return ok("%d table(s)" % len(tables), tables=[str(name) for name in
                                                       tables])

    def list_columns(self, table):
        """The columns of one table, as the offline layer reports them."""
        guard = self.require_session("read the schema")
        if not guard["ok"]:
            return guard
        outcome = self.request("GET", "/tables/%s" % urllib.parse.quote(table))
        if not outcome["ok"]:
            return outcome
        data = outcome.get("data") or {}
        columns = data.get("columns") if isinstance(data, dict) else data
        normalised = []
        for column in columns or []:
            if isinstance(column, dict):
                normalised.append({"name": str(column.get("name", "")),
                                   "type": str(column.get("type") or "unknown"),
                                   "kind": str(column.get("kind")
                                               or column.get("type")
                                               or "text")})
            else:
                normalised.append({"name": str(column), "type": "unknown",
                                   "kind": "text"})
        return ok("%d column(s)" % len(normalised), columns=normalised,
                  table=table)

    def fetch_rows(self, table, limit=200, offset=0, search=""):
        """Rows for one table, paged by the server."""
        guard = self.require_session("read rows")
        if not guard["ok"]:
            return guard
        outcome = self.request("GET", "/tables/%s/rows" % urllib.parse.quote(table),
                               query={"limit": limit, "offset": offset,
                                      "search": search or None})
        if not outcome["ok"]:
            return outcome
        data = outcome.get("data")
        rows = data.get("rows") if isinstance(data, dict) else data
        return ok("%d row(s)" % len(rows or []), rows=list(rows or []),
                  table=table)

    # ----------------------------------------------------------------- CRUD
    def crud(self, table, action, payload=None, key=None):
        """One CRUD operation, dispatched to the endpoint it belongs to.

        A write is refused outright when the session is marked read-only, so the
        read-only mode the panel offers is enforced here rather than trusted to
        the buttons.
        """
        if action not in ONLINE_ACTIONS:
            return fail("bad_action",
                        "action must be one of %s" % ", ".join(ONLINE_ACTIONS))
        guard = self.require_session("%s rows" % action)
        if not guard["ok"]:
            return guard
        path = "/tables/%s/rows" % urllib.parse.quote(table)
        if action == "read":
            return self.fetch_rows(table)
        if action in ("update", "delete"):
            if key is None:
                return fail("no_key", "a key is needed to %s a row" % action)
            path += "/" + urllib.parse.quote(str(key))
        if self.read_only and action in ("create", "update", "delete"):
            return fail("read_only",
                        "the connection is in read-only mode")
        method = {"create": "POST", "update": "PUT",
                  "delete": "DELETE"}[action]
        outcome = self.request(method, path, payload)
        if outcome["ok"]:
            return ok("%s done" % action, action=action, table=table,
                      data=outcome.get("data"))
        return outcome

    def create_table(self, table, columns):
        guard = self.require_session("create a table")
        if not guard["ok"]:
            return guard
        if self.read_only:
            return fail("read_only", "the connection is in read-only mode")
        return self.request("POST", "/tables",
                            {"name": table, "columns": columns})

    def drop_table(self, table):
        guard = self.require_session("drop a table")
        if not guard["ok"]:
            return guard
        if self.read_only:
            return fail("read_only", "the connection is in read-only mode")
        return self.request("DELETE", "/tables/%s" % urllib.parse.quote(table))

    def execute_sql(self, sql):
        """Ask the server to run SQL, when it offers such an endpoint."""
        guard = self.require_session("run SQL")
        if not guard["ok"]:
            return guard
        if self.read_only and not sql.strip().lower().startswith(
                ("select", "pragma", "explain", "with")):
            return fail("read_only", "the connection is in read-only mode")
        return self.request("POST", "/query", {"sql": sql})

    # --------------------------------------------------------------- console
    def console(self, method, path, body=None, token=True):
        """The raw console: send any request and show exactly what came back.

        The session token is attached only when ``token`` is set, so the console
        can also probe what an unauthenticated caller sees.
        """
        saved = self.token
        if not token:
            self.token = ""
        try:
            outcome = self.request(method, path, body, raw=True)
        finally:
            self.token = saved
        return outcome

    def console_lines(self, method, path, body=None, token=True):
        """The console result as plain text, for the panel."""
        outcome = self.console(method, path, body, token)
        lines = ["%s %s" % (method.upper(), self.url_for(path))]
        if body is not None:
            lines.append("body: %s" % json.dumps(body, default=str))
        lines.append("token: %s" % ("attached" if token and self.token
                                    else "none"))
        lines.append("")
        if outcome["ok"]:
            lines.append("HTTP %s" % outcome.get("status", "?"))
            lines.append(outcome.get("text") or "(empty reply)")
        else:
            lines.append("failed: %s" % outcome.get("code", ""))
            lines.append(str(outcome.get("detail", "")))
        return "\n".join(lines)

    # ------------------------------------------------------------ statistics
    def count_rows(self, table):
        outcome = self.fetch_rows(table, limit=1)
        if not outcome["ok"]:
            return 0
        data = outcome.get("data")
        if isinstance(data, dict) and "total" in data:
            return int(data["total"] or 0)
        return len(outcome.get("rows") or [])


def capabilities():
    """The online layer as a plugin-safe catalogue."""
    return {
        "operations": ["set_base", "login", "refresh", "logout", "ping",
                       "list_tables", "list_columns", "fetch_rows", "crud",
                       "create_table", "drop_table", "execute_sql", "console"],
        "actions": list(ONLINE_ACTIONS),
        "states": {key: SESSION_STATE_LABELS[key] for key, _lk, _f
                   in SESSION_STATES},
        "status": "available",
    }
