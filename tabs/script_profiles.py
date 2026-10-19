# -*- coding: utf-8 -*-
"""Script Profiles for ScriptDB.

A profile is a *shape*, not a template. A template in ``tabs/templates.py``
gives you one fixed block of text; a profile describes how a whole script of a
given kind is put together, and the engine writes the file out from that
description:

    structure    shebang, module docstring, the section order, the entry point
    sections     labelled regions, so the file can be read at a glance
    imports      the imports that kind of script actually needs
    functions    the functions it is expected to define, with their docstrings
    startup      the logic that runs when the file is executed

Seven profiles are offered:

    cli          a command-line tool with arguments and a main()
    service      a long-running service with a loop, signals and logging
    automation   a scheduled job with steps, retries and a report
    test         a test module with cases and a runner
    utility      a small helper module of pure functions
    module       an importable package module with a public surface
    api          a client for one HTTP API, with request helpers

The engine is deliberately standard library only and ASCII safe, and it never
executes anything. ``generate(profile, name, **options)`` returns the same shape
the code generator does, so the editor, the reports and the CLI all read it the
same way. A profile registered at run time shows up in ``profiles()``, which is
what makes the feature hot-pluggable without touching the editor.
"""

import datetime as dt
import re


def _now():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# The catalogue
# ---------------------------------------------------------------------------
#: The profile keys, in menu order.
PROFILE_KEYS_ORDER = ("cli", "service", "automation", "test", "utility",
                      "module", "api")

#: Every profile, as (key, label key, fallback, what it is for).
SCRIPT_PROFILES = (
    ("cli", "profile_cli", "Command-line script (CLI)",
     "a tool run by hand, with arguments and an exit code"),
    ("service", "profile_service", "Service script",
     "a long-running loop with logging and a clean shutdown"),
    ("automation", "profile_automation", "Automation script",
     "a scheduled job built from steps that can be retried"),
    ("test", "profile_test", "Test script",
     "a test module with cases and a plain runner"),
    ("utility", "profile_utility", "Utility script",
     "a small library of pure helper functions"),
    ("module", "profile_module", "Module script",
     "an importable module with an explicit public surface"),
    ("api", "profile_api", "API script",
     "a client for one HTTP API with request helpers"),
)

PROFILE_KEYS = tuple(key for key, _lk, _f, _d in SCRIPT_PROFILES)
PROFILE_LABELS = {key: fallback for key, _lk, fallback, _d in SCRIPT_PROFILES}
PROFILE_MEANINGS = {key: detail for key, _lk, _f, detail in SCRIPT_PROFILES}

#: The sections every profile writes, in order.  A profile lists the subset it
#: uses; the engine emits them as labelled regions so the file reads as a map.
SECTIONS = ("imports", "constants", "classes", "functions", "main")

SECTION_LABELS = {
    "imports": "Imports",
    "constants": "Configuration",
    "classes": "Classes",
    "functions": "Functions",
    "main": "Entry point",
}


def _banner(title):
    """A section banner, drawn with plain ASCII."""
    return ["# " + "-" * 74, "# %s" % title, "# " + "-" * 74]


def identifier(text, fallback="script"):
    """A printable-ASCII Python identifier from arbitrary text."""
    text = re.sub(r"[^0-9A-Za-z]+", "_", str(text or "")).strip("_").lower()
    if not text:
        return fallback
    if text[0].isdigit():
        text = "item_" + text
    return text


def class_name(text, fallback="Thing"):
    """``"user client"`` -> ``"UserClient"``."""
    pieces = [piece for piece in identifier(text, fallback.lower()).split("_")
              if piece]
    if not pieces:
        return fallback
    return "".join(piece[:1].upper() + piece[1:] for piece in pieces)


def title_case(text, fallback="Script"):
    """The name as it reads in a sentence: ``"my tool"`` -> ``"My Tool"``."""
    pieces = [piece for piece in re.split(r"[^0-9A-Za-z]+", str(text or ""))
              if piece]
    if not pieces:
        return fallback
    return " ".join(piece[:1].upper() + piece[1:] for piece in pieces)
# ---------------------------------------------------------------------------
# The profiles themselves
# ---------------------------------------------------------------------------
# Each builder returns the pieces of one script.  Keeping them as data (lists
# of lines) rather than one big string is what lets the engine assemble the
# sections in a stable order and lets a caller inspect or replace any part.
def _profile_cli(name, title, description, author):
    """A command-line tool: arguments, a main() and an exit code."""
    return {
        "sections": ("imports", "constants", "functions", "main"),
        "imports": [
            "import argparse",
            "import sys",
        ],
        "constants": [
            "PROGRAM = %r" % name,
            "VERSION = %r" % "0.1.0",
            "EXIT_OK = 0",
            "EXIT_ERROR = 1",
        ],
        "functions": [
            {
                "name": "build_parser",
                "args": "",
                "docstring": "Build the argument parser for this tool.",
                "body": [
                    "parser = argparse.ArgumentParser(",
                    "    prog=PROGRAM,",
                    "    description=%r)" % description,
                    "parser.add_argument('target', nargs='?', default='.',",
                    "                    help='what to work on')",
                    "parser.add_argument('--verbose', action='store_true',",
                    "                    help='print more detail')",
                    "parser.add_argument('--version', action='version',",
                    "                    version='%(prog)s ' + VERSION)",
                    "return parser",
                ],
            },
            {
                "name": "run",
                "args": "arguments",
                "docstring": "Do the work. Return an exit code.",
                "body": [
                    "if arguments.verbose:",
                    "    print('%s %s on %s' % (PROGRAM, VERSION,",
                    "                           arguments.target))",
                    "print('nothing to do yet')",
                    "return EXIT_OK",
                ],
            },
        ],
        "main": [
            "def main(argv=None):",
            '    """The entry point: parse the arguments and run."""',
            "    parser = build_parser()",
            "    arguments = parser.parse_args(argv)",
            "    try:",
            "        return run(arguments)",
            "    except KeyboardInterrupt:",
            "        print('interrupted', file=sys.stderr)",
            "        return EXIT_ERROR",
            "",
            "",
            "if __name__ == '__main__':",
            "    sys.exit(main())",
        ],
    }


def _profile_service(name, title, description, author):
    """A long-running service: a loop, logging and a clean shutdown."""
    return {
        "sections": ("imports", "constants", "classes", "main"),
        "imports": [
            "import logging",
            "import signal",
            "import time",
        ],
        "constants": [
            "SERVICE_NAME = %r" % name,
            "POLL_SECONDS = 5",
        ],
        "classes": [
            {
                "name": class_name(name, "Service"),
                "docstring": "The %s service." % title,
                "methods": [
                    {
                        "name": "__init__",
                        "args": "self, poll_seconds=POLL_SECONDS",
                        "body": [
                            "self.poll_seconds = poll_seconds",
                            "self.running = False",
                        ],
                    },
                    {
                        "name": "start",
                        "args": "self",
                        "docstring": "Prepare whatever the loop needs.",
                        "body": [
                            "logging.info('%s starting', SERVICE_NAME)",
                            "self.running = True",
                        ],
                    },
                    {
                        "name": "tick",
                        "args": "self",
                        "docstring": "One unit of work. Runs every poll.",
                        "body": [
                            "# Replace this with the work the service does.",
                            "logging.debug('%s tick', SERVICE_NAME)",
                        ],
                    },
                    {
                        "name": "stop",
                        "args": "self, *_signal",
                        "docstring": "Ask the loop to stop.",
                        "body": [
                            "logging.info('%s stopping', SERVICE_NAME)",
                            "self.running = False",
                        ],
                    },
                    {
                        "name": "run",
                        "args": "self",
                        "docstring": "Run until stop() is called.",
                        "body": [
                            "self.start()",
                            "while self.running:",
                            "    try:",
                            "        self.tick()",
                            "    except Exception:",
                            "        logging.exception('the tick failed')",
                            "    time.sleep(self.poll_seconds)",
                            "logging.info('%s stopped', SERVICE_NAME)",
                        ],
                    },
                ],
            },
        ],
        "main": [
            "def main():",
            '    """The entry point: install the signal handlers and run."""',
            "    logging.basicConfig(",
            "        level=logging.INFO,",
            "        format='%(asctime)s %(levelname)s %(message)s')",
            "    service = %s()" % class_name(name, "Service"),
            "    signal.signal(signal.SIGINT, service.stop)",
            "    signal.signal(signal.SIGTERM, service.stop)",
            "    service.run()",
            "    return 0",
            "",
            "",
            "if __name__ == '__main__':",
            "    raise SystemExit(main())",
        ],
    }


def _profile_automation(name, title, description, author):
    """A scheduled job: named steps, each of which may be retried."""
    return {
        "sections": ("imports", "constants", "functions", "main"),
        "imports": [
            "import datetime as dt",
            "import logging",
            "import time",
        ],
        "constants": [
            "JOB_NAME = %r" % name,
            "MAX_ATTEMPTS = 3",
            "RETRY_SECONDS = 2",
        ],
        "functions": [
            {
                "name": "step_one",
                "args": "context",
                "docstring": "The first step. Add one function per step.",
                "body": [
                    "context['step_one'] = dt.datetime.now().isoformat()",
                    "return context",
                ],
            },
            {
                "name": "step_two",
                "args": "context",
                "docstring": "The second step, which uses the first.",
                "body": [
                    "if 'step_one' not in context:",
                    "    raise RuntimeError('step_one must run first')",
                    "context['step_two'] = 'done'",
                    "return context",
                ],
            },
            {
                "name": "run_step",
                "args": "step, context, attempts=MAX_ATTEMPTS",
                "docstring": "Run one step, retrying it on failure.",
                "body": [
                    "for attempt in range(1, attempts + 1):",
                    "    try:",
                    "        return step(context)",
                    "    except Exception as exc:",
                    "        logging.warning('%s failed (%d/%d): %s',",
                    "                        step.__name__, attempt, attempts, exc)",
                    "        if attempt < attempts:",
                    "            time.sleep(RETRY_SECONDS)",
                    "raise RuntimeError('%s failed every attempt' % step.__name__)",
                ],
            },
            {
                "name": "report",
                "args": "context",
                "docstring": "A short summary of what the job did.",
                "body": [
                    "lines = ['%s report' % JOB_NAME]",
                    "for key in sorted(context):",
                    "    lines.append('  %s: %s' % (key, context[key]))",
                    "return '\\n'.join(lines)",
                ],
            },
        ],
        "main": [
            "STEPS = (step_one, step_two)",
            "",
            "",
            "def main():",
            '    """The entry point: run every step in order."""',
            "    logging.basicConfig(level=logging.INFO,",
            "                        format='%(asctime)s %(levelname)s %(message)s')",
            "    context = {}",
            "    for step in STEPS:",
            "        context = run_step(step, context)",
            "    print(report(context))",
            "    return 0",
            "",
            "",
            "if __name__ == '__main__':",
            "    raise SystemExit(main())",
        ],
    }


def _profile_test(name, title, description, author):
    """A test module: cases as plain functions, with a runner."""
    subject = identifier(name, "the subject")
    return {
        "sections": ("imports", "constants", "functions", "main"),
        "imports": [
            "import sys",
        ],
        "constants": [
            "CASES = []",
        ],
        "functions": [
            {
                "name": "case",
                "args": "function",
                "docstring": "Register a function as one test case.",
                "body": [
                    "CASES.append(function)",
                    "return function",
                ],
            },
            {
                "name": "test_placeholder",
                "args": "",
                "docstring": "Replace this with a real case.",
                "body": [
                    "expected = 1",
                    "found = 1",
                    "assert found == expected, '%r != %r' % (found, expected)",
                ],
            },
            {
                "name": "run_cases",
                "args": "",
                "docstring": "Run every registered case; return (passed, failed).",
                "body": [
                    "passed = 0",
                    "failed = 0",
                    "for function in CASES:",
                    "    try:",
                    "        function()",
                    "    except AssertionError as exc:",
                    "        failed += 1",
                    "        print('FAIL %s: %s' % (function.__name__, exc))",
                    "    except Exception as exc:",
                    "        failed += 1",
                    "        print('ERROR %s: %s' % (function.__name__, exc))",
                    "    else:",
                    "        passed += 1",
                    "        print('ok   %s' % function.__name__)",
                    "return passed, failed",
                ],
            },
        ],
        "main": [
            "def main():",
            '    """The entry point: run the cases and set the exit code."""',
            "    print('testing " + subject + "')",
            "    passed, failed = run_cases()",
            "    print('%d passed, %d failed' % (passed, failed))",
            "    return 1 if failed else 0",
            "",
            "",
            "if __name__ == '__main__':",
            "    sys.exit(main())",
        ],
    }


def _profile_utility(name, title, description, author):
    """A small library of pure helper functions."""
    return {
        "sections": ("imports", "constants", "functions", "main"),
        "imports": [
            "import re",
        ],
        "constants": [
            "NAME = %r" % name,
        ],
        "functions": [
            {
                "name": "normalise",
                "args": "text",
                "docstring": "Collapse runs of whitespace into single spaces.",
                "body": [
                    "return re.sub(r'\\s+', ' ', str(text or '')).strip()",
                ],
            },
            {
                "name": "first_value",
                "args": "mapping, keys, default=None",
                "docstring": "The first key present in the mapping.",
                "body": [
                    "for key in keys:",
                    "    if key in mapping:",
                    "        return mapping[key]",
                    "return default",
                ],
            },
            {
                "name": "chunk",
                "args": "items, size",
                "docstring": "Split a sequence into chunks of at most size.",
                "body": [
                    "if size <= 0:",
                    "    raise ValueError('size must be positive')",
                    "items = list(items)",
                    "return [items[at:at + size]",
                    "        for at in range(0, len(items), size)]",
                ],
            },
        ],
        "main": [
            "def main():",
            '    """Show what the helpers do, as a smoke test."""',
            "    print(normalise('  a   b  '))",
            "    print(first_value({'a': 1}, ('b', 'a')))",
            "    print(chunk(range(5), 2))",
            "    return 0",
            "",
            "",
            "if __name__ == '__main__':",
            "    raise SystemExit(main())",
        ],
    }


def _profile_module(name, title, description, author):
    """An importable module with an explicit public surface."""
    return {
        "sections": ("imports", "constants", "classes", "functions", "main"),
        "imports": [
            "import logging",
        ],
        "constants": [
            "NAME = %r" % name,
            "VERSION = %r" % "0.1.0",
            "__all__ = ['%s', 'create', 'describe']"
            % class_name(name, "Thing"),
        ],
        "classes": [
            {
                "name": class_name(name, "Thing"),
                "docstring": "The main object this module provides.",
                "methods": [
                    {
                        "name": "__init__",
                        "args": "self, name=NAME",
                        "body": [
                            "self.name = name",
                            "self.log = logging.getLogger(name)",
                        ],
                    },
                    {
                        "name": "describe",
                        "args": "self",
                        "docstring": "This object as a plain dictionary.",
                        "body": [
                            "return {'name': self.name, 'version': VERSION}",
                        ],
                    },
                ],
            },
        ],
        "functions": [
            {
                "name": "create",
                "args": "**options",
                "docstring": "Build the module's main object.",
                "body": [
                    "return %s(**options)" % class_name(name, "Thing"),
                ],
            },
            {
                "name": "describe",
                "args": "",
                "docstring": "What this module is, as a plain dictionary.",
                "body": [
                    "return {'module': NAME, 'version': VERSION}",
                ],
            },
        ],
        "main": [
            "def main():",
            '    """A small demonstration of the public surface."""',
            "    logging.basicConfig(level=logging.INFO)",
            "    thing = create()",
            "    print(thing.describe())",
            "    return 0",
            "",
            "",
            "if __name__ == '__main__':",
            "    raise SystemExit(main())",
        ],
    }


def _profile_api(name, title, description, author):
    """A client for one HTTP API, with a request helper and one method."""
    client = class_name(name, "Api") + "Client"
    return {
        "sections": ("imports", "constants", "classes", "main"),
        "imports": [
            "import json",
            "import urllib.error",
            "import urllib.parse",
            "import urllib.request",
        ],
        "constants": [
            "BASE_URL = %r" % "https://api.example.com",
            "TIMEOUT_SECONDS = 15",
        ],
        "classes": [
            {
                "name": "ApiError",
                "docstring": "A call that did not succeed.",
                "methods": [
                    {
                        "name": "__init__",
                        "args": "self, status, body=''",
                        "body": [
                            "super().__init__('HTTP %s: %s' % (status, body[:200]))",
                            "self.status = status",
                            "self.body = body",
                        ],
                    },
                ],
            },
            {
                "name": client,
                "docstring": "One method per endpoint, plus a request helper.",
                "methods": [
                    {
                        "name": "__init__",
                        "args": "self, base_url=BASE_URL, token=''",
                        "body": [
                            "self.base_url = (base_url or BASE_URL).rstrip('/')",
                            "self.token = token",
                        ],
                    },
                    {
                        "name": "request",
                        "args": "self, method, path, params=None, body=None",
                        "docstring": "Send one call and parse the JSON answer.",
                        "body": [
                            "query = ('?' + urllib.parse.urlencode(params)) if params else ''",
                            "url = self.base_url + path + query",
                            "payload = None",
                            "headers = {'Accept': 'application/json'}",
                            "if self.token:",
                            "    headers['Authorization'] = 'Bearer ' + self.token",
                            "if body is not None:",
                            "    payload = json.dumps(body).encode('utf-8')",
                            "    headers['Content-Type'] = 'application/json'",
                            "request = urllib.request.Request(url, data=payload,",
                            "                                 headers=headers,",
                            "                                 method=method.upper())",
                            "try:",
                            "    with urllib.request.urlopen(request,",
                            "                                  timeout=TIMEOUT_SECONDS) as reply:",
                            "        text = reply.read().decode('utf-8', errors='replace')",
                            "except urllib.error.HTTPError as exc:",
                            "    raise ApiError(exc.code, exc.read().decode('utf-8',",
                            "                                                 errors='replace'))",
                            "try:",
                            "    return json.loads(text)",
                            "except ValueError:",
                            "    return text",
                        ],
                    },
                    {
                        "name": "health",
                        "args": "self",
                        "docstring": "GET /health -- is the service answering?",
                        "body": [
                            "return self.request('GET', '/health')",
                        ],
                    },
                ],
            },
        ],
        "main": [
            "def main():",
            '    """The entry point: a single call, for a quick check."""',
            "    client = %s()" % client,
            "    try:",
            "        print(client.health())",
            "    except ApiError as exc:",
            "        print('the call failed: %s' % exc)",
            "        return 1",
            "    return 0",
            "",
            "",
            "if __name__ == '__main__':",
            "    raise SystemExit(main())",
        ],
    }


#: The builders, by profile key.
PROFILE_BUILDERS = {
    "cli": _profile_cli,
    "service": _profile_service,
    "automation": _profile_automation,
    "test": _profile_test,
    "utility": _profile_utility,
    "module": _profile_module,
    "api": _profile_api,
}
# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------
class ScriptProfileEngine(object):
    """Write a whole script from a profile.

    The engine takes the pieces a profile describes and assembles them in a
    fixed order, adding the parts that every script needs: the module docstring,
    the section banners and, optionally, the shebang and the author line.

        generate("cli", "deploy tool")   ->  a complete, runnable script
        sections("cli")                  ->  the order it will be assembled in
        imports("cli")                   ->  just the import block
        functions("cli")                 ->  just the function names

    Every method returns the same result shape as the code generator, so the
    editor and the reports read it the same way.
    """

    #: The interpreter line, added when a script is meant to be run directly.
    SHEBANG = "#!/usr/bin/env python3"

    def profiles(self):
        """The catalogue, for a menu."""
        return tuple(SCRIPT_PROFILES)

    def keys(self):
        return PROFILE_KEYS

    def describe(self, key):
        """One profile as a plugin-safe structure."""
        if key not in PROFILE_KEYS:
            return None
        return {"key": key, "name": PROFILE_LABELS[key],
                "detail": PROFILE_MEANINGS[key],
                "sections": list(self.sections(key)),
                "functions": self.functions(key),
                "status": "available"}

    # -------------------------------------------------------------- pieces
    def _build(self, key, name="", title="", description="", author=""):
        """Run one profile's builder with the names it needs."""
        builder = PROFILE_BUILDERS.get(key)
        if builder is None:
            return None
        return builder(name or "script", title or "Script",
                       description or "A generated script.", author or "")

    def sections(self, key):
        """The section names a profile writes, in order."""
        pieces = self._build(key)
        return tuple(pieces.get("sections")) if pieces else ()

    def imports(self, key):
        """The import lines a profile needs."""
        pieces = self._build(key)
        return list(pieces.get("imports") or []) if pieces else []

    def functions(self, key):
        """The top-level function names a profile defines."""
        pieces = self._build(key)
        if not pieces:
            return []
        names = [item["name"] for item in pieces.get("functions") or []]
        names += [item["name"] for item in pieces.get("classes") or []]
        return names

    # ----------------------------------------------------------- rendering
    def generate(self, key, name="", **options):
        """Write one complete script from a profile.

        ``options`` may carry ``title``, ``description``, ``author``,
        ``shebang`` (default true) and ``unittest`` (use unittest instead of the
        plain runner the test profile writes).
        """
        if key not in PROFILE_KEYS:
            return {"ok": False, "kind": key, "files": [],
                    "error": "unknown profile: %s" % key}
        module = identifier(name or key, key)
        title = options.get("title") or title_case(name or key, "Script")
        description = options.get("description") or (
            "A %s script." % PROFILE_LABELS[key].lower())
        author = options.get("author") or ""
        pieces = self._build(key, module, title, description, author)
        if pieces is None:
            return {"ok": False, "kind": key, "files": [],
                    "error": "the profile has no builder"}
        body = self._render(pieces, module, title, description, author, options)
        summary = "%s: %d line(s), %d section(s)" % (
            PROFILE_LABELS[key], len(body), len(pieces.get("sections") or ()))
        return {"ok": True, "kind": key, "name": module + ".py",
                "profile": key, "profile_label": PROFILE_LABELS[key],
                "title": title, "files": [(module + ".py", body)],
                "sections": list(pieces.get("sections") or ()),
                "notes": ["%s profile" % PROFILE_LABELS[key],
                          "%d line(s)" % len(body)],
                "summary": summary, "language": "python"}

    def _render(self, pieces, module, title, description, author, options):
        """Assemble the file: docstring, sections, then the entry point."""
        lines = []
        if options.get("shebang", True):
            lines += [self.SHEBANG, ""]
        lines += self._docstring(title, description, author,
                                 pieces.get("sections") or ())
        for section in pieces.get("sections") or ():
            content = self._section_lines(pieces, section)
            if not content:
                continue
            # Two blank lines before each banner, which is what PEP 8 asks for
            # around a top-level definition -- a banner is a comment attached
            # to the definitions below it.
            while lines and lines[-1] == "":
                lines.pop()
            lines += ["", ""]
            lines += _banner(SECTION_LABELS.get(section, section))
            lines += content
        text = "\n".join(lines).rstrip() + "\n"
        return _tidy_blank_lines(text)

    @staticmethod
    def _docstring(title, description, author, sections):
        """The module docstring, which also lists the sections."""
        lines = ['"""%s.' % title.rstrip("."), ""]
        lines.append(description.rstrip(".") + ".")
        if sections:
            lines += ["", "Sections:"]
            for section in sections:
                lines.append("    %s" % SECTION_LABELS.get(section, section))
        if author:
            lines += ["", "Author: %s" % author]
        lines.append('"""')
        return lines

    def _section_lines(self, pieces, section):
        """The lines one section contributes."""
        if section == "imports":
            return self._import_lines(pieces, section)
        if section == "constants":
            return list(pieces.get("constants") or [])
        if section == "classes":
            return self._class_lines(pieces.get("classes") or [])
        if section == "functions":
            return self._function_lines(pieces.get("functions") or [])
        if section == "main":
            return list(pieces.get("main") or [])
        return []

    @staticmethod
    def _import_lines(pieces, section):
        """Imports, grouped: standard library first, then blank, then local."""
        imports = list(pieces.get("imports") or [])
        if not imports:
            return []
        standard, other = [], []
        for line in imports:
            target = other if line.startswith("from .") else standard
            target.append(line)
        out = list(standard)
        if standard and other:
            out.append("")
        out += other
        return out

    @staticmethod
    def _class_lines(classes):
        """Each class, its docstring and its methods."""
        out = []
        for index, entry in enumerate(classes):
            if index:
                out.append("")
            out.append("class %s(object):" % entry["name"])
            out.append('    """%s"""' % entry.get("docstring", ""))
            methods = entry.get("methods") or []
            if not methods:
                out.append("    pass")
                continue
            for method in methods:
                out.append("")
                out.append("    def %s(%s):" % (method["name"],
                                                method.get("args", "self")))
                if method.get("docstring"):
                    out.append('        """%s"""' % method["docstring"])
                body = method.get("body") or ["pass"]
                out += ["        %s" % line if line else ""
                        for line in body]
        return out

    @staticmethod
    def _function_lines(functions):
        """Each top-level function, its docstring and its body."""
        out = []
        for index, entry in enumerate(functions):
            if index:
                # One blank line between definitions: two would become three
                # once the section join adds its own separator.
                out.append("")
            out.append("def %s(%s):" % (entry["name"], entry.get("args", "")))
            out.append('    """%s"""' % entry.get("docstring", ""))
            body = entry.get("body") or ["pass"]
            out += ["    %s" % line if line else "" for line in body]
        return out

    def contract(self):
        """Every profile as a plugin-safe structure."""
        return {key: self.describe(key) for key in PROFILE_KEYS}


def _tidy_blank_lines(text):
    """Collapse three or more blank lines into two, and end with one newline."""
    import re as _re
    text = _re.sub(r"\n{4,}", "\n\n\n", text)
    text = _re.sub(r"[ \t]+\n", "\n", text)
    return text.rstrip() + "\n"


def register(key, label, builder, detail=""):
    """Register a new profile at run time (hot-plugging).

    A plugin adds its own builder and the editor menu picks it up from
    ``profiles()``, so nothing in the editor has to know the plugin exists.
    """
    global SCRIPT_PROFILES
    if key in PROFILE_KEYS:
        return False
    SCRIPT_PROFILES = SCRIPT_PROFILES + ((key, "profile_" + key, label,
                                          detail),)
    PROFILE_LABELS[key] = label
    PROFILE_MEANINGS[key] = detail
    PROFILE_BUILDERS[key] = builder
    return True


# ---------------------------------------------------------------------------
# Module-level convenience
# ---------------------------------------------------------------------------
_DEFAULT_ENGINE = ScriptProfileEngine()


def generate(profile, name="", **options):
    return _DEFAULT_ENGINE.generate(profile, name, **options)


def profiles():
    return _DEFAULT_ENGINE.profiles()


def single(result):
    """The single body of a result, or an empty string."""
    if not result or not result.get("ok"):
        return ""
    files = result.get("files") or []
    return files[0][1] if files else ""


def capabilities():
    """Every profile as a plugin-safe catalogue."""
    return _DEFAULT_ENGINE.contract()
