# -*- coding: utf-8 -*-
"""The ToDo tab: a calendar, a task list and the reminders that join them.

This is the interface half of the feature; every rule (holidays, recurrence,
what is due, what should fire) lives in tabs/todo_core.py, so this file is
only about drawing and about asking the user what they meant.

The tab offers five views over the same tasks, reachable from one strip:

    Today      what is late, what is due today, what is in progress
    Day        one day in hours, with holidays off to the side
    Week       the seven days side by side, the daily driver
    Month      a real month grid, holidays and per-day counts
    Year       twelve months at a glance, for planning

Tasks are edited in a docked panel rather than a modal dialog, so a task can
be compared against the calendar while it is being written. The panel is also
where the developer extras live: an estimate, tags, links to scripts, and a
checklist.

Reminders are posted through tabs/todo_notify.py, which is shared with the
rest of the program, so a reminder appears wherever the user happens to be.
"""

import datetime as dt
import ast
import os
import uuid
import app_paths
from PyQt5 import QtCore, QtGui, QtWidgets
from . import todo_core as core
from . import todo_notify
from .shopping_tab import ShoppingTab

#: The colour a task is drawn with when it set none of its own.
PRIORITY_COLORS = {
    "critical": "#f7768e",
    "high": "#f7c66d",
    "normal": "#8ab4f8",
    "low": "#6e7681",
}

#: Holiday colour, kept apart from the task colours.
HOLIDAY_COLOR = "#c586c0"
WEEKEND_COLOR = "#6e7681"
TODAY_COLOR = "#9ece6a"


class TaskChip(QtWidgets.QFrame):
    """One task, drawn small enough to sit inside a calendar cell.

    A chip is not an editor: it shows the title, a status mark and its colour,
    and it forwards a double-click to the tab, which owns the editing panel.
    """

    def __init__(self, task, tab, compact=False):
        super().__init__(tab)
        self.task = task
        self.tab = tab
        self.compact = compact
        self.setObjectName("taskChip")
        self.setCursor(QtCore.Qt.PointingHandCursor)
        self.setToolTip(self._tooltip())

        colour = task.effective_color()
        background = self._blend(colour, 0.18)
        self.setStyleSheet(
            "QFrame#taskChip { background: %s; border: none;"
            " border-left: 3px solid %s; border-radius: 3px; }"
            " QFrame#taskChip:hover { background: %s; }"
            % (background, colour, self._blend(colour, 0.30)))

        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(5, 1, 4, 1)
        row.setSpacing(4)

        mark = "\u2713" if task.done else (
            "\u25cf" if task.get("status") == "doing" else "\u25a1")
        mark_label = QtWidgets.QLabel(mark)
        mark_label.setStyleSheet("color: %s; font-size: 10px;" % colour)
        row.addWidget(mark_label)

        title = task.title
        if compact and len(title) > 22:
            title = title[:21] + "\u2026"
        label = QtWidgets.QLabel(title)
        label.setStyleSheet(
            "color: %s; font-size: 11px;%s"
            % ("#9aa0a6" if task.done else "#dfe7f5",
               " text-decoration: line-through;" if task.done else ""))
        if not compact:
            label.setWordWrap(False)
        row.addWidget(label, 1)

        if not compact:
            extra = self._badges()
            if extra:
                badges = QtWidgets.QLabel(extra)
                badges.setStyleSheet("color: #9aa0a6; font-size: 10px;")
                row.addWidget(badges)

    @staticmethod
    def _blend(hex_colour, weight):
        """A flat background derived from the task colour."""
        try:
            value = hex_colour.lstrip("#")
            red, green, blue = (int(value[i:i + 2], 16) for i in (0, 2, 4))
        except (ValueError, IndexError):
            red, green, blue = (138, 180, 248)
        base = 30
        return "#%02x%02x%02x" % (
            int(base + (red - base) * weight),
            int(base + (green - base) * weight),
            int(base + (blue - base) * weight))

    def _badges(self):
        """Small markers: due time, blocked, repeat, checklist progress."""
        parts = []
        moment = self.task.due_moment()
        if moment is not None:
            parts.append(moment.strftime("%H:%M"))
        if self.task.get("status") == "blocked":
            parts.append("\u26d4")
        if self.task.get("repeat", "none") != "none":
            parts.append("\u21bb")
        done, total = self.task.checklist_progress()
        if total:
            parts.append("%d/%d" % (done, total))
        return "  ".join(parts)

    def _tooltip(self):
        task = self.task
        lines = [task.title]
        if task.get("due"):
            lines.append("Due: %s" % task.get("due"))
        if task.get("project"):
            lines.append("Project: %s" % task.get("project"))
        if task.tag_list():
            lines.append("Tags: %s" % ", ".join(task.tag_list()))
        if task.get("notes"):
            lines.append("")
            lines.append(task.get("notes")[:400])
        return "\n".join(lines)

    def mouseDoubleClickEvent(self, event):
        self.tab.open_task(self.task.id)
        event.accept()

    def contextMenuEvent(self, event):
        self.tab.show_task_menu(self.task, event.globalPos())
        event.accept()


class CalendarCell(QtWidgets.QFrame):
    """One day in a month grid: the date header and its task chips.
    """

    def __init__(self, tab, day, in_month=True):
        super().__init__(tab)
        self.tab = tab
        self.day = day
        self.in_month = in_month
        self.setObjectName("calendarCell")
        self.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.setMinimumSize(120, 96)

        today = dt.date.today()
        holiday = tab.calendar.label(day, tab.language())
        weekend = day.weekday() >= 5

        if day == today:
            border = TODAY_COLOR
        elif holiday:
            border = HOLIDAY_COLOR
        else:
            border = "palette(mid)"

        background = "palette(base)" if in_month else "palette(window)"
        self.setStyleSheet(
            "QFrame#calendarCell { background: %s; border: 1px solid %s;"
            " border-radius: 4px; }" % (background, border))

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(4, 3, 4, 3)
        layout.setSpacing(2)

        header = QtWidgets.QHBoxLayout()
        header.setSpacing(4)
        number = QtWidgets.QLabel(str(day.day))
        weight = "bold" if day == today else "normal"
        colour = TODAY_COLOR if day == today else (
            WEEKEND_COLOR if weekend and in_month else "palette(text)")
        number.setStyleSheet("font-weight: %s; color: %s;"
                             % (weight, colour if colour.startswith("#")
                                else ""))
        if not colour.startswith("#"):
            number.setProperty("role", "hint" if not in_month else "")
        header.addWidget(number)
        header.addStretch()
        if tab.show_week_numbers() and day.weekday() == 0:
            week = QtWidgets.QLabel("W%d" % core.iso_week(day))
            week.setStyleSheet("color: #6e7681; font-size: 9px;")
            header.addWidget(week)
        layout.addLayout(header)

        if holiday:
            label = QtWidgets.QLabel(holiday)
            label.setWordWrap(True)
            label.setStyleSheet("color: %s; font-size: 9px;" % HOLIDAY_COLOR)
            layout.addWidget(label)

        chips = QtWidgets.QVBoxLayout()
        chips.setSpacing(2)
        tasks = tab.tasks_on_day(day)
        for task in tasks[:tab.chips_per_day()]:
            chips.addWidget(TaskChip(task, tab, compact=True))
        hidden = len(tasks) - tab.chips_per_day()
        if hidden > 0:
            more = QtWidgets.QLabel("+%d more" % hidden)
            more.setStyleSheet("color: #9aa0a6; font-size: 9px;")
            chips.addWidget(more)
        chips.addStretch()
        layout.addLayout(chips)

    def mouseDoubleClickEvent(self, event):
        """Double-clicking empty space starts a task on that day."""
        self.tab.new_task_on(self.day)
        event.accept()


class DayColumn(QtWidgets.QFrame):
    """One day in the week view: a header and its tasks, grown to fill.
    """

    def __init__(self, tab, day):
        super().__init__(tab)
        self.tab = tab
        self.day = day
        self.setObjectName("dayColumn")
        self.setAcceptDrops(True)
        self.setFrameShape(QtWidgets.QFrame.NoFrame)

        today = dt.date.today()
        holiday = tab.calendar.label(day, tab.language())
        weekend = day.weekday() >= 5
        self.setStyleSheet(
            "QFrame#dayColumn { background: %s; border: 1px solid %s;"
            " border-radius: 4px; }"
            % ("palette(base)" if not weekend else "palette(window)",
               TODAY_COLOR if day == today else (
                   HOLIDAY_COLOR if holiday else "palette(mid)")))

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(5, 4, 5, 4)
        layout.setSpacing(3)

        title = QtWidgets.QLabel(
            "%s %d" % (core.day_name(day, tab.language())[:3], day.day))
        title.setStyleSheet(
            "font-weight: bold; color: %s;"
            % (TODAY_COLOR if day == today else "palette(text)"))
        layout.addWidget(title)

        month = QtWidgets.QLabel(core.month_name(day.year, day.month,
                                                 tab.language()))
        month.setStyleSheet("color: #9aa0a6; font-size: 10px;")
        layout.addWidget(month)

        if holiday:
            hol = QtWidgets.QLabel(holiday)
            hol.setWordWrap(True)
            hol.setStyleSheet("color: %s; font-size: 10px;" % HOLIDAY_COLOR)
            layout.addWidget(hol)

        if day == today:
            now = QtWidgets.QLabel(
                tab.t("todo_now_line", "now %s", when=dt.datetime.now().strftime("%H:%M")))
            now.setStyleSheet("color: %s; font-size: 10px;" % TODAY_COLOR)
            layout.addWidget(now)

        chips = QtWidgets.QVBoxLayout()
        chips.setSpacing(3)
        tasks = tab.tasks_on_day(day)
        for task in tasks:
            chips.addWidget(TaskChip(task, tab))
        if not tasks:
            empty = QtWidgets.QLabel(tab.t("todo_free_day", "nothing scheduled"))
            empty.setStyleSheet("color: #6e7681; font-size: 10px;")
            chips.addWidget(empty)
        chips.addStretch()
        layout.addLayout(chips)

    # -- drag a task onto another day ---------------------------------
    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat("application/x-scriptdb-task"):
            event.acceptProposedAction()

    def dropEvent(self, event):
        raw = bytes(event.mimeData().data("application/x-scriptdb-task"))
        task_id = raw.decode("utf-8", "replace")
        self.tab.move_task_to_day(task_id, self.day)
        event.acceptProposedAction()

    def mouseDoubleClickEvent(self, event):
        self.tab.new_task_on(self.day)
        event.accept()


class MonthView(QtWidgets.QWidget):
    """The month grid itself: seven columns, as many rows as the month needs.
    """

    def __init__(self, tab):
        super().__init__(tab)
        self.tab = tab
        self.grid = QtWidgets.QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(4)

    def rebuild(self):
        """(Re)draw the grid for the tabs current month."""
        while self.grid.count():
            item = self.grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        weekdays = core.week_days(self.tab.anchor, self.tab.start_monday())
        for column, day in enumerate(weekdays):
            label = QtWidgets.QLabel(core.day_name(day, self.tab.language()))
            label.setAlignment(QtCore.Qt.AlignCenter)
            label.setStyleSheet(
                "font-weight: bold; color: %s;"
                % (WEEKEND_COLOR if day.weekday() >= 5 else "palette(text)"))
            self.grid.addWidget(label, 0, column)

        weeks = core.month_grid(self.tab.anchor.year, self.tab.anchor.month,
                                self.tab.start_monday())
        if not self.tab.show_weekends():
            weeks = [self._weekdays_only(week) for week in weeks]
        for row, week in enumerate(weeks, start=1):
            for column, day in enumerate(week):
                cell = CalendarCell(self.tab, day,
                                    in_month=(day.month == self.tab.anchor.month))
                self.grid.addWidget(cell, row, column)
                self.grid.setRowStretch(row, 1)
        for column in range(7):
            self.grid.setColumnStretch(column, 1)

    @staticmethod
    def _weekdays_only(week):
        return [day for day in week if day.weekday() < 5]


class YearView(QtWidgets.QWidget):
    """Twelve small months with a per-day load indicator.
    """

    def __init__(self, tab):
        super().__init__(tab)
        self.tab = tab
        self.grid = QtWidgets.QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(10)

    def rebuild(self):
        while self.grid.count():
            item = self.grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        year = self.tab.anchor.year
        for index, (month, _name) in enumerate(core.year_months(year)):
            block = self._month_block(year, month)
            self.grid.addWidget(block, index // 4, index % 4)
        for column in range(4):
            self.grid.setColumnStretch(column, 1)

    def _month_block(self, year, month):
        """One small month: its name and a grid of load-shaded days."""
        box = QtWidgets.QFrame()
        box.setFrameShape(QtWidgets.QFrame.StyledPanel)
        layout = QtWidgets.QVBoxLayout(box)
        layout.setContentsMargins(6, 5, 6, 5)
        layout.setSpacing(4)

        header = QtWidgets.QHBoxLayout()
        name = QtWidgets.QLabel(core.month_name(year, month, self.tab.language()))
        name.setStyleSheet(
            "font-weight: bold; color: %s;"
            % (TODAY_COLOR if (year, month) == (dt.date.today().year,
                                                dt.date.today().month)
               else "palette(text)"))
        header.addWidget(name)
        header.addStretch()
        count = self.tab.month_task_count(year, month)
        summary = QtWidgets.QLabel(str(count))
        summary.setStyleSheet("color: #9aa0a6; font-size: 10px;")
        header.addWidget(summary)
        layout.addLayout(header)

        small = QtWidgets.QGridLayout()
        small.setSpacing(1)
        for column, label in enumerate(("M", "T", "W", "T", "F", "S", "S")):
            head = QtWidgets.QLabel(label)
            head.setAlignment(QtCore.Qt.AlignCenter)
            head.setStyleSheet("color: #6e7681; font-size: 8px;")
            small.addWidget(head, 0, column)

        weeks = core.month_grid(year, month, self.tab.start_monday())
        for row, week in enumerate(weeks, start=1):
            for column, day in enumerate(week):
                small.addWidget(self._day_dot(day, month), row, column)
        layout.addLayout(small)
        layout.addStretch()
        return box

    def _day_dot(self, day, month):
        """A single day as a small square, shaded by how much is on it."""
        dot = QtWidgets.QLabel()
        dot.setAlignment(QtCore.Qt.AlignCenter)
        dot.setFixedSize(15, 14)
        dot.setText(str(day.day) if day.day <= 9 else "")
        load = self.tab.day_load(day)
        holiday = self.tab.calendar.is_holiday(day)
        if day.month != month:
            dot.setStyleSheet("color: #4a4a4a; font-size: 8px;")
        elif holiday:
            dot.setStyleSheet("color: %s; font-size: 8px; font-weight: bold;"
                              % HOLIDAY_COLOR)
        elif load:
            shade = min(3, load)
            colours = {1: "#26418f", 2: "#2b5cad", 3: "#3b78d8"}
            dot.setStyleSheet(
                "background: %s; color: #ffffff; font-size: 8px;"
                " border-radius: 2px;" % colours[shade])
        elif day.weekday() >= 5:
            dot.setStyleSheet("color: #6e7681; font-size: 8px;")
        else:
            dot.setStyleSheet("color: #9aa0a6; font-size: 8px;")
        if day == dt.date.today():
            dot.setStyleSheet(dot.styleSheet() +
                              "border: 1px solid %s;" % TODAY_COLOR)
        dot.setToolTip("%s\n%d task(s)" % (day.isoformat(), load))
        return dot


class DayTimeline(QtWidgets.QWidget):
    """One day drawn in hours, for the "what does today look like" question.

    Untimed tasks sit in a tray at the top; timed ones are placed on the hour
    ruler. Hours outside the working window are drawn but collapsed, so the
    useful part of the day fills the screen without hiding the rest.
    """

    ROW_HEIGHT = 34

    def __init__(self, tab):
        super().__init__(tab)
        self.tab = tab
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat("application/x-scriptdb-task"):
            event.acceptProposedAction()

    def dropEvent(self, event):
        raw = bytes(event.mimeData().data("application/x-scriptdb-task"))
        self.tab.move_task_to_day(raw.decode("utf-8", "replace"), self.tab.anchor)
        event.acceptProposedAction()

    def rebuild(self):
        self.setUpdatesEnabled(False)
        try:
            self._rebuild()
        finally:
            self.setUpdatesEnabled(True)

    def _rebuild(self):
        old = self.layout()
        if old is not None:
            while old.count():
                item = old.takeAt(0)
                widget = item.widget()
                if widget is not None:
                    widget.deleteLater()
            QtWidgets.QWidget().setLayout(old)      # detach the old layout

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        day = self.tab.anchor
        tasks = self.tab.tasks_on_day(day)
        timed = [task for task in tasks if task.due_moment() is not None
                 and task.due_moment().date() == day]
        untimed = [task for task in tasks if task not in timed]

        if untimed:
            tray = QtWidgets.QFrame()
            tray.setFrameShape(QtWidgets.QFrame.StyledPanel)
            tray_layout = QtWidgets.QVBoxLayout(tray)
            tray_layout.setContentsMargins(8, 6, 8, 6)
            tray_layout.setSpacing(3)
            caption = QtWidgets.QLabel(self.tab.t("todo_untimed", "No set time"))
            caption.setProperty("role", "hint")
            tray_layout.addWidget(caption)
            for task in untimed:
                tray_layout.addWidget(TaskChip(task, self.tab))
            layout.addWidget(tray)

        start, end = self.tab.working_hours()
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        # The ruler is taller than the viewport on purpose; the horizontal bar
        # is off because a long title must wrap inside the row, not push the
        # whole timeline sideways.
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        canvas = QtWidgets.QWidget()
        ruler = QtWidgets.QVBoxLayout(canvas)
        ruler.setContentsMargins(0, 0, 0, 0)
        ruler.setSpacing(0)

        by_hour = {}
        for task in timed:
            hour = task.due_moment().hour
            by_hour.setdefault(hour, []).append(task)

        for hour in range(24):
            ruler.addWidget(self._hour_row(hour, by_hour.get(hour, []),
                                           start <= hour < end))
        ruler.addStretch()
        scroll.setWidget(canvas)
        layout.addWidget(scroll, 1)

        # The holiday, if there is one, is stated plainly rather than implied.
        holiday = self.tab.calendar.label(day, self.tab.language())
        if holiday:
            note = QtWidgets.QLabel("\U0001f389 " + holiday)
            note.setStyleSheet("color: %s; font-weight: bold;" % HOLIDAY_COLOR)
            layout.addWidget(note)

    def _hour_row(self, hour, tasks, in_window):
        row = QtWidgets.QWidget()
        row.setMinimumHeight(self.ROW_HEIGHT if in_window else 18)
        layout = QtWidgets.QHBoxLayout(row)
        layout.setContentsMargins(2, 1, 2, 1)
        layout.setSpacing(6)

        label = QtWidgets.QLabel("%02d:00" % hour)
        label.setFixedWidth(42)
        label.setStyleSheet(
            "color: %s; font-size: 11px;"
            % (TODAY_COLOR if in_window else "#6e7681"))
        layout.addWidget(label)
        layout.addWidget(self._separator())

        if tasks:
            box = QtWidgets.QVBoxLayout()
            box.setSpacing(2)
            for task in tasks:
                box.addWidget(TaskChip(task, self.tab))
            layout.addLayout(box, 1)
        else:
            layout.addStretch()
        return row

    @staticmethod
    def _separator():
        line = QtWidgets.QFrame()
        line.setFrameShape(QtWidgets.QFrame.HLine)
        line.setFixedHeight(1)
        line.setStyleSheet("background: palette(mid); border: none;")
        return line


class TodayView(QtWidgets.QWidget):
    """The three questions a list should answer first.

    Late, due today, and in progress. Anything else is one click away in the
    other views, so this one stays short on purpose.
    """

    def __init__(self, tab):
        super().__init__(tab)
        self.tab = tab
        self.scroll = QtWidgets.QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.canvas = QtWidgets.QWidget()
        self.column = QtWidgets.QVBoxLayout(self.canvas)
        self.column.setContentsMargins(4, 4, 4, 4)
        self.column.setSpacing(10)
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.scroll)
        self.scroll.setWidget(self.canvas)

    def rebuild(self):
        while self.column.count():
            item = self.column.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        buckets = self.tab.store.today()
        if self.tab.calendar.is_holiday(dt.date.today()):
            holiday = self.tab.calendar.label(dt.date.today(),
                                              self.tab.language())
            self.column.addWidget(self._banner(
                "\U0001f389 " + holiday, HOLIDAY_COLOR))

        sections = (
            ("todo_section_late", "Late", buckets["overdue"], "#f7768e"),
            ("todo_section_today", "Due today", buckets["today"], TODAY_COLOR),
            ("todo_section_doing", "In progress", buckets["doing"],
             "#8ab4f8"),
        )
        empty = True
        for key, title, tasks, colour in sections:
            if not tasks:
                continue
            empty = False
            self.column.addWidget(self._header(
                "%s  (%d)" % (self.tab.t(key, title), len(tasks)), colour))
            for task in tasks:
                self.column.addWidget(self._row(task, colour))

        if empty:
            done = QtWidgets.QLabel(self.tab.t(
                "todo_nothing_today", "Nothing is due today."))
            done.setAlignment(QtCore.Qt.AlignCenter)
            done.setStyleSheet("color: #9aa0a6; padding: 28px;")
            self.column.addWidget(done)

        soon = self.tab.store.upcoming(7)
        if soon:
            self.column.addWidget(self._header(
                "%s  (%d)" % (self.tab.t("todo_section_soon",
                                         "Next seven days"), len(soon)),
                "#c586c0"))
            for task in soon[:12]:
                self.column.addWidget(self._row(task, "#c586c0"))
        self.column.addStretch()

    @staticmethod
    def _banner(text, colour):
        label = QtWidgets.QLabel(text)
        label.setStyleSheet(
            "color: %s; font-weight: bold; font-size: 14px; padding: 6px;"
            % colour)
        return label

    @staticmethod
    def _header(text, colour):
        label = QtWidgets.QLabel(text)
        label.setStyleSheet(
            "color: %s; font-weight: bold; font-size: 13px;"
            " border-bottom: 1px solid palette(mid); padding-bottom: 3px;"
            % colour)
        return label

    def _row(self, task, colour):
        """A full-width, read-at-a-glance row for one task."""
        row = QtWidgets.QFrame()
        row.setFrameShape(QtWidgets.QFrame.StyledPanel)
        row.setCursor(QtCore.Qt.PointingHandCursor)
        layout = QtWidgets.QHBoxLayout(row)
        layout.setContentsMargins(8, 5, 8, 5)
        layout.setSpacing(8)

        checkbox = QtWidgets.QCheckBox()
        checkbox.setChecked(task.done)
        checkbox.setToolTip(self.tab.t("todo_mark_done", "Mark as done"))
        checkbox.toggled.connect(
            lambda checked, tid=task.id: self.tab.toggle_task(tid))
        layout.addWidget(checkbox)

        title = QtWidgets.QLabel(task.title)
        title.setStyleSheet(
            "color: %s;%s" % ("#9aa0a6" if task.done else "palette(text)",
                              " text-decoration: line-through;"
                              if task.done else ""))
        layout.addWidget(title, 1)

        moment = task.due_moment()
        if moment is not None:
            when = QtWidgets.QLabel(
                moment.strftime("%H:%M") + "  " + core.human_delta(moment))
            when.setStyleSheet("color: %s; font-size: 11px;" % colour)
            layout.addWidget(when)

        if task.get("project"):
            project = QtWidgets.QLabel(task.get("project"))
            project.setStyleSheet(
                "color: #9aa0a6; font-size: 11px;"
                " border: 1px solid palette(mid); border-radius: 3px;"
                " padding: 1px 5px;")
            layout.addWidget(project)

        button = QtWidgets.QToolButton()
        button.setText("\u2026")
        button.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        button.setMenu(self.tab.build_task_menu(task))
        layout.addWidget(button)

        row.mouseDoubleClickEvent = lambda _event: self.tab.open_task(task.id)
        return row



class TaskEditorScrollArea(QtWidgets.QScrollArea):
    """Scrollable host for the full ToDo editor form."""

    _native_wheel_widgets = (QtWidgets.QAbstractScrollArea,
                             QtWidgets.QComboBox,
                             QtWidgets.QAbstractSpinBox,
                             QtWidgets.QSlider)

    def __init__(self, editor, parent=None):
        super().__init__(parent)
        self.setWidget(editor)
        self.setWidgetResizable(True)
        self.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        self.setAlignment(QtCore.Qt.AlignTop)
        self.setFocusPolicy(QtCore.Qt.StrongFocus)
        # Route the wheel from ordinary form controls to this viewport. Native
        # scrollable controls keep their own wheel behaviour.
        for child in [editor] + editor.findChildren(QtWidgets.QWidget):
            child.installEventFilter(self)

    def eventFilter(self, watched, event):
        if (event.type() == QtCore.QEvent.Wheel
                and watched is not self
                and not isinstance(watched, self._native_wheel_widgets)):
            self._scroll_by_wheel(event)
            return True
        return super().eventFilter(watched, event)

    def wheelEvent(self, event):
        self._scroll_by_wheel(event)

    def _scroll_by_wheel(self, event):
        delta = event.pixelDelta().y() or event.angleDelta().y()
        if not delta:
            event.ignore()
            return
        # Angle deltas are normally 120 per mouse notch; keep the movement
        # smooth enough for a long form without making one notch jump a page.
        amount = int(delta / 2) if event.pixelDelta().isNull() else int(delta)
        bar = self.verticalScrollBar()
        bar.setValue(bar.value() - amount)
        event.accept()


class TaskEditor(QtWidgets.QWidget):
    """The task editing panel, docked beside the calendar.

    A panel rather than a modal dialog: a task can be compared against the
    calendar while it is written, and closing a dialog by accident no longer
    throws the work away. Every change is applied on "Save", and the panel
    says which task it is editing so two tasks cannot be confused.
    """

    def __init__(self, tab):
        super().__init__(tab)
        self.tab = tab
        self.current_id = None
        self._loading = False
        self._build()

    def _build(self):
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)
        outer.setSpacing(8)

        header = QtWidgets.QHBoxLayout()
        self.heading = QtWidgets.QLabel("")
        self.heading.setProperty("role", "title")
        header.addWidget(self.heading)
        header.addStretch()
        self.db_label = QtWidgets.QLabel("")
        self.db_label.setProperty("role", "hint")
        header.addWidget(self.db_label)
        outer.addLayout(header)

        form = QtWidgets.QFormLayout()
        form.setLabelAlignment(QtCore.Qt.AlignRight)
        form.setSpacing(6)

        self.title_edit = QtWidgets.QLineEdit()
        self.title_edit.setPlaceholderText(self.tab.t("todo_field_title", "What needs doing?"))

        self.project_edit = QtWidgets.QComboBox()
        self.project_edit.setEditable(True)
        identity_row = QtWidgets.QHBoxLayout()
        identity_row.setSpacing(6)
        identity_row.addWidget(QtWidgets.QLabel(
            self.tab.t("todo_field_title", "Title")))
        identity_row.addWidget(self.title_edit, 3)
        identity_row.addWidget(QtWidgets.QLabel(
            self.tab.t("todo_field_project", "Project")))
        identity_row.addWidget(self.project_edit, 2)
        form.addRow(identity_row)

        row = QtWidgets.QHBoxLayout()
        self.kind_combo = QtWidgets.QComboBox()
        for kind in core.KINDS:
            self.kind_combo.addItem(self.tab.t("todo_kind_" + kind, kind), kind)
        row.addWidget(self.kind_combo, 1)
        self.priority_combo = QtWidgets.QComboBox()
        for priority in core.PRIORITIES:
            self.priority_combo.addItem(
                self.tab.t("todo_priority_" + priority, priority), priority)
        row.addWidget(self.priority_combo, 1)
        self.status_combo = QtWidgets.QComboBox()
        for status in core.STATUSES:
            self.status_combo.addItem(
                self.tab.t("todo_status_" + status, status), status)
        row.addWidget(self.status_combo, 1)
        form.addRow(self.tab.t("todo_field_kind", "Kind"), row)

        # ---- schedule: one compact row ---------------------------------
        self.due_check = QtWidgets.QCheckBox(self.tab.t("todo_field_due", "Due"))
        self.due_check.toggled.connect(self._due_toggled)
        self.due_edit = QtWidgets.QDateTimeEdit()
        self.due_edit.setCalendarPopup(True)
        self.due_edit.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.due_edit.setDateTime(QtCore.QDateTime.currentDateTime().addSecs(3600))
        schedule_row = QtWidgets.QHBoxLayout()
        schedule_row.setSpacing(6)
        schedule_row.addWidget(self.due_check)
        schedule_row.addWidget(self.due_edit, 2)
        quick = QtWidgets.QToolButton()
        quick.setText("\u23f1")
        quick.setToolTip(self.tab.t("todo_quick_due", "Quick due time"))
        quick.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        quick.setMenu(self._due_menu())
        schedule_row.addWidget(quick)

        self.start_check = QtWidgets.QCheckBox(self.tab.t("todo_field_start", "Start"))
        self.start_check.toggled.connect(self._start_toggled)
        self.start_edit = QtWidgets.QDateTimeEdit()
        self.start_edit.setCalendarPopup(True)
        self.start_edit.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.start_edit.setDateTime(QtCore.QDateTime.currentDateTime())
        schedule_row.addWidget(self.start_check)
        schedule_row.addWidget(self.start_edit, 2)

        self.remind_combo = QtWidgets.QComboBox()
        for minutes in core.REMIND_CHOICES:
            self.remind_combo.addItem(self._remind_label(minutes), minutes)
        schedule_row.addWidget(QtWidgets.QLabel(
            self.tab.t("todo_field_remind", "Remind")))
        schedule_row.addWidget(self.remind_combo, 1)

        self.repeat_combo = QtWidgets.QComboBox()
        for rule in core.REPEATS:
            self.repeat_combo.addItem(self.tab.t("todo_repeat_" + rule, rule),
                                      rule)
        self.repeat_combo.currentIndexChanged.connect(self._repeat_changed)
        schedule_row.addWidget(QtWidgets.QLabel(
            self.tab.t("todo_field_repeat", "Repeat")))
        schedule_row.addWidget(self.repeat_combo, 1)

        self.custom_row = QtWidgets.QWidget()
        custom = QtWidgets.QHBoxLayout(self.custom_row)
        custom.setContentsMargins(0, 0, 0, 0)
        self.interval_spin = QtWidgets.QSpinBox()
        self.interval_spin.setRange(1, 365)
        self.unit_combo = QtWidgets.QComboBox()
        for unit in ("day", "week", "month", "year"):
            self.unit_combo.addItem(self.tab.t("todo_unit_" + unit, unit), unit)
        custom.addWidget(QtWidgets.QLabel(self.tab.t("todo_every", "every")))
        custom.addWidget(self.interval_spin)
        custom.addWidget(self.unit_combo, 1)
        schedule_row.addWidget(self.custom_row)
        form.addRow(schedule_row)

        self.tags_edit = QtWidgets.QLineEdit()
        self.tags_edit.setPlaceholderText(self.tab.t("todo_tags_hint",
                                                     "bug, ui, urgent"))

        self.colour_button = QtWidgets.QPushButton()
        self.colour_button.setToolTip(self.tab.t("todo_field_colour", "Colour"))
        self.colour_button.clicked.connect(self._pick_colour)
        self.colour = ""
        tags_row = QtWidgets.QHBoxLayout()
        tags_row.setSpacing(6)
        tags_row.addWidget(QtWidgets.QLabel(
            self.tab.t("todo_field_tags", "Tags")))
        tags_row.addWidget(self.tags_edit, 1)
        tags_row.addWidget(self.colour_button)
        form.addRow(tags_row)

        outer.addLayout(form)

        self.notes_edit = QtWidgets.QPlainTextEdit()
        self.notes_edit.setPlaceholderText(self.tab.t(
            "todo_field_notes", "Notes, steps to reproduce, definition of done..."))
        outer.addWidget(QtWidgets.QLabel(self.tab.t("todo_field_notes", "Notes")))
        outer.addWidget(self.notes_edit, 1)

        # ---- links to the rest of ScriptDB ------------------------------
        links_box = QtWidgets.QGroupBox(self.tab.t("todo_field_links", "Links"))
        links_layout = QtWidgets.QVBoxLayout(links_box)
        link_row = QtWidgets.QHBoxLayout()
        self.link_edit = QtWidgets.QLineEdit()
        self.link_edit.setPlaceholderText(self.tab.t(
            "todo_link_hint", "path, URL, script name or ticket"))
        add_link = QtWidgets.QPushButton(self.tab.t("todo_add", "Add"))
        add_link.clicked.connect(self._add_link)
        link_row.addWidget(self.link_edit, 1)
        link_row.addWidget(add_link)
        links_layout.addLayout(link_row)
        self.links_list = QtWidgets.QListWidget()
        self.links_list.setMaximumHeight(74)
        self.links_list.itemDoubleClicked.connect(self._open_link)
        links_layout.addWidget(self.links_list)
        outer.addWidget(links_box)

        # ---- checklist ---------------------------------------------------
        check_box = QtWidgets.QGroupBox(self.tab.t("todo_field_checklist",
                                                   "Checklist"))
        check_layout = QtWidgets.QVBoxLayout(check_box)
        check_row = QtWidgets.QHBoxLayout()
        self.check_edit = QtWidgets.QLineEdit()
        self.check_edit.setPlaceholderText(self.tab.t("todo_check_hint",
                                                      "one step"))
        self.check_edit.returnPressed.connect(self._add_check)
        add_check = QtWidgets.QPushButton(self.tab.t("todo_add", "Add"))
        add_check.clicked.connect(self._add_check)
        check_row.addWidget(self.check_edit, 1)
        check_row.addWidget(add_check)
        check_layout.addLayout(check_row)
        self.check_list = QtWidgets.QListWidget()
        self.check_list.setMaximumHeight(110)
        check_layout.addWidget(self.check_list)
        outer.addWidget(check_box)

        # ---- actions -----------------------------------------------------
        buttons = QtWidgets.QHBoxLayout()
        self.save_btn = QtWidgets.QPushButton(self.tab.t("todo_save", "Save task"))
        self.save_btn.setDefault(True)
        self.save_btn.clicked.connect(self.save)
        self.duplicate_btn = QtWidgets.QPushButton(self.tab.t("todo_duplicate",
                                                              "Duplicate"))
        self.duplicate_btn.clicked.connect(self.duplicate)
        self.clear_btn = QtWidgets.QPushButton(self.tab.t("todo_new", "New"))
        self.clear_btn.clicked.connect(self.new_task)
        self.delete_btn = QtWidgets.QPushButton(self.tab.t("todo_delete",
                                                           "Delete"))
        self.delete_btn.clicked.connect(self.delete)
        buttons.addWidget(self.save_btn)
        buttons.addWidget(self.duplicate_btn)
        buttons.addWidget(self.clear_btn)
        buttons.addStretch()
        buttons.addWidget(self.delete_btn)
        outer.addLayout(buttons)

        self.set_enabled_for_current(False)

    # -- helpers -----------------------------------------------------------
    def _remind_label(self, minutes):
        if minutes == 0:
            return self.tab.t("todo_remind_at", "at the time")
        if minutes < 60:
            return self.tab.t("todo_remind_min", "{count} min before",
                              count=minutes)
        if minutes < 1440:
            return self.tab.t("todo_remind_hour", "{count} h before",
                              count=minutes // 60)
        return self.tab.t("todo_remind_day", "{count} d before",
                          count=minutes // 1440)

    def _due_menu(self):
        menu = QtWidgets.QMenu(self)
        presets = (
            ("todo_quick_today", "Today 17:00", 0, 17),
            ("todo_quick_tomorrow", "Tomorrow 09:00", 1, 9),
            ("todo_quick_week", "In a week 09:00", 7, 9),
            ("todo_quick_monday", "Next Monday 09:00", None, 9),
        )
        for key, label, days, hour in presets:
            menu.addAction(self.tab.t(key, label),
                           lambda d=days, h=hour: self._set_due_preset(d, h))
        return menu

    def _set_due_preset(self, days, hour):
        base = dt.date.today()
        if days is None:                       # next Monday
            days = (7 - base.weekday()) % 7 or 7
        target = base + dt.timedelta(days=days)
        self.due_check.setChecked(True)
        self.due_edit.setDateTime(QtCore.QDateTime(
            target.year, target.month, target.day, hour, 0))

    def _due_toggled(self, checked):
        self.due_edit.setEnabled(checked)

    def _start_toggled(self, checked):
        self.start_edit.setEnabled(checked)

    def _repeat_changed(self, _index):
        custom = self.repeat_combo.currentData() == "custom"
        self.custom_row.setVisible(custom)

    def _pick_colour(self):
        colour = QtWidgets.QColorDialog.getColor(
            QtGui.QColor(self.colour or "#8ab4f8"), self)
        if colour.isValid():
            self.colour = colour.name()
            self._paint_colour_button()

    def _paint_colour_button(self):
        self.colour_button.setText(self.colour or self.tab.t("todo_auto",
                                                             "automatic"))
        self.colour_button.setStyleSheet(
            "border-left: 12px solid %s;" % (self.colour or "#8ab4f8"))

    # -- links and checklist ----------------------------------------------
    def _add_link(self):
        text = self.link_edit.text().strip()
        if not text:
            return
        if not self._link_exists(text):
            self.links_list.addItem(text)
        self.link_edit.clear()

    def _link_exists(self, text):
        for row in range(self.links_list.count()):
            if self.links_list.item(row).text() == text:
                return True
        return False

    def _open_link(self, item):
        """Follow a link: a file opens in the editor, a script in its tab."""
        self.tab.follow_link(item.text())

    def links(self):
        return [self.links_list.item(row).text()
                for row in range(self.links_list.count())]

    def _add_check(self):
        text = self.check_edit.text().strip()
        if not text:
            return
        self._append_check(text, False)
        self.check_edit.clear()

    def _append_check(self, text, done):
        item = QtWidgets.QListWidgetItem(text)
        item.setFlags(item.flags() | QtCore.Qt.ItemIsUserCheckable)
        item.setCheckState(QtCore.Qt.Checked if done else QtCore.Qt.Unchecked)
        self.check_list.addItem(item)

    def checklist(self):
        return [{"text": self.check_list.item(row).text(),
                 "done": self.check_list.item(row).checkState() == QtCore.Qt.Checked}
                for row in range(self.check_list.count())]

    # -- load / save --------------------------------------------------------
    def set_enabled_for_current(self, enabled):
        for widget in (self.save_btn, self.duplicate_btn):
            widget.setEnabled(enabled)
        self.delete_btn.setEnabled(bool(self.current_id))

    def new_task(self, day=None, hour=None):
        """Start a blank task, optionally anchored on a calendar day."""
        self._loading = True
        try:
            self.current_id = None
            self.heading.setText(self.tab.t("todo_new", "New task"))
            self.title_edit.clear()
            self.notes_edit.clear()
            self.tags_edit.clear()
            self.link_edit.clear()
            self.links_list.clear()
            self.check_list.clear()
            self.colour = ""
            self._paint_colour_button()
            self.kind_combo.setCurrentIndex(
                max(0, self.kind_combo.findData("task")))
            self.priority_combo.setCurrentIndex(
                max(0, self.priority_combo.findData("normal")))
            self.status_combo.setCurrentIndex(
                max(0, self.status_combo.findData("todo")))
            self.repeat_combo.setCurrentIndex(
                max(0, self.repeat_combo.findData("none")))
            self.remind_combo.setCurrentIndex(max(
                0, self.remind_combo.findData(
                    self.tab.store.settings.get("default_remind", 30))))
            self.project_edit.setCurrentText(
                self.tab.store.settings.get("default_project", ""))
            if day is not None:
                self.due_check.setChecked(True)
                self.due_edit.setDateTime(QtCore.QDateTime(
                    day.year, day.month, day.day,
                    hour if hour is not None else 17, 0))
            else:
                self.due_check.setChecked(True)
                self.due_edit.setDateTime(
                    QtCore.QDateTime.currentDateTime().addSecs(3600))
            self.start_check.setChecked(False)
            self._repeat_changed(None)
        finally:
            self._loading = False
        self.set_enabled_for_current(True)
        self.title_edit.setFocus()

    def load(self, task):
        """Fill the panel from an existing task."""
        self._loading = True
        try:
            self.current_id = task.id
            self.heading.setText(self.tab.t("todo_editing", "Editing: {title}",
                                            title=task.title))
            self.title_edit.setText(task.get("title", ""))
            self.notes_edit.setPlainText(task.get("notes", ""))
            self.tags_edit.setText(", ".join(task.tag_list()))
            self.colour = task.get("color", "")
            self._paint_colour_button()
            self.project_edit.setCurrentText(task.get("project", ""))
            self._set_combo(self.kind_combo, task.get("kind", "task"))
            self._set_combo(self.priority_combo, task.get("priority", "normal"))
            self._set_combo(self.status_combo, task.get("status", "todo"))
            self._set_combo(self.repeat_combo, task.get("repeat", "none"))

            moment = task.due_moment()
            self.due_check.setChecked(moment is not None)
            if moment is not None:
                self.due_edit.setDateTime(QtCore.QDateTime(
                    moment.year, moment.month, moment.day, moment.hour,
                    moment.minute))
            start = task.start_moment()
            self.start_check.setChecked(start is not None)
            if start is not None:
                self.start_edit.setDateTime(QtCore.QDateTime(
                    start.year, start.month, start.day, start.hour,
                    start.minute))
            self.remind_combo.setCurrentIndex(max(
                0, self.remind_combo.findData(int(task.get("remind") or 0))))
            self.interval_spin.setValue(int(task.get("repeat_interval") or 1))
            self._set_combo(self.unit_combo,
                            task.get("repeat_unit", "day"))

            self.links_list.clear()
            for link in (task.get("links") or []):
                self.links_list.addItem(str(link))
            self.check_list.clear()
            for entry in (task.get("checklist") or []):
                self._append_check(entry.get("text", ""), entry.get("done"))
            self._repeat_changed(None)
        finally:
            self._loading = False
        self.set_enabled_for_current(True)

    @staticmethod
    def _set_combo(combo, value):
        index = combo.findData(value)
        combo.setCurrentIndex(index if index >= 0 else 0)

    def values(self):
        """The form as a task dictionary (the store adds the bookkeeping)."""
        tags = [part.strip() for part in
                self.tags_edit.text().replace(";", ",").split(",")
                if part.strip()]
        due = ""
        if self.due_check.isChecked():
            due = self.due_edit.dateTime().toPyDateTime().replace(
                microsecond=0).isoformat(" ")
        start = ""
        if self.start_check.isChecked():
            start = self.start_edit.dateTime().toPyDateTime().replace(
                microsecond=0).isoformat(" ")
        return {
            "title": self.title_edit.text().strip(),
            "notes": self.notes_edit.toPlainText().strip(),
            "project": self.project_edit.currentText().strip(),
            "kind": self.kind_combo.currentData() or "task",
            "priority": self.priority_combo.currentData() or "normal",
            "status": self.status_combo.currentData() or "todo",
            "tags": tags,
            "links": self.links(),
            "checklist": self.checklist(),
            "due": due,
            "start": start,
            "remind": int(self.remind_combo.currentData() or 0),
            "repeat": self.repeat_combo.currentData() or "none",
            "repeat_interval": int(self.interval_spin.value()),
            "repeat_unit": self.unit_combo.currentData() or "day",
            "color": self.colour,
        }

    def save(self):
        values = self.values()
        if not values["title"]:
            QtWidgets.QMessageBox.warning(
                self, self.tab.t("todo_warning", "Warning"),
                self.tab.t("todo_need_title", "A task needs a title."))
            return None
        task = self.tab.save_task(self.current_id, values)
        if task is not None:
            self.load(task)
        return task

    def duplicate(self):
        values = self.values()
        title = values.get("title") or self.tab.t("todo_untitled", "untitled")
        values["title"] = title + " (copy)"
        task = self.tab.save_task(None, values)
        if task is not None:
            self.load(task)

    def delete(self):
        if self.current_id is None:
            return
        self.tab.delete_task(self.current_id)
        self.new_task()


class TodoTab(QtWidgets.QWidget):
    """The ToDo tab: calendar views, a task list and the reminder loop.
    """

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        # The task list and the shopping list are databases (db\).
        base = (getattr(window, "db_dir", None)
                or getattr(window, "DB_DIR", None)
                or app_paths.ensure_db_dir())
        self.base_dir = base
        self.store = core.TodoStore(os.path.join(base, core.TodoStore.DATA_FILE))
        self.calendar = core.HolidayCalendar(self.store.settings.get(
            "countries", ["pl", "uk"]))
        self.anchor = dt.date.today()
        self.view = self.store.settings.get("view", "month")
        self.selection = None                 # the task the list follows
        self.notifier = todo_notify.notifier_for(window)
        self._filters = {"query": "", "project": None, "tag": None,
                         "kind": None, "priority": None, "status": None}
        self._reminder_timer = None
        self._build()
        self._start_reminders()
        self.refresh()

    # ------------------------------------------------------------------ t
    def t(self, key, fallback=None, **kwargs):
        """Translate through the main window, falling back to English."""
        try:
            return self.window.t(key, fallback, **kwargs)
        except Exception:
            text = fallback if fallback is not None else key
            try:
                return text.format(**kwargs)
            except (KeyError, IndexError, ValueError):
                return text

    def language(self):
        """The language the calendar names should be printed in."""
        return "pl" if getattr(self.window, "lang", "en") == "pl" else "en"

    # -- settings helpers --------------------------------------------------
    def setting(self, key, fallback=None):
        return self.store.settings.get(key, core.DEFAULT_SETTINGS.get(key, fallback))

    def set_setting(self, key, value, save=True):
        self.store.settings[key] = value
        if save:
            self.save()

    def start_monday(self):
        return bool(self.setting("start_monday", True))

    def show_week_numbers(self):
        return bool(self.setting("show_week_numbers", True))

    def show_weekends(self):
        return bool(self.setting("show_weekends", True))

    def working_hours(self):
        start = max(0, min(23, int(self.setting("work_start", 8))))
        end = max(start + 1, min(24, int(self.setting("work_end", 18))))
        return start, end

    def chips_per_day(self):
        """How many task chips a month cell can show before it says "+n"."""
        return 4 if self.view == "month" else 6

    def save(self):
        """Persist the data file, reporting a failure instead of crashing."""
        try:
            self.store.save()
            return True
        except Exception as exc:
            self.status.setText(str(exc))
            return False

    # ------------------------------------------------------------ queries
    def tasks_on_day(self, day):
        """The tasks on a calendar day, ordered by time then priority."""
        tasks = self.store.on_day(day)
        order = {name: index for index, name in enumerate(core.PRIORITIES)}

        def sort_key(task):
            moment = task.due_moment()
            return (task.get("status") == "done",
                    moment is None,
                    moment or dt.datetime.max,
                    order.get(task.get("priority"), 9),
                    task.title.lower())
        return sorted(tasks, key=sort_key)

    def day_load(self, day):
        """How much is on a day, for the year views shading."""
        return sum(1 for task in self.store.on_day(day)
                   if task.get("status") != "done")

    def month_task_count(self, year, month):
        count = 0
        for day in range(1, 32):
            try:
                count += self.day_load(dt.date(year, month, day))
            except ValueError:
                break
        return count

    def filtered_tasks(self):
        """The list panels contents, after every filter."""
        return self.store.filter(
            query=self._filters["query"],
            project=self._filters["project"],
            tag=self._filters["tag"],
            kind=self._filters["kind"],
            priority=self._filters["priority"],
            status=self._filters["status"])
    # ------------------------------------------------------------- build UI
    def _build(self):
        """Build the workspace as seven views over one task store.

        The old layout put the calendar, the list and the form side by side in
        one row, which left all three too narrow to be read comfortably. They
        are now tabs: each gets the full width of the window, and the pieces
        that belong together -- the grid and its navigation, the list and its
        filters, the form and its notes -- are shown together instead of
        competing for space.
        """
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 8)
        root.setSpacing(6)

        root.addLayout(self._build_header())

        self.tabs = QtWidgets.QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.addTab(self._build_calendar_tab(), "Calendar")
        self.tabs.addTab(self._build_tasks_tab(), "Tasks")
        self.tabs.addTab(self._build_editor_tab(), "Editor")
        self.tabs.addTab(self._build_plan_tab(), "Plan")
        self.tabs.addTab(self._build_notes_tab(), "Notes")
        self.tabs.addTab(self._build_expenses_tab(), "Expenses")
        self.shopping_tab = ShoppingTab(self.window)
        self.tabs.addTab(self.shopping_tab, "Shopping")
        self.tabs.currentChanged.connect(self._on_tab_changed)
        root.addWidget(self.tabs, 1)

        footer = QtWidgets.QHBoxLayout()
        footer.setSpacing(10)
        self.status = QtWidgets.QLabel("")
        self.status.setProperty("role", "hint")
        footer.addWidget(self.status, 1)
        self.summary_label = QtWidgets.QLabel("")
        self.summary_label.setProperty("role", "hint")
        footer.addWidget(self.summary_label)
        self.next_holiday_label = QtWidgets.QLabel("")
        self.next_holiday_label.setProperty("role", "hint")
        self.next_holiday_label.setStyleSheet("color: %s;" % HOLIDAY_COLOR)
        footer.addWidget(self.next_holiday_label)
        root.addLayout(footer)

    def _build_notes_tab(self):
        """A small persistent scratchpad for notes related to the day."""
        page = QtWidgets.QWidget()
        layout = QtWidgets.QHBoxLayout(page)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        left = QtWidgets.QVBoxLayout()
        self.note_search = QtWidgets.QLineEdit()
        self.note_search.setPlaceholderText(
            self.t("todo_notes_search", "Search notes..."))
        self.note_search.textChanged.connect(self._refresh_notes)
        left.addWidget(self.note_search)
        self.note_list = QtWidgets.QListWidget()
        self.note_list.currentRowChanged.connect(self._load_selected_note)
        left.addWidget(self.note_list, 1)
        note_buttons = QtWidgets.QHBoxLayout()
        new_note = QtWidgets.QPushButton(self.t("todo_notes_new", "+ Note"))
        new_note.clicked.connect(self._new_note)
        delete_note = QtWidgets.QPushButton(self.t("todo_notes_delete", "Delete"))
        delete_note.clicked.connect(self._delete_note)
        note_buttons.addWidget(new_note)
        note_buttons.addWidget(delete_note)
        left.addLayout(note_buttons)
        layout.addLayout(left, 1)

        editor = QtWidgets.QVBoxLayout()
        self.note_title = QtWidgets.QLineEdit()
        self.note_title.setPlaceholderText(
            self.t("todo_notes_title", "Note title"))
        editor.addWidget(self.note_title)
        self.note_body = QtWidgets.QPlainTextEdit()
        self.note_body.setPlaceholderText(
            self.t("todo_notes_body", "Write a note..."))
        editor.addWidget(self.note_body, 1)
        save_note = QtWidgets.QPushButton(self.t("todo_notes_save", "Save note"))
        save_note.clicked.connect(self._save_note)
        editor.addWidget(save_note, 0, QtCore.Qt.AlignRight)
        layout.addLayout(editor, 3)
        self._new_note()
        self._refresh_notes()
        return page

    def _build_expenses_tab(self):
        """A local expense ledger with totals and a compact calculator."""
        page = QtWidgets.QWidget()
        layout = QtWidgets.QHBoxLayout(page)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        ledger = QtWidgets.QVBoxLayout()
        form = QtWidgets.QHBoxLayout()
        self.expense_date = QtWidgets.QDateEdit(QtCore.QDate.currentDate())
        self.expense_date.setCalendarPopup(True)
        self.expense_date.setDisplayFormat("yyyy-MM-dd")
        self.expense_description = QtWidgets.QLineEdit()
        self.expense_description.setPlaceholderText(
            self.t("todo_expense_description", "Description"))
        self.expense_category = QtWidgets.QComboBox()
        self.expense_category.setEditable(True)
        self.expense_category.addItems(["Food", "Transport", "Bills", "Shopping", "Other"])
        self.expense_amount = QtWidgets.QDoubleSpinBox()
        self.expense_amount.setRange(0, 999999999)
        self.expense_amount.setDecimals(2)
        self.expense_amount.setSuffix(" PLN")
        add_expense = QtWidgets.QPushButton(
            self.t("todo_expense_add", "+ Add"))
        add_expense.clicked.connect(self._add_expense)
        form.addWidget(self.expense_date)
        form.addWidget(self.expense_description, 2)
        form.addWidget(self.expense_category, 1)
        form.addWidget(self.expense_amount)
        form.addWidget(add_expense)
        ledger.addLayout(form)
        self.expense_table = QtWidgets.QTableWidget(0, 4)
        self.expense_table.setHorizontalHeaderLabels(["Date", "Description", "Category", "Amount"])
        self.expense_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.expense_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.expense_table.horizontalHeader().setStretchLastSection(True)
        ledger.addWidget(self.expense_table, 1)
        actions = QtWidgets.QHBoxLayout()
        remove = QtWidgets.QPushButton(self.t("todo_expense_remove", "Remove selected"))
        remove.clicked.connect(self._remove_expense)
        self.expense_total = QtWidgets.QLabel()
        self.expense_total.setProperty("role", "title")
        actions.addWidget(remove)
        actions.addStretch()
        actions.addWidget(self.expense_total)
        ledger.addLayout(actions)
        layout.addLayout(ledger, 1)
        layout.addWidget(self._build_calculator(), 0)
        self._refresh_expenses()
        return page

    def _build_calculator(self):
        box = QtWidgets.QGroupBox(self.t("todo_calculator", "Calculator"))
        box.setMaximumWidth(220)
        layout = QtWidgets.QVBoxLayout(box)
        self.calculator_display = QtWidgets.QLineEdit()
        self.calculator_display.setAlignment(QtCore.Qt.AlignRight)
        self.calculator_display.setPlaceholderText("0")
        self.calculator_display.returnPressed.connect(self._calculate)
        layout.addWidget(self.calculator_display)
        grid = QtWidgets.QGridLayout()
        buttons = ("7", "8", "9", "/", "4", "5", "6", "*",
                   "1", "2", "3", "-", "0", ".", "=", "+")
        for index, text in enumerate(buttons):
            button = QtWidgets.QPushButton(text)
            button.setMinimumSize(38, 32)
            button.clicked.connect(lambda _checked=False, value=text:
                                   self._calculator_press(value))
            grid.addWidget(button, index // 4, index % 4)
        clear = QtWidgets.QPushButton("C")
        clear.clicked.connect(self.calculator_display.clear)
        grid.addWidget(clear, 4, 0, 1, 4)
        layout.addLayout(grid)
        return box

    def _new_note(self):
        self._note_id = None
        self.note_title.clear()
        self.note_body.clear()

    def _refresh_notes(self):
        if not hasattr(self, "note_list"):
            return
        query = self.note_search.text().strip().lower()
        self.note_list.clear()
        for note in self.store.notes:
            if query and query not in (note.get("title", "") + " " + note.get("body", "")).lower():
                continue
            item = QtWidgets.QListWidgetItem(note.get("title", "Untitled"))
            item.setData(QtCore.Qt.UserRole, note.get("id"))
            self.note_list.addItem(item)

    def _load_selected_note(self, row):
        if row < 0:
            return
        note_id = self.note_list.item(row).data(QtCore.Qt.UserRole)
        note = next((item for item in self.store.notes if item.get("id") == note_id), None)
        if note is not None:
            self._note_id = note_id
            self.note_title.setText(note.get("title", ""))
            self.note_body.setPlainText(note.get("body", ""))

    def _save_note(self):
        title = self.note_title.text().strip() or self.t("todo_notes_untitled", "Untitled")
        data = {"id": self._note_id or uuid.uuid4().hex,
                "title": title, "body": self.note_body.toPlainText(),
                "updated": dt.datetime.now().isoformat(timespec="seconds")}
        existing = next((item for item in self.store.notes
                         if item.get("id") == data["id"]), None)
        if existing is None:
            self.store.notes.insert(0, data)
        else:
            existing.update(data)
        self._note_id = data["id"]
        self.save()
        self._refresh_notes()

    def _delete_note(self):
        if not self._note_id:
            return
        self.store.notes = [note for note in self.store.notes
                            if note.get("id") != self._note_id]
        self.save()
        self._new_note()
        self._refresh_notes()

    def _add_expense(self):
        description = self.expense_description.text().strip()
        amount = float(self.expense_amount.value())
        if not description or amount <= 0:
            return
        self.store.expenses.append({
            "id": uuid.uuid4().hex,
            "date": self.expense_date.date().toString("yyyy-MM-dd"),
            "description": description,
            "category": self.expense_category.currentText().strip() or "Other",
            "amount": amount,
        })
        self.expense_description.clear()
        self.expense_amount.setValue(0)
        self.save()
        self._refresh_expenses()

    def _refresh_expenses(self):
        if not hasattr(self, "expense_table"):
            return
        self.expense_table.setRowCount(0)
        total = 0.0
        for expense in self.store.expenses:
            row = self.expense_table.rowCount()
            self.expense_table.insertRow(row)
            values = (expense.get("date", ""), expense.get("description", ""),
                      expense.get("category", "Other"),
                      "%.2f PLN" % float(expense.get("amount", 0)))
            for column, value in enumerate(values):
                self.expense_table.setItem(row, column,
                                           QtWidgets.QTableWidgetItem(str(value)))
            total += float(expense.get("amount", 0))
        self.expense_total.setText("%.2f PLN" % total)

    def _remove_expense(self):
        rows = sorted({index.row() for index in
                       self.expense_table.selectionModel().selectedRows()}, reverse=True)
        for row in rows:
            del self.store.expenses[row]
        if rows:
            self.save()
            self._refresh_expenses()

    def _calculator_press(self, value):
        if value == "=":
            self._calculate()
        else:
            self.calculator_display.insert(value)

    def _calculate(self):
        try:
            value = self._safe_calculate(self.calculator_display.text())
            self.calculator_display.setText("%.10g" % value)
        except (ValueError, SyntaxError, ZeroDivisionError):
            self.calculator_display.setText("Error")

    @staticmethod
    def _safe_calculate(expression):
        """Evaluate only arithmetic nodes entered into the small calculator."""
        tree = ast.parse(expression, mode="eval")
        operators = {ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b,
                     ast.Mult: lambda a, b: a * b, ast.Div: lambda a, b: a / b,
                     ast.Mod: lambda a, b: a % b, ast.Pow: lambda a, b: a ** b}

        def visit(node):
            if isinstance(node, ast.Expression):
                return visit(node.body)
            if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
                return node.value
            if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
                value = visit(node.operand)
                return value if isinstance(node.op, ast.UAdd) else -value
            if isinstance(node, ast.BinOp) and type(node.op) in operators:
                return operators[type(node.op)](visit(node.left), visit(node.right))
            raise ValueError("unsupported expression")

        return visit(tree)

    # --------------------------------------------------------- the four tabs
    def _build_calendar_tab(self):
        """Calendar tab: the view switch, the navigation and the grid."""
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        layout.addLayout(self._build_view_bar())
        layout.addWidget(self._build_calendar_side(), 1)
        self._page_calendar = page
        return page
    def _build_tasks_tab(self):
        """Tasks tab: the filters and the list, at full width."""
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        layout.addLayout(self._build_filter_bar())
        layout.addWidget(self._build_list_side(), 1)
        self._page_tasks = page
        return page
    def _build_editor_tab(self):
        """Editor tab: the task form on its own, so it is not cramped."""
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        layout.addWidget(self._build_editor_side(), 1)
        self._page_editor = page
        return page
    def _build_plan_tab(self):
        """Plan tab: the fortnight ahead, the log and the numbers."""
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        columns = QtWidgets.QHBoxLayout()
        columns.setSpacing(10)

        # Left: what the next two weeks look like, day by day.
        left = QtWidgets.QGroupBox("Next two weeks")
        left_layout = QtWidgets.QVBoxLayout(left)
        self.plan_tree = QtWidgets.QTreeWidget()
        self.plan_tree.setAlternatingRowColors(True)
        self.plan_tree.setColumnCount(3)
        self.plan_tree.setHeaderLabels(["Day", "Load", "Holiday"])
        self.plan_tree.setRootIsDecorated(False)
        left_layout.addWidget(self.plan_tree)
        columns.addWidget(left, 3)

        # Middle: the completion history, newest first.
        middle = QtWidgets.QGroupBox("Recently completed")
        middle_layout = QtWidgets.QVBoxLayout(middle)
        self.plan_log = QtWidgets.QListWidget()
        middle_layout.addWidget(self.plan_log)
        columns.addWidget(middle, 2)

        # Right: the numbers, and the tools that act on the whole list.
        right = QtWidgets.QGroupBox("Overview")
        right_layout = QtWidgets.QVBoxLayout(right)
        self.plan_stats = QtWidgets.QTreeWidget()
        self.plan_stats.setAlternatingRowColors(True)
        self.plan_stats.setColumnCount(2)
        self.plan_stats.setHeaderLabels(["Measure", "Value"])
        self.plan_stats.setRootIsDecorated(False)
        right_layout.addWidget(self.plan_stats)
        right_layout.addWidget(self._build_organise_box())
        columns.addWidget(right, 2)

        layout.addLayout(columns, 1)
        self._page_plan = page
        return page
    def _build_organise_box(self):
        """The bulk actions, grouped so the Plan tab is useful on its own."""
        box = QtWidgets.QGroupBox("Organise")
        layout = QtWidgets.QVBoxLayout(box)
        layout.setSpacing(4)
        for key, fallback, callback in (
                ("todo_org_overdue", "Push all overdue tasks to today",
                 lambda: self._bulk_move_overdue()),
                ("todo_org_untimed", "Give every task without a time an hour",
                 lambda: self._bulk_time_default()),
                ("todo_org_dedupe", "Remove duplicate tasks",
                 lambda: self._bulk_dedupe()),
                ("todo_org_empty", "Remove tasks with no title",
                 lambda: self._bulk_drop_empty()),
                ("todo_org_archive", "Archive completed tasks",
                 lambda: self._bulk_archive())):
            button = QtWidgets.QPushButton(self.t(key, fallback))
            button.clicked.connect(callback)
            layout.addWidget(button)
        layout.addStretch()
        return box

    def _build_header(self):
        header = QtWidgets.QHBoxLayout()
        self.title_label = QtWidgets.QLabel(self.t("todo_title", "TO DO"))
        self.title_label.setProperty("role", "title")
        header.addWidget(self.title_label)
        self.subtitle = QtWidgets.QLabel(
            self.t("todo_subtitle", "Tasks, calendar and reminders"))
        self.subtitle.setProperty("role", "hint")
        header.addWidget(self.subtitle)
        header.addStretch()

        self.search_edit = QtWidgets.QLineEdit()
        self.search_edit.setPlaceholderText(self.t("todo_search", "Search tasks..."))
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setMaximumWidth(240)
        self.search_edit.textChanged.connect(self._on_search)
        header.addWidget(self.search_edit)

        self.clock_label = QtWidgets.QLabel("")
        self.clock_label.setProperty("role", "hint")
        header.addWidget(self.clock_label)
        return header

    def _build_view_bar(self):
        """The Calendar tab own bar: which view, and which period.

        These controls used to share one long row with the filters and the
        menus, which made every one of them narrower than it needed to be.
        They now sit above the grid they act on, which is the only place they
        mean anything.
        """
        bar = QtWidgets.QHBoxLayout()
        bar.setSpacing(6)

        self.view_buttons = {}
        group = QtWidgets.QButtonGroup(self)
        for key, label in (("today", "Today"), ("day", "Day"), ("week", "Week"),
                           ("month", "Month"), ("year", "Year")):
            button = QtWidgets.QRadioButton(self.t("todo_view_" + key, label))
            group.addButton(button)
            button.toggled.connect(
                lambda checked, k=key: checked and self.set_view(k))
            bar.addWidget(button)
            self.view_buttons[key] = button
        bar.addSpacing(14)

        self.prev_btn = QtWidgets.QToolButton()
        self.prev_btn.setText("\u25c0")
        self.prev_btn.setToolTip(self.t("todo_prev", "Previous"))
        self.prev_btn.clicked.connect(lambda: self.step(-1))
        bar.addWidget(self.prev_btn)

        self.today_btn = QtWidgets.QPushButton(self.t("todo_today", "Today"))
        self.today_btn.clicked.connect(self.go_today)
        bar.addWidget(self.today_btn)

        self.next_btn = QtWidgets.QToolButton()
        self.next_btn.setText("\u25b6")
        self.next_btn.setToolTip(self.t("todo_next", "Next"))
        self.next_btn.clicked.connect(lambda: self.step(1))
        bar.addWidget(self.next_btn)

        self.period_label = QtWidgets.QLabel("")
        self.period_label.setStyleSheet("font-weight: bold;")
        bar.addSpacing(6)
        bar.addWidget(self.period_label)
        bar.addStretch()

        self.add_btn = QtWidgets.QToolButton()
        self.add_btn.setText(self.t("todo_add_task", "+ Task"))
        self.add_btn.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.add_btn.setMenu(self._new_menu())
        bar.addWidget(self.add_btn)
        return bar
    def _build_filter_bar(self):
        """The Tasks tabs own bar: the filters that narrow the list."""
        bar = QtWidgets.QHBoxLayout()
        bar.setSpacing(6)

        bar.addWidget(QtWidgets.QLabel(self.t("todo_filter_show", "Show")))
        self.filter_combo = QtWidgets.QComboBox()
        self.filter_combo.setMinimumWidth(150)
        self.filter_combo.currentIndexChanged.connect(self._on_filter_changed)
        bar.addWidget(self.filter_combo)

        bar.addWidget(QtWidgets.QLabel(self.t("todo_field_project", "Project")))
        self.project_combo = QtWidgets.QComboBox()
        self.project_combo.setMinimumWidth(140)
        self.project_combo.currentIndexChanged.connect(self._on_project_changed)
        bar.addWidget(self.project_combo)

        bar.addWidget(QtWidgets.QLabel(self.t("todo_field_tags", "Tags")))
        self.tag_combo = QtWidgets.QComboBox()
        self.tag_combo.setMinimumWidth(120)
        self.tag_combo.currentIndexChanged.connect(self._on_tag_changed)
        bar.addWidget(self.tag_combo)

        reset = QtWidgets.QPushButton(self.t("todo_filter_reset", "Reset"))
        reset.clicked.connect(self.reset_filters)
        bar.addWidget(reset)

        bar.addStretch()
        self.list_hint = QtWidgets.QLabel("")
        self.list_hint.setProperty("role", "hint")
        bar.addWidget(self.list_hint)

        self.add_btn2 = QtWidgets.QToolButton()
        self.add_btn2.setText(self.t("todo_add_task", "+ Task"))
        self.add_btn2.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.add_btn2.setMenu(self._new_menu())
        bar.addWidget(self.add_btn2)

        self.tools_btn = QtWidgets.QToolButton()
        self.tools_btn.setText(self.t("todo_tools", "Tools"))
        self.tools_btn.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.tools_btn.setMenu(self._tools_menu())
        bar.addWidget(self.tools_btn)
        return bar
    def _on_tab_changed(self, index):
        """Keep each tabs contents fresh when it comes to the front.

        The store is shared, so a task completed on the Tasks tab has to be
        reflected in the calendar behind it. Rebuilding only the tab being
        shown keeps that honest without redrawing all four on every change.
        """
        name = self.tabs.tabText(index)
        if self.view in self.view_buttons:
            self.view_buttons[self.view].setChecked(True)
        if index == 0:
            self._rebuild_calendar()
        elif index == 1:
            self._refresh_combos()
            self.refresh_list()
        elif index == 3:
            self._refresh_plan()
        self._refresh_summary()

    def _refresh_plan(self):
        """Refresh the fortnight, completion log, and overview statistics."""
        if not hasattr(self, "plan_tree"):
            return

        self.plan_tree.clear()
        start = dt.date.today()
        for offset in range(14):
            day = start + dt.timedelta(days=offset)
            tasks = self.store.on_day(day)
            holiday = self.calendar.label(day, self.language()) or ""
            item = QtWidgets.QTreeWidgetItem([
                core.day_name(day, self.language()),
                str(len(tasks)),
                holiday,
            ])
            item.setToolTip(0, day.isoformat())
            self.plan_tree.addTopLevelItem(item)

        self.plan_log.clear()
        for completion in self.store.completions[:20]:
            title = completion.get("title", "")
            stamp = completion.get("at", "")
            self.plan_log.addItem("%s  %s" % (stamp, title))

        self.plan_stats.clear()
        tasks = self.store.tasks
        open_tasks = [task for task in tasks if not task.done]
        completed = [task for task in tasks if task.done]
        overdue = [task for task in open_tasks if task.overdue]
        for label, value in (
                (self.t("todo_stat_total", "Total tasks"), len(tasks)),
                (self.t("todo_stat_open", "Open"), len(open_tasks)),
                (self.t("todo_stat_completed", "Completed"), len(completed)),
                (self.t("todo_stat_overdue", "Overdue"), len(overdue))):
            self.plan_stats.addTopLevelItem(
                QtWidgets.QTreeWidgetItem([label, str(value)]))

    def _new_menu(self):
        menu = QtWidgets.QMenu(self)
        menu.addAction(self.t("todo_new", "New task"), self.new_task)
        menu.addAction(self.t("todo_new_on_day", "New task on the shown day..."),
                       lambda: self.new_task_on(self.anchor))
        menu.addSeparator()
        templates = menu.addMenu(self.t("todo_templates", "From a template"))
        for entry in core.template_tasks():
            templates.addAction(
                self.t("todo_tpl_" + entry["key"], entry["title"]),
                lambda item=entry: self.new_from_template(item))
        return menu

    def _tools_menu(self):
        menu = QtWidgets.QMenu(self)

        holidays = menu.addMenu(self.t("todo_holidays", "Holidays"))
        self._holiday_actions = {}
        for code, name in core.COUNTRIES:
            action = holidays.addAction(
                self.t("todo_country_" + code, name),
                lambda c=code: self.toggle_country(c))
            action.setCheckable(True)
            action.setChecked(code in self.calendar.countries)
            self._holiday_actions[code] = action
        holidays.addSeparator()
        self._holiday_show = holidays.addAction(
            self.t("todo_show_holidays", "Show holidays in the grid"),
            self.toggle_show_holidays)
        self._holiday_show.setCheckable(True)
        self._holiday_show.setChecked(bool(self.setting("show_holidays", True)))

        display = menu.addMenu(self.t("todo_display", "Display"))
        for key, label_key, fallback in (
                ("show_weekends", "todo_show_weekends", "Show weekends"),
                ("show_week_numbers", "todo_show_week_numbers", "Week numbers"),
                ("show_done", "todo_show_done", "Show completed tasks"),
                ("start_monday", "todo_start_monday", "Week starts on Monday")):
            action = display.addAction(self.t(label_key, fallback),
                                       lambda k=key: self.toggle_setting(k))
            action.setCheckable(True)
            action.setChecked(bool(self.setting(key, True)))
            setattr(self, "_setting_action_" + key, action)

        work = menu.addMenu(self.t("todo_work_hours", "Working hours"))
        for label_key, fallback, key, low, high in (
                ("todo_work_start", "Day starts at", "work_start", 0, 23),
                ("todo_work_end", "Day ends at", "work_end", 1, 24)):
            row = QtWidgets.QWidget()
            layout = QtWidgets.QHBoxLayout(row)
            layout.setContentsMargins(8, 2, 8, 2)
            layout.addWidget(QtWidgets.QLabel(self.t(label_key, fallback)))
            spin = QtWidgets.QSpinBox()
            spin.setRange(low, high)
            spin.setValue(int(self.setting(key, 8 if key == "work_start" else 18)))
            spin.valueChanged.connect(
                lambda value, k=key: self._set_work_hour(k, value))
            layout.addWidget(spin)
            widget_action = QtWidgets.QWidgetAction(menu)
            widget_action.setDefaultWidget(row)
            work.addAction(widget_action)

        menu.addSeparator()
        menu.addAction(self.t("todo_review", "Review and statistics..."),
                       self.show_review)
        menu.addAction(self.t("todo_organise", "Sort and organise..."),
                       self.show_organise)

        export = menu.addMenu(self.t("todo_export", "Export"))
        export.addAction(self.t("todo_export_ics", "Calendar (.ics)"),
                         lambda: self.export_ics())
        export.addAction(self.t("todo_export_md", "Markdown report (.md)"),
                         lambda: self.export_markdown())
        export.addAction(self.t("todo_export_json", "All tasks (.json)"),
                         lambda: self.export_json())
        export.addAction(self.t("todo_export_csv", "Tasks as CSV (.csv)"),
                         lambda: self.export_csv())

        import_menu = menu.addMenu(self.t("todo_import", "Import"))
        import_menu.addAction(self.t("todo_import_ics", "Calendar (.ics)"),
                              lambda: self.import_ics())

        menu.addSeparator()
        menu.addAction(self.t("todo_clear_done", "Clear completed tasks"),
                       self.clear_completed)

        notifications = menu.addMenu(self.t("todo_notifications",
                                            "Reminders and notifications"))
        self._notify_action = notifications.addAction(
            self.t("todo_notify_on", "Show reminder popups"),
            self.toggle_notifications)
        self._notify_action.setCheckable(True)
        self._notify_action.setChecked(bool(self.setting("notifications", True)))
        notifications.addAction(self.t("todo_notify_test", "Show a test popup"),
                                self.test_notification)
        notifications.addAction(self.t("todo_notify_history",
                                       "Notification history..."),
                                self.show_notification_history)
        return menu

    def _build_calendar_side(self):
        frame = QtWidgets.QFrame()
        frame.setFrameShape(QtWidgets.QFrame.StyledPanel)
        # The calendar is the main pane; its minimum is what a month grid needs
        # to stay readable (seven columns must not go below ~50px each).
        frame.setMinimumWidth(380)
        layout = QtWidgets.QVBoxLayout(frame)
        layout.setContentsMargins(8, 8, 8, 8)

        self.stack = QtWidgets.QStackedWidget()
        self.today_view = TodayView(self)
        self.day_view = DayTimeline(self)
        self.week_view = QtWidgets.QWidget()
        self.week_grid = QtWidgets.QHBoxLayout(self.week_view)
        self.week_grid.setContentsMargins(0, 0, 0, 0)
        self.week_grid.setSpacing(5)
        self.month_view = MonthView(self)
        self.year_view = YearView(self)

        for widget in (self.today_view, self.day_view, self.week_view,
                       self.month_view, self.year_view):
            self.stack.addWidget(widget)
        layout.addWidget(self.stack, 1)
        self._view_order = ("today", "day", "week", "month", "year")
        return frame

    def _build_list_side(self):
        frame = QtWidgets.QFrame()
        frame.setFrameShape(QtWidgets.QFrame.StyledPanel)
        # The task list can be dragged narrow (or shut): the calendar is the
        # part that must stay legible, so this is the flexible column.
        frame.setMinimumWidth(250)
        layout = QtWidgets.QVBoxLayout(frame)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        head = QtWidgets.QHBoxLayout()
        caption = QtWidgets.QLabel(self.t("todo_list", "Tasks"))
        caption.setStyleSheet("font-weight: bold;")
        head.addWidget(caption)
        head.addStretch()
        self.list_count = QtWidgets.QLabel("")
        self.list_count.setProperty("role", "hint")
        head.addWidget(self.list_count)
        layout.addLayout(head)

        self.task_list = QtWidgets.QListWidget()
        # A minimum width keeps the title and its date readable; without one
        # the splitter squeezes this column to a strip and every row needs a
        # horizontal scrollbar just to be read.
        self.task_list.setMinimumWidth(230)
        self.task_list.setWordWrap(True)
        self.task_list.setTextElideMode(QtCore.Qt.ElideRight)
        self.task_list.setSelectionMode(
            QtWidgets.QAbstractItemView.ExtendedSelection)
        self.task_list.itemSelectionChanged.connect(self._on_list_selection)
        self.task_list.itemDoubleClicked.connect(
            lambda item: self.open_task(item.data(QtCore.Qt.UserRole)))
        self.task_list.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.task_list.customContextMenuRequested.connect(self._list_menu)
        layout.addWidget(self.task_list, 1)

        quick = QtWidgets.QHBoxLayout()
        self.complete_btn = QtWidgets.QPushButton(self.t("todo_done", "Done"))
        self.complete_btn.clicked.connect(self.complete_selected)
        self.editor_btn = QtWidgets.QPushButton(self.t("todo_edit", "Edit"))
        self.editor_btn.clicked.connect(
            lambda: self.open_task(self._selected_id()))
        quick.addWidget(self.complete_btn)
        quick.addWidget(self.editor_btn)
        layout.addLayout(quick)

        self.summary_label = QtWidgets.QLabel("")
        self.summary_label.setProperty("role", "hint")
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)
        return frame

    def _build_editor_side(self):
        # Keep one editor instance alive and scroll its complete form instead
        # of rebuilding or clipping the Editor tab when the window is short.
        self.editor = TaskEditor(self)
        self.editor_scroll = TaskEditorScrollArea(self.editor, self)
        self.editor_scroll.setMinimumWidth(330)
        return self.editor_scroll

    # ------------------------------------------------------------- rendering
    def refresh(self):
        """Rebuild every visible part from the store."""
        if not hasattr(self, "stack"):
            return
        if self.view in self.view_buttons:
            button = self.view_buttons[self.view]
            if not button.isChecked():
                button.blockSignals(True)
                button.setChecked(True)
                button.blockSignals(False)
        self.stack.setCurrentIndex(self._view_order.index(self.view)
                                   if self.view in self._view_order else 3)
        self.period_label.setText(self._period_text())
        self._refresh_combos()
        self._rebuild_calendar()
        self.refresh_list()
        self._refresh_summary()
        self._refresh_holiday_label()
        self.next_holiday_label.setText(self._holiday_countdown_text())

    def _rebuild_calendar(self):
        if self.view == "today":
            self.today_view.rebuild()
        elif self.view == "day":
            self.day_view.rebuild()
        elif self.view == "week":
            self._rebuild_week()
        elif self.view == "month":
            self.month_view.rebuild()
        elif self.view == "year":
            self.year_view.rebuild()

    def _rebuild_week(self):
        while self.week_grid.count():
            item = self.week_grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        days = core.week_days(self.anchor, self.start_monday())
        if not self.show_weekends():
            days = [day for day in days if day.weekday() < 5]
        for day in days:
            self.week_grid.addWidget(DayColumn(self, day), 1)

    def _period_text(self):
        if self.view == "today":
            return "%s, %s" % (core.day_name(dt.date.today(), self.language()),
                               dt.date.today().strftime("%d.%m.%Y"))
        if self.view == "day":
            return "%s, %s" % (core.day_name(self.anchor, self.language()),
                               self.anchor.strftime("%d.%m.%Y"))
        if self.view == "week":
            days = core.week_days(self.anchor, self.start_monday())
            return core.week_label(days, self.language())
        if self.view == "month":
            return core.month_name(self.anchor.year, self.anchor.month,
                                   self.language())
        return str(self.anchor.year)

    def _refresh_combos(self):
        """Refill the project and tag filters without losing the selection."""
        for combo, values, current, label in (
                (self.project_combo, self.store.projects(), self._filters["project"],
                 self.t("todo_all_projects", "All projects")),
                (self.tag_combo, self.store.all_tags(), self._filters["tag"],
                 self.t("todo_all_tags", "All tags"))):
            combo.blockSignals(True)
            combo.clear()
            combo.addItem(label, None)
            for name, count in values:
                combo.addItem("%s (%d)" % (name, count), name)
            index = combo.findData(current)
            combo.setCurrentIndex(index if index >= 0 else 0)
            combo.blockSignals(False)

        if self.filter_combo.count() == 0:
            for key, label in (("all", "All open"), ("status:doing", "In progress"),
                               ("status:blocked", "Blocked"),
                               ("priority:critical", "Critical"),
                               ("priority:high", "High"),
                               ("kind:bug", "Bugs"),
                               ("kind:code", "Code"),
                               ("kind:review", "Reviews"),
                               ("kind:meeting", "Meetings"),
                ("done", "Completed")):
                self.filter_combo.addItem(self.t("todo_filter_" + key.replace(":", "_"),
                                                 label), key)
        self.filter_combo.blockSignals(True)
        self.filter_combo.setCurrentIndex(max(
            0, self.filter_combo.findData(self._current_filter_key())))
        self.filter_combo.blockSignals(False)

    def _current_filter_key(self):
        for key in ("status", "priority", "kind"):
            value = self._filters.get(key)
            if value:
                return "%s:%s" % (key, value)
        if not self.setting("show_done", False) and self._filters.get("query"):
            pass
        return "done" if self.setting("show_done", False) else "all"

    # ------------------------------------------------------------- the list
    def refresh_list(self):
        if not hasattr(self, "task_list"):
            return
        self.task_list.blockSignals(True)
        self.task_list.clear()
        tasks = self.filtered_tasks()
        tasks.sort(key=lambda task: (
            task.done,
            task.due_moment() is None,
            task.due_moment() or dt.datetime.max,
            {name: index for index, name in enumerate(core.PRIORITIES)}.get(
                task.get("priority"), 9),
            task.title.lower()))
        for task in tasks:
            item = QtWidgets.QListWidgetItem(self._list_text(task))
            item.setData(QtCore.Qt.UserRole, task.id)
            item.setForeground(QtGui.QColor(task.effective_color()
                                            if not task.done else "#9aa0a6"))
            if task.overdue:
                font = item.font()
                font.setBold(True)
                item.setFont(font)
            item.setToolTip(self._list_tooltip(task))
            self.task_list.addItem(item)
            if task.id == self.selection:
                self.task_list.setCurrentItem(item)
        self.task_list.blockSignals(False)
        self.list_count.setText(str(len(tasks)))

    def _list_text(self, task):
        mark = "\u2713" if task.done else (
            "\u25cf" if task.get("status") == "doing" else "\u25a1")
        parts = [mark, task.title]
        moment = task.due_moment()
        if moment is not None:
            parts.append(moment.strftime("%d.%m %H:%M"))
        if task.get("project"):
            parts.append("[" + task.get("project") + "]")
        return "  ".join(parts)

    def _list_tooltip(self, task):
        lines = [task.title]
        if task.get("notes"):
            lines.append(task.get("notes")[:400])
        done, total = task.checklist_progress()
        if total:
            lines.append("Checklist %d/%d" % (done, total))
        return "\n".join(lines)

    def _on_list_selection(self):
        task = self._selected_task()
        if task is not None:
            self.selection = task.id
            self.editor.load(task)

    def _selected_id(self):
        item = self.task_list.currentItem()
        return item.data(QtCore.Qt.UserRole) if item is not None else None

    def _selected_task(self):
        task_id = self._selected_id()
        return self.store.get(task_id) if task_id else None

    def selected_tasks(self):
        out = []
        for item in self.task_list.selectedItems():
            task = self.store.get(item.data(QtCore.Qt.UserRole))
            if task is not None:
                out.append(task)
        return out

    def _list_menu(self, point):
        tasks = self.selected_tasks()
        if not tasks:
            menu = self._new_menu()
            menu.exec_(self.task_list.viewport().mapToGlobal(point))
            return
        if len(tasks) == 1:
            menu = self.build_task_menu(tasks[0])
        else:
            menu = self._multi_menu(tasks)
        menu.exec_(self.task_list.viewport().mapToGlobal(point))

    def _multi_menu(self, tasks):
        menu = QtWidgets.QMenu(self)
        caption = self.t("todo_selected", "{count} tasks selected",
                         count=len(tasks))
        header = menu.addAction(caption)
        header.setEnabled(False)
        menu.addSeparator()
        menu.addAction(self.t("todo_done", "Done"),
                       lambda: self.complete_tasks([t.id for t in tasks]))
        menu.addAction(self.t("todo_mark_todo", "Mark as to do"),
                       lambda: self.set_status_many(tasks, "todo"))
        status = menu.addMenu(self.t("todo_set_status", "Set status"))
        for name in core.STATUSES:
            status.addAction(self.t("todo_status_" + name, name),
                             lambda s=name: self.set_status_many(tasks, s))
        priority = menu.addMenu(self.t("todo_set_priority", "Set priority"))
        for name in core.PRIORITIES:
            priority.addAction(self.t("todo_priority_" + name, name),
                               lambda p=name: self.set_priority_many(tasks, p))
        move = menu.addMenu(self.t("todo_snooze", "Snooze"))
        for label, minutes in (("10 min", 10), ("1 h", 60), ("1 day", 1440)):
            move.addAction(self.t("todo_snooze_for", "Snooze {what}", what=label),
                           lambda m=minutes: self.snooze_many(tasks, m))
        menu.addSeparator()
        menu.addAction(self.t("todo_copy", "Copy as text"),
                       lambda: self.copy_tasks(tasks))
        menu.addAction(self.t("todo_delete", "Delete"),
                       lambda: self.delete_tasks([t.id for t in tasks]))
        return menu

    def build_task_menu(self, task):
        """The menu for one task, shared by the chips and the list."""
        menu = QtWidgets.QMenu(self)
        header = menu.addAction(task.title)
        header.setEnabled(False)
        menu.addSeparator()
        if task.done:
            menu.addAction(self.t("todo_reopen", "Reopen"),
                           lambda: self.toggle_task(task.id))
        else:
            menu.addAction(self.t("todo_done", "Done"),
                           lambda: self.toggle_task(task.id))
        menu.addAction(self.t("todo_edit", "Edit"),
                       lambda: self.open_task(task.id))
        menu.addAction(self.t("todo_start_working", "Start working on it"),
                       lambda: self.start_working(task.id))

        menu.addSeparator()
        status = menu.addMenu(self.t("todo_set_status", "Set status"))
        for name in core.STATUSES:
            action = status.addAction(self.t("todo_status_" + name, name),
                                      lambda _c=False, s=name, t=task:
                                      self.set_status(t.id, s))
            action.setCheckable(True)
            action.setChecked(task.get("status") == name)

        priority = menu.addMenu(self.t("todo_set_priority", "Set priority"))
        for name in core.PRIORITIES:
            action = priority.addAction(self.t("todo_priority_" + name, name),
                                        lambda _c=False, p=name, t=task:
                                        self.set_priority(t.id, p))
            action.setCheckable(True)
            action.setChecked(task.get("priority") == name)

        move = menu.addMenu(self.t("todo_move", "Move to day"))
        for label, days in (("Today", 0), ("Tomorrow", 1),
                            ("+1 week", 7), ("+1 month", 30)):
            move.addAction(self.t("todo_move_" + str(days), label),
                           lambda d=days, t=task: self.move_task(t.id, d))

        snooze = menu.addMenu(self.t("todo_snooze", "Snooze"))
        for minutes, label in ((10, "10 min"), (60, "1 h"),
                               (240, "4 h"), (1440, "1 day")):
            snooze.addAction(self.t("todo_snooze_for", "Snooze {what}",
                                    what=label),
                             lambda m=minutes, t=task: self.snooze(t.id, m))

        menu.addSeparator()
        menu.addAction(self.t("todo_duplicate", "Duplicate"),
                       lambda: self.duplicate_task(task.id))
        menu.addAction(self.t("todo_copy", "Copy as text"),
                       lambda: self.copy_tasks([task]))
        menu.addAction(self.t("todo_send_docs", "Send to DOCs"),
                       lambda: self.send_to_docs(task))
        menu.addAction(self.t("todo_send_editor", "Open in the Editor"),
                       lambda: self.send_to_editor(task))
        menu.addSeparator()
        menu.addAction(self.t("todo_delete", "Delete"),
                       lambda: self.delete_task(task.id))
        return menu
    # ----------------------------------------------------------- mutations
    def new_task(self):
        """Start a blank task in the editor panel."""
        self.editor.new_task()

    def new_task_on(self, day, hour=None):
        """Start a task anchored on a calendar day."""
        self.editor.new_task(day, hour)

    def new_from_template(self, entry):
        """Create a task from one of the ready-made templates."""
        task = self.store.add(
            title=entry["title"], notes=entry["notes"], kind=entry["kind"],
            priority=entry["priority"], due=entry.get("due", ""),
            remind=int(self.setting("default_remind", 30)))
        self.save()
        self.selection = task.id
        self.refresh()
        self.editor.load(task)

    def save_task(self, task_id, values):
        """Create or update a task from the editors values."""
        if task_id is None:
            task = self.store.add(**values)
        else:
            task = self.store.get(task_id)
            if task is None:
                return None
            task.update(values)
            task["updated"] = core.now_stamp()
        self.save()
        self.selection = task.id
        self.refresh()
        self.announce("todo_saved", "saved", task)
        return task

    def open_task(self, task_id):
        """Show a task in the editor and make it the current selection."""
        task = self.store.get(task_id)
        if task is None:
            return
        self.selection = task.id
        self.editor.load(task)
        self._select_in_list(task.id)

    def _select_in_list(self, task_id):
        for row in range(self.task_list.count()):
            item = self.task_list.item(row)
            if item.data(QtCore.Qt.UserRole) == task_id:
                self.task_list.blockSignals(True)
                self.task_list.setCurrentItem(item)
                self.task_list.blockSignals(False)
                return

    def toggle_task(self, task_id):
        """Complete or reopen a task; a repeat creates its successor."""
        task = self.store.toggle_done(task_id)
        self.save()
        self.refresh()
        if task is not None:
            if task.done:
                self.announce("todo_completed", "completed", task, kind="done")
            else:
                self.announce("todo_reopened", "reopened", task)
        return task

    def complete_selected(self):
        self.complete_tasks([task.id for task in self.selected_tasks()])

    def complete_tasks(self, task_ids):
        if not task_ids:
            return 0
        done = 0
        for task_id in task_ids:
            task = self.store.get(task_id)
            if task is None or task.done:
                continue
            self.store.toggle_done(task_id)
            done += 1
        self.save()
        self.refresh()
        if done:
            self.status.setText(self.t("todo_completed_n",
                                       "completed {count} task(s)", count=done))
        return done

    def delete_task(self, task_id):
        task = self.store.get(task_id)
        if task is None:
            return 0
        if QtWidgets.QMessageBox.question(
                self, self.t("todo_delete", "Delete"),
                self.t("todo_delete_ask", "Delete \"{title}\"?",
                       title=task.title)) != QtWidgets.QMessageBox.Yes:
            return 0
        return self.delete_tasks([task_id])

    def delete_tasks(self, task_ids):
        removed = 0
        for task_id in task_ids:
            removed += self.store.remove(task_id)
        if removed:
            self.save()
            if self.selection in task_ids:
                self.selection = None
                self.editor.new_task()
            self.refresh()
            self.status.setText(self.t("todo_deleted_n", "deleted {count}",
                                       count=removed))
        return removed

    def duplicate_task(self, task_id):
        task = self.store.get(task_id)
        if task is None:
            return None
        values = dict(task.to_dict())
        values.pop("id", None)
        values["title"] = task.title + " (copy)"
        values["created"] = core.now_stamp()
        values["updated"] = values["created"]
        values["reminded"] = ""
        clone = self.store.add(**values)
        self.save()
        self.refresh()
        self.open_task(clone.id)
        return clone

    def set_status(self, task_id, status):
        task = self.store.set_status(task_id, status)
        if task is None:
            return None
        self.save()
        self.refresh()
        self.announce("todo_status_set", "status", task)
        return task

    def set_status_many(self, tasks, status):
        return self.store.move_all([task.id for task in tasks], status=status) \
            and self._after_bulk()

    def set_priority(self, task_id, priority):
        task = self.store.get(task_id)
        if task is None:
            return None
        task["priority"] = priority
        task["updated"] = core.now_stamp()
        self.save()
        self.refresh()
        return task

    def set_priority_many(self, tasks, priority):
        self.store.move_all([task.id for task in tasks], priority=priority)
        return self._after_bulk()

    def _after_bulk(self):
        self.save()
        self.refresh()
        return True

    def start_working(self, task_id):
        """Mark a task as in progress -- the "I am on it now" action."""
        return self.set_status(task_id, "doing")

    def snooze(self, task_id, minutes):
        task = self.store.snooze(task_id, minutes)
        if task is None:
            return None
        self.save()
        self.refresh()
        self.status.setText(self.t("todo_snoozed", "snoozed until {when}",
                                   when=task.get("due")))
        return task

    def snooze_many(self, tasks, minutes):
        for task in tasks:
            self.store.snooze(task.id, minutes)
        return self._after_bulk()

    def move_task(self, task_id, days):
        task = self.store.postpone(task_id, days)
        if task is None:
            return None
        self.save()
        self.refresh()
        return task

    def move_task_to_day(self, task_id, day):
        """Drop a task onto another calendar day (drag and drop)."""
        task = self.store.get(task_id)
        if task is None:
            return None
        moment = task.due_moment() or dt.datetime.combine(
            day, dt.time(9, 0))
        due = dt.datetime.combine(day, moment.time())
        task["due"] = due.replace(microsecond=0).isoformat(" ")
        task["reminded"] = ""
        task["updated"] = core.now_stamp()
        self.save()
        self.refresh()
        self.status.setText(self.t("todo_moved", "moved to {when}",
                                   when=day.strftime("%d.%m.%Y")))
        return task

    def clear_completed(self):
        count = sum(1 for task in self.store.tasks if task.done)
        if not count:
            self.status.setText(self.t("todo_nothing_completed",
                                       "no completed tasks to clear"))
            return 0
        if QtWidgets.QMessageBox.question(
                self, self.t("todo_clear_done", "Clear completed tasks"),
                self.t("todo_clear_done_ask",
                       "Remove {count} completed task(s)?", count=count)) \
                != QtWidgets.QMessageBox.Yes:
            return 0
        removed = self.store.clear_completed()
        self.save()
        self.refresh()
        self.status.setText(self.t("todo_cleared", "removed {count} task(s)",
                                   count=removed))
        return removed
    # ------------------------------------------------------------ reminders
    def _start_reminders(self):
        """Begin the reminder loop: one timer for the whole tab."""
        if self._reminder_timer is not None:
            return
        self._reminder_timer = QtCore.QTimer(self)
        self._reminder_timer.setInterval(20000)      # every twenty seconds
        self._reminder_timer.timeout.connect(self.check_reminders)
        self._reminder_timer.start()
        QtCore.QTimer.singleShot(1500, self.check_reminders)

    def check_reminders(self):
        """Fire every reminder that has come due since the last check.

        A reminder is fired once: the store records the due moment it was sent
        for, so the next tick does not repeat it, while moving the task does.
        """
        if not available_reminders_enabled(self):
            return 0
        due = self.store.due_reminders()
        fired = 0
        for task in due:
            self.store.mark_reminded(task.id)
            self._post_reminder(task)
            fired += 1
        if fired:
            self.save()
            self.refresh()
        return fired

    def _post_reminder(self, task):
        """Show one reminder popup, with Snooze and Done on it."""
        when = ""
        moment = task.due_moment()
        if moment is not None:
            when = moment.strftime("%H:%M") + "  " + core.human_delta(moment)
        body = (task.get("notes") or "").splitlines()[:2]
        text = when
        if body:
            text = (when + "\n" if when else "") + " ".join(body)[:160]
        actions = [
            (self.t("todo_snooze_10", "Snooze 10 min"),
             lambda tid=task.id: self.snooze(tid, 10)),
            (self.t("todo_done", "Done"),
             lambda tid=task.id: self.toggle_task(tid)),
        ]
        self.notifier.notify(
            self.t("todo_reminder_title", "Reminder: {title}", title=task.title),
            text, kind="reminder",
            seconds=int(self.setting("notify_seconds", 8)),
            actions=actions,
            on_click=lambda tid=task.id: self.open_task(tid))

    def test_notification(self):
        """Show a sample popup, so the user can see where they appear."""
        self.notifier.notify(
            self.t("todo_test_title", "This is a test reminder"),
            self.t("todo_test_body",
                   "Reminders look like this and appear over every tab."),
            kind="reminder", seconds=6,
            actions=[(self.t("todo_close", "Close"), lambda: None)])

    def toggle_notifications(self):
        enabled = not bool(self.setting("notifications", True))
        self.set_setting("notifications", enabled)
        self.status.setText(
            self.t("todo_notify_enabled", "reminder popups: {state}",
                   state=self.t("todo_on", "on") if enabled
                   else self.t("todo_off", "off")))
        self._notify_action.setChecked(enabled)

    def refresh_reminders(self):
        """Re-arm the timers if the tab is re-shown after a long pause."""
        self._start_reminders()
        self.check_reminders()

    def show_notification_history(self):
        """Show what has been announced, newest first."""
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle(self.t("todo_notify_history",
                                     "Notification history"))
        dialog.resize(560, 420)
        layout = QtWidgets.QVBoxLayout(dialog)
        table = QtWidgets.QTreeWidget()
        table.setAlternatingRowColors(True)
        table.setColumnCount(3)
        table.setHeaderLabels([
            self.t("todo_notify_when", "When"),
            self.t("todo_notify_kind", "Kind"),
            self.t("todo_notify_what", "What")])
        for entry in self.notifier.entries:
            item = QtWidgets.QTreeWidgetItem([
                entry.get("when", ""), entry.get("kind", ""),
                entry.get("title", "")])
            if not entry.get("shown"):
                item.setForeground(2, QtGui.QColor("#9aa0a6"))
            table.addTopLevelItem(item)
        layout.addWidget(table, 1)
        buttons = QtWidgets.QHBoxLayout()
        clear = QtWidgets.QPushButton(self.t("todo_clear_log", "Clear"))
        clear.clicked.connect(lambda: (self.notifier.clear_history(),
                                       table.clear()))
        close = QtWidgets.QPushButton(self.t("todo_close", "Close"))
        close.clicked.connect(dialog.accept)
        buttons.addWidget(clear)
        buttons.addStretch()
        buttons.addWidget(close)
        layout.addLayout(buttons)
        dialog.exec_()

    # ------------------------------------------------------- integration
    def announce(self, key, fallback, task, kind="info"):
        """Tell the audit bus about a change, so the timeline records it."""
        notifier = getattr(self.window, "notify_audit", None)
        if callable(notifier):
            try:
                notifier("file_changed", where=task.title if task else "",
                         detail=self.t(key, fallback))
            except Exception:
                pass

    def follow_link(self, text):
        """Open one of a tasks links, wherever it points."""
        text = (text or "").strip()
        if not text:
            return
        if text.startswith(("http://", "https://")):
            QtGui.QDesktopServices.openUrl(QtCore.QUrl(text))
            return
        if os.path.exists(text):
            editor = getattr(self.window, "editor_tab", None)
            tabs = getattr(self.window, "tabs", None)
            if editor is not None and hasattr(editor, "load_file"):
                try:
                    editor.load_file(text)
                except Exception:
                    QtGui.QDesktopServices.openUrl(
                        QtCore.QUrl.fromLocalFile(text))
                if tabs is not None:
                    tabs.setCurrentWidget(editor)
            else:
                QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(text))
            return
        if text.startswith("#") or text.startswith("script:"):
            name = text.split(":", 1)[-1].lstrip("#").strip()
            finder = getattr(self.window, "find_script", None)
            scripts = getattr(self.window, "scripts_only", None)
            match = None
            if callable(scripts):
                match = next((script for script in scripts()
                              if script.get("name", "").lower() == name.lower()),
                             None)
            if match is not None:
                opener = getattr(self.window, "open_script_in_editor", None)
                tabs = getattr(self.window, "tabs", None)
                if callable(opener):
                    opener(match)
                if tabs is not None:
                    editor = getattr(self.window, "editor_tab", None)
                    if editor is not None:
                        tabs.setCurrentWidget(editor)
                return
        if callable(getattr(self.window, "find_script", None)):
            script = self.window.find_script(text)
            if script is not None:
                return
        self.status.setText(self.t("todo_link_unresolved",
                                   "cannot open: {what}", what=text))

    def send_to_docs(self, task):
        """Store the task as a document in the DOCs repository."""
        docs = getattr(self.window, "docs_tab", None)
        tabs = getattr(self.window, "tabs", None)
        if docs is None or getattr(docs, "repo", None) is None:
            self.status.setText(self.t("todo_needs_docs",
                                       "the DOCs tab has no open repository"))
            return None
        try:
            body = core.task_to_markdown(task)
            doc_id = docs.repo.add(task.title, body, "project",
                                   self.t("todo_docs_category", "ToDo"))
        except Exception as exc:
            self.status.setText(str(exc))
            return None
        docs.refresh()
        if tabs is not None:
            tabs.setCurrentWidget(docs)
        self.status.setText(self.t("todo_sent_docs", "stored in DOCs"))
        return doc_id

    def send_to_editor(self, task):
        """Open the task text in the Editor tab as a scratch script."""
        editor = getattr(self.window, "editor_tab", None)
        tabs = getattr(self.window, "tabs", None)
        if editor is None or not hasattr(editor, "load_script"):
            self.status.setText(self.t("todo_needs_editor",
                                       "the Editor tab is unavailable"))
            return
        body = core.task_to_markdown(task)
        editor.load_script({"id": None, "name": task.title, "category": "Other",
                            "code": body, "tags": "todo",
                            "desc": self.t("todo_scratch", "a ToDo task")})
        if tabs is not None:
            tabs.setCurrentWidget(editor)

    def copy_tasks(self, tasks):
        """Put the tasks on the clipboard as Markdown."""
        text = "\n\n".join(core.task_to_markdown(task) for task in tasks)
        QtWidgets.QApplication.clipboard().setText(text)
        self.status.setText(self.t("todo_copied", "copied {count} task(s)",
                                   count=len(tasks)))

    def export_ics(self):
        """Write the calendar as an .ics file any calendar app can read."""
        suggested = self._export_name("todo.ics")
        path, _filter = QtWidgets.QFileDialog.getSaveFileName(
            self, self.t("todo_export_ics", "Calendar (.ics)"), suggested,
            "iCalendar (*.ics);;All files (*)")
        if not path:
            return ""
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(core.tasks_to_ics(self.store.tasks))
        self.status.setText(self.t("todo_exported", "written: {path}", path=path))
        return path

    def import_ics(self):
        """Merge an .ics file into the task list."""
        path, _filter = QtWidgets.QFileDialog.getOpenFileName(
            self, self.t("todo_import_ics", "Calendar (.ics)"), "",
            "iCalendar (*.ics);;All files (*)")
        if not path:
            return 0
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except OSError as exc:
            self.status.setText(str(exc))
            return 0
        added = 0
        for record in core.parse_ics(text):
            record.setdefault("remind", int(self.setting("default_remind", 30)))
            self.store.add(**record)
            added += 1
        if added:
            self.save()
            self.refresh()
        self.status.setText(self.t("todo_imported", "imported {count}",
                                   count=added))
        return added

    def export_markdown(self):
        path, _filter = QtWidgets.QFileDialog.getSaveFileName(
            self, self.t("todo_export_md", "Markdown report (.md)"),
            self._export_name("todo_report.md"), "Markdown (*.md);;All files (*)")
        if not path:
            return ""
        tasks = self.filtered_tasks()
        lines = ["# " + self.t("todo_report_title", "ToDo report"), "",
                 self.t("todo_report_generated", "Generated {when}",
                        when=core.now_stamp()), "",
                 self.t("todo_report_summary", "{count} task(s)", count=len(tasks)),
                 ""]
        for task in tasks:
            lines.append(core.task_to_markdown(task))
            lines.append("")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines))
        self.status.setText(self.t("todo_exported", "written: {path}", path=path))
        return path

    def export_json(self):
        path, _filter = QtWidgets.QFileDialog.getSaveFileName(
            self, self.t("todo_export_json", "All tasks (.json)"),
            self._export_name("todo.json"), "JSON (*.json);;All files (*)")
        if not path:
            return ""
        import json
        with open(path, "w", encoding="utf-8") as handle:
            json.dump([task.to_dict() for task in self.store.tasks], handle,
                      ensure_ascii=False, indent=2)
        self.status.setText(self.t("todo_exported", "written: {path}", path=path))
        return path

    def export_csv(self):
        path, _filter = QtWidgets.QFileDialog.getSaveFileName(
            self, self.t("todo_export_csv", "Tasks as CSV (.csv)"),
            self._export_name("todo.csv"), "CSV (*.csv);;All files (*)")
        if not path:
            return ""
        import csv
        fields = ("title", "status", "priority", "kind", "project", "due",
                  "start", "repeat", "tags", "notes")
        with open(path, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields,
                                    extrasaction="ignore")
            writer.writeheader()
            for task in self.store.tasks:
                row = task.to_dict()
                row["tags"] = ", ".join(task.tag_list())
                writer.writerow(row)
        self.status.setText(self.t("todo_exported", "written: {path}", path=path))
        return path

    def _export_name(self, name):
        return os.path.join(self.base_dir, name)

    def save_ics(self, path=None):
        """Head-less .ics export, used by tests and by other tabs."""
        path = path or self._export_name("todo.ics")
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(core.tasks_to_ics(self.store.tasks))
        return path
    # ------------------------------------------------------ review & tools
    def show_review(self):
        """Statistics, a work log and the holiday list, in one window."""
        stats = self.store.statistics(self.calendar)
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle(self.t("todo_review", "Review and statistics"))
        dialog.resize(880, 600)
        layout = QtWidgets.QVBoxLayout(dialog)

        header = QtWidgets.QLabel(self.t("todo_review_head",
                                         "ToDo review"))
        header.setProperty("role", "title")
        layout.addWidget(header)

        cards = QtWidgets.QHBoxLayout()
        for value, label in (
                (stats["total"], self.t("todo_stat_total", "Tasks")),
                (stats["open"], self.t("todo_stat_open", "Open")),
                (stats["done"], self.t("todo_stat_done", "Done")),
                (stats["overdue"], self.t("todo_stat_late", "Late")),
                (stats["done_last_week"],
                 self.t("todo_stat_week", "Done this week"))):
            card = QtWidgets.QFrame()
            card.setFrameShape(QtWidgets.QFrame.StyledPanel)
            card_layout = QtWidgets.QVBoxLayout(card)
            number = QtWidgets.QLabel(str(value))
            number.setStyleSheet("font-size: 20px; font-weight: bold;")
            caption = QtWidgets.QLabel(label)
            caption.setProperty("role", "hint")
            card_layout.addWidget(number)
            card_layout.addWidget(caption)
            cards.addWidget(card, 1)
        layout.addLayout(cards)

        columns = QtWidgets.QHBoxLayout()

        left = QtWidgets.QTreeWidget()
        left.setColumnCount(2)
        left.setHeaderLabels([self.t("todo_review_by", "Group"),
                              self.t("todo_review_count", "Count")])
        for title, mapping in (
                (self.t("todo_review_project", "By project"), stats["by_project"]),
                (self.t("todo_review_kind", "By kind"), stats["by_kind"]),
                (self.t("todo_review_priority", "By priority"),
                 stats["by_priority"])):
            node = QtWidgets.QTreeWidgetItem([title, str(sum(mapping.values()))])
            node.setForeground(0, QtGui.QColor("#8ab4f8"))
            for name, count in sorted(mapping.items(),
                                      key=lambda item: -item[1]):
                node.addChild(QtWidgets.QTreeWidgetItem(
                    [self.t("todo_" + str(name), str(name)), str(count)]))
            left.addTopLevelItem(node)
        left.expandAll()
        columns.addWidget(left, 1)

        right = QtWidgets.QTreeWidget()
        right.setColumnCount(2)
        right.setHeaderLabels([self.t("todo_review_day", "Day"),
                               self.t("todo_review_load", "Load")])
        today = dt.date.today()
        for offset in range(0, 14):
            day = today + dt.timedelta(days=offset)
            count = self.day_load(day)
            holiday = self.calendar.label(day, self.language())
            label = "%s  %s" % (day.strftime("%d.%m (%a)"),
                                core.day_name(day, self.language())[:3])
            if holiday:
                label += "  \U0001f389 " + holiday
            item = QtWidgets.QTreeWidgetItem([label, str(count)])
            if count:
                item.setForeground(1, QtGui.QColor("#8ab4f8"))
            if holiday:
                item.setForeground(0, QtGui.QColor(HOLIDAY_COLOR))
            right.addTopLevelItem(item)
        columns.addWidget(right, 1)
        layout.addLayout(columns, 1)

        # The work log: what was finished, and when.
        log_box = QtWidgets.QGroupBox(self.t("todo_worklog", "Recently completed"))
        log_layout = QtWidgets.QVBoxLayout(log_box)
        log = QtWidgets.QListWidget()
        for record in self.store.completions[:14]:
            log.addItem("%s  %s" % (record.get("at", ""), record.get("title", "")))
        if not self.store.completions:
            log.addItem(self.t("todo_no_log", "nothing completed yet"))
        log_layout.addWidget(log)
        layout.addWidget(log_box)

        buttons = QtWidgets.QHBoxLayout()
        export = QtWidgets.QPushButton(self.t("todo_export_md",
                                              "Markdown report (.md)"))
        export.clicked.connect(self.export_markdown)
        close = QtWidgets.QPushButton(self.t("todo_close", "Close"))
        close.clicked.connect(dialog.accept)
        buttons.addWidget(export)
        buttons.addStretch()
        buttons.addWidget(close)
        layout.addLayout(buttons)
        dialog.exec_()

    def show_organise(self):
        """Bulk maintenance: overdue, duplicates, empty, archive."""
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle(self.t("todo_organise", "Sort and organise"))
        dialog.resize(560, 460)
        layout = QtWidgets.QVBoxLayout(dialog)

        def add(title_key, fallback, callback):
            button = QtWidgets.QPushButton(self.t(title_key, fallback))
            button.clicked.connect(callback)
            layout.addWidget(button)
            return button

        add("todo_org_overdue", "Push all overdue tasks to today",
            lambda: self._bulk_move_overdue())
        add("todo_org_untimed", "Give every task without a time a due hour",
            lambda: self._bulk_time_default())
        add("todo_org_project", "Assign the current project to every untagged task",
            lambda: self._bulk_assign_project())
        add("todo_org_dedupe", "Remove duplicate tasks (same title and day)",
            lambda: self._bulk_dedupe())
        add("todo_org_empty", "Remove tasks with no title",
            lambda: self._bulk_drop_empty())
        add("todo_org_archive", "Archive every completed task",
            lambda: self._bulk_archive())

        note = QtWidgets.QLabel(self.t(
            "todo_org_note",
            "Every action here is applied to the whole list and can be undone "
            "by closing without saving the data file."))
        note.setWordWrap(True)
        note.setProperty("role", "hint")
        layout.addWidget(note)
        layout.addStretch()

        close = QtWidgets.QPushButton(self.t("todo_close", "Close"))
        close.clicked.connect(dialog.accept)
        layout.addWidget(close)
        dialog.exec_()

    def _bulk_move_overdue(self):
        late = self.store.overdue()
        today = dt.datetime.now().replace(microsecond=0)
        for task in late:
            task["due"] = today.isoformat(" ")
            task["reminded"] = ""
        self.save()
        self.refresh()
        self.status.setText(self.t("todo_org_moved", "moved {count} task(s)",
                                   count=len(late)))

    def _bulk_time_default(self):
        touched = 0
        for task in self.store.tasks:
            due = task.due_date()
            if due is not None and task.due_moment().hour == 0 \
                    and task.due_moment().minute == 0:
                task["due"] = "%s 09:00" % due.isoformat()
                touched += 1
        self.save()
        self.refresh()
        self.status.setText(self.t("todo_org_touched", "changed {count}",
                                   count=touched))

    def _bulk_assign_project(self):
        project = self.project_combo.currentData()
        if not project:
            self.status.setText(self.t("todo_org_need_project",
                                       "pick a project filter first"))
            return
        touched = 0
        for task in self.store.tasks:
            if not task.get("project"):
                task["project"] = project
                touched += 1
        self.save()
        self.refresh()
        self.status.setText(self.t("todo_org_touched", "changed {count}",
                                   count=touched))

    def _bulk_dedupe(self):
        seen, doomed = set(), []
        for task in sorted(self.store.tasks, key=lambda item: item.get("created", "")):
            key = (task.title.lower(), (task.due_date() or "").__str__())
            if key in seen:
                doomed.append(task.id)
            else:
                seen.add(key)
        for task_id in doomed:
            self.store.remove(task_id)
        self.save()
        self.refresh()
        self.status.setText(self.t("todo_org_removed", "removed {count}",
                                   count=len(doomed)))

    def _bulk_drop_empty(self):
        doomed = [task.id for task in self.store.tasks
                  if not (task.get("title") or "").strip()]
        for task_id in doomed:
            self.store.remove(task_id)
        self.save()
        self.refresh()
        self.status.setText(self.t("todo_org_removed", "removed {count}",
                                   count=len(doomed)))

    def _bulk_archive(self):
        removed = self.store.clear_completed()
        self.save()
        self.refresh()
        self.status.setText(self.t("todo_org_removed", "removed {count}",
                                   count=removed))

    # ------------------------------------------------------------- filters
    def _on_search(self, text):
        self._filters["query"] = text
        self.refresh_list()

    def _on_filter_changed(self, _index):
        key = self.filter_combo.currentData()
        self._filters.update({"status": None, "priority": None, "kind": None})
        if key == "done":
            self.set_setting("show_done", True, save=False)
        elif key != "all":
            field, _, value = key.partition(":")
            self._filters[field] = value
        self.refresh_list()
        self._refresh_summary()

    def _on_project_changed(self, _index):
        self._filters["project"] = self.project_combo.currentData()
        self.refresh_list()

    def _on_tag_changed(self, _index):
        self._filters["tag"] = self.tag_combo.currentData()
        self.refresh_list()

    def reset_filters(self):
        """Return the task list filters to their default open-task view."""
        self._filters.update({"query": "", "project": None, "tag": None,
                              "kind": None, "priority": None, "status": None})
        self.set_setting("show_done", False, save=False)
        self.search_edit.blockSignals(True)
        self.search_edit.clear()
        self.search_edit.blockSignals(False)
        self._refresh_combos()
        self.refresh_list()
        self._refresh_summary()

    # ------------------------------------------------------------ controls
    def set_view(self, view):
        if view not in self._view_order:
            return
        self.view = view
        self.set_setting("view", view)
        self.refresh()

    def step(self, direction):
        """Move the shown period by one unit of the current view."""
        if self.view == "today":
            self.anchor = dt.date.today()
        elif self.view == "day":
            self.anchor += dt.timedelta(days=direction)
        elif self.view == "week":
            self.anchor += dt.timedelta(weeks=direction)
        elif self.view == "month":
            year, month = core.shift_month(self.anchor.year, self.anchor.month,
                                           direction)
            self.anchor = dt.date(year, month, 1)
        else:
            self.anchor = dt.date(self.anchor.year + direction,
                                  self.anchor.month, 1)
        self.refresh()

    def go_today(self):
        self.anchor = dt.date.today()
        if self.view == "year":
            self.view = "month"
        self.refresh()

    def goto_date(self, day):
        """Jump to a date (used by the calendar picker and by other tabs)."""
        self.anchor = day
        if self.view in ("today", "year"):
            self.view = "month"
        self.refresh()

    def toggle_country(self, code):
        countries = list(self.calendar.countries)
        if code in countries:
            if len(countries) > 1:
                countries.remove(code)
        else:
            countries.append(code)
        self.calendar = core.HolidayCalendar(countries)
        self.set_setting("countries", countries)
        self.refresh()
        if code in self._holiday_actions:
            self._holiday_actions[code].setChecked(code in countries)

    def toggle_show_holidays(self):
        self.toggle_setting("show_holidays")

    def toggle_setting(self, key):
        value = not bool(self.setting(key, True))
        self.set_setting(key, value)
        action = getattr(self, "_setting_action_" + key, None)
        if action is not None:
            action.setChecked(value)
        self.refresh()

    def _set_work_hour(self, key, value):
        self.set_setting(key, int(value))
        if self.view == "day":
            self.day_view.rebuild()

    def _refresh_summary(self):
        stats = self.store.statistics()
        parts = [
            self.t("todo_sum_open", "{count} open", count=stats["open"]),
            self.t("todo_sum_done", "{count} done", count=stats["done"]),
        ]
        if stats["overdue"]:
            parts.append(self.t("todo_sum_late", "{count} late",
                                count=stats["overdue"]))
        if stats["estimate"]:
            parts.append(self.t("todo_sum_estimate", "{count} min estimated",
                                count=stats["estimate"]))
        self.summary_label.setText("  \u00b7  ".join(parts))
        if hasattr(self, "clock_label"):
            self.clock_label.setText(core.now_stamp())

    def _refresh_holiday_label(self):
        if self.calendar.is_holiday(dt.date.today()):
            self.next_holiday_label.setText(
                "\U0001f389 " + self.calendar.label(dt.date.today(),
                                                    self.language()))

    def _holiday_countdown_text(self):
        days, day, _entry = self.calendar.countdown(today=dt.date.today())
        if days is None:
            return ""
        if days == 0:
            return "\U0001f389 " + self.calendar.label(day, self.language())
        name = self.calendar.label(day, self.language())
        return self.t("todo_holiday_in", "{days} day(s) to {name}",
                      days=days, name=name)

    # --------------------------------------------------------- open from out
    def add_script_task(self, script, note=""):
        """Create a task from a script, used by the other tabs menus."""
        title = self.t("todo_from_script", "Work on {name}",
                       name=script.get("name", ""))
        task = self.store.add(
            title=title, notes=note or script.get("desc", ""),
            kind="code", project=script.get("category", ""),
            tags=script.get("tags", "").split(",") if script.get("tags") else [],
            remind=int(self.setting("default_remind", 30)))
        self.save()
        self.refresh()
        self.open_task(task.id)
        return task

    def add_quick_task(self, title, **extra):
        """Add a task with nothing but a title (used by shortcuts/tests)."""
        values = {"title": title,
                  "remind": int(self.setting("default_remind", 30))}
        values.update(extra)
        task = self.store.add(**values)
        self.save()
        self.refresh()
        return task

    # ---------------------------------------------------------- translation
    def retranslate(self, table):
        """Apply the windows language table to this tabs controls."""
        def label(key, fallback):
            return table.get(key, fallback)

        self.title_label.setText(label("todo_title", "TO DO"))
        self.subtitle.setText(label("todo_subtitle",
                                    "Tasks, calendar and reminders"))
        self.search_edit.setPlaceholderText(label("todo_search",
                                                  "Search tasks..."))
        for key, button in self.view_buttons.items():
            button.setText(label("todo_view_" + key, key.title()))
        self.prev_btn.setToolTip(label("todo_prev", "Previous"))
        self.next_btn.setToolTip(label("todo_next", "Next"))
        self.today_btn.setText(label("todo_today", "Today"))
        self.add_btn.setText(label("todo_add_task", "+ Task"))
        self.tools_btn.setText(label("todo_tools", "Tools"))
        self.complete_btn.setText(label("todo_done", "Done"))
        self.editor_btn.setText(label("todo_edit", "Edit"))
        self.add_btn.setMenu(self._new_menu())
        self.tools_btn.setMenu(self._tools_menu())
        self.tabs.setTabText(0, label("todo_calendar", "Calendar"))
        self.tabs.setTabText(1, label("todo_tasks_tab", "Tasks"))
        self.tabs.setTabText(2, label("todo_editor_tab", "Editor"))
        self.tabs.setTabText(3, label("todo_plan_tab", "Plan"))
        self.tabs.setTabText(4, label("todo_notes_tab", "Notes"))
        self.tabs.setTabText(5, label("todo_expenses_tab", "Expenses"))
        self.tabs.setTabText(6, label("shopping", "Shopping"))
        self.shopping_tab.retranslate(table)
        self._filters["query"] = self.search_edit.text()
        self.refresh()

    # ------------------------------------------------------------ lifecycle
    def showEvent(self, event):
        """Catch up on reminders after the tab has been away for a while."""
        super().showEvent(event)
        if self._reminder_timer is not None:
            self.check_reminders()
            self._refresh_summary()


def available_reminders_enabled(tab):
    """True when this tab should show reminder popups at all."""
    if not bool(tab.setting("notifications", True)):
        return False
    return True



class TodoWindow(QtWidgets.QDialog):
    """The ToDo workspace as a popup window, not a tab.

    It is deliberately a window rather than a tab: a task list is something
    consulted while working on something else, so it has to float over the
    program, be draggable to a second screen, and be closed without losing the
    tab you were on. The window is therefore *not* modal -- the Scripts and
    Editor tabs stay usable behind it -- but it does stay on top of the main
    window, because a reminder that vanishes behind the window is no use.

    One window is kept and re-shown rather than rebuilt, so the view, the
    filters, the selection and the reminder timers all survive being closed.

    Usage from the main window:

        self.todo_window = TodoWindow(self)
        self.todo_window.toggle()          # the ToDo button
        self.todo_window.open_view("week") # a specific view
    """

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self._first_show = True

        self.setWindowTitle(self._title())
        self.setWindowIcon(_app_icon(window))
        # A tool window: no taskbar entry of its own, but a real close button.
        self.setWindowFlags(
            QtCore.Qt.Window
            | QtCore.Qt.WindowMinMaxButtonsHint
            | QtCore.Qt.WindowCloseButtonHint)
        self.setSizeGripEnabled(True)
        # A popup, not a tab: it starts smaller than the main window so it can
        # sit beside it, and it is the *height* that matters most because the
        # calendar and the list both scroll. 1180x780 is comfortable on a
        # 1440-wide display and still usable on a 1366 one.
        self.resize(1180, 780)
        self.setMinimumSize(900, 560)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        # The tab builds its own title row, so the window only has to host it.
        self.todo = TodoTab(self)
        layout.addWidget(self.todo)

        # A footer with the everyday window actions, so the popup does not rely
        # on the title bar alone (which a borderless server may not draw).
        footer = QtWidgets.QHBoxLayout()
        footer.setContentsMargins(10, 4, 10, 8)
        self.hint = QtWidgets.QLabel("")
        self.hint.setProperty("role", "hint")
        footer.addWidget(self.hint, 1)
        self.close_btn = QtWidgets.QPushButton("Close")
        self.close_btn.clicked.connect(self.close)
        footer.addWidget(self.close_btn)
        layout.addLayout(footer)

        self.retranslate(getattr(window, "lang", "en"))

    # ------------------------------------------------------------- plumbing
    def _title(self):
        return "ScriptDB - %s" % self.window.t("todo", "ToDo")

    def retranslate(self, _language=None):
        """Apply a language change to the windows own chrome."""
        self.setWindowTitle(self._title())
        self.close_btn.setText(self.window.t("todo_close_window", "Close window"))
        self.hint.setText(self.window.t(
            "todo_window_hint",
            "This is a separate window: drag it anywhere, "
            "or press Ctrl+T to hide it."))

    # ------------------------------------------------------------- showing
    def open_view(self, view=None):
        """Show the window, optionally switching to a view first."""
        if view is not None and hasattr(self.todo, "set_view"):
            self.todo.set_view(view)
        self.show_window()

    def show_window(self):
        """Bring the window up, restore it if minimised, and catch up."""
        if self.isMinimized():
            self.showNormal()
        if self._first_show and not self._geometry_remembered():
            self._place_beside_window()
        self._first_show = False
        self.show()
        self.raise_()
        self.activateWindow()
        try:
            self.todo.check_reminders()
            self.todo.refresh()
        except Exception:
            pass

    def toggle(self, view=None):
        # Show the window, or hide it when it is already open. The
        # decision is made from isVisible alone: asking whether the window
        # is *active* looks right but is unreliable, because a window
        # manager may not report focus and it is always false on a
        # head-less platform. A specific view always opens rather than
        # toggles, because the caller had something particular in mind.
        if self.isVisible() and view is None:
            self.hide()
            return False
        self.open_view(view)
        return True

    def _geometry_remembered(self):
        """True when the saved settings already place this window."""
        settings = self.window.settings if hasattr(self.window, "settings") else {}
        return isinstance(settings, dict) and settings.get("todo_window_geometry")

    def _place_beside_window(self):
        """Put the popup over the main window, a little inset, the first time.

        A popup that appears half off-screen on a small display is worse than
        one that covers the middle of the program, so the position is derived
        from the window it belongs to and then clamped to the screen.
        """
        app = QtWidgets.QApplication.instance()
        screen = app.primaryScreen() if app is not None else None
        area = screen.availableGeometry() if screen is not None else None
        parent = self.window
        target = None
        try:
            frame = parent.frameGeometry()
            width = min(self.width(), max(700, frame.width() - 60))
            height = min(self.height(), max(520, frame.height() - 60))
            self.resize(width, height)
            target = QtCore.QPoint(frame.x() + 30, frame.y() + 30)
            if area is not None:
                target.setX(min(max(area.left(), target.x()),
                                area.right() - self.width()))
                target.setY(min(max(area.top(), target.y()),
                                area.bottom() - self.height()))
        except Exception:
            target = None
        if target is not None:
            self.move(target)

    # -------------------------------------------------- geometry persistence
    def restore_geometry(self, settings):
        """Apply a saved size and position, if the window had one.

        The settings dict belongs to the main window, so the ToDo window
        with the same settings file as everything else rather than inventing a
        second one.
        """
        if not isinstance(settings, dict):
            return False
        raw = settings.get("todo_window_geometry")
        if not raw:
            return False
        try:
            parts = [int(value) for value in raw]
            if len(parts) != 4:
                return False
            self.setGeometry(QtCore.QRect(*parts))
            self._first_show = False
            return True
        except (TypeError, ValueError):
            return False

    def geometry_state(self):
        """The current geometry as a plain list, ready to be saved."""
        rect = self.geometry()
        return [rect.x(), rect.y(), rect.width(), rect.height()]

    # ------------------------------------------------------------ lifecycle
    def closeEvent(self, event):
        """Remember where it was, and never destroy the workspace itself.

        The window is hidden rather than destroyed so that the next Ctrl+T is
        instant and the reminder timers keep running: a reminder must still
        fire after the list has been put away.
        """
        self._remember_geometry()
        event.ignore()
        self.hide()

    def _remember_geometry(self):
        saver = getattr(self.window, "save_settings", None)
        settings = getattr(self.window, "settings", None)
        if not isinstance(settings, dict) or not callable(saver):
            return
        settings["todo_window_geometry"] = self.geometry_state()
        try:
            saver()
        except Exception:
            pass

    def keyPressEvent(self, event):
        if event.key() == QtCore.Qt.Key_Escape:
            self.hide()
            event.accept()
            return
        super().keyPressEvent(event)


def _app_icon(window):
    """The program icon for the popup, when one can be found."""
    for name in ("load_app_icon", "app_icon"):
        maker = getattr(window, name, None)
        if callable(maker):
            try:
                return maker()
            except Exception:
                pass
    return QtGui.QIcon()
