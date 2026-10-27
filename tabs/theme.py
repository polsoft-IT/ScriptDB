# -*- coding: utf-8 -*-
"""Theme for ScriptDB: one stylesheet, applied once, for the whole window.

Before this module every widget carried its own hard-coded hex colours, so the
application had no single look: a button in one tab was a slightly different
grey from the same button in another, and changing anything meant editing every
file. This module states the palette once and builds one Qt stylesheet from it,
which the main window applies to itself -- child widgets then inherit it.

    THEMES               the palettes, as plain data
    theme_names()        the keys a menu iterates
    build_stylesheet()   the Qt stylesheet for one theme
    palette_for()        the colour mapping, for code that draws by hand
    apply_theme()        set it on a widget (or the whole application)

Two rules are kept deliberately:

    * the palette is plain data, so a third theme is a dictionary entry and
      nothing else;
    * every colour is a printable-ASCII hex string, so the same definition
      works with or without a desktop theme, and nothing here needs a font,
      an image or a resource file to look right.

The "low graphics" mode the editor already offers is respected: ``apply_theme``
takes a flag that swaps the styled sheet for a minimal one, so a machine without
a compositor still gets a usable window.
"""


def _hex(value):
    """A colour as ``#rrggbb``, checked so a typo cannot reach Qt."""
    text = str(value or "").strip()
    if not text.startswith("#"):
        text = "#" + text
    if len(text) == 4:                       #abc -> #aabbcc
        text = "#" + "".join(char * 2 for char in text[1:])
    if len(text) != 7:
        return "#000"
    try:
        int(text[1:], 16)
    except ValueError:
        return "#000"
    return text.lower()


def _mix(first, second, weight=0.5):
    """Blend two colours: ``weight`` 0 gives the first, 1 gives the second."""
    first, second = _hex(first), _hex(second)
    weight = max(0.0, min(1.0, float(weight)))
    out = []
    for index in (1, 3, 5):
        left = int(first[index:index + 2], 16)
        right = int(second[index:index + 2], 16)
        out.append(int(left + (right - left) * weight))
    return "#%02x%02x%02x" % tuple(out)


# ---------------------------------------------------------------------------
# The palettes
# ---------------------------------------------------------------------------
# Every theme names the same roles.  A role is what the colour is *for*, not
# what it looks like, so a new theme only has to answer the same questions.
_DARK = {
    "name": "dark",
    "label": "Dark",
    "window": "#1e1e1e",          # the window itself
    "panel": "#252526",           # a raised surface: a tool row, a group
    "panel_alt": "#2a2d2e",       # the second surface, for stripes
    "input": "#1b1b1c",           # an editable field
    "border": "#3c3c3c",          # the ordinary dividing line
    "border_strong": "#4a4a4a",   # a line that must read as a wall
    "text": "#c8cfda",            # the body text (darker, less glare)
    "text_dim": "#8a9096",        # a label that is secondary
    "text_faint": "#6e7681",      # a hint, a placeholder
    "accent": "#8ab4f8",          # the one colour that means "this app"
    "accent_text": "#0b1220",     # text on top of the accent
    "ok": "#9ece6a",
    "warn": "#f7c66d",
    "error": "#f7768e",
    "special": "#bb9af7",
    "selection": "#264f78",
    "code_bg": "#1f1f1f",
}

_LIGHT = {
    "name": "light",
    "label": "Light",
    "window": "#f5f6f8",
    "panel": "#ffffff",
    "panel_alt": "#eef1f5",
    "input": "#ffffff",
    "border": "#d7dbe0",
    "border_strong": "#b9c0c8",
    "text": "#1f2328",
    "text_dim": "#57606a",
    "text_faint": "#8b949e",
    "accent": "#2b6cb0",
    "accent_text": "#ffffff",
    "ok": "#2f8f46",
    "warn": "#a06800",
    "error": "#c02a3a",
    "special": "#8250df",
    "selection": "#b5d6ff",
    "code_bg": "#ffffff",
}

#: A monochrome palette for a machine with no colour, or a user who wants none.
_MONO = {
    "name": "mono",
    "label": "Monochrome",
    "window": "#0d0d0d",
    "panel": "#161616",
    "panel_alt": "#1e1e1e",
    "input": "#0d0d0d",
    "border": "#3a3a3a",
    "border_strong": "#5a5a5a",
    "text": "#cfcfcf",
    "text_dim": "#949494",
    "text_faint": "#707070",
    "accent": "#e6e6e6",
    "accent_text": "#0d0d0d",
    "ok": "#d0d0d0",
    "warn": "#b8b8b8",
    "error": "#f0f0f0",
    "special": "#c8c8c8",
    "selection": "#3a3a3a",
    "code_bg": "#0d0d0d",
}

#: The themes, in menu order.
THEMES = {
    "dark": _DARK,
    "light": _LIGHT,
    "mono": _MONO,
}

#: The role a piece of code should ask for, by the name it uses.
ROLES = tuple(sorted(_DARK))

#: The theme a fresh install starts with.
DEFAULT_THEME = "dark"


def theme_names():
    """The theme keys, in menu order."""
    return ("dark", "light", "mono")


def resolve_theme(theme=DEFAULT_THEME):
    """Return a valid palette dict for a user preference or theme key."""
    if isinstance(theme, dict):
        return palette_for(theme.get("name", DEFAULT_THEME))
    if theme is None:
        return _DARK
    key = str(theme).strip().lower()
    if key in THEMES:
        return THEMES[key]
    for name, palette in THEMES.items():
        if name.lower() == key:
            return palette
    return _DARK


def theme_label(key):
    """The name of a theme as a person reads it."""
    palette = resolve_theme(key)
    return palette.get("label", str(key))


def _relative_luminance(hex_value):
    """Perceived brightness in the range 0..1 for contrast checks."""
    value = _hex(hex_value)
    red, green, blue = [int(value[index:index + 2], 16) / 255.0
                        for index in (1, 3, 5)]
    channels = []
    for channel in (red, green, blue):
        if channel <= 0.03928:
            channels.append(channel / 12.92)
        else:
            channels.append(((channel + 0.055) / 1.055) ** 2.4)
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def theme_info(theme=DEFAULT_THEME):
    """Theme metadata helpful for widgets and contrast-sensitive UI."""
    palette = palette_for(theme)
    accent = palette.get("accent", _DARK["accent"])
    text_on_accent = palette.get("accent_text", _DARK["accent_text"])
    selection = palette.get("selection", _DARK["selection"])
    window = palette.get("window", _DARK["window"])
    return {
        "name": palette.get("name", DEFAULT_THEME),
        "label": theme_label(palette.get("name", DEFAULT_THEME)),
        "is_dark": bool(_relative_luminance(window) < 0.5),
        "accent": accent,
        "text_on_accent": text_on_accent,
        "selection": selection,
        "window": window,
        "panel": palette.get("panel", _DARK["panel"]),
        "text": palette.get("text", _DARK["text"]),
        "border": palette.get("border", _DARK["border"]),
    }


def palette_for(theme=DEFAULT_THEME):
    """The colours of one theme, with every role present.

    A missing role falls back to the dark theme, so a half-written palette
    still produces a complete, usable window instead of a crash.
    """
    chosen = resolve_theme(theme)
    out = dict(_DARK)
    out.update({key: value for key, value in chosen.items()
                if key in ROLES})
    return out


def color(role, theme=DEFAULT_THEME):
    """One colour, by the role it plays."""
    return palette_for(theme).get(role, _DARK.get(role, "#000"))
# ---------------------------------------------------------------------------
# The stylesheet
# ---------------------------------------------------------------------------
def build_stylesheet(theme=DEFAULT_THEME, compact=False):
    """A complete Qt stylesheet for one theme.

    The sheet is built from the palette rather than written out by hand, so
    every widget gets the *same* surface, border and text colour and the window
    reads as one program.  ``compact`` tightens the padding for a small screen
    without changing any colour.
    """
    p = palette_for(theme)
    radius = "4px" if not compact else "3px"
    pad = "6px 12px" if not compact else "4px 8px"
    gap = "6px" if not compact else "4px"

    # A subtle raised panel: the colour is derived from the surface so a new
    # theme gets a sensible panel without naming one.
    hover = _mix(p["panel"], p["text"], 0.10)
    pressed = _mix(p["panel"], p["text"], 0.18)
    disabled_text = _mix(p["text_dim"], p["window"], 0.45)

    return "\n".join([
        # ---- the window and the ordinary text --------------------------
        "QWidget {",
        "    background-color: %(window)s;" % p,
        "    color: %(text)s;" % p,
        "    font-size: 12px;",
        "}",
        "QMainWindow::separator {",
        "    background: %(border)s;" % p,
        "    width: 2px; height: 2px;",
        "}",
        "QLabel {",
        "    background: transparent;",
        "}",
        "QLabel[role=\"title\"] {",
        "    font-size: 15px;",
        "    font-weight: bold;",
        "    color: %(accent)s;" % p,
        "}",
        "QLabel[role=\"hint\"] {",
        "    color: %(text_dim)s;" % p,
        "}",
        "QLabel[role=\"state-ok\"] { color: %(ok)s; font-weight: bold; }" % p,
        "QLabel[role=\"state-warn\"] { color: %(warn)s; font-weight: bold; }" % p,
        "QLabel[role=\"state-error\"] { color: %(error)s; font-weight: bold; }" % p,

        # ---- buttons ---------------------------------------------------
        "QPushButton, QToolButton {",
        "    background-color: %(panel)s;" % p,
        "    color: %(text)s;" % p,
        "    border: 1px solid %(border)s;" % p,
        "    border-radius: %(radius)s;" % {"radius": radius},
        "    padding: %(pad)s;" % {"pad": pad},
        "}",
        "QPushButton:hover, QToolButton:hover {",
        "    background-color: %(hover)s;" % {"hover": hover},
        "    border-color: %(accent)s;" % p,
        "}",
        "QPushButton:pressed, QToolButton:pressed {",
        "    background-color: %(pressed)s;" % {"pressed": pressed},
        "}",
        "QPushButton:disabled, QToolButton:disabled {",
        "    color: %(dim)s;" % {"dim": disabled_text},
        "    border-color: %(border)s;" % p,
        "}",
        "QPushButton:checked, QToolButton:checked {",
        "    background-color: %(sel)s;" % {"sel": p["selection"]},
        "    border-color: %(accent)s;" % p,
        "}",
        "QPushButton:default {",
        "    border-color: %(accent)s;" % p,
        "}",
        "QToolButton::menu-indicator { width: 8px; }",

        # ---- the tab strip ---------------------------------------------
        "QTabWidget::pane {",
        "    border: 1px solid %(border)s;" % p,
        "    border-radius: %(radius)s;" % {"radius": radius},
        "    top: -1px;",
        "}",
        "QTabBar::tab {",
        "    background: %(panel)s;" % p,
        "    color: %(text_dim)s;" % p,
        "    border: 1px solid %(border)s;" % p,
        "    border-bottom: none;",
        "    border-top-left-radius: %(radius)s;" % {"radius": radius},
        "    border-top-right-radius: %(radius)s;" % {"radius": radius},
        "    padding: 6px 12px;",
        "    margin-right: 2px;",
        "}",
        "QTabBar::tab:selected {",
        "    background: %(window)s;" % p,
        "    color: %(accent)s;" % p,
        "    border-bottom: 2px solid %(accent)s;" % p,
        "}",
        "QTabBar::tab:hover:!selected {",
        "    color: %(text)s;" % p,
        "}",
        "QTabBar::tab:disabled { color: %(dim)s; }"
        % {"dim": disabled_text},

        # ---- text fields -----------------------------------------------
        "QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QComboBox {",
        "    background-color: %(input)s;" % p,
        "    color: %(text)s;" % p,
        "    border: 1px solid %(border)s;" % p,
        "    border-radius: %(radius)s;" % {"radius": radius},
        "    padding: 4px 6px;",
        "    selection-background-color: %(sel)s;" % {"sel": p["selection"]},
        "    selection-color: %(text)s;" % p,
        "}",
        "QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus,"
        " QSpinBox:focus, QComboBox:focus {",
        "    border-color: %(accent)s;" % p,
        "}",
        "QLineEdit:disabled, QPlainTextEdit:disabled, QTextEdit:disabled {",
        "    color: %(dim)s;" % {"dim": disabled_text},
        "}",
        "QComboBox::drop-down {",
        "    border: none;",
        "    width: 18px;",
        "}",
        "QComboBox QAbstractItemView {",
        "    background-color: %(panel)s;" % p,
        "    color: %(text)s;" % p,
        "    border: 1px solid %(border)s;" % p,
        "    selection-background-color: %(sel)s;" % {"sel": p["selection"]},
        "}",
        "QCheckBox, QRadioButton {",
        "    spacing: %(gap)s;" % {"gap": gap},
        "}",
        "QCheckBox::indicator, QRadioButton::indicator {",
        "    width: 14px; height: 14px;",
        "    border: 1px solid %(border_strong)s;" % p,
        "    background: %(input)s;" % p,
        "}",
        "QCheckBox::indicator:checked, QRadioButton::indicator:checked {",
        "    background: %(accent)s;" % p,
        "    border-color: %(accent)s;" % p,
        "}",

        # ---- the lists, trees and tables -------------------------------
        "QListWidget, QTreeWidget, QTableWidget, QListView, QTreeView,"
        " QTableView {",
        "    background-color: %(input)s;" % p,
        "    alternate-background-color: %(alt)s;" % {"alt": p["panel_alt"]},
        "    color: %(text)s;" % p,
        "    border: 1px solid %(border)s;" % p,
        "    border-radius: %(radius)s;" % {"radius": radius},
        "    gridline-color: %(border)s;" % p,
        "    outline: none;",
        "}",
        "QListWidget::item, QTreeWidget::item, QTableWidget::item {",
        "    padding: 3px 4px;",
        "}",
        "QListWidget::item:selected, QTreeWidget::item:selected,"
        " QTableWidget::item:selected {",
        "    background-color: %(sel)s;" % {"sel": p["selection"]},
        "    color: %(text)s;" % p,
        "}",
        "QListWidget::item:hover, QTreeWidget::item:hover,"
        " QTableWidget::item:hover {",
        "    background-color: %(hover)s;" % {"hover": hover},
        "}",
        "QHeaderView::section {",
        "    background-color: %(panel)s;" % p,
        "    color: %(text_dim)s;" % p,
        "    border: none;",
        "    border-right: 1px solid %(border)s;" % p,
        "    border-bottom: 1px solid %(border)s;" % p,
        "    padding: 5px 8px;",
        "    font-weight: bold;",
        "}",
        "QHeaderView::section:hover {",
        "    color: %(accent)s;" % p,
        "}",
        "QTableCornerButton::section {",
        "    background-color: %(panel)s;" % p,
        "    border: none;",
        "}",

        # ---- scrolling --------------------------------------------------
        "QScrollBar:vertical {",
        "    background: transparent;",
        "    width: 12px; margin: 0;",
        "}",
        "QScrollBar::handle:vertical {",
        "    background: %(strong)s;" % {"strong": p["border_strong"]},
        "    border-radius: 5px;",
        "    min-height: 28px;",
        "}",
        "QScrollBar::handle:vertical:hover {",
        "    background: %(accent)s;" % p,
        "}",
        "QScrollBar:horizontal {",
        "    background: transparent;",
        "    height: 12px; margin: 0;",
        "}",
        "QScrollBar::handle:horizontal {",
        "    background: %(strong)s;" % {"strong": p["border_strong"]},
        "    border-radius: 5px;",
        "    min-width: 28px;",
        "}",
        "QScrollBar::handle:horizontal:hover {",
        "    background: %(accent)s;" % p,
        "}",
        "QScrollBar::add-line, QScrollBar::sub-line {",
        "    height: 0; width: 0;",
        "}",
        "QScrollBar::add-page, QScrollBar::sub-page {",
        "    background: transparent;",
        "}",

        # ---- the surface containers ------------------------------------
        "QGroupBox {",
        "    border: 1px solid %(border)s;" % p,
        "    border-radius: %(radius)s;" % {"radius": radius},
        "    margin-top: 10px;",
        "    padding-top: 8px;",
        "}",
        "QGroupBox::title {",
        "    subcontrol-origin: margin;",
        "    left: 8px;",
        "    padding: 0 4px;",
        "    color: %(accent)s;" % p,
        "}",
        "QSplitter::handle {",
        "    background: %(border)s;" % p,
        "}",
        "QSplitter::handle:horizontal { width: 3px; }",
        "QSplitter::handle:vertical { height: 3px; }",
        "QSplitter::handle:hover { background: %(accent)s;" % p,
        "}",
        "QStatusBar {",
        "    background: %(panel)s;" % p,
        "    color: %(text_dim)s;" % p,
        "}",
        "QToolTip {",
        "    background-color: %(panel)s;" % p,
        "    color: %(text)s;" % p,
        "    border: 1px solid %(accent)s;" % p,
        "    padding: 4px 6px;",
        "}",
        "QMenu {",
        "    background-color: %(panel)s;" % p,
        "    color: %(text)s;" % p,
        "    border: 1px solid %(border)s;" % p,
        "    padding: 4px;",
        "}",
        "QMenu::item {",
        "    padding: 5px 22px 5px 12px;",
        "    border-radius: 3px;",
        "}",
        "QMenu::item:selected {",
        "    background-color: %(sel)s;" % {"sel": p["selection"]},
        "}",
        "QMenu::separator {",
        "    height: 1px;",
        "    background: %(border)s;" % p,
        "    margin: 4px 6px;",
        "}",
        "QProgressBar {",
        "    background: %(input)s;" % p,
        "    border: 1px solid %(border)s;" % p,
        "    border-radius: %(radius)s;" % {"radius": radius},
        "    text-align: center;",
        "    color: %(text)s;" % p,
        "}",
        "QProgressBar::chunk {",
        "    background-color: %(accent)s;" % p,
        "    border-radius: %(radius)s;" % {"radius": radius},
        "}",
        "QDialog {",
        "    background-color: %(window)s;" % p,
        "}",
        "QMessageBox QLabel { color: %(text)s; }" % p,

        # ---- sliders, the last control with no styling at all ----------------
        "QSlider::groove:horizontal {",
        "    height: 4px;",
        "    background: %(input)s;" % p,
        "    border: 1px solid %(border)s;" % p,
        "    border-radius: 2px;",
        "}",
        "QSlider::sub-page:horizontal {",
        "    background: %(accent)s;" % p,
        "    border-radius: 2px;",
        "}",
        "QSlider::handle:horizontal {",
        "    background: %(panel)s;" % p,
        "    border: 2px solid %(accent)s;" % p,
        "    width: 12px; height: 12px;",
        "    margin: -5px 0;",
        "    border-radius: 8px;",
        "}",
        "QSlider::handle:horizontal:hover {",
        "    background: %(accent)s;" % p,
        "}",

        # ---- the toolbar buttons that open a menu -------------------------
        # A QToolButton with a menu draws a small arrow; without this the arrow
        # is drawn by the platform style and reads as a different widget.
        "QToolButton[popupMode=\"1\"], QToolButton[popupMode=\"2\"] {",
        "    padding-right: 18px;",
        "}",
        "QToolButton::menu-button {",
        "    border-left: 1px solid %(border)s;" % p,
        "    width: 16px;",
        "}",
        "QToolButton::menu-arrow, QComboBox::down-arrow {",
        "    image: none;",
        "    border-left: 4px solid transparent;",
        "    border-right: 4px solid transparent;",
        "    border-top: 5px solid %(text_dim)s;" % p,
        "    width: 0; height: 0;",
        "}",
        "QToolButton:hover::menu-arrow, QComboBox:hover::down-arrow {",
        "    border-top-color: %(accent)s;" % p,
        "}",

        # ---- the containers that hold a whole tab -----------------------
        # A QScrollArea is transparent by default and then shows the window
        # colour through a panel that was meant to be a surface.
        "QScrollArea { border: none; background: transparent; }",
        "QScrollArea > QWidget > QWidget { background: transparent; }",
        "QAbstractScrollArea::corner { background: %(window)s; }" % p,
        "QFrame[frameShape=\"4\"], QFrame[frameShape=\"5\"] {",
        "    color: %(border)s;" % p,
        "}",
        "QFrame[role=\"panel\"] {",
        "    background: %(panel)s;" % p,
        "    border: 1px solid %(border)s;" % p,
        "    border-radius: %(radius)s;" % {"radius": radius},
        "}",

        # ---- dialogs and their button rows ------------------------------
        "QDialogButtonBox QPushButton {",
        "    min-width: 78px;",
        "}",
        "QDialog QLabel, QMessageBox QLabel { background: transparent; }",

        # ---- the tab strip of a nested QTabWidget -----------------------
        "QTabWidget::tab-bar { left: 0; }",

        # ---- headings the tabs set as object names ----------------------
        "QLabel[role=\"metric\"] {",
        "    font-size: 17px; font-weight: bold; color: %(text)s;" % p,
        "}",
        "QLabel[role=\"caption\"] { color: %(text_dim)s; font-size: 11px; }" % p,
    ])


def build_low_graphics_stylesheet(theme=DEFAULT_THEME):
    """A minimal sheet: no rounding, no hover colour, one border colour.

    This is what the editor already calls "low graphics", extended to the whole
    window: it is for a machine where every extra rule costs something, or for a
    user who wants the window to get out of the way.
    """
    p = palette_for(theme)
    mono = palette_for("mono") if theme != "mono" else p
    return "\n".join([
        "QWidget { background-color: %s; color: %s; }" % (mono["window"],
                                                          mono["text"]),
        "QLabel { background: transparent; }",
        "QPushButton, QToolButton, QLineEdit, QPlainTextEdit, QTextEdit,",
        " QListWidget, QTreeWidget, QTableWidget, QComboBox, QSpinBox {",
        "    background-color: %s;" % mono["input"],
        "    color: %s;" % mono["text"],
        "    border: 1px solid %s;" % mono["border_strong"],
        "    padding: 3px 5px;",
        "}",
        "QTabBar::tab {",
        "    background: %s;" % mono["panel"],
        "    color: %s;" % mono["text_dim"],
        "    border: 1px solid %s;" % mono["border_strong"],
        "    padding: 4px 8px;",
        "}",
        "QTabBar::tab:selected {",
        "    color: %s;" % mono["text"],
        "    border-bottom: 2px solid %s;" % mono["text"],
        "}",
        "QHeaderView::section {",
        "    background: %s;" % mono["panel"],
        "    color: %s;" % mono["text"],
        "    border: 1px solid %s;" % mono["border_strong"],
        "    padding: 3px 6px;",
        "}",
        "QListWidget::item:selected, QTreeWidget::item:selected,",
        " QTableWidget::item:selected {",
        "    background-color: %s;" % mono["selection"],
        "}",
        "QScrollBar:vertical, QScrollBar:horizontal {"
        " background: transparent; width: 10px; height: 10px; }",
        "QScrollBar::handle:vertical, QScrollBar::handle:horizontal {"
        " background: %s; }" % mono["border_strong"],
        "QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }",
    ])

# ---------------------------------------------------------------------------
# The Qt palette
# ---------------------------------------------------------------------------
# The stylesheet covers the widgets it names, but plenty of drawing happens
# through QPalette instead: a QToolTip, the default text colour inside a
# QTextDocument, the highlight of a text selection, the colour a QTableWidget
# uses for a cell the stylesheet never touched.  Stating the same colours a
# second time as a palette is what stops those from falling back to the platform
# default and looking like a different program.  A QPalette needs Qt, so this is
# the one function here that imports it, and it degrades to ``None`` when Qt is
# unavailable so the module still imports for a head-less build.

def build_palette(theme=DEFAULT_THEME, compact=False):
    # The theme as a QPalette, or None when Qt is not importable.""
    del compact                                  # the palette has no sizes
    # A QPalette needs Qt, so this is the one place in the module that imports
    # it; without Qt the caller gets None and only the stylesheet is applied.
    try:
        from PyQt5 import QtGui
    except ImportError:                          # pragma: no cover
        return None
    p = palette_for(theme)
    role = QtGui.QPalette
    out = role()
    pairs = [
        (role.Window, "window"),
        (role.WindowText, "text"),
        (role.Base, "input"),
        (role.AlternateBase, "panel_alt"),
        (role.Text, "text"),
        (role.Button, "panel"),
        (role.ButtonText, "text"),
        (role.BrightText, "error"),
        (role.ToolTipBase, "panel"),
        (role.ToolTipText, "text"),
        (role.Highlight, "selection"),
        (role.HighlightedText, "text"),
        (role.Link, "accent"),
        (role.PlaceholderText, "text_faint") if hasattr(role, "PlaceholderText")
        else None,
    ]
    for entry in pairs:
        if entry is None:
            continue
        which, key = entry
        out.setColor(which, QtGui.QColor(p[key]))
    # The "dark" shade of a colour is what a disabled widget is drawn with; a
    # group is set so both states are covered in one go.
    for group, keys in (
            ((role.Active, role.Inactive),
             (("text", "text"), ("button", "panel"),
              ("button_text", "text"), ("base", "input"),
              ("window", "window"), ("window_text", "text"),
              ("highlight", "selection"), ("highlighted_text", "text"))),
            ((role.Disabled,), (("text", "text_faint"),
                                ("button_text", "text_faint"),
                                ("window_text", "text_faint"),
                                ("highlighted_text", "text_faint"),
                                ("button", "panel"), ("base", "input"),
                                ("window", "window")))):
        for name in group:
            for slot, key in keys:
                method = getattr(out, "setColor", None)
                if method is None:
                    continue
                try:
                    out.setColor(name, getattr(role, slot.title().replace("_", "")),
                                 QtGui.QColor(p[key]))
                except AttributeError:
                    pass
    return out

def apply_theme(widget, theme=DEFAULT_THEME, compact=False, low_graphics=False):
    # # Style one widget (or the application).  Returns the widget so a caller
    # can write ``window = apply_theme(window)``.  A palette is set first and
    # the stylesheet second, because the sheet is what the user sees and the
    # palette is what it falls back to.
    if widget is None:
        return widget
    palette = build_palette(theme, compact)
    if palette is not None:
        try:
            widget.setPalette(palette)
        except (AttributeError, TypeError):
            pass
    if low_graphics:
        sheet = build_low_graphics_stylesheet(theme)
    else:
        sheet = build_stylesheet(theme, compact)
    try:
        widget.setStyleSheet(sheet)
    except (AttributeError, TypeError):
        pass
    return widget
