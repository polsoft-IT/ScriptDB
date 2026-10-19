# -*- coding: utf-8 -*-
"""VBScript syntax module (classic VBS/VBA, also used by .hta payloads)."""

from .base import SyntaxModule


class VBScriptModule(SyntaxModule):
    key = "vbscript"
    label = "VBScript"
    aliases = ("vbscript", "vbs", "vb", "vba", "wsf")
    extensions = (".vbs", ".vbe", ".wsf")
    line_comment = "'"
    # VBScript comments are also written with the "Rem" keyword.
    comments = {"rem": "rem", "hta": "'"}
    keywords = (
        "dim redim if then elseif else end select case for each next to step "
        "while wend do loop until set let new class function sub property "
        "public private const option explicit call exit and or not xor on "
        "error resume goto stop byref byval empty nothing null true false"
    ).split()
    builtins = (
        "WScript CreateObject GetObject MsgBox InputBox Echo Quit Run Exec "
        "Sleep Response WriteLine Shell Application FileSystemObject "
        "CreateTextFile OpenTextFile DriveExists GetFolder MapNetworkDrive "
        "RegRead RegWrite ExpandEnvironmentStrings"
    ).split()
    indent_triggers = ()
