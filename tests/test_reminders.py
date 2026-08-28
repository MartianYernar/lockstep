"""Reminders: parsing, and firing at the three moments that matter."""
import os, sys
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fixtures import check, report
from server import reminders as R

TZ = ZoneInfo("Asia/Almaty")
NOW = datetime(2026, 8, 28, 12, 0, tzinfo=TZ)

print("\n=== every format anyone actually types ===")
for raw, want in [
    ("3 sep 14:40 doctor's appointment", "2026-09-03 14:40"),
    ("03.09 14:40 doctor",              "2026-09-03 14:40"),
    ("2026-09-03 14:40 dentist",        "2026-09-03 14:40"),
    ("sep 3 14:40 hospital",            "2026-09-03 14:40"),
    ("3rd september 14:40 doctor",      "2026-09-03 14:40"),
    ("tomorrow 09:00 call mum",         "2026-08-29 09:00"),
    ("today 23:00 take the bins out",   "2026-08-28 23:00"),
    ("tomorrow 14.40 dentist",          "2026-08-29 14:40"),
]:
    check(f"{raw[:34]!r}", R.parse(raw, NOW).when.strftime("%Y-%m-%d %H:%M"), want)

print("\n=== the date is not mistaken for the time ===")
# "03.09 14:40" contains two things that look like a clock; the first is a date.
check("03.09 14:40 reads as 3 Sep at 14:40",
      R.parse("03.09 14:40 doctor", NOW).when.strftime("%d %b %H:%M"), "03 Sep 14:40")
check("and dotted-only still works",
      R.parse("03.09 14.40 doctor", NOW).when.strftime("%d %b %H:%M"), "03 Sep 14:40")

print("\n=== a bare date already past means next year ===")
dec = datetime(2026, 12, 20, 12, 0, tzinfo=TZ)
check("'3 sep' typed in December is 2027", R.parse("3 sep 14:40 x", dec).when.year, 2027)

print("\n=== refusals ===")
for bad, why in [("doctor appointment", "no time"),
                 ("3 sep doctor", "no time"),
                 ("3 sep 99:99 x", "not a real time"),
                 ("3 sep 14:40", "no text")]:
    try:
        R.parse(bad, NOW)
        check(f"{why}: refused", "accepted", "refused")
    except R.ParseError:
        check(f"{why}: refused", "refused", "refused")

print("\n=== fires three times, each once ===")
when = datetime(2026, 9, 3, 14, 40, tzinfo=TZ)
eve, morn, soon = time(20, 0), time(8, 0), 60
seq = [
    (datetime(2026, 9, 1, 12, 0, tzinfo=TZ), None,   "two days out: nothing"),
    (datetime(2026, 9, 2, 19, 0, tzinfo=TZ), None,   "evening before, too early"),
    (datetime(2026, 9, 2, 20, 1, tzinfo=TZ), "eve",  "evening before"),
    (datetime(2026, 9, 3, 7, 0, tzinfo=TZ),  None,   "before the morning nag"),
    (datetime(2026, 9, 3, 8, 1, tzinfo=TZ),  "morn", "morning of"),
    (datetime(2026, 9, 3, 13, 0, tzinfo=TZ), None,   "midday, nothing owed"),
    (datetime(2026, 9, 3, 13, 45, tzinfo=TZ), "soon", "an hour before"),
    (datetime(2026, 9, 3, 14, 30, tzinfo=TZ), None,  "already warned"),
    (datetime(2026, 9, 3, 15, 30, tzinfo=TZ), None,  "after the fact, silence"),
]
fired = set()
for now, want, why in seq:
    got, settled = R.due_stages(when, now, fired, eve, morn, soon)
    check(why, got, want)
    fired |= settled
check("all three fired, no more", sorted(fired), ["eve", "morn", "soon"])

print("\n=== a reminder set the same morning still gets its nags ===")
late = datetime(2026, 9, 3, 9, 0, tzinfo=TZ)
stage, settled = R.due_stages(when, late, set(), eve, morn, soon)
check("sends 'this morning', not a stale 'tomorrow'", stage, "morn")
check("and the evening nag is written off, not queued", "eve" in settled, True)

sys.exit(report("REMINDER TESTS"))
