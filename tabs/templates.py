# -*- coding: utf-8 -*-
"""The editor's template engine: one-click, ready-to-fill code blocks.

Automation without a model and without a network: a catalogue of code shapes
that a developer reaches for again and again, each rendered into the buffer at
the cursor in one click. It is the ninth surface of the editor and, like the
others, it is data first -- the catalogue is a table, so a menu, a test and a
plugin all read the same entries.

    TEMPLATES      the catalogue: (category, key, label key, fallback, level)
    CATEGORIES     the nine kinds of template, in menu order
    render         one template as text (with the placeholders filled in)
    apply          render, then indent to where the cursor actually sits
    templates_for  the templates that suit a language
    capabilities   the catalogue, for the audit / about layer

Every template is written with three placeholder forms:

    ${name}        a required slot (a function name, a class name, a title)
    $name          the same slot, short form (used inside a longer word)
    $select{a|b}   a choice; the first option is the default

`render` fills them from the values it is given and leaves a clearly marked
blank (`___`) for any slot the caller did not fill, so a template never emits a
half-word and never silently invents a name. `apply` then shifts the whole
block so it lines up with the indentation of the line the cursor is on -- which
is what makes "one click -> a ready block" true inside a class body and not
only at column zero.

Two decisions worth stating:

    * **A template is text, never a callable.** Anything that could run code is
      a script; this is a string with holes in it, so it is safe to hot-plug one
      at run time and safe to render in a test.
    * **The catalogue is language-tagged, not language-switched.** A template
      says which languages it suits; asking for the templates of `Python`
      returns the generic ones plus Python's own, so the list a user sees never
      starts empty in a language nobody wrote a template for yet.
"""

import re

# The placeholder forms, compiled once.
_SLOT = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")
_SELECT = re.compile(r"\$select\{([^}]*)\}")
# What an unfilled slot renders as: a run of underscores that is obvious in the
# buffer and is a valid identifier fragment in every language the editor knows.
BLANK = "___"


# ---------------------------------------------------------------------------
# The categories
# ---------------------------------------------------------------------------
# (key, label key, fallback, what the category is for)
CATEGORIES = (
    ("function", "template_cat_function", "Function templates",
     "a function or method skeleton, with its signature and body"),
    ("class", "template_cat_class", "Class templates",
     "a class skeleton, with its constructor and a method"),
    ("module", "template_cat_module", "Module templates",
     "a whole module: header, imports, definitions, entry point"),
    ("project", "template_cat_project", "Project templates",
     "the files and the layout a new project starts from"),
    ("config", "template_cat_config", "Configuration templates",
     "a config file skeleton (INI, JSON, YAML) with the usual keys"),
    ("test", "template_cat_test", "Test templates",
     "a test case, a test class and a parametrised test"),
    ("documentation", "template_cat_documentation", "Documentation templates",
     "a docstring, a README section and a change-log entry"),
    ("starter", "template_cat_starter", "Startup script templates",
     "a runnable entry point with argument parsing and a main guard"),
    ("block", "template_cat_block", "Repeatable blocks",
     "the control-flow blocks: if, try, loop, match, context manager"),
)

CATEGORY_KEYS = tuple(key for key, _lk, _f, _d in CATEGORIES)
CATEGORY_LABELS = {key: fallback for key, _lk, fallback, _d in CATEGORIES}
CATEGORY_MEANINGS = {key: meaning for key, _lk, _f, meaning in CATEGORIES}

# The languages a template with no language tag suits: everything, i.e. a
# template tagged `("*",)` is offered whatever the editor is showing.
ANY_LANGUAGE = ("*",)


# ---------------------------------------------------------------------------
# The catalogue
# ---------------------------------------------------------------------------
# (category, key, label key, fallback, languages, body)
#
# `languages` is a tuple of syntax-module keys (for example "python", "bash")
# or ("*",) for a template that is not language specific. The bodies are plain
# text; the placeholders are documented in the module docstring.
TEMPLATES = (
    # ---- function --------------------------------------------------------
    ("function", "func_python", "template_func_python", "Python function",
     ("python",),
     "def ${name}(self):\n"
     "    \"\"\"One line saying what it does.\"\"\"\n"
     "    ${body}\n"
     "    return ${value}\n"),
    ("function", "func_async", "template_func_async", "Async function",
     ("python",),
     "async def ${name}():\n"
     "    \"\"\"One line saying what it does.\"\"\"\n"
     "    ${body}\n"),
    ("function", "func_generic", "template_func_generic", "Function (any language)",
     ANY_LANGUAGE,
     "function $name($args) {\n"
     "    // ${body}\n"
     "    return $value;\n"
     "}\n"),
    ("function", "func_js", "template_func_js", "JavaScript function",
     ("javascript",),
     "function ${name}(${args}) {\n"
     "  // your code here\n"
     "  return ${value};\n"
     "}\n"),
    ("function", "func_go", "template_func_go", "Go function",
     ("go",),
     "func ${name}(${args}) ${return_type} {\n"
     "    // your code here\n"
     "    return ${value}\n"
     "}\n"),
    ("function", "func_rust", "template_func_rust", "Rust function",
     ("rust",),
     "fn ${name}(${args}) -> ${return_type} {\n"
     "    // your code here\n"
     "    ${value}\n"
     "}\n"),
    # ---- class -----------------------------------------------------------
    ("class", "class_python", "template_class_python", "Python class",
     ("python",),
     "class ${name}:\n"
     "    \"\"\"One line saying what it is.\"\"\"\n"
     "\n"
     "    def __init__(self${args}):\n"
     "        ${body}\n"),
    ("class", "class_entity", "template_class_entity", "Class with a method",
     ("python",),
     "class ${name}:\n"
     "    \"\"\"One line saying what it is.\"\"\"\n"
     "\n"
     "    def __init__(self):\n"
     "        self.${field} = None\n"
     "\n"
     "    def ${method}(self):\n"
     "        return self.${field}\n"),
    # ---- module ----------------------------------------------------------
    ("module", "module_python", "template_module_python", "Python module",
     ("python",),
     "#!/usr/bin/env python3\n"
     "# -*- coding: utf-8 -*-\n"
     "\"\"\"${title}.\n"
     "\n"
     "What this module is for, in one or two lines.\n"
     "\"\"\"\n"
     "\n"
     "import ${dependency}\n"
     "\n"
     "\n"
     "def main():\n"
     "    ${body}\n"
     "\n"
     "\n"
     "if __name__ == \"__main__\":\n"
     "    main()\n"),
    ("module", "module_html", "template_module_html", "HTML5 document",
     ("html",),
     "<!DOCTYPE html>\n"
     "<html lang=\"en\">\n"
     "<head>\n"
     "  <meta charset=\"UTF-8\">\n"
     "  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">\n"
     "  <title>${title}</title>\n"
     "  <link rel=\"stylesheet\" href=\"style.css\">\n"
     "</head>\n"
     "<body>\n"
     "  <h1>${heading}</h1>\n"
     "  <script src=\"script.js\"></script>\n"
     "</body>\n"
     "</html>\n"),
    ("module", "module_python_cli", "template_module_cli", "Python CLI module",
     ("python",),
     "#!/usr/bin/env python3\n"
     "# -*- coding: utf-8 -*-\n"
     "\"\"\"\"${title}: a command-line entry point.\"\"\"\n"
     "\n"
     "import argparse\n"
     "\n"
     "\n"
     "def parse_args(argv=None):\n"
     "    parser = argparse.ArgumentParser(description='${title}')\n"
     "    parser.add_argument('${argument}', help='${help}')\n"
     "    return parser.parse_args(argv)\n"
     "\n"
     "\n"
     "def main(argv=None):\n"
     "    args = parse_args(argv)\n"
     "    ${body}\n"
     "\n"
     "\n"
     "if __name__ == \"__main__\":\n"
     "    main()\n"),
    # ---- project ---------------------------------------------------------
    ("project", "project_python", "template_project_python", "Python project tree",
     ANY_LANGUAGE,
     "${name}/\n"
     "    ${name}/\n"
     "        __init__.py\n"
     "        __main__.py\n"
     "    tests/\n"
     "        test_${name}.py\n"
     "    README.md\n"
     "    pyproject.toml\n"
     "    .gitignore\n"),
    ("project", "project_plain", "template_project_plain", "Plain project tree",
     ANY_LANGUAGE,
     "${name}/\n"
     "    src/\n"
     "    tests/\n"
     "    docs/\n"
     "    README.md\n"
     "    LICENSE\n"),
    # ---- config ----------------------------------------------------------
    ("config", "config_ini", "template_config_ini", "INI config",
     ANY_LANGUAGE,
     "[${section}]\n"
     "${key} = ${value}\n"
     "; a comment\n"),
    ("config", "config_json", "template_config_json", "JSON config",
     ("json",),
     "{\n"
     "  \"${app}\": {\n"
     "    \"version\": \"1.0.0\",\n"
     "    \"debug\": false,\n"
     "    \"paths\": [\n"
     "      \"${path}\"\n"
     "    ]\n"
     "  }\n"
     "}\n"),
    ("config", "config_yaml", "template_config_yaml", "YAML config",
     ("data", "system"),
     "${app}:\n"
     "  version: 1.0.0\n"
     "  debug: false\n"
     "  paths:\n"
     "    - ${path}\n"),
    # ---- test ------------------------------------------------------------
    ("test", "test_python_func", "template_test_func", "Python test function",
     ("python",),
     "def test_${name}():\n"
     "    \"\"\"${behaviour}.\"\"\"\n"
     "    ${given}\n"
     "    assert ${result} == ${expected}\n"),
    ("test", "test_python_class", "template_test_class", "Python test class",
     ("python",),
     "class Test${Name}:\n"
     "    \"\"\"Tests for ${name}.\"\"\"\n"
     "\n"
     "    def test_${method}(self):\n"
     "        ${body}\n"
     "\n"
     "    def test_${method2}_raises(self):\n"
     "        with pytest.raises(${Error}):\n"
     "            ${body}\n"),
    ("test", "test_param", "template_test_param", "Parametrised test",
     ("python",),
     "import pytest\n"
     "\n"
     "\n"
     "@pytest.mark.parametrize(\"${arg}, expected\", [\n"
     "    (${case1}, ${expected1}),\n"
     "    (${case2}, ${expected2}),\n"
     "])\n"
     "def test_${name}(${arg}, expected):\n"
     "    assert ${call} == expected\n"),
    # ---- documentation ---------------------------------------------------
    ("documentation", "doc_python", "template_doc_python", "Python docstring",
     ("python",),
     "\"\"\"${summary}.\n"
     "\n"
     "Args:\n"
     "    ${arg}: what it is\n"
     "\n"
     "Returns:\n"
     "    ${value}: what comes back\n"
     "\"\"\"\n"),
    ("documentation", "doc_readme", "template_doc_readme", "README section",
     ANY_LANGUAGE,
     "## ${title}\n"
     "\n"
     "${summary}\n"
     "\n"
     "### Usage\n"
     "\n"
     "```\n"
     "${example}\n"
     "```\n"),
    ("documentation", "doc_changelog", "template_doc_changelog",
     "Change-log entry",
     ANY_LANGUAGE,
     "## [${version}] - ${date}\n"
     "\n"
     "### Added\n"
     "- ${added}\n"
     "\n"
     "### Changed\n"
     "- ${changed}\n"
     "\n"
     "### Fixed\n"
     "- ${fixed}\n"),
    # ---- starter ---------------------------------------------------------
    ("starter", "starter_python", "template_starter_python",
     "Python startup script",
     ("python",),
     "#!/usr/bin/env python3\n"
     "# -*- coding: utf-8 -*-\n"
     "\"\"\"${title}.\"\"\"\n"
     "\n"
     "import sys\n"
     "\n"
     "\n"
     "def main(argv):\n"
     "    ${body}\n"
     "    return 0\n"
     "\n"
     "\n"
     "if __name__ == \"__main__\":\n"
     "    sys.exit(main(sys.argv[1:]))\n"),
    ("starter", "starter_bash", "template_starter_bash", "Bash startup script",
     ("bash",),
     "#!/usr/bin/env bash\n"
     "set -euo pipefail\n"
     "\n"
     "main() {\n"
     "    ${body}\n"
     "}\n"
     "\n"
     "main \"$@\"\n"),
    ("starter", "starter_batch", "template_starter_batch",
     "Batch startup script",
     ("batch",),
     "@echo off\n"
     "setlocal\n"
     "\n"
     "echo ${title}\n"
     "${body}\n"
     "\n"
     "endlocal\n"),
    ("starter", "starter_powershell", "template_starter_powershell",
     "PowerShell startup script",
     ("powershell",),
     "#!/usr/bin/env pwsh\n"
     "Set-StrictMode -Version Latest\n"
     "$ErrorActionPreference = 'Stop'\n"
     "\n"
     "function Invoke-Main {\n"
     "    ${body}\n"
     "}\n"
     "\n"
     "Invoke-Main\n"),
    # ---- reviewable blocks -----------------------------------------------
    ("block", "block_if", "template_block_if", "If / else block",
     ("python",),
     "if ${condition}:\n"
     "    ${body}\n"
     "else:\n"
     "    ${else_body}\n"),
    ("block", "block_try", "template_block_try", "Try / except block",
     ("python",),
     "try:\n"
     "    ${body}\n"
     "except ${Error} as exc:\n"
     "    ${handler}\n"),
    ("block", "block_for", "template_block_for", "For loop",
     ("python",),
     "for ${item} in ${items}:\n"
     "    ${body}\n"),
    ("block", "block_while", "template_block_while", "While loop",
     ("python",),
     "while ${condition}:\n"
     "    ${body}\n"),
    ("block", "block_with", "template_block_with", "Context manager block",
     ("python",),
     "with ${resource} as ${handle}:\n"
     "    ${body}\n"),
    ("block", "block_match", "template_block_match", "Match / case block",
     ("python",),
     "match ${value}:\n"
     "    case ${pattern1}:\n"
     "        ${body1}\n"
     "    case _:\n"
     "        ${body2}\n"),
    ("block", "block_generic", "template_block_generic",
     "If / loop block (any language)",
     ANY_LANGUAGE,
     "if ($condition) {\n"
     "    ${body}\n"
     "} else {\n"
     "    ${else_body}\n"
     "}\n"),
)

TEMPLATE_KEYS = tuple(key for _c, key, _lk, _f, _lang, _b in TEMPLATES)
TEMPLATE_LABELS = {key: fallback
                   for _c, key, _lk, fallback, _lang, _b in TEMPLATES}
TEMPLATE_CATEGORY = {key: category
                     for category, key, _lk, _f, _lang, _b in TEMPLATES}
TEMPLATE_LANGUAGES = {key: languages
                      for _c, key, _lk, _f, languages, _b in TEMPLATES}
TEMPLATE_BODIES = {key: body
                   for _c, key, _lk, _f, _lang, body in TEMPLATES}

# Templates added at run time live apart from the shipped ones, so a hot-plugged
# template can never shadow a built-in by accident and can always be removed
# again without the catalogue having to be rebuilt.
_EXTRA_TEMPLATES = {}


def register(category, key, label, body, languages=ANY_LANGUAGE):
    """Add one template at run time (hot-plugging).

    The template becomes available to `render`, `templates_in` and the
    editor's menu immediately. Returns ``{ok, key, detail}``; a key that
    already exists (built in or added) is refused rather than silently
    overwritten, because a template that quietly changes meaning under a
    saved menu entry is a trap.
    """
    global TEMPLATE_KEYS
    key = str(key or "").strip()
    if not key or not str(body or "").strip():
        return {"ok": False, "key": key, "detail": "a key and a body are needed"}
    if category not in CATEGORY_KEYS:
        return {"ok": False, "key": key,
                "detail": "unknown category %r" % category}
    if key in TEMPLATE_BODIES or key in _EXTRA_TEMPLATES:
        return {"ok": False, "key": key, "detail": "the key is already taken"}
    languages = tuple(languages) if not isinstance(languages, str) \
        else (languages,)
    _EXTRA_TEMPLATES[key] = (category, key, "", label or key, languages, body)
    TEMPLATE_KEYS = TEMPLATE_KEYS + (key,)
    TEMPLATE_LABELS[key] = label or key
    TEMPLATE_CATEGORY[key] = category
    TEMPLATE_LANGUAGES[key] = languages
    TEMPLATE_BODIES[key] = body
    return {"ok": True, "key": key, "detail": "template registered"}


def unregister(key):
    """Remove a template that was added at run time (built-ins stay)."""
    global TEMPLATE_KEYS
    if key not in _EXTRA_TEMPLATES:
        return {"ok": False, "key": key,
                "detail": "not a registered template"}
    _EXTRA_TEMPLATES.pop(key, None)
    TEMPLATE_KEYS = tuple(k for k in TEMPLATE_KEYS if k != key)
    TEMPLATE_LABELS.pop(key, None)
    TEMPLATE_CATEGORY.pop(key, None)
    TEMPLATE_LANGUAGES.pop(key, None)
    TEMPLATE_BODIES.pop(key, None)
    return {"ok": True, "key": key, "detail": "template removed"}

# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def _fill_select(text, values):
    """Replace every ``$select{a|b|c}`` with the chosen option (first by
    default, or the one `values` names for the placeholder inside the braces)."""
    def choose(match):
        options = [option.strip() for option in match.group(1).split("|")
                   if option.strip()]
        if not options:
            return ""
        key = options[0]
        chosen = values.get(key)
        return str(chosen) if chosen is not None else options[0]
    return _SELECT.sub(choose, text)


def _fill_slots(text, values):
    """Replace every ``${name}`` / ``$name`` with its value or a blank."""
    def fill(match):
        name = match.group(1)
        if name in values and values[name] is not None:
            return str(values[name])
        return BLANK
    return _SLOT.sub(fill, text)


def placeholders(body):
    """The slot names a template body declares, in the order they appear."""
    seen = []
    for match in _SLOT.finditer(_SELECT.sub("", body)):
        name = match.group(1)
        if name not in seen:
            seen.append(name)
    return seen


def render(key, **values):
    """One template as text, with the placeholders filled from `values`.

    An unfilled slot becomes :data:`BLANK` (``___``) rather than an empty
    string, so a rendered template is always syntactically shaped and a reader
    can see exactly which holes remain. An unknown key returns "" -- never an
    exception -- so a stale menu entry cannot break the editor.
    """
    body = TEMPLATE_BODIES.get(key)
    if body is None:
        return ""
    filled = _fill_select(body, values)
    return _fill_slots(filled, values)


def render_with_blanks(key):
    """Render a template with every slot blank (what the menu inserts)."""
    return render(key)


def indent_of(line):
    """The leading whitespace of a line (spaces/tabs), as a string."""
    match = re.match(r"[ \t]*", line or "")
    return match.group(0)


def apply(key, cursor_line="", values=None, indent_unit="    "):
    """A template rendered and indented to sit where the cursor is.

    `cursor_line` is the text of the line the cursor is on; its leading
    whitespace is prefixed to every line of the rendered block, so inserting a
    class into a class body lines up without the user fixing it by hand. The
    first line is *not* indented when the cursor is at column zero, which is
    what keeps a template at the top level flush left.
    """
    text = render(key, **(values or {}))
    if not text:
        return ""
    prefix = indent_of(cursor_line)
    if not prefix:
        return text
    lines = text.split("\n")
    return "\n".join([lines[0]] + [prefix + line for line in lines[1:]])


# ---------------------------------------------------------------------------
# Looking templates up
# ---------------------------------------------------------------------------
def templates_in(category, language=None):
    """Every template of one category, optionally for one language.

    A language filter keeps the language-agnostic templates too, so the list is
    never empty in a language nobody wrote a template for yet. Returns the
    template keys in catalogue order.
    """
    language = (language or "").strip().lower() or None
    out = []
    for entry_category, key, _lk, _f, languages, _b in TEMPLATES:
        if entry_category != category:
            continue
        if language is None or "*" in languages or language in languages:
            out.append(key)
    # Run-time templates are offered too, after the shipped ones, so a plugin's
    # template appears in the menu without the menu knowing how it got there.
    for key, entry in _EXTRA_TEMPLATES.items():
        entry_category, _key, _lk, _label, languages, _body = entry
        if entry_category != category:
            continue
        if language is None or "*" in languages or language in languages:
            out.append(key)
    return out


def templates_for(language):
    """The templates that suit one language, grouped by category.

    Returns ``[(category, [(key, label), ...]), ...]`` in menu order -- the
    shape the editor builds its menu from, so the menu cannot offer a template
    the engine does not have.
    """
    language = (language or "").strip().lower() or None
    grouped = []
    for category, _lk, _f, _d in CATEGORIES:
        keys = templates_in(category, language)
        if keys:
            grouped.append((category, [(key, TEMPLATE_LABELS[key])
                                       for key in keys]))
    return grouped


def categories_with(language=None):
    """The categories that have at least one template for a language."""
    return [category for category, _items in templates_for(language)]


# ---------------------------------------------------------------------------
# Describing the surface
# ---------------------------------------------------------------------------
def format_templates(language=None):
    """The catalogue as ASCII lines, optionally for one language."""
    lines = ["Template engine", "==============="]
    if language:
        lines.append("language: %s" % language)
        lines.append("")
    for category, _lk, fallback, meaning in CATEGORIES:
        keys = templates_in(category, language)
        lines.append("%s (%d)" % (fallback, len(keys)))
        lines.append("  %s" % meaning)
        for key in keys:
            lines.append("    %-24s %s" % (key, TEMPLATE_LABELS[key]))
    return lines


def capabilities(language=None):
    """Describe the template engine for the audit / about layer."""
    return {
        "categories": list(CATEGORY_KEYS),
        "category_labels": dict(CATEGORY_LABELS),
        "category_meanings": dict(CATEGORY_MEANINGS),
        "templates": list(TEMPLATE_KEYS),
        "template_labels": dict(TEMPLATE_LABELS),
        "template_category": dict(TEMPLATE_CATEGORY),
        "template_languages": {key: list(value) for key, value
                               in TEMPLATE_LANGUAGES.items()},
        "available": {category: list(templates_in(category, language))
                      for category in CATEGORY_KEYS},
        "count": len(TEMPLATE_KEYS),
        "offline": True,
        "ascii_only": True,
        "zero_dependency": True,
        "cross_platform": True,
    }