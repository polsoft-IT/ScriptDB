# -*- coding: utf-8 -*-
"""Hot-pluggable syntax registry for the Editor tab.

The registry is the single place that knows which languages exist. Every
language is a ``SyntaxModule`` subclass living in its own module file, and the
registry can be changed while the application runs:

    from tabs import syntax

    syntax.languages()                     # menu entries: (key, label)
    syntax.module_for("C++")               # resolve by script category
    syntax.module_for_extension(".reg")    # resolve by file extension
    syntax.replace_module(MyPython)        # override a built-in language
    syntax.register_module(MySqlModule)    # add a brand new language
    syntax.unregister_module("ruby")       # remove one
    syntax.load_plugin("my_syntax_pack")   # import + auto-register a plugin

A plugin is any importable module that defines

    def syntax_modules():
        return [MySqlModule, MyOtherModule]

as well as the ``SYNTAX_MODULES`` name. Loading a plugin never breaks the
application: a bad plugin returns an error string instead of raising.
"""

import importlib

from .base import SyntaxModule
from .bash_lang import BashModule
from .batch_lang import BatchModule
from .cpp_lang import CppModule
from .data_lang import DataModule
from .html_lang import HtmlModule
from .java_lang import JavaModule
from .javascript_lang import JavaScriptModule
from .powershell_lang import PowerShellModule
from .python_lang import PythonModule
from .ruby_lang import RubyModule
from .sql_lang import SqlModule
from .system_lang import SystemConfigModule
from .vbscript_lang import VBScriptModule

# Every module shipped with the editor, in picker order.
BUILTIN_MODULES = (
    PythonModule,
    CppModule,
    JavaModule,
    JavaScriptModule,
    PowerShellModule,
    BatchModule,
    VBScriptModule,
    DataModule,
    BashModule,
    HtmlModule,
    RubyModule,
    SqlModule,
    SystemConfigModule,
)

# Used whenever a category matches nothing else.
DEFAULT_KEY = "python"


class SyntaxRegistry(object):
    """A mutable table of syntax modules with alias and extension lookup."""

    def __init__(self, module_classes=BUILTIN_MODULES):
        self._modules = {}
        self._aliases = {}
        self._extensions = {}
        for module_class in module_classes:
            self.register_module(module_class)

    # ------------------------------------------------------------ lookup
    def keys(self):
        return list(self._modules.keys())

    def modules(self):
        """Every registered module, in registration order."""
        return list(self._modules.values())

    def languages(self):
        """(key, label) pairs for a language picker."""
        return [(module.key, module.label) for module in self._modules.values()]

    def labels(self):
        return [module.label for module in self._modules.values()]

    def get(self, key, default=None):
        """The module registered under an exact key."""
        module = self._modules.get((key or "").lower())
        if module is not None:
            return module
        return default

    def module_for(self, category):
        """Resolve a script category, alias or label to a module (never None)."""
        lowered = (category or "").lower().strip()
        if not lowered:
            return self._modules[DEFAULT_KEY]
        module = self._modules.get(lowered)
        if module is not None:
            return module
        for alias in sorted(self._aliases, key=len, reverse=True):
            if alias and alias in lowered:
                return self._aliases[alias]
        # Last resort: match on the visible label of a module.
        for other in self._modules.values():
            if other.label.lower() in lowered:
                return other
        return self._modules[DEFAULT_KEY]

    def module_for_extension(self, filename):
        """Resolve a file name or extension to a module (never None)."""
        lowered = (filename or "").lower()
        dot = lowered.rfind(".")
        extension = lowered[dot:] if dot >= 0 else ""
        module = self._extensions.get(extension)
        return module if module is not None else self._modules[DEFAULT_KEY]

    def key_for(self, category):
        return self.module_for(category).key

    # ----------------------------------------------------------- mutation
    def register_module(self, module_class):
        """Add (or replace) a language. Returns True when it was stored."""
        module = _instantiate(module_class)
        if module is None:
            return False
        self.unregister_module(module.key)
        self._modules[module.key] = module
        # Aliases are matched as sub-strings of the lower-cased category.
        self._aliases[module.key] = module
        self._aliases[module.label.lower()] = module
        for alias in module.aliases:
            self._aliases[(alias or "").lower()] = module
        for extension in module.extensions:
            self._extensions[(extension or "").lower()] = module
        # Drop aliases and extensions that now point at an older module whose
        # key changed, so a replacement always wins.
        self._prune()
        return True

    def replace_module(self, module_class):
        """Alias for ``register_module`` (an override replaces in place)."""
        return self.register_module(module_class)

    def unregister_module(self, key):
        """Remove a language by key. Returns True when something was removed."""
        key = (key or "").lower()
        module = self._modules.pop(key, None)
        if module is None:
            return False
        for alias, owner in list(self._aliases.items()):
            if owner is module:
                del self._aliases[alias]
        for extension, owner in list(self._extensions.items()):
            if owner is module:
                del self._extensions[extension]
        if DEFAULT_KEY not in self._modules and self._modules:
            # Keep the fallback pointing at something that exists.
            globals()["DEFAULT_KEY"] = next(iter(self._modules))
        return True

    def load_plugin(self, name):
        """Import a plugin module and register the languages it provides.

        Returns the list of registered keys, or a one element list holding the
        error message -- loading a plugin never raises.
        """
        try:
            plugin = importlib.import_module(name)
        except Exception as exc:
            return ["error: %s" % exc]
        found = list(getattr(plugin, "SYNTAX_MODULES", ()) or ())
        factory = getattr(plugin, "syntax_modules", None)
        if callable(factory):
            try:
                found += list(factory() or [])
            except Exception as exc:
                return ["error: %s" % exc]
        registered = []
        for module_class in found:
            if self.register_module(module_class):
                registered.append(_key_of(module_class))
        return registered

    def reset(self):
        """Restore exactly the built-in languages (used by the tests)."""
        self._modules = {}
        self._aliases = {}
        self._extensions = {}
        globals()["DEFAULT_KEY"] = "python"
        for module_class in BUILTIN_MODULES:
            self.register_module(module_class)

    def capabilities(self):
        """Describe this registry for the audit/report layer."""
        return {
            "languages": len(self._modules),
            "hot_pluggable": True,
            "zero_dependency": True,
            "sandbox_safe": True,
            "aliases": len(self._aliases),
            "extensions": len(self._extensions),
        }

    # ---------------------------------------------------------- internals
    def _prune(self):
        """Drop aliases and extensions owned by a module no longer bound."""
        live = set(id(module) for module in self._modules.values())
        for alias, owner in list(self._aliases.items()):
            if id(owner) not in live:
                del self._aliases[alias]
        for extension, owner in list(self._extensions.items()):
            if id(owner) not in live:
                del self._extensions[extension]


def _instantiate(module_class):
    """Accept a class or an already built module instance."""
    if isinstance(module_class, SyntaxModule):
        return module_class
    try:
        module = module_class()
    except Exception:
        return None
    return module


def _key_of(module_class):
    module = _instantiate(module_class)
    return module.key if module is not None else ""


# The process-wide registry every editor shares by default.
REGISTRY = SyntaxRegistry()


# ---------------------------------------------------------------- helpers
def module_for(category):
    """The syntax module for a script category (never None)."""
    return REGISTRY.module_for(category)


def module_for_extension(filename):
    """The syntax module for a file name or extension (never None)."""
    return REGISTRY.module_for_extension(filename)


def key_for(category):
    """The language key for a script category."""
    return REGISTRY.key_for(category)


def languages():
    """(key, label) pairs for the language picker."""
    return REGISTRY.languages()


def register_module(module_class):
    return REGISTRY.register_module(module_class)


def replace_module(module_class):
    return REGISTRY.replace_module(module_class)


def unregister_module(key):
    return REGISTRY.unregister_module(key)


def load_plugin(name):
    return REGISTRY.load_plugin(name)


def highlight_spans(text, category, theme, low_graphics=False):
    """Highlight one line in the language of ``category``.

    Kept as a module level function so the highlighter stays usable without an
    editor instance (previews, exports, tests).
    """
    module = module_for(category)
    return module.highlighter(theme, low_graphics).spans(text, category)
