"""The ten-minute loop: name the next tiny thing, do it, say it's done.

18 Oct 2026, his words: "every 10min I need to write the bot the next super
simple step that I am gonna do and then send text message that I did it and
write next super simple step. bro but i kinda dont want to send image every
10 min, what could be an alternative".

Text proves nothing -- anyone can type "done" -- so this does not replace the
photo. What it does is two things that a photo alone cannot:

1. It keeps the work in front of him in pieces small enough to start, in his
   own words, every ten minutes.
2. It makes the next photo harder to fake. The photo is checked against the
   step he just claimed, not only the block's name, so "done: wrote paragraph
   three" followed by a picture of something else does not pass.

And it buys him something real: while the loop is alive the photo cadence
relaxes from every 20 minutes to every 30. Stop answering and it tightens
straight back. The looser cadence is paid for by staying in the loop.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

SCHEMA = """
CREATE TABLE IF NOT EXISTS microsteps (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    date      TEXT NOT NULL,
    label     TEXT NOT NULL DEFAULT '',
    asked_at  TEXT NOT NULL,
    step      TEXT,
    step_at   TEXT,
    done_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_microsteps_date ON microsteps(date);
"""

DONE_WORDS = re.compile(
    r"^\s*(done|did it|did|finished|finish|complete[d]?|✅|✔|\+|ok done|"
    r"готово|сделал|сделано|всё|все)\s*[.!]*\s*$", re.I)

ASKS = [
    "next step? one tiny thing, in your words",
    "what's the next small step?",
    "next move — smallest version of it",
]


class Steps:
    def __init__(self, db, cfg):
        self.db = db
        self.cfg = cfg
        db.conn.executescript(SCHEMA)
        db.conn.commit()

    @property
    def conf(self) -> dict:
        return self.cfg.raw.get("micro", {}) or {}

    @property
    def enabled(self) -> bool:
        return bool(self.conf.get("enabled", False))

    def every(self) -> int:
        return max(5, int(self.conf.get("every_min", 10)))

    def skip_categories(self) -> set[str]:
        return {str(x).lower() for x in self.conf.get(
            "skip_categories", ["commute", "meal", "sleep", "relax"])}

    # -- the record ------------------------------------------------------------
    def ask(self, now: datetime, label: str) -> int:
        return self.db.run(
            "INSERT INTO microsteps (date, label, asked_at) VALUES (?, ?, ?)",
            (now.date().isoformat(), label or "", now.isoformat())).lastrowid

    def question(self, n: int) -> str:
        return ASKS[n % len(ASKS)]

    def waiting(self, now: datetime, minutes: int = 20):
        """The last prompt he hasn't written a step for."""
        return self.db.one(
            "SELECT * FROM microsteps WHERE step IS NULL AND asked_at >= ?"
            " ORDER BY id DESC LIMIT 1",
            ((now - timedelta(minutes=minutes)).isoformat(),))

    def open_step(self, now: datetime, minutes: int = 45):
        """The step he named and hasn't finished."""
        return self.db.one(
            "SELECT * FROM microsteps WHERE step IS NOT NULL AND done_at IS NULL"
            " AND step_at >= ? ORDER BY id DESC LIMIT 1",
            ((now - timedelta(minutes=minutes)).isoformat(),))

    def set_step(self, row_id: int, text: str, now: datetime) -> bool:
        return self.db.run(
            "UPDATE microsteps SET step = ?, step_at = ? WHERE id = ? AND step IS NULL",
            (text.strip()[:300], now.isoformat(), row_id)).rowcount > 0

    def mark_done(self, now: datetime) -> dict | None:
        row = self.open_step(now)
        if row is None:
            return None
        self.db.run("UPDATE microsteps SET done_at = ? WHERE id = ?",
                    (now.isoformat(), row["id"]))
        return dict(row)

    def start_free(self, text: str, now: datetime, label: str) -> int:
        """A step he wrote without being asked."""
        rid = self.ask(now, label)
        self.set_step(rid, text, now)
        return rid

    # -- how it reads ----------------------------------------------------------
    def alive(self, now: datetime, minutes: int | None = None) -> bool:
        """Is he actually in the loop right now?

        Anything he did -- named a step or finished one -- inside the window.
        This is what buys the slower photo cadence, so it has to mean activity
        and not merely that the bot asked something.
        """
        if not self.enabled:
            return False
        mins = int(self.conf.get("alive_min", 14) if minutes is None else minutes)
        since = (now - timedelta(minutes=mins)).isoformat()
        row = self.db.one(
            "SELECT 1 FROM microsteps WHERE (step_at >= ? OR done_at >= ?) LIMIT 1",
            (since, since))
        return row is not None

    def current_text(self, now: datetime) -> str:
        """What he says he is doing this minute, for the photo check."""
        row = self.open_step(now)
        if row is None:
            row = self.db.one(
                "SELECT * FROM microsteps WHERE done_at IS NOT NULL AND done_at >= ?"
                " ORDER BY id DESC LIMIT 1",
                ((now - timedelta(minutes=12)).isoformat(),))
        return (row["step"] or "").strip() if row else ""

    def today(self, day: date) -> list:
        return self.db.q("SELECT * FROM microsteps WHERE date = ? ORDER BY id",
                         (day.isoformat(),))

    def score(self, day: date) -> tuple[int, int]:
        rows = self.today(day)
        return sum(1 for r in rows if r["done_at"]), sum(1 for r in rows if r["step"])

    def describe(self, day: date, limit: int = 8) -> str:
        rows = [r for r in self.today(day) if r["step"]][-limit:]
        if not rows:
            return ""
        return "\n".join(
            f"  {r['step_at'][11:16]} {'✓' if r['done_at'] else '…'} {r['step']}"
            for r in rows)
