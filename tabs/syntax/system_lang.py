# -*- coding: utf-8 -*-
"""System configuration syntax module.

Covers the formats the editor meets outside plain scripts: Windows .reg files,
.inf/setup answers, .env key/value files and generic .conf files.
"""

from .base import SyntaxModule


class SystemConfigModule(SyntaxModule):
    key = "system"
    label = "System configuration"
    aliases = ("registry", "reg", "system", "config", "inf", "env",
               "configuration")
    extensions = (".reg", ".inf", ".env", ".conf", ".cfg")
    line_comment = ";"
    comments = {"reg": ";", "ini": ";", "env": "#", "conf": "#"}
    # A top-level configuration keyword: HKEY, Windows Registry Editor, etc.
    keywords = (
        "Windows Registry Editor Version HKEY_CLASSES_ROOT HKEY_CURRENT_USER "
        "HKEY_LOCAL_MACHINE HKEY_USERS HKEY_CURRENT_CONFIG DWORD QWORD "
        "REG_SZ REG_DWORD REG_QWORD REG_BINARY REG_MULTI_SZ REG_EXPAND_SZ"
    ).split()
    builtins = (
        "Export Import Regedt32 Reged Reg Add Delete Query Copy Save Restore "
        "Load Unload Compare SystemRoot Path UserProfile"
    ).split()
    indent_triggers = ("[",)
