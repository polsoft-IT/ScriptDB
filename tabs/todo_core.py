# -*- coding: utf-8 -*-
"""ToDo engine: tasks, recurrence, reminders, and a PL/UK holiday calendar.

This module is deliberately pure: no Qt, no network, no background threads.
It answers questions -- "what does today look like", "what is due", "which
days are holidays" -- and the tab (tabs/todo_tab.py) only draws the answers.
That split is the same one the other tabs use, and it means every rule here
can be reasoned about (and tested) without a window on screen.

The pieces:

    HolidayCalendar   Polish and UK public holidays, computable for any year
    Task              one piece of work, with tags, priority, links, checklist
    Recurrence        the repeat rule of a task (none/daily/.../custom)
    TodoStore         the task collection: load, save, query, mutate
    due_reminders()   the reminders that should fire at a given moment

Everything is stored as plain dictionaries under one JSON list so the file
stays readable, diffable and repairable by hand -- the same bargain the rest
of ScriptDB makes with its data.
"""

import calendar as _calendar
import datetime as dt
import json
import os
import re
import tempfile
import uuid

# ---------------------------------------------------------------------------
# Calendar: holidays
# ---------------------------------------------------------------------------

# The two calendars the user asked for. A holiday is a rule, not a date, so the
# years never have to be listed: the rule is evaluated for whatever year is on
# screen, including years far outside any table we could ship.
COUNTRIES = (("pl", "Poland"), ("uk", "United Kingdom"))


def _easter_sunday(year):
    """Easter Sunday for a year (anonymous Gregorian algorithm)."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return dt.date(year, month, day)


def _nth_weekday(year, month, weekday, n):
    """The n-th (1-based) `weekday` of a month, or None when it does not exist."""
    if n < 0:
        # Count from the end: the codebase already uses -1 for "last".
        first = dt.date(year, month, 1)
        last_day = _calendar.monthrange(year, month)[1]
        last = dt.date(year, month, last_day)
        delta = (last.weekday() - weekday) % 7
        return last - dt.timedelta(days=delta + (-n - 1) * 7)
    first = dt.date(year, month, 1)
    delta = (weekday - first.weekday()) % 7
    day = 1 + delta + (n - 1) * 7
    last_day = _calendar.monthrange(year, month)[1]
    if day > last_day:
        return None
    return dt.date(year, month, day)


def _monday_on_or_before(days=1):
    """A bank-holiday substitute day: the next Monday on or after the date."""
    def rule(date):
        return date + dt.timedelta(days=(7 - date.weekday()) % 7 or 0)
    return rule


def _next_weekday_not_weekend(date):
    """The next Monday-to-Friday day on or after `date` (a substitute day)."""
    while date.weekday() >= 5:
        date += dt.timedelta(days=1)
    return date


def _poland(year):
    """Polish public holidays (statutory days off)."""
    easter = _easter_sunday(year)
    entries = [
        (dt.date(year, 1, 1), "New Year", u"Nowy Rok"),
        (dt.date(year, 1, 6), "Epiphany", u"Święto Trzech Króli"),
        (easter - dt.timedelta(days=2), "Good Friday", u"Wielki Piątek"),
        (easter - dt.timedelta(days=1), "Holy Saturday", u"Wielka Sobota"),
        (easter, "Easter Sunday", u"Wielkanoc"),
        (easter + dt.timedelta(days=1), "Easter Monday",
         u"Poniedziałek Wielkanocny"),
        (dt.date(year, 5, 1), "Labour Day", u"Święto Pracy"),
        (dt.date(year, 5, 3), "Constitution Day",
         u"Święto Konstytucji 3 Maja"),
        (easter + dt.timedelta(days=49), "Pentecost", u"Zielone Świątki"),
        (easter + dt.timedelta(days=60), "Corpus Christi", u"Boże Ciało"),
        (dt.date(year, 8, 15), "Assumption of Mary",
         u"Wniebowzięcie NMP"),
        (dt.date(year, 11, 1), "All Saints Day",
         u"Wszystkich \u015awi\u0119tych"),
        (dt.date(year, 11, 11), "Independence Day",
         u"Święto Niepodległości"),
        (dt.date(year, 12, 24), "Christmas Eve", u"Wigilia"),
        (dt.date(year, 12, 25), "Christmas Day", u"Boże Narodzenie"),
        (dt.date(year, 12, 26), "Second Day of Christmas",
         u"Drugi dzień Bożego Narodzenia"),
        (dt.date(year, 12, 31), "Sylwester", "New Year's Eve"),
    ]
    # Good Friday and Holy Saturday are not statutory in Poland, but they are
    # calendar holidays and a developer planning a release wants to see them.
    entries.append((easter - dt.timedelta(days=2), "Wielki Piątek",
                    "Good Friday"))
    entries.append((easter - dt.timedelta(days=1), "Wielka Sobota",
                    "Holy Saturday"))
    return entries


def _uk(year):
    """UK bank holidays for England and Wales, plus the notable common days."""
    easter = _easter_sunday(year)
    entries = [
        (dt.date(year, 1, 1), "New Year's Day", "Nowy Rok"),
        (easter - dt.timedelta(days=2), "Good Friday", "Wielki Piątek"),
        (easter + dt.timedelta(days=1), "Easter Monday",
         "Poniedziałek Wielkanocny"),
        (dt.date(year, 12, 25), "Christmas Day", "Boże Narodzenie"),
        (dt.date(year, 12, 26), "Boxing Day",
         "Drugi dzień Bożego Narodzenia"),
    ]
    # Early May and spring bank holidays: the first and last Mondays of May.
    early_may = _nth_weekday(year, 5, 0, 1)
    spring = _nth_weekday(year, 5, 0, -1)
    summer = _nth_weekday(year, 8, 0, -1)
    for date, name, name_pl in ((early_may, "Early May Bank Holiday",
                                 "Święto majowe"),
                                (spring, "Spring Bank Holiday",
                                 "Wiosenne święto bankowe"),
                                (summer, "Summer Bank Holiday",
                                 "Letnie święto bankowe")):
        if date is not None:
            entries.append((date, name, name_pl))
    # A holiday that falls at the weekend is taken on the next working day,
    # which is what a bank holiday actually means in the UK.
    substitutes = []
    for date, name, name_pl in list(entries):
        if date.weekday() >= 5:
            substitutes.append((_next_weekday_not_weekend(
                date + dt.timedelta(days=1)),
                name + " (substitute day)", name_pl + " (dzień zastępczy)"))
    entries.extend(substitutes)
    return entries


HOLIDAY_SOURCES = {"pl": _poland, "uk": _uk}


class HolidayCalendar:
    """Public holidays for Poland and the UK, for any year.

    The two calendars are kept apart and merged only when asked, so the tab
    can show "Polish holidays", "UK holidays" or both. Each day names itself
    in both languages, because the language can be switched at run time and a
    holiday name must not be rebuilt from a table key.
    """

    def __init__(self, countries=("pl", "uk")):
        self.countries = tuple(c for c in countries if c in HOLIDAY_SOURCES) \
            or ("pl", "uk")
        self._cache = {}

    def _for_year(self, year):
        if year not in self._cache:
            days = {}
            for country in self.countries:
                for date, name, name_pl in HOLIDAY_SOURCES[country](year):
                    slot = days.setdefault(date, {"names": [], "countries": []})
                    slot["names"].append({"en": name, "pl": name_pl})
                    slot["countries"].append(country)
            self._cache[year] = days
        return self._cache[year]

    def is_holiday(self, date):
        """True when the date is a holiday of any selected country."""
        return date in self._for_year(date.year)

    def info(self, date):
        """The holiday entry for a date, or None.

        The result carries ``names`` (one per country, each with an English
        and a Polish form) and ``countries`` so a view can show both.
        """
        return self._for_year(date.year).get(date)

    def label(self, date, language="en"):
        """The holiday name in one language, or empty when it is not one."""
        entry = self.info(date)
        if not entry:
            return ""
        names = [name.get(language) or name.get("en") or ""
                 for name in entry["names"]]
        return " / ".join(name for name in names if name)

    def labels(self, date):
        """Every name of a holiday, deduplicated, in both languages."""
        entry = self.info(date)
        if not entry:
            return []
        out = []
        for name in entry["names"]:
            for key in ("en", "pl"):
                text = name.get(key)
                if text and text not in out:
                    out.append(text)
        return out

    def year_days(self, year):
        """Every holiday of a year as {date: entry}."""
        return dict(self._for_year(year))

    def month_days(self, year, month):
        """Every holiday of one month, as {date: entry}."""
        return {date: entry for date, entry in self._for_year(year).items()
                if date.month == month}

    def next_holiday(self, date):
        """
        The next holiday on or after a date, or None within three years.

        The walk starts at the given year and only moves on when that
        year has nothing left, so a date in a past year still finds the
        next holiday in that same year instead of jumping ahead.
        """
        cursor = date
        for _ in range(3):
            days = self._for_year(cursor.year)
            later = sorted(day for day in days if day >= cursor)
            if later:
                return later[0], days[later[0]]
            cursor = dt.date(cursor.year + 1, 1, 1)
        return None

    def countdown(self, date=None, today=None):
        """Days until the next holiday on or after today."""
        today = today or date or dt.date.today()
        if self.is_holiday(today):
            return 0, today, self.info(today)
        found = self.next_holiday(today)
        if not found:
            return None, None, None
        day, entry = found
        return (day - today).days, day, entry


# ---------------------------------------------------------------------------
# The task model
# ---------------------------------------------------------------------------

#: Every state a task can be in. "doing" and "blocked" exist because a
#: developer's list needs to distinguish "not started" from "waiting on
#: somebody else" -- otherwise the list lies about what can be worked on.
STATUSES = ("todo", "doing", "blocked", "done", "cancelled")

#: Priorities, most urgent first. The order is what the sort uses.
PRIORITIES = ("critical", "high", "normal", "low")

#: The kinds of work the tab understands. "code"/"bug"/"review"/"research"
#: are the coding ones; the rest make the same list usable for everything.
KINDS = ("task", "code", "bug", "review", "research", "meeting",
         "release", "admin", "idea")

#: The repeat rules. "custom" reads the interval fields.
REPEATS = ("none", "daily", "weekdays", "weekly", "biweekly", "monthly",
           "quarterly", "yearly", "custom")

#: Reminder offsets, in minutes before the due moment.
REMIND_CHOICES = (0, 5, 10, 15, 30, 60, 120, 1440, 2880, 10080)

#: How many days each repeat rule adds. "monthly"/"yearly" are handled by
#: calendar arithmetic instead, because a month is not 30 days.
REPEAT_DAYS = {"daily": 1, "weekly": 7, "biweekly": 14, "quarterly": 91}


def now_stamp():
    """The current local time as an ISO string without microseconds."""
    return dt.datetime.now().replace(microsecond=0).isoformat(" ")


def parse_stamp(text):
    """Read a stored timestamp; returns None when it is not one.

    Accepts both "YYYY-MM-DD HH:MM" and the ISO "T" form, because a task may
    have been written by hand or by another tool.
    """
    if not text:
        return None
    text = str(text).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        return dt.datetime.fromisoformat(text)
    except ValueError:
        return None


def parse_date(text):
    """Read a stored date; returns None when it is not one."""
    if not text:
        return None
    if isinstance(text, dt.datetime):
        return text.date()
    if isinstance(text, dt.date):
        return text
    stamp = parse_stamp(text)
    if stamp is not None:
        return stamp.date()
    try:
        return dt.datetime.strptime(str(text).strip(), "%d.%m.%Y").date()
    except ValueError:
        return None


def human_delta(moment, reference=None):
    """A short "in 2 h" / "3 d ago" phrase for a due moment."""
    reference = reference or dt.datetime.now()
    seconds = (moment - reference).total_seconds()
    past = seconds < 0
    seconds = abs(seconds)
    if seconds < 90:
        text = "now"
        return text
    if seconds < 3600:
        text = "%d min" % round(seconds / 60)
    elif seconds < 86400:
        text = "%d h" % round(seconds / 3600)
    elif seconds < 86400 * 30:
        text = "%d d" % round(seconds / 86400)
    else:
        text = "%d mo" % round(seconds / (86400 * 30))
    return ("-%s" % text) if past else ("+%s" % text)


class Task:
    """One unit of work.

    A task is a plain dictionary wrapped in a small class so the field names
    live in one place. Nothing here touches a file: TodoStore owns the file.
    """

    def __init__(self, data=None):
        data = dict(data or {})
        self.data = data
        data.setdefault("id", uuid.uuid4().hex)
        data.setdefault("title", )
        data.setdefault("notes", )
        data.setdefault("project", )
        data.setdefault("kind", "task")
        data.setdefault("status", "todo")
        data.setdefault("priority", "normal")
        data.setdefault("tags", [])
        data.setdefault("links", [])
        data.setdefault("checklist", [])
        data.setdefault("estimate", 0)
        data.setdefault("spent", 0)
        data.setdefault("created", now_stamp())
        data.setdefault("updated", data["created"])
        data.setdefault("due", )
        data.setdefault("start", )
        data.setdefault("remind", 30)
        data.setdefault("reminded", )
        data.setdefault("repeat", "none")
        data.setdefault("repeat_interval", 1)
        data.setdefault("repeat_unit", "day")
        data.setdefault("repeat_until", )
        data.setdefault("color", )

    # -- field access ------------------------------------------------------
    def __getitem__(self, key):
        return self.data.get(key)

    def __setitem__(self, key, value):
        self.data[key] = value

    def get(self, key, default=None):
        return self.data.get(key, default)

    def update(self, values):
        self.data.update(values)

    # -- meaning -----------------------------------------------------------
    @property
    def id(self):
        return self.data.get("id")

    @property
    def title(self):
        return self.data.get("title", ) or "(untitled)"

    @property
    def done(self):
        return self.data.get("status") == "done"

    @property
    def open(self):
        return self.data.get("status") not in ("done", "cancelled")

    @property
    def overdue(self):
        if not self.open:
            return False
        due = self.due_moment()
        return due is not None and due < dt.datetime.now()

    def due_moment(self):
        return parse_stamp(self.data.get("due"))

    def start_moment(self):
        return parse_stamp(self.data.get("start"))

    def due_date(self):
        return parse_date(self.data.get("due"))

    def start_date(self):
        return parse_date(self.data.get("start"))

    def covers(self, day):
        """True when the task belongs on a calendar day.

        A task with a start and a due date spans both; one with only a due
        date sits on that day; one with neither sits on nothing.
        """
        start, due = self.start_date(), self.due_date()
        if start and due:
            return start <= day <= due
        if due:
            return due == day
        if start:
            return start == day
        return False

    def spans(self, day):
        "# True when the day is in the middle of a multi-day task."
        start, due = self.start_date(), self.due_date()
        return bool(start and due and start < day < due)

    def checklist_progress(self):
        """(done, total) of the checklist."""
        items = self.data.get("checklist") or []
        finished = sum(1 for item in items if item.get("done"))
        return finished, len(items)

    def tag_list(self):
        """Tags as a clean list, whichever way they were stored."""
        tags = self.data.get("tags") or []
        if isinstance(tags, str):
            tags = re.split(r"[,\s]+", tags)
        return [str(tag).strip() for tag in tags if str(tag).strip()]

    def effective_color(self):
        """The colour to draw the task with, derived when it was not set."""
        explicit = self.data.get("color")
        if explicit:
            return explicit
        if self.data.get("status") == "done":
            return "#5a9e6f"
        if self.data.get("status") == "blocked":
            return "#c02a3a"
        return {"critical": "#f7768e", "high": "#f7c66d",
                "low": "#6e7681"}.get(self.data.get("priority"), "#8ab4f8")

    def matches(self, needle, fields=None):
        """Free-text search across the fields a user would expect."""
        needle = (needle or "").strip().lower()
        if not needle:
            return True
        haystack = " ".join([
            self.title, self.data.get("notes", ""),
            self.data.get("project", ""), self.data.get("kind", ""),
            self.data.get("status", ""), self.data.get("priority", ""),
            " ".join(self.tag_list()),
        ]).lower()
        return needle in haystack

    # -- transitions -------------------------------------------------------
    def toggle_done(self, when=None):
        """Flip between done and the previous open state."""
        if self.done:
            self.data["status"] = self.data.get("_status_before") or "todo"
        else:
            self.data["_status_before"] = self.data.get("status")
            self.data["status"] = "done"
            self.data["done_at"] = when or now_stamp()
        self.data["updated"] = now_stamp()
        return self.data["status"]

    def next_occurrence(self):
        """The due moment of the next repeat, or None when it does not repeat.

        The rule is applied to the *due* moment, not to today, so a daily task
        completed three days late still lands on the original rhythm instead of
        drifting forward with every completion.
        """
        repeat = self.data.get("repeat", "none")
        if repeat in ("none", "", None):
            return None
        base = self.due_moment() or parse_stamp(self.data.get("start")) \
            or dt.datetime.now()
        until = parse_stamp(self.data.get("repeat_until"))
        if repeat == "weekdays":
            candidate = base + dt.timedelta(days=1)
            while candidate.weekday() >= 5:
                candidate += dt.timedelta(days=1)
        elif repeat in REPEAT_DAYS:
            candidate = base + dt.timedelta(days=REPEAT_DAYS[repeat])
        elif repeat == "monthly":
            candidate = _add_months(base, 1)
        elif repeat == "yearly":
            candidate = _add_months(base, 12)
        elif repeat == "custom":
            interval = max(1, int(self.data.get("repeat_interval") or 1))
            unit = self.data.get("repeat_unit") or "day"
            candidate = _add_units(base, interval, unit)
        else:
            return None
        if until is not None and candidate > until:
            return None
        return candidate

    def to_dict(self):
        """The stored form: the private bookkeeping keys are dropped."""
        return {key: value for key, value in self.data.items()
                if not key.startswith("_")}


def _stamp(moment):
    """
    One moment in the format every stored timestamp uses.
    Seconds are dropped because nothing in the tab works at that resolution,
    and a value written here must compare equal to one written by hand in the
    editor ("2025-05-02 09:00", not "2025-05-02 09:00:00").
    """
    return moment.replace(second=0, microsecond=0).isoformat(" ")


def _add_months(moment, months):
    """Add whole months, clamping the day to the length of the target month."""
    month = moment.month - 1 + months
    year = moment.year + month // 12
    month = month % 12 + 1
    day = min(moment.day, _calendar.monthrange(year, month)[1])
    return moment.replace(year=year, month=month, day=day)


def _add_units(moment, interval, unit):
    """Add `interval` units of "day"/"week"/"month"/"year" to a moment."""
    if unit == "day":
        return moment + dt.timedelta(days=interval)
    if unit == "week":
        return moment + dt.timedelta(weeks=interval)
    if unit == "month":
        return _add_months(moment, interval)
    if unit == "year":
        return _add_months(moment, 12 * interval)
    return moment + dt.timedelta(days=interval)


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------

DEFAULT_SETTINGS = {
    "view": "month",
    "language": "en",
    "countries": ["pl", "uk"],
    "work_start": 8,
    "work_end": 18,
    "show_weekends": True,
    "show_holidays": True,
    "show_done": False,
    "show_week_numbers": True,
    "start_monday": True,
    "default_remind": 30,
    "notifications": True,
    "notify_seconds": 8,
    "show_overdue_badge": True,
    "first_day_highlight": True,
    "default_project": "",
}


class TodoStore:
    """The task collection: the file, the queries and every mutation.

    A task is never edited in place by the interface; the interface asks the
    store for a change and the store owns the bookkeeping (updating the
    ``updated`` stamp, keeping the file consistent, recording completions).
    """

    DATA_FILE = "todo_db.json"
    SCHEMA = 1

    def __init__(self, path):
        self.path = path
        self.tasks = []
        self.settings = dict(DEFAULT_SETTINGS)
        self.warnings = []
        self.completions = []          # a short local history, newest first
        self.notes = []
        self.expenses = []
        self.load()

    # -- persistence -------------------------------------------------------
    def load(self):
        if not os.path.exists(self.path):
            return []
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError) as exc:
            self.warnings.append("Could not read %s: %s"
                                 % (os.path.basename(self.path), exc))
            return []
        if isinstance(payload, list):
            records, settings = payload, {}
        elif isinstance(payload, dict):
            records = payload.get("tasks", [])
            settings = payload.get("settings", {}) or {}
            self.completions = list(payload.get("completions", []) or [])
            self.notes = list(payload.get("notes", []) or [])
            self.expenses = list(payload.get("expenses", []) or [])
        else:
            self.warnings.append("todo data is not a list")
            return []
        if not isinstance(records, list):
            self.warnings.append("todo tasks are not a list")
            records = []
        self.tasks = [Task(record) for record in records
                      if isinstance(record, dict)]
        if not isinstance(self.notes, list):
            self.notes = []
        if not isinstance(self.expenses, list):
            self.expenses = []
        for key, value in settings.items():
            if key in DEFAULT_SETTINGS:
                self.settings[key] = value
        if not isinstance(self.settings.get("countries"), list):
            self.settings["countries"] = list(DEFAULT_SETTINGS["countries"])
        return self.tasks

    def save(self):
        """Write the file atomically so a crash cannot truncate it."""
        payload = {
            "schema": self.SCHEMA,
            "saved": now_stamp(),
            "settings": self.settings,
            "completions": self.completions[:200],
            "notes": self.notes,
            "expenses": self.expenses,
            "tasks": [task.to_dict() for task in self.tasks],
        }
        directory = os.path.dirname(self.path) or "."
        handle, temporary = tempfile.mkstemp(dir=directory, prefix=".todo-",
                                             suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except Exception:
            try:
                os.remove(temporary)
            except OSError:
                pass
            raise
        return self.path

    # -- queries -----------------------------------------------------------
    def get(self, task_id):
        return next((task for task in self.tasks if task.id == task_id), None)

    def add(self, **values):
        task = Task(values)
        self.tasks.append(task)
        return task

    def remove(self, task_id):
        """Delete a task; returns the number removed."""
        before = len(self.tasks)
        self.tasks = [task for task in self.tasks if task.id != task_id]
        return before - len(self.tasks)

    def visible(self, include_done=None, include_cancelled=False):
        """The tasks a list should show, honouring the settings."""
        if include_done is None:
            include_done = bool(self.settings.get("show_done"))
        out = []
        for task in self.tasks:
            status = task.get("status")
            if status == "cancelled" and not include_cancelled:
                continue
            if status == "done" and not include_done:
                continue
            out.append(task)
        return out

    def filter(self, query="", project=None, tag=None, kind=None, status=None,
               priority=None, day=None, include_done=None):
        """The tasks matching every given filter (None means "any")."""
        result = []
        for task in self.visible(include_done):
            if not task.matches(query):
                continue
            if project is not None and task.get("project", "") != project:
                continue
            if tag is not None and tag not in task.tag_list():
                continue
            if kind is not None and task.get("kind") != kind:
                continue
            if status is not None and task.get("status") != status:
                continue
            if priority is not None and task.get("priority") != priority:
                continue
            if day is not None and not task.covers(day):
                continue
            result.append(task)
        return result

    def on_day(self, day, include_done=None):
        """Every task that belongs on a calendar day."""
        return [task for task in self.visible(include_done)
                if task.covers(day)]

    def due_between(self, start, end, include_done=None):
        """Tasks whose due moment falls between start and end."""
        out = []
        for task in self.visible(include_done):
            moment = task.due_moment()
            if moment is not None and start <= moment < end:
                out.append(task)
        out.sort(key=lambda task: task.due_moment())
        return out

    def overdue(self):
        return sorted((task for task in self.visible()
                       if task.overdue), key=lambda task: task.due_moment())

    def upcoming(self, days=7):
        """Open tasks due within the next `days` days, soonest first."""
        start = dt.datetime.now()
        end = start + dt.timedelta(days=days)
        return [task for task in self.due_between(start, end)
                if task.get("status") != "done"]

    def today(self):
        """What is on today: overdue, due today, and in progress."""
        day = dt.date.today()
        buckets = {"overdue": self.overdue(), "today": [], "doing": []}
        for task in self.visible():
            moment = task.due_moment()
            if moment and moment.date() == day and task.get("status") != "done":
                buckets["today"].append(task)
            elif task.get("status") == "doing":
                buckets["doing"].append(task)
        buckets["today"].sort(key=lambda task: task.due_moment())
        return buckets

    def projects(self):
        """Every project name in use, sorted, with a task count each."""
        counts = {}
        for task in self.tasks:
            name = (task.get("project") or "").strip()
            if name:
                counts[name] = counts.get(name, 0) + 1
        return sorted(counts.items(), key=lambda item: item[0].lower())

    def all_tags(self):
        """Every tag in use with its count, most used first."""
        counts = {}
        for task in self.tasks:
            for tag in task.tag_list():
                counts[tag] = counts.get(tag, 0) + 1
        return sorted(counts.items(), key=lambda item: (-item[1], item[0]))

    def assignable_tags(self):
        "# The tags that already exist, for the editor's suggestions."
        return [tag for tag, _count in self.all_tags()]

    def find(self, needle):
        """A loose lookup by title, for "open the task called ..."."""
        needle = (needle or "").strip().lower()
        for task in self.tasks:
            if task.title.lower() == needle:
                return task
        for task in self.tasks:
            if needle and needle in task.title.lower():
                return task
        return None

    # -- mutations ---------------------------------------------------------
    def toggle_done(self, task_id):
        """Complete a task and, when it repeats, schedule the next one.

        This is the one place a repeat rule is applied, so a task can only
        ever spawn exactly one successor per completion.
        """
        task = self.get(task_id)
        if task is None:
            return None
        if task.done:
            task.toggle_done()
            return task
        task.toggle_done()
        self.completions.insert(0, {"task": task.id, "title": task.title,
                                    "at": now_stamp()})
        del self.completions[200:]
        successor = self.roll_over(task)
        if successor is not None:
            return successor
        return task

    def roll_over(self, task):
        """Create the next occurrence of a repeating task, or return None."""
        nxt = task.next_occurrence()
        if nxt is None:
            return None
        clone = Task(task.to_dict())
        clone["id"] = uuid.uuid4().hex
        clone["status"] = "todo"
        clone["created"] = now_stamp()
        clone["updated"] = clone["created"]
        clone["reminded"] = ""
        clone["done_at"] = ""
        clone["spent"] = 0
        clone["due"] = _stamp(nxt)
        start = task.start_moment()
        if start is not None:
            offset = nxt - (task.due_moment() or nxt)
            clone["start"] = _stamp(start + offset)
        clone["checklist"] = [dict(item, done=False)
                              for item in (task.get("checklist") or [])]
        self.tasks.append(clone)
        return clone

    def snooze(self, task_id, minutes):
        """Push a tasks due moment (and its reminder) into the future."""
        task = self.get(task_id)
        if task is None:
            return None
        base = task.due_moment() or dt.datetime.now()
        if base < dt.datetime.now():
            base = dt.datetime.now()
        due = (base + dt.timedelta(minutes=minutes)).replace(microsecond=0)
        task["due"] = due.isoformat(" ")
        task["reminded"] = ""
        task["updated"] = now_stamp()
        return task

    def postpone(self, task_id, days):
        """Move a task a whole number of days, keeping its time of day."""
        task = self.get(task_id)
        if task is None:
            return None
        base = task.due_moment() or dt.datetime.now()
        due = base + dt.timedelta(days=days)
        task["due"] = due.replace(microsecond=0).isoformat(" ")
        task["reminded"] = ""
        task["updated"] = now_stamp()
        return task

    def set_status(self, task_id, status):
        task = self.get(task_id)
        if task is None:
            return None
        if status not in STATUSES:
            return None
        task["status"] = status
        task["updated"] = now_stamp()
        if status == "done":
            task["done_at"] = now_stamp()
        return task

    def statistics(self, calendar=None):
        """The numbers the summary panel shows."""
        tasks = [task for task in self.tasks
                 if task.get("status") != "cancelled"]
        done = sum(1 for task in tasks if task.done)
        doing = sum(1 for task in tasks if task.get("status") == "doing")
        blocked = sum(1 for task in tasks if task.get("status") == "blocked")
        overdue = sum(1 for task in tasks if task.overdue)
        estimate = sum(int(task.get("estimate") or 0)
                       for task in tasks if task.open)
        spent = sum(int(task.get("spent") or 0) for task in tasks)
        week = self.completions_within(7)
        by_kind, by_priority, by_project = {}, {}, {}
        for task in tasks:
            by_kind[task.get("kind")] = by_kind.get(task.get("kind"), 0) + 1
            by_priority[task.get("priority")] = \
                by_priority.get(task.get("priority"), 0) + 1
            if task.get("project"):
                by_project[task["project"]] = \
                    by_project.get(task["project"], 0) + 1
        out = {
            "total": len(tasks), "done": done, "doing": doing,
            "blocked": blocked, "overdue": overdue,
            "open": len(tasks) - done,
            "estimate": estimate, "spent": spent,
            "done_last_week": len(week),
            "by_kind": by_kind, "by_priority": by_priority,
            "by_project": by_project,
        }
        if calendar is not None:
            days, day, _entry = calendar.countdown(dt.date.today())
            out["days_to_holiday"] = days
            out["next_holiday"] = day
        return out

    def completions_within(self, days):
        """Completions recorded in the last `days` days."""
        limit = dt.datetime.now() - dt.timedelta(days=days)
        out = []
        for record in self.completions:
            moment = parse_stamp(record.get("at"))
            if moment is not None and moment >= limit:
                out.append(record)
        return out

    # -- reminders ---------------------------------------------------------
    def due_reminders(self, moment=None):
        """The open tasks whose reminder is due now and has not fired yet.

        A task fires once: ``reminded`` records the due moment the reminder
        was sent for, so editing an unrelated field cannot resurrect it, but
        moving the due moment (snooze, postpone, a repeat) does.
        """
        moment = moment or dt.datetime.now()
        fired = []
        for task in self.visible(include_done=False):
            due = task.due_moment()
            if due is None:
                continue
            lead = int(task.get("remind") or 0)
            when = due - dt.timedelta(minutes=lead)
            if when > moment:
                continue
            if task.get("reminded") == task.get("due"):
                continue
            fired.append(task)
        fired.sort(key=lambda task: task.due_moment())
        return fired

    def mark_reminded(self, task_id):
        task = self.get(task_id)
        if task is not None:
            task["reminded"] = task.get("due", )
        return task

    # -- bulk operations ---------------------------------------------------
    def clear_completed(self):
        """Remove every completed task; returns how many went."""
        before = len(self.tasks)
        self.tasks = [task for task in self.tasks if not task.done]
        return before - len(self.tasks)

    def move_all(self, task_ids, **changes):
        """Apply the same change to several tasks at once."""
        moved = []
        for task_id in task_ids:
            task = self.get(task_id)
            if task is None:
                continue
            task.update(changes)
            task["updated"] = now_stamp()
            moved.append(task)
        return moved


# ---------------------------------------------------------------------------
# Calendar geometry: the grids a month/week view is drawn from
# ---------------------------------------------------------------------------

def week_start(day, start_monday=True):
    """The first day of the week containing `day`."""
    first = 0 if start_monday else 6
    return day - dt.timedelta(days=(day.weekday() - first) % 7)


def week_days(day, start_monday=True):
    """The seven days of the week containing `day`, in display order."""
    start = week_start(day, start_monday)
    return [start + dt.timedelta(days=offset) for offset in range(7)]


def month_grid(year, month, start_monday=True):
    """The weeks of a month view: a list of lists of `datetime.dat

    Complete weeks are returned, so the first and last rows include the
    neighbouring month's days -- which is what makes a month grid readable.
    """
    first = dt.date(year, month, 1)
    last_day = _calendar.monthrange(year, month)[1]
    last = dt.date(year, month, last_day)
    start = week_start(first, start_monday)
    end = week_start(last, start_monday) + dt.timedelta(days=6)
    weeks, cursor = [], start
    while cursor <= end:
        weeks.append([cursor + dt.timedelta(days=offset) for offset in range(7)])
        cursor += dt.timedelta(days=7)
    return weeks


def year_months(year):
    """The twelve months of a year, each with its label number."""
    return [(month, _calendar.month_name[month]) for month in range(1, 13)]


def iso_week(date):
    """The ISO week number of a date."""
    return date.isocalendar()[1]


def shift_month(year, month, delta):
    """Move a (year, month) pair by `delta` months."""
    index = (year * 12 + month - 1) + delta
    return index // 12, index % 12 + 1


def day_name(date, language="en"):
    """The weekday name, in the requested language."""
    names = {
        "en": ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
               "Saturday", "Sunday"),
        "pl": ("Poniedziałek", "Wtorek", "Środa", "Czwartek", "Piątek",
               "Sobota", "Niedziela"),
    }
    return names.get(language, names["en"])[date.weekday()]


def month_name(year, month, language="en"):
    """The month name, in the requested language."""
    if language == "pl":
        names = ("styczeń", "luty", "marzec", "kwiecień", "maj", "czerwiec",
                 "lipiec", "sierpień", "wrzesień", "październik", "listopad",
                 "grudzień")
        return "%s %d" % (names[month - 1], year)
    return "%s %d" % (_calendar.month_name[month], year)


def week_label(days, language="en"):
    """A short "12-18 May 2025" label for a week row or header."""
    if not days:
        return ""
    first, last = days[0], days[-1]
    if first.month == last.month:
        return "%d-%d %s %d" % (first.day, last.day,
                                month_name(first.year, first.month,
                                           language).rsplit(" ", 1)[0],
                                first.year)
    return "%s - %s" % (first.strftime("%d.%m"), last.strftime("%d.%m.%Y"))


# ---------------------------------------------------------------------------
# Templates: the "what should I do next" presets
# ---------------------------------------------------------------------------

#: Ready-made tasks a developer starts from, so the tab is useful before it
#: has been taught anything. `{date}` and `{today}` are filled in on use.
TASK_TEMPLATES = (
    ("code_feature", "Implement a feature", "code", "normal",
     "Implement: <what>\n\nDefinition of done:\n- [ ] code written\n"
     "- [ ] tests pass\n- [ ] documented"),
    ("code_bug", "Fix a bug", "bug", "high",
     "What happens: <observed>\nWhat should happen: <expected>\n"
     "Steps to reproduce:\n1. \n2. \n\nRoot cause: "),
    ("code_review", "Review a change", "review", "normal",
     "Review: <commit / branch>\n\nLook for:\n- [ ] correctness\n"
     "- [ ] edge cases\n- [ ] naming\n- [ ] tests"),
    ("code_refactor", "Refactor", "code", "low",
     "Refactor: <module>\nWhy: \nKeep the behaviour identical; run the tests "
     "before and after."),
    ("code_test", "Write tests", "code", "normal",
     "Cover: <module>\n- [ ] happy path\n- [ ] edge cases\n- [ ] errors"),
    ("release", "Cut a release", "release", "high",
     "- [ ] changelog\n- [ ] version bumped\n- [ ] tagged\n- [ ] built\n"
     "- [ ] deployed\n- [ ] announced"),
    ("deploy", "Deploy", "release", "high",
     "Target: \n- [ ] backup\n- [ ] deploy\n- [ ] smoke test\n- [ ] verify"),
    ("standup", "Daily standup", "meeting", "normal",
     "Yesterday: \nToday: \nBlockers: "),
    ("review_week", "Weekly review", "admin", "normal",
     "- [ ] inbox\n- [ ] plan next week\n- [ ] close finished tasks"),
    ("research", "Research", "research", "low",
     "Question: <what do I need to know>\nTime box: \nFindings: "),
    ("idea", "Idea", "idea", "low", "Idea: \nWhy it could matter: "),
)


def template_tasks(day=None):
    """The templates as task dictionaries, dated today by default."""
    day = day or dt.date.today()
    when = dt.datetime(day.year, day.month, day.day, 9, 0)
    out = []
    for key, title, kind, priority, notes in TASK_TEMPLATES:
        out.append({"key": key, "title": title, "kind": kind,
                    "priority": priority, "notes": notes,
                    "due": when.isoformat(" ")})
    return out


# ---------------------------------------------------------------------------
# Export: the tab can hand its data to the other tabs as text
# ---------------------------------------------------------------------------

def task_to_markdown(task, language="en"):
    """One task as a Markdown block (for DOCs, a report or a clipboard)."""
    done, total = task.checklist_progress()
    lines = ["## " + task.title, ""]
    lines.append("- Status: %s" % task.get("status"))
    lines.append("- Priority: %s" % task.get("priority"))
    lines.append("- Kind: %s" % task.get("kind"))
    if task.get("project"):
        lines.append("- Project: %s" % task.get("project"))
    if task.get("due"):
        lines.append("- Due: %s" % task.get("due"))
    if task.tag_list():
        lines.append("- Tags: %s" % ", ".join(task.tag_list()))
    if total:
        lines.append("- Checklist: %d/%d" % (done, total))
    if task.get("links"):
        lines.append("- Links: %s" % ", ".join(task.get("links") or []))
    lines.append("")
    if task.get("notes"):
        lines.append(task.get("notes"))
        lines.append("")
    for item in (task.get("checklist") or []):
        mark = "x" if item.get("done") else ""
        lines.append("- [%s] %s" % (mark, item.get("text", "")))
    return "\n".join(lines)


def tasks_to_ics(tasks, calendar_name="ScriptDB ToDo"):
    """Render tasks as an iCalendar (RFC 5545) calendar -- plain text.

    Only the fields a calendar client needs are emitted, and everything is
    kept ASCII and CRLF-terminated, which is what the format requires. A task
    without a due moment is skipped: it has nothing to place on a calendar.
    """
    lines = ["BEGIN:VCALENDAR",
             "VERSION:2.0",
             "PRODID:-//ScriptDB//ToDo//EN",
             "CALSCALE:GREGORIAN",
             "X-WR-CALNAME:" + _ics_escape(calendar_name)]
    stamp = dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    for task in tasks:
        due = task.due_moment()
        if due is None:
            continue
        start = task.start_moment() or due
        lines.append("BEGIN:VEVENT")
        lines.append("UID:%s@scriptdb" % task.id)
        lines.append("DTSTAMP:" + stamp)
        lines.append("DTSTART:" + start.strftime("%Y%m%dT%H%M%S"))
        lines.append("DTEND:" + due.strftime("%Y%m%dT%H%M%S"))
        lines.append("SUMMARY:" + _ics_escape(task.title))
        description = task.get("notes") or ""
        if description:
            lines.append("DESCRIPTION:" + _ics_escape(description))
        categories = ", ".join(task.tag_list() + [task.get("project") or ""])
        categories = categories.strip(", ")
        if categories:
            lines.append("CATEGORIES:" + _ics_escape(categories))
        lines.append("STATUS:" + ("COMPLETED" if task.done else "NEEDS-ACTION"))
        lines.append("PRIORITY:%d" % {"critical": 1, "high": 3,
                                      "normal": 5, "low": 9}.get(
                                          task.get("priority"), 5))
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def _ics_escape(text):
    """Escape a value the way iCalendar wants it, and keep it ASCII."""
    text = str(text or "")
    text = text.replace("\\", "\\\\").replace(";", "\\;")
    text = text.replace(",", "\\,").replace("\r\n", "\\n")
    text = text.replace("\n", "\\n")
    return text.encode("ascii", "replace").decode("ascii")


def parse_ics(text):
    """Read a minimal iCalendar into task dictionaries.

    This reads what it wrote (and what most simple exports produce): one
    VEVENT per task, with SUMMARY/DTSTART/DTEND/DESCRIPTION/CATEGORIES. Lines
    it does not understand are ignored rather than raising, because a calendar
    export from elsewhere is not a format we control.
    """
    events, current = [], None
    for raw in (text or "").splitlines():
        line = raw.strip()
        if line == "BEGIN:VEVENT":
            current = {}
            continue
        if line == "END:VEVENT":
            if current:
                events.append(current)
            current = None
            continue
        if current is None or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.split(";")[0].upper()
        value = value.replace("\\n", "\n").replace("\\,", ",") \
            .replace("\\;", ";").replace("\\\\", "\\")
        if key == "SUMMARY":
            current["title"] = value
        elif key == "DESCRIPTION":
            current["notes"] = value
        elif key in ("DTSTART", "DTEND"):
            current["start" if key == "DTSTART" else "due"] = \
                _ics_moment(value)
        elif key == "CATEGORIES":
            current["tags"] = [part.strip() for part in value.split(",")
                               if part.strip()]
        elif key == "STATUS":
            current["status"] = "done" if value.upper() == "COMPLETED" else "todo"
    tasks = []
    for event in events:
        if not event.get("title"):
            continue
        event.setdefault("due", event.get("start", ))
        event.setdefault("kind", "task")
        event.setdefault("priority", "normal")
        tasks.append(event)
    return tasks


def _ics_moment(value):
    """Read an iCalendar datetime (basic form, UTC or floating)."""
    value = (value or "").strip()
    for fmt in ("%Y%m%dT%H%M%SZ", "%Y%m%dT%H%M%S", "%Y%m%d"):
        try:
            return dt.datetime.strptime(value, fmt).replace(
                microsecond=0).isoformat(" ")
        except ValueError:
            continue
    return value
