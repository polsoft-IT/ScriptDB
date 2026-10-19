# -*- coding: utf-8 -*-
"""Code Generator for the ScriptDB editor.
    
This module is the engine behind the editor's "Code generation" menu.  It is
one step above the template engine in ``tabs/templates.py``: a template is a
fixed block of text, while the generator *derives* the code from something the
project already knows.
    
    CodeGenerator.generate(kind, source)   -- build one artifact
    generate_from_config(text)             -- a config.json / config.yaml
    generate_from_metadata(text)           -- project metadata
    generate_from_json(text, type)         -- a JSON/YAML document
    generate_from_api(text)                -- an API definition
    generate_from_modules(text)            -- a module definition
    generate_from_project(path)            -- a whole project tree
    
What it generates, and from what:
    
    config    -> initialisation code (constants, defaults, a loader)
    metadata  -> a project descriptor module (name, version, description)
    json      -> a dataclass per object with a from_dict/from_json loader
    api       -> a client with one method per path and a handler skeleton
    modules   -> a package: __init__, one starter per module, a manifest
    project   -> a project scaffold (README, config, entry point, tests)
    
Design rules, the same ones the rest of the editor follows:
    
    * standard library and PyQt5 only, printable ASCII identifiers;
    * nothing is executed -- JSON, YAML-ish and Python are read statically;
    * every artifact is plain text, so it can be dropped straight into the
      editor buffer, exported, or written to disk;
    * a source that cannot be understood yields a clear error, never a
      half-guessed artifact.
    
The generator is deliberately hot-pluggable: ``register`` adds a new kind at
run time, and the editor menu is rebuilt from ``kinds()``, so a plugin can
teach the editor a new artifact without touching ``tabs/editor.py``.
    """

import json
import os
import re


# --------------------------------------------------------------------------
# The catalogue.  Each entry is
# (key, label key, fallback label, what it reads, what it produces).
# The key is the one ``generate`` dispatches on, so the table cannot promise a
# kind the engine does not actually build.
# --------------------------------------------------------------------------
CODE_GENERATOR_KINDS = (
    ("config", "codegen_kind_config", "Config -> initialisation",
     "config.json / config.yaml / config.ini / .env",
     "constants, defaults and a loader that reads the config back"),
    ("metadata", "codegen_kind_metadata", "Metadata -> project module",
     "project metadata (JSON or key/value pairs)",
     "a project descriptor module with name, version and description"),
    ("json", "codegen_kind_json", "JSON/YAML -> data classes",
     "a JSON or YAML document",
     "one dataclass per object, with from_dict / from_json loaders"),
    ("api", "codegen_kind_api", "API -> client",
     "an API definition (paths, methods, parameters)",
     "a client with one method per endpoint and a handler skeleton"),
    ("modules", "codegen_kind_modules", "Modules -> package",
     "a module definition (name, dependencies, exports)",
     "a package: __init__, a starter per module and a manifest"),
    ("project", "codegen_kind_project", "Project -> scaffold",
     "a project directory or a project name",
     "README, config, entry point, package and a test"),
)

CODE_GENERATOR_KEYS = tuple(key for key, _lk, _f, _r, _g in CODE_GENERATOR_KINDS)
CODE_GENERATOR_LABELS = {key: fallback
                         for key, _lk, fallback, _r, _g in CODE_GENERATOR_KINDS}
CODE_GENERATOR_READS = {key: reads
                        for key, _lk, _f, reads, _g in CODE_GENERATOR_KINDS}
CODE_GENERATOR_GIVES = {key: gives
                        for key, _lk, _f, _r, gives in CODE_GENERATOR_KINDS}

# File extensions the generator understands as "JSON-like" and as config.
JSON_EXTENSIONS = (".json", ".json5", ".yaml", ".yml", ".toml")
CONFIG_EXTENSIONS = (".json", ".yaml", ".yml", ".ini", ".cfg", ".toml", ".env",
                     ".properties")
PYTHON_RESERVED = frozenset((
    "False", "None", "True", "and", "as", "assert", "async", "await", "break",
    "class", "continue", "def", "del", "elif", "else", "except", "finally",
    "for", "from", "global", "if", "import", "in", "is", "lambda", "nonlocal",
    "not", "or", "pass", "raise", "return", "try", "while", "with", "yield",
))


# --------------------------------------------------------------------------
# Small helpers, shared by every generator.
# --------------------------------------------------------------------------
def slugify(text, fallback="value"):
    """A printable-ASCII Python identifier derived from arbitrary text.

    ``"User Account-Id"`` -> ``"user_account_id"``, ``"1st"`` -> ``"item_1st"``.
    """
    text = (text or "").strip()
    text = re.sub(r"[^0-9A-Za-z]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_").lower()
    if not text:
        return fallback
    if text[0].isdigit():
        text = "item_" + text
    if text in PYTHON_RESERVED:
        text = text + "_value"
    return text


def constant_name(text, fallback="SETTING"):
    """``"api_timeout"`` -> ``"API_TIMEOUT"``."""
    return slugify(text, fallback.lower()).upper()


def class_name(text, fallback="Item"):
    """``"user account"`` -> ``"UserAccount"``."""
    pieces = [piece for piece in slugify(text, fallback.lower()).split("_")
              if piece]
    if not pieces:
        return fallback
    return "".join(piece[:1].upper() + piece[1:] for piece in pieces)


def python_string(value):
    """A single-quoted, ASCII-safe Python literal for ``value``."""
    if not isinstance(value, str):
        return repr(value)
    text = value.encode("ascii", "backslashreplace").decode("ascii")
    text = text.replace("\\", "\\\\").replace("'", "\\'")
    text = text.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return "'%s'" % text


def python_literal(value, indent=0):
    """Render a Python value, recursively and deterministically."""
    pad = " " * indent
    inner = "    " * (indent + 4)
    if isinstance(value, dict):
        if not value:
            return "{}"
        items = ["%s%s: %s" % (inner, python_string(str(key)) if
                               not _is_identifier(key) else str(key),
                               python_literal(item, indent + 4))
                 for key, item in value.items()]
        return "{\n" + ",\n".join(items) + ",\n" + pad + "}"
    if isinstance(value, (list, tuple)):
        if not value:
            return "[]"
        items = ["%s%s" % (inner, python_literal(item, indent + 4))
                 for item in value]
        return "[\n" + ",\n".join(items) + ",\n" + pad + "]"
    return python_string(value)


def _is_identifier(value):
    """True when ``value`` can be used as a bare Python key."""
    return bool(re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", str(value or "")))


def _pascal_words(text):
    """Split a name into words, whatever the case convention was."""
    text = re.sub(r"[^0-9A-Za-z]+", "_", str(text or ""))
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", text)
    return [word for word in text.split("_") if word]


def infer_type(value):
    """The Python type name that best describes a JSON-ish value."""
    if value is None:
        return "None"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, dict):
        return "dict"
    if isinstance(value, (list, tuple)):
        return "list"
    return "object"


# --------------------------------------------------------------------------
# Readers: JSON, YAML-ish and key/value, all static and dependency-free.
# --------------------------------------------------------------------------
def parse_document(text):
    """Parse a JSON or YAML-ish document into Python data.

    Returns ``(data, error)``.  JSON is parsed with the standard library; the
    YAML reader handles the subset a configuration file actually uses --
    nested mappings, sequences, scalars, comments -- and is used as a
    fallback, so no third-party dependency is needed.
    """
    text = (text or "").strip()
    if not text:
        return None, "the document is empty"
    try:
        return json.loads(text), None
    except ValueError:
        pass
    try:
        return parse_yaml(text), None
    except ValueError as exc:
        return None, "cannot read the document: %s" % exc


def parse_yaml(text):
    """A small, dependency-free reader for the YAML subset used by configs."""
    root = {}
    stack = [(-1, root)]
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    index = 0
    while index < len(lines):
        raw = lines[index]
        index += 1
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        line = raw.strip()
        if line in ("---", "..."):
            continue
        # A sequence entry belongs to the list opened just above it.
        if line.startswith("- "):
            item_text = line[2:].strip()
            while stack and stack[-1][0] >= indent:
                stack.pop()
            parent = stack[-1][1] if stack else root
            if not isinstance(parent, list):
                # The key above was a mapping value that turns out to be a
                # list; rebuild it as one.
                raise ValueError("a list entry has no list to belong to")
            item, index = _yaml_item(item_text, lines, index)
            parent.append(item)
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().strip("'\"")
        value = value.strip()
        while stack and stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1] if stack else root
        if not isinstance(parent, dict):
            raise ValueError("cannot nest '%s' under a list" % key)
        if value in ("", "|", ">"):
            # Look ahead: a deeper line makes this a container.
            nxt = _next_meaningful(lines, index)
            if nxt is not None and nxt[0] > indent and nxt[1].lstrip().startswith("- "):
                container = []
            else:
                container = {}
            parent[key] = container
            stack.append((indent, container))
        else:
            parent[key] = _yaml_scalar(value)
    return root


def _next_meaningful(lines, index):
    while index < len(lines):
        raw = lines[index]
        if raw.strip() and not raw.lstrip().startswith("#"):
            return len(raw) - len(raw.lstrip(" ")), raw.strip()
        index += 1
    return None


def _yaml_item(item_text, lines, index):
    """One sequence entry: a scalar, or the head of a mapping."""
    if not item_text:
        nxt = _next_meaningful(lines, index)
        return ({} if nxt is None or not nxt[1].startswith("- ") else []), index
    if ":" in item_text and not item_text.startswith(("'", '"')):
        key, _, value = item_text.partition(":")
        entry = {key.strip().strip("'\""): _yaml_scalar(value.strip())}
        return entry, index
    return _yaml_scalar(item_text), index


def _yaml_scalar(value):
    """Read a YAML scalar as the Python value it means."""
    text = (value or "").strip()
    if not text:
        return ""
    if text[0] in "\"'" and text[-1:] == text[0] and len(text) > 1:
        return text[1:-1]
    lowered = text.lower()
    if lowered in ("true", "yes", "on"):
        return True
    if lowered in ("false", "no", "off"):
        return False
    if lowered in ("null", "none", "~"):
        return None
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [_yaml_scalar(part) for part in inner.split(",")]
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return text


def parse_key_values(text):
    """Parse ``key = value`` / ``key: value`` pairs (config.ini, .env, args)."""
    data = {}
    for raw in (text or "").replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        for separator in ("=", ":"):
            if separator in line:
                key, _, value = line.partition(separator)
                data[key.strip().strip("'\"")] = _yaml_scalar(value)
                break
    return data


def read_source(source):
    """Turn a path, or a body of text, into ``(text, name, error)``."""
    if source is None:
        return "", "no source was given"
    text = str(source)
    looks_like_path = ("\n" not in text and len(text) < 400
                       and os.path.exists(text))
    if looks_like_path and os.path.isfile(text):
        try:
            with open(text, "r", encoding="utf-8", errors="replace") as handle:
                return handle.read(), os.path.basename(text), None
        except OSError as exc:
            return "", "cannot read %s: %s" % (text, exc)
    return text, "", None


# --------------------------------------------------------------------------
# The generator.
# --------------------------------------------------------------------------
class CodeGenerator(object):
    """Turn a project artifact into ready-to-paste code.

    Every ``generate_*`` method returns the same shape::

        {"ok": True, "kind": ..., "name": ..., "files": [(path, code), ...],
         "notes": [...], "summary": "...", "language": "python"}

    or ``{"ok": False, "kind": ..., "error": "..."}`` when the source cannot
    be understood.  One artifact can be several files (a package, a project
    scaffold), which is why ``files`` is a list rather than a single string;
    ``single()`` returns the first body for the one-file kinds.
    """

    # --------------------------------------------------------------- API
    def kinds(self):
        """The catalogue, for a menu or an about page."""
        return tuple(CODE_GENERATOR_KINDS)

    def keys(self):
        return CODE_GENERATOR_KEYS

    def describe(self, key):
        """A plugin-safe description of one generator kind."""
        if key not in CODE_GENERATOR_KEYS:
            return None
        return {
            "key": key,
            "label": CODE_GENERATOR_LABELS[key],
            "reads": CODE_GENERATOR_READS[key],
            "gives": CODE_GENERATOR_GIVES[key],
            "status": "available",
        }

    def generate(self, kind, source=None, **options):
        """Dispatch to one generator by key."""
        handlers = {
            "config": self.generate_from_config,
            "metadata": self.generate_from_metadata,
            "json": self.generate_from_json,
            "api": self.generate_from_api,
            "modules": self.generate_from_modules,
            "project": self.generate_from_project,
        }
        handler = handlers.get(kind)
        if handler is None:
            return self._failed(kind, "unknown generator: %s" % kind)
        try:
            return handler(source, **options)
        except Exception as exc:                  # never break the editor
            return self._failed(kind, "%s: %s" % (type(exc).__name__, exc))

    @staticmethod
    def single(result):
        """The single body of a result, or an empty string."""
        if not result or not result.get("ok"):
            return ""
        files = result.get("files") or []
        return files[0][1] if files else ""

    # ------------------------------------------------------------ results
    def _ok(self, kind, name, files, notes=(), language="python"):
        files = [(path, body) for path, body in files if body is not None]
        return {
            "ok": True, "kind": kind, "name": name, "files": files,
            "notes": list(notes), "language": language,
            "summary": "%s: %d file(s)" % (CODE_GENERATOR_LABELS.get(kind, kind),
                                           len(files)),
        }

    def _failed(self, kind, error):
        return {"ok": False, "kind": kind, "error": error, "files": [],
                "notes": [], "summary": "%s failed: %s" % (kind, error)}

    # ------------------------------------------------------------ config
    def generate_from_config(self, source=None, module_name="settings",
                             **options):
        """Generate initialisation code from a config file.

        The output is a self-contained module: one constant per key (with the
        nested path flattened), a ``DEFAULTS`` mapping, and a ``load_config``
        that reads the same file back at run time and falls back to the
        defaults.  That is the "config.json -> ready initialisation code" case.
        """
        text, name, error = read_source(source)
        if error:
            return self._failed("config", error)
        data, error = parse_document(text)
        if error or not isinstance(data, dict):
            data = parse_key_values(text)
        if not data:
            return self._failed("config", "no settings were found in the source")

        module = slugify(module_name or name or "settings", "settings")
        flat = _flatten(data)
        lines = [
            "# -*- coding: utf-8 -*-",
            '"""Generated initialisation from %s."""' % (name or "the config"),
            "",
            "import json",
            "import os",
            "",
            "# --- settings -----------------------------------------------------",
        ]
        for path, value in flat:
            lines.append("%s = %s" % (constant_name(path), python_literal(value)))
        lines += [
            "",
            "# --- defaults -----------------------------------------------------",
            "DEFAULTS = {",
        ]
        for path, value in flat:
            lines.append("    %s: %s," % (python_string(path),
                                          python_literal(value, 4)))
        lines += [
            "}",
            "",
            "CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),",
            "                           %s)" % python_string(name or "config.json"),
            "",
            "",
            "def load_config(path=None):",
            '    """Read the config file, falling back to DEFAULTS."""',
            "    target = path or CONFIG_PATH",
            "    settings = dict(DEFAULTS)",
            "    try:",
            "        with open(target, 'r', encoding='utf-8') as handle:",
            "            loaded = json.load(handle)",
            "    except (OSError, ValueError):",
            "        return settings",
            "    if isinstance(loaded, dict):",
            "        settings.update(_flatten(loaded))",
            "    return settings",
            "",
            "",
            "def _flatten(data, prefix=''):",
            '    """Flatten a nested mapping into dotted keys."""',
            "    flat = {}",
            "    for key, value in (data or {}).items():",
            "        path = '%s.%s' % (prefix, key) if prefix else str(key)",
            "        if isinstance(value, dict):",
            "            flat.update(_flatten(value, path))",
            "        else:",
            "            flat[path] = value",
            "    return flat",
            "",
            "",
            "def get(key, default=None):",
            '    """One setting by its dotted path."""',
            "    return load_config().get(key, default)",
            "",
        ]
        notes = ["%d setting(s) read" % len(flat)]
        if options.get("with_sections"):
            lines = _insert_section_comments(lines, data)
            notes.append("sections labelled")
        return self._ok("config", module + ".py", [("%s.py" % module,
                                                   "\n".join(lines))], notes)

    # ---------------------------------------------------------- metadata
    def generate_from_metadata(self, source=None, module_name="project",
                               **options):
        """Generate a project descriptor module from metadata."""
        text, name, error = read_source(source)
        if error:
            return self._failed("metadata", error)
        data, error = parse_document(text)
        if error or not isinstance(data, dict):
            data = parse_key_values(text)
            if not data:
                # A bare value is treated as the project name.
                data = {"name": text.strip()} if text.strip() else {}
        if not data:
            return self._failed("metadata", "no metadata was found in the source")

        module = slugify(module_name or data.get("name") or "project", "project")
        project = str(data.get("name") or data.get("title") or module)
        version = str(data.get("version") or "0.1.0")
        description = str(data.get("description") or "")
        author = str(data.get("author") or "")
        tags = data.get("tags") or []
        if isinstance(tags, str):
            tags = [part.strip() for part in tags.split(",") if part.strip()]

        lines = [
            "# -*- coding: utf-8 -*-",
            '"""Project descriptor for %s."""' % project,
            "",
            'NAME = %s' % python_string(project),
            "VERSION = %s" % python_string(version),
            "DESCRIPTION = %s" % python_string(description),
            "AUTHOR = %s" % python_string(author),
            "TAGS = %s" % python_literal(list(tags)),
            "",
            "METADATA = {",
        ]
        for key, value in data.items():
            lines.append("    %s: %s," % (python_string(str(key)),
                                          python_literal(value, 4)))
        lines += [
            "}",
            "",
            "",
            "def describe():",
            '    """The project as a plain dictionary."""',
            "    info = dict(METADATA)",
            "    info.update({'name': NAME, 'version': VERSION,",
            "                 'description': DESCRIPTION, 'author': AUTHOR})",
            "    return info",
            "",
            "",
            "def as_markdown():",
            '    """The project as a short README header."""',
            "    lines = ['# %s' % NAME, '']",
            "    if DESCRIPTION:",
            "        lines += [DESCRIPTION, '']",
            "    lines.append('%s  |  version %s' % (AUTHOR or 'unknown', VERSION))",
            "    return '\\n'.join(lines)",
            "",
            "",
            'if __name__ == "__main__":',
            "    print(as_markdown())",
            "",
        ]
        return self._ok("metadata", module + ".py",
                        [("%s.py" % module, "\n".join(lines))],
                        ["%d metadata field(s) read" % len(data)])

    # -------------------------------------------------------------- JSON
    def generate_from_json(self, source=None, root_class="", **options):
        """Generate dataclasses with loaders from a JSON/YAML document.

        One class per nested object; lists of objects become ``List[Class]``
        fields and are loaded element by element, so the generated module
        round-trips the document it was generated from.
        """
        text, name, error = read_source(source)
        if error:
            return self._failed("json", error)
        data, error = parse_document(text)
        if error:
            return self._failed("json", error)
        if data is None:
            return self._failed("json", "the document has no content")

        base = class_name(root_class or os.path.splitext(name or "Model")[0]
                          or "Model", "Model")
        models = {}
        _collect_models(data, base, models)
        lines = [
            "# -*- coding: utf-8 -*-",
            '"""Data classes generated from %s."""' % (name or "the document"),
            "",
            "from dataclasses import asdict, dataclass, field",
            "from typing import Any, Dict, List, Optional",
            "",
            "",
        ]
        for class_key in sorted(models, key=_model_order(models)):
            lines += _render_model(class_key, models[class_key])
            lines += ["", ""]
        lines += [
            "def load(data):",
            '    """Build %s from a parsed document."""' % base,
            "    if isinstance(data, str):",
            "        import json as _json",
            "        data = _json.loads(data)",
            "    return %s.from_dict(data)" % base,
            "",
            "",
            "def dump(model):",
            '    """The document back, as a plain dictionary."""',
            "    return asdict(model)",
            "",
        ]
        return self._ok("json", base + ".py", [("%s.py" % slugify(base),
                                                "\n".join(lines))],
                        ["%d data class(es) generated" % len(models)])

    # --------------------------------------------------------------- API
    def generate_from_api(self, source=None, client_name="", **options):
        """Generate an API client and a handler skeleton from a definition.

        Accepted input, first match wins:

            * an OpenAPI document (``paths`` with per-method definitions);
            * a JSON list of ``{"path": ..., "method": ...}`` entries;
            * plain text lines such as ``GET /users/{id} - fetch one user``.
        """
        text, name, error = read_source(source)
        if error:
            return self._failed("api", error)
        endpoints, title = _read_api(text)
        if not endpoints:
            return self._failed(
                "api", "no endpoints were found (expected an OpenAPI document, "
                       "a JSON list or 'METHOD /path' lines)")

        client = class_name(client_name or title or "Api", "Api") + "Client"
        base_url = ""
        try:
            parsed, _error = parse_document(text)
            if isinstance(parsed, dict):
                base_url = str(parsed.get("base_url")
                               or parsed.get("host")
                               or (parsed.get("servers") or [{}])[0].get("url")
                               or "")
        except Exception:
            base_url = ""

        lines = [
            "# -*- coding: utf-8 -*-",
            '"""API client generated from %s."""' % (name or title or "the definition"),
            "",
            "import json",
            "import urllib.parse",
            "import urllib.request",
            "",
            "BASE_URL = %s" % python_string(base_url or "http://localhost"),
            "",
            "",
            "class ApiError(Exception):",
            '    """An HTTP call that did not succeed."""',
            "",
            "    def __init__(self, status, body=''):",
            "        super().__init__('HTTP %s: %s' % (status, body[:200]))",
            "        self.status = status",
            "        self.body = body",
            "",
            "",
            "class %s(object):" % client,
            '    """One method per endpoint, plus a shared request helper."""',
            "",
            "    def __init__(self, base_url=BASE_URL, headers=None, opener=None):",
            "        self.base_url = (base_url or BASE_URL).rstrip('/')",
            "        self.headers = dict(headers or {})",
            "        self.opener = opener or urllib.request.urlopen",
            "",
            "    def request(self, method, path, params=None, body=None):",
            '        """Send one call and parse the JSON answer."""',
            "        query = ('?%s' % urllib.parse.urlencode(params)) if params else ''",
            "        url = self.base_url + path + query",
            "        payload = None",
            "        headers = dict(self.headers)",
            "        if body is not None:",
            "            payload = json.dumps(body).encode('utf-8')",
            "            headers['Content-Type'] = 'application/json'",
            "        request = urllib.request.Request(url, data=payload,",
            "                                         headers=headers,",
            "                                         method=method.upper())",
            "        try:",
            "            with self.opener(request) as answer:",
            "                raw = answer.read().decode('utf-8')",
            "        except Exception as exc:",
            "            raise ApiError(getattr(exc, 'code', 0), str(exc))",
            "        try:",
            "            return json.loads(raw)",
            "        except ValueError:",
            "            return raw",
            "",
        ]
        used = set()
        for entry in endpoints:
            method = entry["method"]
            path = entry["path"]
            name_key = _method_name(entry, used)
            used.add(name_key)
            args = _path_arguments(path)
            signature = ["self", "path_params=None", "params=None", "body=None"]
            lines += [
                "    def %s(%s):" % (name_key, ", ".join(signature)),
                '        """%s %s -- %s"""' % (
                    method.upper(), path,
                    entry.get("summary") or "generated call"),
            ]
            if args:
                lines.append("        values = dict(path_params or {})")
                for arg in args:
                    lines.append("        if '%s' not in values:" % arg)
                    lines.append("            raise ValueError(")
                    lines.append("                'missing path parameter: %s')" % arg)
                lines.append("        filled = %s" % python_string(path))
                for arg in args:
                    lines.append("        filled = filled.replace('{%s}'," % arg)
                    lines.append("                          urllib.parse.quote("
                                 "str(values['%s'])))" % arg)
            else:
                lines.append("        filled = %s" % python_string(path))
            lines += [
                "        return self.request(%s, filled, params, body)" % python_string(method.upper()),
                "",
            ]
        lines += [
            "",
            "class %sHandler(object):" % client[:-6] if client.endswith("Client")
            else "class ApiHandler(object):",
            '    """One method per endpoint on the server side."""',
            "",
        ]
        used = set()
        for entry in endpoints:
            name_key = _method_name(entry, used)
            used.add(name_key)
            lines += [
                "    def %s(self, request, **kwargs):" % name_key,
                '        """%s %s"""' % (entry["method"].upper(), entry["path"]),
                "        raise NotImplementedError(%s)" % python_string(
                    "%s %s" % (entry["method"].upper(), entry["path"])),
                "",
            ]
        lines += [
            "",
            "ENDPOINTS = %s" % python_literal(
                [(entry["method"].upper(), entry["path"]) for entry in endpoints], 0),
            "",
        ]
        return self._ok("api", slugify(client, "api") + ".py",
                        [("%s.py" % slugify(client, "api"), "\n".join(lines))],
                        ["%d endpoint(s) generated" % len(endpoints)])

    # ----------------------------------------------------------- modules
    def generate_from_modules(self, source=None, package="app", **options):
        """Generate a package from a module definition.

        The definition is a JSON/YAML document, a list of names, or lines such
        as ``core: file system helpers``.  Each module becomes a starter
        module with a docstring, a constant, a class and a function, wired
        into ``__init__.py`` and described in ``modules.json``.
        """
        text, name, error = read_source(source)
        if error:
            return self._failed("modules", error)
        modules = _read_modules(text)
        if not modules:
            return self._failed("modules", "no modules were defined in the source")

        package = slugify(package or "app", "app")
        init_lines = [
            "# -*- coding: utf-8 -*-",
            '"""%s package -- generated by the ScriptDB code generator."""' % package,
            "",
        ]
        files = []
        manifest = []
        for module in modules:
            key = slugify(module["name"], "module")
            description = module.get("description") or ("the %s module" % key)
            dependencies = module.get("imports") or []
            exports = module.get("exports") or []
            body = [
                "# -*- coding: utf-8 -*-",
                '"""%s -- %s."""' % (key, description),
                "",
                "NAME = %s" % python_string(key),
                "DESCRIPTION = %s" % python_string(description),
                "",
            ]
            if dependencies:
                body += ["# Dependencies declared by the module definition:"]
                for dependency in dependencies:
                    body.append("#   - %s" % dependency)
                body.append("")
            body += [
                "",
                "class %s(object):" % class_name(key),
                '    """The %s component."""' % key,
                "",
                "    def __init__(self, name=None):",
                "        self.name = name or NAME",
                "",
                "    def describe(self):",
                '        """What this component is."""',
                "        return {'name': self.name, 'description': DESCRIPTION}",
                "",
                "",
                "def run(**options):",
                '    """Entry point for the %s module."""' % key,
                "    component = %s()" % class_name(key),
                "    return component.describe()",
                "",
            ]
            files.append(("%s/%s.py" % (package, key), "\n".join(body)))
            init_lines.append("from .%(key)s import %(cls)s, run as run_%(key)s"
                              % {"key": key, "cls": class_name(key)})
            manifest.append({"name": key, "description": description,
                             "dependencies": list(dependencies),
                             "exports": [class_name(key), "run"] + list(exports)})

        init_lines += ["", "MODULES = %s" % python_literal(
            [item["name"] for item in manifest], 0), ""]
        files.insert(0, ("%s/__init__.py" % package, "\n".join(init_lines)))
        files.append(("%s/modules.json" % package,
                      json.dumps({"package": package, "modules": manifest},
                                 indent=2, sort_keys=False) + "\n"))
        root = [
            "# -*- coding: utf-8 -*-",
            '"""Entry point for the %s package."""' % package,
            "",
            "from %s import MODULES" % package,
            "",
            "",
            "def main():",
            '    """Describe every module the package ships."""',
            "    for key in MODULES:",
            "        print(key)",
            "    return len(MODULES)",
            "",
            "",
            'if __name__ == "__main__":',
            "    main()",
            "",
        ]
        files.append(("run_%s.py" % package, "\n".join(root)))
        return self._ok("modules", package + "/",
                        files, ["%d module(s) generated" % len(manifest)])

    # ----------------------------------------------------------- project
    def generate_from_project(self, source=None, project_name="", **options):
        """Generate a project scaffold from a directory, a config or a name.

        When the source is a directory, README, config files and package
        folders are discovered and folded into the scaffold: the entry point
        lists the packages it found and the config carries the discovered
        root.  When the source is text, it is read as a project name and
        description.
        """
        root = ""
        discovered = []
        description = ""
        if source and "\n" not in str(source) and os.path.isdir(str(source)):
            root = os.path.abspath(str(source))
            discovered = _scan_project(root)
            project_name = project_name or os.path.basename(root)
        else:
            text, _name, error = read_source(source)
            if error:
                return self._failed("project", error)
            data, _error = parse_document(text)
            if isinstance(data, dict) and (data.get("name") or data.get("title")):
                project_name = project_name or str(
                    data.get("name") or data.get("title") or "")
                description = str(data.get("description") or "")
            elif text.strip():
                first, _, rest = text.strip().partition("\n")
                project_name = project_name or first.strip()
                description = rest.strip()

        # Only the first line names the project; anything after it is prose.
        if project_name and "\n" in project_name:
            project_name = project_name.strip().splitlines()[0]
        package = slugify(project_name or "project", "project")
        title = project_name or package
        readme = [
            "# %s" % title,
            "",
            description or "Generated by the ScriptDB code generator.",
            "",
            "## Layout",
            "",
            "    %s/          the package" % package,
            "    run_%s.py    the entry point" % package,
            "    config.json  the default configuration",
            "    tests/       the test package",
            "",
        ]
        if discovered:
            readme += ["## Discovered in the source project", ""]
            readme += ["    %s" % item for item in discovered]
            readme += [""]
        readme += [
            "## Running",
            "",
            "    python run_%s.py" % package,
            "",
        ]
        config = {
            "name": title,
            "version": "0.1.0",
            "debug": False,
            "root": root or ".",
            "packages": sorted({item.split("/")[0] for item in discovered
                                if "/" in item}),
        }
        init_body = [
            "# -*- coding: utf-8 -*-",
            '"""%s -- generated package."""' % title,
            "",
            'NAME = %s' % python_string(title),
            'VERSION = %s' % python_string("0.1.0"),
            "",
            "",
            "def describe():",
            '    """The package as a plain dictionary."""',
            "    return {'name': NAME, 'version': VERSION}",
            "",
        ]
        runner = [
            "# -*- coding: utf-8 -*-",
            '"""Entry point for %s."""' % title,
            "",
            "import json",
            "import os",
            "",
            "from %s import describe" % package,
            "",
            "",
            "def load_config(path=None):",
            '    """Read config.json next to this file."""',
            "    target = path or os.path.join(os.path.dirname(os.path.abspath(__file__)),",
            "                                  'config.json')",
            "    try:",
            "        with open(target, 'r', encoding='utf-8') as handle:",
            "            return json.load(handle)",
            "    except (OSError, ValueError):",
            "        return {}",
            "",
            "",
            "def main():",
            '    """Describe the package and its configuration."""',
            "    info = describe()",
            "    info['config'] = load_config()",
            "    print(json.dumps(info, indent=2, sort_keys=True))",
            "    return 0",
            "",
            "",
            'if __name__ == "__main__":',
            "    raise SystemExit(main())",
            "",
        ]
        test = [
            "# -*- coding: utf-8 -*-",
            '"""Tests for %s."""' % title,
            "",
            "from %s import describe" % package,
            "",
            "",
            "def test_describe():",
            "    info = describe()",
            "    assert info['name']",
            "    assert info['version']",
            "",
        ]
        files = [
            ("README.md", "\n".join(readme)),
            ("config.json", json.dumps(config, indent=2) + "\n"),
            ("%s/__init__.py" % package, "\n".join(init_body)),
            ("run_%s.py" % package, "\n".join(runner)),
            ("tests/__init__.py", ""),
            ("tests/test_%s.py" % package, "\n".join(test)),
        ]
        notes = ["project scaffold for %s" % title]
        if discovered:
            notes.append("%d file(s) discovered in the source project"
                         % len(discovered))
        return self._ok("project", title, files, notes)


# --------------------------------------------------------------------------
# Module-level helpers used by the generators.
# --------------------------------------------------------------------------
def _flatten(data, prefix=""):
    """Flatten a nested mapping into ``[(dotted path, value), ...]``."""
    flat = []
    for key, value in (data or {}).items():
        path = "%s.%s" % (prefix, key) if prefix else str(key)
        if isinstance(value, dict):
            flat.extend(_flatten(value, path))
        else:
            flat.append((path, value))
    return flat


def _insert_section_comments(lines, data):
    """Put a section banner before the constants of every top-level key."""
    out = []
    keys = list(data)
    for line in lines:
        name = line.split(" ")[0]
        for key in keys:
            if constant_name(key) == name and not any(
                    key in existing for existing in out[-1:]):
                out.append("")
                out.append("# --- %s ---" % key)
                break
        out.append(line)
    return out


def _model_order(models):
    """Sort classes so a class is emitted before the ones that use it."""
    def rank(name):
        return (0 if not models[name].get("parents") else 1, name)
    return rank


def _collect_models(data, class_key, models):
    """Walk a parsed document and build one model per object shape."""
    like = {}
    for key, value in (data or {}).items():
        if isinstance(value, dict):
            like[key] = value
        elif isinstance(value, list) and value and isinstance(value[0], dict):
            like[key] = value[0]
    fields = []
    for key, value in (data or {}).items():
        if isinstance(value, dict):
            fields.append({"name": key, "type": class_name(key, "Item"),
                           "kind": "model"})
        elif isinstance(value, list) and value and isinstance(value[0], dict):
            fields.append({"name": key, "type": class_name(key, "Item"),
                           "kind": "models"})
        elif isinstance(value, list):
            item = value[0] if value else None
            fields.append({"name": key, "type": infer_type(item),
                           "kind": "list"})
        else:
            fields.append({"name": key, "type": infer_type(value),
                           "kind": "scalar"})
    models[class_key] = {"fields": fields,
                         "parents": sorted(class_name(key, "Item")
                                           for key in like
                                           if class_name(key, "Item") != class_key)}
    for key, value in (data or {}).items():
        if isinstance(value, dict):
            _collect_models(value, class_name(key, "Item"), models)
        elif isinstance(value, list) and value and isinstance(value[0], dict):
            _collect_models(value[0], class_name(key, "Item"), models)


def _render_model(class_key, model):
    """One dataclass, with its from_dict loader.

    The loader is emitted at method depth (8 spaces for a statement, 12
    for its continuation), so the class body it is joined into stays
    valid Python.
    """
    lines = ["@dataclass", "class %s:" % class_key]
    fields = model.get("fields") or []
    if not fields:
        lines.append("    pass")
        return lines
    body = []
    for field in fields:
        name = slugify(field["name"], "field")
        kind = field["kind"]
        if kind == "model":
            annotation = "Optional[%s] = None" % field["type"]
            body.append("        value = data.get('%s')" % field["name"])
            body.append("        if isinstance(value, dict):")
            body.append("            result['%s'] = %s.from_dict(value)"
                        % (field["name"], field["type"]))
            body.append("        else:")
            body.append("            result['%s'] = None" % field["name"])
        elif kind == "models":
            annotation = "List[%s] = field(default_factory=list)" % field["type"]
            body.append("        result['%s'] = [%s.from_dict(item)"
                        % (field["name"], field["type"]))
            body.append("                       for item in data.get('%s') or []]"
                        % field["name"])
        elif kind == "list":
            annotation = "List[%s] = field(default_factory=list)" % field["type"]
            body.append("        result['%s'] = list(data.get('%s') or [])"
                        % (field["name"], field["name"]))
        else:
            default = "None"
            if field["type"] in ("int", "float"):
                default = "0"
            elif field["type"] == "bool":
                default = "False"
            elif field["type"] == "str":
                default = "''"
            elif field["type"] == "dict":
                default = "field(default_factory=dict)"
            elif field["type"] == "list":
                default = "field(default_factory=list)"
            annotation = "%s = %s" % (field["type"], default)
            body.append("        result['%s'] = data.get('%s', %s)"
                        % (field["name"], field["name"], default))
        lines.append("    %s: %s" % (name, annotation))
    lines += [
        "",
        "    @classmethod",
        "    def from_dict(cls, data):",
        '        """Build the object from a parsed mapping."""',
        "        data = data or {}",
        "        result = {}",
    ]
    lines += body
    lines.append("        return cls(**result)")
    return lines


def _read_api(text):
    """Read endpoints from OpenAPI, a JSON list or ``METHOD /path`` lines."""
    endpoints = []
    title = ""
    data, _error = parse_document(text)
    if isinstance(data, dict) and isinstance(data.get("paths"), dict):
        title = str((data.get("info") or {}).get("title") or "")
        for path, operations in data["paths"].items():
            if not isinstance(operations, dict):
                continue
            for method, detail in operations.items():
                if method.lower() not in ("get", "post", "put", "patch",
                                          "delete", "head", "options"):
                    continue
                summary = ""
                if isinstance(detail, dict):
                    summary = str(detail.get("summary")
                                  or detail.get("operationId") or "")
                endpoints.append({"method": method.lower(), "path": path,
                                  "summary": summary})
        return endpoints, title
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and item.get("path"):
                endpoints.append({"method": str(item.get("method", "get")).lower(),
                                  "path": str(item["path"]),
                                  "summary": str(item.get("summary") or "")})
            elif isinstance(item, str):
                entry = _read_api_line(item)
                if entry:
                    endpoints.append(entry)
        return endpoints, title
    for line in (text or "").replace("\r\n", "\n").split("\n"):
        entry = _read_api_line(line)
        if entry:
            endpoints.append(entry)
    return endpoints, title


def _read_api_line(line):
    """One ``METHOD /path [-- summary]`` line."""
    text = (line or "").strip().strip("-").strip()
    if not text or text.startswith("#"):
        return None
    if "#" in text:
        text = text.split("#")[0].strip()
    match = re.match(r"^(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+(\S+)(.*)$",
                     text, re.IGNORECASE)
    if not match:
        return None
    summary = match.group(3).strip().lstrip("-").strip()
    return {"method": match.group(1).lower(), "path": match.group(2),
            "summary": summary}


def _path_arguments(path):
    """The ``{name}`` placeholders of a path, in order."""
    return [part.strip() for part in re.findall(r"\{([^}]+)\}", path or "")]


def _method_name(entry, used):
    """A unique Python method name for an endpoint."""
    words = _pascal_words(entry.get("summary") or "")
    if not words:
        words = _pascal_words(entry.get("path") or "")
    name = slugify("_".join(words), "call")
    if not name or name in PYTHON_RESERVED:
        name = "call"
    candidate = name
    suffix = 2
    while candidate in used:
        candidate = "%s_%d" % (name, suffix)
        suffix += 1
    return candidate


def _read_modules(text):
    """Read a module definition into ``[{name, description, ...}, ...]``."""
    modules = []
    data, _error = parse_document(text)
    if isinstance(data, dict):
        entries = data.get("modules") or data.get("packages") or data
        if isinstance(entries, dict):
            for key, value in entries.items():
                if isinstance(value, dict):
                    modules.append(_module_entry(key, value.get("description"),
                                                 value))
                else:
                    modules.append(_module_entry(key, value, {}))
            return modules
        if isinstance(entries, list):
            for item in entries:
                if isinstance(item, dict):
                    modules.append(_module_entry(
                        item.get("name") or item.get("module") or "",
                        item.get("description"), item))
                else:
                    modules.append(_module_entry(str(item), "", {}))
            return modules
    if isinstance(data, list):
        for item in data:
            modules.append(_module_entry(str(item), "", {}))
        return modules
    for line in (text or "").replace("\r\n", "\n").split("\n"):
        entry = _module_entry(*_split_module_line(line))
        if entry:
            modules.append(entry)
    return [module for module in modules if module]


def _split_module_line(line):
    text = (line or "").strip().lstrip("-*").strip()
    if not text or text.startswith("#"):
        return "", ""
    for separator in (":", "=", " - ", "--"):
        if separator in text:
            name, _, description = text.partition(separator)
            return name.strip(), description.strip()
    return text, ""


def _module_entry(name, description, extra=None):
    name = str(name or "").strip()
    if not name:
        return None
    extra = extra or {}
    dependencies = extra.get("imports") or extra.get("dependencies") or []
    if isinstance(dependencies, str):
        dependencies = [part.strip() for part in dependencies.split(",")
                        if part.strip()]
    exports = extra.get("exports") or []
    if isinstance(exports, str):
        exports = [part.strip() for part in exports.split(",") if part.strip()]
    return {"name": name, "description": str(description or ""),
            "imports": list(dependencies), "exports": list(exports)}


def _scan_project(root):
    """What a source project already contains, as relative paths."""
    found = []
    interesting = (".py", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
                   ".md", ".sh", ".ps1", ".bat")
    for base, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs
                         if d not in {".git", "__pycache__", ".venv", "venv",
                                      "node_modules", ".idea"})
        for name in sorted(files):
            if not name.lower().endswith(interesting):
                continue
            found.append(os.path.relpath(os.path.join(base, name),
                                         root).replace(os.sep, "/"))
            if len(found) >= 200:
                return found
    return found


# --------------------------------------------------------------------------
# Convenience functions: one call per generator kind.
# --------------------------------------------------------------------------
_DEFAULT_GENERATOR = CodeGenerator()


def generate(kind, source=None, **options):
    """Generate one artifact with the default generator."""
    return _DEFAULT_GENERATOR.generate(kind, source, **options)


def generate_from_config(source=None, **options):
    return _DEFAULT_GENERATOR.generate_from_config(source, **options)


def generate_from_metadata(source=None, **options):
    return _DEFAULT_GENERATOR.generate_from_metadata(source, **options)


def generate_from_json(source=None, **options):
    return _DEFAULT_GENERATOR.generate_from_json(source, **options)


def generate_from_api(source=None, **options):
    return _DEFAULT_GENERATOR.generate_from_api(source, **options)


def generate_from_modules(source=None, **options):
    return _DEFAULT_GENERATOR.generate_from_modules(source, **options)


def generate_from_project(source=None, **options):
    return _DEFAULT_GENERATOR.generate_from_project(source, **options)


def capabilities():
    """The generator as a plugin-safe catalogue."""
    return {
        key: {"name": CODE_GENERATOR_LABELS[key],
              "reads": CODE_GENERATOR_READS[key],
              "gives": CODE_GENERATOR_GIVES[key],
              "status": "available"}
        for key in CODE_GENERATOR_KEYS
    }
