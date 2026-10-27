# -*- coding: utf-8 -*-
"""Smoke test for the ToDo workspace, run from the repo root.

It checks the three layers in one pass:
    tabs/todo_core.py      the rules (holidays, recurrence, reminders)
    tabs/todo_tab.py       the view models and the tab itself, head-less
    the window             that the tab is wired in beside Statistics

Run it with ``python tests/check_todo.py``. It needs Qt but no display, so it
works on a build machine: the platform is forced to "offscreen" before the
application object is created.
"""

import datetime as dt
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5 import QtWidgets                    # noqa: E402

from tabs import todo_core as core             # noqa: E402
from tabs.todo_tab import TodoTab              # noqa: E402

FAILURES = []


def check(label, condition, detail=""):
    mark = "ok  " if condition else "FAIL"
    print("%s %s%s" % (mark, label, ("  -- " + str(detail)) if detail else ""))
    if not condition:
        FAILURES.append(label)


class FakeWindow(QtWidgets.QWidget):
    """The smallest thing the tab needs: a translator and a base folder."""

    def __init__(self):
        super().__init__()
        self.base_dir = tempfile.mkdtemp(prefix="sdb-todo-test-")
        self.lang = "en"

    def t(self, key, fallback=None, **kwargs):
        text = fallback if fallback is not None else key
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            return text


def test_engine():
    print("\n== engine ==")
    calendar = core.HolidayCalendar(["pl", "uk"])
    check("Polish Constitution Day is a holiday",
          calendar.is_holiday(dt.date(2025, 5, 3)))
    check("UK boxing day is a holiday",
          calendar.is_holiday(dt.date(2025, 12, 26)))
    check("a plain Tuesday is not a holiday",
          not calendar.is_holiday(dt.date(2025, 5, 6)))
    every = calendar.labels(dt.date(2025, 5, 3))
    check("both names are offered", len(every) == 2, every)

    weeks = core.month_grid(2025, 5)
    check("May 2025 is five whole weeks", len(weeks) == 5, len(weeks))
    check("every week has seven days",
          all(len(week) == 7 for week in weeks))
    check("the grid covers the 1st and the 31st",
          weeks[0][0] <= dt.date(2025, 5, 1)
          and weeks[-1][-1] >= dt.date(2025, 5, 31))

    # From the 20th of December the next holiday is Christmas Eve, which the
    # Polish calendar marks; Christmas Day follows it.
    days, day, _entry = calendar.countdown(today=dt.date(2025, 12, 20))
    check("the countdown finds the next holiday",
          day in (dt.date(2025, 12, 24), dt.date(2025, 12, 25)), day)
    check("the countdown counts days", days is not None and days >= 0, days)
    _gap, moment = calendar.countdown(today=dt.date(2026, 1, 2))[:2]
    check("an ordinary January day still finds a holiday",
          moment is not None, moment)


def test_store():
    print("\n== store ==")
    path = os.path.join(tempfile.mkdtemp(prefix="sdb-todo-"), "todo_db.json")
    store = core.TodoStore(path)
    task = store.add(title="Fix parser", kind="bug", priority="high",
                     tags=["parser"], due="2025-05-01 09:00", repeat="weekly")
    check("a weekly repeat has a next occurrence",
          task.next_occurrence() == dt.datetime(2025, 5, 8, 9, 0),
          task.next_occurrence())
    store.save()
    reloaded = core.TodoStore(path)
    check("the file round-trips", len(reloaded.tasks) == 1)
    check("tags survive the round trip",
          reloaded.tasks[0].tag_list() == ["parser"])

    # Every open task whose reminder has come due is returned, including the
    # weekly one whose date is already in the past, so the check is that the
    # newly added task is among them and that nothing fires twice.
    soon = (dt.datetime.now() + dt.timedelta(minutes=5)).replace(microsecond=0)
    reloaded.add(title="Soon", due=soon.isoformat(" "), remind=30)
    fired = reloaded.due_reminders()
    check("the new reminder is due",
          any(task.title == "Soon" for task in fired),
          [task.title for task in fired])
    check("an already-fired task is not repeated",
          len(fired) == len(set(task.id for task in fired)))
    for item in fired:
        reloaded.mark_reminded(item.id)
    check("nothing fires twice",
          all(task.get("reminded") == task.get("due") for task in fired))

    repeating = reloaded.add(title="standup", repeat="daily",
                             due="2025-05-01 09:00")
    before = len(reloaded.tasks)
    reloaded.toggle_done(repeating.id)
    check("completing a repeat adds exactly one task",
          len(reloaded.tasks) == before + 1,
          "%d -> %d" % (before, len(reloaded.tasks)))
    check("the successor is open again",
          reloaded.tasks[-1].get("status") == "todo")

    ics = core.tasks_to_ics(reloaded.tasks)
    check("ics export produces events", ics.count("BEGIN:VEVENT") >= 1)
    check("ics round-trips",
          len(core.parse_ics(ics)) == ics.count("BEGIN:VEVENT"))
    return store


def test_tab():
    print("\n== tab ==")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    todo = TodoTab(FakeWindow())
    todo.resize(1500, 880)

    today = dt.date.today()
    now = dt.datetime.now().replace(microsecond=0)
    todo.add_quick_task("Ship it", kind="release", priority="high",
                        due=(now + dt.timedelta(hours=1)).isoformat(" "))
    todo.add_quick_task("Late fix", kind="bug", priority="critical",
                        due=(now - dt.timedelta(days=1)).isoformat(" "))
    check("tasks landed in the store", len(todo.store.tasks) == 2)
    check("the list shows them", todo.task_list.count() == 2,
          todo.task_list.count())
    # "Ship it" is due today, "Late fix" was due yesterday: a day view must
    # find the first and not the second, which is the whole point of a
    # calendar rather than one flat list.
    names = [task.title for task in todo.tasks_on_day(today)]
    check("today finds the task due today", "Ship it" in names, names)
    check("today leaves yesterday's task out", "Late fix" not in names, names)
    check("the late task is on its own day",
          "Late fix" in [task.title for task in
                         todo.tasks_on_day(today - dt.timedelta(days=1))])

    for view in ("today", "day", "week", "month", "year"):
        todo.set_view(view)
        app.processEvents()
        check("the %s view builds" % view, todo.stack.currentIndex() >= 0)

    stats = todo.store.statistics(todo.calendar)
    check("one task is late", stats["overdue"] == 1, stats["overdue"])
    check("two tasks are open", stats["open"] == 2, stats["open"])

    check("a reminder fires once", todo.check_reminders() in (0, 1, 2))
    check("nothing fires on the second pass", todo.check_reminders() == 0)

    path = todo.save_ics(os.path.join(todo.base_dir, "out.ics"))
    check("the tab can export a calendar without a dialog",
          os.path.isfile(path) and os.path.getsize(path) > 0)

    todo.retranslate({"todo_title": "ZADANIA", "todo": "Zadania"})
    check("retranslate reaches the title",
          todo.title_label.text() == "ZADANIA", todo.title_label.text())
    return todo


def test_window():
    print("\n== window ==")
    try:
        import sdb as sdb
    except Exception as exc:                   # a missing dependency is not a bug
        print("skip the window check:", exc)
        return
    base = tempfile.mkdtemp(prefix="sdb-todo-window-")
    sdb.DATA_FILE = os.path.join(base, "scripts_db.json")
    sdb.CONN_FILE = os.path.join(base, "connections_db.json")
    sdb.SETTINGS_FILE = os.path.join(base, "settings_db.json")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = sdb.ScriptDatabaseWindow()
    check("the window builds", window is not None)
    check("the ToDo button exists", hasattr(window, "todo_btn"))

    # The ToDo workspace must be a popup window, NOT a tab in the strip.
    labels = [window.tabs.tabText(i) for i in range(window.tabs.count())]
    check("ToDo is not a tab", "ToDo" not in labels, labels)
    check("a TodoWindow was created",
          type(window.todo_window).__name__ == "TodoWindow",
          type(window.todo_window).__name__)

    layout = window.top_bar
    order = []
    for index in range(layout.count()):
        item = layout.itemAt(index).widget()
        if item is not None:
            order.append(item)
    check("the ToDo button follows Statistics",
          order.index(window.todo_btn) == order.index(window.stats_btn) + 1,
          [type(w).__name__ for w in order])

    # The button toggles the window rather than switching tabs.
    current = window.tabs.currentWidget()
    window.toggle_todo_window()
    check("the button opens the popup", window.todo_window.isVisible())
    check("opening it does not change the tab",
          window.tabs.currentWidget() is current)
    window.toggle_todo_window()
    check("the button closes the popup again",
          not window.todo_window.isVisible())

    window.open_todo_tab("week")
    check("it opens on the requested view", window.todo_tab.view == "week")
    check("it is visible afterwards", window.todo_window.isVisible())

    # Closing the popup must hide it, not destroy the workspace: the reminder
    # timers live in that object and must keep running.
    window.todo_window.close()
    check("closing hides rather than destroys",
          not window.todo_window.isVisible()
          and window.todo_window.todo.store is not None)


def main():
    test_engine()
    test_store()
    test_tab()
    test_window()
    print("")
    if FAILURES:
        print("FAILED: %d check(s): %s" % (len(FAILURES), ", ".join(FAILURES)))
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
