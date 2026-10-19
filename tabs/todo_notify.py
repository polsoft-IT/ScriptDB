# -*- coding: utf-8 -*-
"""Notification centre: small popups that work from anywhere in ScriptDB.

A reminder must be able to appear no matter which tab is open, so this is not
part of the ToDo tab: it is a tiny service the whole window shares.

    Notifier.post(...)      show a popup (non-blocking, stacks in a corner)
    Notifier.notify(...)    a reminder popup with Snooze / Done buttons
    Notifier.history()      what was shown, newest first
    notifier_for(window)    the one notifier per program

The popups are ordinary ``QWidget`` windows with ``Qt.ToolTip``-like flags, so
they do not steal focus, do not appear in the taskbar, and are not modal: a
reminder arriving while somebody types must not take the keyboard away.

Everything degrades safely. If there is no ``QApplication`` yet (a unit test,
the CLI) ``post()`` returns False instead of raising, and the ToDo tab falls
back to a status line.
"""

import datetime as dt

try:
    from PyQt5 import QtCore, QtGui, QtWidgets
except ImportError:  # pragma: no cover - Qt is required by the tabs
    QtCore = QtGui = QtWidgets = None


#: How long a plain (informational) popup stays up by default.
DEFAULT_SECONDS = 8

#: The corner a popup stacks into.
CORNER_TOP_RIGHT = "top-right"
CORNER_BOTTOM_RIGHT = "bottom-right"
CORNER_BOTTOM_LEFT = "bottom-left"
CORNER_TOP_LEFT = "top-left"

#: The kinds of message, and the colour role each one uses.
KINDS = {
    "info": ("accent", "\u2139"),
    "reminder": ("warn", "\u23f0"),
    "done": ("ok", "\u2713"),
    "warning": ("warn", "\u26a0"),
    "error": ("error", "\u2716"),
}


def _palette():
    """The colours the popups draw with.

    The theme module owns the palette; if it is unavailable the popups fall
    back to the values ScriptDB uses everywhere else, so a popup never renders
    black-on-black.
    """
    fallback = {"accent": "#8ab4f8", "warn": "#f7c66d", "ok": "#9ece6a",
                "error": "#f7768e", "panel": "#252526",
                "border": "#3c3c3c", "text": "#dfe7f5",
                "text_dim": "#9aa0a6"}
    try:
        from . import theme as _theme
        for role in list(fallback):
            fallback[role] = _theme.color(role)
    except Exception:
        pass
    return fallback


if QtWidgets is not None:

    class Toast(QtWidgets.QFrame):
        """One popup. Owns its own timer and removes itself when it expires."""

        WIDTH = 340
        MARGIN = 18
        GAP = 8

        def __init__(self, notifier, title, body="", kind="info",
                     seconds=DEFAULT_SECONDS, actions=(), icon_text=None,
                     on_close=None):
            super().__init__(None)
            self.notifier = notifier
            self._on_close = on_close
            self._closing = False
            palette = _palette()

            self.setWindowFlags(
                QtCore.Qt.ToolTip
                | QtCore.Qt.FramelessWindowHint
                | QtCore.Qt.WindowStaysOnTopHint
            )
            self.setAttribute(QtCore.Qt.WA_ShowWithoutActivating, True)
            self.setAttribute(QtCore.Qt.WA_DeleteOnClose, True)
            self.setFixedWidth(self.WIDTH)
            self.setStyleSheet(
                "QFrame { background: %(panel)s; border: 1px solid %(border)s;"
                " border-radius: 6px; }"
                " QLabel { background: transparent; color: %(text)s; }"
                " QPushButton { background: %(panel)s; color: %(text)s;"
                " border: 1px solid %(border)s; border-radius: 3px;"
                " padding: 3px 10px; }"
                " QPushButton:hover { border-color: %(accent)s; }"
                % palette
            )

            root = QtWidgets.QHBoxLayout(self)
            root.setContentsMargins(12, 10, 10, 10)
            root.setSpacing(10)

            role = KINDS.get(kind, KINDS["info"])[0]
            glyph = QtWidgets.QLabel(icon_text or KINDS.get(kind, KINDS["info"])[1])
            glyph.setStyleSheet(
                "color: %s; font-size: 20px; font-weight: bold;"
                % palette.get(role, palette["accent"]))
            glyph.setFixedWidth(26)
            glyph.setAlignment(QtCore.Qt.AlignTop | QtCore.Qt.AlignHCenter)
            root.addWidget(glyph)

            column = QtWidgets.QVBoxLayout()
            column.setSpacing(4)
            self.title_label = QtWidgets.QLabel(title or "")
            self.title_label.setStyleSheet("font-weight: bold; font-size: 13px;")
            self.title_label.setWordWrap(True)
            column.addWidget(self.title_label)
            if body:
                self.body_label = QtWidgets.QLabel(body)
                self.body_label.setWordWrap(True)
                self.body_label.setStyleSheet(
                    "color: %s;" % palette["text_dim"])
                column.addWidget(self.body_label)

            if actions:
                row = QtWidgets.QHBoxLayout()
                row.setSpacing(6)
                for label, callback in actions:
                    button = QtWidgets.QPushButton(label)
                    button.setCursor(QtCore.Qt.PointingHandCursor)
                    button.clicked.connect(
                        lambda _checked=False, fn=callback: self._run(fn))
                    row.addWidget(button)
                row.addStretch()
                column.addLayout(row)

            root.addLayout(column, 1)

            close = QtWidgets.QPushButton("\u2715")
            close.setFixedSize(20, 20)
            close.setCursor(QtCore.Qt.PointingHandCursor)
            close.setToolTip("Dismiss")
            close.clicked.connect(self.dismiss)
            root.addWidget(close, 0, QtCore.Qt.AlignTop)

            # A progress line that visibly runs down, so the popup says how
            # long it still has instead of vanishing without warning.
            self._bar = QtWidgets.QProgressBar(self)
            self._bar.setTextVisible(False)
            self._bar.setFixedHeight(3)
            self._bar.setRange(0, 100)
            self._bar.setValue(100)
            self._bar.setStyleSheet(
                "QProgressBar { border: none; background: %(panel)s; }"
                " QProgressBar::chunk { background: %(role)s; }"
                % {"panel": palette["panel"],
                   "role": palette.get(role, palette["accent"])})

            self._remaining = float(max(1, seconds))
            self._elapsed = 0.0
            self._timer = QtCore.QTimer(self)
            self._timer.setInterval(100)
            self._timer.timeout.connect(self._tick)
            self._timer.start()

        # -- lifetime -----------------------------------------------------
        def _tick(self):
            self._elapsed += 0.1
            left = max(0.0, self._remaining - self._elapsed)
            self._bar.setValue(int(100 * left / self._remaining))
            if left <= 0:
                self.dismiss()

        def freeze(self):
            """Stop the countdown: a popup with buttons waits for an answer."""
            self._timer.stop()
            self._bar.setValue(100)

        def _run(self, callback):
            try:
                callback()
            finally:
                self.dismiss()

        def dismiss(self):
            if self._closing:
                return
            self._closing = True
            self._timer.stop()
            if callable(self._on_close):
                try:
                    self._on_close()
                except Exception:
                    pass
            self.notifier.forget(self)
            self.hide()
            self.close()

        def enterEvent(self, event):
            """Hovering pauses the countdown so a popup can be read."""
            self._timer.stop()
            super().enterEvent(event)

        def leaveEvent(self, event):
            if not self._actions_hold():
                self._timer.start()
            super().leaveEvent(event)

        def _actions_hold(self):
            return bool(getattr(self, "_hold", False))

        def mousePressEvent(self, event):
            if event.button() == QtCore.Qt.LeftButton and self._on_click:
                self._on_click()
                self.dismiss()
            super().mousePressEvent(event)

        _on_click = None

    class Notifier(QtCore.QObject):
        """Posts popups and keeps them stacked in one corner of the screen."""

        def __init__(self, parent=None, corner=CORNER_BOTTOM_RIGHT):
            super().__init__(parent)
            self.corner = corner
            self.toasts = []
            self.entries = []            # the history, newest first
            self.limit = 200
            self.enabled = True

        # -- posting ------------------------------------------------------
        def post(self, title, body="", kind="info", seconds=None,
                 actions=None, on_click=None, parent_widget=None):
            """Show a popup. Returns the toast, or None when none was shown."""
            if not self.enabled:
                self.remember(title, body, kind, shown=False)
                return None
            app = QtWidgets.QApplication.instance()
            if app is None:                       # head-less: no popup at all
                self.remember(title, body, kind, shown=False)
                return None
            seconds = DEFAULT_SECONDS if seconds is None else max(1, seconds)
            actions = list(actions or [])
            try:
                toast = Toast(self, title, body, kind, seconds, actions,
                              on_close=None)
                toast._hold = bool(actions)
                toast._on_click = on_click
                if actions:
                    toast.freeze()
                self.toasts.append(toast)
                self._reposition()
                toast.show()
                self.remember(title, body, kind, shown=True)
                return toast
            except Exception:
                self.remember(title, body, kind, shown=False)
                return None

        def notify(self, title, body="", kind="info", seconds=None,
                   actions=None, on_click=None):
            """A reminder popup: same as post(), kept as a separate name."""
            return self.post(title, body, kind, seconds, actions, on_click)

        def remember(self, title, body, kind, shown):
            self.entries.insert(0, {
                "when": dt.datetime.now().replace(microsecond=0).isoformat(" "),
                "title": title, "body": body, "kind": kind, "shown": shown,
            })
            del self.entries[self.limit:]

        def clear_history(self):
            self.entries = []

        def forget(self, toast):
            """Drop a toast from the stack and re-flow the rest."""
            self.toasts = [item for item in self.toasts if item is not toast]
            self._reposition()

        def close_all(self):
            for toast in list(self.toasts):
                toast.dismiss()
            self.toasts = []

        # -- placement ----------------------------------------------------
        def _reposition(self):
            """Stack the popups in the chosen corner, newest nearest the edge."""
            app = QtWidgets.QApplication.instance()
            if app is None or not self.toasts:
                return
            screen = app.desktop().availableGeometry(
                self.toasts[-1])
            y = screen.top() + Toast.MARGIN
            for toast in reversed(self.toasts):
                toast.adjustSize()
                height = toast.sizeHint().height()
                if self.corner in (CORNER_TOP_RIGHT, CORNER_TOP_LEFT):
                    toast.move(self._x(screen, toast), y)
                    y += height + Toast.GAP
                else:
                    # Bottom corners stack upwards, so the newest is lowest.
                    y += height + Toast.GAP
            if self.corner in (CORNER_BOTTOM_RIGHT, CORNER_BOTTOM_LEFT):
                bottom = screen.bottom() - Toast.MARGIN
                for toast in reversed(self.toasts):
                    height = toast.sizeHint().height()
                    bottom -= height
                    toast.move(self._x(screen, toast), bottom)
                    bottom -= Toast.GAP

        def _x(self, screen, toast):
            if self.corner in (CORNER_TOP_RIGHT, CORNER_BOTTOM_RIGHT):
                return screen.right() - toast.width() - Toast.MARGIN
            return screen.left() + Toast.MARGIN


#: The notifier is process-wide: every tab posts to the same corner.
_INSTANCE = [None]


def notifier_for(window=None):
    # The one notifier for this program, created on first use.
    #
    # A notifier whose parent has been destroyed (the window was closed and a
    # new one built, as happens on a restart or in a test) is replaced rather
    # than reused, because touching a dead Qt object raises.
    current = _INSTANCE[0]
    if current is not None:
        try:
            parent = current.parent()
            if parent is not None and window is not None and parent is not window:
                current = None
        except RuntimeError:
            current = None                      # the C++ side is gone
    if current is None:
        _INSTANCE[0] = Notifier(window)
    elif window is not None and current.parent() is None:
        current.setParent(window)
    return _INSTANCE[0]
def reset_notifier():
    """Forget the shared notifier (used between tests)."""
    _INSTANCE[0] = None


def available():
    """True when popups can actually be shown."""
    return QtWidgets is not None \
        and QtWidgets.QApplication.instance() is not None
