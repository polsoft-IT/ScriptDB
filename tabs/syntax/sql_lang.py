# -*- coding: utf-8 -*-
"""SQL syntax module."""

from .base import SyntaxModule


class SqlModule(SyntaxModule):
    key = "sql"
    label = "SQL"
    aliases = ("sql", "sqlite", "tsql", "plsql")
    extensions = (".sql", ".sqlite")
    line_comment = "--"
    block_comments = (("/*", "*/"),)
    # SQL is conventionally written in upper case, so keywords and built-ins
    # are matched without regard to case.
    case_insensitive = True
    # SQL is conventionally written in upper case, so keywords and built-ins
    # are matched without regard to case.
    case_insensitive = True
    keywords = (
        "select insert update delete create alter drop truncate table view "
        "index trigger procedure function from where group by having order "
        "limit offset join left right inner outer full cross on using union "
        "all distinct into values set as and or not null is like between "
        "exists in case when then else end primary key foreign references "
        "unique default check constraint autoincrement cascade begin commit "
        "rollback transaction with recursive replace explain pragma vacuum"
    ).split()
    builtins = (
        "count sum avg min max abs round coalesce nullif cast convert "
        "substr trim upper lower length date datetime now row_number "
        "rank dense_rank over partition integer text real blob varchar"
    ).split()
    indent_triggers = ("(",)
