# -*- coding: utf-8 -*-
"""Per-language syntax modules for the ScriptDB editor.

One file per language, one ``SyntaxModule`` subclass per file, all of them
wired together by ``registry.py``. Adding a language means dropping a new file
in this folder and registering it -- nothing in the editor has to change::

    from tabs import syntax
    syntax.register_module(MyLanguageModule)

Modules:
    base.py             ->  SyntaxModule protocol
    highlighter.py      ->  SyntaxHighlighter (line spans, theme aware)
    registry.py         ->  SyntaxRegistry + module level helpers
    python_lang.py      ->  Python
    cpp_lang.py         ->  C++
    java_lang.py        ->  Java
    javascript_lang.py  ->  JavaScript / TypeScript
    powershell_lang.py  ->  PowerShell
    batch_lang.py       ->  Batch / CMD
    vbscript_lang.py    ->  VBScript
    data_lang.py        ->  JSON / YAML / INI
    bash_lang.py        ->  Bash / shell
    html_lang.py        ->  HTML / HTA
    ruby_lang.py        ->  Ruby
    sql_lang.py         ->  SQL
    system_lang.py      ->  System configuration (REG, INF, ENV, CONF)
"""

from .base import SyntaxModule
from .highlighter import DEFAULT_COLOURS, SPAN_KINDS, SyntaxHighlighter
from .registry import (BUILTIN_MODULES, DEFAULT_KEY, REGISTRY, SyntaxRegistry,
                       highlight_spans, key_for, languages, load_plugin,
                       module_for, module_for_extension, register_module,
                       replace_module, unregister_module)

__all__ = [
    "SyntaxModule", "SyntaxHighlighter", "SPAN_KINDS", "DEFAULT_COLOURS",
    "SyntaxRegistry", "REGISTRY", "BUILTIN_MODULES", "DEFAULT_KEY",
    "module_for", "module_for_extension", "key_for", "languages",
    "register_module", "replace_module", "unregister_module", "load_plugin",
    "highlight_spans",
]
