# -*- coding: utf-8 -*-
"""JSON / YAML / INI syntax module (data and configuration files)."""

from .base import SyntaxModule


class DataModule(SyntaxModule):
    key = "json"
    label = "JSON / YAML / INI"
    aliases = ("json", "yaml", "yml", "toml", "ini", "conf", "cfg", "data")
    extensions = (".json", ".yaml", ".ini")
    # JSON has no comments; YAML and INI use "#" or ";", which is what the
    # editor also shows in the JSON viewer, so "#" is the default marker.
    line_comment = "#"
    comments = {"ini": ";", "conf": ";", "cfg": ";", "json": "#"}
    keywords = (
        "true false null yes no on off undefined"
    ).split()
    builtins = ()
    indent_triggers = ("{", "[")
