"""His fixed weekly commitments: things the day is built around, not out of.

11 Oct 2026, a Sunday. At 10:02 he told Friday he was out teaching until 11am,
then asked for a plan. The plan that came back started "10:04-11:30 Do U of T
application" -- while he was standing in front of a class -- and the siren
began asking for a photo of it. He teaches every weekend 10:00-11:00 and gets
home at 12:00. That is not something to re-explain to a model each Saturday;
it is a fact about his week, so it lives here and the app writes it into
every plan itself.

Routines cost no photo by default. A teaching hour or a bus ride is not
something he chose over work, and a photo of a classroom is a photo of other
people's faces.
"""
from __future__ import annotations

import re
from datetime import date, datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS routines (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    days       TEXT NOT NULL,              -- "5,6" -- Mon=0 .. Sun=6, "" if dated
    start_min  INTEGER NOT NULL,
    end_min    INTEGER NOT NULL,
    label      TEXT NOT NULL,
    photo      INTEGER NOT NULL DEFAULT 0,
    active     INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
"""

# Added later: a routine can land on ONE date instead of a weekday, and it can
# be quiet -- nothing asked, nothing sounded, for an exam.
MIGRATIONS = {"on_date": "TEXT", "quiet": "INTEGER NOT NULL DEFAULT 0"}

DAY_NAMES = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
             "пн": 0, "вт": 1, "ср": 2, "чт": 3, "пт": 4, "сб": 5, "вс": 6}


def parse_days(text: str) -> list[int]:
    """'weekends', 'sat,sun', 'weekdays', 'every day', 'mon wed fri', '5,6'."""
    t = (text or "").lower()
    if not t.strip():
        return []
    if "weekend" in t or "выходн" in t:
        return [5, 6]
    if "weekday" in t or "будн" in t:
        return [0, 1, 2, 3, 4]
    if "every" in t or "daily" in t or "каждый" in t:
        return list(range(7))
    found = {int(x) for x in re.findall(r"\b[0-6]\b", t)}
    for word, idx in DAY_NAMES.items():
        if re.search(rf"\b{word}", t):
            found.add(idx)
    return sorted(found)


def parse_span(text: str) -> tuple[int, int] | None:
    m = re.search(r"(\d{1,2})[:.](\d{2})\s*[-–—to]+\s*(\d{1,2})[:.](\d{2})", text or "")
    if not m:
        return None
    a = int(m[1]) * 60 + int(m[2])
    b = int(m[3]) * 60 + int(m[4])
    return (a, b) if 0 <= a < b <= 24 * 60 else None


class Routines:
    def __init__(self, db):
        self.db = db
        db.conn.executescript(SCHEMA)
        have = {r["name"] for r in db.conn.execute("PRAGMA table_info(routines)")}
        for col, decl in MIGRATIONS.items():
            if col not in have:
                db.conn.execute(f"ALTER TABLE routines ADD COLUMN {col} {decl}")
        db.conn.commit()

    def add(self, days: list[int], start: int, end: int, label: str, now: datetime,
            photo: bool = False, on_date: date | None = None,
            quiet: bool = False) -> int | None:
        label = (label or "").strip()
        if (not days and on_date is None) or not label or not 0 <= start < end <= 24 * 60:
            return None
        key = ",".join(str(d) for d in sorted(set(days or [])))
        iso = on_date.isoformat() if on_date else None
        have = self.db.one(
            "SELECT id FROM routines WHERE active=1 AND days=? AND start_min=? AND end_min=?"
            " AND IFNULL(on_date,'')=IFNULL(?,'') AND lower(label)=lower(?)",
            (key, start, end, iso, label))
        if have:
            return have["id"]
        return self.db.run(
            "INSERT INTO routines (days, start_min, end_min, label, photo, created_at,"
            " on_date, quiet) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (key, start, end, label, 1 if photo else 0, now.isoformat(),
             iso, 1 if quiet else 0)).lastrowid

    def remove(self, rid: int) -> bool:
        return self.db.run("UPDATE routines SET active=0 WHERE id=? AND active=1",
                           (rid,)).rowcount > 0

    def seed(self, items, now: datetime) -> int:
        """Config routines, added once each. One he removed stays removed."""
        n = 0
        for it in items or []:
            span = parse_span(str(it.get("time", "")))
            days = parse_days(str(it.get("days", "")))
            on = it.get("date")
            if on and not isinstance(on, date):
                try:
                    on = date.fromisoformat(str(on)[:10])
                except ValueError:
                    on = None
            if not span or (not days and not on):
                continue
            key = ",".join(str(d) for d in (days or []))
            if self.db.one("SELECT 1 FROM routines WHERE days=? AND start_min=? AND end_min=?"
                           " AND IFNULL(on_date,'')=IFNULL(?,'') AND lower(label)=lower(?)",
                           (key, span[0], span[1], on.isoformat() if on else None,
                            str(it.get("label", "")).strip())):
                continue
            if self.add(days, span[0], span[1], str(it.get("label", "")), now,
                        photo=bool(it.get("photo", False)), on_date=on,
                        quiet=bool(it.get("quiet", False))):
                n += 1
        return n

    def all(self) -> list:
        return self.db.q("SELECT * FROM routines WHERE active=1 ORDER BY days, start_min")

    def for_day(self, day: date) -> list:
        wd = str(day.weekday())
        iso = day.isoformat()
        return [r for r in self.all()
                if (r["on_date"] == iso if r["on_date"]
                    else wd in (r["days"] or "").split(","))]

    def quiet_now(self, now: datetime):
        """The quiet window covering this minute, if any. Nothing is asked or
        sounded inside one: on 18 Oct he sits an IELTS exam from 10:00 to
        15:00, and a siren in an exam hall is not accountability."""
        minute = now.hour * 60 + now.minute
        for r in self.for_day(now.date()):
            if r["quiet"] and r["start_min"] <= minute < r["end_min"]:
                return r
        return None

    def quiet_spans(self, day: date) -> list[tuple[int, int]]:
        return [(r["start_min"], r["end_min"]) for r in self.for_day(day) if r["quiet"]]

    def photo_free(self, day: date, start: int, end: int, label: str) -> bool:
        return any(r["label"] == label and not r["photo"]
                   and r["start_min"] < end and start < r["end_min"]
                   for r in self.for_day(day))

    def describe(self) -> str:
        names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        out = []
        for r in self.all():
            if r["on_date"]:
                when = f"on {date.fromisoformat(r['on_date']):%a %d %b}"
            else:
                ds = [int(x) for x in (r["days"] or "").split(",") if x != ""]
                when = ("weekends" if ds == [5, 6] else "weekdays" if ds == [0, 1, 2, 3, 4]
                        else "every day" if len(ds) == 7
                        else ",".join(names[d] for d in ds))
            out.append(f"  r{r['id']}: {when} {r['start_min']//60:02d}:{r['start_min']%60:02d}-"
                       f"{r['end_min']//60:02d}:{r['end_min']%60:02d} {r['label']}"
                       + (" (SILENT: nothing asked, no siren)" if r["quiet"]
                          else "" if r["photo"] else " (no photo)"))
        return "\n".join(out)

    def overlay(self, day: date, blocks: list, start: int, end: int, planner) -> list:
        """Write that day's routines into a plan, cutting whatever was there.

        Whatever the model produced, the fixed hours come out right: this is
        arithmetic, not a request.
        """
        fixed = [(max(r["start_min"], start), min(r["end_min"], end), r["label"])
                 for r in self.for_day(day) if r["end_min"] > start and r["start_min"] < end]
        if not fixed:
            return blocks
        out = []
        for b in blocks:
            pieces = [(b.start_min, b.end_min)]
            for fs, fe, _ in fixed:
                nxt = []
                for s, e in pieces:
                    if e <= fs or s >= fe:
                        nxt.append((s, e))
                        continue
                    if s < fs:
                        nxt.append((s, fs))
                    if e > fe:
                        nxt.append((fe, e))
                pieces = nxt
            for s, e in pieces:
                if e - s >= 5:
                    out.append(planner.Block(s, e, b.label, b.category))
        for fs, fe, label in fixed:
            if fe > fs:
                out.append(planner.Block(fs, fe, label, planner.categorise(label)))
        out.sort(key=lambda x: x.start_min)
        # close any hole a dropped sliver left by stretching its neighbour
        fixed_out = []
        for blk in out:
            if fixed_out and blk.start_min > fixed_out[-1].end_min:
                prev = fixed_out[-1]
                fixed_out[-1] = planner.Block(prev.start_min, blk.start_min, prev.label,
                                              prev.category)
            fixed_out.append(blk)
        return fixed_out
