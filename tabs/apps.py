
# -*- coding: utf-8 -*-
"""Personal application launcher tab for ScriptDB."""

import json
import os
import shlex
import subprocess
import time
import urllib.parse
import uuid

import app_paths
from PyQt5 import QtCore, QtGui, QtWidgets


class AppsTab(QtWidgets.QWidget):
    """A local, personalizable launcher with groups and popup folders."""

    DATA_FILE = "apps_db.json"
    SCHEMA_VERSION = 2
    RECENT_LIMIT = 10
    # Launch kinds the launcher knows about. "auto" is resolved from the path
    # at launch time (see _launch_kind_for).
    KINDS = (("auto", "Automatic"), ("exec", "Executable"),
             ("shell", "Shell command"), ("url", "URL / link"),
             ("folder", "Folder"), ("doc", "Document"),
             ("script", "ScriptDB script"))

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        # The launcher keeps its entries in the databases folder (db\).
        base = (getattr(window, "db_dir", None)
                or getattr(window, "DB_DIR", None)
                or app_paths.ensure_db_dir())
        self.data_path = os.path.join(base, self.DATA_FILE)
        self.apps = []
        self.groups = []
        self.view_mode = "grid"
        self.sort_key = "name"
        self.sort_desc = False
        self.icon_size = 64
        self.recent = []
        self.sort_menu = None        # built lazily, needs translated labels
        # Icon cache: building a QFileIconProvider per card per refresh is
        # needlessly expensive because refresh() runs on every resize.
        self._icon_cache = {}
        # The cards are rebuilt only when their content changes; a resize just
        # re-flows them into a new column count (see _relayout / resizeEvent).
        self._columns = 0
        self._load()
        self._build_ui()
        self.refresh()

    def t(self, key, fallback=None, **kwargs):
        try:
            return self.window.t(key, fallback, **kwargs)
        except Exception:
            text = fallback or key
            return text.format(**kwargs) if kwargs else text

    def _load(self):
        try:
            with open(self.data_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            self.apps = data.get("apps", []) if isinstance(data, dict) else []
            self.groups = data.get("groups", []) if isinstance(data, dict) else []
            self.view_mode = data.get("view", "grid") if isinstance(data, dict) else "grid"
            self.sort_key = data.get("sort", "name") if isinstance(data, dict) else "name"
            self.sort_desc = bool(data.get("sort_desc")) if isinstance(data, dict) else False
            self.icon_size = int(data.get("icon_size", 64)) if isinstance(data, dict) else 64
            recent = data.get("recent", []) if isinstance(data, dict) else []
            self.recent = [str(rid) for rid in recent
                           if isinstance(recent, list)][:self.RECENT_LIMIT]
        except (OSError, ValueError, TypeError):
            self.apps = []
            self.groups = []
            self.recent = []
        if not isinstance(self.apps, list):
            self.apps = []
        if not isinstance(self.groups, list):
            self.groups = []
        # Keep only usable entries: a hand-edited file must not crash the tab.
        self.apps = [app for app in self.apps if isinstance(app, dict)]
        self.groups = [group for group in self.groups if isinstance(group, dict)]
        for group in self.groups:
            group.setdefault("icon", "")
        self.recent = [rid for rid in self.recent
                       if any(app.get("id") == rid for app in self.apps)]
        self.icon_size = max(40, min(112, self.icon_size))
        if self.view_mode not in ("grid", "list"):
            self.view_mode = "grid"
        for app in self.apps:
            app.setdefault("id", uuid.uuid4().hex)
            app.setdefault("launch_count", 0)
            if not isinstance(app.get("launch_count"), int):
                app["launch_count"] = 0
            if app.get("kind") not in dict(self.KINDS):
                app["kind"] = "auto"

    def _save(self):
        payload = {"schema": self.SCHEMA_VERSION,
                   "apps": self.apps, "groups": self.groups,
                   "view": self.view_mode, "sort": self.sort_key,
                   "sort_desc": self.sort_desc,
                   "icon_size": self.icon_size,
                   "recent": self.recent}
        temporary = self.data_path + ".tmp"
        try:
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            os.replace(temporary, self.data_path)
        except OSError:
            try:
                os.remove(temporary)
            except OSError:
                pass

    def _build_ui(self):
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        header = QtWidgets.QHBoxLayout()
        self.title = QtWidgets.QLabel("APPS")
        self.title.setProperty("role", "title")
        header.addWidget(self.title)
        self.subtitle = QtWidgets.QLabel()
        self.subtitle.setProperty("role", "hint")
        header.addWidget(self.subtitle)
        header.addStretch()
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("Search apps...")
        self.search.setClearButtonEnabled(True)
        self.search.setMaximumWidth(260)
        self.search.textChanged.connect(self.refresh)
        header.addWidget(self.search)
        self.view_button = QtWidgets.QToolButton()
        self.view_button.setCheckable(True)
        self.view_button.clicked.connect(self.toggle_view)
        header.addWidget(self.view_button)
        # A single "Add" menu button: adding an app and adding a group are the
        # same gesture from the user's point of view, so they share one entry.
        self.add_button = QtWidgets.QToolButton()
        self.add_button.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.add_menu = QtWidgets.QMenu(self.add_button)
        self.act_add_app = self.add_menu.addAction("+ Add app", self.add_app)
        self.act_add_multi = self.add_menu.addAction("+ Add several...",
                                                     self.add_apps_many)
        self.act_add_group = self.add_menu.addAction("+ Group", self.add_group)
        self.add_button.setMenu(self.add_menu)
        header.addWidget(self.add_button)
        root.addLayout(header)

        controls = QtWidgets.QHBoxLayout()
        self.group_combo = QtWidgets.QComboBox()
        self.group_combo.currentIndexChanged.connect(self.refresh)
        controls.addWidget(self.group_combo, 1)
        self.sort_combo = QtWidgets.QComboBox()
        self.sort_combo.addItem(self.t("apps_sort_name", "Name"), "name")
        self.sort_combo.addItem(self.t("apps_sort_path", "Path"), "path")
        self.sort_combo.addItem(self.t("apps_sort_group", "Group"), "group")
        self.sort_combo.setCurrentIndex(max(0, self.sort_combo.findData(self.sort_key)))
        self.sort_combo.currentIndexChanged.connect(self.change_sort)
        controls.addWidget(self.sort_combo)
        self.sort_dir_button = QtWidgets.QToolButton()
        self.sort_dir_button.setCheckable(True)
        self.sort_dir_button.setToolTip("Sort descending")
        self.sort_dir_button.setText("\u2193")
        self.sort_dir_button.toggled.connect(self.set_sort_direction)
        controls.addWidget(self.sort_dir_button)
        self.folder_button = QtWidgets.QPushButton()
        self.folder_button.clicked.connect(self.open_selected_folder)
        controls.addWidget(self.folder_button)
        # The card view is read-only and launched by clicking; a dedicated
        # Launch button gives the same action a keyboard-reachable home.
        self.launch_button = QtWidgets.QPushButton()
        self.launch_button.clicked.connect(self.launch_selected)
        controls.addWidget(self.launch_button)
        self.size_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.size_slider.setRange(40, 112)
        self.size_slider.setValue(self.icon_size)
        # Persist the size once the user lets go of the handle.
        self.size_slider.sliderReleased.connect(self._save)
        self.size_slider.setFixedWidth(130)
        self.size_slider.valueChanged.connect(self.change_icon_size)
        controls.addWidget(self.size_slider)
        root.addLayout(controls)

        self.scroll = QtWidgets.QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.canvas = QtWidgets.QWidget()
        # Right-clicking the empty background opens the launcher menu (add app,
        # add group, view and sort) so the whole tab is reachable from the menu.
        self.canvas.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.canvas.customContextMenuRequested.connect(
            lambda point: self._show_context_menu(None))
        self.cards = QtWidgets.QGridLayout(self.canvas)
        self.cards.setAlignment(QtCore.Qt.AlignTop | QtCore.Qt.AlignLeft)
        self.cards.setSpacing(12)
        self.scroll.setWidget(self.canvas)
        root.addWidget(self.scroll, 1)
        self.status = QtWidgets.QLabel()
        self.status.setProperty("role", "hint")
        root.addWidget(self.status)

    def _icon(self, app):
        # Return the icon for an app entry, cached by (source, size). A custom
        # "icon" path (png/ico/anything QIcon can read) wins; next the
        # executable's own icon; finally a generic computer icon. Cached so
        # repeated rebuilds do no disk work for unchanged entries.
        custom = app.get("icon", "") if isinstance(app, dict) else ""
        path = app.get("path", "") if isinstance(app, dict) else str(app or "")
        source = custom if custom and os.path.exists(custom) else path
        key = (source, self.icon_size)
        cached = self._icon_cache.get(key)
        if cached is not None:
            return cached
        if source and os.path.exists(source):
            icon = QtWidgets.QFileIconProvider().icon(QtCore.QFileInfo(source))
        else:
            icon = self.style().standardIcon(QtWidgets.QStyle.SP_ComputerIcon)
        self._icon_cache[key] = icon
        return icon

    def _group_icon(self, group, size=16):
        """Return a group's custom icon, or a standard folder icon."""
        custom = group.get("icon", "") if isinstance(group, dict) else ""
        key = ("group", custom if custom and os.path.exists(custom) else "",
               size, bool(group.get("folder")) if isinstance(group, dict) else False)
        cached = self._icon_cache.get(key)
        if cached is not None:
            return cached
        if custom and os.path.exists(custom):
            icon = QtGui.QIcon(custom)
        else:
            standard = (QtWidgets.QStyle.SP_DirIcon if group.get("folder")
                        else QtWidgets.QStyle.SP_FileIcon)
            icon = self.style().standardIcon(standard)
        self._icon_cache[key] = icon
        return icon

    def _clear_cards(self):
        self._card_widgets = []
        while self.cards.count():
            item = self.cards.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

    def _relayout(self):
        # Re-flow the existing cards for the current width without rebuilding.
        # resizeEvent calls this instead of refresh(): the cards already exist,
        # only the number of grid columns changes, so there is no flicker and
        # no icon/disk work on a window drag.
        cards = getattr(self, "_card_widgets", None)
        if not cards:
            return
        columns = (1 if self.view_mode == "list"
                   else max(1, self.scroll.viewport().width() // 190))
        if columns == self._columns:
            return
        self._columns = columns
        # Re-adding a widget that is already in the layout simply moves it, so
        # there is no need to query item positions (indexOf can return -1 for a
        # widget Qt has not laid out yet, and getItemPosition(-1) crashes).
        for position, card in enumerate(cards):
            self.cards.addWidget(card, position // columns, position % columns)

    def _selected_group(self):
        return self.group_combo.currentData() or "all"

    def _visible_apps(self):
        # The apps passing the current group filter and search query.
        query = self.search.text().strip().lower()
        group_id = self._selected_group()
        entries = [app for app in self.apps
                   if (group_id == "all" or app.get("group") == group_id)
                   and (not query
                        or query in app.get("name", "").lower()
                        or query in app.get("path", "").lower()
                        or query in app.get("args", "").lower()
                        or query in app.get("tags", "").lower())]
        return entries
    def _sort_value(self, app, key):
        if key == "last_used":
            return int(app.get("last_used") or 0)
        if key == "launch_count":
            return int(app.get("launch_count") or 0)
        if key == "missing":
            # "Missing first" puts entries whose path no longer exists on top.
            return 0 if self._app_missing(app) else 1
        return str(app.get(key, "") or "").lower()

    def _ordered_apps(self, entries):
        # Sort the filtered entries. The favourites and the recent rows are laid
        # out separately, so this only decides the order inside each of those
        # rows and the order of the "All apps" remainder.
        entries = list(entries)
        entries.sort(key=lambda app: self._sort_value(app, self.sort_key),
                     reverse=bool(self.sort_desc))
        # Favourites first, then the rest, keeping the chosen sort inside each.
        return ([app for app in entries if app.get("favorite")]
                + [app for app in entries if not app.get("favorite")])

    def _group_names(self):
        return {group.get("id"): group.get("name", "")
                for group in self.groups}

    def _app_badges(self, app):
        """Badges shown on a card: missing path, folder group, tags."""
        badges = []
        if self._app_missing(app):
            badges.append("\u26a0 missing")
        group = next((item for item in self.groups
                      if item.get("id") == app.get("group")), None)
        if group and group.get("folder"):
            badges.append("\u25a3 " + group.get("name", ""))
        tags = (app.get("tags") or "") if isinstance(app, dict) else ""
        if tags:
            badges.append("#" + ", #".join(
                part.strip() for part in tags.replace(";", ",").split(",")
                if part.strip()))
        return "  ".join(badges)

    @staticmethod
    def _app_missing(app):
        """Whether a local, non-URL path of an app is missing on disk."""
        if not isinstance(app, dict):
            return False
        path = app.get("path", "") or ""
        if not path or path.startswith(("http://", "https://", "shell:")):
            return False
        if app.get("kind") in ("url", "shell", "script"):
            return False
        return not os.path.exists(path)

    def _recent_apps(self, entries):
        """Most recently launched apps, from the persisted id list."""
        by_id = {app.get("id"): app for app in entries}
        recent = [by_id[rid] for rid in self.recent if rid in by_id]
        return recent

    def _favorite_entries(self, entries):
        # Pinned apps, always shown first regardless of the sort key.
        return [app for app in entries if app.get("favorite")]

    def _recent_entries(self, entries, limit=5):
        # Apps launched with the count missing (the legacy data file) still
        # have a timestamp, so fall back to the most recently used ones.
        recent = self._recent_apps(entries)
        if not recent:
            recent = [app for app in entries if app.get("last_used")]
            recent.sort(key=lambda app: app.get("last_used", 0), reverse=True)
        return recent[:limit]

    def _section_label(self, text):
        label = QtWidgets.QLabel(text)
        label.setProperty("role", "hint")
        return label
    def refresh(self):
        if not hasattr(self, "cards"):
            return
        current = self._selected_group()
        self.group_combo.blockSignals(True)
        self.group_combo.clear()
        self.group_combo.addItem(self.t("apps_all", "All apps"), "all")
        for group in self.groups:
            self.group_combo.addItem(
                self._group_icon(group), group.get("name", ""), group.get("id"))
        index = self.group_combo.findData(current)
        self.group_combo.setCurrentIndex(max(0, index))
        self.group_combo.blockSignals(False)
        self._clear_cards()
        entries = self._ordered_apps(self._visible_apps())
        favorites = self._favorite_entries(entries)
        recents = [app for app in self._recent_entries(entries)
                   if not app.get("favorite")]
        eaten = set(id(app) for app in favorites) | set(id(app) for app in recents)
        rest = [app for app in entries if id(app) not in eaten]
        # Favourites and recent apps are shown in their own labelled rows so they
        # stay reachable no matter how long the main list grows.
        sequence = []
        if favorites:
            sequence.append(self._section_label(self.t("apps_favorites", "Favorites")))
            sequence.extend(favorites)
        if recents:
            sequence.append(self._section_label(self.t("apps_recent", "Recently used")))
            sequence.extend(recents)
        if (favorites or recents) and rest:
            sequence.append(self._section_label(self.t("apps_all", "All apps")))
        sequence.extend(rest)
        # Build one widget per item; the grid positions are assigned afterwards.
        widgets = []
        for item in sequence:
            widgets.append(item if isinstance(item, QtWidgets.QWidget)
                           else self._card(item))
        self._card_widgets = widgets
        self._columns = 0
        self._relayout()
        missing = sum(1 for app in entries if self._app_missing(app))
        status = self.t("apps_status", "{count} applications", count=len(entries))
        if missing:
            status += "  |  " + self.t("apps_status_missing",
                                       "{count} missing path(s)", count=missing)
        self.status.setText(status)
        self.view_button.setText(self.t("apps_list_view", "List view") if self.view_mode == "grid" else self.t("apps_grid_view", "Grid view"))
        selected = next((group for group in self.groups if group.get("id") == current), None)
        self.folder_button.setEnabled(bool(selected and selected.get("folder")))
        self.launch_button.setEnabled(bool(entries))

    def _build_app_menu(self, app):
        # Menu for one application: launch, edit, remove, folder, view, sort.
        menu = QtWidgets.QMenu(self)
        if app is not None:
            menu.addAction(self.t("apps_launch", "Launch"),
                           lambda: self.launch(app))
            menu.addAction(self.t("apps_edit", "Edit"),
                           lambda: self.edit_app(app))
            pin = menu.addAction(
                self.t("apps_unpin", "Remove from favorites") if app.get("favorite")
                else self.t("apps_pin", "Add to favorites"),
                lambda: self.toggle_favorite(app))
            pin.setCheckable(True)
            pin.setChecked(bool(app.get("favorite")))
            menu.addSeparator()
            move = menu.addMenu(self.t("apps_move_menu", "Send to..."))
            for group in self.groups:
                action = move.addAction(group.get("name", ""),
                                        lambda _c=False, g=group: self.move_to_group(app, g))
                action.setCheckable(True)
                action.setChecked(app.get("group") == group.get("id"))
            move.addSeparator()
            none_action = move.addAction(self.t("apps_no_group", "No group"),
                                        lambda: self.move_to_group(app, None))
            none_action.setCheckable(True)
            none_action.setChecked(not app.get("group"))
            menu.addSeparator()
            menu.addAction(self.t("apps_notes", "Notes..."),
                           lambda: self.edit_notes(app))
            menu.addAction(self.t("apps_use_as", "Use as..."),
                           lambda: self.use_as(app))
            menu.addSeparator()
            menu.addAction(self.t("apps_reveal_folder", "Open location"),
                           lambda: self.reveal_in_folder(app)).setEnabled(
                self._can_reveal(app))
            copy_path = menu.addAction(self.t("apps_copy_path", "Copy path"),
                                       lambda: QtWidgets.QApplication.clipboard().setText(app.get("path", "")))
            copy_path.setEnabled(bool(app.get("path")))
            menu.addSeparator()
            menu.addAction(self.t("apps_remove", "Remove"),
                           lambda: self.remove_app(app))
            menu.addSeparator()
        menu.addAction(self.t("apps_add", "+ Add app"), self.add_app)
        menu.addAction(self.t("apps_new_group", "+ Group"), self.add_group)
        current_group = self._selected_group()
        selected_group = next((group for group in self.groups
                               if group.get("id") == current_group), None)
        if app is None and selected_group is not None:
            menu.addAction(self.t("apps_edit_group", "Edit group"),
                           lambda: self.edit_group(selected_group))
        menu.addSeparator()
        menu.addMenu(self._view_menu(menu))
        menu.addMenu(self._sort_menu(menu))
        menu.addMenu(self._tools_menu(menu))
        menu.addSeparator()
        menu.addAction(self.t("apps_review", "Review all apps"), self.show_review)
        menu.addAction(self.t("context_refresh", "Refresh"), self.refresh)
        return menu
    @staticmethod
    def _can_reveal(app):
        """Whether the entry has a local path whose folder can be opened."""
        path = app.get("path", "") if isinstance(app, dict) else ""
        return bool(path) and not path.startswith(("http://", "https://")) \
            and not path.startswith("shell:") and app.get("kind") != "script"
    def _view_menu(self, parent):
        menu = QtWidgets.QMenu(self.t("apps_view_menu", "View"), parent)
        grid = menu.addAction(self.t("apps_grid_view", "Grid view"),
                              lambda: self.set_view("grid"))
        grid.setCheckable(True); grid.setChecked(self.view_mode == "grid")
        listing = menu.addAction(self.t("apps_list_view", "List view"),
                                 lambda: self.set_view("list"))
        listing.setCheckable(True); listing.setChecked(self.view_mode == "list")
        menu.addSeparator()
        menu.addAction(self.t("apps_icon_small", "Small icons"),
                       lambda: self.set_icon_size(48))
        menu.addAction(self.t("apps_icon_medium", "Medium icons"),
                       lambda: self.set_icon_size(64))
        menu.addAction(self.t("apps_icon_large", "Large icons"),
                       lambda: self.set_icon_size(96))
        return menu
    def _sort_menu(self, parent=None):
        # Built lazily: the labels come from the translation table, which is
        # only complete once retranslate() has run at least once.
        menu = QtWidgets.QMenu(self.t("apps_sort_menu", "Sort by"), parent or self.add_button)
        self.sort_menu = menu
        for key, label in (("name", self.t("apps_sort_name", "Name")),
                           ("path", self.t("apps_sort_path", "Path")),
                           ("group", self.t("apps_sort_group", "Group")),
                           ("launch_count", self.t("apps_sort_usage", "Most used")),
                           ("last_used", self.t("apps_sort_recent", "Last used")),
                           ("missing", self.t("apps_sort_missing", "Missing path"))):
            action = menu.addAction(label, lambda k=key: self.set_sort(k))
            action.setCheckable(True)
            action.setChecked(self.sort_key == key)
        menu.addSeparator()
        descending = menu.addAction(self.t("apps_sort_desc", "Descending"),
                                    lambda: self.set_sort_direction(
                                        not self.sort_dir_button.isChecked()))
        descending.setCheckable(True)
        descending.setChecked(bool(self.sort_desc))
        return menu
    def _tools_menu(self, parent=None):
        """Import / export and the folder shortcuts, used by both menus."""
        menu = QtWidgets.QMenu(self.t("apps_tools_menu", "Launcher"), parent or self.add_button)
        menu.addAction(self.t("apps_export", "Export apps..."), self.export_apps)
        menu.addAction(self.t("apps_import", "Import apps..."), self.import_apps)
        menu.addSeparator()
        menu.addAction(self.t("apps_open_dir", "Open launcher folder"),
                       self.open_launcher_folder)
        menu.addAction(self.t("apps_check_paths", "Check all paths"), self.check_paths)
        menu.addSeparator()
        danger = self.t("apps_danger_menu", "Remove...")
        submenu = menu.addMenu(danger)
        submenu.addAction(self.t("apps_clear_broken", "Remove broken entries"),
                          self.remove_broken)
        submenu.addAction(self.t("apps_clear_all", "Remove everything"),
                          self.clear_all)
        return menu
    def _show_context_menu(self, app, global_pos=None):
        menu = self._build_app_menu(app)
        menu.exec_(global_pos or QtGui.QCursor.pos())

    def _card(self, app):
        card = QtWidgets.QFrame()
        card.setFrameShape(QtWidgets.QFrame.StyledPanel)
        card.setMinimumSize(172, 132 if self.view_mode == "grid" else 72)
        card.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        card.customContextMenuRequested.connect(
            lambda point, item=app: self._show_context_menu(
                item, card.mapToGlobal(point)))
        layout = QtWidgets.QHBoxLayout(card) if self.view_mode == "list" else QtWidgets.QVBoxLayout(card)
        layout.setContentsMargins(12, 10, 12, 10)

        icon = QtWidgets.QToolButton()
        icon.setIcon(self._icon(app))
        icon.setIconSize(QtCore.QSize(self.icon_size, self.icon_size))
        tip = app.get("path", "")
        if app.get("args"):
            tip = "%s\n%s" % (tip, app["args"])
        if app.get("last_used"):
            tip = "%s\n%s" % (tip, self.t("apps_last_used", "Last used: {when}",
                                            when=time.strftime(
                                                "%Y-%m-%d %H:%M",
                                                time.localtime(app["last_used"]))))
        icon.setToolTip(tip)
        icon.clicked.connect(lambda: self.launch(app))
        icon.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        icon.customContextMenuRequested.connect(
            lambda point, item=app: self._show_context_menu(
                item, icon.mapToGlobal(point)))
        layout.addWidget(icon, 0, QtCore.Qt.AlignCenter)
        caption = ("\u2605 " if app.get("favorite") else "") + app.get("name", "Unnamed")
        text = QtWidgets.QLabel(caption)
        text.setWordWrap(True)
        text.setAlignment(QtCore.Qt.AlignCenter if self.view_mode == "grid" else QtCore.Qt.AlignVCenter)
        layout.addWidget(text, 1)
        # A short badge line (missing path, folder group, tags) turns the tile
        # into something readable when the icons are small.
        badge_text = self._app_badges(app)
        if badge_text and self.view_mode == "grid":
            badge = QtWidgets.QLabel(badge_text)
            badge.setProperty("role", "hint")
            badge.setWordWrap(True)
            badge.setAlignment(QtCore.Qt.AlignCenter)
            if self._app_missing(app):
                badge.setStyleSheet("color: #f7c66d;")
            layout.addWidget(badge, 0)
        menu = QtWidgets.QToolButton()
        menu.setText("...")
        menu.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        menu.setMenu(self._build_app_menu(app))
        layout.addWidget(menu, 0, QtCore.Qt.AlignRight if self.view_mode == "list" else QtCore.Qt.AlignCenter)
        return card

    def _dialog_fields(self, app=None):
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle(self.t("apps_add_title", "Add application"))
        form = QtWidgets.QFormLayout(dialog)
        name = QtWidgets.QLineEdit((app or {}).get("name", ""))
        path = QtWidgets.QLineEdit((app or {}).get("path", ""))
        browse = QtWidgets.QPushButton(self.t("apps_browse", "Browse..."))
        browse.clicked.connect(lambda: path.setText(QtWidgets.QFileDialog.getOpenFileName(dialog, self.t("apps_browse", "Browse..."))[0] or path.text()))
        path_row = QtWidgets.QHBoxLayout(); path_row.addWidget(path); path_row.addWidget(browse)
        args = QtWidgets.QLineEdit((app or {}).get("args", ""))
        # Optional custom icon: any file QIcon can read (png/ico/...).
        icon_edit = QtWidgets.QLineEdit((app or {}).get("icon", ""))
        icon_browse = QtWidgets.QPushButton(self.t("apps_browse", "Browse..."))
        icon_browse.clicked.connect(lambda: icon_edit.setText(QtWidgets.QFileDialog.getOpenFileName(dialog, self.t("apps_icon", "Icon"), "", "Images (*.png *.ico *.jpg *.jpeg *.bmp *.svg)")[0] or icon_edit.text()))
        icon_row = QtWidgets.QHBoxLayout(); icon_row.addWidget(icon_edit); icon_row.addWidget(icon_browse)
        group = QtWidgets.QComboBox()
        group.addItem(self.t("apps_no_group", "No group"), "")
        for item in self.groups:
            group.addItem(item.get("name", ""), item.get("id"))
        group.setCurrentIndex(max(0, group.findData((app or {}).get("group", ""))))
        # The launch kind decides how the entry is started; "auto" keeps the
        # original behaviour (path or URL), which is right for almost everyone.
        kind = QtWidgets.QComboBox()
        for key, label in self.KINDS:
            kind.addItem(self.t("apps_kind_" + key, label), key)
        kind.setCurrentIndex(max(0, kind.findData((app or {}).get("kind", "auto"))))
        tags = QtWidgets.QLineEdit((app or {}).get("tags", ""))
        tags.setPlaceholderText(self.t("apps_tags_hint", "work, tools, games"))
        hotkey = QtWidgets.QKeySequenceEdit(QtGui.QKeySequence(
            (app or {}).get("hotkey", "")))
        hotkey.setToolTip(self.t(
            "apps_hotkey_hint",
            "Launch this app from anywhere with this shortcut"))
        favorite = QtWidgets.QCheckBox()
        favorite.setChecked(bool((app or {}).get("favorite")))
        form.addRow(self.t("apps_name", "Name"), name)
        form.addRow(self.t("apps_path", "Application"), path_row)
        form.addRow(self.t("apps_args", "Arguments"), args)
        form.addRow(self.t("apps_kind", "Type"), kind)
        form.addRow(self.t("apps_icon", "Icon"), icon_row)
        form.addRow(self.t("apps_group", "Group"), group)
        form.addRow(self.t("apps_tags", "Tags"), tags)
        form.addRow(self.t("apps_hotkey", "Shortcut"), hotkey)
        form.addRow(self.t("apps_pin", "Add to favorites"), favorite)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept); buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec_() != QtWidgets.QDialog.Accepted or not name.text().strip() or not path.text().strip():
            return None
        target = path.text().strip()
        # Warn about a path that does not exist, but still allow saving (network
        # shares, URLs, launchers that resolve later).  A typo otherwise becomes
        # a silently dead entry.
        chosen_kind = kind.currentData() or "auto"
        if chosen_kind in ("auto", "exec", "folder", "doc") \
                and not target.startswith(("http://", "https://")) \
                and not os.path.exists(target):
            if QtWidgets.QMessageBox.warning(
                    self, self.t("apps_missing_path", "Path not found"),
                    self.t("apps_missing_path_body",
                           "{path}\n\nSave anyway?", path=target),
                    QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No) != QtWidgets.QMessageBox.Yes:
                return None
        return {"name": name.text().strip(), "path": target,
                "args": args.text().strip(),
                "icon": icon_edit.text().strip(),
                "kind": chosen_kind,
                "tags": tags.text().strip(),
                "hotkey": hotkey.keySequence().toString(),
                "favorite": favorite.isChecked(),
                "group": group.currentData() or ""}

    def add_app(self):
        data = self._dialog_fields()
        if data:
            data["id"] = uuid.uuid4().hex
            self.apps.append(data); self._save(); self.refresh()

    def edit_app(self, app):
        data = self._dialog_fields(app)
        if data:
            app.update(data); self._save(); self.refresh()

    def remove_app(self, app):
        if QtWidgets.QMessageBox.question(self, self.t("apps_remove", "Remove"), app.get("name", "")) == QtWidgets.QMessageBox.Yes:
            self.apps.remove(app); self._save(); self.refresh()

    def toggle_favorite(self, app):
        app["favorite"] = not app.get("favorite")
        self._save(); self.refresh()

    def add_apps_many(self):
        """Add several applications at once by picking more than one file."""
        paths, _filter = QtWidgets.QFileDialog.getOpenFileNames(
            self, self.t("apps_add_multi", "Add several applications"), "",
            "Programs (*.exe *.bat *.cmd *.lnk *.app *.desktop *.sh *.ps1);;All files (*)")
        if not paths:
            return
        group_id = self._selected_group()
        if group_id == "all":
            group_id = ""
        existing = {app.get("path") for app in self.apps}
        added = 0
        for path in paths:
            if path in existing:
                continue
            entry = {"id": uuid.uuid4().hex, "name": self._suggest_name(path),
                     "path": path, "args": "", "icon": "", "kind": "auto",
                     "tags": "", "hotkey": "", "favorite": False,
                     "group": group_id, "launch_count": 0}
            self.apps.append(entry)
            added += 1
        if added:
            self._save(); self.refresh()
            self.status.setText(self.t("apps_added_many",
                                       "added {count} application(s)", count=added))

    @staticmethod
    def _suggest_name(path):
        """A readable default name for an imported application path."""
        cleaned = (path or "").rstrip("/\\")
        stem = os.path.splitext(os.path.basename(cleaned))[0]
        return stem or os.path.basename(cleaned) or path
    def move_to_group(self, app, group):
        """Put an app in a group (or in no group when group is None)."""
        app["group"] = group.get("id", "") if group else ""
        self._save(); self.refresh()

    def edit_notes(self, app):
        """Edit the free-form notes of one application."""
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle(self.t("apps_notes", "Notes...") + " - " + app.get("name", ""))
        dialog.resize(460, 320)
        layout = QtWidgets.QVBoxLayout(dialog)
        notes = QtWidgets.QPlainTextEdit(app.get("notes", ""))
        notes.setPlaceholderText(self.t("apps_notes_hint",
                                        "Where to find it, licence, account, password hint..."))
        layout.addWidget(notes)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec_() == QtWidgets.QDialog.Accepted:
            app["notes"] = notes.toPlainText()
            self._save(); self.refresh()

    def use_as(self, app):
        """The "Use as..." menu: feed this entry into the other ScriptDB tabs."""
        menu = QtWidgets.QMenu(self)
        path = app.get("path", "")
        text = bool(path) and not path.startswith(("http://", "https://", "shell:"))
        add_file = menu.addAction(self.t("apps_use_diff_a", "Compare its file in the Diff tab"),
                                  lambda: self.send_to_diff(app, left=True))
        add_other = menu.addAction(self.t("apps_use_diff_b", "Compare it against a file..."),
                                   lambda: self.send_to_diff(app, left=False))
        add_file.setEnabled(text)
        add_other.setEnabled(text)
        add_doc = menu.addAction(self.t("apps_use_docs", "Store its content in DOCs"),
                                 lambda: self.send_to_documents(app))
        add_doc.setEnabled(text)
        add_search = menu.addAction(self.t("apps_use_search", "Search its name in the Search tab"),
                                    lambda: self.search_app(app))
        add_search.setEnabled(True)
        menu.exec_(QtGui.QCursor.pos())

    def send_to_diff(self, app, left=True):
        """Load an application's file into the Diff tab for comparison."""
        diff = getattr(self.window, "diff_tab", None)
        if diff is None or not hasattr(diff, "_read_source"):
            return
        source = diff._read_source(app.get("path", ""), "left" if left else "right")
        if left:
            diff._left = source
        else:
            diff._right = source
        diff.recompute()
        tabs = getattr(self.window, "tabs", None)
        if tabs is not None:
            tabs.setCurrentWidget(diff)

    def send_to_documents(self, app):
        """Import the file behind an app into the DOCs repository."""
        docs = getattr(self.window, "docs_tab", None)
        path = app.get("path", "")
        if docs is None or not path or not os.path.isfile(path):
            self.status.setText(self.t("apps_into_docs_failed",
                                       "DOCs is unavailable for this entry"))
            return
        try:
            added = docs.import_paths([path])
        except Exception:
            added = 0
        tabs = getattr(self.window, "tabs", None)
        if added and tabs is not None:
            tabs.setCurrentWidget(docs)
        self.status.setText(self.t("apps_into_docs",
                                   "imported into DOCs: {count}", count=added))

    def search_app(self, app):
        """Hand the entry's name to the Search tab as the query."""
        search = getattr(self.window, "search_tab", None)
        tabs = getattr(self.window, "tabs", None)
        if search is None:
            return
        for attribute in ("query_edit", "search_edit", "filter_edit"):
            widget = getattr(search, attribute, None)
            if widget is not None and hasattr(widget, "setText"):
                widget.setText(app.get("name", ""))
                break
        if tabs is not None:
            tabs.setCurrentWidget(search)

    def notes_available(self, app):
        # Whether a note exists for an entry (used to mark the menu entry).
        return bool((app or {}).get("notes"))

    def reveal_in_folder(self, app):
        # Open the containing folder of the application in the file manager.
        path = app.get("path", "")
        if not path or path.startswith(("http://", "https://", "shell:")):
            return
        if not os.path.exists(path):
            self.status.setText(self.t("apps_missing_path", "Path not found"))
            return
        if os.path.isdir(path):
            folder = path
        else:
            folder = os.path.dirname(path) or os.path.dirname(self.data_path)
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(folder))

    def set_view(self, mode):
        if mode in ("grid", "list") and mode != self.view_mode:
            self.view_mode = mode
            self._save(); self.refresh()

    def set_sort(self, key):
        if key and key != self.sort_key:
            self.sort_key = key
            index = self.sort_combo.findData(key)
            if index >= 0:
                self.sort_combo.blockSignals(True)
                self.sort_combo.setCurrentIndex(index)
                self.sort_combo.blockSignals(False)
            self._save(); self.refresh()

    def set_icon_size(self, value):
        value = max(40, min(112, int(value)))
        if value != self.icon_size:
            self.icon_size = value
            self.size_slider.blockSignals(True)
            self.size_slider.setValue(value)
            self.size_slider.blockSignals(False)
            self._save(); self.refresh()

    def set_sort_direction(self, descending):
        """Flip the sort order (A-Z / Z-A, most used / least used)."""
        self.sort_desc = bool(descending)
        self.sort_dir_button.blockSignals(True)
        self.sort_dir_button.setChecked(self.sort_desc)
        self.sort_dir_button.setText("\u2193" if self.sort_desc else "\u2191")
        self.sort_dir_button.blockSignals(False)
        self._save(); self.refresh()

    def _group_dialog(self, group=None):
        """Create or edit a group, including its optional custom icon."""
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle(self.t("apps_group", "Group"))
        form = QtWidgets.QFormLayout(dialog)
        name = QtWidgets.QLineEdit((group or {}).get("name", ""))
        icon_edit = QtWidgets.QLineEdit((group or {}).get("icon", ""))
        browse = QtWidgets.QPushButton(self.t("apps_browse", "Browse..."))
        browse.clicked.connect(lambda: icon_edit.setText(
            QtWidgets.QFileDialog.getOpenFileName(
                dialog, self.t("apps_icon", "Icon"), "",
                "Images (*.png *.ico *.jpg *.jpeg *.bmp *.svg)")[0]
            or icon_edit.text()))
        icon_row = QtWidgets.QHBoxLayout()
        icon_row.addWidget(icon_edit)
        icon_row.addWidget(browse)
        folder = QtWidgets.QCheckBox(
            self.t("apps_folder_question", "Open as popup folder?"))
        folder.setChecked(bool((group or {}).get("folder")))
        form.addRow(self.t("apps_group_name", "Group name:"), name)
        form.addRow(self.t("apps_icon", "Icon"), icon_row)
        form.addRow(self.t("apps_group", "Folder"), folder)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec_() != QtWidgets.QDialog.Accepted or not name.text().strip():
            return None
        return {"name": name.text().strip(), "icon": icon_edit.text().strip(),
                "folder": folder.isChecked()}

    def add_group(self):
        data = self._group_dialog()
        if data:
            data["id"] = uuid.uuid4().hex
            self.groups.append(data)
            self._save()
            self.refresh()

    def edit_group(self, group):
        data = self._group_dialog(group)
        if data:
            group.update(data)
            self._icon_cache.clear()
            self._save()
            self.refresh()

    def toggle_view(self):
        self.view_mode = "list" if self.view_mode == "grid" else "grid"
        self._save(); self.refresh()

    def change_sort(self):
        self.sort_key = self.sort_combo.currentData() or "name"
        self._save(); self.refresh()

    def open_selected_folder(self):
        group_id = self._selected_group()
        group = next((item for item in self.groups if item.get("id") == group_id), None)
        if group:
            self.show_folder(group)

    def change_icon_size(self, value):
        # Dragging the slider fires this on every step; only update the live
        # size and re-flow, and persist once the handle is released rather
        # than rewriting the whole JSON file dozens of times per drag.
        self.icon_size = value
        self._relayout_for_icons()
        if not self.size_slider.isSliderDown():
            self._save()

    def _relayout_for_icons(self):
        # Icons changed size, so every card needs redrawing (not just re-flow).
        self.refresh()

    def launch_selected(self):
        """Launch the first app that the current filter shows."""
        selected = self._selected_group()
        entries = self._ordered_apps(self._visible_apps())
        if not entries:
            self.status.setText(self.t("apps_none", "no applications to launch"))
            return
        target = entries[0]
        # Inside a group the first card is the most relevant one; outside it,
        # the first favourite is a better guess than whatever sorts first.
        if selected == "all":
            favorites = [app for app in entries if app.get("favorite")]
            if favorites:
                target = favorites[0]
        self.launch(target)

    def launch(self, app):
        path = app.get("path", "")
        kind = app.get("kind", "auto")
        try:
            if kind == "script":
                self._launch_script(app)
            elif kind == "shell":
                self._launch_shell(app)
            elif kind == "folder":
                QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(path))
            elif kind == "doc":
                QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(path))
            elif path.startswith(("http://", "https://")):
                QtGui.QDesktopServices.openUrl(QtCore.QUrl(path))
            else:
                subprocess.Popen([path] + shlex.split(app.get("args", ""), posix=False), cwd=os.path.dirname(path) or None)
        except (OSError, ValueError) as exc:
            QtWidgets.QMessageBox.warning(self, self.t("apps_error", "Launcher error"), str(exc))
            return
        # Record the launch time and count so the "Recently used" and "Most
        # used" orders stay current.
        app["last_used"] = int(time.time())
        app["launch_count"] = int(app.get("launch_count") or 0) + 1
        self._remember_recent(app)
        self._save()
        self.refresh()

    def _remember_recent(self, app):
        """Move an app to the front of the persisted recent list."""
        app_id = app.get("id")
        if not app_id:
            return
        self.recent = [rid for rid in self.recent if rid != app_id]
        self.recent.insert(0, app_id)
        del self.recent[self.RECENT_LIMIT:]

    def _launch_shell(self, app):
        """Run the entry as a shell command line (shell=True on purpose)."""
        command = (app.get("path", "") + " " + app.get("args", "")).strip()
        if not command:
            raise ValueError(self.t("apps_empty_command", "empty command"))
        subprocess.Popen(command, shell=True,
                         cwd=os.path.dirname(self.data_path) or None)

    def _launch_script(self, app):
        """
        Run one of the ScriptDB scripts through the existing runner.

        Nothing about script execution is duplicated here: the entry only
        points at a script, and the Scripts tab's own runner does the work.
        """
        script = None
        finder = getattr(self.window, "find_script", None)
        if callable(finder) and app.get("script_id"):
            script = finder(app.get("script_id"))
        if script is None:
            # No linked script (or it was deleted): fall back to a name match
            # so a renamed script still launches instead of failing silently.
            name = (app.get("script_name") or app.get("name") or "").lower()
            scripts = getattr(self.window, "scripts_only", None)
            for candidate in (scripts() if callable(scripts) else []):
                if (candidate.get("name", "") or "").lower() == name:
                    script = candidate
                    break
        if script is None:
            raise ValueError(self.t(
                "apps_no_script", "no ScriptDB script is linked to this entry"))
        runner = getattr(self.window, "run_script_entry", None)
        if callable(runner):
            runner(script)
            return
        # Without a dedicated entry point, open the Scripts tab with the script
        # selected; the user then presses Run exactly as they normally would.
        selector = getattr(self.window, "select_script_by_id", None)
        if callable(selector):
            selector(script.get("id"))
        tabs = getattr(self.window, "tabs", None)
        list_tab = getattr(self.window, "list_tab", None)
        if tabs is not None and list_tab is not None:
            tabs.setCurrentWidget(list_tab)
        raiser = getattr(self.window, "run_selected_script", None)
        if callable(raiser):
            raiser()

    # ------------------------------------------------------ library I/O
    def export_apps(self):
        """Export the whole launcher (apps, groups, order) as one JSON file."""
        path, _filter = QtWidgets.QFileDialog.getSaveFileName(
            self, self.t("apps_export", "Export apps..."),
            "apps_export.json", "JSON files (*.json)")
        if not path:
            return ""
        payload = {"schema": self.SCHEMA_VERSION, "apps": self.apps,
                   "groups": self.groups, "recent": self.recent}
        try:
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
        except OSError as exc:
            QtWidgets.QMessageBox.warning(
                self, self.t("apps_error", "Launcher error"), str(exc))
            return ""
        self.status.setText(self.t("apps_exported", "exported: {path}", path=path))
        return path

    def import_apps(self):
        """Merge a previously exported launcher file into this one.

        Entries that are already there (same id, or same name and path) are
        skipped, so importing twice never duplicates the list. Groups are
        matched by name instead of by id, so an import does not create a second
        group with the same label.
        """
        path, _filter = QtWidgets.QFileDialog.getOpenFileName(
            self, self.t("apps_import", "Import apps..."), "", "JSON files (*.json)")
        if not path:
            return 0
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError) as exc:
            QtWidgets.QMessageBox.warning(
                self, self.t("apps_error", "Launcher error"), str(exc))
            return 0
        apps = data.get("apps") if isinstance(data, dict) else data
        groups = data.get("groups", []) if isinstance(data, dict) else []
        if not isinstance(apps, list):
            return 0
        by_name = {group.get("name", ""): group.get("id")
                   for group in self.groups}
        for group in groups or []:
            if isinstance(group, dict) and group.get("name") \
                    and group.get("name") not in by_name:
                fresh = {"id": uuid.uuid4().hex, "name": group.get("name"),
                         "icon": group.get("icon", ""),
                         "folder": bool(group.get("folder"))}
                self.groups.append(fresh)
                by_name[fresh["name"]] = fresh["id"]
        known_ids = {app.get("id") for app in self.apps}
        known_pairs = {(app.get("name"), app.get("path")) for app in self.apps}
        # Which imported group id maps onto which group name, so the entries
        # keep their grouping across two independently created libraries.
        old_names = {group.get("id"): group.get("name")
                     for group in (groups or []) if isinstance(group, dict)}
        added = 0
        for entry in apps:
            if not isinstance(entry, dict) or not entry.get("name"):
                continue
            pair = (entry.get("name"), entry.get("path"))
            if entry.get("id") in known_ids or pair in known_pairs:
                continue
            clone = dict(entry)
            clone["id"] = uuid.uuid4().hex
            clone.setdefault("favorite", False)
            clone.setdefault("launch_count", 0)
            clone["group"] = by_name.get(old_names.get(entry.get("group")), "")
            self.apps.append(clone)
            known_ids.add(clone["id"])
            known_pairs.add(pair)
            added += 1
        self._save(); self.refresh()
        self.status.setText(self.t("apps_imported",
                                   "imported {count} application(s)", count=added))
        return added

    def open_launcher_folder(self):
        """Open the folder holding the launcher's data file."""
        folder = os.path.dirname(self.data_path) or os.getcwd()
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(folder))

    # --------------------------------------------------- batch operations
    def check_paths(self):
        """Report every entry whose local path can no longer be found."""
        missing = [app for app in self.apps if self._app_missing(app)]
        title = self.t("apps_check_paths", "Check all paths")
        if not missing:
            text = self.t("apps_all_ok", "every path exists")
            self.status.setText(text)
            QtWidgets.QMessageBox.information(self, title, text)
            return []
        names = chr(10).join(
            "\u2022 %s  (%s)" % (app.get("name", ""), app.get("path", ""))
            for app in missing[:20])
        if len(missing) > 20:
            names += chr(10) + "..."
        self.status.setText(self.t("apps_status_missing",
                                   "{count} missing path(s)", count=len(missing)))
        QtWidgets.QMessageBox.warning(self, title, names)
        return missing

    def remove_broken(self):
        """Delete the entries whose local path no longer exists."""
        missing = [app for app in self.apps if self._app_missing(app)]
        if not missing:
            self.status.setText(self.t("apps_all_ok", "every path exists"))
            return 0
        if QtWidgets.QMessageBox.question(
                self, self.t("apps_clear_broken", "Remove broken entries"),
                self.t("apps_clear_broken_body",
                       "Remove {count} entr(ies) whose path is missing?",
                       count=len(missing))) != QtWidgets.QMessageBox.Yes:
            return 0
        doomed = set(id(app) for app in missing)
        self.apps = [app for app in self.apps if id(app) not in doomed]
        self.recent = [rid for rid in self.recent
                       if any(app.get("id") == rid for app in self.apps)]
        self._save(); self.refresh()
        self.status.setText(self.t("apps_cleared_broken",
                                   "removed {count} entr(ies)", count=len(missing)))
        return len(missing)

    def clear_all(self):
        """Empty the launcher (its groups, apps and recent history)."""
        if not self.apps and not self.groups:
            return 0
        if QtWidgets.QMessageBox.question(
                self, self.t("apps_clear_all", "Remove everything"),
                self.t("apps_clear_all_body",
                       "Remove all {apps} app(s) and {groups} group(s)?",
                       apps=len(self.apps), groups=len(self.groups))) \
                != QtWidgets.QMessageBox.Yes:
            return 0
        removed = len(self.apps)
        self.apps = []
        self.groups = []
        self.recent = []
        self._save(); self.refresh()
        self.status.setText(self.t("apps_cleared_all",
                                   "launcher emptied ({count})", count=removed))
        return removed

    def show_review(self):
        """An overview table: name, group, type, path state, usage and tags."""
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle(self.t("apps_review", "Review all apps"))
        dialog.resize(880, 540)
        layout = QtWidgets.QVBoxLayout(dialog)
        tree = QtWidgets.QTreeWidget()
        tree.setAlternatingRowColors(True)
        tree.setColumnCount(7)
        tree.setHeaderLabels([
            self.t("apps_name", "Name"), self.t("apps_group", "Group"),
            self.t("apps_kind", "Type"), self.t("apps_path", "Application"),
            self.t("apps_state", "State"), self.t("apps_usage", "Launches"),
            self.t("apps_tags", "Tags")])
        names = self._group_names()
        kinds = dict(self.KINDS)
        for app in self._ordered_apps(self.apps):
            state = (self.t("apps_missing_path", "Path not found")
                     if self._app_missing(app)
                     else self.t("apps_state_ok", "OK"))
            item = QtWidgets.QTreeWidgetItem([
                app.get("name", ""), names.get(app.get("group"), ""),
                self.t("apps_kind_" + app.get("kind", "auto"),
                       kinds.get(app.get("kind", "auto"), "Automatic")),
                app.get("path", ""), state,
                str(app.get("launch_count") or 0), app.get("tags", "")])
            if self._app_missing(app):
                item.setForeground(4, QtGui.QColor("#f7c66d"))
            tree.addTopLevelItem(item)
        layout.addWidget(tree)
        close = QtWidgets.QPushButton(self.t("apps_close", "Close"))
        close.clicked.connect(dialog.accept)
        footer = QtWidgets.QHBoxLayout()
        footer.addStretch(); footer.addWidget(close)
        layout.addLayout(footer)
        dialog.exec_()

    def install_hotkeys(self):
        """Register the per-app shortcuts declared in the data file.

        A shortcut that Qt cannot parse is skipped rather than allowed to break
        the whole launcher, and every previously installed one is removed first
        so changing a shortcut in the dialog does not leave the old one alive.
        """
        for shortcut in list(getattr(self, "_hotkeys", [])):
            try:
                shortcut.setEnabled(False)
                shortcut.deleteLater()
            except Exception:
                pass
        self._hotkeys = []
        for app in self.apps:
            sequence = (app.get("hotkey") or "").strip()
            if not sequence:
                continue
            try:
                shortcut = QtWidgets.QShortcut(QtGui.QKeySequence(sequence), self)
                shortcut.activated.connect(lambda item=app: self.launch(item))
            except Exception:
                continue
            self._hotkeys.append(shortcut)
        return len(self._hotkeys)
    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Only re-flow the existing cards into the new column count.  A full
        # refresh() here rebuilt every card (and re-read every icon) on each
        # pixel of a window drag, which flickered and stuttered.
        self._relayout()

    def retranslate(self, _table):
        self.title.setText(self.t("apps_title", "APPS"))
        self.subtitle.setText(self.t("apps_subtitle", "Your personal launchpad"))
        self.search.setPlaceholderText(self.t("apps_search", "Search apps..."))
        self.add_button.setText(self.t("apps_add", "+ Add app"))
        self.act_add_app.setText(self.t("apps_add", "+ Add app"))
        self.act_add_multi.setText(self.t("apps_add_multi", "+ Add several..."))
        self.act_add_group.setText(self.t("apps_new_group", "+ Group"))
        self.launch_button.setText(self.t("apps_launch", "Launch"))
        self.folder_button.setText(self.t("apps_open_folder", "Open folder"))
        self.sort_dir_button.setToolTip(self.t("apps_sort_dir", "Sort descending"))
        self.sort_combo.setItemText(0, self.t("apps_sort_name", "Name"))
        self.sort_combo.setItemText(1, self.t("apps_sort_path", "Path"))
        self.sort_combo.setItemText(2, self.t("apps_sort_group", "Group"))
        self.view_button.setText(self.t("apps_list_view", "List view") if self.view_mode == "grid" else self.t("apps_grid_view", "Grid view"))
        # The sort menu carries translated labels, so it is rebuilt here.
        self.sort_menu = None
        self.refresh()
        self.install_hotkeys()

    def show_folder(self, group):
        """A compact popup holding one group's apps, with a search box."""
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle(group.get("name", "Folder"))
        dialog.setWindowIcon(self._group_icon(group, 32))
        dialog.resize(420, 380)
        layout = QtWidgets.QVBoxLayout(dialog)
        finder = QtWidgets.QLineEdit()
        finder.setPlaceholderText(self.t("apps_search", "Search apps..."))
        finder.setClearButtonEnabled(True)
        layout.addWidget(finder)
        listing = QtWidgets.QListWidget()
        listing.setIconSize(QtCore.QSize(32, 32))
        members = [app for app in self.apps
                   if app.get("group") == group.get("id")]
        for app in self._ordered_apps(members):
            item = QtWidgets.QListWidgetItem(self._icon(app), app.get("name", ""))
            item.setData(QtCore.Qt.UserRole, app)
            if self._app_missing(app):
                item.setForeground(QtGui.QColor("#f7c66d"))
            listing.addItem(item)

        def filter_members(text):
            query = (text or "").strip().lower()
            for row in range(listing.count()):
                entry = listing.item(row).data(QtCore.Qt.UserRole) or {}
                haystack = (entry.get("name", "") + entry.get("tags", "")).lower()
                listing.item(row).setHidden(bool(query) and query not in haystack)
        finder.textChanged.connect(filter_members)

        def launch_current(row=None):
            item = listing.currentItem()
            if item is None:
                return
            entry = item.data(QtCore.Qt.UserRole)
            if entry is not None:
                self.launch(entry)
        listing.itemActivated.connect(lambda item: launch_current())
        listing.itemDoubleClicked.connect(lambda item: launch_current())
        layout.addWidget(listing, 1)
        run = QtWidgets.QPushButton(self.t("apps_launch", "Launch"))
        run.clicked.connect(launch_current)
        close = QtWidgets.QPushButton(self.t("apps_close", "Close"))
        close.clicked.connect(dialog.accept)
        footer = QtWidgets.QHBoxLayout()
        footer.addWidget(run)
        footer.addStretch()
        footer.addWidget(close)
        layout.addLayout(footer)
        dialog.exec_()
