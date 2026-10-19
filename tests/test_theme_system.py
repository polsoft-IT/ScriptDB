import tabs.theme as app_theme


def test_theme_resolution_and_labels():
    assert app_theme.resolve_theme("dark")["name"] == "dark"
    assert app_theme.resolve_theme("missing")["name"] == "dark"
    assert app_theme.theme_label("light") == "Light"


def test_theme_meta_reports_accessible_contrast():
    info = app_theme.theme_info("light")
    assert info["is_dark"] is False
    assert info["accent"] == "#2b6cb0"
    assert info["text_on_accent"] == "#ffffff"
    assert info["selection"] == "#b5d6ff"
