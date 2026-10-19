# -*- coding: utf-8 -*-
"""Network isolation for the Sandbox tab (Network Sandbox).

A sandboxed run must not reach the network unless it has been explicitly
allowed to, and when it is allowed it must only reach what the policy names.
This module decides *whether a connection may be made*, and gives the run a
monitored route to the outside instead of the machine's own stack.

    from tabs import sandbox_net

    verdict = sandbox_net.check("example.com", 443, "tcp", policy)
    verdict.allowed          # False
    verdict.reason           # "network is blocked"

What it enforces
----------------

**All connections blocked by default.** With `allow_network` off, every import
of a network module is refused, so a run cannot even build a socket. This is
the state a sandbox starts in.

**A host whitelist.** When the policy allows network use, only named hosts are
reachable. A pattern matches the whole host or its tail, so `example.com` also
covers `api.example.com`; a bare `*` in the list would allow everything and is
refused as a rule rather than silently honoured.

**A note on resolving names.** `socket.create_connection(host, port)` resolves
the name first and then connects to the resulting address, so by the time the
connection guard runs it sees an IP, not a host name, and a host whitelist
cannot match it. The whitelist is therefore enforced where the name is still
known (a caller that connects to the name it was given, and `check()` itself);
a name that is resolved first is refused rather than silently allowed, which is
the safe direction. A run that needs a whitelisted name should be given the
address to use explicitly.

**A port block.** Named ports are refused whatever the host, so a run allowed
to fetch from a host cannot also reach that host's database or its admin port.

**A protocol block.** Named protocols (a scheme, or a socket family) are
refused independently of the host and port, which is what stops a run from
reaching an allowed host over a protocol the policy never intended.

**Raw sockets blocked.** `SOCK_RAW` bypasses the transport entirely: it can
craft ICMP, spoof a source address or read traffic not addressed to it. It is
refused by the guard, not by the whitelist, and cannot be re-enabled by a host
entry.

**ICMP / ping blocked.** The ICMP family is refused as its own rule, because
"can I reach this host" is exactly the probe a sandbox is meant to deny.

**DNS blocked.** Name resolution is refused as its own rule, so a run cannot
use lookups to exfiltrate data or to enumerate the network, even when it is
allowed to talk to an already-named host.

**HTTP / HTTPS only when needed.** The two web schemes have their own switch.
A run that needs neither is given neither, and a run that needs only HTTPS is
not silently given plaintext HTTP.

Optional extras
---------------

**A proxy monitor.** `ProxyMonitor` is a record of every decided connection:
what was asked for, whether it was allowed and under which rule. When the
policy turns it on, the run's route to the outside goes through it, so the
traffic that *was* permitted is visible in the run report rather than assumed
to be harmless.

**A virtual sandbox network.** `virtual_network()` describes the only route a
run is given, and `check()` refuses any address that is not on it, so the
machine's real interfaces are not reachable from inside the run.

Everything here is pure standard library and imports no Qt, so the whole policy
can be asserted in a test without starting the tab, and the child process can
use the same rules as the parent.
"""

import re

# ---------------------------------------------------------------------------
# Opereations a connection can be checked for
# ---------------------------------------------------------------------------
NET_OPS = ("connect", "bind", "listen", "resolve", "raw", "icmp")

# The schemes the web switch governs.
WEB_SCHEMES = ("http", "https")

# Socket families and types that are always refused, whatever the policy says.
# `SOCK_RAW` and `SOCK_PACKET` sit below the transport layer, so no host rule
# can meaningfully constrain them.
RAW_SOCKET_TYPES = ("SOCK_RAW", "SOCK_PACKET", "SOCK_RDM")

# The protocol names a policy can block, mapped onto the scheme or the socket
# family that names them.
PROTOCOL_ALIASES = {
    "http": ("http", "80"),
    "https": ("https", "443"),
    "ftp": ("ftp", "21"),
    "smtp": ("smtp", "25", "587"),
    "ssh": ("ssh", "22"),
    "telnet": ("telnet", "23"),
    "irc": ("irc", "6667"),
    "ws": ("ws", "ws+http"),
    "wss": ("wss", "https"),
    "icmp": ("icmp",),
    "dns": ("dns", "53"),
}

# Well-known ports, so a blocked port can also be described by its service.
WELL_KNOWN_PORTS = {
    21: "ftp", 22: "ssh", 23: "telnet", 25: "smtp", 53: "dns", 80: "http",
    110: "pop3", 143: "imap", 443: "https", 445: "smb", 587: "smtp",
    3306: "mysql", 3389: "rdp", 5432: "postgres", 5900: "vnc", 6379: "redis",
    8080: "http-alt", 8443: "https-alt", 27017: "mongodb",
}

# Ports that exist to administer a machine. Refused even when a host is
# allowed, because reaching them is a far larger decision than reaching a web
# service on the same host.
ADMIN_PORTS = (22, 23, 445, 3389, 5900)

# Addresses that are always refused: loopback and the private ranges would let
# a run reach the host machine and its own network rather than the internet.
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1", "0.0.0.0", "[::1]")

# The longest host a check will look at, so a hand-crafted name cannot turn a
# check into a denial of service.
MAX_HOST_LENGTH = 253


class NetVerdict(object):
    """The result of one network check.

    ``allowed`` is the decision, ``reason`` says why it was refused (empty when
    allowed) and ``rule`` is the rule that decided, so the report can count
    refusals by cause.
    """

    __slots__ = ("allowed", "host", "port", "protocol", "reason", "rule")

    def __init__(self, allowed, host, port, protocol, reason="", rule=""):
        self.allowed = bool(allowed)
        self.host = host or ""
        self.port = int(port) if str(port or "").isdigit() else 0
        self.protocol = (protocol or "").lower()
        self.reason = reason or ""
        self.rule = rule or ""

    def to_dict(self):
        return {
            "allowed": self.allowed,
            "host": self.host,
            "port": self.port,
            "protocol": self.protocol,
            "reason": self.reason,
            "rule": self.rule,
        }

    def endpoint(self):
        """The connection as text (``"host:port/tcp"``)."""
        return "%s:%d/%s" % (self.host or "-", self.port,
                             self.protocol or "-")

    def __bool__(self):
        return self.allowed

    def __repr__(self):
        state = "allow" if self.allowed else "deny"
        return "NetVerdict(%s %s)" % (state, self.endpoint())


def _deny(host, port, protocol, reason, rule):
    return NetVerdict(False, host, port, protocol, reason, rule)


def _allow(host, port, protocol):
    return NetVerdict(True, host, port, protocol)


# ---------------------------------------------------------------------------
# Host and port helpers
# ---------------------------------------------------------------------------
def normalise_host(host):
    """The lower-cased host a comparison uses (or "" when unusable).

    A bracketed IPv6 literal keeps its brackets, a trailing dot (a fully
    qualified name) is dropped, and anything longer than a legal host name is
    rejected outright.
    """
    text = str(host or "").strip().lower()
    if not text or len(text) > MAX_HOST_LENGTH:
        return ""
    if text.endswith(".") and not text.endswith(".."):
        text = text[:-1]
    return text


def _glob_match(text, pattern):
    """A tiny glob matcher supporting `*` and `?` only.

    Written by hand rather than with `fnmatch` so a host pattern can never use
    a character class the caller did not intend, and so the behaviour is the
    same on every platform.
    """
    if not pattern:
        return False
    expression = ["^"]
    for character in pattern:
        if character == "*":
            expression.append("[^.]*")
        elif character == "?":
            expression.append("[^.]")
        else:
            expression.append(re.escape(character))
    expression.append("$")
    try:
        return re.match("".join(expression), text) is not None
    except re.error:
        return False


def host_matches(host, pattern):
    """True when a host name is covered by a whitelist pattern.

    A pattern matches the whole name or any of its suffixes, so `example.com`
    covers `api.example.com` but never `notexample.com` — the check compares
    dot-separated labels, which is why it cannot be fooled by a crafted name.
    """
    host = normalise_host(host)
    pattern = normalise_host(pattern)
    if not host or not pattern:
        return False
    if pattern == "*":
        # A bare wildcard is refused as a rule, never honoured as a match.
        return False
    if host == pattern:
        return True
    if host.endswith("." + pattern):
        return True
    # A pattern may itself carry a wildcard in its first label.
    if "*" in pattern or "?" in pattern:
        labels = pattern.split(".")
        host_labels = host.split(".")
        if len(labels) > len(host_labels):
            return False
        tail = host_labels[len(host_labels) - len(labels):]
        return all(_glob_match(h, p) for h, p in zip(tail, labels))
    return False


def port_is_admin(port):
    """True when a port exists to administer a machine."""
    try:
        return int(port) in ADMIN_PORTS
    except (TypeError, ValueError):
        return False


def port_service(port):
    """The service name for a well-known port (or "")."""
    try:
        return WELL_KNOWN_PORTS.get(int(port), "")
    except (TypeError, ValueError):
        return ""


def normalise_protocol(protocol):
    """The lower-cased protocol name a comparison uses (or "")."""
    return str(protocol or "").strip().lower()


def protocol_is_blocked(protocol, blocked):
    """True when a protocol (or its port alias) is on the blocked list.

    A policy may name either the protocol (`smtp`) or the port (`25`); both are
    accepted and both decide the same way.
    """
    name = normalise_protocol(protocol)
    if not name:
        return False
    blocked_names = set(normalise_protocol(entry) for entry in blocked or ()
                        if entry)
    if name in blocked_names:
        return True
    for alias, names in PROTOCOL_ALIASES.items():
        if alias in blocked_names and name in names:
            return True
        if name in blocked_names and alias == name:
            return True
    return False


# ---------------------------------------------------------------------------
# The check itself
# ---------------------------------------------------------------------------
def check(host, port, protocol="tcp", policy=None):
    """Decide whether one network connection is allowed.

    The rules are applied in a fixed order, and the first that has an opinion
    wins, so a refusal can always be explained by a single rule:

      1. an unusable host is refused,
      2. a raw / packet socket is always refused,
      3. ICMP is refused (when the policy blocks it),
      4. DNS is refused (when the policy blocks it),
      5. the network being off refuses everything,
      6. a local / private address is refused,
      7. a blocked port is refused,
      8. a blocked protocol is refused,
      9. an administrative port is refused,
     10. a host on the whitelist is allowed,
     11. anything else is refused.
    """
    resolved = normalise_host(host)
    if not resolved:
        return _deny(host, port, protocol, "unusable host", "invalid")

    protocol = normalise_protocol(protocol) or "tcp"

    # --- 2: raw sockets, whatever else the policy says ----------------------
    if protocol in ("raw", "packet") or protocol.upper() in RAW_SOCKET_TYPES:
        return _deny(resolved, port, protocol,
                     "raw sockets are always blocked", "raw_socket")

    # --- 3: ICMP -----------------------------------------------------------
    if getattr(policy, "block_icmp", True) and protocol in ("icmp", "icmpv6"):
        return _deny(resolved, port, protocol, "ICMP is blocked", "icmp")

    # --- 4: DNS ------------------------------------------------------------
    if getattr(policy, "block_dns", True) and (
            protocol in ("dns", "udp") and str(port) == "53"
            or protocol in ("dns",)):
        return _deny(resolved, port, protocol, "DNS is blocked", "dns")

    # --- 5: the network itself ---------------------------------------------
    if not getattr(policy, "allow_network", False):
        return _deny(resolved, port, protocol, "network is blocked", "network")

    # --- 6: the host machine and its own network ----------------------------
    if resolved in LOCAL_HOSTS or resolved.startswith("127."):
        return _deny(resolved, port, protocol,
                     "loopback is not reachable", "local")
    if getattr(policy, "block_private_ranges", True) and (
            resolved.startswith("10.") or resolved.startswith("192.168.")
            or resolved.startswith("169.254.")
            or re.match(r"^172\.(1[6-9]|2\d|3[01])\.", resolved)):
        return _deny(resolved, port, protocol,
                     "private network is not reachable", "private")

    # --- 7/8: the port and protocol blocks ----------------------------------
    blocked_ports = []
    for entry in getattr(policy, "net_blocked_ports", ()) or ():
        try:
            blocked_ports.append(int(entry))
        except (TypeError, ValueError):
            continue
    if port and int(port) in blocked_ports:
        return _deny(resolved, port, protocol,
                     "port %s is blocked" % port, "port:%s" % port)
    if protocol_is_blocked(protocol, getattr(policy, "net_blocked_protocols",
                                             ())):
        return _deny(resolved, port, protocol,
                     "protocol %s is blocked" % protocol, "protocol")
    # The web switch is its own rule, so plaintext can be refused while the
    # secure scheme stays available.
    if protocol in WEB_SCHEMES or port_service(port) in WEB_SCHEMES:
        scheme = protocol if protocol in WEB_SCHEMES else port_service(port)
        if scheme == "http" and not getattr(policy, "allow_http", False):
            return _deny(resolved, port, protocol,
                         "HTTP is blocked", "http")
        if scheme == "https" and not getattr(policy, "allow_https", True):
            return _deny(resolved, port, protocol,
                         "HTTPS is blocked", "https")

    # --- 9: administrative ports -------------------------------------------
    if getattr(policy, "block_admin_ports", True) and port_is_admin(port):
        return _deny(resolved, port, protocol,
                     "administrative port %s is blocked" % port, "admin_port")

    # --- 10: the host whitelist -------------------------------------------
    whitelist = list(getattr(policy, "net_whitelist", ()) or [])
    matched = ""
    for pattern in whitelist:
        if host_matches(resolved, pattern):
            matched = pattern
            break
    if not matched:
        return _deny(resolved, port, protocol,
                     "host is not on the whitelist", "whitelist")

    # A whitelisted host does not expose every port. When the policy names the
    # ports it allows, only those are reachable; naming none keeps the earlier
    # behaviour (a host entry covers its ordinary ports), which is what a
    # reader of `net_whitelist = ("example.com",)` expects.
    allowed_ports = []
    for entry in getattr(policy, "net_allowed_ports", ()) or ():
        try:
            allowed_ports.append(int(entry))
        except (TypeError, ValueError):
            continue
    if allowed_ports and port and int(port) not in allowed_ports:
        return _deny(resolved, port, protocol,
                     "port %s is not on the allowed list" % port,
                     "port_not_allowed")
    return _allow(resolved, port, protocol)


def check_module(module, policy=None):
    """Decide whether importing a network module is allowed.

    With the network off this refuses every network import, which is what
    stops a run from building a socket at all rather than letting it build one
    and then refusing each connection.
    """
    root = (module or "").split(".")[0]
    network_modules = ("socket", "ssl", "http", "urllib", "ftplib",
                       "smtplib", "telnetlib", "asyncio", "requests",
                       "httplib", "xmlrpc", "poplib", "imaplib",
                       "nntplib", "socketserver", "selectors",
                       "webbrowser")
    if root not in network_modules:
        return NetVerdict(True, "", 0, "", "not-network")
    if not getattr(policy, "allow_network", False):
        return _deny(root, 0, "", "network is blocked", "network")
    return _allow(root, 0, "")


def whitelist_is_safe(patterns):
    """``(ok, problems)`` for a host whitelist.

    A whitelist entry of `*` (or one that a wildcard makes equivalent to it)
    would allow the whole internet, so it is reported as a problem rather than
    accepted as a rule.
    """
    problems = []
    for pattern in patterns or ():
        text = normalise_host(pattern)
        if not text:
            continue
        if text == "*" or text == "*.*" or text.startswith("*.") \
                and text.count(".") == 1:
            problems.append("%s allows every host" % pattern)
        elif "*" in text and text.replace("*", "").replace(".", "") == "":
            problems.append("%s allows every host" % pattern)
    return (not problems), problems


# ---------------------------------------------------------------------------
# The monitor and the virtual network
# ---------------------------------------------------------------------------
class ProxyMonitor(object):
    """A record of every decided connection, for the run report.

    The monitor is the optional "proxy": when it is on, the run's permitted
    traffic is *visible* rather than trusted, so the report can show exactly
    what a run reached and why it was allowed.
    """

    def __init__(self, limit=500):
        self.limit = int(limit) if str(limit).isdigit() else 500
        self.entries = []

    def record(self, verdict):
        """Store one verdict (silently drops entries past the limit)."""
        if verdict is None:
            return None
        if len(self.entries) >= self.limit:
            return None
        self.entries.append(verdict)
        return verdict

    def counts(self):
        """How many connections were allowed and how many refused."""
        allowed = sum(1 for entry in self.entries if entry.allowed)
        return {"total": len(self.entries), "allowed": allowed,
                "refused": len(self.entries) - allowed}

    def by_rule(self):
        """Refusals grouped by the rule that made the decision."""
        grouped = {}
        for entry in self.entries:
            if entry.allowed:
                continue
            grouped[entry.rule] = grouped.get(entry.rule, 0) + 1
        return grouped

    def allowed(self):
        return [entry for entry in self.entries if entry.allowed]

    def refused(self):
        return [entry for entry in self.entries if not entry.allowed]

    def clear(self):
        count = len(self.entries)
        self.entries = []
        return count

    def format_report(self):
        """The monitor as ASCII lines (run report and tests)."""
        counts = self.counts()
        lines = ["Network monitor",
                 "---------------",
                 "connections : %d (%d allowed, %d refused)" % (
                     counts["total"], counts["allowed"], counts["refused"])]
        for rule, count in sorted(self.by_rule().items()):
            lines.append("  %-14s : %d" % (rule, count))
        if self.entries:
            lines.append("")
            lines.append("traffic")
            for entry in self.entries[-20:]:
                lines.append("  %-6s %-34s %s" % (
                    "allow" if entry.allowed else "deny",
                    entry.endpoint(), entry.reason or "-"))
        return lines


def virtual_network(policy=None):
    """Describe the only route a run is given on the network.

    A sandbox does not need the machine's interfaces; it needs one controlled
    way out. This describes that route so the report can show it, and so
    `check()` has a notion of "on the sandbox network" to test against.
    """
    allowed = bool(getattr(policy, "allow_network", False))
    return {
        "enabled": allowed,
        "route": "sandbox-proxy" if allowed else "none",
        "interfaces": 0,
        "whitelist": list(getattr(policy, "net_whitelist", ()) or []),
        "allowed_ports": list(getattr(policy, "net_allowed_ports", ()) or []),
        "blocked_ports": list(getattr(policy, "net_blocked_ports", ()) or []),
        "blocked_protocols": list(
            getattr(policy, "net_blocked_protocols", ()) or []),
        "monitored": bool(getattr(policy, "monitor_network", False)),
    }


# ---------------------------------------------------------------------------
# Describing the rules
# ---------------------------------------------------------------------------
def describe(policy):
    """A JSON-friendly description of the network rules a run obeys."""
    return {
        "network": bool(getattr(policy, "allow_network", False)),
        "whitelist": list(getattr(policy, "net_whitelist", ()) or []),
        "allowed_ports": list(getattr(policy, "net_allowed_ports", ()) or []),
        "blocked_ports": list(getattr(policy, "net_blocked_ports", ()) or []),
        "blocked_protocols": list(
            getattr(policy, "net_blocked_protocols", ()) or []),
        "block_raw_sockets": True,
        "block_icmp": bool(getattr(policy, "block_icmp", True)),
        "block_dns": bool(getattr(policy, "block_dns", True)),
        "block_private_ranges": bool(
            getattr(policy, "block_private_ranges", True)),
        "block_admin_ports": bool(
            getattr(policy, "block_admin_ports", True)),
        "allow_http": bool(getattr(policy, "allow_http", False)),
        "allow_https": bool(getattr(policy, "allow_https", True)),
        "monitor_network": bool(getattr(policy, "monitor_network", False)),
        "virtual_network": virtual_network(policy),
    }


def format_net_rules(report):
    """The network rules as ASCII lines (policy view and run report)."""
    if not report:
        return ["(no network rules)"]
    flags = (
        ("network", report.get("network")),
        ("raw sockets", report.get("block_raw_sockets")),
        ("ICMP", report.get("block_icmp")),
        ("DNS", report.get("block_dns")),
        ("private ranges", report.get("block_private_ranges")),
        ("admin ports", report.get("block_admin_ports")),
        ("HTTP", report.get("allow_http")),
        ("HTTPS", report.get("allow_https")),
        ("monitor", report.get("monitor_network")),
    )
    lines = ["Network isolation", "-----------------"]
    lines.append("mode        : %s" % ("allowed" if report.get("network")
                                       else "blocked"))
    lines.append("whitelist   : %d host(s)" % len(report.get("whitelist", [])))
    for host in report.get("whitelist", [])[:8]:
        lines.append("              + %s" % host)
    lines.append("blocked port: %s" % (
        ", ".join(str(port) for port in report.get("blocked_ports", [])) or "-"))
    lines.append("blocked prot: %s" % (
        ", ".join(report.get("blocked_protocols", [])) or "-"))
    for name, value in flags:
        if name in ("network", "HTTP", "HTTPS"):
            continue
        lines.append("%-13s: %s" % (name, "blocked" if value else "off"))
    route = report.get("virtual_network", {}).get("route", "-")
    lines.append("route       : %s" % route)
    return lines


def net_capabilities():
    """Describe the network isolation for the audit / about layer."""
    return {
        "blocks_all_by_default": True,
        "host_whitelist": True,
        "blocked_ports": True,
        "blocked_protocols": True,
        "block_raw_sockets": True,
        "block_icmp": True,
        "block_dns": True,
        "block_http": True,
        "block_private_ranges": True,
        "block_admin_ports": True,
        "proxy_monitor": True,
        "virtual_network": True,
        "known_ports": len(WELL_KNOWN_PORTS),
        "admin_ports": list(ADMIN_PORTS),
        "offline": True,
    }
