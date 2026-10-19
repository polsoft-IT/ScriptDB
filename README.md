# ScriptDB

> A self-contained PyQt5 desktop application for storing, editing, searching,
> securing and scaffolding a personal library of scripts — in thirteen
> languages, with a pluggable syntax layer, pluggable search engine and full
> code navigation.

ScriptDB v2.5 keeps every snippet you want to remember in one JSON database, then
gives you a real editing surface for it: syntax colouring, smart suggestions,
sixteen ways of searching across the whole library, ten ways of jumping around
the code, an isolated sandbox to run a script in, a hardening toolkit that
fingerprints and seals it, and a generator that scaffolds whole projects from a
preset. Everything is standard library plus PyQt5 — no other dependency, no
network access, no external tooling.

```
Scripts │ Apps │ Editor │ Analyzer │ Audit │ Debugger │ Sandbox │ Diff │ Vault │
Security │ Structure │ DOCs │ SQLite │ Reports │ Metadata │ Search
```

Sixteen tabs, each one file in `tabs/`, talking to a UI-free engine.

---

## Table of contents

- [Features](#features)
- [Screens & tabs](#screens--tabs)
- [Installation](#installation)
- [Quick start](#quick-start)
- [Search engine](#search-engine)
- [Code navigation](#code-navigation)
- [Syntax layer](#syntax-layer)
- [Editor](#editor)
- [Debugger](#debugger)
- [Sandbox](#sandbox)
- [Vault](#vault)
- [Security](#security)
- [Structure](#structure)
- [DOCs](#docs)
- [SQLite](#sqlite)
- [Reports](#reports)
- [Metadata](#metadata)
- [Project layout](#project-layout)
- [API reference](#api-reference)
- [Testing](#testing)
- [Extending](#extending)
- [Troubleshooting](#troubleshooting)
- [Conventions](#conventions)

---

## Features

| Area | What you get |
|---|---|
| **Library** | Store scripts with name, category, tags, description and edit history. Favourites, search/filter, import & export, row numbering. Importing a file, a folder or a JSON export assigns the category/language from the file extension automatically. |
| **Run** | One `Run...` button, like `Import...`, whose menu holds both ways to execute: in the runner window (live output, stdin, stop, 300 s timeout) or in a new console window. The script context menu offers the same two entries under `Run...`. |
| **Editor** | Syntax colouring for 13 languages, line numbers, ASCII minimap, bracket-pair highlight, smart indent, auto-close, smart suggestions, dark/light/low-graphics themes, find & replace with regex. |
| **Search** | Sixteen search kinds run over the whole database, a selection, the open buffer, a directory (recursive) or every open project/workspace — text, string (word/line/prefix/suffix), regex, multiline, multi-file, directory, workspace, semantic, imports, blocks, comments, AST, calls, definitions, references, TODO/FIXME. |
| **Navigation** | Ten code jumps — definition, reference, import, class, function, variable, error, warning, section, enclosing block — plus `F12`, `Ctrl+Click`, `Alt`+arrow shortcuts and a jump-to-problem button. |
| **Analysis** | A quality/structure/dependency analyser and an audit timeline that records every change. |
| **Debugging** | An offline debugging workspace: breakpoints, watches, stepping, stack and a trace log. |
| **Sandbox** | Run a script in an isolated, throw-away workspace under an explicit policy (fs/net/syscalls/env/process/logs). |
| **Security** | Harden the scripts: SHA-256/512/SHA-3/BLAKE2 digests, HMAC, PBKDF2/scrypt KDF, sealing, signatures, a hash registry, tamper detection, backups and reports. |
| **Structure** | A mini build system for file trees: presets, a token template engine, a validator, a pipeline, plugins and git/CI/IDE/Docker emitters. |
| **Documents** | A portable, optionally encrypted SQLite document repository with an offline full-text index and preview engine. |
| **Databases** | A SQLite workbench with an offline data layer, an online data layer and a sync layer joining them. |
| **Diff** | A side-by-side and unified diff view, also drivable from the command line. |
| **Metadata** | Read, validate and bulk-write the header metadata block that lives inside the script files. |
| **i18n** | Full English and Polish translation tables. The choice is remembered across restarts. |
| **Hot-plugging** | Add a language, a search kind or a navigation scanner at run time without editing the GUI. |

---

## Screens & tabs

The main window opens on a `QTabWidget` with sixteen tabs, each built by its
own module in `tabs/`. The order matches the running application:

| # | Tab | Module | Purpose |
|---|---|
| 0 | **Scripts** | `sdb_scripts.py` | The library: list, filter, create, edit metadata, import/export. |
| 1 | **Apps** | `tabs/apps.py` | A personal launcher with original system icons, groups, popup folders, grid/list views, sorting, search and icon sizing. |
| 2 | **Editor** | `tabs/editor.py` | Edit one script at a time; search and navigate within it. |
| 3 | **Analyzer** | `tabs/analyzer.py` | Quality and structure analysis of the scripts. |
| 4 | **Audit** | `tabs/audit.py` | Event timeline of everything that changed. |
| 4 | **Debugger** | `tabs/debugger.py` | An offline debugging workspace: breakpoints, watches, stepping, stack and a trace log. |
| 5 | **Sandbox** | `tabs/sandbox.py` | Run a script in an isolated, throw-away workspace under an explicit policy (see [Sandbox](#sandbox)). |
| 6 | **Diff** | `sdb_scripts.py` | Compare two scripts, side by side or unified. |
| 7 | **Vault** | `tabs/vault_tab.py` | The encrypted, isolated private safe: master password, auto-lock, failed-attempt lockout, optional 2FA, five internal categories, search, analysis, reports, integration and gated import/export. |
| 8 | **Security** | `tabs/secure_tab.py` | Harden the scripts: digests, HMAC, KDF, sealing, signatures, a hash registry, tamper detection, backups and reports (see [Security](#security)). |
| 9 | **Structure** | `tabs/structure_tab.py` | Generate project file trees from presets: a template engine, validator, pipeline, plugins and git/CI/IDE/Docker emitters (see [Structure](#structure)). |
| 10 | **DOCs** | `tabs/docs_tab.py` | A document repository: organise, store, archive, manage and edit documents of every common type in one portable SQLite file, optionally password protected, with an offline documentation engine (see [DOCs](#docs)). |
| 11 | **SQLite** | `tabs/sqlite_tab.py` | A database workbench: an offline data layer over files, an online data layer over an API, and the sync layer joining them (see [SQLite](#sqlite)). |
| 12 | **Reports** | `tabs/reports_tab.py` | A repository of generated report artifacts: list, group and filter every report (see [Reports](#reports)). |
| 13 | **Metadata** | `tabs/metadata_tab.py` | Read, validate and bulk-write the header metadata block inside the script files (see [Metadata](#metadata)). |
| 15 | **Search** | `tabs/search_tab.py` | Run the sixteen search kinds across the database, a selection, the open buffer, a directory or the workspace. |

The **ToDo** workspace opens in a separate window. Its internal views include
Calendar, Tasks, Editor, Plan, Notes, Expenses and Shopping; the shopping list
keeps its data in `shopping_db.json`.

### How an imported script gets its category

Every import path (`Import... ▸ Source file(s)...`, `Import... ▸ Whole folder...`
and the JSON export) puts a script in the right language by itself. The name
decides first, because a file name is the strongest signal about what a script
is — `deploy.ps1` is PowerShell whatever the first line looks like:

1. **the extension** — the canonical table (`.py`, `.sh`, `.ps1`, `.vbs`, `.js`,
   `.java`, `.cpp`, `.bat`, `.html`, `.hta`, `.reg`, `.json`, `.yaml`, `.ini`,
   `.cfg`, `.sql`, `.rb`) and then the alias table (`.pyw`, `.bash`, `.zsh`,
   `.cmd`, `.ts`, `.mjs`, `.htm`, `.h`, `.hpp`, `.yml`, `.toml`, `.inf`, `.env`,
   `.rake`, `.sqlite`, `.wsf`, …);
2. **the content** — when the extension is missing or unfamiliar: a `#!` shebang
   (`bash` → Bash, `python` → Python), `<html>` → HTML, `<HTA:APPLICATION>` →
   HTA, `Windows Registry Editor` → Registry, `SELECT`/`CREATE TABLE` → SQL,
   `Write-Host` → PowerShell, `@echo off` → Batch, `console.log` →
   JavaScript, `public static void main` → Java, `#include <` → C++,
   `Option Explicit` → VBScript, `import`/`def` → Python, a JSON document → JSON;
3. **`Other`** — when neither says anything.

A category the import carried itself is always kept, including one that is not in
the list (it is added to the list rather than discarded), so a hand-made export
never loses its grouping.

---

## Installation

**Requirements**

- Python 3.8 or newer (developed and tested on 3.13)
- PyQt5

```bash
pip install PyQt5
```

No other third-party package is needed. The whole project is standard library
plus PyQt5.

**Get the code**

```bash
git clone <your-repository-url> sbd-new
cd sbd-new
```

---

## Quick start

**GUI**

```bash
python sdb_scripts.py
```

**Command line (diff mode)**

```bash
python sdb_scripts.py old_script.py new_script.py
```

With two file arguments the program behaves like `diffgui file1 file2`. The
entry point is `run_cli(argv)`, which returns an exit code instead of calling
`sys.exit`, so it can be embedded in another application.

```bash
# Print a unified diff instead of opening the window
python sdb_scripts.py old.py new.py --no-gui --format unified
# Only the statistics
python sdb_scripts.py old.py new.py --stats
# Write a report, or diagnostic artefacts as JSON
python sdb_scripts.py old.py new.py --report changes.patch
python sdb_scripts.py old.py new.py --diagnostics diag.json
```

| Option | Meaning |
|---|---|
| `--algorithm` | `myers` (default), `patience`, `histogram` |
| `--format` | `gui` (default), `text`, `unified`, `json` |
| `--no-gui` | Print the diff instead of opening the window |
| `--ignore-ws` / `--ignore-case` / `--ignore-comments` / `--ignore-blank` | Normalise before comparing |
| `--stats` | Print the diff statistics only |
| `--report PATH` | Write a report (`.patch`, `.json` or `.txt`) |
| `--diagnostics PATH` | Write diagnostic artefacts as JSON |

With no arguments at all the CLI prints its help and returns 0.

**As a library**

```python
from tabs import search_engine, nav_engine

documents = [
    ("alpha.py", "def greet():\n    return 1\n", "Python"),
]

result = search_engine.search(documents, "greet", "definitions")
print(result.summary())            # "1 hit(s) in 1 document(s)"

target = nav_engine.locate(documents[0], "definition", 2, 5, "greet")
print(target)                      # NavLocation('function', 'greet', 1:0)
```

---

## Search engine

`tabs/search_engine.py` — sixteen search kinds, one small pure-Python engine,
**no Qt import at module level**, so every search is unit-testable and drivable
from a shell.

```python
search_engine.search(documents, query, kind="text", **options)
```

`documents` may be `(name, code, category)` triples, dicts, or objects with
those attributes. Unknown kinds and bad patterns come back as a `SearchResult`
carrying an `.error` instead of raising, so the GUI never has to guard the call.

### The sixteen kinds

| Kind | Query needed | What it does | Extra option |
|---|---|
| `text` | yes | Plain substring search, one hit per line. | `case_sensitive` |
| `string` | yes | A literal string with a match mode: substring / whole word / whole line / starts with / ends with. Nothing is a pattern, so punctuation is safe to paste. | `match` (`substring`/`word`/`line`/`prefix`/`suffix`) |
| `regex` | yes | One regular expression, evaluated per line. Capture groups are shown in the detail column. | `case_sensitive` |
| `multiline` | yes | A pattern allowed to span line breaks (`re.DOTALL` over the buffer). | `max_matches`, `dot_all` |
| `multifile` | yes | The textual search across several documents at once — in memory, or across an explicit `paths` list (files or directories, walked recursively). | `mode` (`text`/`string`/`regex`/`multiline`), `paths` |
| `files` | yes | Every matching file below one directory, **recursively**. | `root`, `mode`, `extensions`, `max_files` |
| `workspace` | yes | Every matching file below one or more open project/workspace roots, recursively. | `roots`, `mode`, `extensions`, `max_files` |
| `semantic` | optional | Symbols by kind, from how the code *declares* things (functions, classes, methods, variables, constants, imports). | `symbol_kind` (`any`/`function`/`class`/`method`/`variable`/`constant`/`import`) |
| `imports` | optional | Every `import` / `from ... import` / `#include` / `using` / `require` / `source` / `load` statement, per language. | `import_kind` (`any`/`import`/`from`/`include`/`using`/`require`) |
| `blocks` | optional | Code blocks: the line that opens each `if` / `for` / `while` / `try` / `catch` / `switch` / function block. | `block_kind` (`any`/`if`/`for`/`while`/`try`/`catch`/`switch`/`function`) |
| `comments` | optional | Every comment, with commented-out code flagged and Python docstrings recognised via the AST. | `comment_kind` (`any`/`inline`/`block`/`docstring`/`empty`) |
| `ast` | optional | Full Python AST walk with a node-kind filter. | `node_kind` (`any`/`function`/`class`/`call`/`import`/`assign`/`name`/`constant`/`loop`/`conditional`) |
| `calls` | yes | Every call site of a name, with the argument text. | `include_methods` |
| `definitions` | yes | Where a name is defined, in the language of each document. | — |
| `references` | yes | Every mention of a name, definition sites flagged. | `mark_definitions` |
| `todo` | optional | `TODO` / `FIXME` / `XXX` / `HACK` / `BUG` / `NOTE` / `OPTIMIZE` / `REVIEW` / `DEPRECATED` / `WARNING`. | `markers` |

### Searching on disk

The two filesystem kinds are also available as plain functions, so a script can
run the same search the Search tab does:

```python
from tabs import search_engine
# Recursive search below one directory.
result = search_engine.search_filesystem(
    r"C:\\projects", "def main", mode="regex", extensions=("py", "js"))

# Every open project / workspace root at once.
result = search_engine.search_workspace(
    [r"C:\\projects", r"D:\\work"], "TODO", mode="text")

# An explicit list of files and folders (recursively), via the multi-file kind.
result = search_engine.search_multifile(
    [], "needle", paths=["a.py", r"src\\"], mode="string", match="word")
```

### Results

```python
result.kind          # the kind that ran
result.query         # the query
result.hits          # list[SearchHit]
result.count         # len(hits)
result.documents()   # document names with at least one hit
result.by_kind()     # {"definition": 3, "call": 2, ...}
result.summary()     # "7 hit(s) in 2 document(s)"
result.error         # "" or a human-readable message
```

Each `SearchHit` carries `doc`, `line` (1-based), `column` (0-based), `text`,
`kind` and `detail`.

### Hot-plugging a search kind

```python
from tabs import search_engine

def find_license(documents, query, **options):
    hits = []
    for name, code, _category in search_engine.normalize_documents(documents):
        for number, line in enumerate((code or "").splitlines(), 1):
            if "copyright" in line.lower():
                hits.append(search_engine.SearchHit(
                    name, number, 0, line.strip(), "license"))
    return search_engine.SearchResult("license", query, hits)

search_engine.register_kind("license", find_license)
```

The kind is immediately runnable through `search_engine.search(...)` and appears
in the Search tab after `refresh()`.

---

## Code navigation

`tabs/nav_engine.py` — ten jump kinds, same design rules as the search engine:
pure Python, no Qt at module level, cached scans.

```python
nav_engine.locate(document, kind, line, column=0, word="", direction=1, wrap=True)
```

### The ten jumps

| Kind | Follows the symbol? | Behaviour |
|---|---|---|
| `definition` | yes | The declaration behind the symbol under the cursor. |
| `reference` | yes | The next use of the symbol (the declaration is skipped). |
| `import` | yes | The import line, or the name an import brings in. |
| `class` | no | The next (or previous) class. |
| `function` | no | The next (or previous) function. |
| `variable` | no | The next (or previous) variable. |
| `error` | no | The next (or previous) parse error. |
| `warning` | no | The next (or previous) marker warning. |
| `section` | no | The next (or previous) comment banner / `# region`. |
| `enclosing` | no | The innermost section the cursor sits inside. |

### Public API

```python
nav_engine.symbols(document, kind="definition")   # declarations, in document order
nav_engine.references(document, name, limit=0)    # mentions, definitions pinned first
nav_engine.enclosing(document, kind, line, col)   # innermost class/function around a cursor
nav_engine.imports(document, query="")            # import targets, optionally filtered
nav_engine.problems(document, kind="error")       # "error", "warning" or "any"
nav_engine.sections(document)                     # banners and region markers
nav_engine.symbol_at(document, line, col, word)   # the definition under a cursor
nav_engine.word_at(text, offset)                  # (name, start, end) at an offset
nav_engine.word_at_cursor(text, line, column)     # the identifier at a position
nav_engine.summary(document)                      # per-kind counts for a status line
nav_engine.capabilities()                         # kinds, languages, scanners
```

### Keyboard shortcuts in the Editor

| Shortcut | Jump |
|---|---|
| `F12` | Go to definition |
| `Shift+F12` | Go to reference |
| `Ctrl` + click | Go to definition (pointer position) |
| `Alt+Up` | Enclosing section |
| `Alt+Left` / `Alt+Right` | Previous / next section |
| `Alt+Down` | Repeat the last jump |
| `Alt+E` | Go to error |
| `Alt+W` | Go to warning |
| `Alt+C` | Go to class |
| `Alt+F` | Go to function |
| `Alt+V` | Go to variable |
| `Alt+I` | Go to import |

### Hot-plugging a scanner

```python
from tabs import nav_engine

def my_scanner(text, key):
    return {"section": [nav_engine.NavLocation("section", "PLUG", 1, 0, 1, 0)]}

nav_engine.register_scanner("python", my_scanner)
nav_engine.invalidate()
```

---

## Syntax layer

`tabs/syntax/` — one file per language, one `SyntaxModule` subclass per file,
wired together by `registry.py`. Thirteen languages ship with the app:

```
python  cpp  java  javascript  powershell  batch  vbscript
json    bash html  ruby        sql         system
```

`system` covers Windows registry, INF, ENV and CONF files.

### Using the registry

```python
from tabs import syntax

syntax.module_for("C++").key                  # "cpp"   (category -> module)
syntax.module_for_extension("app.hta").key    # "html"  (filename -> module)
syntax.key_for("Batch")                       # "batch"
syntax.languages()                            # [("python", "Python"), ...]
syntax.highlight_spans("def f():", "Python", theme)   # [(0, 3, "keyword"), ...]
```

### Adding a language

```python
from tabs import syntax

class NitModule(syntax.SyntaxModule):
    key = "nit"
    label = "NIT"
    aliases = ("nit",)
    keywords = ("module", "forall")
    builtins = ("cout",)
    comment_marker = "#"

syntax.register_module(NitModule)     # editor picks it up immediately
syntax.unregister_module("nit")       # and it can be removed again
```

Nothing in `editor.py` or `search_tab.py` has to change: both read the registry.

---

## Editor

`tabs/editor.py` — the editing surface, and the place where the syntax, search
and navigation engines meet.

| Feature | Detail |
|---|---|
| Syntax colouring | Delegated to the active `SyntaxModule`; theme aware. |
| Line numbers | Gutter with the current line highlighted. |
| Minimap | ASCII overview with the visible range marked. |
| Bracket pairs | The matching bracket is highlighted at the cursor. |
| Smart indent | Keeps indentation; adds one level after a block opener. |
| Auto-close | Brackets and quotes are paired; the cursor stays inside. |
| Smart suggestions | A popup built from the buffer (functions, classes, variables, keywords, imports). `Ctrl+Space` opens it on demand. |
| Find & replace | Plain or regex, case-sensitive or not, single or replace-all. |
| Themes | `dark`, `light`, and a monochrome `low graphics` mode. |
| Views | Line numbers, current line, bracket pairs, minimap, auto-close, smart indent and suggestions are individually toggleable. |
| Navigation | The ten jumps above, with the shortcut set listed in that section. |
| Search panel | The sixteen search kinds run against the open buffer or the whole database. |
| Code generation | Six generators that derive code from something the project already knows. |
| Code rules | Ten automatic lint + fix rules, run one at a time or all at once. |

### Code generation
`tabs/code_generator.py` — one step above the template engine: a template is a
fixed block of text, the generator *derives* the code from a definition.

| Generator | Reads | Produces |
|---|---|---|
| Config -> initialisation | `config.json` / `.yaml` / `.ini` / `.env` | a settings module: one constant per key, `DEFAULTS` and `load_config()` |
| Metadata -> project module | project metadata (JSON or key/value) | a descriptor module with `NAME`, `VERSION`, `describe()` |
| JSON/YAML -> data classes | any JSON or YAML document | one `@dataclass` per object, with `from_dict` / `load` / `dump` |
| API -> client | OpenAPI, a JSON endpoint list, or `GET /path` lines | a client with one method per endpoint, plus a handler skeleton |
| Modules -> package | a module definition | a package: `__init__`, a starter per module, `modules.json` |
| Project -> scaffold | a project directory or a name | README, `config.json`, entry point, package and a test |

Every artifact is plain text: a single-file result is inserted at the cursor, a
multi-file result (a package, a scaffold) opens in a tree that can be previewed
and saved to a folder. Nothing is ever executed, and a source that cannot be
understood reports a clear error instead of guessing.

### Code rules
`tabs/code_rules.py` — an automatic linter *and* fixer, deliberately without AI:
every rule is a stated, inspectable transform that only rewrites the buffer when
it can prove the change is safe. For Python the buffer is re-parsed after every
step, so a rule that would break the parse is rolled back and reported.

| Rule | What it fixes |
|---|---|
| Add missing / remove unused imports | imports a used name needs, and imports nothing references |
| Normalise formatting | indentation characters, trailing spaces, blank-line runs |
| Fix indentation | lines that do not match their block depth |
| Add missing return | a function body that can fall off the end |
| Add missing break | a C-style `case` arm that would fall through |
| Add missing except | a bare `try` (the handler re-raises, so failures still surface) |
| Add missing docstrings | module, class and function docstrings derived from the name |
| Add missing type hints | parameter and return hints the body actually proves |
| Add missing config sections | the sections an `.ini`, `.env` or JSON config lacks |
| Fix remaining style issues | `== None`, `== True` / `== False`, trailing whitespace |

The menu runs one rule at a time, or all of them, or reports what they *would*
change without touching the buffer. Rules that only make sense for Python are
greyed out for other languages.

---

## Debugger
`tabs/debugger.py` (the tab) plus the supporting engines `tabs/debug_langs.py`
(language adapters), `tabs/debug_steps.py` (the step / replay model) and
`tabs/breakpoints.py` (the breakpoint model). The tab prepares and inspects an
offline debugging session against the scripts the Audit tab verifies.

| Area | Detail |
|---|---|
| Breakpoints | Line, function, conditional, logpoint and exception breakpoints, each validated and matched against a recorded event stream. |
| Watches | Expressions evaluated against the session state. |
| Stepping | Step over / into / out, driven by `debug_steps.eval_condition` and the session snapshots. |
| Stack | The call stack reconstructed from the recorded events. |
| Trace log | A code-flow recording (`tabs/code_flow.py`, `tabs/code_flow_view.py`) shown as a timeline, tree or map. |
| Snapshots | Named points a session can rewind to and restore. |
| Replay | Replay a recorded session step by step under a chosen mode. |

The tab is offline and never executes a script: it inspects an event stream, so
it is safe to run against any snippet.

## Sandbox
`tabs/sandbox.py` and its supporting modules — an isolated environment for
running a script safely. Every run happens in a throw-away working directory,
inside a separate child process, under a small explicit policy, with a hard
timeout and a cap on captured output.

| Module | What it contributes |
|---|---|
| `tabs/sandbox.py` | The tab and the run loop. |
| `tabs/sandbox_langs.py` | Interpreter resolution and command building per language. |
| `tabs/sandbox_isolation.py` | The scrubbed environment, isolated PATH and the security profile. |
| `tabs/sandbox_fs.py` | The filesystem verdict: allow-lists, deny rules, no escape. |
| `tabs/sandbox_net.py` | The network verdict: host and port allow-lists. |
| `tabs/sandbox_syscall.py` | The syscall verdict: category mapping and entry points. |
| `tabs/sandbox_monitor.py` | The runtime monitor: limits (CPU, memory, I/O, files, processes, threads) and their units. |
| `tabs/sandbox_audit.py` | The run audit: violations, findings and the report. |
| `tabs/sandbox_integration.py` | The scopes (script / module / project / session) and the shadow layers (fs / env / net). |
| `tabs/sandbox_server.py` | The session object that prepares and records a run. |

Policy presets are `strict`, `standard` and `permissive`; the isolation switches
(separate process / environment / PATH / working directory / runtime, blocked
terminal) are on by default and no preset loosens them.

## Security
`tabs/secure_tab.py` (the tab) and `tabs/secure_core.py` (the engine) — the
toolkit that hardens the scripts the database holds. It is standard library only
and never executes a script.

| Layer | Detail |
|---|---|
| Digests | SHA-256, SHA-512, SHA3-256 and BLAKE2b fingerprints of every script. |
| HMAC & signatures | A keyed HMAC-SHA256 tag and a per-script signature over the fingerprint, name-bound and compared in constant time. |
| KDF | PBKDF2-HMAC-SHA256 (`200000` iterations), scrypt and a dependency-free iterated digest, plus a password-strength score. |
| Sealing | An HMAC-SHA256 keystream envelope with an integrity tag; a unique per-file key derived from the master key; an optional second layer under the rotating session key. |
| Integrity registry | A `registry.json` hash registry with versions, tamper detection (`intact` / `changed` / `unsigned` / `foreign` / `missing`) and alarms. |
| Authorization | A master passphrase, an optional second-factor token, an attempt limit with a cool-down clock, auto-lock and high-security mode. |
| Shadow keys | One-time / session / project keys, issuable and revocable. |
| Backups & versioning | Timestamped backups, time-travel restore and a bounded version history. |
| Reports | Integrity, changes, access, errors, warnings and risk, exportable to JSON. |

A tamper policy can lock the session when a script no longer matches its record.

## Structure
`tabs/structure_tab.py` (the tab) and `tabs/structure_core.py` (the engine) — a
mini build system for file trees. A preset (JSON, TOML or a YAML subset) is
turned into a real project on disk, validated, and compiled through a pipeline.
Everything is standard library only.

| Layer | Detail |
|---|---|
| Config parser | JSON, TOML/INI and a small YAML reader, with `extends` + override + deep merge. |
| Template engine | Its own token system: `${name}`, `$select{a\|b}`, `$if{x}...$end`, `$each{items}...$end`, and a `$${...}` escape for Make / CMake variables. |
| Generator | A preset becomes an ordered tree of folders and files; build / CI / IDE / Docker files are emitted from the preset's settings. |
| Validator | Refuses escaping paths (`..`, absolute), name conflicts, empty files and unfilled tokens; reports files promised but missing on disk. |
| Compiler / pipeline | Five stages (pre, generate, validate, post, artifacts) with a run log and a dry-run whose plan is exactly what a commit would do. |
| Preset repository | Four scopes (user / organization / project / global), versioned with history; nine built-in presets across Python, C++, Node, Java, Rust, Unity, Godot, a C/C++ library and a plugin. |
| Plugins | A hot-plug registry of generators and hooks. |
| Integrations | Git init, GitHub / GitLab / Azure CI, Docker + compose, VS Code / PyCharm / IntelliJ, and CMake / Make / Gradle / npm / pip / Cargo build systems. |
| Intelligence | Reverse analysis of an existing folder, missing-file suggestions, preset migrations and generation from a one-line description. |
| Artifacts | ZIP export, preset packs, Markdown documentation and a generation log. |

A command-line front end (`structure_core.cli`) runs the same `run_pipeline` the
GUI uses: `init`, `generate`, `validate`, `compile`, with `--dry-run`, `--json`
and `--zip`.

---

## Vault
`tabs/vault_tab.py` (the tab) and `tabs/vault_core.py` (the engine) — a private
safe. It reuses what the app already has instead of duplicating it: the crypto
from `tabs/docs_tab.py`, the encrypted SQLite repository, atomic writes, the
export/import shape of `tabs/docs_actions.py`. What it adds is the policy.

| Layer | Detail |
|---|---|
| Encryption | PBKDF2-HMAC-SHA256 key derivation + an HMAC-SHA256 keystream with an integrity tag, over the master password. Nothing is written that could be read without the password. |
| Master password | Never stored. Only a salt and a verifier (an HMAC of a fixed label) are written, so a locked vault can be unlocked again without the key living in memory. |
| Sessions | A rotating session key issues on every unlock and expires after `SESSION_KEY_LIFETIME`, at which point it is replaced. |
| Auto-lock | A single timer asks the core whether the session has gone idle, so the rule lives in one place. Locking drops the key from memory and clears every list. |
| Failed attempts | A counter with a configurable allowance and lockout clock. A locked-out vault refuses even the correct password until the clock runs out. |
| Optional 2FA | When required, a correct password pauses the unlock and issues a challenge; a wrong code counts as another failed attempt. |
| Isolation | The store lives under its own directory, created owner-only, and `is_isolated()` / `path_is_inside()` prove it. Export outside that directory is refused unless it is explicitly permitted. |

**The five internal categories** are `Scripts`, `Documents`, `Files`, `Passwords`
and `Snapshots`. Every resource carries `pinned`, `critical`, `hidden` and
`readonly` flags, and a list refresh keeps the selection across a rebuild.

| Resource management | Add, edit, copy, move between categories, delete (confirmed), tag, pin, mark critical, hide, read-only lock |
|---|---|
| Search | By name, content, tags, type, date or metadata — one aspect or all of them. A search always covers hidden resources too, because a hidden secret that cannot be found is a lost secret; the status line says how many were hidden. |
| Analysis | Document structure, script integrity (parsed with `ast`, never executed), security risk (a stated pattern list with severities and a score) and change history against the snapshots. |
| Integration | Send a resource to the Editor, Debugger, Sandbox, Audit, Diff, or generate a report. The vault checks the session before it hands anything out, so a target cannot pull content on its own. |
| Import / export | Import auto-encrypts. An export inside the vault needs the session; an export *outside* it re-asks for the master password. Snapshots and the vault configuration export separately. |

The five reports are `Security`, `Integrity`, `Access`, `Changes` and `Risk`, and
they are built from the same two sources as the UI — the core's policy and
records, and the store's contents — so a report cannot claim a protection the
vault is not running.

**Premium protections** (each a named switch): double-layer encryption (seal
under the master key, then under the session key), one-time keys (minted, spent
once, a replay recorded as an intrusion), auto-destruction after the lockout
limit, intrusion detection and manipulation detection (a body that no longer
matches its recorded SHA-256 digest).

## DOCs
`tabs/docs_tab.py` — a document repository. It organises, stores, archives,
manages and edits documents of every common type in one portable SQLite file
that other database programs can open, optionally password protected.

| Part | Detail |
|---|---|
| Repository | `DocsRepository(path, password=None)` — one SQLite file holding every document, with the same PBKDF2 + HMAC keystream encryption the vault reuses. |
| Types | `tabs/docs_types.py` — the offline registry of nine document groups (text, config, log, report, audit, project, snapshot, script, system), each with a preview strategy and a translation key. |
| Engine | `tabs/docs_engine.py` — the offline knowledge engine (see the [API reference](#tabsdocs_engine----offline-documentation-engine)). |
| Actions | `tabs/docs_actions.py` — open, edit, delete, move, copy, mark important. |
| Search | `tabs/docs_search.py` — the document query parser and facets. |
| Integration | `tabs/docs_integration.py` — hand a document to Diff, Search, Audit or the Analyzer. |

The content index uses SQLite **FTS5** when the build provides it and falls back
to a portable inverted-index table otherwise, so full-text search behaves the
same everywhere.

## SQLite
`tabs/sqlite_tab.py` and its three layers — the database workbench.

| Layer | Module | Detail |
|---|---|---|
| Offline | `tabs/sqlite_offline.py` | Databases that live in files (SQLite, CSV/JSON document snapshots), with read-only and create modes. |
| Online | `tabs/sqlite_online.py` | A database behind an HTTP API, with base URL, sign-in state and per-table URLs. |
| Sync | `tabs/sqlite_sync.py` | Row-level sync between the two: key columns, row signatures and the available modes. |

The tab provides table browsing, a row editor, query tools, logs and settings.

## Reports
`tabs/reports_tab.py` plus `tabs/reports_analysis.py` (report analysis, cross-module
integration and report operations) and `tabs/reports_premium.py` (automatic
generation, the scheduler, comparative, environment-diff and security reports).

| Function | Purpose |
|---|---|
| `ReportsTab(window)` | The "Reports" tab: browse, group and filter report artifacts. |
| `ReportRepository(dirs).scan()` | Every report below the watched directories, newest first. |
| `classify(name, head)` / `classify_tags(name, head)` | Report type and tags from the name and content. |
| `filter_reports(reports, quick, kind, text, name, content, problems, since)` | The quick filters (`last24h` / `lastweek` / `errors` / `tests` / `diff` / `analysis`) plus the type / project / module / tag / text / name / content / problems / since filters. |
| `group_reports(reports, key)` | Group by project / module / type / date / tag. |
| `search_by_name` / `search_by_content` / `search_by_tags` / `search_by_type` / `search_problems` | The five search axes. |
| `generation_history(reports)` / `search_history(reports, needle, kind, since)` | The generation timeline and a search over it. |
| `find_errors` / `find_warnings` / `find_duplicates` / `detect_changes` (analysis) | Report analysis helpers. |
| `write_report` / `comparative_report` / `capture_environment` / `env_diff` (premium) | Automatic and comparative report generation. |
| `tab.send_to_docs()` / `tab.export_index()` | Store the selected report in DOCs / write an index of the visible reports. |

## Metadata
`tabs/metadata_tab.py` (the tab) and `tabs/metadata_core.py` (the engine). It
edits the header metadata block that lives *inside* the script files themselves:
it reads, validates and writes that block, and it is the only place a bulk
metadata change is made.

| Function | Purpose |
|---|---|
| `detect_language(path, text)` | Detect the file's language from its name and content. |
| `comment_style(key)` | The comment marker a language uses for the header. |
| `parse_header(text, language)` / `header_values(fields)` | Read the header block. |
| `render_header(values, language, custom, width)` / `apply_header(text, values, language, custom)` | Render and write the header block. |
| `MetadataTab(window)` | Scan a folder, show the header table, edit and validate in bulk, and export. |

## Project layout

### Where the program stores its files

The application ships as a single frozen executable, so the folder it sits in is
never used for data. The locations are fixed and always belong to the user
running the program, so an exe handed to somebody else writes under their
profile and not yours. There are two levels:

```
%APPDATA%\.polsoft\ScriptDB\        = C:\Users\<userprofile>\AppData\Roaming\.polsoft\ScriptDB\
%APPDATA%\.polsoft\ScriptDB\db\     the databases
```

The profile folder is read from `%APPDATA%` and `%USERPROFILE%` (with
`%HOMEDRIVE%%HOMEPATH%` and `~` as fallbacks), so a local account, a domain
account and a redirected profile all resolve correctly. On Linux and macOS the
same two levels sit below `%XDG_DATA_HOME%` instead.

The `db` folder holds everything that is program state: `scripts_db.json`,
`settings_db.json`, `connections_db.json`, `todo_db.json`, `shopping_db.json`,
`apps_db.json`, `debugger_db.json`, `batch_templates.json`, `docs_db.sqlite`,
the audit log and timeline, and the `vault\`, `security\` and `structure\`
subfolders.

`settings_db.json` is the one that makes the program *yours*. It is written on
every change and read back before any widget is built, so a restart brings back:

| Setting | Key | Note |
|---|---|---|
| Language | `lang` | `en` / `pl`, the button in the top bar |
| Colour theme | `theme` | Dark, Light or Monochrome |
| Compact layout | `compact_ui` | Denser spacing throughout the window |
| Window geometry and tab layout | `window_geometry` | Size, position and the tab strip order |
| Tab left open | `last_tab` | The program opens where you were working |
| Filter text and favourites-only | `filter_text`, `filter_favorites` | The list opens on the view you were looking at |
| Sort direction | `sort_ascending` | A-Z or Z-A |
| Visible columns, group by category, separators, zebra stripes, row numbers | `columns`, `group_by_category`, `separators_enabled`, … | The list layout |

`settings_db.json` is plain JSON, so it can be edited by hand. Every value is
checked on load — wrong type, or an unknown language code, and the default is
used instead. A hand-edited file can therefore never stop the program starting.

The folder above it holds what the program produces: `reports\`, `exports\`,
`audit_exports\`, `analyzer_exports\`, `config_snapshots\`, `audit_snapshots\`,
`snapshots\`, the diff and search output, and the code-flow traces.

Both folders are created on first run and the databases bundled in the exe are
copied into `db\` only when they are not already there, so an existing
installation is never overwritten.

`app_paths.py` is the single place that knows these locations (`data_dir()`,
`db_dir()`, `ensure_data_dir()`, `ensure_db_dir()`, `data_file()`,
`db_file()`, `seed_data_dir()`, and `user_profile_dir()` / `roaming_app_data()`
for the profile behind them). Nothing else in the program computes a data folder
of its own.

### Source tree

```
sdb-new/
├── sdb_scripts.py            # main window, Scripts tab, Diff tab, CLI entry point
├── app_paths.py              # the application data folder (all runtime files)
├── scripts_db.json           # the script library (created on first run)
├── connections_db.json       # saved connections (created on first run)
├── settings_db.json          # window settings (created on first run)
├── audit_timeline.json       # the audit event log
├── debugger_db.json          # the debugger session store
├── docs_db.sqlite            # the DOCs document repository (created on first run)
├── run_checks.ps1            # run every automated check (tests, verifications, syntax)
├── README.md                 # this file
├── builder/                  # PyInstaller build (build.bat, ScriptDB.spec, sdb.ico)
├── reports/                  # generated report artifacts (created on first run)
├── exports/                  # generated exports (created on first run)
├── verifications/            # the test suite (see Testing)
│   ├── verify_structure.py
│   ├── verify_security.py
│   ├── verify_translations.py
│   ├── verify_sandbox_isolation.py
│   ├── verify_debugger.py
│   ├── verify_docs_search.py
│   ├── verify_docs_integration.py
│   └── verify_docs_actions*.py
└── tabs/
    ├── __init__.py           # exports the 13 <Name>Tab widgets (see the tab table)
    ├── editor.py             # CodeEditor + EditorTab
    ├── search_tab.py         # SearchTab
    ├── search_engine.py      # the sixteen search kinds
    ├── nav_engine.py         # the ten navigation jumps
    ├── templates.py          # the editor's template engine
    ├── code_generator.py     # the six code generators
    ├── code_rules.py         # the ten automatic code rules
    ├── analyzer.py           # ScriptAnalyzerTab
    ├── audit.py              # AuditTab + AuditEventBus
    ├── debugger.py           # DebuggerTab
    ├── debug_langs.py        # language adapters for the debugger
    ├── debug_steps.py        # step / replay model
    ├── breakpoints.py        # the breakpoint model
    ├── code_flow.py          # the code-flow recording
    ├── flow_view.py          # the flow graph model (timeline / tree / map)
    ├── sandbox.py            # SandboxTab + the run loop
    ├── sandbox_langs.py      # interpreter resolution
    ├── sandbox_isolation.py  # scrubbed environment, isolated PATH, profile
    ├── sandbox_fs.py         # the filesystem verdict
    ├── sandbox_net.py        # the network verdict
    ├── sandbox_syscall.py    # the syscall verdict
    ├── sandbox_monitor.py    # the runtime monitor
    ├── sandbox_audit.py      # the run audit
    ├── sandbox_integration.py# scopes and shadow layers
    ├── sandbox_server.py     # the sandbox session
    ├── vault_core.py         # VaultSecurityCore + VaultStore (the vault engine)
    ├── vault_tab.py          # VaultTab (the "Vault" tab)
    ├── secure_core.py        # the script-security engine
    ├── secure_tab.py         # SecurityTab (the "Security" tab)
    ├── structure_core.py     # the project-structure engine
    ├── structure_tab.py      # StructureTab (the "Structure" tab)
    ├── docs_tab.py           # DocsTab + DocsRepository (the "DOCs" tab)
    ├── docs_engine.py        # DocsIndex: the offline documentation engine
    ├── docs_types.py         # the offline document-type registry
    ├── docs_actions.py       # document operations
    ├── docs_search.py        # the document query parser
    ├── docs_integration.py   # hand a document to another tab
    ├── sqlite_tab.py         # SqliteTab (the "SQLite" tab)
    ├── sqlite_offline.py     # the offline data layer
    ├── sqlite_online.py      # the online data layer
    ├── sqlite_sync.py        # the sync layer
    ├── reports_tab.py        # ReportsTab + ReportRepository (the "Reports" tab)
    ├── reports_analysis.py   # report analysis and operations
    ├── reports_premium.py    # automatic / comparative / security reports
    ├── metadata_tab.py       # MetadataTab (the "Metadata" tab)
    ├── metadata_core.py      # the header-metadata engine
    ├── theme.py              # palettes and stylesheets
    ├── script_profiles.py    # the script profiles for the generator
    ├── workflow_engine.py    # the workflow/block model
    └── syntax/
        ├── __init__.py
        ├── base.py           # SyntaxModule protocol
        ├── highlighter.py    # SyntaxHighlighter, SPAN_KINDS, DEFAULT_COLOURS
        ├── registry.py       # SyntaxRegistry + module-level helpers
        └── *_lang.py         # one module per language
```

---

## API reference

### `tabs.search_engine`

| Function | Purpose |
|---|---|
| `search(documents, query, kind, **options)` | The single entry point; never raises. |
| `register_kind(name, finder)` | Hot-plug a search kind. |
| `unregister_kind(name)` | Remove one. |
| `kinds()` | Registered kind names, in UI order. |
| `normalize_documents(documents)` | Coerce triples/dicts/objects to triples. |
| `capabilities()` | Engine description for reports. |
| `SearchHit`, `SearchResult` | Result records. |

### `tabs.docs_tab`

| Function | Purpose |
|---|---|
| `DocsTab(window)` | The "DOCs" tab: organise, store, archive and edit documents. |
| `DocsRepository(path, password=None)` | One SQLite file holding every document; portable and optionally encrypted. |
| `repo.add/update/get/delete/list_documents/search(...)` | CRUD over the repository. |
| `repo.set_password(pw)` / `repo.unlock(pw)` | Enable / remove, and open, password protection. |
| `repo.export_json/csv/sql(path)` | Export to formats other programs can read. |
| `tab.search_documents()` | The documents as `(name, code, category)`, ready for the Search engine. |
| `tab.send_to_editor()` / `tab.send_to_search()` | Hand the selected document to the Editor / Search tab. |

### `tabs.docs_engine` -- offline documentation engine

The Core Knowledge Engine behind the DOCs tab. It keeps three indexes plus a
cache and snapshots in the same SQLite file, and works with no network at all.

| Function | Purpose |
|---|---|
| `DocsIndex(conn)` | Attach the engine to a repository connection and build the indexes. |
| `index.index_document(doc, source_path=None)` | (Re)index one document: catalogue, facets, content and cache. |
| `index.reindex_all(documents)` | Rebuild the whole knowledge base. |
| `index.search(query, category, doc_type, tag)` | Full-text search over the content index, filtered by the metadata index. |
| `index.facets(facet)` / `index.metadata_index()` | The metadata index (category / type / tag / meta:key). |
| `index.cached_text(id)` / `cache_is_fresh(id, body)` / `clear_cache()` | The offline text cache. |
| `index.snapshot(name, docs)` / `snapshots()` / `restore_snapshot(id, add)` | Point-in-time documentation snapshots. |
| `index.stale_sources()` / `read_source(path)` | Detect and read tracked local files that changed on disk. |
| `index.engine_info()` | Index mode (fts5 / inverted), counts, offline flag. |
| `index.preview_ascii(text, width, ascii_only, wrap)` | Offline plain-text / ASCII preview (transliterates to ASCII on request). |
| `index.document_structure(text)` | Structure outline: headings, sections, code blocks, lists, rules. |
| `index.minimap(text, rows, buckets)` | A dense text mini-map of the whole document. |
| `index.document_metadata(doc)` | Counts, dates, tags, checksum and free metadata. |
| `index.document_links(doc, documents)` | What relates to the document (shared tags, category, title mentions). |
| `index.compare_versions(old, new)` | Changed hunks between two versions (additions / removals / changes). |
| `index.snapshot_text(snapshot_id, doc_id)` | A document's body as captured in a snapshot. |
| `index.config_outline(text)` | Key/value outline of JSON / YAML / INI / TOML. |
| `index.log_summary(text)` | Level histogram plus the most recent log lines. |
| `index.table_outline(text)` | Column/row outline of a CSV/TSV document. |
| `index.type_preview(key, text)` | The preview the document type asks for (see `tabs.docs_types`). |

The content index uses **SQLite FTS5** when the SQLite build provides it and
falls back to a portable inverted-index table otherwise, so the full-text search
behaves the same everywhere. The tab exposes it through `tab.engine_search()`,
`tab.reindex_documents()`, `tab.take_snapshot()` / `tab.restore_snapshot()` and
`tab.update_stale_documents()`.

**Offline document preview.** The Preview page of the DOCs tab renders a
document without any renderer or network: `ASCII / plain text` (optionally
transliterated to pure ASCII), `Structure (outline)` (headings, sections, code
blocks and lists), `Mini-map` (a dense shape map of the whole document),
`Metadata` (counts, dates, tags, checksum, free keys), `Links (relations)`
(shared tags / category / title mentions) and `Changes vs snapshot` (a hunk
diff against a stored snapshot), plus `By document type` (the preview the type
asks for). The same functions are on the engine, so the preview is unit-testable
and reusable from a script.

**Supported document types (offline).** `tabs/docs_types.py` is the single
registry of the document types the tab understands; each is a `DocumentType`
with its extensions, group and preview strategy. The nine groups are:

| Group | Examples | Preview |
|---|---|---|
| Text documents | `txt`, `md`, `rst` | text / structure |
| Configuration documents | `json`, `yaml`, `ini`, `toml`, `xml` | key/value outline |
| Log documents | `log`, `out`, `syslog` | level counts + tail |
| Report documents | `report`, `csv`, `tsv`, spreadsheets | table outline |
| Audit documents | `audit`, changelog, policy | text / structure |
| Project documents | `proj`, README, plan/roadmap, diagrams | structure |
| Snapshot documents | `snapshot`, `bak` | key/value / text |
| Script documents | source code, `sql`, notebooks | structure |
| System documents | manifests, metadata, `reg`, `plist` | key/value outline |

Every entry is data, so the supported list can be enumerated and tested without a
GUI: `docs_types.guess_type(name)`, `docs_types.types_in_group(group)` and
`docs_types.capabilities()`. A `DocumentType` also carries a translation key, so
the picker follows the app language.

Every tab is one program: the DOCs documents are searchable from the Search tab,
openable in the Editor tab, and every database change is announced on the shared
audit event bus so the Audit timeline stays complete.

### `tabs.reports_tab` -- report artifact repository

The Reports tab collects every generated report artifact (audit reports,
analyzer output, diff reports, search exports, document previews, log dumps)
and makes them findable. It never regenerates a report; it lists and reads what
already exists. `ReportRepository` scans the watched directories (by default
`reports/`, `audit_exports/`, `analyzer_exports/`, `exports/`) and describes
each file.

| Function | Purpose |
|---|---|
| `ReportsTab(window)` | The "Reports" tab: browse, group and filter report artifacts. |
| `ReportRepository(dirs).scan()` | Every report below the watched directories, newest first. |
| `classify(name, head)` / `classify_tags(name, head)` | Report type and tags from the name and content. |
| `filter_reports(reports, quick, kind, text, name, content, problems, since, ...)` | Apply the quick filter (`last24h` / `lastweek` / `errors` / `tests` / `diff` / `analysis`) plus the type / project / module / tag / text / name / content / problems / since filters. |
| `group_reports(reports, key)` | Group by project / module / type / date / tag (or a flat list). |
| `search_by_name(reports, needle)` | Search by report name. |
| `search_by_content(reports, needle)` | Search inside the report body (an in-memory excerpt, no disk read per keystroke). |
| `search_by_tags(reports, tags, match_all=False)` | Search by one or more tags. |
| `search_by_type(reports, kind)` | Search by report type (`audit`, `diff`, `test`, `build`, `deploy`, `lint`, `metrics`, ...). |
| `search_problems(reports, level, needle)` | Search reports carrying errors and / or warnings (`level` = `any` / `error` / `warning`). |
| `generation_history(reports)` | The generation timeline: reports grouped by the day they were produced. |
| `search_history(reports, needle, kind, since)` | Search the generation history by name / content, type and date. |
| `tab.visible_reports()` | The reports matching the current UI filters. |
| `tab.clear_search()` | Reset every search field. |
| `tab.generation_history()` / `tab.search_history(...)` | The generation timeline of the current view / a history search. |
| `tab.send_to_docs()` | Store the selected report in the DOCs repository. |
| `tab.export_index()` | Write an index of the visible reports to `reports_index.json`. |
| `repository_capabilities()` | The groupings, quick filters and report types, for reports. |

### `tabs.nav_engine`

| Function | Purpose |
|---|---|
| `locate(document, kind, line, column, word, direction, wrap)` | The single entry point. |
| `symbols(document, kind)` | Definitions or references of a document. |
| `references(document, name, limit)` | Mentions of a name, definitions pinned first. |
| `enclosing(document, kind, line, column)` | Innermost class/function around a cursor. |
| `imports(document, query)` | Import targets. |
| `problems(document, kind)` | Errors, warnings or both. |
| `sections(document)` | Comment banners and region markers. |
| `register_scanner(key, scanner)` / `unregister_scanner(key)` | Hot-plug a language scanner. |
| `invalidate(category=None)` | Drop cached scans. |
| `word_at(text, offset)` / `word_at_cursor(text, line, column)` | Identifier helpers. |
| `NavLocation` | Result record (`kind`, `name`, `line`, `column`, `end_line`, `end_column`, `detail`). |

### `tabs.syntax`

| Function | Purpose |
|---|---|
| `module_for(category)` | Category → syntax module (never `None`). |
| `module_for_extension(filename)` | Filename → syntax module. |
| `key_for(category)` | Category → module key. |
| `languages()` | `[(key, label), ...]` for every registered module. |
| `register_module(cls)` / `replace_module(cls)` / `unregister_module(key)` | Hot-plug a language. |
| `load_plugin(path)` | Load a module from a file. |
| `highlight_spans(text, category, theme, low_graphics=False)` | `[(start, length, kind), ...]`. |

### `tabs.secure_core` -- script-security engine
| Function | Purpose |
|---|---|
| `SecurityCore(path)` | The policy, key material and session state (no UI). |
| `core.create(pw)` / `core.unlock(pw, token)` / `core.lock(reason)` | Create / open / close the session. |
| `core.hash_script(script, algorithm)` / `core.sign(script)` / `core.verify_signature(script, sig)` | Digest, sign and verify a script. |
| `core.seal_script(script)` / `core.unseal_script(name, envelope)` | Encrypt / decrypt a script under its per-file key. |
| `core.issue_shadow_key(label)` / `core.revoke_shadow_key(token)` | One-time / session / project keys. |
| `core.detect_manipulation(detail, expected, found)` / `core.detect_intrusion(detail)` | Raise a tamper / intrusion alarm. |
| `SecurityRegistry(path)` | The hash registry: `record_script`, `check`, `verify_all`, `summary`, `push_version`. |
| `digest_text(text, algorithm)` / `hmac_tag(key, message)` / `derive(pw, salt, function)` | The primitives. |
| `seal_text(text, key)` / `unseal_text(envelope, key)` / `per_file_key(master, name)` | The sealing primitives. |
| `backup_script(script, dir)` / `restore_backup(path)` / `list_backups(dir)` | Backups and time travel. |
| `sandbox_description(policy)` | The isolation description for a run. |
| `security_report(kind, core, registry, states)` | The six reports, as text. |

### `tabs.structure_core` -- project-structure engine
| Function | Purpose |
|---|---|
| `parse_config(text, fmt)` | JSON / TOML / YAML -> dict. |
| `resolve_inheritance(preset, resolver)` | Fold an `extends` chain with override. |
| `render_template(body, values)` | The token engine; returns `(text, missing)`. |
| `safe_join(root, relative)` | Join a path, refusing to escape the project. |
| `StructureGenerator()` | `resolve(name)`, `build_tree(preset)`, `values_for(...)`. |
| `validate_structure(nodes, root)` / `format_validation(report)` | The validator. |
| `plan_tree(nodes, root)` / `commit_tree(nodes, root, overwrite)` | The dry-run plan and the real write. |
| `StructureCompiler(generator, plugins).compile(preset, ...)` | The pipeline. |
| `PresetRepository(dir)` | `get`, `all`, `names`, `save_preset`, `delete_preset`, `versions`. |
| `PluginRegistry()` | `register`, `unregister`, `add_hook`, `run_generators`, `summary`. |
| `analyze_project(root)` / `suggest_missing(preset, root)` / `migrate_preset(preset)` | Reverse analysis, suggestions, migrations. |
| `parse_description(text)` / `apply_description(preset, text)` | Generate from a sentence. |
| `run_pipeline(preset, ...)` / `cli(argv)` | The high-level pipeline and the CLI. |
| `structure_report(kind, result, preset, repository)` | The reports, as text. |

### `tabs.metadata_core` -- header metadata engine
| Function | Purpose |
|---|---|
| `detect_language(path, text)` | The language of a file. |
| `comment_style(key)` | The header comment marker. |
| `parse_header(text, language)` / `header_values(fields)` | Read the header. |
| `render_header(values, language, custom, width)` / `apply_header(text, values, language, custom)` | Render / write the header. |

---

## Testing

Every test is a standalone script in `verifications/` that prints `OK`/`FAIL`
per check and exits non-zero on failure, so they work from a shell and from CI.
Each one presses the buttons rather than looking for the calls: it drives the
real engine and the real (offscreen) tab, so a wired-but-not-connected control
fails the test.

| Script | Covers |
|---|---|
| `verifications/verify_structure.py` | The structure engine: parser (JSON/TOML/YAML), inheritance, templates, path safety, generation, validation, the pipeline, ZIP, reverse analysis, migration, CLI and the Structure tab. |
| `verifications/verify_security.py` | The security engine: digests, HMAC, KDF, sealing, signatures, the registry, tamper detection, backups, reports, the policy and the Security tab. |
| `verifications/verify_translations.py` | `retranslate` reaches every control on the Debugger and Sandbox tabs. |
| `verifications/verify_sandbox_isolation.py` | The sandbox isolation profile and the scrubbed environment. |
| `verifications/verify_debugger.py` | The debugger session, breakpoints and stepping. |
| `verifications/verify_docs_search.py` | The document search query parser and facets. |
| `verifications/verify_docs_integration.py` | Handing a document to another tab. |
| `verifications/verify_docs_actions*.py` | The document open / edit / delete / move / copy actions. |

Run one:

```bash
python verifications/verify_security.py
```

Run them all (PowerShell):

```powershell
Get-ChildItem verifications\verify_*.py | ForEach-Object {
    python $_.FullName
    if ($LASTEXITCODE -ne 0) { Write-Host "FAILED: $($_.Name)" }
}
```

The Qt-based tests force the offscreen platform themselves
(`QT_QPA_PLATFORM=offscreen`), so they run on a headless machine. When running
one by hand from a terminal that buffers output, add `-u`:
`python -u verifications/verify_structure.py`.

**Building the portable executable**

```bat
builder\build.bat            REM clean build -> builder\dist\ScriptDB.exe
builder\build.bat /fast      REM keep the work folder (a little quicker)
builder\build.bat /nopause   REM do not wait for a key at the end (for CI)
```

The result is one self-contained file that runs on Windows with no Python and
no installer. See [builder/README](builder/README.md) for what is inside it.

---

## Extending

The app is built around four registries, all reachable at run time:

```python
from tabs import search_engine, nav_engine, syntax, structure_core
syntax.register_module(MyLanguage)          # a new language
search_engine.register_kind("license", fn)  # a new search kind
nav_engine.register_scanner("mylang", fn)   # a new navigation scanner
# A new project structure: a plugin the Structure tab can load and toggle.
structure_core.PluginRegistry().register(MyPlugin())   # .name + .generate()
```

Each one is picked up by the UI on the next `refresh()` — no edit to
`editor.py`, `search_tab.py`, `structure_tab.py` or `sdb_scripts.py` is required.
The structure engine also supports hooks: `registry.add_hook("pre"|"post", fn)`
runs a plain callable before or after a generation, and `StructureGenerator
.add_rule(fn)` lets a rule add files for a given preset.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `PyQt5 is required` on start | `pip install PyQt5`. |
| `unterminated string literal` after a manual edit | A docstring or literal lost a closing quote. Run `python -c "import ast; ast.parse(open('tabs\<file>.py', encoding='utf-8').read())"` to get the file and line, then close the quote. |
| `re.error: unbalanced parenthesis` from a language rule | A pattern in `tabs/nav_engine.py` or `tabs/search_engine.py` was edited and lost a bracket. Patterns that hold quote characters are built from `chr(34)` / `chr(39)` on purpose — keep them that way. |
| A search returns `error` instead of hits | Check `result.error`; an invalid regex is reported there, never raised. |
| A jump reports "nothing to go to" | The cursor is not on a symbol and the line has no usable identifier, or the file has no target of that kind. |
| Tests hang with no output | Buffered stdout. Re-run with `python -u <script>`. |

---

## Conventions

- **One file per tab, one engine per feature.** A tab module exposes a
  `<Name>Tab` widget class that takes the main window as `window` and builds its
  own UI in `__init__`; the logic lives in a sibling `*_core.py` (or `*_engine.py`)
  that imports no Qt.
- **Engines stay UI-free.** `search_engine.py`, `nav_engine.py`, `secure_core.py`,
  `structure_core.py`, `vault_core.py`, `docs_engine.py` and `metadata_core.py`
  import no Qt, which is what makes them testable from a shell.
- **Standard library only.** Beside PyQt5 there is no third-party dependency; the
  crypto, the TOML/YAML readers and the template engine are all written in-tree.
- **ASCII-safe source.** Docstrings and labels avoid non-ASCII characters; the
  translation tables carry the Polish text.
- **Safe by default.** Engines return a result object with an `.error` / `.ok`
  field rather than raising, so the GUI never has to guard a call; path handling
  goes through `safe_join`, which refuses to escape the project.
- **Hot-pluggable.** New languages, search kinds, scanners and structure plugins
  are registered at run time; the UI reads the registries.
- **Quote-bearing patterns are built from `chr()`.** A quote next to a backslash
  in a source literal is fragile; the engine builds those patterns from
  character codes instead.

---

## License

Add your license of choice here (for example MIT) before publishing.
