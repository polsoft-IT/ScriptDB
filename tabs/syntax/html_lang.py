# -*- coding: utf-8 -*-
"""HTML / HTA syntax module (markup plus embedded script blocks)."""

from .base import SyntaxModule


class HtmlModule(SyntaxModule):
    key = "html"
    label = "HTML / HTA"
    aliases = ("html", "hta", "xhtml", "xml", "markup")
    extensions = (".html", ".hta", ".htm")
    line_comment = "//"
    # A webpage mixes dialects: markup comments, VBScript comments inside
    # <script language="VBScript"> and PowerShell "#" style includes.
    comments = {"hta": "//", "vbscript": "'", "powershell": "#"}
    block_comments = (("<!--", "-->"),)
    keywords = (
        "html head body title meta link script style div span p a img ul ol "
        "li table tr td th form input button select option textarea h1 h2 h3 "
        "h4 h5 h6 br hr iframe frameset frame object param embed video audio "
        "canvas svg section header footer nav main article aside template"
    ).split()
    builtins = (
        "window document navigator location screen history console alert "
        "setTimeout getElementById querySelector addEventListener "
        "createObject innerHTML class id style href src lang charset"
    ).split()
    indent_triggers = ("{",)
