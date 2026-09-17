"""The small things every night, which everything else stands on.

Clean socks, tomorrow's clothes laid out, teeth done. None of them is a plan
block and none is worth a photo, but a morning without them starts behind --
and they are exactly the things that slip on a day that went badly, which is
when the next day most needs a clean start.

So they are a checklist, not a checkpoint: sent near the end of the day with a
button each, nudged once more before bed if anything is left, and counted in
the nightly report. Nothing sirens over socks.
"""
from __future__ import annotations

from datetime import date, datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS foundations (
    date     TEXT NOT NULL,
    idx      INTEGER NOT NULL,
    text     TEXT NOT NULL,
    done_at  TEXT,
    PRIMARY KEY (date, idx)
);
"""

DEFAULT_ITEMS = ["wash socks", "prepare clothes for tomorrow", "mouth hygiene"]


class Foundations:
    def __init__(self, db, cfg):
        self.db = db
        self.cfg = cfg
        db.conn.executescript(SCHEMA)
        db.conn.commit()

    @property
    def conf(self) -> dict:
        return self.cfg.raw.get("foundations", {}) or {}

    @property
    def enabled(self) -> bool:
        return bool(self.conf.get("enabled", True)) and bool(self.items())

    def items(self) -> list[str]:
        return [str(x).strip() for x in self.conf.get("items", DEFAULT_ITEMS) if str(x).strip()]

    def for_day(self, day: date) -> list:
        """Tonight's list. Created on first look, from whatever config says then."""
        rows = self.db.q("SELECT * FROM foundations WHERE date = ? ORDER BY idx",
                         (day.isoformat(),))
        if rows:
            return rows
        self.db.conn.executemany(
            "INSERT OR IGNORE INTO foundations (date, idx, text) VALUES (?, ?, ?)",
            [(day.isoformat(), i, t) for i, t in enumerate(self.items(), start=1)])
        self.db.conn.commit()
        return self.db.q("SELECT * FROM foundations WHERE date = ? ORDER BY idx",
                         (day.isoformat(),))

    def tick(self, day: date, idx: int, now: datetime) -> bool:
        self.for_day(day)
        return self.db.run(
            "UPDATE foundations SET done_at = ? WHERE date = ? AND idx = ? AND done_at IS NULL",
            (now.isoformat(), day.isoformat(), idx)).rowcount > 0

    def find(self, day: date, text: str):
        """An item by number or by a word in it ("socks", "teeth")."""
        t = (text or "").strip().lower()
        rows = self.for_day(day)
        if t.isdigit():
            return next((r for r in rows if r["idx"] == int(t)), None)
        synonyms = {"teeth": "mouth", "brush": "mouth", "floss": "mouth",
                    "clothes": "clothes", "outfit": "clothes", "socks": "socks"}
        for word, key in synonyms.items():
            if word in t:
                t = key
                break
        hits = [r for r in rows if t and (t in r["text"].lower()
                                          or any(w in r["text"].lower() for w in t.split()))]
        return hits[0] if len(hits) == 1 else None

    def left(self, day: date) -> list:
        return [r for r in self.for_day(day) if not r["done_at"]]

    def night_of(self, now: datetime) -> date:
        """After midnight it's still last night's list until morning."""
        from datetime import timedelta
        return now.date() - timedelta(days=1) if now.hour < 5 else now.date()

    def describe(self, day: date) -> str:
        rows = self.for_day(day)
        return "; ".join(f"{r['text']} {'(done)' if r['done_at'] else '(not yet)'}" for r in rows)
