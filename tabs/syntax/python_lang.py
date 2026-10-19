# -*- coding: utf-8 -*-
"""Python syntax module."""

from .base import SyntaxModule


class PythonModule(SyntaxModule):
    key = "python"
    label = "Python"
    aliases = ("python", ".py", "py")
    extensions = (".py",)
    line_comment = "#"
    block_comments = (('"""', '"""'), ("'''", "'''"))
    indent_triggers = (":",)
    keywords = (
        "def class import from return if elif else for while try except "
        "finally with as lambda None True False self pass break continue "
        "global nonlocal yield raise assert del in is not and or async await "
        "match case"
    ).split()
    builtins = (
        "print len range str int float list dict set tuple open self super "
        "isinstance enumerate zip map filter sum min max abs sorted any all "
        "repr type id input format next iter reversed round exit property "
        "staticmethod classmethod"
    ).split()
