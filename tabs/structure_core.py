# -*- coding: utf-8 -*-
"""Project-structure engine for ScriptDB.

This is the engine behind the "Structure" tab: a mini build system for file
trees.  It turns a declarative preset (names, folders, files, tokens) into a
real project on disk, validates it, compiles it through a pipeline of stages,
and can read an existing project back into a preset.

The whole module is standard library only and printable ASCII in its source.

    parse_config(text)           -- YAML / JSON / TOML / INI -> a dict
    StructureGenerator           -- preset -> a tree of (path, body)
    validate_structure(tree)     -- missing files, name conflicts, escapes
    compile_structure(...)       -- run the pipeline, produce artifacts
    PresetRepository             -- global / user / org / versioned presets
    PluginRegistry               -- hot-pluggable generators and hooks
"""

import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import shutil
import zipfile
import tempfile


# ---------------------------------------------------------------------------
# Names and defaults
# ---------------------------------------------------------------------------
STRUCTURE_DIR = "structures"
PRESET_DIR = "presets"
PLUGIN_DIR = "plugins"
TEMPLATE_DIR = "templates"
EXPORT_DIR = "exports"
LOG_FILE = "structure_log.json"

# The scopes a preset can live in.  Each entry is (key, label key, fallback,
# what it is).  The order is the search order used when a preset name is given
# without a scope: the most specific scope wins.
PRESET_SCOPES = (
    ("user", "st_scope_user", "User",
     "your own profiles and folder layouts"),
    ("organization", "st_scope_org", "Organization",
     "the standards of a company, a team or a project"),
    ("project", "st_scope_project", "Project",
     "structures for games, applications and modules"),
    ("global", "st_scope_global", "Global",
     "the standard structures for a language"),
)

PRESET_SCOPE_KEYS = tuple(key for key, _lk, _f, _d in PRESET_SCOPES)
PRESET_SCOPE_LABELS = {key: fallback for key, _lk, fallback, _d
                       in PRESET_SCOPES}

# The artifact kinds a preset can describe.  Each entry is (key, label key,
# fallback, what it is).
STRUCTURE_KINDS = (
    ("application", "st_kind_app", "Application",
     "a runnable program with an entry point"),
    ("module", "st_kind_module", "Module",
     "a single importable unit"),
    ("game", "st_kind_game", "Game",
     "an engine project (Unity, Unreal, Godot)"),
    ("library", "st_kind_library", "Library",
     "a reusable package or C/C++ library"),
    ("framework", "st_kind_framework", "Framework",
     "a pluggable framework with an extension point"),
    ("plugin", "st_kind_plugin", "Plugin",
     "an add-on that extends a host"),
)

STRUCTURE_KIND_KEYS = tuple(key for key, _lk, _f, _d in STRUCTURE_KINDS)
STRUCTURE_KIND_LABELS = {key: fallback for key, _lk, fallback, _d
                         in STRUCTURE_KINDS}

# The languages a preset can target.  Each entry is (key, label key, fallback,
# main extension, entry file).  The extension and entry are the defaults a
# preset inherits when it does not override them.
STRUCTURE_LANGUAGES = (
    ("python", "st_lang_python", "Python", ".py", "main.py"),
    ("cpp", "st_lang_cpp", "C++", ".cpp", "src/main.cpp"),
    ("node", "st_lang_node", "Node / JavaScript", ".js", "index.js"),
    ("java", "st_lang_java", "Java", ".java", "src/Main.java"),
    ("rust", "st_lang_rust", "Rust", ".rs", "src/main.rs"),
    ("csharp", "st_lang_csharp", "C# / Unity", ".cs", "src/Program.cs"),
    ("unreal", "st_lang_unreal", "Unreal", ".cpp",
     "Source/Project/Project.cpp"),
    ("gdscript", "st_lang_godot", "Godot", ".gd", "Main.gd"),
)

STRUCTURE_LANGUAGE_KEYS = tuple(key for key, _lk, _f, _e, _n
                                in STRUCTURE_LANGUAGES)
STRUCTURE_LANGUAGE_LABELS = {key: fallback for key, _lk, fallback, _e, _n
                             in STRUCTURE_LANGUAGES}
STRUCTURE_LANGUAGE_EXT = {key: ext for key, _lk, _f, ext, _n
                          in STRUCTURE_LANGUAGES}
STRUCTURE_LANGUAGE_ENTRY = {key: entry for key, _lk, _f, _e, entry
                            in STRUCTURE_LANGUAGES}

# The build systems a preset can emit.  (key, label key, fallback, files).
BUILD_SYSTEMS = (
    ("none", "st_build_none", "None", ()),
    ("cmake", "st_build_cmake", "CMake", ("CMakeLists.txt",)),
    ("make", "st_build_make", "Make", ("Makefile",)),
    ("gradle", "st_build_gradle", "Gradle", ("build.gradle",)),
    ("npm", "st_build_npm", "npm", ("package.json",)),
    ("pip", "st_build_pip", "pip", ("pyproject.toml", "requirements.txt")),
    ("cargo", "st_build_cargo", "Cargo", ("Cargo.toml",)),
)

BUILD_SYSTEM_KEYS = tuple(key for key, _lk, _f, _files in BUILD_SYSTEMS)
BUILD_SYSTEM_LABELS = {key: fallback for key, _lk, fallback, _files
                       in BUILD_SYSTEMS}

# The platform a preset targets; it changes which scripts and configs appear.
PLATFORMS = (
    ("any", "st_platform_any", "Any"),
    ("windows", "st_platform_windows", "Windows"),
    ("linux", "st_platform_linux", "Linux"),
    ("macos", "st_platform_macos", "macOS"),
)

PLATFORM_KEYS = tuple(key for key, _lk, _f in PLATFORMS)
PLATFORM_LABELS = {key: fallback for key, _lk, fallback in PLATFORMS}

# The pipeline stages.  Each entry is (key, label key, fallback, what it does).
PIPELINE_STAGES = (
    ("pre", "st_stage_pre", "Pre-generation",
     "prepare the environment and the token set"),
    ("generate", "st_stage_generate", "Generation",
     "write the folders, files and manifest"),
    ("validate", "st_stage_validate", "Validation",
     "check the tree that was produced"),
    ("post", "st_stage_post", "Post-generation",
     "format, lint and initialise a repository"),
    ("artifacts", "st_stage_artifacts", "Artifacts",
     "package the output as a zip and a report"),
)

PIPELINE_STAGE_KEYS = tuple(key for key, _lk, _f, _d in PIPELINE_STAGES)
PIPELINE_STAGE_LABELS = {key: fallback for key, _lk, fallback, _d
                         in PIPELINE_STAGES}

# The CI systems a preset can emit configuration for.
CI_SYSTEMS = (
    ("none", "st_ci_none", "None"),
    ("github", "st_ci_github", "GitHub Actions"),
    ("gitlab", "st_ci_gitlab", "GitLab CI"),
    ("azure", "st_ci_azure", "Azure Pipelines"),
)

CI_SYSTEM_KEYS = tuple(key for key, _lk, _f in CI_SYSTEMS)
CI_SYSTEM_LABELS = {key: fallback for key, _lk, fallback in CI_SYSTEMS}

# The IDE project files a preset can emit.
IDE_TARGETS = (
    ("none", "st_ide_none", "None"),
    ("vscode", "st_ide_vscode", "VS Code"),
    ("pycharm", "st_ide_pycharm", "PyCharm"),
    ("intellij", "st_ide_intellij", "IntelliJ"),
)

IDE_TARGET_KEYS = tuple(key for key, _lk, _f in IDE_TARGETS)
IDE_TARGET_LABELS = {key: fallback for key, _lk, fallback in IDE_TARGETS}

# Every action the engine records, so the log and the report agree.
STRUCTURE_EVENTS = (
    "st_preset_saved", "st_preset_loaded", "st_preset_deleted",
    "st_prepared", "st_generated", "st_validated", "st_compiled",
    "st_plugin_loaded", "st_plugin_disabled", "st_exported", "st_imported",
    "st_reversed", "st_migrated", "st_dry_run", "st_error",
    "st_security_block", "st_signature_bad",
)

# Directories the reverse analysis never descends into and the validator treats
# as foreign.
IGNORED_DIRS = {".git", ".hg", ".svn", "__pycache__", ".venv", "venv",
                "node_modules", ".idea", ".vscode", "target", "dist", "build",
                ".mypy_cache", ".pytest_cache"}

# File extensions the reverse analysis treats as "source" rather than data.
SOURCE_EXTENSIONS = (".py", ".cpp", ".c", ".h", ".hpp", ".cs", ".js", ".mjs",
                     ".ts", ".java", ".rs", ".gd", ".go", ".rb")

# Files the artifact stage never packs (build noise and secrets).
NEVER_PACK = (".git", "node_modules", "__pycache__", ".venv", "venv")


# ---------------------------------------------------------------------------
# Small result helpers (the same shape the other cores use)
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
# ---------------------------------------------------------------------------
# Layer 1 -- the configuration parser
# ---------------------------------------------------------------------------
# The parser reads the four formats a preset may arrive in.  JSON and INI/TOML
# come from the standard library; the small YAML subset is read here so a preset
# never needs a third-party package.

def _strip_comment(line, markers=("#",)):
    """Drop a trailing comment, respecting single and double quotes."""
    quote = None
    for index, char in enumerate(line):
        if quote:
            if char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
        elif char in markers:
            return line[:index]
    return line


def _scalar(text):
    """Turn a YAML/INI scalar into a Python value."""
    value = text.strip()
    if value == "":
        return ""
    lowered = value.lower()
    if lowered in ("true", "yes", "on"):
        return True
    if lowered in ("false", "no", "off"):
        return False
    if lowered in ("null", "none", "~"):
        return None
    if (value[:1] == value[-1:] and value[:1] in "\"'"):
        return value[1:-1]
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [_scalar(part) for part in _split_top(inner, ",")]
    if value.startswith("{") and value.endswith("}"):
        out = {}
        for part in _split_top(value[1:-1], ","):
            if ":" in part:
                key, _, rest = part.partition(":")
                out[_scalar(key)] = _scalar(rest)
        return out
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def _split_top(text, separator):
    """Split on ``separator`` at depth 0, ignoring quotes and nesting."""
    parts = []
    depth = 0
    quote = None
    current = []
    for char in text:
        if quote:
            current.append(char)
            if char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
            current.append(char)
        elif char in "([{":
            depth += 1
            current.append(char)
        elif char in ")]}":
            depth -= 1
            current.append(char)
        elif char == separator and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return [part for part in parts if part.strip() != "" or part == parts[-1]]


def parse_yaml(text):
    """A small YAML reader: nested maps, lists, scalars and block lists.

    It covers the subset a preset uses (`key: value`, `key:` blocks, `- item`
    lists and inline `[a, b]` / `{k: v}` forms), so no YAML package is needed.
    Anything outside that subset is read as a plain string rather than guessed.
    """
    # Keep the meaningful lines with their indentation, then read them with a
    # tiny recursive-descent reader -- far easier to keep correct than a stack.
    rows = []
    for raw in (text or "").splitlines():
        line = _strip_comment(raw)
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        rows.append((indent, line.strip()))
    if not rows:
        return {}
    value, _index = _yaml_block(rows, 0, rows[0][0])
    return value if isinstance(value, dict) else {}


def _yaml_block(rows, index, indent):
    """Read one block at ``indent``; returns ``(value, next_index)``.

    A block is a list when its first line is a dash item, otherwise a map.  This
    is the whole of the YAML support: maps, block lists and scalars.
    """
    if index < len(rows) and rows[index][1].startswith("- "):
        items = []
        while index < len(rows) and rows[index][0] == indent \
                and rows[index][1].startswith("- "):
            item = rows[index][1][2:].strip()
            if ":" in item and not item.startswith((chr(34), chr(39))):
                key, _, rest = item.partition(":")
                record = {key.strip(): _scalar(rest)}
                index += 1
                # A nested map under the item continues at a deeper indent.
                if index < len(rows) and rows[index][0] > indent:
                    nested, index = _yaml_block(rows, index, rows[index][0])
                    if isinstance(nested, dict):
                        record.update(nested)
                items.append(record)
            else:
                items.append(_scalar(item))
                index += 1
        return items, index
    mapping = {}
    while index < len(rows) and rows[index][0] == indent:
        line = rows[index][1]
        if ":" not in line:
            index += 1
            continue
        key, _, rest = line.partition(":")
        key = key.strip()
        rest = rest.strip()
        index += 1
        if rest != "":
            mapping[key] = _scalar(rest)
        elif index < len(rows) and rows[index][0] > indent:
            child, index = _yaml_block(rows, index, rows[index][0])
            mapping[key] = child
        else:
            mapping[key] = {}
    return mapping, index


def parse_toml(text):
    """A small TOML/INI reader: `[table]` sections and `key = value` lines."""
    root = {}
    section = root
    for raw in (text or "").splitlines():
        line = _strip_comment(raw, markers=("#", ";"))
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            name = stripped[1:-1].strip()
            section = root
            for part in name.split("."):
                section = section.setdefault(part.strip(), {})
            continue
        if "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        section[key.strip()] = _scalar(value)
    return root


def parse_config(text, fmt=""):
    """Parse a preset document into a dict.

    The format is chosen from an explicit ``fmt`` or guessed from the text
    (a leading brace or bracket is JSON, ``[section]`` is TOML, otherwise the
    YAML subset).  A parse failure returns ``{}`` so the caller can report it
    rather than raising.
    """
    body = text or ""
    fmt = (fmt or "").lower()
    if not fmt:
        head = body.lstrip()[:1]
        # A `[section]` on its own line is TOML, not a JSON array, so the TOML
        # shape is checked before the generic bracket test.
        if re.search(r"^\s*\[[A-Za-z0-9_.\-]+\]\s*$", body, re.M):
            fmt = "toml"
        elif head == "{":
            fmt = "json"
        elif head == "[":
            fmt = "json"
        elif "=" in body.split("\n")[0] and ":" not in body.split("\n")[0]:
            fmt = "toml"
        else:
            fmt = "yaml"
    try:
        if fmt == "json":
            data = json.loads(body or "{}")
        elif fmt == "toml":
            data = parse_toml(body)
        else:
            data = parse_yaml(body)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _deep_merge(base, overlay):
    """Merge ``overlay`` into ``base``; nested dicts merge, the rest replace."""
    out = dict(base or {})
    for key, value in (overlay or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def resolve_inheritance(preset, resolver=None, _seen=None):
    """Fold a preset's ``extends`` chain into one flat preset.

    ``resolver(name)`` returns the parent preset; when it is not given the chain
    is left as-is.  A cycle is broken rather than followed, and the child always
    wins on a conflict -- that is what makes "extends + override" predictable.
    """
    _seen = _seen or set()
    if not isinstance(preset, dict):
        return {}
    if isinstance(resolver, dict):
        # A dict may be handed in for convenience; look the parent up in it.
        table = resolver
        resolver = lambda name: table.get(name, {})
    parent_name = preset.get("extends")
    if not parent_name or resolver is None or parent_name in _seen:
        out = dict(preset)
        out.pop("extends", None)
        return out
    _seen.add(parent_name)
    parent = resolver(parent_name) or {}
    parent = resolve_inheritance(parent, resolver, _seen)
    child = dict(preset)
    child.pop("extends", None)
    return _deep_merge(parent, child)
# ---------------------------------------------------------------------------
# Layer 2 -- the template engine (our own token system)
# ---------------------------------------------------------------------------
# A body may contain:
#   ${name}          -> the value of ``name`` (a plain token)
#   $select{a|b|c}   -> the value of the token named by the first option
#   $if{name}...$end -> the block, when the token is truthy
#   $each{items}...$end -> the block once per item, with ${item} bound
# Unfilled tokens are left as they are, so a template never emits a wrong value
# silently -- the validator flags the leftovers instead.

_TOKEN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_SELECT = re.compile(r"\$select\{([^}]*)\}")
_IF = re.compile(r"\$if\{([A-Za-z_][A-Za-z0-9_]*)\}(.*?)\$end", re.S)
_EACH = re.compile(r"\$each\{([A-Za-z_][A-Za-z0-9_]*)\}(.*?)\$end", re.S)


def template_tokens(body):
    """Every token name a body references, in first-seen order."""
    seen = []
    for pattern in (_TOKEN, _SELECT, _IF, _EACH):
        for match in pattern.finditer(body or ""):
            name = match.group(1) if pattern is not _SELECT else \
                match.group(1).split("|")[0].strip()
            if pattern is _SELECT:
                for option in match.group(1).split("|"):
                    option = option.strip()
                    if option and option not in seen:
                        seen.append(option)
                continue
            if name not in seen:
                seen.append(name)
    return seen


def render_template(body, values=None, strict=False):
    """Render a body, returning ``(text, missing)``.

    ``missing`` lists the tokens that had no value.  With ``strict`` the missing
    tokens are replaced by an empty string; otherwise they are left in place so
    the caller can see exactly what was not supplied.
    """
    values = dict(values or {})
    missing = []

    def replace_each(match):
        name = match.group(1)
        block = match.group(2)
        items = values.get(name) or []
        if isinstance(items, (str, dict)):
            items = [items]
        out = []
        for index, item in enumerate(items):
            local = dict(values)
            local["item"] = item
            local["index"] = index
            text, _m = render_template(block, local)
            out.append(text)
        return "".join(out)

    def replace_if(match):
        name = match.group(1)
        return match.group(2) if values.get(name) else ""

    def replace_select(match):
        for option in match.group(1).split("|"):
            option = option.strip()
            if option in values:
                return str(values[option])
        return ""

    def replace_token(match):
        name = match.group(1)
        if name in values and values[name] is not None:
            return str(values[name])
        missing.append(name)
        return match.group(0) if not strict else ""

    text = body or ""
    # A doubled dollar is an escape: `$${SOURCES}` stays a literal `${SOURCES}`,
    # which is how a Makefile or CMake variable survives the token engine.
    text = text.replace("$${", "\x00{")
    # Loops first, then conditionals, then selects, then plain tokens: each
    # expansion may reveal the next kind, so the order matters.
    text = _EACH.sub(replace_each, text)
    text = _IF.sub(replace_if, text)
    text = _SELECT.sub(replace_select, text)
    text = _TOKEN.sub(replace_token, text)
    text = text.replace("\x00{", "${")
    return text, missing


def default_values(name="project", language="python", kind="application",
                   author="", **extra):
    """The token set every preset starts from, before its own values."""
    stem = _slug(name, "project")
    identifier = re.sub(r"[^A-Za-z0-9_]", "_", name or "project") or "project"
    if identifier[0].isdigit():
        identifier = "_" + identifier
    values = {
        "project": name or "project",
        "project_name": name or "project",
        "package": identifier.lower(),
        "module": identifier.lower(),
        "class": "".join(part.capitalize() for part in
                         re.split(r"[^A-Za-z0-9]+", name or "project")
                         if part) or "Project",
        "slug": stem,
        "language": language,
        "kind": kind,
        "ext": STRUCTURE_LANGUAGE_EXT.get(language, ".py"),
        "ext_word": STRUCTURE_LANGUAGE_EXT.get(language, ".py").lstrip("."),
        "entry": STRUCTURE_LANGUAGE_ENTRY.get(language, "main.py"),
        "author": author or "author",
        "year": str(dt.datetime.now().year),
        "date": dt.datetime.now().strftime("%Y-%m-%d"),
        "version": "0.1.0",
        "license": "MIT",
    }
    values.update(extra)
    return values
# ---------------------------------------------------------------------------
# Layer 3 -- path safety (no escape out of the project)
# ---------------------------------------------------------------------------
def safe_join(root, relative, allow_absolute=False):
    """Join ``relative`` under ``root``, refusing to escape it.

    Returns ``(path, error)`` where exactly one is set.  Every path a preset
    produces goes through here, so a malicious preset cannot write outside the
    project directory with ``..`` or an absolute path.
    """
    if relative is None:
        return "", "empty path"
    text = str(relative).replace("\\", "/").strip()
    if not text:
        return "", "empty path"
    if os.path.isabs(text) or text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        if not allow_absolute:
            return "", "absolute paths are not allowed: %s" % text
        return os.path.abspath(text.replace("/", os.sep)), ""
    parts = []
    for part in text.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            return "", "the path escapes the project: %s" % relative
        parts.append(part)
    if not parts:
        return "", "empty path"
    joined = os.path.join(root, *parts)
    root_abs = os.path.abspath(root)
    joined_abs = os.path.abspath(joined)
    if joined_abs != root_abs and not joined_abs.startswith(
            root_abs + os.sep):
        return "", "the path escapes the project: %s" % relative
    return joined_abs, ""


def is_safe_relative(path):
    """True when a relative path stays inside its project."""
    _joined, error = safe_join(os.getcwd(), path)
    return not error
# ---------------------------------------------------------------------------
# Layer 4 -- built-in folder and file templates
# ---------------------------------------------------------------------------
# A preset lists folders and files.  A file entry is (path, template body); the
# bodies below cover the artifact templates the tab offers out of the box, so a
# preset can name one instead of carrying the text inline.
FOLDER_TEMPLATES = {
    "src": ("src", "Source code"),
    "assets": ("assets", "Art, audio and other resources"),
    "modules": ("modules", "Independent modules"),
    "tests": ("tests", "Test suites"),
    "docs": ("docs", "Documentation"),
    "pipelines": ("pipelines", "Build and CI definitions"),
    "config": ("config", "Configuration files"),
    "scripts": ("scripts", "Helper and build scripts"),
    "dist": ("dist", "Built output"),
}

README_BODY = """# ${project}

${description}

- Language: ${language}
- Kind:     ${kind}
- Author:   ${author}
- Version:  ${version}
"""

LICENSE_MIT = """MIT License

Copyright (c) ${year} ${author}

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND.
"""

MANIFEST_BODY = """{
  "name": "${slug}",
  "version": "${version}",
  "description": "${description}",
  "language": "${language}",
  "kind": "${kind}",
  "author": "${author}",
  "entry": "${entry}"
}
"""

GITIGNORE_BODY = """# build output
dist/
build/
target/

# caches
__pycache__/
*.pyc
.venv/
venv/
node_modules/

# editors
.idea/
.vscode/

# secrets
*.env
*.key
*.pem
"""

EDITOR_CONFIG_BODY = """root = true

[*]
end_of_line = lf
insert_final_newline = true
trim_trailing_whitespace = true

[*.${ext_word}]
indent_style = space
indent_size = 4
"""


def _entry_source_body(language, name):
    """A minimal but real entry point for the chosen language."""
    if language == "python":
        return ('"""Entry point for %s."""\n\n\ndef main():\n'
                '    print("hello from %s")\n\n'
                'if __name__ == "__main__":\n    main()\n'
                % (name, name))
    if language == "node":
        return ('// Entry point for %s\nfunction main() {\n'
                '  console.log("hello from %s");\n}\n\nmain();\n'
                % (name, name))
    if language == "cpp":
        return ('// Entry point for %s\n#include <iostream>\n\n'
                'int main() {\n  std::cout << "hello from %s" << std::endl;\n'
                '  return 0;\n}\n' % (name, name))
    if language == "csharp":
        return ('using System;\n\nnamespace %s {\n  class Program {\n'
                '    static void Main(string[] args) {\n'
                '      Console.WriteLine("hello from %s");\n    }\n  }\n}\n'
                % (_pascal(name), name))
    if language == "rust":
        return ('// Entry point for %s\nfn main() {\n'
                '    println!("hello from %s");\n}\n' % (name, name))
    if language == "java":
        return ('public class Main {\n  public static void main(String[] args)'
                ' {\n    System.out.println("hello from %s");\n  }\n}\n'
                % name)
    if language == "gdscript":
        return ('extends Node\n# Entry point for %s\n'
                'func _ready():\n    print("hello from %s")\n' % (name, name))
    return "entry point for %s\n" % name


def _pascal(text):
    return "".join(part.capitalize() for part in
                   re.split(r"[^A-Za-z0-9]+", text or "") if part) or "Project"


def module_body(language, name):
    """A starter module/class for the chosen language."""
    stem = _slug(name, "module")
    cls = _pascal(name)
    if language == "python":
        return ('"""The %s module."""\n\nclass %s(object):\n'
                '    """A starter class."""\n\n'
                '    def __init__(self):\n        self.name = "%s"\n\n'
                '    def run(self):\n        return self.name\n'
                % (stem, cls, stem))
    if language == "node":
        return ('// The %s module.\n\nclass %s {\n'
                '  constructor() { this.name = "%s"; }\n'
                '  run() { return this.name; }\n}\n\n'
                'module.exports = { %s };\n' % (stem, cls, stem, cls))
    if language == "cpp":
        return ('// The %s module.\n#pragma once\n'
                'class %s {\npublic:\n  %s() : name_("%s") {}\n'
                '  const char* name() const { return name_.c_str(); }\n'
                'private:\n  std::string name_;\n};\n'
                % (stem, cls, cls, stem))
    if language == "csharp":
        return ('namespace %s {\n  public class %s {\n'
                '    public string Name = "%s";\n  }\n}\n'
                % (_pascal(name), cls, stem))
    if language == "rust":
        return ('// The %s module.\n\npub struct %s {\n'
                '    pub name: String,\n}\n\nimpl %s {\n'
                '    pub fn new() -> Self {\n'
                '        %s { name: "%s".to_string() }\n    }\n}\n'
                % (stem, cls, cls, cls, stem))
    if language == "java":
        return ('// The %s module.\npublic class %s {\n'
                '  public String name = "%s";\n}\n' % (stem, cls, stem))
    return "// the %s module\n" % stem


def test_body(language, name):
    """A starter test for the chosen language."""
    stem = _slug(name, "module")
    if language == "python":
        return ('"""Tests for %s."""\n\nimport unittest\n'
                'class Test%s(unittest.TestCase):\n\n'
                '    def test_placeholder(self):\n        self.assertTrue(True)\n\n'
                'if __name__ == "__main__":\n    unittest.main()\n'
                % (stem, _pascal(name)))
    if language == "node":
        return ('// Tests for %s\nconst test = require("assert");\n\n'
                'test.ok(true, "%s works");\n' % (stem, stem))
    if language == "cpp":
        return ('// Tests for %s\n#include <cassert>\n\n'
                'int main() {\n  assert(true);\n  return 0;\n}\n' % stem)
    return "// tests for %s\n" % stem
# ---------------------------------------------------------------------------
# Layer 5 -- built-in presets
# ---------------------------------------------------------------------------
# Each built-in preset is a plain dict in the same shape a user preset takes, so
# the built-ins are the documentation for the format.  ``folders`` is a list of
# relative folders, ``files`` a list of (path, template body) pairs, ``modules``
# a list of module names, ``tokens`` extra values for the template engine.
def _preset(name, title, kind, language, folders, files, modules=(),
            tokens=None, build="none", ci="none", ide="none", platform="any",
            version=1, scope="global", description=""):
    return {
        "name": name,
        "title": title,
        "kind": kind,
        "language": language,
        "platform": platform,
        "build": build,
        "ci": ci,
        "ide": ide,
        "scope": scope,
        "version": version,
        "description": description,
        "folders": list(folders),
        "files": [(path, body) for path, body in files],
        "modules": list(modules),
        "tokens": dict(tokens or {}),
    }


def _standard_extras(language, modules=(), with_tests=True):
    """The files every project of a language shares."""
    ext = STRUCTURE_LANGUAGE_EXT.get(language, ".py")
    files = [
        ("README.md", README_BODY),
        ("LICENSE", LICENSE_MIT),
        ("manifest.json", MANIFEST_BODY),
        (".gitignore", GITIGNORE_BODY),
        (".editorconfig", EDITOR_CONFIG_BODY),
    ]
    entry = STRUCTURE_LANGUAGE_ENTRY.get(language, "main.py")
    files.append((entry, _entry_source_body(language, "${project}")))
    if with_tests:
        files.append(("tests/test_${slug}" + ext,
                      test_body(language, "${project}")))
    for module in modules:
        stem = _slug(module, "module")
        files.append(("src/%s%s" % (stem, ext), module_body(language, module)))
    return files


# The global presets: one per language, the standard structure for that stack.
GLOBAL_PRESETS = (
    _preset("python-app", "Python application", "application", "python",
            ["src", "tests", "docs"],
            _standard_extras("python", ["config", "service"]),
            modules=["config", "service"], build="pip",
            description="a Python program with a src layout and a test suite"),
    _preset("cpp-app", "C++ application", "application", "cpp",
            ["src", "include", "tests", "docs"],
            _standard_extras("cpp", ["engine"], with_tests=True),
            modules=["engine"], build="cmake",
            description="a CMake C++ program with headers and tests"),
    _preset("node-app", "Node application", "application", "node",
            ["src", "tests", "docs"],
            _standard_extras("node", ["server", "routes"]),
            modules=["server", "routes"], build="npm",
            description="a Node program with a src layout"),
    _preset("java-app", "Java application", "application", "java",
            ["src", "tests", "docs"],
            _standard_extras("java", ["Main"], with_tests=False),
            build="gradle",
            description="a Gradle Java program"),
    _preset("rust-app", "Rust application", "application", "rust",
            ["src", "tests", "docs"],
            _standard_extras("rust", ["lib"], with_tests=False),
            build="cargo",
            description="a Cargo Rust program"),
)

# The project presets: structures for the artifact kinds the tab offers.
PROJECT_PRESETS = (
    _preset("game-unity", "Unity game", "game", "csharp",
            ["Assets", "Assets/Scripts", "Assets/Prefabs", "Assets/Scenes",
             "ProjectSettings", "Packages"],
            [("README.md", README_BODY), ("LICENSE", LICENSE_MIT),
             (".gitignore", GITIGNORE_BODY),
             ("Assets/Scripts/GameObject.cs", module_body("csharp",
                                                           "GameObject")),
             ("Assets/Scripts/Player.cs", module_body("csharp", "Player")),
             ("ProjectSettings/ProjectVersion.txt",
              "m_EditorVersion: 2022.3.0f1\n")],
            modules=["Player", "Enemy", "GameManager"], ide="vscode",
            description="a Unity project layout"),
    _preset("game-godot", "Godot game", "game", "gdscript",
            ["scenes", "scripts", "assets", "addons"],
            [("README.md", README_BODY), ("LICENSE", LICENSE_MIT),
             (".gitignore", GITIGNORE_BODY),
             ("project.godot", "[application]\nconfig/name=\"${project}\"\n"),
             ("scripts/Main.gd", _entry_source_body("gdscript", "${project}")),
             ("scripts/Player.gd", module_body("gdscript", "Player"))],
            modules=["Player", "Level"], ide="vscode",
            description="a Godot project layout"),
    _preset("lib-ccpp", "C/C++ library", "library", "cpp",
            ["src", "include", "tests", "examples", "docs"],
            _standard_extras("cpp", ["api"], with_tests=True),
            modules=["api"], build="cmake",
            description="a library with a public header and examples"),
)

# The artifact presets: single-purpose structures a user can extend.
ARTIFACT_PRESETS = (
    _preset("plugin-python", "Python plugin", "plugin", "python",
            ["plugin", "plugin/hooks", "tests"],
            [("README.md", README_BODY),
             ("plugin/__init__.py",
              '"""The ${project} plugin."""\n\n'
              'def register(host):\n    """Attach this plugin to the host."""\n'
              '    host.add_generator("${slug}", generate)\n\n'
              'def generate(options=None):\n    return {"ok": True}\n'),
             ("plugin/manifest.json", MANIFEST_BODY),
             ("tests/test_plugin.py", test_body("python", "${project}")),
             ("plugin/hooks/pre_generate.py",
              '"""Runs before generation."""\n\n'
              'def hook(state):\n    return state\n')],
            build="pip",
            description="a hot-pluggable generator a host can load"),
)

ALL_BUILTIN_PRESETS = GLOBAL_PRESETS + PROJECT_PRESETS + ARTIFACT_PRESETS


def builtin_presets():
    """Every built-in preset, keyed by name."""
    return {preset["name"]: dict(preset) for preset in ALL_BUILTIN_PRESETS}
# ---------------------------------------------------------------------------
# Layer 6 -- build / CI / IDE file emitters
# ---------------------------------------------------------------------------
def build_files(build, language, values):
    """The files a build system needs, as (path, body) pairs."""
    if build == "cmake":
        return [("CMakeLists.txt",
                 "cmake_minimum_required(VERSION 3.16)\n"
                 "project(${project})\n\nset(CMAKE_CXX_STANDARD 17)\n\n"
                 "file(GLOB_RECURSE SOURCES src/*.cpp)\n"
                 "add_executable(${slug} $${SOURCES})\n"
                 "target_include_directories(${slug} PRIVATE include)\n")]
    if build == "make":
        return [("Makefile",
                 "CC ?= cc\nCFLAGS ?= -Wall -O2\n"
                 "all:\n\t$(CC) $(CFLAGS) -o ${slug} ${entry}\n\n"
                 "clean:\n\trm -f ${slug}\n")]
    if build == "gradle":
        return [("build.gradle",
                 "plugins { id 'application' }\n\n"
                 "application { mainClass = 'Main' }\n\n"
                 "repositories { mavenCentral() }\n")]
    if build == "npm":
        return [("package.json",
                 '{\n  "name": "${slug}",\n  "version": "${version}",\n'
                 '  "main": "${entry}",\n  "scripts": {\n'
                 '    "start": "node ${entry}",\n    "test": "node tests"\n'
                 '  }\n}\n')]
    if build == "pip":
        return [("pyproject.toml",
                 "[build-system]\nrequires = [\"setuptools\"]\n"
                 "build-backend = \"setuptools.build_meta\"\n\n"
                 "[project]\nname = \"${slug}\"\nversion = \"${version}\"\n"
                 "description = \"${description}\"\n"
                 "authors = [{ name = \"${author}\" }]\n"
                 "requires-python = \">=3.8\"\n"),
                ("requirements.txt", "# runtime dependencies\n")]
    if build == "cargo":
        return [("Cargo.toml",
                 "[package]\nname = \"${slug}\"\nversion = \"${version}\"\n"
                 "edition = \"2021\"\n\n[dependencies]\n")]
    return []


def ci_files(ci, values):
    """The CI configuration a preset asked for, as (path, body) pairs."""
    if ci == "github":
        return [(".github/workflows/ci.yml",
                 "name: ci\non: [push, pull_request]\n\njobs:\n"
                 "  build:\n    runs-on: ubuntu-latest\n    steps:\n"
                 "      - uses: actions/checkout@v4\n"
                 "      - name: run\n        run: echo \"build ${project}\"\n")]
    if ci == "gitlab":
        return [(".gitlab-ci.yml",
                 "stages:\n  - build\nbuild:\n  stage: build\n"
                 "  script:\n    - echo \"build ${project}\"\n")]
    if ci == "azure":
        return [("azure-pipelines.yml",
                 "trigger:\n  - main\npool:\n  vmImage: ubuntu-latest\n"
                 "steps:\n  - script: echo \"build ${project}\"\n"
                 "    displayName: build\n")]
    return []


def docker_files(values, language="python"):
    """A Dockerfile and a compose file for the project."""
    if language == "python":
        base, run = "python:3.12-slim", "python ${entry}"
    elif language == "node":
        base, run = "node:20-slim", "node ${entry}"
    elif language == "rust":
        base, run = "rust:1-slim", "cargo run"
    else:
        base, run = "ubuntu:24.04", "./${slug}"
    return [("Dockerfile",
             "FROM %s\nWORKDIR /app\nCOPY .\nCMD [\"sh\", \"-c\", \"%s\"]\n"
             % (base, run)),
            ("docker-compose.yml",
             "services:\n  app:\n    build: .\n"
             "    container_name: ${slug}\n"
             "    volumes:\n      - .:/app\n")]


def ide_files(ide, language, values):
    """The IDE project files a preset asked for."""
    if ide == "vscode":
        return [(".vscode/settings.json",
                 '{\n  "files.eol": "\\n",\n'
                 '  "editor.tabSize": 4\n}\n'),
                (".vscode/launch.json",
                 '{\n  "version": "0.2.0",\n  "configurations": [\n'
                 '    {\n      "name": "Run ${project}",\n'
                 '      "type": "debugpy",\n      "request": "launch",\n'
                 '      "program": "${entry}"\n    }\n  ]\n}\n')]
    if ide == "pycharm":
        return [(".idea/modules.xml",
                 "<project version=\"4\">\n <component name=\"ProjectModuleManager\">\n"
                 "    <modules/>\n </component>\n</project>\n")]
    if ide == "intellij":
        return [(".idea/misc.xml",
                 "<project version=\"4\">\n <component name=\"ProjectRootManager\"/>\n"
                 "</project>\n")]
    return []
# ---------------------------------------------------------------------------
# Layer 7 -- the generator and the tree model
# ---------------------------------------------------------------------------
class TreeNode(object):
    """One entry in a generated tree: a file with a body, or a folder."""

    def __init__(self, path, body="", folder=False, template=""):
        self.path = path.replace("\\", "/").strip()
        self.body = body
        self.folder = folder
        self.template = template
        self.missing = []
        self.rendered = False

    def to_dict(self):
        return {"path": self.path, "folder": self.folder,
                "template": self.template, "bytes": len(self.body or ""),
                "missing": list(self.missing)}

    def __repr__(self):
        return "TreeNode(%r, folder=%s)" % (self.path, self.folder)


class StructureGenerator(object):
    """Turns a preset dict into a tree of :class:`TreeNode` objects.

    The generator never writes to disk: it produces the tree, which the caller
    validates, shows in a dry-run or commits.  That split is what makes the
    dry-run and the real generation share one code path.
    """

    def __init__(self, presets=None):
        # ``presets`` maps a name to a preset dict; the built-ins are the base
        # layer so a user preset can extend them.
        self.presets = builtin_presets()
        if presets:
            self.presets.update(presets)
        self.rules = []
        self.extra_files = []       # (path, body) from plugins
        self.log = []

    # ------------------------------------------------------------- presets
    def register_preset(self, preset):
        if isinstance(preset, dict) and preset.get("name"):
            self.presets[preset["name"]] = preset
            return preset["name"]
        return None

    def resolve(self, name):
        """Return a preset by name with its inheritance already folded in."""
        return resolve_inheritance(self.presets.get(name, {}),
                                   self.presets.get)

    def preset_names(self):
        return sorted(self.presets)

    def add_rule(self, rule):
        """Add a rule: a callable that may add files for a given preset."""
        if callable(rule):
            self.rules.append(rule)

    # ------------------------------------------------------------- generate
    def values_for(self, preset, name="", extra=None):
        """The token set a preset renders with."""
        values = default_values(
            name=name or preset.get("name", "project"),
            language=preset.get("language", "python"),
            kind=preset.get("kind", "application"),
            author=preset.get("author", ""))
        values["description"] = preset.get("description", "")
        values["project"] = name or values["project"]
        values.update(preset.get("tokens") or {})
        values.update(extra or {})
        return values

    def entries_for(self, preset, name="", extra=None):
        """The full file list for a preset, before rendering.

        The order is: the preset's own files, then the build system, the CI
        config, the IDE files, Docker, then anything the rules add.  A preset's
        own file always wins when two entries share a path.
        """
        values = self.values_for(preset, name, extra)
        language = preset.get("language", "python")
        entries = []
        seen = set()

        def add(path, body, template=""):
            key = path.replace("\\", "/").lower()
            if key in seen:
                return
            seen.add(key)
            entries.append((path, body, template))

        for path, body in preset.get("files", []):
            add(path, body)
        for module in preset.get("modules", []):
            stem = _slug(module, "module")
            ext = STRUCTURE_LANGUAGE_EXT.get(language, ".py")
            add("src/%s%s" % (stem, ext), module_body(language, module))
        for path, body in build_files(preset.get("build", "none"), language,
                                      values):
            add(path, body)
        for path, body in ci_files(preset.get("ci", "none"), values):
            add(path, body)
        for path, body in ide_files(preset.get("ide", "none"), language,
                                    values):
            add(path, body)
        for path, body in self.extra_files:
            add(path, body)
        # Rules are consulted last and may add more entries.
        state = {"preset": preset, "values": values, "entries": entries,
                 "added": add}
        for rule in self.rules:
            try:
                rule(state)
            except Exception as exc:  # a broken rule must not abort generation
                self.log.append({"time": _now(), "kind": "rule_error",
                                 "detail": str(exc)})
        return values, entries

    def build_tree(self, preset, name="", extra=None):
        """Render a preset into an ordered list of :class:`TreeNode`."""
        values, entries = self.entries_for(preset, name, extra)
        nodes = []
        folders = set()
        for folder in preset.get("folders", []):
            rendered, _missing = render_template(str(folder), values)
            folders.add(rendered.replace("\\", "/").strip("/"))
        for path, body, template in entries:
            rendered_path, _path_missing = render_template(path, values)
            parent = os.path.dirname(rendered_path.replace("\\", "/"))
            while parent:
                folders.add(parent)
                parent = os.path.dirname(parent)
        ordered_folders = sorted(folders, key=lambda item: (item.count("/"),
                                                           item))
        for folder in ordered_folders:
            if folder:
                nodes.append(TreeNode(folder, folder=True))
        for path, body, template in entries:
            # The path is rendered with the same values as the body, so a
            # filename such as `test_${slug}.py` becomes a real name rather than
            # a literal token the user has to clean up.
            rendered_path, path_missing = render_template(path, values)
            text, missing = render_template(body, values)
            node = TreeNode(rendered_path, text, template=template)
            node.missing = sorted(set(missing) | set(path_missing))
            node.rendered = not node.missing
            nodes.append(node)
        return values, nodes
# ---------------------------------------------------------------------------
# Layer 8 -- the validator
# ---------------------------------------------------------------------------
def validate_structure(nodes, root="", strict=True):
    """Check a generated tree; returns a report dict.

    The checks are the ones that catch a broken preset before it touches disk:
    unsafe paths, duplicate paths, empty files, unfilled tokens, and (against a
    real ``root``) files a preset promises but that are missing on disk.
    """
    findings = []
    seen = {}
    folders = set()
    files = set()
    for node in nodes:
        path = node.path
        if not is_safe_relative(path):
            findings.append({"level": "error", "kind": "escape",
                             "path": path,
                             "detail": "the path escapes the project"})
            continue
        key = path.lower()
        if key in seen:
            findings.append({"level": "error", "kind": "conflict", "path": path,
                             "detail": "declared more than once"})
        seen[key] = node
        if node.folder:
            folders.add(path)
        else:
            files.add(path)
        if node.folder and key in files:
            findings.append({"level": "error", "kind": "conflict", "path": path,
                             "detail": "a file and a folder share the path"})
        if not node.folder and not (node.body or "").strip():
            findings.append({"level": "warning", "kind": "empty", "path": path,
                             "detail": "the file would be empty"})
        if node.missing:
            level = "error" if strict else "warning"
            findings.append({"level": level, "kind": "token", "path": path,
                             "detail": "unfilled token(s): %s"
                             % ", ".join(sorted(set(node.missing)))})
        if not node.folder and path.endswith("/"):
            findings.append({"level": "warning", "kind": "path", "path": path,
                             "detail": "a file path ends with a slash"})
    missing_on_disk = []
    if root and os.path.isdir(root):
        for path in sorted(files):
            target = os.path.join(root, *path.split("/"))
            if not os.path.exists(target):
                missing_on_disk.append(path)
        if missing_on_disk:
            findings.append({"level": "warning", "kind": "missing",
                             "path": "", "detail": "%d file(s) promised by the "
                             "preset are not on disk" % len(missing_on_disk)})
    errors = [item for item in findings if item["level"] == "error"]
    warnings = [item for item in findings if item["level"] == "warning"]
    return {
        "ok": not errors,
        "valid": not errors,
        "files": len(files),
        "folders": len(folders),
        "findings": findings,
        "errors": errors,
        "warnings": warnings,
        "missing_on_disk": missing_on_disk,
    }


def format_validation(report):
    """A validator report as plain text, for the tab and the log."""
    lines = ["Structure validation",
             "  files:    %d" % report.get("files", 0),
             "  folders:  %d" % report.get("folders", 0),
             "  errors:   %d" % len(report.get("errors", [])),
             "  warnings: %d" % len(report.get("warnings", [])),
             ""]
    for item in report.get("findings", []):
        lines.append("  [%-7s] %-10s %-30s %s"
                     % (item.get("level"), item.get("kind"),
                        item.get("path", ""), item.get("detail", "")))
    if not report.get("findings"):
        lines.append("  no problems found")
    return "\n".join(lines)
# ---------------------------------------------------------------------------
# Layer 9 -- writing the tree to disk
# ---------------------------------------------------------------------------
def plan_tree(nodes, root):
    """The list of actions a commit would take, without taking them (dry-run).

    Returns a list of dicts: ``{"action": create|overwrite|skip, "path", ...}``.
    The dry-run is exactly this list, so what is shown is what happens.
    """
    plan = []
    for node in nodes:
        target, error = safe_join(root, node.path)
        if error:
            plan.append({"action": "skip", "path": node.path,
                         "detail": error})
            continue
        if node.folder:
            plan.append({"action": "folder" if not os.path.isdir(target)
                         else "skip", "path": node.path + "/",
                         "detail": "directory"})
            continue
        exists = os.path.isfile(target)
        plan.append({"action": "overwrite" if exists else "create",
                     "path": node.path, "bytes": len(node.body or ""),
                     "detail": "file"})
    return plan


def commit_tree(nodes, root, overwrite=False, make_dirs=True):
    """Write a generated tree under ``root``; returns a result dict.

    Every path is checked with :func:`safe_join`, and an existing file is only
    replaced when ``overwrite`` is on -- so a mis-typed target cannot quietly
    destroy work.
    """
    written = []
    skipped = []
    created_dirs = []
    errors = []
    if make_dirs:
        try:
            os.makedirs(root, exist_ok=True)
        except OSError as exc:
            return _fail("mkdir_failed", str(exc))
    for node in nodes:
        target, error = safe_join(root, node.path)
        if error:
            errors.append({"path": node.path, "detail": error})
            continue
        if node.folder:
            try:
                if not os.path.isdir(target):
                    os.makedirs(target, exist_ok=True)
                    created_dirs.append(node.path)
            except OSError as exc:
                errors.append({"path": node.path, "detail": str(exc)})
            continue
        if os.path.exists(target) and not overwrite:
            skipped.append(node.path)
            continue
        try:
            parent = os.path.dirname(target)
            if parent and not os.path.isdir(parent):
                os.makedirs(parent, exist_ok=True)
            with open(target, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(node.body or "")
            written.append(node.path)
        except OSError as exc:
            errors.append({"path": node.path, "detail": str(exc)})
    return _ok("generated",
               written=written, skipped=skipped, created_dirs=created_dirs,
               errors=errors,
               files=len(written), folders=len(created_dirs))


def format_plan(plan):
    """A dry-run plan as plain text."""
    lines = ["Dry run (nothing has been written yet)", ""]
    counts = {}
    for item in plan:
        counts[item["action"]] = counts.get(item["action"], 0) + 1
        lines.append("  %-10s %s%s" % (item["action"], item["path"],
                                       "" if not item.get("detail") else
                                       "   (%s)" % item["detail"]))
    lines += ["", "  create %d / overwrite %d / skip %d"
              % (counts.get("create", 0), counts.get("overwrite", 0),
                 counts.get("skip", 0))]
    return "\n".join(lines)
# ---------------------------------------------------------------------------
# Layer 10 -- the preset repository
# ---------------------------------------------------------------------------
class PresetRepository(object):
    """Stores presets on disk, one JSON file per scope, with versions.

    The scopes are searched user -> organization -> project -> global, so a
    user preset shadows a project one and a built-in is the last resort.  Each
    preset keeps a version list, which is what makes "structures evolve"
    possible without losing the older layout.
    """

    def __init__(self, directory=None, core=None):
        self.directory = os.path.abspath(directory or PRESET_DIR)
        self._builtins = builtin_presets()
        self._memory = {scope: {} for scope in PRESET_SCOPE_KEYS}
        self.load()

    # ---------------------------------------------------------- directories
    def path_for(self, scope):
        return os.path.join(self.directory, "%s.json" % scope)

    def ensure(self):
        os.makedirs(self.directory, exist_ok=True)
        return self.directory

    # ------------------------------------------------------------ persistence
    def load(self):
        in_memory = {scope: {} for scope in PRESET_SCOPE_KEYS}
        for scope in PRESET_SCOPE_KEYS:
            path = self.path_for(scope)
            if not os.path.isfile(path):
                continue
            try:
                payload = _read_json(path)
            except Exception:
                continue
            presets = payload.get("presets") if isinstance(payload, dict) \
                else None
            if isinstance(presets, dict):
                in_memory[scope] = presets
        self._memory = in_memory
        return in_memory

    def save(self, scope=None):
        self.ensure()
        scopes = [scope] if scope in PRESET_SCOPE_KEYS else PRESET_SCOPE_KEYS
        for key in scopes:
            payload = {"format": "scriptdb-presets", "scope": key,
                       "updated": _now(), "presets": self._memory[key]}
            try:
                _atomic_write_json(self.path_for(key), payload)
            except Exception:
                return False
        return True

    # ---------------------------------------------------------------- access
    def get(self, name, scope=None):
        """Find a preset: the named scope first, then the search order."""
        if scope and scope in self._memory and name in self._memory[scope]:
            return dict(self._memory[scope][name])
        for key in ("user", "organization", "project", "global"):
            if name in self._memory.get(key, {}):
                return dict(self._memory[key][name])
        if name in self._builtins:
            return dict(self._builtins[name])
        return {}

    def all(self, include_builtins=True):
        """Every preset as {name: preset}, user scopes shadowing the built-ins."""
        out = dict(self._builtins) if include_builtins else {}
        for key in ("global", "project", "organization", "user"):
            out.update(self._memory.get(key, {}))
        return out

    def names(self, scope=None, include_builtins=True):
        if scope:
            names = set(self._memory.get(scope, {}))
            if include_builtins:
                names |= set(self._builtins)
            return sorted(names)
        return sorted(self.all(include_builtins))

    def scope_of(self, name):
        for key in ("user", "organization", "project", "global"):
            if name in self._memory.get(key, {}):
                return key
        if name in self._builtins:
            return "global"
        return ""

    def save_preset(self, preset, scope="user"):
        """Write a preset into a scope; bumps its version and keeps history."""
        if scope not in PRESET_SCOPE_KEYS:
            return _fail("bad_scope", "unknown scope: %s" % scope)
        if not isinstance(preset, dict) or not preset.get("name"):
            return _fail("bad_preset", "a preset needs a name")
        preset = dict(preset)
        preset["scope"] = scope
        bucket = self._memory.setdefault(scope, {})
        previous = bucket.get(preset["name"]) or {}
        history = list(previous.get("history", []))
        version = int(previous.get("version", 0)) + 1
        if previous:
            history.append({"version": previous.get("version", 1),
                            "saved": previous.get("saved", ""),
                            "folders": len(previous.get("folders", [])),
                            "files": len(previous.get("files", []))})
        preset["version"] = version
        preset["saved"] = _now()
        preset["history"] = history[-50:]
        bucket[preset["name"]] = preset
        self.save(scope)
        return _ok("preset saved", name=preset["name"], version=version)

    def delete_preset(self, name, scope=None):
        scope = scope or self.scope_of(name)
        if scope in PRESET_SCOPE_KEYS and name in self._memory.get(scope, {}):
            self._memory[scope].pop(name, None)
            self.save(scope)
            return _ok("preset deleted", name=name)
        return _fail("not_found", "no such preset: %s" % name)

    def versions(self, name):
        preset = self.get(name)
        return list(preset.get("history", [])) + [
            {"version": preset.get("version", 1),
             "saved": preset.get("saved", ""),
             "folders": len(preset.get("folders", [])),
             "files": len(preset.get("files", []))}]

    def to_dict(self):
        return {"format": "scriptdb-presets-export", "exported": _now(),
                "presets": {scope: dict(bucket)
                            for scope, bucket in self._memory.items()}}


# ---------------------------------------------------------------------------
# Layer 11 -- the plugin registry
# ---------------------------------------------------------------------------
class PluginRegistry(object):
    """A tiny hot-plug registry for generators, language emitters and hooks.

    A plugin is any object with ``name`` and a ``generate(preset, values)`` that
    returns ``[(path, body), ...]``; hooks are plain callables.  Everything the
    tab offers is itself reachable as a plugin, so nothing is "special-cased".
    """

    def __init__(self):
        self.generators = {}
        self.hooks = {"pre": [], "post": []}
        self.enabled = {}

    def register(self, plugin, enabled=True):
        name = getattr(plugin, "name", None) or (
            plugin.get("name") if isinstance(plugin, dict) else None)
        if not name:
            return _fail("bad_plugin", "a plugin needs a name")
        self.generators[name] = plugin
        self.enabled[name] = bool(enabled)
        return _ok("plugin loaded", name=name)

    def unregister(self, name):
        self.generators.pop(name, None)
        self.enabled.pop(name, None)
        return _ok("plugin disabled", name=name)

    def add_hook(self, stage, hook):
        if stage in self.hooks and callable(hook):
            self.hooks[stage].append(hook)
            return True
        return False

    def run_hooks(self, stage, state):
        """Run the hooks of a stage in order; a broken hook is recorded, not fatal."""
        results = []
        for hook in self.hooks.get(stage, []):
            try:
                results.append(hook(state))
            except Exception as exc:
                results.append({"ok": False, "detail": str(exc)})
        return results

    def run_generators(self, preset, values, only=None):
        """Let every enabled generator add files; returns (path, body) pairs."""
        out = []
        for name, plugin in sorted(self.generators.items()):
            if not self.enabled.get(name):
                continue
            if only and name not in only:
                continue
            try:
                produced = plugin.generate(preset, values) \
                    if hasattr(plugin, "generate") else \
                    plugin.get("generate", lambda *a: [])()
            except Exception:
                continue
            for entry in produced or []:
                if isinstance(entry, (list, tuple)) and len(entry) == 2:
                    out.append((entry[0], entry[1]))
        return out

    def summary(self):
        return [{"name": name, "enabled": self.enabled.get(name, False)}
                for name in sorted(self.generators)]
# ---------------------------------------------------------------------------
# Layer 12 -- git, artifacts and export
# ---------------------------------------------------------------------------
def git_init_plan(root, message="Initial commit"):
    """The git commands a post-generation step would run (no execution here).

    Returning the commands rather than running them keeps the engine side-effect
    free; the tab runs them only when the user asks for a repository.
    """
    return [["git", "init"],
            ["git", "add", "-A"],
            ["git", "commit", "-m", message]]


def write_zip(root, source_paths, target, skip=NEVER_PACK):
    """Pack ``source_paths`` (relative to ``root``) into a zip at ``target``.

    Build noise and secrets named in ``skip`` are never packed, so an artifact
    cannot leak a virtual environment or a repository by accident.
    """
    try:
        os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
        count = 0
        packed = set()
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            for relative in sorted(source_paths):
                full = os.path.join(root, *relative.split("/"))
                if not os.path.exists(full):
                    continue
                top = relative.split("/")[0]
                if top in skip:
                    continue
                if os.path.isdir(full):
                    for base, dirs, files in os.walk(full):
                        dirs[:] = [d for d in dirs if d not in skip]
                        for name in files:
                            path = os.path.join(base, name)
                            arc = os.path.relpath(path, root).replace(os.sep,
                                                                    "/")
                            if arc in packed:
                                continue
                            packed.add(arc)
                            archive.write(path, arc)
                            count += 1
                else:
                    if relative in packed:
                        continue
                    packed.add(relative)
                    archive.write(full, relative)
                    count += 1
        return _ok("packed", path=target, files=count)
    except (OSError, zipfile.BadZipFile) as exc:
        return _fail("zip_failed", str(exc))


def export_preset_pack(presets, target):
    """Write a set of presets to a JSON pack (the preset export)."""
    try:
        payload = {"format": "scriptdb-preset-pack", "version": 1,
                   "exported": _now(), "presets": presets}
        _atomic_write_json(target, payload)
        return _ok("preset pack written", path=target,
                   count=len(presets or {}))
    except OSError as exc:
        return _fail("export_failed", str(exc))


def import_preset_pack(path):
    """Read a preset pack; returns the presets dict or an error."""
    try:
        payload = _read_json(path)
    except Exception as exc:
        return _fail("read_failed", str(exc))
    if not isinstance(payload, dict):
        return _fail("bad_pack", "the pack is not an object")
    if payload.get("format") not in ("scriptdb-preset-pack",
                                     "scriptdb-presets-export"):
        return _fail("bad_pack", "unrecognised pack format")
    presets = payload.get("presets") or {}
    return _ok("pack read", presets=presets)


def export_documentation(tree, target, title="Project structure"):
    """Render the tree as Markdown documentation."""
    lines = ["# %s" % title, "",
             "_generated %s_" % _now(), "", "```"]
    for node in tree:
        depth = node.path.count("/")
        name = node.path.split("/")[-1]
        lines.append("%s%s%s" % ("  " * depth, name,
                                   "/" if node.folder else ""))
    lines += ["```", ""]
    body = "\n".join(lines)
    try:
        with open(target, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(body)
        return _ok("documentation written", path=target)
    except OSError as exc:
        return _fail("write_failed", str(exc))


# ---------------------------------------------------------------------------
# Layer 13 -- reverse analysis
# ---------------------------------------------------------------------------
def analyze_project(root, max_files=400):
    """Read an existing project back into a preset-shaped dict.

    The walk skips the directories in :data:`IGNORED_DIRS`, counts folders and
    files, guesses the dominant language from the source extensions and keeps a
    small sample of paths so the result can be shown and re-generated.  Files
    are never read into memory: only the layout is described.
    """
    if not root or not os.path.isdir(root):
        return _fail("not_a_directory", "no such directory: %s" % root)
    folders = []
    files = []
    extension_counts = {}
    for base, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in IGNORED_DIRS)
        relative = os.path.relpath(base, root).replace(os.sep, "/")
        if relative != ".":
            folders.append(relative)
        for name in sorted(names):
            path = os.path.join(base, name)
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            files.append(rel)
            ext = os.path.splitext(name)[1].lower()
            extension_counts[ext] = extension_counts.get(ext, 0) + 1
            if len(files) >= max_files:
                break
        if len(files) >= max_files:
            break
    language = _guess_language(extension_counts)
    total_bytes = 0
    for rel in files[:200]:
        try:
            total_bytes += os.path.getsize(os.path.join(root, *rel.split("/")))
        except OSError:
            pass
    preset = {
        "name": _slug(os.path.basename(os.path.abspath(root)), "project"),
        "title": os.path.basename(os.path.abspath(root)),
        "kind": "application",
        "language": language,
        "scope": "user",
        "version": 1,
        "description": "reverse-engineered from %s" % root,
        "folders": folders,
        "files": [(rel, "") for rel in files],
        "modules": [],
        "tokens": {},
    }
    return _ok("analyzed", preset=preset, folders=len(folders),
               files=len(files), language=language,
               extensions=extension_counts, bytes=total_bytes,
               sample=files[:40])


def _guess_language(extension_counts):
    """Pick the language whose extensions dominate the project."""
    mapping = {ext: key for key, _lk, _f, ext, _n in STRUCTURE_LANGUAGES}
    best, best_count = "python", 0
    for ext, count in (extension_counts or {}).items():
        key = mapping.get(ext)
        if key and count > best_count:
            best, best_count = key, count
    return best


def suggest_missing(preset, root):
    """Files a preset promises that are absent (the "auto-complete" hints)."""
    _values, nodes = StructureGenerator().build_tree(preset)
    report = validate_structure(nodes, root)
    return report.get("missing_on_disk", [])


def migrate_preset(preset, target_version=2):
    """Upgrade an older preset to the current layout conventions.

    The migrations are additive and idempotent: v2 adds the standard folders and
    the manifest when they are missing, so running it twice changes nothing the
    second time.
    """
    out = dict(preset or {})
    changed = []
    if int(out.get("version", 1)) >= target_version:
        return _ok("already current", preset=out, changed=[])
    folders = list(out.get("folders", []))
    for folder in ("docs", "tests"):
        if folder not in folders:
            folders.append(folder)
            changed.append("added %s/" % folder)
    files = list(out.get("files", []))
    paths = {path for path, _body in files}
    if "README.md" not in paths:
        files.append(("README.md", README_BODY))
        changed.append("added README.md")
    if "manifest.json" not in paths:
        files.append(("manifest.json", MANIFEST_BODY))
        changed.append("added manifest.json")
    out["folders"] = folders
    out["files"] = files
    out["version"] = target_version
    return _ok("migrated", preset=out, changed=changed)
# ---------------------------------------------------------------------------
# Layer 14 -- the compiler (the pipeline)
# ---------------------------------------------------------------------------
class StructureCompiler(object):
    """Runs a preset through the pipeline and produces the artifacts.

    The stages are the ones in :data:`PIPELINE_STAGES`.  Each stage records what
    it did, so the run log is the record of the compilation and nothing is
    implied that did not happen.  The compiler never executes a plugin or a hook
    as a subprocess -- hooks are plain Python callables the caller supplies.
    """

    def __init__(self, generator=None, plugins=None):
        self.generator = generator or StructureGenerator()
        self.plugins = plugins or PluginRegistry()
        self.log = []

    def _record(self, stage, detail, **extra):
        entry = {"time": _now(), "stage": stage, "detail": detail}
        entry.update(extra)
        self.log.append(entry)
        return entry

    def compile(self, preset, name="", root="", extra=None, dry_run=False,
                overwrite=False, validate=True, stage_hooks=None):
        """Run one preset through the pipeline; returns a result dict."""
        if not isinstance(preset, dict) or not preset.get("name"):
            return _fail("bad_preset", "a preset needs a name")
        stage_hooks = stage_hooks or {}
        result = {"ok": True, "stage": "made", "preset": preset.get("name"),
                  "dry_run": dry_run, "log": self.log}
        values = self.generator.values_for(preset, name, extra)
        # --- pre -----------------------------------------------------------
        self._record("pre", "prepared", values=len(values))
        pre_state = {"preset": preset, "values": values, "root": root}
        self.plugins.run_hooks("pre", pre_state)
        if callable(stage_hooks.get("pre")):
            stage_hooks["pre"](pre_state)
        # --- generate ------------------------------------------------------
        values, nodes = self.generator.build_tree(preset, name, extra)
        plugin_files = self.plugins.run_generators(preset, values)
        if plugin_files:
            seen = {node.path.lower() for node in nodes}
            for path, body in plugin_files:
                if path.lower() in seen:
                    continue
                seen.add(path.lower())
                text, missing = render_template(body, values)
                node = TreeNode(path, text, template="plugin")
                node.missing = missing
                nodes.append(node)
        self._record("generate", "tree built", files=sum(
            1 for node in nodes if not node.folder),
            folders=sum(1 for node in nodes if node.folder))
        result["tree"] = nodes
        result["values"] = values
        # --- validate ------------------------------------------------------
        report = validate_structure(nodes, root if not dry_run else "")
        result["validation"] = report
        self._record("validate", "checked", errors=len(report["errors"]),
                     warnings=len(report["warnings"]))
        if validate and report["errors"]:
            result["ok"] = False
            result["stage"] = "validate"
            return result
        # --- write or dry-run ----------------------------------------------
        if dry_run:
            plan = plan_tree(nodes, root or os.getcwd())
            result["plan"] = plan
            self._record("dry_run", "plan built", actions=len(plan))
        elif root:
            outcome = commit_tree(nodes, root, overwrite=overwrite)
            result["commit"] = outcome
            if not outcome["ok"]:
                result["ok"] = False
                result["stage"] = "generate"
                return result
            self._record("generate", "written",
                         files=outcome.get("files", 0))
        # --- post ----------------------------------------------------------
        post_state = {"preset": preset, "values": values, "root": root,
                      "tree": nodes, "result": result}
        hook_results = self.plugins.run_hooks("post", post_state)
        if callable(stage_hooks.get("post")):
            stage_hooks["post"](post_state)
        result["hooks"] = hook_results
        self._record("post", "post steps run", hooks=len(hook_results))
        result["stage"] = "complete"
        return result


# ---------------------------------------------------------------------------
# Reports and the run log
# ---------------------------------------------------------------------------
def format_tree(nodes, max_lines=400):
    """The generated tree as an ASCII tree, for a preview."""
    lines = []
    for node in nodes[:max_lines]:
        label = node.path + ("/" if node.folder else "")
        marker = ""
        if node.missing:
            marker = "   <unfilled: %s>" % ",".join(sorted(set(node.missing)))
        lines.append("  " + label + marker)
    if len(nodes) > max_lines:
        lines.append("  ... %d more" % (len(nodes) - max_lines))
    return "\n".join(lines)


def structure_report(kind, result, preset=None, repository=None, extra=None):
    """A report on a compilation, as plain text.

    ``kind`` is one of: structure, validation, pipeline, presets, security.
    """
    preset = preset or {}
    result = result or {}
    extra = extra or {}
    lines = ["ScriptDB structure report",
             "kind: %s" % kind,
             "generated: %s" % _now(),
             "preset: %s" % preset.get("name", result.get("preset", "")),
             ""]
    if kind == "structure":
        lines += _report_structure(result)
    elif kind == "validation":
        lines += format_validation(result.get("validation", {})).splitlines()
    elif kind == "pipeline":
        lines += _report_pipeline(result)
    elif kind == "presets":
        lines += _report_presets(repository)
    elif kind == "security":
        lines += _report_security(result, preset)
    else:
        lines.append("unknown report kind")
    return "\n".join(lines) + "\n"


def _report_structure(result):
    nodes = result.get("tree", [])
    files = [n for n in nodes if not n.folder]
    folders = [n for n in nodes if n.folder]
    lines = ["Files:   %d" % len(files), "Folders: %d" % len(folders), "",
             format_tree(nodes)]
    return lines


def _report_pipeline(result):
    lines = ["Stage:   %s" % result.get("stage", ""),
             "Dry run: %s" % bool(result.get("dry_run")), ""]
    for entry in result.get("log", []):
        lines.append("  %s  %-10s %s" % (entry.get("time"), entry.get("stage"),
                                         entry.get("detail")))
    commit = result.get("commit") or {}
    if commit:
        lines += ["", "Written:  %d" % commit.get("files", 0),
                  "Skipped:  %d" % len(commit.get("skipped", [])),
                  "Errors:   %d" % len(commit.get("errors", []))]
    return lines


def _report_presets(repository):
    if repository is None:
        return ["no repository is open"]
    lines = ["Presets: %d" % len(repository.names()), ""]
    for name in repository.names():
        preset = repository.get(name)
        lines.append("  %-20s %-12s %-6s v%d  %d file(s)"
                     % (name, repository.scope_of(name),
                        preset.get("kind", ""), preset.get("version", 1),
                        len(preset.get("files", []))))
    return lines


def _report_security(result, preset):
    """The path-safety side of a compilation: no escape, no foreign write."""
    nodes = result.get("tree", [])
    unsafe = [n.path for n in nodes if not is_safe_relative(n.path)]
    plan = result.get("plan", [])
    outside = [item for item in plan if "escape" in (item.get("detail") or "")]
    lines = ["Unsafe paths in the tree: %d" % len(unsafe),
             "Escaping plan entries:    %d" % len(outside),
             "Preset scope:             %s" % preset.get("scope", ""),
             "Preset version:           %d" % preset.get("version", 1),
             ""]
    if unsafe:
        lines += ["Unsafe"] + ["  " + path for path in unsafe]
    else:
        lines.append("Every path stays inside the project.")
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
    handle, temp = tempfile.mkstemp(dir=directory, prefix=".st-", suffix=".tmp")
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


class StructureLog(object):
    """An append-only generation log, newest first, persisted as JSON."""

    def __init__(self, path=None):
        self.path = path or ""
        self.entries = []
        self.load()

    def load(self):
        if not self.path or not os.path.isfile(self.path):
            return self.entries
        try:
            payload = _read_json(self.path)
        except Exception:
            return self.entries
        entries = payload.get("entries") if isinstance(payload, dict) else None
        self.entries = entries if isinstance(entries, list) else []
        return self.entries

    def save(self):
        if not self.path:
            return False
        _atomic_write_json(self.path, {"format": "scriptdb-structure-log",
                                       "updated": _now(),
                                       "entries": self.entries[:500]})
        return True

    def record(self, event, detail="", **extra):
        if event not in STRUCTURE_EVENTS and not event.startswith("st_"):
            return None
        entry = {"time": _now(), "event": event, "detail": detail}
        entry.update(extra)
        self.entries.insert(0, entry)
        self.entries = self.entries[:500]
        self.save()
        return entry

    def rows(self, limit=200):
        return [(e.get("time", ""), e.get("event", ""), e.get("detail", ""))
                for e in self.entries[:limit]]
# ---------------------------------------------------------------------------
# Layer 15 -- the description parser (generate from a sentence)
# ---------------------------------------------------------------------------
# "make an RPG game in godot" is turned into preset overrides, so a person can
# start from a sentence instead of a form.  It is a keyword matcher, not a model
# -- it only ever picks from the values the tab already understands.
DESCRIPTION_HINTS = (
    ("game rpg", {"kind": "game", "language": "gdscript", "tokens":
                  {"genre": "rpg"}}),
    ("rpg", {"kind": "game", "tokens": {"genre": "rpg"}}),
    ("game", {"kind": "game"}),
    ("unity", {"kind": "game", "language": "csharp", "ide": "vscode"}),
    ("godot", {"kind": "game", "language": "gdscript"}),
    ("unreal", {"kind": "game", "language": "unreal"}),
    ("library", {"kind": "library"}),
    ("framework", {"kind": "framework"}),
    ("plugin", {"kind": "plugin"}),
    ("module", {"kind": "module"}),
    ("app", {"kind": "application"}),
    ("python", {"language": "python"}),
    ("c++", {"language": "cpp"}),
    ("cpp", {"language": "cpp"}),
    ("node", {"language": "node"}),
    ("javascript", {"language": "node"}),
    ("rust", {"language": "rust"}),
    ("java", {"language": "java"}),
    ("c#", {"language": "csharp"}),
    ("docker", {"tokens": {"docker": True}}),
    ("ci", {"ci": "github"}),
)


def parse_description(text):
    """Turn a sentence into preset overrides; returns a result dict.

    The result always carries the overrides found, the name it inferred and the
    words it recognised, so the tab can show what it understood rather than
    pretending to be clever.
    """
    text = (text or "").strip()
    if not text:
        return _fail("empty", "describe what you want to build")
    lowered = text.lower()
    overrides = {"tokens": {}}
    matched = []
    for keyword, change in DESCRIPTION_HINTS:
        if keyword in lowered:
            matched.append(keyword)
            for key, value in change.items():
                if key == "tokens":
                    overrides["tokens"].update(value)
                else:
                    overrides[key] = value
    name = _guess_name(text)
    overrides["name"] = name
    overrides["title"] = name
    if "description" not in overrides:
        overrides["description"] = text
    return _ok("parsed", overrides=overrides, name=name, matched=matched)


def _guess_name(text):
    """Infer a project name: a quoted word, else the last noun-ish token."""
    quoted = re.search(r"[\"']([^\"']{2,40})[\"']", text)
    if quoted:
        return _slug(quoted.group(1), "project")
    words = re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", text)
    stop = {"make", "create", "build", "the", "for", "with", "and", "new",
            "project", "generate", "structure", "game", "app", "library",
            "module", "plugin", "python", "rust", "node", "java"}
    for word in reversed(words):
        if word.lower() not in stop:
            return _slug(word, "project")
    return "project"


def apply_description(preset, text):
    """Apply a description to a base preset, returning a new preset."""
    parsed = parse_description(text)
    if not parsed["ok"]:
        return parsed
    out = dict(preset or {})
    for key, value in parsed["overrides"].items():
        if key == "tokens":
            merged = dict(out.get("tokens", {}))
            merged.update(value)
            out["tokens"] = merged
        else:
            out[key] = value
    return _ok("applied", preset=out, matched=parsed["matched"],
               name=parsed["name"])


# ---------------------------------------------------------------------------
# Layer 16 -- the high-level pipeline entry points
# ---------------------------------------------------------------------------
def default_stack():
    """The generator, plugin registry and compiler wired together."""
    generator = StructureGenerator()
    plugins = PluginRegistry()
    compiler = StructureCompiler(generator, plugins)
    return generator, plugins, compiler


def run_pipeline(preset, name="", root="", dry_run=False, overwrite=False,
                 make_zip=False, zip_target="", extra=None, repository=None):
    """One call that runs the whole pipeline and returns every artifact.

    It is what the CLI and the tab both call, so a run through the GUI and a run
    through the command line take exactly the same path.
    """
    generator, _plugins, compiler = default_stack()
    result = compiler.compile(preset, name=name, root=root, dry_run=dry_run,
                              overwrite=overwrite, extra=extra)
    artifacts = []
    if result.get("ok") and make_zip and root and not dry_run:
        paths = [node.path for node in result.get("tree", [])]
        if zip_target:
            packed = write_zip(root, paths, zip_target)
            if packed["ok"]:
                artifacts.append(packed["path"])
    if repository is not None and result.get("ok"):
        repository.save_preset(preset, preset.get("scope", "user"))
    result["artifacts"] = artifacts
    return result


def cli(argv):
    """A small command-line front end: init / generate / validate / compile.

    It parses a preset file and a target and calls :func:`run_pipeline`, so the
    CLI is a thin skin over the same engine the GUI uses.  It returns
    ``(exit_code, output_lines)`` rather than printing, so it is testable.
    """
    args = list(argv or [])
    out = []
    if not args:
        return 2, ["usage: structure <command> [options]",
                   "  init <preset> <target>",
                   "  generate <preset> <target> [--dry-run] [--json <file>]",
                   "  validate <preset>",
                   "  compile <preset> <target> [--zip <file>]"]
    command = args.pop(0)
    options = {"dry_run": False, "json": "", "zip": ""}
    positional = []
    while args:
        token = args.pop(0)
        if token == "--dry-run":
            options["dry_run"] = True
        elif token == "--json" and args:
            options["json"] = args.pop(0)
        elif token == "--zip" and args:
            options["zip"] = args.pop(0)
        else:
            positional.append(token)
    # The CLI only *reads* presets (the built-ins are always merged in), so its
    # repository points at a throw-away directory rather than the current
    # working directory -- a command-line run must never litter the folder it
    # was started from.
    repository = PresetRepository(tempfile.mkdtemp(prefix="structure-cli-"))
    if command in ("init", "generate", "compile"):
        if not positional:
            return 2, ["missing preset name"]
        preset = repository.get(positional[0])
        if not preset:
            return 1, ["no such preset: %s" % positional[0]]
        target = positional[1] if len(positional) > 1 else ""
        result = run_pipeline(preset, name=preset.get("name", ""), root=target,
                              dry_run=options["dry_run"] or command == "generate",
                              make_zip=bool(options["zip"]),
                              zip_target=options["zip"], repository=repository)
        out.append("preset: %s" % preset.get("name"))
        out.append("tree:   %d node(s)"
                   % len(result.get("tree", [])))
        if options["dry_run"]:
            out.append(format_plan(result.get("plan", [])))
        out.append(format_validation(result.get("validation", {})))
        if options["json"]:
            _atomic_write_json(options["json"], {
                "preset": preset.get("name"), "ok": result.get("ok"),
                "validation": result.get("validation"),
                "files": [n.path for n in result.get("tree", [])
                          if not n.folder]})
            out.append("wrote %s" % options["json"])
        return (0 if result.get("ok") else 1), out
    if command == "validate":
        if not positional:
            return 2, ["missing preset name"]
        preset = repository.get(positional[0])
        if not preset:
            return 1, ["no such preset: %s" % positional[0]]
        _values, nodes = StructureGenerator().build_tree(preset)
        report = validate_structure(nodes)
        return (0 if report["ok"] else 1), \
            format_validation(report).splitlines()
    return 2, ["unknown command: %s" % command]
