"""Miss the target, owe an SAT drill. His own idea, 18 Oct 2026.

    "it is 10 push ups if i miss a task... ohhh or there should be other
     punishment like... doing 10 questions of sat, one minute each, and i need
     to get do over and over until i get at least 8/10, each time new
     questions... until i dont send that 8/10 siren should keep firing"

Three things make this better than the push-ups he first suggested:

- It is verifiable without trusting him. His own drill app reports the score
  to this server directly, with a session id that can only be spent once, so
  a screenshot of an old 9/10 buys nothing.
- It is not wasted time. The SAT is on 3 October and the punishment is SAT
  practice under exam timing, so the worst case is that a bad evening turns
  into drilling he needed anyway.
- It has a natural end. Eight out of ten clears it; anything less and the next
  round is new questions. The siren runs until it is paid, inside the day's
  window only -- sleep is never the thing that pays for a missed block.

What it is NOT: a way to buy off a missed photo. The miss still stands on the
record, the block is still short in the report. This is a cost on top, not an
exchange rate.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

SCHEMA = """
CREATE TABLE IF NOT EXISTS penalties (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    date       TEXT NOT NULL,
    reason     TEXT NOT NULL,
    label      TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    paid_at    TEXT,
    attempts   INTEGER NOT NULL DEFAULT 0,
    detail     TEXT
);
CREATE INDEX IF NOT EXISTS idx_penalties_date ON penalties(date);

-- Every drill result the app has reported, so one cannot be spent twice.
CREATE TABLE IF NOT EXISTS drills (
    session    TEXT PRIMARY KEY,
    at         TEXT NOT NULL,
    score      INTEGER NOT NULL,
    total      INTEGER NOT NULL,
    seconds    INTEGER NOT NULL DEFAULT 0,
    passed     INTEGER NOT NULL DEFAULT 0,
    penalty_id INTEGER
);
"""


class Penalties:
    def __init__(self, db, cfg):
        self.db = db
        self.cfg = cfg
        db.conn.executescript(SCHEMA)
        db.conn.commit()

    # -- settings --------------------------------------------------------------
    @property
    def conf(self) -> dict:
        return self.cfg.raw.get("penalty", {}) or {}

    @property
    def enabled(self) -> bool:
        return bool(self.conf.get("enabled", False))

    def questions(self) -> int:
        return int(self.conf.get("questions", 10))

    def pass_score(self) -> int:
        return int(self.conf.get("pass_score", 8))

    def seconds_each(self) -> int:
        return int(self.conf.get("seconds_each", 60))

    def max_per_day(self) -> int:
        return int(self.conf.get("max_per_day", 3))

    def sirens(self) -> bool:
        return bool(self.conf.get("siren", True))

    def url(self) -> str:
        return str(self.conf.get("url", "https://sat-speedy-training.vercel.app"))

    # -- the physical one ------------------------------------------------------
    def pushups_on(self) -> bool:
        return bool((self.conf.get("pushups") or {}).get("enabled", False))

    def reps(self) -> int:
        return int((self.conf.get("pushups") or {}).get("reps", 20))

    def rep_seconds(self) -> tuple[float, float]:
        """How long a real set takes. Faster than the floor is a hand over the
        sensor; slower than the ceiling is the phone left on a desk."""
        p = self.conf.get("pushups") or {}
        return (float(p.get("min_seconds_per_rep", 1.1)),
                float(p.get("max_seconds_per_rep", 6.0)))

    def max_motion(self) -> float:
        """How much the phone itself may move, in m/s^2 with gravity removed.

        18 Oct, his own objection: "i could slack off by moving phone up and
        down to the floor". In a real push-up the phone lies still and HE
        moves; lifting it to the floor and back is the phone accelerating.
        A phone at rest reads about zero, so anything past a couple of m/s^2
        is a phone being waved.
        """
        return float((self.conf.get("pushups") or {}).get("max_motion", 2.0))

    def judge_reps(self, reps: int, seconds: float,
                   motion: float | None = None) -> tuple[bool, str]:
        want = self.reps()
        if reps < want:
            return False, f"{reps} of {want}"
        lo, hi = self.rep_seconds()
        if seconds and seconds < reps * lo:
            return False, (f"{reps} in {int(seconds)}s is faster than a push-up — "
                           "that reads as a hand over the sensor")
        if seconds and seconds > reps * hi:
            return False, (f"{int(seconds)}s for {reps} — too slow to be a set, "
                           "the phone was probably sitting there")
        if motion is not None and motion > self.max_motion():
            return False, (f"the phone was moving ({motion:.1f} m/s²) — it stays on "
                           "the floor and you move, not the other way round")
        return True, f"{reps} push-ups in {int(seconds)}s"

    # -- owing -----------------------------------------------------------------
    def owe(self, now: datetime, reason: str, label: str = "") -> int | None:
        """Add one. Capped per day: a bad evening should not become homework
        he can never finish, which is how a punishment stops meaning anything.
        """
        if not self.enabled:
            return None
        day = now.date()
        today = self.db.one(
            "SELECT COUNT(*) c FROM penalties WHERE date = ?", (day.isoformat(),))["c"]
        if today >= self.max_per_day():
            return None
        return self.db.run(
            "INSERT INTO penalties (date, reason, label, created_at) VALUES (?, ?, ?, ?)",
            (day.isoformat(), reason, label, now.isoformat())).lastrowid

    def pending(self) -> list[dict]:
        """Unpaid, oldest first. They carry across days: a penalty earned at
        21:00 is not cancelled by going to sleep."""
        return [dict(r) for r in self.db.q(
            "SELECT * FROM penalties WHERE paid_at IS NULL ORDER BY id")]

    def owed(self) -> int:
        return len(self.pending())

    def next_one(self) -> dict | None:
        rows = self.pending()
        return rows[0] if rows else None

    def bump(self, row_id: int) -> None:
        self.db.run("UPDATE penalties SET attempts = attempts + 1 WHERE id = ?", (row_id,))

    def pay(self, row_id: int, now: datetime, detail: dict) -> bool:
        return self.db.run(
            "UPDATE penalties SET paid_at = ?, detail = ? WHERE id = ? AND paid_at IS NULL",
            (now.isoformat(), json.dumps(detail), row_id)).rowcount > 0

    # -- drill results ---------------------------------------------------------
    def seen_session(self, session: str) -> bool:
        return self.db.one("SELECT 1 FROM drills WHERE session = ?", (session,)) is not None

    def record(self, session: str, score: int, total: int, seconds: int,
               passed: bool, now: datetime, penalty_id: int | None) -> None:
        self.db.run(
            "INSERT OR IGNORE INTO drills (session, at, score, total, seconds, passed,"
            " penalty_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (session, now.isoformat(), int(score), int(total), int(seconds),
             1 if passed else 0, penalty_id))

    def judge(self, score: int, total: int, seconds: int) -> tuple[bool, str]:
        """Does this result clear one? Strict about the shape of the drill, so
        ten easy questions at five minutes each cannot pass for the real thing.
        """
        if total < self.questions():
            return False, (f"that was {total} questions — it has to be at least "
                           f"{self.questions()}")
        if seconds and seconds > self.seconds_each() + 5:
            return False, (f"{seconds}s per question — the drill is "
                           f"{self.seconds_each()}s")
        need = self.pass_score()
        if total > self.questions():
            need = round(need * total / self.questions())
        if score < need:
            return False, f"{score}/{total} — you need {need}"
        return True, f"{score}/{total}"

    def recent(self, now: datetime, hours: int = 24) -> list[dict]:
        return [dict(r) for r in self.db.q(
            "SELECT * FROM drills WHERE at >= ? ORDER BY at DESC",
            ((now - timedelta(hours=hours)).isoformat(),))]

    def describe(self, day: date) -> str:
        rows = [dict(r) for r in self.db.q(
            "SELECT * FROM penalties WHERE date = ? ORDER BY id", (day.isoformat(),))]
        if not rows:
            return ""
        out = []
        for r in rows:
            got = ""
            if r["detail"]:
                try:
                    got = f" ({json.loads(r['detail']).get('result', '')})"
                except ValueError:
                    got = ""
            out.append(f"  {'✅' if r['paid_at'] else '⏳'} {r['reason']}"
                       + (f" — {r['label'][:40]}" if r["label"] else "") + got)
        return "\n".join(out)
