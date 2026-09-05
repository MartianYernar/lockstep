"""SQLite storage.  One file, no server, trivially backed up."""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import date, datetime
from typing import Any, Iterable

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS days (
    date              TEXT PRIMARY KEY,
    plan_submitted_at TEXT,
    plan_locked       INTEGER NOT NULL DEFAULT 0,
    offsets_used      INTEGER NOT NULL DEFAULT 0,
    offset_minutes    INTEGER NOT NULL DEFAULT 0,
    offset_reason     TEXT,
    checkpoints_built INTEGER NOT NULL DEFAULT 0,
    reported          INTEGER NOT NULL DEFAULT 0,
    extend_minutes    INTEGER NOT NULL DEFAULT 0,
    -- Minutes /latenight bought for this night, and why. 0 on a normal day.
    bed_offset_minutes INTEGER NOT NULL DEFAULT 0,
    bed_reason        TEXT
);

CREATE TABLE IF NOT EXISTS blocks (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    date      TEXT NOT NULL,
    start_min INTEGER NOT NULL,
    end_min   INTEGER NOT NULL,
    label     TEXT NOT NULL,
    category  TEXT NOT NULL DEFAULT 'other',
    FOREIGN KEY (date) REFERENCES days(date)
);
CREATE INDEX IF NOT EXISTS idx_blocks_date ON blocks(date);

-- The 3-4 things that actually matter today, written before the timetable.
-- A plan says where the hours go; it does not say what the day was for. Six
-- days of history had blocks like "work: analysing" with no way to tell
-- afterwards whether anything was achieved.
CREATE TABLE IF NOT EXISTS goals (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    date  TEXT NOT NULL,
    idx   INTEGER NOT NULL,
    text  TEXT NOT NULL,
    done  INTEGER NOT NULL DEFAULT 0,
    UNIQUE(date, idx)
);
CREATE INDEX IF NOT EXISTS idx_goals_date ON goals(date);

CREATE TABLE IF NOT EXISTS checkpoints (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    date         TEXT NOT NULL,
    due_at       TEXT NOT NULL,           -- ISO 8601, timezone aware
    block_id     INTEGER,
    kind         TEXT NOT NULL DEFAULT 'activity',   -- see engine.PROOF_KINDS
    state        TEXT NOT NULL DEFAULT 'scheduled',  -- scheduled|asked|satisfied|missed|excused
    asked_at     TEXT,
    settled_at   TEXT,
    attempts     INTEGER NOT NULL DEFAULT 0,
    verdict      TEXT,
    photo_path   TEXT,
    UNIQUE(date, due_at)
);
CREATE INDEX IF NOT EXISTS idx_cp_date ON checkpoints(date);
CREATE INDEX IF NOT EXISTS idx_cp_state ON checkpoints(state);

CREATE TABLE IF NOT EXISTS lock_state (
    id                INTEGER PRIMARY KEY CHECK (id = 1),
    enabled           INTEGER NOT NULL DEFAULT 1,
    guardian_hash     TEXT,
    guardian_set_at   TEXT,
    commitment_end    TEXT,
    paused_until      TEXT,
    failed_attempts   INTEGER NOT NULL DEFAULT 0,
    last_attempt_at   TEXT,
    last_tick_at      TEXT,
    last_agent_poll   TEXT,
    guardian_chat_id  INTEGER,
    blocked_since     TEXT,
    siren_test_until  TEXT,
    muted_until       TEXT,
    apps_open_until   TEXT,
    apps_opened_at    TEXT,
    stop_at           TEXT,
    stop_reason       TEXT
);

-- "Has this already happened?" for things that must fire exactly once.
-- The tick runs every 30 seconds, so anything not driven by a state change
-- needs a key here or it turns into a message flood.
CREATE TABLE IF NOT EXISTS flags (
    key TEXT PRIMARY KEY,
    ts  TEXT NOT NULL
);

-- Append-only, hash-chained.  See ledger.py.
CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    kind      TEXT NOT NULL,
    payload   TEXT NOT NULL DEFAULT '{}',
    prev_hash TEXT NOT NULL,
    hash      TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        from .reminders import SCHEMA as REMINDER_SCHEMA
        self.conn.executescript(REMINDER_SCHEMA)
        self._migrate()
        self.conn.execute("INSERT OR IGNORE INTO lock_state (id) VALUES (1)")
        self.conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created."""
        dhave = {r["name"] for r in self.conn.execute("PRAGMA table_info(days)")}
        if "extend_minutes" not in dhave:
            self.conn.execute("ALTER TABLE days ADD COLUMN extend_minutes INTEGER NOT NULL DEFAULT 0")
        if "bed_offset_minutes" not in dhave:
            # Minutes /latenight bought for this night, 0 on a normal day.
            self.conn.execute(
                "ALTER TABLE days ADD COLUMN bed_offset_minutes INTEGER NOT NULL DEFAULT 0")
        if "bed_reason" not in dhave:
            self.conn.execute("ALTER TABLE days ADD COLUMN bed_reason TEXT")
        chave = {r["name"] for r in self.conn.execute("PRAGMA table_info(checkpoints)")}
        if "kind" not in chave:
            self.conn.execute(
                "ALTER TABLE checkpoints ADD COLUMN kind TEXT NOT NULL DEFAULT 'activity'")
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(lock_state)")}
        for col, decl in (("last_tick_at", "TEXT"), ("last_agent_poll", "TEXT"),
                          ("guardian_chat_id", "INTEGER"), ("blocked_since", "TEXT"),
                          ("siren_test_until", "TEXT"),
                          ("muted_until", "TEXT"),
                          ("apps_open_until", "TEXT"), ("apps_opened_at", "TEXT"),
                          ("stop_at", "TEXT"), ("stop_reason", "TEXT")):
            if col not in have:
                self.conn.execute(f"ALTER TABLE lock_state ADD COLUMN {col} {decl}")
        self.conn.commit()

    # -- thin helpers ----------------------------------------------------------
    def q(self, sql: str, args: Iterable = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, tuple(args)).fetchall()

    def one(self, sql: str, args: Iterable = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, tuple(args)).fetchone()

    def run(self, sql: str, args: Iterable = ()) -> sqlite3.Cursor:
        cur = self.conn.execute(sql, tuple(args))
        self.conn.commit()
        return cur

    # -- day bookkeeping -------------------------------------------------------
    def ensure_day(self, day: date) -> sqlite3.Row:
        d = day.isoformat()
        self.run("INSERT OR IGNORE INTO days (date) VALUES (?)", (d,))
        return self.one("SELECT * FROM days WHERE date = ?", (d,))

    def day(self, day: date) -> sqlite3.Row | None:
        return self.one("SELECT * FROM days WHERE date = ?", (day.isoformat(),))

    def blocks(self, day: date) -> list[sqlite3.Row]:
        return self.q(
            "SELECT * FROM blocks WHERE date = ? ORDER BY start_min", (day.isoformat(),)
        )

    def replace_blocks(self, day: date, blocks: list[tuple[int, int, str, str]]) -> None:
        d = day.isoformat()
        self.run("DELETE FROM blocks WHERE date = ?", (d,))
        self.conn.executemany(
            "INSERT INTO blocks (date, start_min, end_min, label, category)"
            " VALUES (?, ?, ?, ?, ?)",
            [(d, s, e, lbl, cat) for (s, e, lbl, cat) in blocks],
        )
        self.conn.commit()
        self.reattach_checkpoints(day)

    def reattach_checkpoints(self, day: date) -> int:
        """Re-point existing checkpoints at the rebuilt blocks.

        Blocks are replaced by DELETE + INSERT, which issues fresh autoincrement
        ids. Every checkpoint that survived a /replan was left pointing at a row
        that no longer existed, so the bot had no label to ask for and the score
        could not attribute the check to anything. Matching by time instead of
        id survives any number of rewrites.
        """
        blocks = self.blocks(day)
        moved = 0
        for cp in self.q("SELECT id, due_at, block_id FROM checkpoints WHERE date = ?",
                         (day.isoformat(),)):
            minute = int(cp["due_at"][11:13]) * 60 + int(cp["due_at"][14:16])
            b = next((x for x in blocks
                      if x["start_min"] <= minute < x["end_min"]), None)
            new_id = b["id"] if b else None
            if new_id != cp["block_id"]:
                self.conn.execute("UPDATE checkpoints SET block_id = ? WHERE id = ?",
                                  (new_id, cp["id"]))
                moved += 1
        self.conn.commit()
        return moved

    # -- the day's few things that matter ------------------------------------
    def goals(self, day: date) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM goals WHERE date = ? ORDER BY idx",
                      (day.isoformat(),))

    def set_goals(self, day: date, texts: list[str]) -> None:
        """Replace the day's goals. Rewriting them resets what was ticked off.

        That is deliberate: if the goals changed, yesterday's ticks were about
        different goals and carrying them over would flatter the record.
        """
        d = day.isoformat()
        self.run("DELETE FROM goals WHERE date = ?", (d,))
        self.conn.executemany(
            "INSERT INTO goals (date, idx, text) VALUES (?, ?, ?)",
            [(d, i, t) for i, t in enumerate(texts, start=1)],
        )
        self.conn.commit()

    def mark_goal(self, day: date, idx: int, done: bool = True) -> bool:
        cur = self.run("UPDATE goals SET done = ? WHERE date = ? AND idx = ?",
                       (1 if done else 0, day.isoformat(), idx))
        return cur.rowcount > 0

    def claim(self, key: str) -> bool:
        """True exactly once per key, ever. False on every later call."""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO flags (key, ts) VALUES (?, datetime('now'))", (key,)
        )
        self.conn.commit()
        return cur.rowcount == 1

    def lock(self) -> sqlite3.Row:
        return self.one("SELECT * FROM lock_state WHERE id = 1")

    def set_lock(self, **fields: Any) -> None:
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.run(f"UPDATE lock_state SET {cols} WHERE id = 1", tuple(fields.values()))
