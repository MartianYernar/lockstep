"""Checkpoint scheduling, debt accounting and escalation.

Deliberately Telegram-free: tick() returns a list of Actions and the bot layer
decides how to deliver them.  That makes the whole rule set testable without a
network.

The debt model
--------------
"Debt" is the number of seconds since the *oldest still-unanswered* checkpoint
blew past its grace period.  It resets to zero the moment you send a valid
photo -- not partially, fully.  That matters: catching up should feel like
catching up, and a system you can never dig out of is one you'll rip out.

Escalation level = debt_minutes // step_minutes, which is the canvas rule
"every 20 minutes of accumulated ignoring opens 5 more".
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Literal


# scheduled -> asked -> overdue -> satisfied | missed
#   asked     : due, still inside the grace period
#   overdue   : grace blown, still answerable, ACCRUING DEBT
#   missed    : terminal. no longer answerable, no longer accruing.
#   excused   : offset or pause covered it; never counted.
State = Literal["scheduled", "asked", "overdue", "satisfied", "missed", "excused"]
OPEN_STATES = ("asked", "overdue")


@dataclass
class Action:
    kind: str          # ask | mark_missed | plan_nag | plan_missing | daily_report | commitment_over
    payload: dict = field(default_factory=dict)


@dataclass
class AgentState:
    """What the phone polls for. The phone is the whole enforcement surface."""
    active: bool
    level: int
    debt_minutes: int
    phone_alarm: bool
    alarm_volume_pct: int
    alarm_repeats: int          # how many times to sound off per poll
    poll_sec: int
    headline: str
    detail: str

    def as_dict(self) -> dict:
        return {
            "active": self.active,
            "level": self.level,
            "debt_minutes": self.debt_minutes,
            "phone_alarm": self.phone_alarm,
            "alarm_volume_pct": self.alarm_volume_pct,
            "alarm_repeats": self.alarm_repeats,
            "poll_sec": self.poll_sec,
            "headline": self.headline,
            "detail": self.detail,
        }


class Engine:
    def __init__(self, cfg, db, ledger):
        self.cfg = cfg
        self.db = db
        self.ledger = ledger

    # -- window helpers --------------------------------------------------------
    def window(self, day: date) -> tuple[int, int]:
        """Enforcement window for a day, in minutes past midnight, offset included."""
        row = self.db.day(day)
        shift = row["offset_minutes"] if row else 0
        return self.cfg.enforce_start_min, self.cfg.enforce_end_min + shift

    def in_window(self, now: datetime) -> bool:
        start, end = self.window(now.date())
        minute = now.hour * 60 + now.minute
        return start <= minute < end

    def has_plan(self, day: date) -> bool:
        row = self.db.day(day)
        return bool(row and row["plan_locked"])

    # -- checkpoint construction ----------------------------------------------
    def _fixed_times(self, day: date) -> list[tuple[int, int | None]]:
        start, end = self.window(day)
        blocks = self.db.blocks(day)
        out = []
        for minute in range(start, end, self.cfg.interval_min):
            b = next((x for x in blocks if x["start_min"] <= minute < x["end_min"]), None)
            out.append((minute, b["id"] if b else None))
        return out

    def _spot_check_times(self, day: date) -> list[tuple[int, int | None]]:
        """Unpredictable checks, spread across the planned blocks.

        A fixed interval tells you exactly how long your next gap is, which is
        the hole in the whole idea: snap a photo, then do whatever you like for
        the next 29 minutes. Random placement removes that certainty -- you
        cannot pre-stage a photo for a moment you can't predict.

        Placement is stratified rather than uniform: the block is cut into n
        equal slices and one minute is drawn inside each. Purely uniform draws
        clump, which would leave long stretches unwatched by luck alone.

        Seeded from the block's own identity, so rebuilding a day reproduces the
        same times. That keeps build_checkpoints idempotent -- otherwise every
        rebuild would scatter fresh checkpoints on top of the old ones.
        """
        start, end = self.window(day)
        avg = max(5, self.cfg.spot_avg_min)
        picks: list[tuple[int, int | None]] = []

        for b in self.db.blocks(day):
            lo, hi = max(b["start_min"], start), min(b["end_min"], end)
            dur = hi - lo
            if dur <= 0:
                continue
            # Leave the very edges alone: a check one minute into a commute
            # proves you own a camera, not that you commuted.
            pad = min(2, max(0, dur // 5))
            lo, hi = lo + pad, hi - pad
            if hi <= lo:
                lo, hi = max(b["start_min"], start), min(b["end_min"], end)

            n = max(1, round(dur / avg))
            rng = random.Random(f"{day.isoformat()}|{b['id']}|{b['start_min']}|{b['end_min']}")
            width = (hi - lo) / n
            for i in range(n):
                slice_lo = lo + width * i
                slice_hi = lo + width * (i + 1)
                minute = int(rng.uniform(slice_lo, max(slice_lo + 1, slice_hi)))
                picks.append((min(minute, hi - 1 if hi > lo else lo), b["id"]))

        # Enforce a floor on the gap, but never at the cost of leaving a planned
        # block completely unchecked.
        picks.sort()
        kept: list[tuple[int, int | None]] = []
        covered: set = set()
        last = None
        for minute, bid in picks:
            if last is not None and minute - last < self.cfg.spot_min_gap_min:
                if bid in covered:
                    continue                       # block already has one; drop it
                minute = min(last + self.cfg.spot_min_gap_min, end - 1)
                if minute <= last:
                    continue
            kept.append((minute, bid))
            covered.add(bid)
            last = minute
        return kept

    def build_checkpoints(self, day: date, now: datetime | None = None) -> dict:
        """Materialise a day's checkpoints. Idempotent.

        Anything already in the past at build time is born `excused`, never
        `scheduled`. Submit a plan at 18:56 for a window that opened at 16:00 and
        the old code would demand photographic proof of 16:34 -- a moment that no
        longer exists -- and put you in two hours of debt for failing to produce
        it. You cannot photograph the past. Those slots are void.
        """
        now = now or self.cfg.now()
        times = (self._fixed_times(day) if self.cfg.mode == "fixed"
                 else self._spot_check_times(day))
        made = voided = 0
        for minute, block_id in times:
            due = self.cfg.at(day, minute)
            past = due <= now
            cur = self.db.conn.execute(
                "INSERT OR IGNORE INTO checkpoints (date, due_at, block_id, state, settled_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (day.isoformat(), due.isoformat(), block_id,
                 "excused" if past else "scheduled", now.isoformat() if past else None),
            )
            if cur.rowcount:
                made += 1
                voided += 1 if past else 0
        self.db.conn.commit()
        self.db.run("UPDATE days SET checkpoints_built = 1 WHERE date = ?", (day.isoformat(),))
        upcoming = self.db.one(
            "SELECT due_at FROM checkpoints WHERE date=? AND state='scheduled'"
            " ORDER BY due_at LIMIT 1", (day.isoformat(),))
        return {"made": made, "voided": voided, "live": made - voided,
                "first": upcoming["due_at"] if upcoming else None}

    def checkpoints(self, day: date) -> list:
        return self.db.q(
            "SELECT c.*, b.label AS label, b.category AS category "
            "FROM checkpoints c LEFT JOIN blocks b ON b.id = c.block_id "
            "WHERE c.date = ? ORDER BY c.due_at",
            (day.isoformat(),),
        )

    def open_checkpoints(self, day: date) -> list:
        return [c for c in self.checkpoints(day) if c["state"] in OPEN_STATES]

    def current_checkpoint(self, day: date, now: datetime):
        """The newest checkpoint that has already come due and is still unanswered."""
        opens = self.open_checkpoints(day)
        due = [c for c in opens if datetime.fromisoformat(c["due_at"]) <= now]
        return due[-1] if due else None

    def is_first_of_block(self, day: date, cp_id: int) -> bool:
        """True if this is the earliest checkpoint belonging to its block.

        The blocker screenshot proves a *session* was started, so demanding it
        once per block is the point. Asking for it at every check would just be
        the same screenshot six times.
        """
        row = self.db.one("SELECT block_id, due_at FROM checkpoints WHERE id = ?", (cp_id,))
        if not row or row["block_id"] is None:
            return False
        first = self.db.one(
            "SELECT id FROM checkpoints WHERE date = ? AND block_id = ? ORDER BY due_at LIMIT 1",
            (day.isoformat(), row["block_id"]),
        )
        return bool(first and first["id"] == cp_id)

    # -- debt ------------------------------------------------------------------
    def debt_seconds(self, day: date, now: datetime) -> int:
        if not self.in_window(now):
            return 0

        # Missing plan is itself a violation: debt runs from the window opening.
        if not self.has_plan(day):
            start, _ = self.window(day)
            return max(0, int((now - self.cfg.at(day, start)).total_seconds()))

        grace = timedelta(minutes=self.cfg.grace_min)
        deadlines = [
            datetime.fromisoformat(c["due_at"]) + grace
            for c in self.open_checkpoints(day)
        ]
        overdue = [d for d in deadlines if d <= now]
        if not overdue:
            return 0
        return int((now - min(overdue)).total_seconds())

    def agent_state(self, now: datetime) -> AgentState:
        lock = self.db.lock()
        day = now.date()

        e0 = self.cfg.esc
        idle = AgentState(False, 0, 0, False,
                          int(e0.get("phone_alarm_volume_pct", 55)), 0,
                          int(e0.get("phone_poll_sec", 60)), "", "")

        # A siren you can fire on demand. Testing the punishment otherwise means
        # waiting for the window to open and then deliberately ignoring a real
        # checkpoint, which is a slow way to discover the audio stream is wrong.
        test_until = lock["siren_test_until"]
        if test_until and now < datetime.fromisoformat(test_until):
            left = int((datetime.fromisoformat(test_until) - now).total_seconds())
            return AgentState(
                active=True, level=1, debt_minutes=0, phone_alarm=True,
                alarm_volume_pct=int(e0.get("phone_alarm_volume_pct", 55)),
                alarm_repeats=int(e0.get("phone_repeats_base", 1)),
                poll_sec=int(e0.get("phone_poll_sec", 60)),
                headline="TEST — this is what being behind sounds like",
                detail=f"Test siren, {left}s left. Nothing is actually overdue.")
        if not lock["enabled"]:
            return idle
        if lock["paused_until"] and now < datetime.fromisoformat(lock["paused_until"]):
            return idle
        if not self.in_window(now):
            return idle

        debt = self.debt_seconds(day, now)
        if debt <= 0:
            return idle

        e = self.cfg.esc
        debt_min = debt // 60
        level = debt_min // int(e["step_minutes"])
        # Escalation on a phone is repetition, not volume. The volume stays at
        # the level you chose; what grows is how many times it goes off each
        # poll, so falling further behind gets steadily harder to sit through
        # without ever becoming painful.
        repeats = min(
            int(e.get("phone_repeats_base", 1)) + int(e.get("phone_repeats_per_step", 1)) * level,
            int(e.get("phone_repeats_max", 4)),
        )

        if not self.has_plan(day):
            headline = "No plan submitted for today"
            detail = "Write today's plan in Telegram. Nothing stops until you do."
        else:
            cp = self.current_checkpoint(day, now)
            label = (cp["label"] if cp and cp["label"] else "your planned task")
            headline = f"Send a photo of: {label}"
            n = len([c for c in self.open_checkpoints(day)
                     if datetime.fromisoformat(c["due_at"]) <= now])
            detail = f"{n} checkpoint{'s' if n != 1 else ''} unanswered · {debt_min} min behind"

        alarming = debt_min >= int(e["phone_alarm_after_min"])
        return AgentState(
            active=True, level=int(level), debt_minutes=int(debt_min),
            phone_alarm=alarming,
            alarm_volume_pct=int(e.get("phone_alarm_volume_pct", 55)),
            alarm_repeats=int(repeats) if alarming else 0,
            poll_sec=int(e.get("phone_poll_sec", 60)),
            headline=headline, detail=detail,
        )

    # -- the tick --------------------------------------------------------------
    def tick(self, now: datetime) -> list[Action]:
        actions: list[Action] = []
        lock = self.db.lock()
        day = now.date()

        if lock["commitment_end"] and date.fromisoformat(lock["commitment_end"]) <= day \
                and lock["enabled"] and not lock["paused_until"] \
                and self.db.claim(f"commitment_over:{lock['commitment_end']}"):
            actions.append(Action("commitment_over", {"date": day.isoformat()}))

        if not lock["enabled"]:
            return actions
        if lock["paused_until"] and now < datetime.fromisoformat(lock["paused_until"]):
            return actions

        self.db.ensure_day(day)

        # If we were down longer than the grace period, everything that came due
        # in the meantime is void. Two reasons: he could not have answered a
        # question that was never asked, and without this a VPS reboot would
        # fire eight backlogged prompts at once and hand him hours of debt he
        # never earned. Downtime is the system's fault, not his.
        amnesty = self._outage_amnesty(day, now)
        if amnesty:
            actions.append(Action("outage_amnesty", amnesty))
        self.db.set_lock(last_tick_at=now.isoformat())

        minute = now.hour * 60 + now.minute
        start, end = self.window(day)

        # --- inside the enforcement window ---
        if start <= minute < end:
            if not self.has_plan(day):
                # Telegram gets one nudge per 10 minutes. The relentless part is
                # the laptop and the phone -- a message every 30 seconds would
                # just get the bot rate-limited and teach him to mute the chat.
                bucket = minute // 10
                if self.db.claim(f"plan_missing:{day.isoformat()}:{bucket}"):
                    actions.append(Action("plan_missing", {"date": day.isoformat()}))
                return actions

            row = self.db.day(day)
            if not row["checkpoints_built"]:
                self.build_checkpoints(day, now)

            grace = timedelta(minutes=self.cfg.grace_min)
            for cp in self.checkpoints(day):
                due = datetime.fromisoformat(cp["due_at"])
                if cp["state"] == "scheduled" and due <= now:
                    self.db.run(
                        "UPDATE checkpoints SET state='asked', asked_at=? WHERE id=?",
                        (now.isoformat(), cp["id"]),
                    )
                    actions.append(Action("ask", {
                        "checkpoint_id": cp["id"],
                        "label": cp["label"] or "your planned task",
                        "category": cp["category"] or "other",
                        "due_at": cp["due_at"],
                        "require_blocker": (
                            bool(self.cfg.vision.get("require_blocker_for_work"))
                            and (cp["category"] or "other") == "work"
                            and self.is_first_of_block(day, cp["id"])
                        ),
                    }))
                elif cp["state"] == "asked" and due + grace <= now:
                    self.db.run("UPDATE checkpoints SET state='overdue' WHERE id=?", (cp["id"],))
                    actions.append(Action("mark_missed", {
                        "checkpoint_id": cp["id"],
                        "label": cp["label"] or "your planned task",
                    }))

        # --- is the phone still alive? ---
        if start <= minute < end:
            silence = self._phone_silence(now)
            if silence is not None:
                bucket = int(silence // 15)          # renag every 15 min at most
                if self.db.claim(f"phone_silent:{day.isoformat()}:{bucket}"):
                    actions.append(Action("phone_silent", {
                        "minutes": int(silence), "escalate": silence >= 60,
                    }))

        # --- once the window shuts: close the books and report ---
        if minute >= end:
            self.db.run(
                "UPDATE checkpoints SET state='missed', settled_at=? "
                "WHERE date=? AND state IN ('scheduled','asked','overdue')",
                (now.isoformat(), day.isoformat()),
            )
            row = self.db.day(day)
            report_min = self.cfg.daily_report_at.hour * 60 + self.cfg.daily_report_at.minute
            if minute >= max(report_min, end) and row and not row["reported"]:
                self.db.run("UPDATE days SET reported = 1 WHERE date = ?", (day.isoformat(),))
                actions.append(Action("daily_report", {"date": day.isoformat()}))

        # --- nag for tomorrow's plan at the configured times ---
        # Deliberately outside the window check. The default nag times (20:00,
        # 21:30) fall *inside* the enforcement window, and nesting this under
        # "after the window" meant they could never fire at all.
        tomorrow = day + timedelta(days=1)
        if not self.has_plan(tomorrow):
            for nag in self.cfg.plan_nag_times:
                nag_min = nag.hour * 60 + nag.minute
                if nag_min <= minute < nag_min + 1 and self.db.claim(
                        f"plan_nag:{tomorrow.isoformat()}:{nag_min}"):
                    actions.append(Action("plan_nag", {"date": tomorrow.isoformat()}))

        return actions

    def _phone_silence(self, now: datetime) -> float | None:
        """Minutes since the phone last polled, or None if it's fine.

        This is the failure mode that matters most: the phone agent dying
        quietly. No siren goes off, nothing looks wrong, and you find out weeks
        later that the system stopped watching. Silence has to be loud.
        """
        grace = max(5, int(self.cfg.esc.get("phone_poll_sec", 60)) * 5 / 60)
        last = self.db.lock()["last_agent_poll"]
        if not last:
            # Never polled at all means the agent was never installed, which is
            # a setup step, not a fault. Nagging about a component that has never
            # existed just trains you to ignore the channel. /status reports it
            # instead, quietly, where you'll look for it.
            return None
        gap = (now - datetime.fromisoformat(last)).total_seconds() / 60
        return gap if gap > grace else None

    def purge_photos(self, days: int) -> int:
        """Delete stored photos older than `days`. Returns how many went."""
        import os
        import shutil
        root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "photos")
        if not os.path.isdir(root) or days <= 0:
            return 0
        cutoff = (self.cfg.today() - timedelta(days=days)).isoformat()
        gone = 0
        for name in os.listdir(root):
            path = os.path.join(root, name)
            if os.path.isdir(path) and name < cutoff:
                gone += len(os.listdir(path))
                shutil.rmtree(path, ignore_errors=True)
        if gone:
            self.ledger.append("photos_purged", {"files": gone, "older_than": cutoff})
        return gone

    def _outage_amnesty(self, day: date, now: datetime) -> dict | None:
        last_raw = self.db.lock()["last_tick_at"]
        if not last_raw:
            return None
        last = datetime.fromisoformat(last_raw)
        gap = (now - last).total_seconds()
        if gap <= self.cfg.grace_min * 60:
            return None                        # a normal 30-second tick

        floor = last - timedelta(minutes=self.cfg.grace_min)
        excused = self.db.run(
            "UPDATE checkpoints SET state='excused', settled_at=? "
            "WHERE date=? AND due_at >= ? AND due_at <= ? "
            "AND state IN ('scheduled','asked','overdue')",
            (now.isoformat(), day.isoformat(), floor.isoformat(), now.isoformat()),
        ).rowcount
        if not excused:
            return None
        self.ledger.append("outage_amnesty",
                           {"minutes": int(gap // 60), "excused": excused})
        return {"minutes": int(gap // 60), "excused": excused,
                "from": last.isoformat(), "to": now.isoformat()}

    # -- settling a checkpoint -------------------------------------------------
    def satisfy(self, day: date, now: datetime, cp_id: int, verdict_json: str,
                photo_path: str | None) -> dict:
        """Mark cp_id done and close out anything older that's still hanging.

        One valid photo cannot retroactively prove three earlier moments, so the
        older ones stay on the record as missed -- but the debt clears, because
        an unpayable debt just gets the whole system uninstalled.
        """
        self.db.run(
            "UPDATE checkpoints SET state='satisfied', settled_at=?, verdict=?, photo_path=?,"
            " attempts = attempts + 1 WHERE id = ?",
            (now.isoformat(), verdict_json, photo_path, cp_id),
        )
        target_due = self.db.one("SELECT due_at FROM checkpoints WHERE id=?", (cp_id,))["due_at"]
        swept = self.db.run(
            "UPDATE checkpoints SET state='missed', settled_at=? "
            "WHERE date=? AND due_at < ? AND state IN ('scheduled','asked','overdue')",
            (now.isoformat(), day.isoformat(), target_due),
        ).rowcount
        return {"swept": swept}

    def score(self, day: date, now: datetime | None = None) -> dict:
        now = now or self.cfg.now()
        rows = self.checkpoints(day)
        counted = [c for c in rows if c["state"] != "excused"]
        due_now = [c for c in counted if datetime.fromisoformat(c["due_at"]) <= now]
        hit = len([c for c in due_now if c["state"] == "satisfied"])
        missed = len([c for c in due_now if c["state"] == "missed"])
        return {
            "hit": hit, "missed": missed, "due": len(due_now), "total": len(counted),
            "excused": len([c for c in rows if c["state"] == "excused"]),
            "pct": round(100 * hit / len(due_now)) if due_now else 100,
        }

    # -- offset ----------------------------------------------------------------
    def apply_offset(self, day: date, now: datetime, minutes: int, reason: str) -> dict:
        """Push the day back by `minutes`: excuse what's open plus the next hour,
        slide the remaining blocks, and extend the window by the same amount."""
        horizon = now + timedelta(minutes=minutes)
        excused = self.db.run(
            "UPDATE checkpoints SET state='excused', settled_at=? "
            "WHERE date=? AND due_at < ? AND state IN ('scheduled','asked','overdue')",
            (now.isoformat(), day.isoformat(), horizon.isoformat()),
        ).rowcount

        now_min = now.hour * 60 + now.minute
        self.db.run(
            "UPDATE blocks SET start_min = start_min + ?, end_min = end_min + ? "
            "WHERE date = ? AND end_min > ?",
            (minutes, minutes, day.isoformat(), now_min),
        )
        self.db.run(
            "UPDATE days SET offsets_used = offsets_used + 1, offset_minutes = offset_minutes + ?,"
            " offset_reason = ? WHERE date = ?",
            (minutes, reason, day.isoformat()),
        )
        self.db.run(
            "DELETE FROM checkpoints WHERE date=? AND state='scheduled' AND due_at > ?",
            (day.isoformat(), now.isoformat()),
        )
        built = self.build_checkpoints(day, now)
        return {"excused": excused, "added": built["live"]}
