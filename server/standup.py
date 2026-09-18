"""Every twenty minutes: what exactly, how fast, and what's in the way.

A photo proves he is at the desk. It cannot tell the difference between
"solving q12 on page 40" and "re-reading the same paragraph for the fourth
time", and that difference is the whole day. "Doing math" is not an answer --
it is the shape of an answer, and it is exactly what someone says when they
have been drifting.

So three questions, on the twenty-minute marks inside a block:

1. what precisely are you doing right now -- the page, the slide, the problem;
2. are you actually moving, or drifting;
3. what is in the way, why, and what would get past it.

Answers are text, kept as a record of the block. Friday reads them and pushes
back on the vague ones, which is the only part a machine can usefully do here.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

SCHEMA = """
CREATE TABLE IF NOT EXISTS standups (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    date      TEXT NOT NULL,
    asked_at  TEXT NOT NULL,
    label     TEXT NOT NULL DEFAULT '',   -- the block it was asked in
    answer    TEXT,                       -- what he wrote back
    answer_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_standup_date ON standups(date);
"""

QUESTIONS = [
    ("quick check 📋\n"
     "1. what exactly are you doing right this second? (\"solving q12 p40\", "
     "not \"math\")\n"
     "2. moving fast or drifting?\n"
     "3. stuck on anything? what, why, and what would unstick it?"),
    ("standup time 👀\n"
     "1. the exact thing on your screen rn — which problem, page, slide?\n"
     "2. speed — actually shipping, or circling?\n"
     "3. what's in the way, and what's your next move on it?"),
    ("20 min check\n"
     "1. name the precise thing you're on (page, problem, slide)\n"
     "2. going fast or slow, honestly?\n"
     "3. anything blocking you? why, and how do you get past it?"),
]


class Standups:
    def __init__(self, db, cfg):
        self.db = db
        self.cfg = cfg
        db.conn.executescript(SCHEMA)
        db.conn.commit()

    @property
    def conf(self) -> dict:
        return self.cfg.raw.get("standup", {}) or {}

    @property
    def enabled(self) -> bool:
        return bool(self.conf.get("enabled", True))

    def every(self) -> int:
        return max(5, int(self.conf.get("every_min", 20)))

    def skip_categories(self) -> set[str]:
        return {str(x).lower() for x in self.conf.get(
            "skip_categories", ["commute", "meal", "sleep", "relax"])}

    def question(self, n: int) -> str:
        return QUESTIONS[n % len(QUESTIONS)]

    # -- records ---------------------------------------------------------------
    def ask(self, now: datetime, label: str) -> int:
        cur = self.db.run(
            "INSERT INTO standups (date, asked_at, label) VALUES (?, ?, ?)",
            (now.date().isoformat(), now.isoformat(), label or ""))
        return cur.lastrowid

    def pending(self, now: datetime, minutes: int = 25):
        """The last standup still waiting for an answer, if it's recent."""
        row = self.db.one(
            "SELECT * FROM standups WHERE answer IS NULL AND asked_at >= ?"
            " ORDER BY id DESC LIMIT 1",
            ((now - timedelta(minutes=minutes)).isoformat(),))
        return row

    def answer(self, standup_id: int, text: str, now: datetime) -> bool:
        return self.db.run(
            "UPDATE standups SET answer = ?, answer_at = ? WHERE id = ? AND answer IS NULL",
            (text.strip()[:1000], now.isoformat(), standup_id)).rowcount > 0

    def today(self, day: date) -> list:
        return self.db.q("SELECT * FROM standups WHERE date = ? ORDER BY id",
                         (day.isoformat(),))

    def count_asked(self, day: date) -> int:
        return len(self.today(day))

    def recent(self, day: date, limit: int = 4) -> list:
        rows = self.db.q(
            "SELECT * FROM standups WHERE date = ? AND answer IS NOT NULL"
            " ORDER BY id DESC LIMIT ?", (day.isoformat(), limit))
        return list(reversed(rows))

    def describe(self, day: date) -> str:
        """What he said he was doing, in his own words, for Friday to compare."""
        rows = self.recent(day)
        if not rows:
            return ""
        return "\n".join(
            f"  {r['asked_at'][11:16]} ({r['label']}): {r['answer']}" for r in rows)

    def score(self, day: date) -> tuple[int, int]:
        rows = self.today(day)
        return sum(1 for r in rows if r["answer"]), len(rows)
