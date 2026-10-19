# -*- coding: utf-8 -*-
"""JavaScript / TypeScript syntax module."""

from .base import SyntaxModule


class JavaScriptModule(SyntaxModule):
    key = "javascript"
    label = "JavaScript"
    aliases = ("javascript", "typescript", "ecmascript", "node", "js", "ts")
    extensions = (".js", ".ts", ".jsx")
    line_comment = "//"
    block_comments = (("/*", "*/"),)
    keywords = (
        "function const let var return if else for while do switch case "
        "break continue new typeof instanceof class extends import export "
        "default async await try catch finally throw this null undefined true "
        "false delete in of yield static get set super void debugger with"
    ).split()
    builtins = (
        "console document window require module exports process global JSON "
        "Math Object Array String Number Boolean Promise Map Set Symbol "
        "setTimeout setInterval clearTimeout fetch parseInt parseFloat"
    ).split()
