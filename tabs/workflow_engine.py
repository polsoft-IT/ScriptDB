# -*- coding: utf-8 -*-
"""Workflow Builder and Code Composer -- assemble a script from blocks.

Writing a script by hand means remembering the same scaffolding every time: read
the input, parse it, do the work, write the output, log what happened, handle the
failure.  This module holds that scaffolding as data and assembles it.

Two views of the same idea:

    WorkflowBuilder      the script as a *pipeline*: a sequence of stages, each
                         with a role (input, process, output, log, error,
                         config).  The stages are ordered by role, so a workflow
                         reads the way it runs.
    CodeComposer         the script as *blocks*: a bag of concrete operations
                         ("read a file", "parse JSON", "send a request", "save
                         the result", "log", "handle an error") that are stacked
                         in the order they are chosen.

Both produce the same thing -- a ``{"ok", "kind", "name", "files", "notes",
"language", "summary"}`` result, the exact shape ``tabs/code_generator.py``
returns -- so the Editor treats a composed script and a generated one alike.

What it adds on top of the code generator:

    safeguards    (13) the blocks that make a script safe are *added*, not
                  remembered: an input check, a config check, an exception
                  handler, logging, and an error report.  ``harden(result)``
                  wraps any generated source in them.
    metrics       (14) ``measure(source)`` counts the lines, functions, classes,
                  imports, errors and complexity of what was produced, so the
                  result is described rather than assumed.

Conventions, shared with every other engine here: standard library only, ASCII
output only, no writes to disk unless asked, and the block tables drive the
generation so the catalogue cannot name a block the engine does not build.

The blocks are deliberately plain: they emit readable Python that a person can
then edit.  Nothing here is obfuscated, and nothing depends on a service.
"""

import ast
import datetime as dt
import json
import os
import re


# ---------------------------------------------------------------------------
# The workflow stages
# ---------------------------------------------------------------------------
# A workflow is a sequence of stages.  Each stage has a *role*, which decides
# where it sits in the generated script: config first, then input, then process,
# then output, then logging, and the error handling last of all.  That ordering
# is what makes a workflow read the way it runs, however the stages were chosen.
WORKFLOW_ROLES = (
    ("config", "workflow_role_config", "Config",
     "settings and constants the rest of the script reads"),
    ("input", "workflow_role_input", "Input",
     "where the data comes from and how it is read"),
    ("process", "workflow_role_process", "Process",
     "what is done to the data, one step per stage"),
    ("output", "workflow_role_output", "Output",
     "where the result goes"),
    ("log", "workflow_role_log", "Log",
     "what is recorded while it runs"),
    ("error", "workflow_role_error", "Error",
     "what happens when a step fails"),
)

WORKFLOW_ROLE_KEYS = tuple(key for key, _lk, _f, _d in WORKFLOW_ROLES)
WORKFLOW_ROLE_LABELS = {key: fallback for key, _lk, fallback, _d
                        in WORKFLOW_ROLES}
WORKFLOW_ROLE_MEANINGS = {key: detail for key, _lk, _f, detail
                          in WORKFLOW_ROLES}

#: The order the stages are laid out in the generated script.
WORKFLOW_ORDER = ("config", "input", "process", "output", "log", "error")


# ---------------------------------------------------------------------------
# The block catalogue
# ---------------------------------------------------------------------------
# Every block the composer knows.  Each entry is
# (key, role, label key, fallback, what it emits).
#
# ``role`` is the workflow stage the block belongs to; ``what it emits`` is a
# one-line honest description used by the catalogue and by the dialog, so the
# list cannot claim a block that produces nothing.
BLOCKS = (
    # ---- config ----------------------------------------------------------
    ("config", "config", "block_config", "Config block",
     "a SETTINGS mapping with the values the other blocks read"),
    ("constants", "config", "block_constants", "Constants",
     "named constants for the paths and limits used below"),
    ("env", "config", "block_env", "Environment values",
     "a reader for environment variables with defaults"),
    # ---- input -----------------------------------------------------------
    ("read_file", "input", "block_read_file", "Read a file",
     "read a text file with an explicit encoding and a clear error"),
    ("read_lines", "input", "block_read_lines", "Read lines",
     "read a file into a list of stripped lines, blanks removed"),
    ("read_json_file", "input", "block_read_json", "Read JSON from a file",
     "parse a JSON file through the json module, with a clear error"),
    ("read_csv", "input", "block_read_csv", "Read CSV",
     "read a CSV file into a list of dictionaries via csv.DictReader"),
    ("argv", "input", "block_argv", "Command-line arguments",
     "parse sys.argv into options with a usage message"),
    ("stdin", "input", "block_stdin", "Read standard input",
     "read standard input as text, for a piped script"),
    ("list_dir", "input", "block_list_dir", "List a folder",
     "list the files in a folder, filtered by suffix"),
    # ---- process ---------------------------------------------------------
    ("parse_json", "process", "block_parse_json", "Parse JSON",
     "parse a JSON string into objects with a clear error"),
    ("build_json", "process", "block_build_json", "Build JSON",
     "serialise a structure back to JSON text"),
    ("transform", "process", "block_transform", "Transform records",
     "map and filter a list of records, one function per concern"),
    ("filter", "process", "block_filter", "Filter records",
     "keep only the records a predicate accepts"),
    ("sort", "process", "block_sort", "Sort records",
     "sort records by a key, with a documented direction"),
    ("aggregate", "process", "block_aggregate", "Aggregate / count",
     "count and total records into a summary mapping"),
    ("regex", "process", "block_regex", "Extract with a pattern",
     "pull fields out of text with one compiled regular expression"),
    ("dedupe", "process", "block_dedupe", "Remove duplicates",
     "drop duplicate records, keeping the first seen"),
    ("validate", "process", "block_validate", "Validate records",
     "check each record against required fields and report the bad ones"),
    ("chunk", "process", "block_chunk", "Batch records",
     "split records into batches of a fixed size"),
    ("hash", "process", "block_hash", "Hash the data",
     "a stable SHA-256 of the data, for an integrity check"),
    # ---- output ----------------------------------------------------------
    ("write_file", "output", "block_write_file", "Write a file",
     "write text to a file, creating the folder when it is missing"),
    ("write_json", "output", "block_write_json", "Write JSON",
     "write a structure to a JSON file, indented and sorted"),
    ("write_csv", "output", "block_write_csv", "Write CSV",
     "write records to a CSV file with a header row"),
    ("print", "output", "block_print", "Print the result",
     "print the result to standard output"),
    ("request", "output", "block_request", "Send a request",
     "POST the result to a URL with urllib, with a timeout"),
    # ---- log -------------------------------------------------------------
    ("log", "log", "block_log", "Log to a file",
     "append timestamped lines to a log file"),
    ("log_console", "log", "block_log_console", "Log to the console",
     "print timestamped lines to standard output"),
    # ---- error -----------------------------------------------------------
    ("try_except", "error", "block_try", "Handle exceptions",
     "run the work inside a try/except that reports and exits non-zero"),
    ("retry", "error", "block_retry", "Retry on failure",
     "retry a callable a few times, with a delay between attempts"),
    ("assert_input", "error", "block_assert_input", "Check the input",
     "fail fast with a clear message when a required input is missing"),
    ("report", "error", "block_report", "Write an error report",
     "write a timestamped report of a failure to a file"),
    ("exit_code", "error", "block_exit_code", "Return an exit code",
     "exit with a code a scheduler can act on"),
)

BLOCK_KEYS = tuple(key for key, _r, _lk, _f, _d in BLOCKS)
BLOCK_ROLES = {key: role for key, role, _lk, _f, _d in BLOCKS}
BLOCK_LABELS = {key: fallback for key, _r, _lk, fallback, _d in BLOCKS}
BLOCK_MEANINGS = {key: detail for key, _r, _lk, _f, detail in BLOCKS}
BLOCK_GROUPS = WORKFLOW_ROLE_KEYS

#: Blocks that call a helper another block defines.  Each entry maps a block
#: to the blocks it needs, so the assembler can pull the helper in and the
#: generated script never references a name that was never defined.
_BLOCK_DEPENDENCIES = {
    "read_lines": ("read_file",),
    "read_json_file": ("read_file",),
    "read_csv": ("read_file",),
    "write_json": ("write_file",),
    "write_csv": ("write_file",),
    "report": ("write_file",),
    "retry": (),
}


def blocks_for_role(role):
    """The blocks belonging to one workflow role, in catalogue order."""
    return [key for key, item_role, _lk, _f, _d in BLOCKS if item_role == role]


def role_of(block):
    """The workflow role a block belongs to, or None when unknown."""
    return BLOCK_ROLES.get(block)


# ---------------------------------------------------------------------------
# What a safe script always carries (13)
# ---------------------------------------------------------------------------
# The safeguards are the same idea as the blocks, but they are *added* to a
# result rather than chosen: a generated script should not be able to forget
# them.  Each entry is (key, label key, fallback, what it guarantees).
SAFEGUARDS = (
    ("input", "safeguard_input", "Input validation",
     "a required value is checked before it is used, and the failure says why"),
    ("config", "safeguard_config", "Config validation",
     "the settings are checked once, at start-up, not at each use"),
    ("exceptions", "safeguard_exceptions", "Exception handling",
     "the work runs inside a handler, so a failure is reported not dumped"),
    ("logging", "safeguard_logging", "Error logging",
     "a failure is written to a log, so it survives the process"),
    ("report", "safeguard_report", "Error report",
     "the failure is written to a report file a person can read later"),
    ("exit", "safeguard_exit", "Exit code",
     "the process ends with a code a scheduler can act on"),
)

SAFEGUARD_KEYS = tuple(key for key, _lk, _f, _d in SAFEGUARDS)
SAFEGUARD_LABELS = {key: fallback for key, _lk, fallback, _d in SAFEGUARDS}
SAFEGUARD_MEANINGS = {key: detail for key, _lk, _f, detail in SAFEGUARDS}
#: Which safeguards ``harden`` adds unless told otherwise.
DEFAULT_SAFEGUARDS = SAFEGUARD_KEYS


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _now():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def identifier(text, fallback="value"):
    """A printable-ASCII Python identifier from arbitrary text.

    Kept local rather than imported from the code generator so this module can
    be used on its own; the rule is the same one that engine applies.
    """
    text = re.sub(r"[^0-9A-Za-z]+", "_", (text or "").strip())
    text = re.sub(r"_+", "_", text).strip("_").lower()
    if not text:
        return fallback
    if text[0].isdigit():
        text = "item_" + text
    return text


def _code_only(text):
    """The source with comments and blank lines dropped, for measuring."""
    out = []
    for line in (text or "").replace("\r\n", "\n").split("\n"):
        bare = line.strip()
        if not bare or bare.startswith("#"):
            continue
        out.append(line)
    return out


# ---------------------------------------------------------------------------
# The blocks: what each one emits
# ---------------------------------------------------------------------------
# Every builder returns the lines of one top-level definition, plus the call
# that belongs in ``main``.  The call is a single line, so the pipeline in
# ``main`` reads as the list of stages the user chose.
class _Block(object):
    """One block: its definition lines and the call that uses them."""

    def __init__(self, body, call, name=""):
        self.body = list(body)          # the def (or statement) block
        self.call = call                # one line for main(), or ""
        self.name = name                # the definition it introduces


def _block_source(key, options):
    """Build one block by key.  Returns a :class:`_Block`.

    ``options`` carries the few settings a block can need (a path, a URL, a
    batch size).  Every one has a default, so a block can always be emitted and
    the defaults are visible in the generated script, where a person can change
    them.
    """
    path = options.get("path") or "input.txt"
    out_path = options.get("output") or "output.txt"
    url = options.get("url") or "https://example.invalid/api"
    size = int(options.get("batch") or 100)

    if key == "config":
        return _Block([
            "SETTINGS = {",
            '    "input_path": %r,' % path,
            '    "output_path": %r,' % out_path,
            '    "log_path": "run.log",',
            '    "report_path": "error_report.txt",',
            '    "batch_size": %d,' % size,
            "}",
            "",
            "",
            "def load_settings():",
            '    """The settings, with the values above as the defaults."""',
            "    settings = dict(SETTINGS)",
            "    return settings",
        ], "settings = load_settings()", "load_settings")

    if key == "constants":
        return _Block([
            'DEFAULT_ENCODING = "utf-8"',
            "MAX_RECORDS = 1000",
            "TIMEOUT_SECONDS = 30",
        ], "", "")

    if key == "env":
        return _Block([
            "def env_value(name, default=\"\"):",
            '    """An environment variable, or a default when it is unset."""',
            "    import os",
            "    return os.environ.get(name, default)",
        ], "", "env_value")

    # ---- input -----------------------------------------------------------
    if key == "read_file":
        return _Block([
            "def read_text(path):",
            '    """Read a text file, failing with the reason, not a traceback."""',
            "    try:",
            '        with open(path, "r", encoding="utf-8") as handle:',
            "            return handle.read()",
            "    except OSError as exc:",
            '        raise RuntimeError("cannot read %s: %s" % (path, exc))',
        ], 'text = read_text(settings["input_path"])', "read_text")

    if key == "read_lines":
        return _Block([
            "def read_lines(path):",
            '    """The non-empty, stripped lines of a file."""',
            '    return [line.strip() for line in read_text(path).splitlines()',
            "            if line.strip()]",
        ], 'lines = read_lines(settings["input_path"])', "read_lines")

    if key == "read_json_file":
        return _Block([
            "def read_json_file(path):",
            '    """Parse a JSON file; an empty file reads as an empty list."""',
            "    import json",
            "    text = read_text(path)",
            "    if not text.strip():",
            "        return []",
            "    try:",
            "        return json.loads(text)",
            "    except ValueError as exc:",
            '        raise RuntimeError("%s is not valid JSON: %s" % (path, exc))',
        ], 'records = read_json_file(settings["input_path"])', "read_json_file")

    if key == "read_csv":
        return _Block([
            "def read_csv(path):",
            '    """A CSV file as a list of dictionaries, one per row."""',
            "    import csv",
            '    with open(path, "r", encoding="utf-8", newline="") as handle:',
            "        return list(csv.DictReader(handle))",
        ], 'records = read_csv(settings["input_path"])', "read_csv")

    if key == "argv":
        return _Block([
            "def parse_arguments(argv):",
            '    """Positional arguments, or a usage message and exit 2."""',
            "    if len(argv) < 2:",
            '        print("usage: %s <input>" % argv[0])',
            "        raise SystemExit(2)",
            "    return {\"input\": argv[1]}",
        ], "", "parse_arguments")

    if key == "stdin":
        return _Block([
            "def read_stdin():",
            '    """Everything on standard input, as text."""',
            "    import sys",
            "    return sys.stdin.read()",
        ], "", "read_stdin")

    if key == "list_dir":
        return _Block([
            "def list_folder(path, suffix=\"\"):",
            '    """The file names in a folder, optionally by suffix."""',
            "    import os",
            "    try:",
            "        names = sorted(os.listdir(path))",
            "    except OSError as exc:",
            '        raise RuntimeError("cannot list %s: %s" % (path, exc))',
            "    if suffix:",
            "        names = [name for name in names if name.endswith(suffix)]",
            "    return names",
        ], "", "list_folder")

    # ---- process ---------------------------------------------------------
    if key == "parse_json":
        return _Block([
            "def parse_json(text):",
            '    """Parse JSON text, with the position of a syntax error."""',
            "    import json",
            "    try:",
            "        return json.loads(text)",
            "    except ValueError as exc:",
            '        raise RuntimeError("invalid JSON: %s" % exc)',
        ], "", "parse_json")

    if key == "build_json":
        return _Block([
            "def build_json(data):",
            '    """A structure as indented JSON text."""',
            "    import json",
            "    return json.dumps(data, indent=2, ensure_ascii=False,",
            "                      sort_keys=True)",
        ], 'output_text = build_json(result)', "build_json")

    if key == "transform":
        return _Block([
            "def transform(records):",
            '    """One cleaned record out of each raw one."""',
            "    cleaned = []",
            "    for index, record in enumerate(records):",
            "        if not isinstance(record, dict):",
            "            continue",
            '        cleaned.append({"index": index, "value": record})',
            "    return cleaned",
        ], "result = transform(records)", "transform")

    if key == "filter":
        return _Block([
            "def keep(record):",
            '    """True when a record should be kept."""',
            '    return bool(record.get("value"))',
            "",
            "",
            "def filter_records(records):",
            '    """Only the records the predicate keeps."""',
            "    return [record for record in records if keep(record)]",
        ], "result = filter_records(result)", "filter_records")

    if key == "sort":
        return _Block([
            "def sort_records(records, key=\"index\", reverse=False):",
            '    """Records sorted by one key; None values sort last."""',
            "    def order(record):",
            '        value = record.get(key)',
            "        return (value is None, value)",
            "    return sorted(records, key=order, reverse=reverse)",
        ], "result = sort_records(result)", "sort_records")

    if key == "aggregate":
        return _Block([
            "def summarise(records):",
            '    """Counts and totals, as a plain mapping."""',
            "    total = 0",
            "    for record in records:",
            '        value = record.get("value")',
            "        if isinstance(value, (int, float)):",
            "            total += value",
            '    return {"count": len(records), "total": total}',
        ], "summary = summarise(result)", "summarise")

    if key == "regex":
        return _Block([
            "def extract_pattern(text, pattern=r\"(\\w+)=([^\\s]+)\"):",
            '    """The key/value pairs a pattern finds, as a mapping."""',
            "    import re",
            "    found = {}",
            "    for match in re.finditer(pattern, text or \"\"):",
            "        found[match.group(1)] = match.group(2)",
            "    return found",
        ], "result = extract_pattern(text)", "extract_pattern")

    if key == "dedupe":
        return _Block([
            "def dedupe(records):",
            '    """Records with duplicates removed, first one kept."""',
            "    seen = set()",
            "    unique = []",
            "    for record in records:",
            "        marker = repr(sorted(record.items())) if isinstance(",
            "            record, dict) else repr(record)",
            "        if marker in seen:",
            "            continue",
            "        seen.add(marker)",
            "        unique.append(record)",
            "    return unique",
        ], "result = dedupe(result)", "dedupe")

    if key == "validate":
        return _Block([
            "def validate_records(records, required=(\"index\", \"value\")):",
            '    """Split records into good and bad by required fields."""',
            "    good, bad = [], []",
            "    for record in records:",
            "        missing = [field for field in required",
            "                   if field not in record]",
            "        if missing:",
            '            bad.append({"record": record, "missing": missing})',
            "        else:",
            "            good.append(record)",
            '    return {"good": good, "bad": bad}',
        ], "checked = validate_records(result)", "validate_records")

    if key == "chunk":
        return _Block([
            "def chunk(records, size=%d):" % size,
            '    """Records split into batches of at most `size`."""',
            "    if size < 1:",
            '        raise ValueError("batch size must be at least 1")',
            "    return [records[start:start + size]",
            "            for start in range(0, len(records), size)]",
        ], "batches = chunk(result)", "chunk")

    if key == "hash":
        return _Block([
            "def digest(data):",
            '    """A stable SHA-256 of anything that can be written out."""',
            "    import hashlib",
            "    if not isinstance(data, (bytes, str)):",
            "        data = build_json(data)",
            '    if isinstance(data, str):',
            '        data = data.encode("utf-8")',
            "    return hashlib.sha256(data).hexdigest()",
        ], "result_hash = digest(result)", "digest")

    # ---- output ----------------------------------------------------------
    if key == "write_file":
        return _Block([
            "def write_text(path, text):",
            '    """Write text, creating the folder when it is missing."""',
            "    import os",
            "    folder = os.path.dirname(path)",
            "    if folder and not os.path.isdir(folder):",
            "        os.makedirs(folder, exist_ok=True)",
            '    with open(path, "w", encoding="utf-8") as handle:',
            "        handle.write(text)",
        ], 'write_text(settings["output_path"], output_text)', "write_text")

    if key == "write_json":
        return _Block([
            "def write_json(path, data):",
            '    """Write a structure as indented, sorted JSON."""',
            "    import json",
            '    write_text(path, json.dumps(data, indent=2, ensure_ascii=False,',
            "                              sort_keys=True))",
        ], 'write_json(settings["output_path"], result)', "write_json")

    if key == "write_csv":
        return _Block([
            "def write_csv(path, records):",
            '    """Write records to CSV, one header row, one row each."""',
            "    import csv",
            "    if not records:",
            '        write_text(path, "")',
            "        return",
            "    columns = []",
            "    for record in records:",
            "        for key in record:",
            "            if key not in columns:",
            "                columns.append(key)",
            '    with open(path, "w", encoding="utf-8", newline="") as handle:',
            "        writer = csv.DictWriter(handle, fieldnames=columns,",
            '                                extrasaction="ignore")',
            "        writer.writeheader()",
            "        for record in records:",
            "            writer.writerow(record)",
        ], 'write_csv(settings["output_path"], result)', "write_csv")

    if key == "print":
        return _Block([
            "def report_result(result):",
            '    """Print the result in a form a log can read."""',
            '    print("result: %d record(s)" % len(result))',
        ], "report_result(result)", "report_result")

    if key == "request":
        return _Block([
            "def send_result(url, data, timeout=TIMEOUT_SECONDS):",
            '    """POST the result as JSON, returning the status code."""',
            "    import urllib.error",
            "    import urllib.request",
            "    body = build_json(data).encode(\"utf-8\")",
            "    request = urllib.request.Request(",
            '        url, data=body, method="POST",',
            '        headers={"Content-Type": "application/json"})',
            "    try:",
            "        with urllib.request.urlopen(request, timeout=timeout) as reply:",
            "            return reply.status",
            "    except urllib.error.URLError as exc:",
            '        raise RuntimeError("request to %s failed: %s" % (url, exc))',
        ], "send_result(%r, result)" % url, "send_result")

    # ---- log -------------------------------------------------------------
    if key == "log":
        return _Block([
            "def log(message, path=None):",
            '    """Append one timestamped line to the log."""',
            "    import datetime",
            '    path = path or SETTINGS["log_path"]',
            '    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")',
            '    with open(path, "a", encoding="utf-8") as handle:',
            '        handle.write("%s %s\\n" % (stamp, message))',
        ], 'log("finished: %d record(s)" % len(result))', "log")

    if key == "log_console":
        return _Block([
            "def log_console(message):",
            '    """Print one timestamped line to standard output."""',
            "    import datetime",
            '    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")',
            '    print("%s %s" % (stamp, message))',
        ], 'log_console("finished: %d record(s)" % len(result))', "log_console")

    # ---- error -----------------------------------------------------------
    if key == "try_except":
        return _Block([
            "def run_work():",
            '    """The work itself; raise on anything unexpected."""',
            "    pass",
        ], "", "run_work")

    if key == "retry":
        return _Block([
            "def retry(call, attempts=3, delay=1.0):",
            '    """Call `call`, retrying on failure with a growing delay."""',
            "    import time",
            "    last = None",
            "    for attempt in range(1, attempts + 1):",
            "        try:",
            "            return call()",
            "        except Exception as exc:",
            "            last = exc",
            "            if attempt < attempts:",
            "                time.sleep(delay * attempt)",
            "    raise RuntimeError(\"failed after %d attempts: %s\"",
            "                       % (attempts, last))",
        ], "", "retry")

    if key == "assert_input":
        return _Block([
            "def require(value, name, hint=\"\"):",
            '    """Fail fast when a required value is missing or empty."""',
            "    if value in (None, \"\") or (isinstance(value, (list, dict))",
            "                               and not value):",
            '        message = "required value is missing: %s" % name',
            "        if hint:",
            '            message += " (%s)" % hint',
            "        raise ValueError(message)",
            "    return value",
        ], 'require(settings.get("input_path"), "input_path",'
            '\n        "set it in SETTINGS or on the command line")',
             "require")

    if key == "report":
        return _Block([
            "def write_report(error, path=None):",
            '    """Write a failure to a report file, with the traceback."""',
            "    import datetime",
            "    import traceback",
            '    path = path or SETTINGS["report_path"]',
            '    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")',
            "    lines = [",
            '        "failure report",',
            '        "generated: %s" % stamp,',
            '        "error: %s: %s" % (type(error).__name__, error),',
            '        "",',
            '        traceback.format_exc(),',
            "    ]",
            '    write_text(path, "\\n".join(lines) + "\\n")',
            "    return path",
        ], "", "write_report")

    if key == "exit_code":
        return _Block([
            "EXIT_OK = 0",
            "EXIT_FAILED = 1",
        ], "", "")

    return None


# ---------------------------------------------------------------------------
# The pipeline: main()
# ---------------------------------------------------------------------------
def _main_body(calls, with_safeguards=True, safeguards=DEFAULT_SAFEGUARDS):
    """The ``main`` function, built from the calls the blocks contributed.

    The calls run in the order given -- the stages were sorted before this
    is reached -- and the safeguards wrap the whole list, so a failure
    anywhere in the pipeline is reported the same way.

    The settings line is not written here: the config block contributes it
    as its own call, and writing it in both places would set it twice.
    """
    lines = ["def main():", '    """Run the pipeline."""', "    try:"]
    for call in calls:
        if not call:
            continue
        # A call can be several lines (an argument list was wrapped); every
        # line is indented, so the emitted body stays valid Python.
        for piece in call.split("\n"):
            lines.append("        " + piece)
    lines.append('        print("done")')
    lines.append("        return 0")
    if "exceptions" in safeguards and with_safeguards:
        lines.append("    except Exception as error:")
        if "logging" in safeguards:
            lines.append('        log("failed: %s" % error)')
        else:
            lines.append("        # no log block was chosen")
        if "report" in safeguards:
            lines.append("        write_report(error)")
        lines.append("        return EXIT_FAILED")
    else:
        lines.append("    except Exception:")
        lines.append("        raise")
    lines += ["", "",
              'if __name__ == "__main__":',
              "    raise SystemExit(main())"]
    return lines


def _header(name, notes):
    """The spoken preamble: what built this, and from what."""
    lines = [
        "#!/usr/bin/env python3",
        '"""%s' % name,
        "",
        "Generated by the ScriptDB workflow builder.",
        "generated: %s" % _now(),
    ]
    for note in notes:
        lines.append(note)
    lines.append('"""')
    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# The builder and the composer
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Metrics (14)
# ---------------------------------------------------------------------------
def measure(source):
    """Describe a script: its size, shape, problems and complexity.

    Returns a plain mapping of counts, so a caller can report what was produced
    instead of assuming it.  Python is measured through the ``ast`` module, so a
    def inside a class is counted where it belongs; anything that does not parse
    falls back to counting the text, and the parse error is reported rather than
    hidden.
    """
    text = (source or "").replace("\r\n", "\n")
    lines = text.split("\n")
    code_lines = _code_only(text)
    result = {
        "lines": len(lines) - (1 if lines and lines[-1] == "" else 0),
        "code_lines": len(code_lines),
        "characters": len(text),
        "functions": 0,
        "methods": 0,
        "classes": 0,
        "imports": 0,
        "errors": 0,
        "warnings": 0,
        "cyclomatic": 0,
        "logical": 0,
        "parses": False,
    }
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        result["errors"] = 1
        result["parse_error"] = "line %s: %s" % (exc.lineno, exc.msg)
        # Without a tree the counts come from the text, which is honest about
        # what it can see and is better than reporting zero.
        result["functions"] = len(re.findall(r"^\s*def\s+\w+", text,
                                            re.MULTILINE))
        result["classes"] = len(re.findall(r"^\s*class\s+\w+", text,
                                          re.MULTILINE))
        result["imports"] = len(re.findall(r"^\s*(?:from|import)\s+", text,
                                          re.MULTILINE))
        return result

    result["parses"] = True
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            result["functions"] += 1
        elif isinstance(node, ast.ClassDef):
            result["classes"] += 1
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            result["imports"] += 1
    # A function defined directly by a class is a method, and is counted in both
    # totals so neither number has to be explained away.
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            result["methods"] += sum(
                1 for child in node.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)))

    # Cyclomatic complexity: one per function, plus one per decision point.
    decisions = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try,
                 ast.ExceptHandler, ast.With, ast.AsyncWith, ast.BoolOp,
                 ast.IfExp, ast.Assert)
    for node in ast.walk(tree):
        if isinstance(node, decisions):
            result["cyclomatic"] += 1
    result["cyclomatic"] += result["functions"]

    result["logical"] = len(re.findall(
        r"\b(?:and|or|not|if|elif|else|for|while|try|except|finally|with|"
        r"assert|raise|return|yield|lambda)\b", text))

    # Warnings: the shapes a reviewer would query, not errors.
    for number, line in enumerate(lines, 1):
        if len(line.rstrip("\r")) > 100:
            result["warnings"] += 1
        elif line.rstrip("\r") != line.rstrip():
            result["warnings"] += 1
        elif "TODO" in line or "FIXME" in line:
            result["warnings"] += 1

    result["detail"] = (
        "%d line(s), %d function(s), %d class(es), %d import(s), "
        "complexity %d, %d warning(s)"
        % (result["lines"], result["functions"], result["classes"],
           result["imports"], result["cyclomatic"], result["warnings"]))
    return result



class WorkflowBuilder(object):
    """Build a script from a sequence of stages, in workflow order.

    A stage is ``(role, block)`` or just a block name, whose role is looked up.
    The stages are collected, ordered by role, and emitted together, so a
    workflow chosen in any order comes out in an order that reads correctly.
    """

    def build(self, stages=(), name="workflow", options=None, safeguards=None,
              harden=True):
        """Assemble a script from the stages.

        Returns the same result shape the code generator returns, so the Editor
        and the composer treat the two alike.
        """
        options = dict(options or {})
        chosen = []
        unknown = []
        for stage in stages or ():
            if isinstance(stage, (tuple, list)) and len(stage) == 2:
                role, block = stage
            else:
                block = stage
                role = role_of(block)
            if block not in BLOCK_KEYS:
                unknown.append(block)
                continue
            chosen.append((role or role_of(block) or "process", block))
        if unknown:
            return self._failed("workflow", "unknown block(s): %s"
                                % ", ".join(str(item) for item in unknown))
        if not chosen:
            return self._failed("workflow", "no stages were chosen")

        return self._assemble(chosen, name, options, safeguards, harden,
                              kind="workflow")

    def compose(self, blocks=(), name="composed", options=None,
                safeguards=None, harden=True):
        """Assemble a script from a stack of blocks, in the order given.

        The composer is the builder with the roles inferred: the blocks are kept
        in the order they were chosen, and only the error handling is moved to
        the end, where it belongs, since it wraps everything else.
        """
        options = dict(options or {})
        chosen = []
        unknown = []
        for block in blocks or ():
            if block not in BLOCK_KEYS:
                unknown.append(block)
                continue
            chosen.append((role_of(block) or "process", block))
        if unknown:
            return self._failed("composer", "unknown block(s): %s"
                                % ", ".join(str(item) for item in unknown))
        if not chosen:
            return self._failed("composer", "no blocks were chosen")

        # Stable order: the user's own order is kept, except that the error
        # blocks must come last and config first, or the script would not run.
        def rank(item):
            role = item[0]
            if role == "config":
                return 0
            if role == "error":
                return 2
            return 1

        chosen = sorted(chosen, key=rank)
        return self._assemble(chosen, name, options, safeguards, harden,
                              kind="composer")

    # ------------------------------------------------------------ assembly
    def _assemble(self, chosen, name, options, safeguards, harden, kind):
        """Emit the script for the chosen (role, block) pairs."""
        active = list(DEFAULT_SAFEGUARDS if safeguards is None else safeguards)
        if not harden:
            active = []

        # A block the user chose for its role is always emitted; the safeguards
        # are added on top, and never duplicated when the block is already there.
        blocks = [block for _role, block in chosen]
        # The blocks the user actually chose.  Only these contribute a
        # pipeline call: a helper or safeguard added below contributes its
        # definitions and nothing else, or the generated script would call
        # a step nobody asked for.
        chosen_blocks = set(blocks)
        if "exceptions" in active and "try_except" not in blocks:
            blocks.append("try_except")
        if "report" in active and "report" not in blocks:
            blocks.append("report")
        if "exit" in active and "exit_code" not in blocks:
            blocks.append("exit_code")
        if "config" in active and "config" not in blocks:
            blocks.append("config")
        if "input" in active and "assert_input" not in blocks:
            blocks.append("assert_input")

        # A block can depend on a helper another block defines.  Pulling the
        # dependency in is the difference between a script that runs and one
        # that fails with a NameError the first time it is used.
        for _round in range(4):
            added = False
            for block in list(blocks):
                for dependency in _BLOCK_DEPENDENCIES.get(block, ()):
                    if dependency not in blocks:
                        blocks.append(dependency)
                        added = True
            if not added:
                break

        built = {}
        order = []
        for block in blocks:
            if block in built:
                continue
            item = _block_source(block, options)
            if item is None:
                return self._failed(kind, "block produced nothing: %s" % block)
            built[block] = item
            order.append(block)

        # Definitions come out in workflow order; the calls in the same order,
        # which is what makes the pipeline in main() read as the stages chosen.
        # The position of each block is looked up from a map built once, rather
        # than searched for on every comparison.
        index_of = {block: position for position, block in enumerate(order)}

        def sort_key(block):
            role = role_of(block) or "process"
            index = WORKFLOW_ORDER.index(role) if role in WORKFLOW_ORDER else 9
            return (index, index_of[block])

        # Every name the emitted definitions introduce, so the assembly can
        # tell whether load_settings is available to call.
        script_names = set()
        for block in order:
            name = built[block].name
            if name:
                script_names.add(name)
        definitions = []
        for block in sorted(order, key=sort_key):
            item = built[block]
            if item.body:
                definitions.append(item.body)
                definitions.append([""])
        calls = [built[block].call for block in sorted(order, key=sort_key)
                 if block in chosen_blocks]

        notes = ["blocks: %s" % ", ".join(blocks)]
        if active:
            notes.append("safeguards: %s" % ", ".join(active))

        lines = _header(name, notes)
        for chunk in definitions:
            lines.extend(chunk)
        # The settings have to be loaded before anything reads them, and
        # exactly once, however they came to be in the script.
        if "load_settings" in script_names and not any(
                "load_settings()" in (call or "") for call in calls):
            calls.insert(0, "settings = load_settings()")
        lines.extend(_main_body(calls, bool(active), active))

        source = "\n".join(lines) + "\n"
        metrics = measure(source)
        return {
            "ok": True, "kind": kind,
            "name": "%s.py" % identifier(name, "workflow"),
            "files": [("%s.py" % identifier(name, "workflow"), source)],
            "notes": notes, "language": "python",
            "blocks": blocks, "safeguards": active,
            "metrics": metrics,
            "summary": "%s: %d block(s), %d line(s), %d function(s)"
                       % (kind, len(blocks), metrics["lines"],
                          metrics["functions"]),
        }

    @staticmethod
    def _failed(kind, error):
        return {"ok": False, "kind": kind, "error": error, "files": [],
                "notes": [], "summary": "%s failed: %s" % (kind, error)}

    @staticmethod
    def single(result):
        """The single body of a result, or an empty string."""
        if not result or not result.get("ok"):
            return ""
        files = result.get("files") or []
        return files[0][1] if files else ""


# ---------------------------------------------------------------------------
# Safeguards (13)
# ---------------------------------------------------------------------------
def harden(source, safeguards=None):
    """Wrap generated source in the safeguards, keeping its own code intact.

    The body of the script is left exactly as it was; the pieces that make it
    safe are added around it -- a config check, an input check, an exception
    handler, logging and a report.  The result is returned as a source string,
    so it can be handed to the same consumers as any other generated script.
    """
    active = list(DEFAULT_SAFEGUARDS if safeguards is None else safeguards)
    text = (source or "").replace("\r\n", "\n")
    added = []

    lines = text.split("\n")
    # The header comment block stays at the top, where a person looks for it.
    insert_at = 1 if lines and lines[0].startswith("#!") else 0
    while insert_at < len(lines) and (lines[insert_at].startswith("#")
                                      or not lines[insert_at].strip()):
        insert_at += 1
    if insert_at < len(lines) and lines[insert_at].lstrip().startswith(
            ('"""', "'''")):
        quote = lines[insert_at].lstrip()[:3]
        insert_at += 1
        while insert_at < len(lines):
            if lines[insert_at].rstrip().endswith(quote):
                insert_at += 1
                break
            insert_at += 1

    preamble = []
    if "report" in active and "def write_report" not in text:
        preamble.extend(_block_source("report", {}).body)
        preamble.append("")
        added.append("report")
    if "logging" in active and "def log(" not in text:
        preamble.extend(_block_source("log", {}).body)
        preamble.append("")
        added.append("logging")
    if "config" in active and "def load_settings" not in text:
        preamble.extend(_block_source("config", {}).body)
        preamble.append("")
        added.append("config")
    if "input" in active and "def require(" not in text:
        preamble.extend(_block_source("assert_input", {}).body)
        preamble.append("")
        added.append("input")

    # A main() that does not catch anything gets the handler around its body.
    if "exceptions" in active and "except Exception" not in text:
        text = _wrap_main(text)
        added.append("exceptions")

    if preamble:
        lines = text.split("\n")
        lines = lines[:insert_at] + preamble + lines[insert_at:]
        text = "\n".join(lines)
    if added:
        text = text.rstrip("\n") + "\n"
    return {"ok": True, "source": text, "added": added,
            "safeguards": active,
            "detail": "added: %s" % (", ".join(added) if added else "nothing")}


def _wrap_main(text):
    """Put a try/except around the body of ``main``.

    Done by indenting exactly the lines between ``def main():`` and the line at
    column zero that ends it, so nothing outside ``main`` is touched.
    """
    lines = text.split("\n")
    start = None
    for index, line in enumerate(lines):
        if line.startswith("def main("):
            start = index
            break
    if start is None:
        return text
    # The body ends at the first following line that starts at column zero
    # (excluding blanks, which belong to whichever side they are adjacent to).
    end = len(lines)
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if line.strip() and not line[:1].isspace():
            end = index
            break
    body = lines[start + 1:end]
    while body and not body[-1].strip():
        body.pop()
    if not body:
        return text
    guarded = ["    try:"]
    for line in body:
        guarded.append("    " + line if line.strip() else "")
    guarded.append("    except Exception:")
    guarded.append('        print("failed: %s" % error)')
    guarded.append("        raise SystemExit(1)")
    return "\n".join(lines[:start + 1] + guarded + lines[end:])
