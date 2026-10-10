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
import re

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
- Never write "work: " in front of a label. It is not a word, it is a switch
  that changes what proof the app demands, and only he flips it. If HE wrote
  "work: something", keep that block's label exactly as he wrote it. His words
  are his: if he wrote "do math sat questions on knees", that block stays
  exactly that.
- 45 to 120 minutes per work block. Shorter than 45 is churn, longer than 120
  he will not sustain. Non-work blocks can be any length.
- Give every big task he names its own block. Never merge two tasks into one
  block ("math and physics" is not a block).
- Nothing for a task after its deadline. If the chat says something is due at
  a time today ("IA due 8pm"), every block for it ends by then -- work on it
  after the deadline is worthless.

HOW TO CHOOSE WHAT GOES IN THE FREE TIME:
- The agenda below is ordered by urgency. Anything marked WILL MISS or OVERDUE
  gets time today, before anything marked on track.
- Respect "needs Xh/day" -- that is the number that makes the deadline.
- Two or three different tasks in an evening, not five. Switching costs him.
- Hardest thing first, while he is fresh, unless he says otherwise.
- Leave what he already fixed exactly where he put it.
- Read the chat below. If he told Friday something today ("IA deadline moved to
  8pm", "I'm wiped", "SAT first"), it shapes this plan. A deadline tonight
  means that task goes first and ends before the deadline.
- Never invent a class, lesson, meeting or event. Only put in fixed things he
  wrote or that are in the FIXED list.
- HIS LIST FOR TODAY, if one is below, is what he decided this day is for. The
  unticked lines are the work; build the blocks out of them, in his words, and
  group related ones into a single block rather than making a block per line.
  Ticked lines are already done -- never plan them again."""

BUILD = """TODAY IS {today}. This plan is for {day} ({weekday}).
The enforced window is {start}-{end}. He must be doing something the whole time.

WHAT HE SAID:
{text}

HIS BIG TASKS, most urgent first:
{agenda}

{events}{steps}{doc}{chat}
Build the plan. If he fixed some hours, keep them exactly and fill the rest."""

# 21 Sep: every "change X" re-sent the whole old plan as if he had typed it,
# under a prompt that says keep what he fixed. The planner kept everything and
# could not honour the change -- seven rebuilds in nine minutes. A revision is
# its own job: here is the plan, here is the one thing to change.
REVISE = """TODAY IS {today}. You proposed this plan for {day} ({weekday}); he
has seen it and wants a change.

THE PLAN HE WAS SHOWN:
{current}

THE CHANGE HE WANTS (do exactly this):
{change}

What he asked for in the first place:
{text}

HIS BIG TASKS, most urgent first:
{agenda}

{events}{doc}{chat}
Make his change. Keep every other block as it was unless the change forces it
to move; if it does, move the fewest blocks you can -- and never past a
task's deadline (check the chat for a time today). If his change can't be done
without breaking a deadline, do the closest thing that keeps it and say so in
"note". Cover {start}-{end}
exactly. In "note", say in one line what you changed."""

REPAIR = """That plan was rejected: {error}

Send the corrected JSON, same shape. Keep everything he fixed. Cover
{start}-{end} exactly."""


PROPOSAL_SCHEMA = """
-- A built plan, exactly as he was shown it, waiting for his button.
--
-- 19 Sep he locked one and /today showed something else. The button carried
-- no identity: it read "whatever proposal is in memory right now", so a
-- second offer -- he replies, it rebuilds -- silently replaced the first, and
-- tapping the older message locked the newer plan. In memory it also died on
-- every restart. Now the id is in the button and the text is on disk, so what
-- he taps is what he gets, an hour later or after a deploy.
CREATE TABLE IF NOT EXISTS proposals (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    date       TEXT NOT NULL,
    text       TEXT NOT NULL,
    note       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    used_at    TEXT
);
"""


class Proposals:
    def __init__(self, db):
        self.db = db
        db.conn.executescript(PROPOSAL_SCHEMA)
        db.conn.commit()

    def add(self, day, text: str, note: str, now) -> int:
        return self.db.run(
            "INSERT INTO proposals (date, text, note, created_at) VALUES (?, ?, ?, ?)",
            (day.isoformat(), text, note or "", now.isoformat())).lastrowid

    def get(self, pid: int):
        return self.db.one("SELECT * FROM proposals WHERE id = ?", (pid,))

    def use(self, pid: int, now) -> bool:
        """Claim it exactly once, so a double tap cannot lock it twice."""
        return self.db.run(
            "UPDATE proposals SET used_at = ? WHERE id = ? AND used_at IS NULL",
            (now.isoformat(), pid)).rowcount > 0


class DayPlanner:
    def __init__(self, coach, cfg, db, agenda, doc=None):
        self.coach = coach
        self.cfg = cfg
        self.db = db
        self.agenda = agenda
        self.doc = doc
        self._doc_text = ""

    def _events(self, day: date) -> str:
        rows = self.db.q(
            "SELECT at_ts, text FROM reminders WHERE at_ts >= ? AND at_ts < ?"
            " ORDER BY at_ts",
            (f"{day}T00:00", f"{day}T23:59"))
        if not rows:
            return ""
        lines = "\n".join(f"  {r['at_ts'][11:16]} {r['text']}" for r in rows)
        return f"FIXED THAT DAY -- plan around these, they are not optional:\n{lines}\n\n"

    def _chat(self, now: datetime) -> str:
        """Today's conversation with Friday -- where "the IA moved to 8pm" lives.

        This replaced yesterday's plan, which the model copied from: a Monday
        lesson it had seen once turned up in a proposal as if it were fixed.
        """
        rows = self.db.q(
            "SELECT role, text FROM coach_log WHERE date = ? ORDER BY id DESC LIMIT 8",
            (now.date().isoformat(),))
        if not rows:
            return ""
        lines = "\n".join(
            f"  {'him' if r['role'] == 'user' else 'Friday'}: {r['text'][:300]}"
            for r in reversed(rows))
        return f"TODAY'S CHAT WITH FRIDAY (newest last):\n{lines}\n\n"

    @staticmethod
    def _keep_his_switch(blocks: list, his: str) -> list:
        """Drop a "work:" the model added. Keep one he wrote himself.

        The prompt says this; 21 Sep it still put "work:" on "sat prep", and
        the app demanded a blocker screenshot for SAT questions on paper. A
        rule that decides what proof he owes is enforced here, not requested.
        """
        low = re.sub(r"\s+", " ", (his or "").lower())
        out = []
        for b in blocks:
            m = re.match(r"(?i)\s*work:\s*(.+)$", b.label)
            if m:
                rest = m.group(1).strip()
                if not re.search(r"work:\s*" + re.escape(rest.lower()), low):
                    b = planner.Block(b.start_min, b.end_min, rest, planner.categorise(rest))
            out.append(b)
        return out

    def _steps(self, day: date) -> str:
        """Anything he said at the 20-minute checks -- what he is mid-way through."""
        rows = self.db.q(
            "SELECT label, answer FROM standups WHERE answer IS NOT NULL"
            " ORDER BY id DESC LIMIT 3")
        if not rows:
            return ""
        lines = "\n".join(f"  {r['label']}: {r['answer']}" for r in rows)
        return f"WHERE HE LEFT OFF (his own words, at the last checks):\n{lines}\n\n"

    async def _doc_section(self, day: date, now: datetime) -> str:
        """His own list for that day, from the Google Doc. "" if there is none."""
        doc = getattr(self, "doc", None)
        if doc is None or not doc.enabled:
            return ""
        try:
            sec = await doc.for_day(day, now)
        except Exception as exc:                              # noqa: BLE001
            log.warning("doc lookup failed: %s", exc)
            return ""
        body = doc.describe(sec)
        if not body:
            return ""
        return ("HIS LIST FOR THAT DAY, written by him in his doc "
                "([x] = already done, do not plan it again):\n" + body + "\n\n")

    async def build(self, day: date, text: str, now: datetime, tries: int = 2,
                    current: str | None = None, change: str | None = None
                    ) -> tuple[list, str]:
        """Returns (blocks, note). Raises planner.PlanError if it can't be made.

        With `current` and `change`, edits a plan he was already shown instead
        of starting again.
        """
        start = self.cfg.enforce_start_for(day)
        end = self.cfg.enforce_end_for(day)
        hhmm = lambda m: f"{m//60:02d}:{m%60:02d}"            # noqa: E731
        system = SYSTEM.format(name=self.cfg.user_name, start=hhmm(start), end=hhmm(end))
        said = (text or "").strip()
        self._doc_text = await self._doc_section(day, now)
        common = dict(
            today=f"{now:%A %d %B %Y}", day=day.isoformat(), weekday=f"{day:%A}",
            start=hhmm(start), end=hhmm(end),
            agenda=self.agenda.for_planner(now.date()) or "  (no big tasks saved)",
            events=self._events(day), chat=self._chat(now),
            doc=self._doc_text)
        if current and change:
            prompt = REVISE.format(current=current, change=change.strip(),
                                   text=said or "(he asked you to build the day)", **common)
        else:
            prompt = BUILD.format(
                text=said or "(nothing -- build the whole day from the agenda)",
                steps=self._steps(day), **common)
        his = "\n".join(x for x in (said, change or "") if x)

        error = None
        for attempt in range(tries):
            contents = [prompt] if error is None else [
                prompt, REPAIR.format(error=error, start=hhmm(start), end=hhmm(end))]
            raw = await self.coach._ask(system, contents, now, max_tokens=1400,
                                        schema=SCHEMA)
            try:
                blocks, note = self._parse(raw)
                blocks = self._keep_his_switch(blocks, his)
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
