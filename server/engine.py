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

# What each checkpoint wants to see. The design note behind this: being asked
# for a photo every few minutes is its own stressor, and a system that raises
# your cortisol to make you work is trading one problem for another. So proof
# happens once, at the START of a block, and then it leaves you alone -- the
# blocker screenshots are what carry the rest of the session, because a running
# block with an allowlist is evidence about the whole hour rather than a moment.
def _minutes_of(hhmm: str) -> int:
    hh, mm = str(hhmm).split(":")
    return int(hh) * 60 + int(mm)


PROOF_KINDS = {
    "blocker_toggle": "the blocker running, with the clock visible",
    "blocker_allow":  "what the block covers — the allowed or blocked list",
    # No longer generated; kept so checkpoints recorded before the cut resolve.
    "blocker_block":  "the BLOCKED sites list",
    "activity":       "the activity itself",
    "bed":            "your bed, opened and ready to sleep in",
    "outside":        "where you are — the street, the yard, the stairwell",
}


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
    apps_blocked: bool
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
            "apps_blocked": self.apps_blocked,
            "headline": self.headline,
            "detail": self.detail,
        }


class Engine:
    def __init__(self, cfg, db, ledger):
        self.cfg = cfg
        self.db = db
        self.ledger = ledger

    def _penalties(self):
        try:
            from .penalty import Penalties
            p = Penalties(self.db, self.cfg)
            return p if p.enabled else None
        except Exception:                                     # noqa: BLE001
            return None

    def penalty_owed(self) -> int:
        p = self._penalties()
        return p.owed() if p is not None and p.sirens() else 0

    def _deliverables(self):
        try:
            from .deliverables import Deliverables
            d = Deliverables(self.db, self.cfg)
            return d if d.enabled else None
        except Exception:                                     # noqa: BLE001
            return None

    def _wants_deliverable(self, block) -> bool:
        d = self._deliverables()
        return d is not None and d.wants(block)

    def _steps(self):
        try:
            from .microsteps import Steps
            s = Steps(self.db, self.cfg)
            return s if s.enabled else None
        except Exception:                                     # noqa: BLE001
            return None

    def _defer_for_steps(self, cp, due: datetime, now: datetime) -> bool:
        """Let a photo slip while the ten-minute loop is being answered.

        He writes the next small step and says when it is done, every ten
        minutes. While that is live the photo cadence relaxes -- 20 minutes
        becomes 30 -- because there is a running account of the work in his
        own words, and the photo that does come is checked against it.

        Once per checkpoint, never for the first proof of a block, and never
        for bed. Stop answering and nothing is deferred: the looser cadence is
        bought by staying in the loop, not granted.
        """
        steps = self._steps()
        if steps is None or cp["kind"] == "bed" or cp["deferred"]:
            return False
        slip = int(steps.conf.get("defer_photo_min", 10))
        if slip <= 0 or not steps.alive(now):
            return False
        if self.is_first_of_block(due.date(), cp["id"]):
            return False
        self.db.run("UPDATE checkpoints SET due_at = ?, deferred = 1 WHERE id = ?",
                    ((due + timedelta(minutes=slip)).isoformat(), cp["id"]))
        return True

    def _routines(self):
        try:
            from .routines import Routines
            return Routines(self.db)
        except Exception:                                     # noqa: BLE001
            return None

    def quiet_now(self, now: datetime):
        """The quiet window covering this minute, if any."""
        r = self._routines()
        return r.quiet_now(now) if r is not None else None

    def quiet_soon(self, now: datetime, lead_min: int = 45) -> bool:
        """Inside a quiet window, or close enough that he is already leaving."""
        if self.quiet_now(now) is not None:
            return True
        minute = now.hour * 60 + now.minute
        return any(qs - lead_min <= minute < qe for qs, qe in self.quiet_spans(now.date()))

    def quiet_spans(self, day: date) -> list[tuple[int, int]]:
        r = self._routines()
        return r.quiet_spans(day) if r is not None else []

    def _routine_photo_free(self, day: date, b) -> bool:
        try:
            rows = self.db.q("SELECT * FROM routines WHERE active=1 AND photo=0")
        except Exception:                                     # noqa: BLE001
            return False
        wd = str(day.weekday())
        iso = day.isoformat()
        return any((r["on_date"] == iso if r["on_date"]
                    else wd in (r["days"] or "").split(","))
                   and r["label"] == b["label"]
                   and r["start_min"] < b["end_min"] and b["start_min"] < r["end_min"]
                   for r in rows)

    def retire_blocker_checks(self) -> int:
        """With screenshots switched off, an already-scheduled one is a demand
        nothing can meet. Turn every open one into an ordinary photo check.

        10 Oct 2026: after a day of sending photos of his screen and being told
        "no Cold Turkey window in the shot", he switched the screenshot proof
        off for good. The config change only reaches checkpoints built after
        it; the ones already on today's plan would have gone on asking. This
        runs at startup, and only touches checks still waiting for an answer --
        nothing settled is rewritten.
        """
        if bool(self.cfg.vision.get("require_blocker_for_work", False)):
            return 0
        n = self.db.run(
            "UPDATE checkpoints SET kind='activity' WHERE kind LIKE 'blocker%'"
            " AND state IN ('scheduled','asked','overdue')").rowcount
        if n:
            self.ledger.append("blocker_checks_retired", {"checks": n})
        return n

    # -- window helpers --------------------------------------------------------
    def window(self, day: date) -> tuple[int, int]:
        """Enforcement window, in minutes past midnight, including any shifts."""
        row = self.db.day(day)
        # Only an offset (something outside the plan happened) moves the end of
        # the day. Extending a task does not: the day is finite, and letting an
        # overrun push bedtime later is how a productivity system quietly turns
        # into a sleep-debt machine. Extra time comes out of something else.
        shift = (row["offset_minutes"] or 0) if row else 0
        # Weekends can have their own hours: the weekday window starts when
        # school ends, which left every Saturday and Sunday morning outside the
        # system entirely.
        start = self.cfg.enforce_start_for(day)
        end = self.cfg.enforce_end_for(day) + shift

        # The plan may reach outside the usual hours -- an hour of work before
        # school, say. Those blocks carry proof like any other, so the window
        # stretches to whatever was actually planned. Previously anything
        # written outside 16:10-22:00 was clamped into the window, landed after
        # its own end time, and produced no checkpoints at all: the block was
        # accepted, displayed, and then silently unenforced.
        blocks = self.db.blocks(day)
        if blocks:
            start = min(start, min(b["start_min"] for b in blocks))
            end = max(end, max(b["end_min"] for b in blocks))

        # Two limits a plan does not get to argue with. Sleep is the one thing
        # nothing is allowed to borrow from, at either end of the day -- not an
        # overrun, not an emergency offset, and not an ambitious 4am block.
        start = max(start, self.cfg.earliest_min)
        end = min(end, self.cfg.hard_end_min + self.bed_offset(day))
        return start, max(start, end)

    # -- the end of the day ----------------------------------------------------
    #
    # The last block of the evening was the one nobody ever checked: the window
    # shut at 22:00 and whatever happened after that was invisible. So the day
    # ends with proof of the thing it is actually for -- being asleep.
    def _sleep_conf(self) -> dict:
        return self.cfg.raw.get("sleep", {}) or {}

    # -- being away from the wifi, legitimately ------------------------------
    #
    # The phone has no mobile data, so it goes dark every time he leaves the
    # house. The flow treats an unreachable server as a siren, which is right
    # when he has killed the wifi to dodge a checkpoint and wrong when he has
    # simply walked to the shop. Nothing on the phone can tell those apart --
    # both look like no connection -- so the distinction is made here, before
    # he goes, with a photo of where he is.
    #
    # It buys silence, not forgiveness: checkpoints inside the excused stretch
    # still come due and still go missed. Otherwise this is just a /skip with
    # extra steps.
    def outside_conf(self) -> dict:
        return self.cfg.raw.get("outside", {}) or {}

    def outside_until(self, day: date) -> datetime | None:
        row = self.db.day(day)
        raw = row["outside_until"] if row is not None else None
        return datetime.fromisoformat(raw) if raw else None

    def outside_used(self, day: date) -> int:
        row = self.db.day(day)
        return int(row["outside_used"] or 0) if row is not None else 0

    def outside_active(self, now: datetime) -> bool:
        """True while a granted trip is still running.

        Checks yesterday as well: a trip granted at 23:50 that runs an hour
        does not stop being true at midnight.
        """
        for day in (now.date(), now.date() - timedelta(days=1)):
            until = self.outside_until(day)
            if until is not None and now < until:
                return True
        return False

    def grant_outside(self, now: datetime, minutes: int) -> datetime:
        """Start the clock. Returns when it runs out."""
        day = now.date()
        self.db.ensure_day(day)
        until = now + timedelta(minutes=minutes)
        self.db.run(
            "UPDATE days SET outside_until = ?, outside_used = outside_used + 1"
            " WHERE date = ?", (until.isoformat(), day.isoformat()))
        return until

    def bed_offset(self, day: date) -> int:
        row = self.db.day(day)
        return int(row["bed_offset_minutes"] or 0) if row else 0

    def plan_end(self, day: date) -> int:
        """The last minute a plan for that day may reach.

        Normally the window's end. After a /latenight is granted it is that
        plus the hours it bought -- 20 Oct he earned two hours and the planner
        still stopped at 22:00, so the extension bought him a later bed photo
        and nothing to do before it.
        """
        return self.cfg.enforce_end_for(day) + self.bed_offset(day)

    def bed_due(self, day: date) -> datetime:
        """When the bed photo is asked for, including any /latenight offset."""
        base = _minutes_of(self._sleep_conf().get("bed_photo_at", "22:20"))
        return self.cfg.at(day, base + self.bed_offset(day))

    def bed_cutoff(self, day: date) -> datetime:
        """When the siren gives up and the night is written off.

        It deliberately stops. A siren nobody can silence while asleep is a
        sleep-destroyer, not an accountability system, and this project's whole
        argument is that borrowing from sleep defeats the point.
        """
        raw = _minutes_of(self._sleep_conf().get("siren_until", "01:00"))
        base = _minutes_of(self._sleep_conf().get("bed_photo_at", "22:20"))
        # siren_until is normally past midnight, so it belongs to the next day.
        at = self.cfg.at(day, raw + self.bed_offset(day))
        if raw <= base:
            at += timedelta(days=1)
        return at

    def bed_checkpoint(self, day: date):
        return self.db.one(
            "SELECT * FROM checkpoints WHERE date = ? AND kind = 'bed'",
            (day.isoformat(),))

    def plan_overdue_for(self, now: datetime) -> date | None:
        """The day whose plan is overdue right now, or None.

        Nagging by Telegram at the configured times was the only pressure to
        write tomorrow's plan, and the record says it wasn't enough: 9 of 19
        days had a plan at all, and the days without one produced nothing,
        because with no plan there are no checkpoints to miss. The plan is the
        thing everything else hangs off, so it gets the siren too.

        It runs from the deadline until BEDTIME, not until the bed cutoff. Past
        bedtime the bed check takes over and sleep wins: a missing plan must not
        be a reason to keep him up, which is the one rule this project has never
        broken. The plan is still enforced tomorrow anyway -- a day with no plan
        sirens from the moment its window opens -- so the night-before siren is
        pressure, not the only chance.

        A plan_due configured later than bedtime therefore produces no window at
        all, which is the honest outcome of contradictory settings.
        """
        due = self.cfg.plan_due.hour * 60 + self.cfg.plan_due.minute
        # After midnight the calendar has rolled but the evening has not: at
        # 00:30 the plan still owed is for the day that just started.
        for d in (now.date(), now.date() - timedelta(days=1)):
            if self.cfg.at(d, due) <= now < self.bed_due(d):
                target = d + timedelta(days=1)
                return None if self.has_plan(target) else target
        return None

    def phone_needs_charger(self, now: datetime) -> int | None:
        """Battery percentage if the phone is low and unplugged, else None.

        The agent dies with the phone, and this one has been at 2% during an
        enforcement window more than once -- which looks exactly like a working
        system on a day nothing was owed. Only acts on a reading the phone sent
        in the last ten minutes; a stale one tells you nothing about now, and
        phone silence is already its own alarm.
        """
        pct_limit = int(self.cfg.esc.get("charge_below_pct", 0))
        if pct_limit <= 0:
            return None
        row = self.db.lock()
        at, batt = row["phone_batt_at"], row["phone_battery"]
        if not at or batt is None:
            return None
        if (now - datetime.fromisoformat(at)).total_seconds() > 600:
            return None
        if row["phone_charging"]:
            return None
        return int(batt) if int(batt) < pct_limit else None

    def bed_night(self, now: datetime) -> date | None:
        """Which day's bedtime we are inside, if any.

        After midnight the calendar has rolled over but the night has not, so
        01:00 still belongs to yesterday's bed check.
        """
        for day in (now.date(), now.date() - timedelta(days=1)):
            if self.bed_due(day) <= now < self.bed_cutoff(day):
                return day
        return None

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

    def _block_start_times(self, day: date) -> list[tuple[int, int | None, str]]:
        """Proof at the top of each block: three blocker shots a minute apart,
        plus a photo of the activity itself for anything that isn't `work:`.

        A `work:` block is done at a screen, so a running blocker with the right
        allowlist already proves what is and isn't reachable. A sport or sleep
        block isn't -- no screenshot shows you doing push-ups -- so those need
        the extra photo of the thing itself.
        """
        start, end = self.window(day)
        gap = int(self.cfg.raw.get("proof", {}).get("shot_gap_min", 1))
        out: list[tuple[int, int | None, str]] = []
        meal_free = int((self.cfg.raw.get("friend", {}) or {}).get("meal_grace_min", 45))
        quiet = self.quiet_spans(day)
        for b in self.db.blocks(day):
            # A photo of dinner proves nothing, and asking for one sounded the
            # siren in the middle of the meal (10 Oct 20:07: "if I send u image
            # of food, can u stop it"). A short meal is proof-free. A long one
            # is a question, and the rewrite guard already asks it whenever a
            # meal grows out of work time.
            if (b["category"] == "meal"
                    and b["end_min"] - b["start_min"] <= meal_free):
                continue
            # His fixed weekly commitments -- teaching, the commute home -- are
            # not proved by photo: he didn't choose them over work, and a photo
            # of a classroom is a photo of other people's faces.
            if self._routine_photo_free(day, b):
                continue
            lo = max(b["start_min"], start)
            if lo >= end:
                continue
            # vision.require_blocker_for_work was documented in README and
            # SETUP as the switch for all of this and was referenced nowhere in
            # the code -- there was no way to turn it off, only a config key
            # that looked like one. Off means one activity photo per block, work
            # or not: a block still has to be proved, just not by screenshot.
            #
            # When it is on: two shots, not three. For an allow-only block the
            # allowed and blocked lists are the same window, so a third shot
            # photographed one screen twice a minute apart. blocker_block stays
            # defined so older checkpoints resolve; it is no longer generated.
            if bool(self.cfg.vision.get("require_blocker_for_work", False)):
                # One screenshot: the block toggled on and running. The scope
                # shot is gone. It cost an hour of a school night to three
                # rounds of wrong guesses about Cold Turkey's UI, and a proof
                # you cannot reliably produce is worth less than no proof at
                # all -- it just teaches you to ignore the bot.
                #
                # compare_policy in vision.py is kept and tested, unwired. When
                # a scope shot can be produced without a fight, generate
                # "blocker_allow" here again and the allowlist check comes back
                # with it.
                # Exactly one photo per block, and which one depends only on
                # whether the block is `work:`. A work block happens at a
                # screen, so the blocker running is the evidence; anything else
                # happens in the room, so a photo of it is. Asking a non-work
                # block for both meant a gym block wanted a screenshot of a
                # website blocker, which proves nothing about a gym block.
                # The colon matters. This tested startswith("work"), so a block
                # called "workout outside" counted as screen work and was asked
                # for a Cold Turkey screenshot -- of a gym session. "workshop"
                # and "working out" fell down the same hole. The tag is "work:"
                # and nothing else is it.
                kinds = (["blocker_toggle"]
                         if b["label"].lower().lstrip().startswith("work:")
                         else ["activity"])
            else:
                kinds = ["activity"]
            # He names the finish line before the work, and shows it after.
            # Both are enforced like a photo: a block whose target was never
            # written is a block nobody can judge afterwards, which is how
            # ninety minutes of sitting in front of the right document used to
            # count as a perfect block.
            if self._wants_deliverable(b):
                d = self._deliverables()
                for seg in d.segments(b, start, end):
                    s0, s1 = seg["start_min"], seg["end_min"] - 1
                    if not any(qs <= s0 < qe for qs, qe in quiet):
                        out.append((s0, b["id"], "declare"))
                    if s1 > s0 and not any(qs <= s1 < qe for qs, qe in quiet):
                        out.append((s1, b["id"], "deliver"))

            # The target question owns the block's first minute, so the photo
            # that follows it starts one gap later -- two checkpoints cannot
            # share a minute, and the one that lost was the photo.
            base = lo + (gap if self._wants_deliverable(b) else 0)
            for i, kind in enumerate(kinds):
                minute = base + i * gap
                if minute < end and minute < b["end_min"] \
                        and not any(qs <= minute < qe for qs, qe in quiet):
                    out.append((minute, b["id"], kind))

            # On top of the proof at the top of the block, a photo every N
            # minutes while the block runs. A block-start shot proves a session
            # was started; it says nothing about the ninety minutes after it.
            #
            # Set proof.within_block_min: 0 (or leave it out) to go back to
            # block starts only. Worth knowing what this costs: the README
            # argues against constant interruption, and an earlier version of
            # this project asked for a photo every three minutes -- 180 in one
            # day, of which 9 were answered. The honest test is whether you
            # still answer them in a week.
            every = int(self.cfg.raw.get("proof", {}).get("within_block_min", 0))
            segmented = (self._wants_deliverable(b)
                         and self._deliverables().every() > 0)
            if every > 0 and not segmented:
                minute = lo + every
                last = min(b["end_min"], end) - 1
                while minute < min(b["end_min"], end):
                    # The end-of-block proof already covers the last minutes.
                    if self._wants_deliverable(b) and minute >= last - every // 2:
                        break
                    if not any(qs <= minute < qe for qs, qe in quiet):
                        out.append((minute, b["id"], "activity"))
                    minute += every
        return out

    def build_checkpoints(self, day: date, now: datetime | None = None) -> dict:
        """Materialise a day's checkpoints. Idempotent.

        A checkpoint whose block has already ENDED is born `excused`, never
        `scheduled`. Submit a plan at 18:56 for a window that opened at 16:00 and
        the old code would demand photographic proof of 16:34 -- a moment that no
        longer exists -- and put you in two hours of debt for failing to produce
        it. You cannot photograph the past. Those slots are void.

        A slot that has only *just* passed is a different thing, and used to be
        voided with the rest. Replanning at 17:06 into a block starting 17:06
        produced a checkpoint born void, and fourteen minutes of the one block
        he had just decided to do went entirely unchecked. A moment seconds old
        is still answerable, so the cutoff is the grace period rather than the
        instant itself -- the same tolerance the rest of the system already
        allows for answering a checkpoint at all.
        """
        now = now or self.cfg.now()
        answerable_since = now - timedelta(minutes=self.cfg.grace_min)
        if self.cfg.mode == "block_start":
            times = self._block_start_times(day)
        elif self.cfg.mode == "fixed":
            times = [(m, b, "activity") for m, b in self._fixed_times(day)]
        else:
            times = [(m, b, "activity") for m, b in self._spot_check_times(day)]
        made = voided = 0
        for minute, block_id, kind in times:
            due = self.cfg.at(day, minute)
            past = due <= answerable_since
            cur = self.db.conn.execute(
                "INSERT OR IGNORE INTO checkpoints (date, due_at, block_id, kind, state, settled_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (day.isoformat(), due.isoformat(), block_id, kind,
                 "excused" if past else "scheduled", now.isoformat() if past else None),
            )
            if cur.rowcount:
                made += 1
                voided += 1 if past else 0
        # The bed photo sits outside the block loop on purpose: it belongs to
        # the day rather than to any block, and it is due after the window has
        # already shut.
        bed_at = self.bed_due(day)
        cur = self.db.conn.execute(
            "INSERT OR IGNORE INTO checkpoints (date, due_at, block_id, kind, state, settled_at)"
            " VALUES (?, ?, NULL, 'bed', ?, ?)",
            (day.isoformat(), bed_at.isoformat(),
             "excused" if bed_at <= now else "scheduled",
             now.isoformat() if bed_at <= now else None),
        )
        if cur.rowcount:
            made += 1
            voided += 1 if bed_at <= now else 0
        self.db.conn.commit()
        self.db.run("UPDATE days SET checkpoints_built = 1 WHERE date = ?", (day.isoformat(),))
        upcoming = self.db.one(
            "SELECT due_at FROM checkpoints WHERE date=? AND state='scheduled'"
            " ORDER BY due_at LIMIT 1", (day.isoformat(),))
        return {"made": made, "voided": voided, "live": made - voided,
                "first": upcoming["due_at"] if upcoming else None}

    def checkpoints(self, day: date) -> list:
        return self.db.q(
            "SELECT c.*, b.label AS label, b.category AS category, "
            "b.start_min AS start_min, b.end_min AS end_min "
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
        if not self.in_window(now) or self.quiet_now(now) is not None:
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
                          int(e0.get("phone_poll_sec", 60)),
                          self.apps_blocked(now), "", "")

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
                apps_blocked=self.apps_blocked(now),
                headline="TEST — this is what being behind sounds like",
                detail=f"Test siren, {left}s left. Nothing is actually overdue.")
        if not lock["enabled"]:
            return idle
        if lock["paused_until"] and now < datetime.fromisoformat(lock["paused_until"]):
            return idle
        # Tomorrow's plan comes before bed, because its deadline is earlier and
        # because going to sleep without one means tomorrow is unenforced.
        owed = self.plan_overdue_for(now)
        if owed is not None:
            due_at = self.cfg.at(owed - timedelta(days=1),
                                 self.cfg.plan_due.hour * 60 + self.cfg.plan_due.minute)
            late = int((now - due_at).total_seconds() // 60)
            return AgentState(
                active=True, level=1, debt_minutes=late, phone_alarm=True,
                alarm_volume_pct=int(e0.get("phone_alarm_volume_pct", 55)),
                alarm_repeats=int(e0.get("phone_repeats_base", 1)),
                poll_sec=int(e0.get("phone_poll_sec", 60)),
                apps_blocked=True,
                headline=f"Send the plan for {owed:%A}. /goals first.",
                detail=f"{late} min past the deadline. Nothing stops until it lands.")

        # The bed check runs after the window has shut, so it gets its own
        # branch. Between bed time and the cutoff the siren is on until the
        # photo lands -- otherwise "stay up as long as you like" is the one
        # thing the whole day never checked.
        night = self.bed_night(now)
        if night is not None:
            cp = self.bed_checkpoint(night)
            if cp is not None and cp["state"] != "satisfied":
                late = int((now - self.bed_due(night)).total_seconds() // 60)
                return AgentState(
                    active=True, level=1, debt_minutes=late, phone_alarm=True,
                    alarm_volume_pct=int(e0.get("phone_alarm_volume_pct", 55)),
                    alarm_repeats=int(e0.get("phone_repeats_base", 1)),
                    poll_sec=int(e0.get("phone_poll_sec", 60)),
                    apps_blocked=True,
                    headline="Go to bed. Send a photo of it, opened.",
                    detail=f"{late} min past bedtime. This stops when the photo lands.")

        if not self.in_window(now):
            return idle
        # An exam hall is not a place to be sirened at. Nothing is owed and
        # nothing sounds while a quiet window is running.
        if self.quiet_now(now) is not None:
            return idle

        owed = self.penalty_owed()
        debt = self.debt_seconds(day, now)
        if debt <= 0 and owed:
            # His own rule, 18 Oct: miss the target and the siren runs until
            # the drill is passed. Inside the window only -- sleep never pays
            # for a missed block, and an unpaid one simply carries to tomorrow.
            p = self._penalties()
            e2 = self.cfg.esc
            return AgentState(
                active=True, level=1, debt_minutes=0, phone_alarm=True,
                alarm_volume_pct=int(e2.get("phone_alarm_volume_pct", 55)),
                alarm_repeats=int(e2.get("phone_repeats_base", 1)),
                poll_sec=int(e2.get("phone_poll_sec", 60)),
                apps_blocked=self.apps_blocked(now),
                headline=f"{owed} SAT drill{'s' if owed != 1 else ''} owed — "
                         f"{p.pass_score()}/{p.questions()} to clear",
                detail="You missed what you said you'd finish. One drill, "
                       f"{p.questions()} questions at {p.seconds_each()}s. "
                       "The siren stops when the app reports the score.")
        if debt <= 0:
            # Nothing owed, but a phone about to die is a siren about to stop.
            # Checked last, so it never overrides a real checkpoint -- being
            # behind matters more than being unplugged.
            low = self.phone_needs_charger(now)
            if low is not None:
                return AgentState(
                    active=True, level=0, debt_minutes=0, phone_alarm=True,
                    alarm_volume_pct=int(e0.get("phone_alarm_volume_pct", 55)),
                    alarm_repeats=int(e0.get("phone_repeats_base", 1)),
                    poll_sec=int(e0.get("phone_poll_sec", 60)),
                    apps_blocked=self.apps_blocked(now),
                    headline=f"Put the phone on the charger — {low}%.",
                    detail="A dead phone is a dead siren. This stops when it's plugged in.")
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

        # Mute silences the phone only. Checkpoints still come, debt still
        # accrues, misses still count. It is a volume control, not a pause --
        # otherwise it would just be a way out with extra steps.
        muted = lock["muted_until"] and now < datetime.fromisoformat(lock["muted_until"])
        alarming = debt_min >= int(e["phone_alarm_after_min"]) and not muted
        return AgentState(
            active=True, level=int(level), debt_minutes=int(debt_min),
            phone_alarm=alarming,
            alarm_volume_pct=int(e.get("phone_alarm_volume_pct", 55)),
            alarm_repeats=int(repeats) if alarming else 0,
            poll_sec=int(e.get("phone_poll_sec", 60)),
            apps_blocked=self.apps_blocked(now),
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
                if cp["state"] == "scheduled" and due <= now \
                        and self._defer_for_steps(cp, due, now):
                    continue
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
                        "kind": cp["kind"],
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

        # --- the bed check, which lives past the window ---
        night = self.bed_night(now)
        if night is not None:
            cp = self.bed_checkpoint(night)
            if cp is None:
                self.build_checkpoints(night, now)
                cp = self.bed_checkpoint(night)
            if cp is not None and cp["state"] in ("scheduled", "asked", "overdue"):
                if cp["state"] == "scheduled":
                    self.db.run(
                        "UPDATE checkpoints SET state='asked', asked_at=? WHERE id=?",
                        (now.isoformat(), cp["id"]))
                    actions.append(Action("ask", {"checkpoint_id": cp["id"], "kind": "bed",
                                                  "label": "bed", "due_at": cp["due_at"]}))
                elif self.db.claim(f"bed_nag:{night.isoformat()}:{minute // 5}"):
                    actions.append(Action("bed_overdue", {
                        "minutes": int((now - self.bed_due(night)).total_seconds() // 60)}))
        else:
            # Past the cutoff with no photo: the night is written off. The siren
            # stops either way -- one that cannot be silenced while asleep
            # destroys the sleep this whole project exists to protect.
            for d in (day, day - timedelta(days=1)):
                bed = self.bed_checkpoint(d)
                if bed and bed["state"] in ("scheduled", "asked", "overdue") \
                        and now >= self.bed_cutoff(d):
                    self.db.run("UPDATE checkpoints SET state='missed', settled_at=? WHERE id=?",
                                (now.isoformat(), bed["id"]))
                    if self.db.claim(f"bed_missed:{d.isoformat()}"):
                        actions.append(Action("bed_missed", {"date": d.isoformat()}))

        # --- once the window shuts: close the books and report ---
        if minute >= end:
            self.db.run(
                "UPDATE checkpoints SET state='missed', settled_at=? "
                "WHERE date=? AND state IN ('scheduled','asked','overdue') AND kind != 'bed'",
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

    def apps_blocked(self, now: datetime) -> bool:
        """Should the distraction blocker be ON right now?

        Default: yes, always. The guardian can open a short window with a
        command; it closes itself when the window expires. There is no password
        anywhere in this path, so there is nothing to forget or re-set.
        """
        until = self.db.lock()["apps_open_until"]
        if until and now < datetime.fromisoformat(until):
            return False
        return True

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

    def current_block(self, day: date, now: datetime):
        minute = now.hour * 60 + now.minute
        for b in self.db.blocks(day):
            if b["start_min"] <= minute < b["end_min"]:
                return b
        return None

    def block_was_proven(self, day: date, block_id: int) -> bool:
        """Has at least one photo already been accepted for this block?"""
        row = self.db.one(
            "SELECT 1 FROM checkpoints WHERE date=? AND block_id=? AND state='satisfied' LIMIT 1",
            (day.isoformat(), block_id))
        return row is not None

    def extend_block(self, day: date, now: datetime, block_id: int, minutes: int) -> dict:
        """Give the current task more time, taken from what comes after it.

        The day does not get longer. Everything following shrinks to pay for it,
        starting with the next block, and anything squeezed to nothing is
        dropped. That is the honest arithmetic: an hour spent here is an hour
        not spent there, and the system should make you feel that rather than
        hide it by moving bedtime.
        """
        block = self.db.one("SELECT * FROM blocks WHERE id = ?", (block_id,))
        _, window_end = self.window(day)
        later = [b for b in self.db.blocks(day) if b["start_min"] >= block["end_min"]]

        available = sum(min(b["end_min"], window_end) - b["start_min"] for b in later)
        if available <= 0:
            return {"error": "This is the last block of the day — nothing left to take from."}
        minutes = min(minutes, available - 1)      # never consume the day entirely
        if minutes <= 0:
            return {"error": "There isn't enough time left in the day for that."}

        self.db.run("UPDATE blocks SET end_min = end_min + ? WHERE id = ?",
                    (minutes, block_id))

        # Walk forward taking time until the debt is paid.
        owed, cursor, shortened, dropped = minutes, block["end_min"] + minutes, [], []
        for b in later:
            length = b["end_min"] - b["start_min"]
            if owed >= length:
                self.db.run("DELETE FROM blocks WHERE id = ?", (b["id"],))
                dropped.append(b["label"])
                owed -= length
                continue
            self.db.run("UPDATE blocks SET start_min = ?, end_min = ? WHERE id = ?",
                        (cursor, cursor + (length - owed), b["id"]))
            if owed:
                shortened.append((b["label"], owed))
            cursor += length - owed
            owed = 0

        self.db.run("UPDATE days SET extend_minutes = extend_minutes + ? WHERE date = ?",
                    (minutes, day.isoformat()))
        self.db.run(
            "DELETE FROM checkpoints WHERE date=? AND state='scheduled' AND due_at > ?",
            (day.isoformat(), now.isoformat()))
        self.build_checkpoints(day, now)
        self.ledger.append("block_extended",
                           {"date": day.isoformat(), "block": block["label"],
                            "minutes": minutes, "dropped": dropped,
                            "shortened": [f"{l} -{m}m" for l, m in shortened]})
        return {"label": block["label"], "minutes": minutes,
                "new_end": block["end_min"] + minutes,
                "dropped": dropped, "shortened": shortened}

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
