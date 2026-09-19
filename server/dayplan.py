"""Build the day from how he actually talks, and from what is actually due.

19 Sep, 13:52, he wrote:

    8:00-13:50 olympiad lost
    From 13:50 till 14:20 lunch and Foundationals like mouth hygiene, socks,
    cloth. From 14:20-14:30 workout outside 14:30-14:40 shower
    From 14:40-15:40 do math sat questions without chair at table, on knees.
    Rest plan do yourself based on my tasks and reminders

and got "I couldn't read these lines". Every word of that is a plan. Three
blocks share one line, the times are written four different ways, and the
last sentence hands the rest of the day over -- which is the part worth
having, because the rest of the day is exactly where the SAT and the IA
deadlines should be landing.

So the writing stays his, and the arithmetic comes from the agenda: what is
due, how many hours are left in it, how many hours a day that means. The
output is the same strict block list the rest of the system already trusts,
and it goes through the same coverage check -- a plan that does not tile the
window is rejected here exactly as it would be if he had typed it.
"""
from __future__ import annotations

import json
import logging

from datetime import date, datetime

from . import planner

log = logging.getLogger("dayplan")

SYSTEM = """You build one day's timetable for {name}, and you are good at it.

Output JSON only: {{"blocks": [{{"start": "HH:MM", "end": "HH:MM", "label": "..."}}],
"note": "one short line to him about the choices you made"}}

HARD RULES -- a plan that breaks one is thrown away:
- Cover {start}-{end} exactly: no gaps, no overlaps, first block starts at
  {start}, last ends at {end}. Blocks outside that range are allowed before
  {start} if he asked for them, never after {end}.
- Use his words for anything he specified. Keep his labels.
- Put "work: " in front of blocks that are study or project work, and nothing
  else. Never on meals, commute, sport, rest, hygiene.
- 45 to 120 minutes per work block. Shorter than 45 is churn, longer than 120
  he will not sustain. Non-work blocks can be any length.
- Give every big task he names its own block. Never merge two tasks into one
  block ("math and physics" is not a block).

HOW TO CHOOSE WHAT GOES IN THE FREE TIME:
- The agenda below is ordered by urgency. Anything marked WILL MISS or OVERDUE
  gets time today, before anything marked on track.
- Respect "needs Xh/day" -- that is the number that makes the deadline.
- Two or three different tasks in an evening, not five. Switching costs him.
- Hardest thing first, while he is fresh, unless he says otherwise.
- Leave what he already fixed exactly where he put it."""

BUILD = """TODAY IS {today}. This plan is for {day} ({weekday}).
The enforced window is {start}-{end}. He must be doing something the whole time.

WHAT HE SAID:
{text}

HIS BIG TASKS, most urgent first:
{agenda}

{events}{steps}{history}
Build the plan. If he fixed some hours, keep them exactly and fill the rest."""

REPAIR = """That plan was rejected: {error}

Send the corrected JSON, same shape. Keep everything he fixed. Cover
{start}-{end} exactly."""


class DayPlanner:
    def __init__(self, coach, cfg, db, agenda):
        self.coach = coach
        self.cfg = cfg
        self.db = db
        self.agenda = agenda

    def _events(self, day: date) -> str:
        rows = self.db.q(
            "SELECT at_ts, text FROM reminders WHERE at_ts >= ? AND at_ts < ?"
            " ORDER BY at_ts",
            (f"{day}T00:00", f"{day}T23:59"))
        if not rows:
            return ""
        lines = "\n".join(f"  {r['at_ts'][11:16]} {r['text']}" for r in rows)
        return f"FIXED THAT DAY -- plan around these, they are not optional:\n{lines}\n\n"

    def _history(self, day: date) -> str:
        prev = self.db.q(
            "SELECT date, start_min, end_min, label FROM blocks WHERE date < ?"
            " ORDER BY date DESC, start_min LIMIT 8", (day.isoformat(),))
        if not prev:
            return ""
        last = prev[0]["date"]
        lines = "\n".join(
            f"  {b['start_min']//60:02d}:{b['start_min']%60:02d}-"
            f"{b['end_min']//60:02d}:{b['end_min']%60:02d} {b['label']}"
            for b in prev if b["date"] == last)
        return f"WHAT HE PLANNED ON {last}, for the shape of his day:\n{lines}\n\n"

    def _steps(self, day: date) -> str:
        """Anything he said at the 20-minute checks -- what he is mid-way through."""
        rows = self.db.q(
            "SELECT label, answer FROM standups WHERE answer IS NOT NULL"
            " ORDER BY id DESC LIMIT 3")
        if not rows:
            return ""
        lines = "\n".join(f"  {r['label']}: {r['answer']}" for r in rows)
        return f"WHERE HE LEFT OFF (his own words, at the last checks):\n{lines}\n\n"

    async def build(self, day: date, text: str, now: datetime,
                    tries: int = 2) -> tuple[list, str]:
        """Returns (blocks, note). Raises planner.PlanError if it can't be made."""
        start = self.cfg.enforce_start_for(day)
        end = self.cfg.enforce_end_for(day)
        hhmm = lambda m: f"{m//60:02d}:{m%60:02d}"            # noqa: E731
        system = SYSTEM.format(name=self.cfg.user_name, start=hhmm(start), end=hhmm(end))
        prompt = BUILD.format(
            today=f"{now:%A %d %B %Y}", day=day.isoformat(), weekday=f"{day:%A}",
            start=hhmm(start), end=hhmm(end),
            text=(text or "").strip() or "(nothing -- build the whole day from the agenda)",
            agenda=self.agenda.for_planner(now.date()) or "  (no big tasks saved)",
            events=self._events(day), steps=self._steps(day), history=self._history(day))

        error = None
        for attempt in range(tries):
            contents = [prompt] if error is None else [
                prompt, REPAIR.format(error=error, start=hhmm(start), end=hhmm(end))]
            raw = await self.coach._ask(system, contents, now, max_tokens=1400,
                                        schema=SCHEMA)
            try:
                blocks, note = self._parse(raw)
                planner.validate_coverage(blocks, start, end)
                return blocks, note
            except planner.PlanError as exc:
                error = str(exc)
                log.info("dayplan attempt %d rejected: %s", attempt + 1, error)
        raise planner.PlanError(error or "I couldn't build a plan that covers the window.")

    @staticmethod
    def _parse(raw: str) -> tuple[list, str]:
        try:
            data = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise planner.PlanError(f"the plan came back unreadable ({exc})")
        rows = data.get("blocks") or []
        if not rows:
            raise planner.PlanError("no blocks came back")
        lines = []
        for b in rows:
            s, e = str(b.get("start", "")).strip(), str(b.get("end", "")).strip()
            label = str(b.get("label", "")).strip()
            if not (s and e and label):
                raise planner.PlanError("a block came back without a time or a name")
            lines.append(f"{s}-{e} {label}")
        return planner.parse("\n".join(lines)), str(data.get("note") or "").strip()


SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "blocks": {"type": "ARRAY", "items": {
            "type": "OBJECT",
            "properties": {"start": {"type": "STRING"}, "end": {"type": "STRING"},
                           "label": {"type": "STRING"}},
            "required": ["start", "end", "label"],
        }},
        "note": {"type": "STRING"},
    },
    "required": ["blocks", "note"],
}
