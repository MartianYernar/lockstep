"""One-off reminders, nagged at the moments you'd actually want them.

A doctor's appointment at 14:40 on 3 September is no use to you as a calendar
entry you never open. It's useful the evening before, while you're writing the
next day's plan and could put it in — and again that morning, before the day
gets away from you.

So each reminder fires three times: the evening before, the morning of, and
shortly before the thing itself.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    # the months as they get typed here
    "янв": 1, "фев": 2, "мар": 3, "апр": 4, "мая": 5, "май": 5, "июн": 6,
    "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12,
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS reminders (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    at_ts      TEXT NOT NULL,
    text       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    fired      TEXT NOT NULL DEFAULT ''   -- comma list: eve,morn,soon
);
CREATE INDEX IF NOT EXISTS idx_rem_at ON reminders(at_ts);
"""


class ParseError(ValueError):
    """Message is written to be shown to the user."""


@dataclass
class Parsed:
    when: datetime
    text: str


_TIME = re.compile(r"\b(\d{1,2})([:.])(\d{2})\b")


def _find_time(s: str):
    """Pick the clock out of a string that also contains a date.

    "03.09 14:40" has two things that look like a time, and the first one is
    the date. A colon is only ever a clock, so colon matches win outright; if
    there are none, the LAST dotted pair is the time, because dates come first
    in every format anyone actually writes.
    """
    matches = list(_TIME.finditer(s))
    if not matches:
        return None
    colons = [m for m in matches if m.group(2) == ":"]
    return (colons[-1] if colons else matches[-1])


def parse(raw: str, now: datetime) -> Parsed:
    """Read '3 sep 14:40 doctor', '03.09 14:40 doctor', 'tomorrow 09:00 dentist'."""
    s = raw.strip()
    if not s:
        raise ParseError("Nothing to remind you about.")

    m = _find_time(s)
    if not m:
        raise ParseError(
            "I need a time. Try:\n"
            "<code>/remind 3 sep 14:40 doctor's appointment</code>")
    hh, mi = int(m.group(1)), int(m.group(3))
    if not (0 <= hh < 24 and 0 <= mi < 60):
        raise ParseError(f"'{m.group(0)}' isn't a real time.")
    before, after = s[:m.start()].strip(), s[m.end():].strip()

    day, leftover = _parse_day(before, now)
    # "3 sep 14:40" with nothing after it used to fall back to using the date
    # itself as the reminder text, which produced a reminder called "3 sep".
    text = (after or leftover).strip(" ,–-")
    if not text:
        raise ParseError("Say what the reminder is for, not just when.")

    when = datetime.combine(day, datetime.min.time(),
                            tzinfo=now.tzinfo).replace(hour=hh, minute=mi)
    if when <= now:
        raise ParseError(f"{when:%d %b %H:%M} is in the past.")
    if when > now + timedelta(days=365):
        raise ParseError("More than a year out. Use a calendar for that.")
    return Parsed(when, text)


def _parse_day(chunk: str, now: datetime) -> tuple[date, str]:
    """Returns the date and whatever text was left after removing it."""
    c = chunk.lower().strip()
    if not c:
        return now.date(), ""
    for word, day in (("today", now.date()), ("сегодня", now.date()),
                      ("tomorrow", now.date() + timedelta(days=1)),
                      ("tmr", now.date() + timedelta(days=1)),
                      ("завтра", now.date() + timedelta(days=1))):
        if word in c:
            return day, chunk[:c.index(word)] + chunk[c.index(word) + len(word):]

    # 2026-09-03
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", c)
    if m:
        return (date(int(m.group(1)), int(m.group(2)), int(m.group(3))),
                chunk[:m.start()] + chunk[m.end():])

    # 03.09 / 03.09.2026 / 3/9
    m = re.search(r"\b(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\b", c)
    if m:
        d, mo = int(m.group(1)), int(m.group(2))
        y = int(m.group(3) or now.year)
        if y < 100:
            y += 2000
        return _guard(date(y, mo, d), now), chunk[:m.start()] + chunk[m.end():]

    # 3 sep / sep 3 / 3rd september
    m = re.search(r"\b(\d{1,2})\s*(?:st|nd|rd|th)?\s*([a-zа-я]{3,})", c)
    if not m:
        m2 = re.search(r"\b([a-zа-я]{3,})\s*(\d{1,2})", c)
        if m2:
            mo = MONTHS.get(m2.group(1)[:3])
            if mo:
                return (_guard(date(now.year, mo, int(m2.group(2))), now),
                        chunk[:m2.start()] + chunk[m2.end():])
        raise ParseError(
            f"I couldn't read a date in '{chunk}'.\n"
            "Try <code>3 sep</code>, <code>03.09</code>, <code>2026-09-03</code> "
            "or <code>tomorrow</code>.")
    mo = MONTHS.get(m.group(2)[:3])
    if not mo:
        raise ParseError(f"'{m.group(2)}' isn't a month I know.")
    return (_guard(date(now.year, mo, int(m.group(1))), now),
            chunk[:m.start()] + chunk[m.end():])


def _guard(d: date, now: datetime) -> date:
    """A bare '3 sep' typed in December means next year, not nine months ago."""
    if d < now.date():
        try:
            return d.replace(year=d.year + 1)
        except ValueError:                      # 29 Feb
            return d.replace(year=d.year + 1, day=28)
    return d


def due_stages(when: datetime, now: datetime, fired: set[str],
               eve_at, morn_at, soon_min: int) -> tuple[str | None, set[str]]:
    """Which nag to send now, and which stages are settled by sending it.

    Returns the *latest* stage that has come due, not the earliest still owed.
    A reminder created on the morning of the appointment would otherwise open
    with "Tomorrow" — technically the first unfired stage, and obviously wrong
    to read. Everything earlier is marked done at the same time, so a stage
    whose moment has passed never surfaces late.
    """
    eve = datetime.combine(when.date() - timedelta(days=1), eve_at, tzinfo=now.tzinfo)
    morn = datetime.combine(when.date(), morn_at, tzinfo=now.tzinfo)
    soon = when - timedelta(minutes=soon_min)
    stages = [("eve", eve), ("morn", morn), ("soon", soon)]

    if now > when:
        return None, {s for s, _ in stages}          # the moment has gone

    due = [s for s, at in stages if at <= now]
    if not due:
        return None, set()
    latest = due[-1]
    if latest in fired:
        return None, set(due)
    return latest, set(due)
