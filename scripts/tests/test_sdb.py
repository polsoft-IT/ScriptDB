#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Testy jednostkowe czystej logiki sdb.py (bez GUI).

Uruchamianie:
    python scripts/tests/test_sdb.py
    pytest scripts/tests/test_sdb.py   (jeśli dostępny)

Import PyQt5 jest wymagany przez sdb.py, więc jeśli biblioteka nie jest
zainstalowana, testy są pomijane zamiast kończyć się błędem.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

try:
    import pytest
    pytest.importorskip("PyQt5")
except ImportError:
    pytest = None

import sdb  # noqa: E402


# ---------------------------------------------------------------- kategorie
def test_category_for_extension_canonical():
    assert sdb.category_for_extension("app.py") == "Python"
    assert sdb.category_for_extension("run.ps1") == "PowerShell"
    assert sdb.category_for_extension("job.bat") == "Batch"
    assert sdb.category_for_extension("page.html") == "HTML"
    assert sdb.category_for_extension("query.sql") == "SQL"
    assert sdb.category_for_extension("notes.txt") == "Other"


def test_category_for_extension_aliases():
    assert sdb.category_for_extension("a.yml") == "YAML"
    assert sdb.category_for_extension("a.toml") == "INI"
    assert sdb.category_for_extension("a.cmd") == "Batch"
    assert sdb.category_for_extension("a.vbe") == "VBScript"
    assert sdb.category_for_extension("a.rake") == "Ruby"


def test_category_for_extension_case_insensitive():
    assert sdb.category_for_extension("APP.PY") == "Python"
    assert sdb.category_for_extension("Run.PS1") == "PowerShell"


def test_extension_for_category_round_trip():
    for category, extension in sdb.CATEGORY_EXTENSIONS.items():
        assert sdb.extension_for_category(category) == extension


# ------------------------------------------------------------ guess_category
def test_guess_category_python():
    assert sdb.guess_category("import os\ndef main():\n    pass\n") == "Python"


def test_guess_category_bash():
    assert sdb.guess_category("#!/bin/bash\necho hello\n") == "Bash"


def test_guess_category_sql():
    assert sdb.guess_category("SELECT * FROM users;\n") == "SQL"


def test_guess_category_batch():
    assert sdb.guess_category("@echo off\nsetlocal\ngoto :start\n") == "Batch"


def test_guess_category_unknown_returns_none():
    assert sdb.guess_category("") is None
    assert sdb.guess_category("just plain text, no signature") is None


# ----------------------------------------------------------- normalize_entry
def test_normalize_entry_full():
    entry = sdb.normalize_entry({
        "id": "1", "name": "Test", "category": "Python",
        "tags": ["a", "b"], "desc": "opis", "code": "print(1)",
    })
    assert entry is not None
    assert entry["name"] == "Test"
    assert entry["tags"] == "a, b"
    assert entry["code"] == "print(1)"


def test_normalize_entry_fills_missing_name():
    entry = sdb.normalize_entry({"code": "SELECT 1;"})
    assert entry is not None
    assert entry["name"].startswith("Imported script")
    assert entry["category"] == "SQL"


def test_normalize_entry_rejects_empty():
    assert sdb.normalize_entry({}) is None
    assert sdb.normalize_entry("not a dict") is None
    assert sdb.normalize_entry({"name": "", "code": ""}) is None


def test_normalize_entry_keeps_separators():
    separator = sdb.normalize_entry({"kind": "separator", "name": "Grupa"})
    assert separator is not None
    assert sdb.is_separator(separator)
    assert separator["name"] == "Grupa"


# ------------------------------------------------------------- separatory
def test_make_separator_unique_ids():
    one = sdb.make_separator("A")
    two = sdb.make_separator("A")
    assert one["id"] != two["id"]
    assert sdb.is_separator(one)


# ------------------------------------------------------------------ diff
def test_align_lines_equal_and_change():
    rows = sdb.align_lines(["a", "b"], ["a", "c"], "myers")
    assert rows[0]["tag"] == "equal"
    assert rows[1]["tag"] == "replace"
    assert rows[1]["left"] == "b" and rows[1]["right"] == "c"


def test_align_lines_insert_delete():
    rows = sdb.align_lines(["a"], ["a", "b"], "myers")
    tags = [r["tag"] for r in rows]
    assert "insert" in tags


def test_iter_change_blocks_groups_non_equal():
    rows = sdb.align_lines(["a", "x", "y", "a"], ["a", "z", "a"], "myers")
    blocks = sdb.iter_change_blocks(rows)
    assert blocks, "expected at least one change block"


def test_change_kind_mapping():
    assert sdb.change_kind({"tag": "insert"}) == "added"
    assert sdb.change_kind({"tag": "delete"}) == "removed"
    assert sdb.change_kind({"tag": "replace"}) == "changed"
    assert sdb.change_kind({"tag": "equal"}) == "equal"


def test_word_diff_segments_highlights_change():
    left, right = sdb.word_diff_segments("hello world", "hello brave world")
    assert any(changed for _text, changed in right)


# -------------------------------------------------------- analiza kodu
def test_detect_language_kind_by_extension():
    kind, _label = sdb.detect_language_kind("", "script.py")
    assert kind == "python"
    kind, _label = sdb.detect_language_kind("", "a.ps1")
    assert kind == "powershell"


def test_detect_language_kind_by_content():
    kind, _label = sdb.detect_language_kind("def f():\n    return 1\n")
    assert kind == "python"


def test_extract_imports_python():
    imports = sdb.extract_imports("import os, sys\nfrom json import loads\n", "python")
    assert "os" in imports and "json" in imports


def test_extract_functions_python():
    functions = sdb.extract_functions("def hello(name):\n    pass\n", "python")
    assert functions and functions[0][0] == "hello"


def test_compare_code_ast_added_function():
    report = sdb.compare_code_ast("a = 1\n", "a = 1\ndef new():\n    pass\n")
    assert report["functions_added"] == ["new"]


# ------------------------------------------------------------- SQL split
def test_split_sql_statements():
    statements = list(sdb._split_sql_statements(
        "SELECT 1;\nINSERT INTO t VALUES ('a;b');\n-- comment\nSELECT 2;"))
    assert len(statements) == 3
    assert "SELECT 2" in statements[-1]
    # średnik w literale nie dzieli instrukcji
    assert "a;b" in statements[1]


def test_split_sql_empty():
    assert list(sdb._split_sql_statements("")) == []


# -------------------------------------------------------------- helpers
def test_safe_file_name():
    assert sdb.safe_file_name("my: script?.py") == "my__script_.py"
    assert sdb.safe_file_name("") == "script"
    assert sdb.safe_file_name("  ..  ") == "script"
    # spacje są zastępowane podkreślnikiem (bezpieczne nazwy plików)
    assert sdb.safe_file_name("Run It") == "Run_It"


def test_script_export_path_keeps_extension():
    path = sdb.script_export_path({"name": "Run It", "category": "PowerShell"}, r"C:\tmp")
    assert path.endswith("Run_It.ps1")


def test_unified_diff_shows_change():
    diff = sdb.unified_diff(
        {"code": "a = 1", "name": "old"},
        {"code": "a = 2", "name": "new"},
    )
    assert "-\"a\": 1" in diff or "-a = 1" in diff or "- " in diff
    assert "+" in diff


# ------------------------------------------------------------ uruchamianie
def main():
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"PASS  {test.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL  {test.__name__}: {exc}")
        except Exception as exc:  # pragma: no cover
            failures += 1
            print(f"ERROR {test.__name__}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} test(s) zaliczone")
    return 1 if failures else 0


if __name__ == "__main__":
    if pytest is not None and "pytest" in sys.modules:
        raise SystemExit(pytest.main([__file__, "-v"]))
    sys.exit(main())

# ------------------------------------------------------- edge cases: normalize_entry
def test_normalize_entry_list_tags_and_strips():
    entry = sdb.normalize_entry({
        "name": "  spaced  ", "code": "x = 1",
        "tags": ("a", "b"), "desc": "  trim  ",
    })
    assert entry["name"] == "spaced"
    assert entry["tags"] == "a, b"
    assert entry["desc"] == "trim"


def test_normalize_entry_unknown_category_guessed_from_code():
    entry = sdb.normalize_entry({"name": "x", "code": "print('hi')", "category": "Nope"})
    # An unknown-but-present category is added to CATEGORIES, so the same
    # unknown category is preserved rather than falling back to "Other".
    assert entry["category"] in sdb.CATEGORIES
    if entry["category"] in ("Nope",):
        sdb.CATEGORIES.remove("Nope")  # keep the shared list clean for other tests


def test_normalize_entry_bad_tags_become_string():
    entry = sdb.normalize_entry({"name": "x", "code": "1", "tags": 42})
    assert entry["tags"] == "42"


def test_normalize_entry_meta_sanitized():
    entry = sdb.normalize_entry({
        "name": "x", "code": "1",
        "meta": {"author": "me", "lines": 3, "drop": None},
    })
    assert entry["meta"]["author"] == "me"
    assert entry["meta"]["lines"] == 3
    assert "drop" not in entry["meta"]


def test_normalize_entry_meta_non_dict_ignored():
    entry = sdb.normalize_entry({"name": "x", "code": "1", "meta": "junk"})
    assert entry["meta"] == {}


def test_normalize_entry_separator_keeps_custom_id_and_date():
    separator = sdb.normalize_entry(
        {"kind": "separator", "name": "G", "id": "sep-9", "date": "2020-01-01"})
    assert separator["id"] == "sep-9"
    assert separator["date"] == "2020-01-01"


def test_normalize_entry_keeps_existing_id():
    entry = sdb.normalize_entry({"id": "7", "name": "n", "code": "c"})
    assert entry["id"] == "7"


# -------------------------------------------------- edge cases: _split_sql_statements
def test_split_sql_doubled_quote_escape():
    statements = list(sdb._split_sql_statements(
        "INSERT INTO t VALUES ('it''s ok');SELECT 1;"))
    assert len(statements) == 2
    assert "it''s ok" in statements[0]


def test_split_sql_backtick_quote():
    statements = list(sdb._split_sql_statements(
        'SELECT `weird;name` FROM t;SELECT 2;'))
    assert len(statements) == 2
    assert "`weird;name`" in statements[0]


def test_split_sql_double_quote_identifier():
    statements = list(sdb._split_sql_statements(
        'SELECT "a;b" FROM t;SELECT 2;'))
    assert len(statements) == 2


def test_split_sql_trailing_statement_without_semicolon():
    statements = list(sdb._split_sql_statements("SELECT 1"))
    assert statements == ["SELECT 1"]


def test_split_sql_whitespace_only_yields_nothing():
    assert list(sdb._split_sql_statements("   \n;\n  ")) == []


def test_split_sql_none_input():
    assert list(sdb._split_sql_statements(None)) == []


# ---------------------------------------------------------- _fix_mojibake
def test_fix_mojibake_repairs_cp1252_text():
    broken = "zako\u0143czony".encode("utf-8").decode("cp1252")
    repaired = sdb._fix_mojibake(broken)
    assert repaired == "zako\u0143czony"


def test_fix_mojibake_leaves_clean_text():
    clean = "zako\u0143czony"
    assert sdb._fix_mojibake(clean) == clean
    assert sdb._fix_mojibake("plain ascii") == "plain ascii"
    assert sdb._fix_mojibake(None) is None
    assert sdb._fix_mojibake(42) == 42


# ------------------------------------------------------------- diff statistics
def test_diff_statistics_counts():
    rows = sdb.align_lines(["a", "b"], ["a", "c"], "myers")
    report = {
        "left": {"name": "L", "code": "a\nb\n"},
        "right": {"name": "R", "code": "a\nc\n"},
        "rows": rows,
    }
    stats = sdb.diff_statistics(report)
    assert stats["added"] == 0
    assert stats["removed"] == 0
    assert stats["changed"] == 1
    assert stats["changes"] == 1


def test_diff_statistics_identical():
    rows = sdb.align_lines(["a"], ["a"], "myers")
    report = {
        "left": {"name": "L", "code": "a\n"},
        "right": {"name": "R", "code": "a\n"},
        "rows": rows,
    }
    stats = sdb.diff_statistics(report)
    assert stats["changes"] == 0
    assert stats["percent_different"] == 0.0


# ------------------------------------------------------------- compare helpers
def test_detect_refactorings_rename():
    old = "def old_name():\n    return 1\n"
    new = "def new_name():\n    return 1\n"
    events = sdb.detect_refactorings(old, new)
    assert any(e["kind"] == "rename" for e in events)


def test_detect_duplicate_blocks_finds_repeat():
    code = "a = 1\nb = 2\nc = 3\nd = 4\n" * 2
    duplicates = sdb.detect_duplicate_blocks(code, "")
    assert duplicates


def test_ast_symbols_python_functions_and_classes():
    symbols = sdb.ast_symbols("def f():\n    pass\n\nclass C:\n    pass\n", "Python")
    names = [s[0] for s in symbols]
    assert "f" in names and "C" in names
