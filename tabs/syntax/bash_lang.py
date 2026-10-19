# -*- coding: utf-8 -*-
"""Bash / POSIX shell syntax module."""

from .base import SyntaxModule


class BashModule(SyntaxModule):
    key = "bash"
    label = "Bash / Shell"
    aliases = ("bash", "shell", "sh", "zsh", "ksh", "console")
    extensions = (".sh", ".bash")
    line_comment = "#"
    keywords = (
        "if then else elif fi for in do done case esac while until function "
        "select time coproc return exit break continue local export readonly "
        "declare typeset source alias unset shift eval exec trap set test"
    ).split()
    builtins = (
        "echo printf read cd pwd ls cp mv rm mkdir rmdir touch cat grep sed "
        "awk sort uniq head tail chmod chown find xargs curl wget sudo "
        "getopts let mapfile"
    ).split()
