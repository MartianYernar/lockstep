"""Append-only, hash-chained event log.

Why this exists
---------------
You have SSH on the box the bot runs on.  Nothing can *stop* you editing the
database -- any claim otherwise would be a lie.  What this does instead is make
edits **loud**: every event is chained to the one before it, so deleting or
rewriting history changes the tip hash.  The guardian receives the tip hash in
the daily report.  A hash that doesn't follow from yesterday's is proof the log
was rewritten, and the bot says so out loud rather than quietly carrying on.

The enforcement here is social, not cryptographic.  That is the honest design:
you cannot build a jail you hold the keys to, but you can build a room with
windows.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

GENESIS = "0" * 64


def _digest(prev_hash: str, ts: str, kind: str, payload: str) -> str:
    blob = "\x1f".join((prev_hash, ts, kind, payload)).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


class Ledger:
    def __init__(self, db):
        self.db = db

    def tip(self) -> tuple[int, str]:
        row = self.db.one("SELECT id, hash FROM events ORDER BY id DESC LIMIT 1")
        return (row["id"], row["hash"]) if row else (0, GENESIS)

    def append(self, kind: str, payload: dict[str, Any] | None = None, *, ts: str | None = None) -> str:
        _, prev = self.tip()
        ts = ts or datetime.now().astimezone().isoformat(timespec="seconds")
        body = json.dumps(payload or {}, sort_keys=True, ensure_ascii=False, default=str)
        digest = _digest(prev, ts, kind, body)
        self.db.run(
            "INSERT INTO events (ts, kind, payload, prev_hash, hash) VALUES (?, ?, ?, ?, ?)",
            (ts, kind, body, prev, digest),
        )
        return digest

    def verify(self) -> tuple[bool, int | None]:
        """Walk the chain.  Returns (ok, first_bad_event_id)."""
        prev = GENESIS
        for row in self.db.q("SELECT * FROM events ORDER BY id"):
            if row["prev_hash"] != prev:
                return False, row["id"]
            expect = _digest(row["prev_hash"], row["ts"], row["kind"], row["payload"])
            if expect != row["hash"]:
                return False, row["id"]
            prev = row["hash"]
        return True, None

    def since(self, kind: str, day_iso: str) -> list:
        return self.db.q(
            "SELECT * FROM events WHERE kind = ? AND ts >= ? ORDER BY id", (kind, day_iso)
        )
