# -*- coding: utf-8 -*-
"""Java syntax module."""

from .base import SyntaxModule


class JavaModule(SyntaxModule):
    key = "java"
    label = "Java"
    aliases = ("java", "kotlin", "groovy")
    extensions = (".java", ".kt")
    line_comment = "//"
    block_comments = (("/*", "*/"),)
    keywords = (
        "abstract assert boolean break byte case catch char class const "
        "continue default do double else enum extends final finally float "
        "for goto if implements import instanceof int interface long native "
        "new package private protected public return short static strictfp "
        "super switch synchronized this throw throws transient try void "
        "volatile while true false null var record sealed"
    ).split()
    builtins = (
        "System out println print String Integer Long Double Boolean Object "
        "List ArrayList Map HashMap Set HashSet Optional Stream Math "
        "Objects StringBuilder Thread Runnable Exception RuntimeException"
    ).split()
