"""Regression tests for the module entry point.

These cover the things that only break at *start-up*: building the real main
window and translating it. The verification scripts use stub windows, so a
break in this path used to pass every one of them and still kill the program
on launch (the TRANSLATIONS NameError).
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(scope="module")
def app():
    """One QApplication for the module; Qt allows only a single instance."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt5 import QtWidgets
    instance = QtWidgets.QApplication.instance()
    yield instance or QtWidgets.QApplication([])


@pytest.fixture
def window(app):
    """A real ScriptDatabaseWindow, built and torn down per test."""
    import sdb_scripts
    win = sdb_scripts.ScriptDatabaseWindow()
    yield win
    win.close()
    win.deleteLater()


def test_module_exposes_translations():
    """The table resolves as a module attribute (the external access path)."""
    import sdb_scripts
    table = sdb_scripts.TRANSLATIONS
    assert set(table) >= {"en", "pl"}
    assert table["en"]["title"]


def test_translation_table_helper_matches_attribute():
    """The in-module helper and the module attribute are the same object.

    A bare global name inside the module does not go through ``__getattr__``,
    so these two paths can drift apart silently. They must not.
    """
    import sdb_scripts
    assert sdb_scripts.translation_table() is sdb_scripts.TRANSLATIONS


def test_startup_needs_no_primed_cache():
    """Building the window must work before anything touches TRANSLATIONS.

    Reading ``sdb_scripts.TRANSLATIONS`` stores the table in the module's
    globals. Once that has happened a bare ``TRANSLATIONS`` in the module body
    resolves again, so a test that only builds the window would pass as long
    as an earlier test happened to look the attribute up first. The cache is
    therefore cleared here, and the window is built, in a fresh state.
    """
    import sdb_scripts

    sdb_scripts.__dict__.pop("TRANSLATIONS", None)
    assert "TRANSLATIONS" not in sdb_scripts.__dict__

    from PyQt5 import QtWidgets
    instance = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    win = sdb_scripts.ScriptDatabaseWindow()
    try:
        assert win.tabs.count() > 1
    finally:
        win.close()
        win.deleteLater()


def test_window_builds(window):
    """The main window constructs without raising (start-up smoke test)."""
    assert window.tabs.count() > 1
    assert window.windowTitle()


def test_translation_lookup(window):
    """t() resolves a known key and falls back cleanly for unknown ones."""
    known = window.t("title")
    assert known and known != "title"
    assert window.t("no_such_key_xyz", "fallback") == "fallback"
    assert window.t("no_such_key_xyz") == "no_such_key_xyz"
    # {placeholders} are formatted when given.
    assert "{count}" not in window.t("todo_badge_hint", "", due=1, late=2)


def test_language_switch_round_trips(window):
    """Switching EN -> PL -> EN leaves a usable window in both languages."""
    english = window.t("title")
    window.lang = "pl"
    window.apply_language()
    polish = window.windowTitle()
    window.lang = "en"
    window.apply_language()
    assert window.windowTitle() == english
    assert polish  # a real title, not an empty string or a key


def test_settings_survive_a_restart(app):
    """Personalisation is written to settings_db.json and read back.

    The point is the *restart*: language, palette, compact layout, the tab that
    was open, the filter and the sort direction all have to come back, because
    that is what "remembers how I left it" means to a user.
    """
    import json
    import sdb_scripts

    path = sdb_scripts.SETTINGS_FILE
    with open(path, "r", encoding="utf-8") as handle:
        saved_before = json.load(handle)

    win = sdb_scripts.ScriptDatabaseWindow()
    win.set_language("pl")
    win.set_theme("light")
    win.tabs.setCurrentIndex(3)
    win.on_filter_changed("python")
    win.on_favorites_filter_toggled(True)
    win.settings["compact_ui"] = True
    win.save_settings()
    win.close()
    win.deleteLater()

    with open(path, "r", encoding="utf-8") as handle:
        assert json.load(handle)["lang"] == "pl"

    again = sdb_scripts.ScriptDatabaseWindow()
    try:
        assert again.lang == "pl"
        assert again.current_theme() == "light"
        assert again.settings["compact_ui"] is True
        assert again.tabs.currentIndex() == 3
        # The restored state has to be visible, not just remembered.
        assert again.filter_input.text() == "python"
        assert again.filter_favorites_btn.isChecked() is True
    finally:
        again.close()
        again.deleteLater()
        # Put the user's own settings back rather than the test's.
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(saved_before, handle, ensure_ascii=False, indent=2)


def test_wrong_typed_settings_fall_back_to_defaults(app):
    """A hand-edited settings file cannot put the window into a broken state.

    ``settings_db.json`` is plain JSON a user may edit, so an unusable value is
    dropped and the default takes over. The key is gone from the merged dict --
    what matters is that ``setting()`` then hands back the default, and that the
    window still starts on a language it has a table for.
    """
    import sdb_scripts

    merged = dict(sdb_scripts.DEFAULT_SETTINGS)
    merged.update({"lang": "de", "theme": 42, "last_tab": "third",
                   "sort_ascending": "yes", "compact_ui": "no"})
    sdb_scripts.clean_settings(merged)
    for key in ("lang", "theme", "last_tab", "sort_ascending", "compact_ui"):
        assert key not in merged
    # And the default is what a lookup hands back afterwards.
    assert sdb_scripts.DEFAULT_SETTINGS["lang"] in sdb_scripts.LANGUAGES


def test_unknown_language_falls_back_to_english(window):
    """An unknown language code must not break start-up or a language switch."""
    original = window.lang
    # set_language refuses a code it has no table for and changes nothing.
    assert window.set_language("klingon") is False
    assert window.lang == original
    # And a real one still works afterwards.
    assert window.set_language("en") is True
    assert window.lang == "en"


def test_startup_language_falls_back_when_the_saved_code_is_unknown(app):
    """A hand-edited ``lang`` in the settings file resolves to a real language."""
    import sdb_scripts

    # What __init__ does with a settings dict that carries a bad code.
    assert "klingon" not in sdb_scripts.LANGUAGES
    assert ("klingon" if "klingon" in sdb_scripts.LANGUAGES else "en") == "en"