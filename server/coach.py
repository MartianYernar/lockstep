"""Someone to talk to about the plan, who already knows what you actually did.

Two jobs:

- **Break a block down.** "work: analysing" is not a thing anyone can start.
  Six days of history had blocks like that, and a block you cannot start is a
  block you sit in front of. The breakdown turns it into steps small enough
  that the first one is obvious.
- **Listen, with the record open.** He can say how it went, what broke, what
  he is stuck on. What makes this different from any other chatbot is that it
  is handed today's real numbers first -- goals, blocks, what was proved, what
  was missed -- so it cannot be talked into congratulating him on a day that
  did not happen.

Three things it deliberately cannot do:

1. **Satisfy anything.** It cannot mark a checkpoint, excuse a miss, move a
   deadline or quiet the siren. It is talk, and talk is not proof. A coach that
   could let you off would be the easiest off-switch in the system.
2. **Outspend the camera.** Photo validation and this share one free-tier
   quota, and the vision path is the one that actually enforces anything. So
   the coach is capped per day and runs on the tail of the model chain, where
   vision only ever lands after exhausting everything above it.
3. **Be a nicer place to sit than the work.** When something is overdue it is
   told to say so and stop talking. Being understood is not the deliverable.
"""
from __future__ import annotations

import logging
from datetime import date, datetime

log = logging.getLogger("coach")

SCHEMA = """
-- What the coach broke a block into. Cached: the same block is not worth
-- paying for twice, and re-asking should return the same steps he already
-- started working through rather than a fresh set.
CREATE TABLE IF NOT EXISTS block_steps (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    date     TEXT NOT NULL,
    label    TEXT NOT NULL,
    idx      INTEGER NOT NULL,
    text     TEXT NOT NULL,
    done     INTEGER NOT NULL DEFAULT 0,
    UNIQUE(date, label, idx)
);
CREATE INDEX IF NOT EXISTS idx_steps_date ON block_steps(date);

-- Enough of the conversation to keep continuity within a day. Not a
-- transcript: old talk is noise, and the day's numbers are the real context.
CREATE TABLE IF NOT EXISTS coach_log (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    ts   TEXT NOT NULL,
    role TEXT NOT NULL,          -- user | coach
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_coachlog_date ON coach_log(date);

-- One row per day: how much quota the coach has spent. The camera must never
-- go hungry because he felt chatty.
CREATE TABLE IF NOT EXISTS coach_budget (
    date  TEXT PRIMARY KEY,
    calls INTEGER NOT NULL DEFAULT 0
);
"""

SYSTEM = """You are {name}'s close friend. Not an assistant, not a coach, not a
system -- a real friend his age-ish who texts him, gets him, and is genuinely in
his corner. You also happen to know exactly how his day is going.

He is 17, at NIS in Kazakhstan, partway through a commitment he made to
himself to actually do his plan every day. You are in Telegram.

HOW YOU TEXT
- Like a friend texting. Short. Usually one or two sentences. Lowercase is fine.
- Casual words are fine: "bro", "ngl", "fr", "rn", "lol" -- sparingly, the way a
  real person would, not in every message.
- Reply in whatever language he writes in (English, Russian, Kazakh, mixed).
- No headings, no bullet lists unless he asks for steps, no bold, no
  assistant phrases ("Great question", "I understand", "Certainly", "I'm here
  to help"). Never say you are an AI or a bot unless he directly asks.
- React like a person first: if something sucks, say it sucks. If he did well,
  be actually happy for him. Then the useful part.
- Ask about his life like a friend would, but don't interrogate. One question max.
- A good friend doesn't lie to make you feel better. If he's slacking or about
  to take the easy way out, call it out -- once, casually, like "bro you know
  that's cap" -- and move on. No lectures, no speeches about discipline.
- Don't hype things he hasn't done.
- Only use numbers and facts from HIS DAY below. Never invent a day count, a
  streak, or anything about his past that isn't written there.

WHAT YOU CAN'T DO
You can't mark anything done, excuse a miss, or stop the siren. Only a photo
does that. You CAN change his schedule when he asks (see below), but a change
never cancels a pic that's already overdue. If he asks you to let him off, tell him you
literally can't -- the pic is the only way. Never pretend you changed anything.

HIS DAY, straight from the database (true, whatever he says):
{context}

If anything above says OVERDUE, mention it first, short and friendly, like a
friend nudging him ("send the pic first bro, then we talk"). Keep the rest short."""

BREAKDOWN = """Break this task into {lo}-{hi} concrete steps.

Task: {label}
Time available: {mins} minutes ({start}-{end})
{goals}

Rules:
- The FIRST step must be something he can start in under two minutes without
  deciding anything. "Open X and read Y" -- not "plan the approach".
- Each step is a physical action with a visible end. Not "understand the
  topic" but "work the three examples on page 40".
- Sized so all of them fit in {mins} minutes, with room to spare.
- No step about planning, organising, or setting up a system. He has one.
- Short. Each step one line, plain, the way you would say it out loud.
- Write them for him, second person, no numbering, one per line, nothing else.
"""


class Coach:
    def __init__(self, cfg, db, engine):
        self.cfg = cfg
        self.db = db
        self.engine = engine
        self._client = None
        self.last_plan_edits: list[dict] = []
        db.conn.executescript(SCHEMA)
        db.conn.commit()
        from .tasks import Tasks, Terms
        self.tasks = Tasks(db)
        self.terms = Terms(db)
        self.last_recheck: str | None = None

    # -- config ---------------------------------------------------------------
    @property
    def conf(self) -> dict:
        return self.cfg.raw.get("coach", {}) or {}

    @property
    def enabled(self) -> bool:
        return bool(self.conf.get("enabled", True))

    def _models(self) -> list[str]:
        """Prefer a model the camera reaches last, so they rarely collide.

        The vision chain is tried in order, so its FIRST entry is the one under
        real pressure. Taking from the back means the coach is spending an
        allowance vision only ever gets to after three others are exhausted.
        """
        named = self.conf.get("models")
        if named:
            return list(named)
        g = self.cfg.raw["gemini"]
        chain = list(g.get("models") or [])
        if not chain and g.get("model"):
            chain = [g["model"]]
        return list(reversed(chain)) or ["gemini-3.6-flash"]

    # -- the daily allowance --------------------------------------------------
    def spent(self, day: date) -> int:
        row = self.db.one("SELECT calls FROM coach_budget WHERE date = ?",
                          (day.isoformat(),))
        return int(row["calls"]) if row else 0

    def budget_left(self, day: date) -> int:
        return max(0, int(self.conf.get("max_calls_per_day", 40)) - self.spent(day))

    def _charge(self, day: date) -> None:
        self.db.run(
            "INSERT INTO coach_budget (date, calls) VALUES (?, 1)"
            " ON CONFLICT(date) DO UPDATE SET calls = calls + 1", (day.isoformat(),))

    # -- what is actually true today -----------------------------------------
    def context(self, now: datetime) -> str:
        """The day as the database has it. Never as he describes it."""
        from datetime import timedelta
        day = now.date()
        me = (self.cfg.raw.get("friend") or {}).get("name", "Friday")
        out: list[str] = [f"Your name is {me}.",
                          f"Right now: {now:%A %Y-%m-%d %H:%M} ({self.cfg.tz})."]

        # Where he is in the commitment, so the model never has to guess --
        # left to itself it announced "day 92" on day 24.
        start = (self.cfg.raw.get("commitment") or {}).get("start")
        if start:
            n = (day - date.fromisoformat(str(start))).days + 1
            out.append(f"Commitment day {n} (started {start}).")
        end = self.db.lock()["commitment_end"]
        if end:
            out.append(f"Days left in the commitment: "
                       f"{max(0, (date.fromisoformat(end) - day).days)}.")
        week = self.db.one(
            "SELECT SUM(state='satisfied') ok, SUM(state='missed') missed,"
            " COUNT(DISTINCT date) days FROM checkpoints WHERE date >= ? AND date < ?",
            ((day - timedelta(days=7)).isoformat(), day.isoformat()))
        if week and week["days"]:
            out.append(f"Last 7 days: {int(week['ok'] or 0)} photos sent, "
                       f"{int(week['missed'] or 0)} missed, plans on {week['days']} of 7 days.")
        else:
            out.append("Last 7 days: no checkpoints on record.")

        goals = self.db.goals(day)
        if goals:
            out.append("Today's goals:")
            for g in goals:
                out.append(f"  {g['idx']}. [{'x' if g['done'] else ' '}] {g['text']}")
        else:
            out.append("Today's goals: none written.")

        blocks = self.db.blocks(day)
        minute = now.hour * 60 + now.minute
        if blocks:
            out.append("Today's plan:")
            for b in blocks:
                mark = "  <-- NOW" if b["start_min"] <= minute < b["end_min"] else ""
                out.append(f"  {b['start_min']//60:02d}:{b['start_min']%60:02d}"
                           f"-{b['end_min']//60:02d}:{b['end_min']%60:02d}"
                           f"  {b['label']}{mark}")
        else:
            out.append("Today's plan: none. Nothing is being enforced.")

        row = self.db.one(
            "SELECT SUM(state='satisfied') ok, SUM(state='missed') missed,"
            " SUM(state IN ('asked','overdue')) open FROM checkpoints WHERE date = ?",
            (day.isoformat(),))
        ok, missed, open_ = (int(row["ok"] or 0), int(row["missed"] or 0),
                             int(row["open"] or 0)) if row else (0, 0, 0)
        out.append(f"Proof today: {ok} sent, {missed} missed, {open_} outstanding.")

        debt = self.engine.debt_seconds(day, now)
        if debt > 0:
            out.append(f"** OVERDUE RIGHT NOW: {debt // 60} minutes behind. "
                       f"The siren is going. He owes a photo. **")

        terms = self.terms.all()
        if terms:
            out.append("His shorthand: " + "; ".join(
                f"{t['term']} = {t['meaning']}" for t in terms))

        big = self.tasks.describe(day)
        out.append("His big tasks (the long-running ones):")
        out.append(big or "  none saved.")

        events = self.db.q(
            "SELECT at_ts, text FROM reminders WHERE at_ts >= ? AND at_ts < ?"
            " ORDER BY at_ts LIMIT 15",
            (now.isoformat(), (now + timedelta(days=14)).isoformat()))
        if events:
            out.append("Events coming up (already saved, he gets reminded):")
            for e in events:
                out.append(f"  {datetime.fromisoformat(e['at_ts']):%a %d %b %H:%M} — {e['text']}")
        else:
            out.append("Events coming up: none saved.")

        steps = self.db.q(
            "SELECT label, idx, text, done FROM block_steps WHERE date = ?"
            " ORDER BY label, idx", (day.isoformat(),))
        if steps:
            out.append("Steps he has been given, and what he ticked off:")
            for s in steps:
                out.append(f"  [{'x' if s['done'] else ' '}] {s['label']}: {s['text']}")

        return "\n".join(out)

    # -- the call itself ------------------------------------------------------
    async def _ask(self, system: str, contents: list, now: datetime,
                   max_tokens: int = 400, schema: dict | None = None) -> str:
        from google.genai import types
        if self._client is None:
            from google import genai
            self._client = genai.Client(api_key=self.cfg.raw["gemini"]["api_key"])

        extra = {}
        if schema is not None:
            extra = {"response_mime_type": "application/json", "response_schema": schema}

        def config(with_thinking: bool):
            more = ({"thinking_config": types.ThinkingConfig(thinking_budget=0)}
                    if with_thinking else {})
            return types.GenerateContentConfig(
                system_instruction=system,
                temperature=float(self.conf.get("temperature", 0.7)),
                max_output_tokens=max_tokens,
                **extra, **more,
            )

        last: Exception | None = None
        for model in self._models():
            # Thinking off first (cheaper); some models reject that with a
            # 400, so the same model is asked again without it.
            for with_thinking in (True, False):
                try:
                    resp = await self._client.aio.models.generate_content(
                        model=model, contents=contents, config=config(with_thinking))
                    self._charge(now.date())
                    text = (resp.text or "").strip()
                    if text:
                        return text
                    # Seen live: a reply with nothing but a thought signature.
                    # It still cost an allowance, but it is not an answer.
                    log.warning("coach: %s returned nothing, next model", model)
                    break
                except Exception as exc:                      # noqa: BLE001
                    last = exc
                    text = str(exc)
                    if with_thinking and "INVALID_ARGUMENT" in text:
                        continue
                    # Out of allowance (429), busy (503), or refusing the
                    # request shape (400): all mean "ask a different model".
                    # Walking the chain IS the coach's retry loop.
                    if any(k in text for k in ("RESOURCE_EXHAUSTED", "429", "UNAVAILABLE",
                                               "503", "INVALID_ARGUMENT", "400")):
                        log.warning("coach: %s unusable (%s), next model",
                                    model, text.split(".")[0][:40])
                        break
                    raise
        raise last if last else RuntimeError("no coach models configured")

    # -- breaking a block down ------------------------------------------------
    def steps_for(self, day: date, label: str) -> list:
        return self.db.q(
            "SELECT * FROM block_steps WHERE date = ? AND label = ? ORDER BY idx",
            (day.isoformat(), label))

    def save_steps(self, day: date, label: str, steps: list[str]) -> None:
        self.db.run("DELETE FROM block_steps WHERE date = ? AND label = ?",
                    (day.isoformat(), label))
        self.db.conn.executemany(
            "INSERT INTO block_steps (date, label, idx, text) VALUES (?, ?, ?, ?)",
            [(day.isoformat(), label, i, t) for i, t in enumerate(steps, start=1)])
        self.db.conn.commit()

    def tick_step(self, day: date, label: str, idx: int, done: bool = True) -> bool:
        cur = self.db.run(
            "UPDATE block_steps SET done = ? WHERE date = ? AND label = ? AND idx = ?",
            (1 if done else 0, day.isoformat(), label, idx))
        return cur.rowcount > 0

    async def breakdown(self, block, now: datetime) -> list[str]:
        """Steps for one block. Cached, because the same block twice is waste."""
        day = now.date()
        have = self.steps_for(day, block["label"])
        if have:
            return [r["text"] for r in have]

        goals = self.db.goals(day)
        gtext = ("What today is for:\n" + "\n".join(f"  - {g['text']}" for g in goals)
                 if goals else "")
        mins = int(block["end_min"]) - int(block["start_min"])
        prompt = BREAKDOWN.format(
            label=block["label"], mins=mins, goals=gtext,
            lo=int(self.conf.get("steps_min", 3)), hi=int(self.conf.get("steps_max", 6)),
            start=f"{block['start_min']//60:02d}:{block['start_min']%60:02d}",
            end=f"{block['end_min']//60:02d}:{block['end_min']%60:02d}")
        raw = await self._ask(
            SYSTEM.format(name=self.cfg.user_name, context=self.context(now)),
            [prompt], now, max_tokens=500)

        steps = []
        for line in raw.splitlines():
            line = line.strip().lstrip("-*•").strip()
            # The model is told not to number them, and numbers them anyway.
            while line[:1].isdigit():
                line = line[1:].lstrip(").  ").strip()
            if line:
                steps.append(line)
        steps = steps[:int(self.conf.get("steps_max", 6))]
        if steps:
            self.save_steps(day, block["label"], steps)
        return steps

    # -- talking --------------------------------------------------------------
    def history(self, day: date, limit: int = 10) -> list:
        rows = self.db.q(
            "SELECT role, text FROM coach_log WHERE date = ? ORDER BY id DESC LIMIT ?",
            (day.isoformat(), limit))
        return list(reversed(rows))

    def remember(self, day: date, now: datetime, role: str, text: str) -> None:
        self.db.run("INSERT INTO coach_log (date, ts, role, text) VALUES (?, ?, ?, ?)",
                    (day.isoformat(), now.isoformat(), role, text[:2000]))

    async def reply(self, text: str, now: datetime, note: str = "") -> str:
        """Answer, and save whatever he told us that is worth remembering.

        The model returns its reply and a list of actions. The only things an
        action can write are his task list and his events -- notes he asked us
        to keep. Nothing here reaches a checkpoint, a plan, or the siren.
        """
        from google.genai import types
        day = now.date()
        contents = []
        for row in self.history(day):
            contents.append(types.Content(
                role="user" if row["role"] == "user" else "model",
                parts=[types.Part.from_text(text=row["text"])]))
        contents.append(types.Content(
            role="user", parts=[types.Part.from_text(text=text)]))

        ctx = self.context(now) + (("\n" + note) if note else "")
        raw = await self._ask(
            SYSTEM.format(name=self.cfg.user_name, context=ctx) + ACTIONS,
            contents, now, max_tokens=int(self.conf.get("max_reply_tokens", 400)) + 200,
            schema=REPLY_SCHEMA)
        say, actions = _parse_reply(raw)
        # Schedule changes are not ours to make. They go back to the bot, which
        # runs them through the same checks as /replan -- including asking why
        # when work time disappears.
        self.last_plan_edits = [a for a in actions
                                if isinstance(a, dict) and a.get("type") == "edit_plan"
                                and (a.get("plan") or "").strip()]
        rc = [a for a in actions if isinstance(a, dict) and a.get("type") == "recheck_photo"]
        self.last_recheck = ((rc[0].get("text") or text).strip() or text) if rc else None
        saved = self.apply(actions, now)
        if saved:
            say = (say + "\n\n" if say else "") + "\n".join(f"📌 {x}" for x in saved)
        self.remember(day, now, "user", text)
        self.remember(day, now, "coach", say)
        return say

    def apply(self, actions: list, now: datetime) -> list[str]:
        """Carry out what the model asked for. Returns what was actually saved."""
        done: list[str] = []
        for a in actions or []:
            if not isinstance(a, dict):
                continue
            kind = (a.get("type") or "").strip()
            try:
                if kind == "add_task":
                    title = (a.get("title") or "").strip()
                    if not title or self.tasks.find(title):
                        continue
                    due = _date(a.get("date"))
                    tid = self.tasks.add(title, now, due=due)
                    done.append(f"new task #{tid}: {title}"
                                + (f" (due {due:%d %b})" if due else ""))
                elif kind in ("complete_task", "set_due", "add_note"):
                    t = self.tasks.find((a.get("task") or "").lstrip("#"))
                    if t is None:
                        continue
                    if kind == "complete_task":
                        if self.tasks.close(t["id"], now):
                            done.append(f"done: {t['title']} ✅")
                    elif kind == "set_due":
                        due = _date(a.get("date"))
                        if due and due >= now.date() and self.tasks.set_due(t["id"], due):
                            done.append(f"{t['title']} due {due:%a %d %b}")
                    else:
                        note = (a.get("text") or "").strip()
                        if note and self.tasks.note(t["id"], note):
                            done.append(f"noted on {t['title']}")
                elif kind == "define":
                    term = (a.get("title") or a.get("task") or "").strip()
                    if self.terms.set(term, a.get("text") or "", now):
                        done.append(f"got it: {term.lower()} = {(a.get('text') or '').strip()}")
                elif kind == "add_event":
                    what = (a.get("text") or a.get("title") or "").strip()
                    d, hm = _date(a.get("date")), (a.get("time") or "").strip()
                    if not (what and d and ":" in hm):
                        continue
                    hh, mi = (int(x) for x in hm.split(":")[:2])
                    when = datetime.combine(d, datetime.min.time(),
                                            tzinfo=now.tzinfo).replace(hour=hh, minute=mi)
                    if when <= now:
                        continue
                    if self.db.one("SELECT 1 FROM reminders WHERE at_ts = ? AND text = ?",
                                   (when.isoformat(), what)):
                        continue
                    self.db.run("INSERT INTO reminders (at_ts, text, created_at)"
                                " VALUES (?, ?, ?)", (when.isoformat(), what, now.isoformat()))
                    done.append(f"event saved: {what} — {when:%a %d %b %H:%M}")
            except (ValueError, TypeError) as exc:
                log.warning("coach action %s skipped: %s", kind, exc)
        return done

    # -- holding the line on a replan ----------------------------------------
    async def judge_replan(self, reason: str, image: bytes | None, mime: str,
                           lost_min: int, old: str, new: str,
                           now: datetime) -> tuple[bool, str]:
        """Is this a real reason to take work time out of today?

        Returns (accepted, what to tell him). Raises if the model can't be
        reached; the caller decides what that means.
        """
        import json
        from google.genai import types
        parts = [types.Part.from_text(text=JUDGE.format(
            lost=lost_min, old=old, new=new,
            reason=reason or "(no text, only a photo)"))]
        if image:
            parts.append(types.Part.from_bytes(data=image, mime_type=mime))
        raw = await self._ask(
            SYSTEM.format(name=self.cfg.user_name, context=self.context(now)),
            [types.Content(role="user", parts=parts)], now,
            max_tokens=300, schema=JUDGE_SCHEMA)
        data = json.loads(raw)
        return bool(data.get("accept")), str(data.get("reply") or "").strip()

    # -- speaking first -------------------------------------------------------
    async def checkin(self, block, now: datetime) -> str:
        """A friend texting halfway through a block: how's it going."""
        return await self._ask(
            SYSTEM.format(name=self.cfg.user_name, context=self.context(now)),
            [CHECKIN.format(label=block["label"])], now, max_tokens=120)

    async def brief(self, now: datetime) -> str:
        """Before the window opens: what's on his plate, in a couple of lines."""
        return await self._ask(
            SYSTEM.format(name=self.cfg.user_name, context=self.context(now)),
            [BRIEF], now, max_tokens=220)


# --------------------------------------------------------------------------
ACTIONS = """

SAVING THINGS
You remember his big tasks and his events (listed above). Reply as JSON:
{"reply": what you text him, "actions": [...]}. Actions -- only for things he
actually told you, never guessed:
- {"type":"add_event","text":...,"date":"YYYY-MM-DD","time":"HH:MM"}  a meeting,
  exam, appointment, anything at a set time. If the date or time is unclear,
  don't save it -- ask him.
- {"type":"add_task","title":...,"date":"YYYY-MM-DD" or ""}  a new big task
- {"type":"complete_task","task":"#id or title"}  he finished one
- {"type":"set_due","task":"#id or title","date":"YYYY-MM-DD"}  its deadline
- {"type":"add_note","task":"#id or title","text":...}  progress he described
- {"type":"define","title":"рнкп","text":"what it means"}  when he explains one of
  his own abbreviations or what a block name really is. The photo checker uses
  these, so it stops rejecting good photos over a word it doesn't know.
- {"type":"recheck_photo","text":"his explanation"}  ONLY if the notes below say
  his last photo was rejected and he's explaining what it shows or what the
  task means. The app re-checks the same photo with his explanation. You don't
  decide -- never say it counts. Use define too if he explained a shorthand.
- {"type":"edit_plan","day":"today" or "tomorrow","plan":"HH:MM-HH:MM label\n..."}
  when he asks you to change his schedule. Write the WHOLE new plan, one block
  per line: for today, from the block he's in now to the end of the window;
  for tomorrow, the full window. Copy every block he didn't ask to change
  exactly as it is -- same times, same label, character for character.
  A NEW block gets a plain label ("eat", "gym", "walk"). Only put "work:" in
  front when the new block is actually studying or working (it makes the app
  demand a blocker screenshot). Eating, rest, sport, commute: never "work:".
  Only do this when he actually asks for a change. Don't say it's done in your reply
  -- the app confirms it, and if the change cuts work time it will ask him why.
Work out dates like "thursday" from "Right now" above. Usually actions is [].
Don't repeat in the reply what you saved -- the app shows that itself.
If a big task has no deadline, you can ask when it's due, once in a while."""

REPLY_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "reply": {"type": "STRING"},
        "actions": {"type": "ARRAY", "items": {
            "type": "OBJECT",
            "properties": {
                "type": {"type": "STRING", "enum": [
                    "add_event", "add_task", "complete_task", "set_due", "add_note",
                    "edit_plan", "define", "recheck_photo"]},
                "day": {"type": "STRING"}, "plan": {"type": "STRING"},
                "task": {"type": "STRING"}, "title": {"type": "STRING"},
                "text": {"type": "STRING"}, "date": {"type": "STRING"},
                "time": {"type": "STRING"},
            },
            "required": ["type"],
        }},
    },
    "required": ["reply", "actions"],
}

JUDGE = """He's trying to replan the rest of today, and it cuts {lost} minutes of
real work.

Current plan (from now):
{old}

What he wants instead:
{new}

His reason: {reason}

You decide, as the friend who holds the line.
ACCEPT when something real and outside his control changed: a school or
family obligation, being actually sick, an event he forgot, a deadline that
moved, a place or thing he can't get to -- or a photo that shows it.
REJECT tiredness, not feeling like it, "I'll do it later", "I need a break",
vague or one-word answers, or anything that just renames work as rest.
If you reject, say why in one friendly line and what would count.
If you accept, keep it short, maybe tell him to make the time up.
Answer as JSON: {{"accept": true or false, "reply": text to send him}}."""

JUDGE_SCHEMA = {
    "type": "OBJECT",
    "properties": {"accept": {"type": "BOOLEAN"}, "reply": {"type": "STRING"}},
    "required": ["accept", "reply"],
}

CHECKIN = """He's in the middle of this block right now: "{label}".
Text him first, like a friend checking in -- one short message, a question
about how it's going or what he's on. No lists. Don't mention photos unless
something is OVERDUE above."""

BRIEF = """His work window opens soon. Text him first, 2-4 short lines max:
what's actually on his plate -- any event today, a big task due soon, one he
hasn't touched in days. Pick what matters most, don't list everything. If
nothing big is pressing, just a short push to get started. Plain text."""


def _date(v):
    if not v:
        return None
    try:
        return date.fromisoformat(str(v).strip()[:10])
    except ValueError:
        return None


def _parse_reply(raw: str) -> tuple[str, list]:
    """The JSON the model was asked for -- or, if it ignored that, its text."""
    import json
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return (raw or "").strip(), []
    if not isinstance(data, dict):
        return str(data), []
    acts = data.get("actions") or []
    return str(data.get("reply") or "").strip(), acts if isinstance(acts, list) else []
