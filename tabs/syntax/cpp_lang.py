# -*- coding: utf-8 -*-
"""C++ syntax module."""

from .base import SyntaxModule


class CppModule(SyntaxModule):
    key = "cpp"
    label = "C++"
    aliases = ("c++", "cpp", "cplus", "cxx", "c")
    extensions = (".cpp", ".hpp", ".cc")
    line_comment = "//"
    block_comments = (("/*", "*/"),)
    keywords = (
        "auto break case catch class constexpr continue default delete "
        "do else enum explicit export extern false for friend goto if inline "
        "mutable namespace new noexcept nullptr operator private protected "
        "public register return sizeof static struct switch template this "
        "throw true try typedef typeid typename union using virtual void "
        "volatile while int double float char bool long short signed unsigned "
        "const_cast dynamic_cast static_cast reinterpret_cast include define "
        "ifdef ifndef endif pragma"
    ).split()
    builtins = (
        "std cout cin cerr endl string vector map set unordered_map array "
        "unique_ptr shared_ptr make_unique make_shared printf malloc free "
        "size_t nullptr_t move forward pair tuple"
    ).split()
