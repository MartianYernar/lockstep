"""Declare the finish line before the block, prove it after.

18 Oct 2026. He asked how to mandate the work when the ten-minute step loop is
only text, and typing "done" costs nothing. The honest answer is that no proof
of PRESENCE can do it: you can sit in front of a laptop for ninety minutes
with the right document open and produce nothing, and until today the system
would have called that a perfect block.

His own objection to counting words: "not every task is text and is measured
in different things, like sometimes it is uni app filling or finishing
gathering doc or writing essay or solving math questions... I feel like
there's something more foundational deeper".

This is that. No metric generalises across those tasks, but one sentence does:

    at the end of this block, something will exist that does not exist now.

So he names it himself, at the start, while he is fresh and honest -- "the
activities section showing 5 entries", "questions 12-20 worked in my
notebook", "the transcript sitting in the Drive folder" -- and at the end the
checker is shown his own words, the photo from the start of the block and the
photo from the end, and asked one question: is it there?

Three properties that matter:

- It generalises, because he picks the artifact, not the system.
- It is falsifiable: "5 entries" against a photo showing two is a refusal, and
  the refusal quotes him to himself.
- The declaration is locked when it is made. At 16:00 he is honest about what
  ninety minutes should produce; at 17:30, tired, he would like it to have
  been enough. Only the first version counts.

The hole this does not close: he can declare something trivial. No code fixes
that -- Friday pushes back when a target looks thin, and the declaration and
its result both go in the nightly report, which is read by his father. A
system you administer alone bends to whoever you are at 21:00.
"""
from __future__ import annotations

import json
from datetime import date, datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS deliverables (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT NOT NULL,
    label       TEXT NOT NULL,
    start_min   INTEGER NOT NULL,
    end_min     INTEGER NOT NULL,
    declared    TEXT,
    declared_at TEXT,
    state       TEXT NOT NULL DEFAULT 'open',   -- open|delivered|short|void
    verdict     TEXT,
    settled_at  TEXT,
    before_path TEXT,
    UNIQUE (date, start_min, label)
);
CREATE INDEX IF NOT EXISTS idx_deliverables_date ON deliverables(date);
"""


class Deliverables:
    def __init__(self, db, cfg):
        self.db = db
        self.cfg = cfg
        db.conn.executescript(SCHEMA)
        db.conn.commit()

    # -- settings --------------------------------------------------------------
    @property
    def conf(self) -> dict:
        return self.cfg.raw.get("deliver", {}) or {}

    @property
    def enabled(self) -> bool:
        return bool(self.conf.get("enabled", False))

    def min_block(self) -> int:
        """Short blocks are exempt: a fifteen-minute block has no deliverable
        worth naming, and demanding one would only teach him to type noise."""
        return int(self.conf.get("min_block_min", 45))

    def skip_categories(self) -> set[str]:
        return {str(x).lower() for x in self.conf.get(
            "skip_categories", ["commute", "meal", "sleep", "relax", "clean",
                                "skincare", "sport"])}

    def wants(self, block) -> bool:
        if not self.enabled or block is None:
            return False
        if (block["category"] or "").lower() in self.skip_categories():
            return False
        return (block["end_min"] - block["start_min"]) >= self.min_block()

    # -- the record ------------------------------------------------------------
    def open_for(self, day: date, block) -> dict | None:
        row = self.db.one(
            "SELECT * FROM deliverables WHERE date=? AND start_min=? AND label=?",
            (day.isoformat(), block["start_min"], block["label"]))
        return dict(row) if row else None

    def ensure(self, day: date, block) -> dict:
        row = self.open_for(day, block)
        if row is not None:
            return row
        self.db.run(
            "INSERT OR IGNORE INTO deliverables (date, label, start_min, end_min)"
            " VALUES (?, ?, ?, ?)",
            (day.isoformat(), block["label"], block["start_min"], block["end_min"]))
        return self.open_for(day, block)

    def declare(self, day: date, block, text: str, now: datetime) -> bool:
        """His own words, written once. A second attempt does not overwrite."""
        self.ensure(day, block)
        return self.db.run(
            "UPDATE deliverables SET declared=?, declared_at=? WHERE date=? AND"
            " start_min=? AND label=? AND declared IS NULL",
            (text.strip()[:400], now.isoformat(), day.isoformat(),
             block["start_min"], block["label"])).rowcount > 0

    def set_before(self, day: date, block, path: str | None) -> None:
        if not path:
            return
        self.db.run(
            "UPDATE deliverables SET before_path=? WHERE date=? AND start_min=?"
            " AND label=? AND before_path IS NULL",
            (path, day.isoformat(), block["start_min"], block["label"]))

    def settle(self, row_id: int, state: str, verdict: dict, now: datetime) -> None:
        self.db.run(
            "UPDATE deliverables SET state=?, verdict=?, settled_at=? WHERE id=?",
            (state, json.dumps(verdict), now.isoformat(), row_id))

    def declared_for(self, day: date, block) -> str:
        row = self.open_for(day, block)
        return (row or {}).get("declared") or ""

    def unsettled(self, day: date, minute: int) -> list[dict]:
        """Blocks that have ended, were declared, and nobody has judged yet."""
        return [dict(r) for r in self.db.q(
            "SELECT * FROM deliverables WHERE date=? AND state='open'"
            " AND declared IS NOT NULL AND end_min <= ? ORDER BY start_min",
            (day.isoformat(), minute))]

    def today(self, day: date) -> list[dict]:
        return [dict(r) for r in self.db.q(
            "SELECT * FROM deliverables WHERE date=? ORDER BY start_min",
            (day.isoformat(),))]

    def score(self, day: date) -> tuple[int, int]:
        rows = [r for r in self.today(day) if r["declared"]]
        return sum(1 for r in rows if r["state"] == "delivered"), len(rows)

    def describe(self, day: date) -> str:
        rows = [r for r in self.today(day) if r["declared"]]
        if not rows:
            return ""
        mark = {"delivered": "✅", "short": "❌", "open": "…", "void": "—"}
        out = []
        for r in rows:
            v = json.loads(r["verdict"]) if r["verdict"] else {}
            got = v.get("what_changed") or ""
            out.append(f"  {mark.get(r['state'], '?')} {r['start_min']//60:02d}:"
                       f"{r['start_min']%60:02d} {r['label'][:38]} — said: "
                       f"{r['declared'][:70]}" + (f" | got: {got[:60]}" if got else ""))
        return "\n".join(out)
