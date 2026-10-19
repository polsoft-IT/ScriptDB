# -*- coding: utf-8 -*-
"""Smoke test for the ToDo engine (tabs/todo_core.py), run from the repo root.

Everything here is pure logic, so it runs without Qt and without a display.
"""

import datetime as dt
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tabs import todo_core as C


def main():
    calendar = C.HolidayCalendar(["pl", "uk"])

    print("--- holidays ---")
    for day in (dt.date(2025, 5, 1), dt.date(2025, 5, 3), dt.date(2025, 5, 5),
                dt.date(2025, 5, 26), dt.date(2025, 12, 25),
                dt.date(2025, 8, 25), dt.date(2026, 1, 1)):
        print(day, day.strftime("%a"), "->", calendar.labels(day))

    print("--- calendar geometry ---")
    weeks = C.month_grid(2025, 5)
    print("May 2025:", len(weeks), "weeks",
          weeks[0][0], "->", weeks[-1][-1])
    print("week of 2025-05-01:", [d.day for d in C.week_days(dt.date(2025, 5, 1))])
    print("countdown from 2025-04-30:", calendar.countdown(dt.date(2025, 4, 30)))

    print("--- store ---")
    path = os.path.join(tempfile.mkdtemp(), "todo_db.json")
    store = C.TodoStore(path)
    task = store.add(title="Fix parser", kind="bug", priority="high",
                     tags=["parser", "urgent"], due="2025-05-02 10:00",
                     repeat="weekly")
    print("repeat ->", task.next_occurrence())
    store.save()
    reloaded = C.TodoStore(path)
    print("reloaded:", len(reloaded.tasks),
          reloaded.tasks[0].title, reloaded.tasks[0].tag_list())
    stats = reloaded.statistics(calendar)
    print("stats:", stats["total"], "total,", stats["open"], "open,",
          stats["overdue"], "overdue")
    print("about:", C.human_delta(dt.datetime.now() + dt.timedelta(hours=3)))

    print("--- reminders fire once ---")
    soon = (dt.datetime.now() + dt.timedelta(minutes=10)).replace(microsecond=0)
    reloaded.add(title="Remind me", due=soon.isoformat(" "), remind=30)
    fired = reloaded.due_reminders()
    print("fired:", [item.title for item in fired])
    for item in fired:
        reloaded.mark_reminded(item.id)
    print("fired again:", reloaded.due_reminders())

    print("--- recurrence on completion ---")
    repeat = reloaded.add(title="standup", repeat="daily", due="2025-05-01 09:00")
    before = len(reloaded.tasks)
    reloaded.toggle_done(repeat.id)
    print("tasks:", before, "->", len(reloaded.tasks))
    successor = reloaded.tasks[-1]
    print("successor due:", successor.get("due"),
          "status:", successor.get("status"))

    print("--- export ---")
    ics = C.tasks_to_ics(reloaded.tasks)
    print("ics events:", ics.count("BEGIN:VEVENT"))
    parsed = C.parse_ics(ics)
    print("ics round-trip:", len(parsed))
    print("markdown header:", C.task_to_markdown(reloaded.tasks[0]).splitlines()[0])

    print("--- calendar view models ---")
    buckets = reloaded.today()
    print("today buckets:", {key: len(value) for key, value in buckets.items()})
    print("templates:", len(C.template_tasks()))
    print("ENGINE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
