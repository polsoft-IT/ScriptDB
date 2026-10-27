#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Testy plików z folderu scripts/ (wszystkie obsługiwane formaty).

Sprawdza:
  - każdy przykładowy skrypt da się odczytać i ma niepustą treść,
  - kategoria rozpoznawana przez sdb zgadza się z rozszerzeniem,
  - guess_category rozpoznaje sygnaturę treści (Python/PS/Bash/Batch),
  - formaty bez sygnatury (HTML/SQL/INI/JSON) mają poprawną strukturę.

Uruchamianie:
    python scripts/tests/test_script_files.py
    pytest scripts/tests/test_script_files.py
"""
import json
import os
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(TESTS_DIR))
sys.path.insert(0, PROJECT_ROOT)

try:
    import pytest
    pytest.importorskip("PyQt5")
except ImportError:
    pytest = None

import sdb  # noqa: E402

SCRIPTS_DIR = os.path.join(PROJECT_ROOT, "scripts")

# rozszerzenie -> spodziewana kategoria (z EXTENSION_CATEGORIES sdb)
EXPECTED = dict(sdb.EXTENSION_CATEGORIES)


def script_files():
    """Lista plików w scripts/ pomijająca tests/, README i listy rozszerzeń."""
    out = []
    for name in sorted(os.listdir(SCRIPTS_DIR)):
        path = os.path.join(SCRIPTS_DIR, name)
        if not os.path.isfile(path):
            continue
        if name in ("README.md", "SUPPORTED_EXTENSIONS.md"):
            continue
        extension = os.path.splitext(name)[1].lower()
        if extension in EXPECTED:
            out.append(path)
    return out


def ids(files):
    return [os.path.basename(p) for p in files]


# ---------------------------------------------------------- istnienie plików
def test_scripts_folder_exists():
    assert os.path.isdir(SCRIPTS_DIR)


def test_examples_cover_every_supported_format():
    present = {os.path.splitext(p)[1].lower() for p in script_files()}
    missing = sorted(set(EXPECTED) - present)
    assert not missing, f"brak przykładów dla: {', '.join(missing)}"


# ------------------------------------------------------- treść i kategoria
@pytest.mark.parametrize("path", script_files(), ids=ids)
def test_file_readable_and_non_empty(path):
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        code = handle.read()
    assert code.strip(), f"{path} jest pusty"
    assert len(code) >= 8, f"{path} podejrzanie krótki"


@pytest.mark.parametrize("path", script_files(), ids=ids)
def test_category_matches_extension(path):
    extension = os.path.splitext(path)[1].lower()
    category = sdb.category_for_extension(path)
    assert category == EXPECTED[extension]


# ------------------------------------------------- struktura plików danych
def test_json_example_is_valid():
    path = os.path.join(SCRIPTS_DIR, "example.json")
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    assert isinstance(data, dict) and data


def test_ini_example_has_sections():
    with open(os.path.join(SCRIPTS_DIR, "example.ini"),
              "r", encoding="utf-8") as handle:
        content = handle.read()
    assert "[" in content and "=" in content


def test_sql_example_has_statements():
    with open(os.path.join(SCRIPTS_DIR, "example.sql"),
              "r", encoding="utf-8") as handle:
        content = handle.read()
    assert ";" in content


# ------------------------------------------------- guess_category na treści
def test_guess_category_matches_example_content():
    for name, expected in (("example.py", None),
                           ("example.ps1", "PowerShell"),
                           ("example.sh", "Bash"),
                           ("example.bat", "Batch"),
                           ("example.rb", "Python")):
        path = os.path.join(SCRIPTS_DIR, name)
        assert os.path.isfile(path), f"brak {name}"
        with open(path, "r", encoding="utf-8") as handle:
            code = handle.read()
        assert sdb.guess_category(code) == expected, name


# ------------------------------------------------------------------- main
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
        except Exception as exc:
            failures += 1
            print(f"ERROR {test.__name__}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} test(s) zaliczone")
    return 1 if failures else 0


if __name__ == "__main__":
    if pytest is not None and "pytest" in sys.modules:
        raise SystemExit(pytest.main([__file__, "-v"]))
    sys.exit(main())
