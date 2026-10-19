# -*- coding: utf-8 -*-
"""Batch / CMD syntax module."""

from .base import SyntaxModule


class BatchModule(SyntaxModule):
    key = "batch"
    label = "Batch / CMD"
    aliases = ("batch", "cmd", "dos", "bat")
    extensions = (".bat", ".cmd")
    # "rem" is the canonical Batch comment; a stray "#" is rarely a comment, but
    # Windows' PATH-style files use it, so both are accepted.
    line_comment = "rem"
    comments = {"cmd": "rem", "powershell": "rem", "registry": "rem"}
    keywords = (
        "echo setlocal endlocal set goto call if else for do pause exit start "
        "shift for in not exist defined errorlevel off on title color cd chdir "
        "md mkdir rd rmdir del erase copy move ren rename type findstr "
        "attrib pushd popd choice timeout taskkill net reg wmic"
    ).split()
    builtins = (
        "echo off on rem goto call exit prompt label path cd dir copy del "
        "ren type find cls ver vol date time"
    ).split()
    indent_triggers = ("(",)
