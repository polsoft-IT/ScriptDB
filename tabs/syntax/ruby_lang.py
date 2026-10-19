# -*- coding: utf-8 -*-
"""Ruby syntax module."""

from .base import SyntaxModule


class RubyModule(SyntaxModule):
    key = "ruby"
    label = "Ruby"
    aliases = ("ruby", "rb", "rails")
    extensions = (".rb", ".rake")
    line_comment = "#"
    block_comments = (("=begin", "=end"),)
    keywords = (
        "def end if elsif else unless while until for in do then begin rescue "
        "ensure retry return yield class module self super nil true false and "
        "or not lambda proc next break redo case when require_relative "
        "attr_accessor attr_reader attr_writer"
    ).split()
    builtins = (
        "puts print p gets require_relative new freeze respond_to each map "
        "select reject inject reduce times upto downto String Integer Float "
        "Array Hash Symbol Struct File IO Dir Time Enumerable Kernel"
    ).split()
