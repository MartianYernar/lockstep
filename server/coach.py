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

-- What he has told Friday that is true beyond today and is not a task or an
-- event: "IELTS exam is 18 Oct", "I submitted the IA", "mum's birthday
-- Friday". 22 Sep the chat only ever saw today's last ten messages, so at
-- 08:48 nothing from the night before existed. This is the part that lasts.
CREATE TABLE IF NOT EXISTS memories (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    text       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    active     INTEGER NOT NULL DEFAULT 1
);

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

HOW YOU THINK (this is what makes you worth talking to)
- Read what he ACTUALLY asked or told you, and answer THAT first. If he asks
  for something ("give me the plan", "what's next", "why sat first"), the
  first thing in your reply is the answer or the action, not a nag.
- Infer. He texts fast and half-finished: "the ia" is Physics IA, "3rd oct" is
  the SAT, "extended to 8pm" means the deadline is today at 20:00. Work out
  what he means from HIS DAY and the chat so far; don't make him spell it out,
  and don't ask something the context already answers.
- His NEWS beats the database. The database knows proof: photos, misses, what
  was planned. It does NOT know the world. When he tells you a deadline moved,
  a class got cancelled, an event changed -- that is newer than anything
  below. Believe it, save it (set_due / add_event), and talk as if it's
  already true. Never answer his news with the old value ("it's already a
  day late") -- that's the dumbest thing a friend can do.
- What the database DOES win on: whether he did the work. "I did 2 hours" with
  no photos on record is still 0 proven hours -- say so if it matters.
- Your reply and your actions go out together, so the reply must already
  agree with the actions. If you set a new deadline, talk about the new one.
- Think one step ahead for him: what's due soonest, what the next block is,
  what's about to be a problem. Mention it only if it matters right now.

WHAT YOU CAN'T DO
You can't mark anything done, excuse a miss, or stop the siren. Only a photo
does that. You CAN change his schedule when he asks (see below), but a change
never cancels a pic that's already overdue. If he asks you to let him off, tell him you
literally can't -- the pic is the only way. Never pretend you changed anything.

HIS DAY, straight from the database (the record of what he proved; his news
about deadlines and events is newer):
{context}

If something above says OVERDUE, work it in -- one short line, and name the
real thing he owes (a plan is not a photo). But answer his actual message
first; a nag instead of an answer makes you useless."""

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
        from .foundations import Foundations
        self.foundations = Foundations(db, cfg)
        from .standup import Standups
        self.standups = Standups(db, cfg)
        self.last_recheck: str | None = None
        self.last_judge_was_reason = True
        self.last_latenight: str | None = None
        self.last_make_plan: dict | None = None
        self.last_lock_plan = False
        # Filled in before each reply by the bot, which owns the fetching.
        self.doc_today = ""
        self.last_on_paper: str | None = None

    # -- config ---------------------------------------------------------------
    @property
    def conf(self) -> dict:
        return self.cfg.raw.get("coach", {}) or {}

    @property
    def enabled(self) -> bool:
        return bool(self.conf.get("enabled", True))

    def _models(self, tier: str = "smart") -> list[str]:
        """Which models a call may use, strongest first.

        "smart" is for anything that has to understand him: chat, building a
        plan, judging a reason. It leads with the one full model in the chain.
        "cheap" is for the bot's own nags, briefs and check-ins, and never
        touches that model.

        21 Sep: 86 calls by the evening, most of them wording nags he never
        answered. That emptied the full model's daily allowance, so when he
        actually talked to Friday he got a lite model -- which is why it told
        him his IA was "a day late" a sentence after he said the deadline had
        moved. The nags were spending the brain on the mouth.

        Both tiers still start from the back of the vision chain, so the
        camera's first choice is the last thing either of them touches.
        """
        named = self.conf.get(f"{tier}_models") or (
            self.conf.get("models") if tier == "smart" else None)
        if named:
            return list(named)
        g = self.cfg.raw["gemini"]
        chain = list(g.get("models") or [])
        if not chain and g.get("model"):
            chain = [g["model"]]
        order = list(reversed(chain)) or ["gemini-3.6-flash"]
        if tier == "cheap" and len(order) > 1:
            return order[1:]
        return order

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
            # 21 Sep, 16:17: no plan was locked yet, and Friday told him to send
            # "the pic for physics ia" -- a photo nothing was asking for. Say
            # what the siren actually wants.
            if not self.engine.has_plan(day):
                out.append(f"** OVERDUE RIGHT NOW: {debt // 60} minutes behind because "
                           "TODAY HAS NO LOCKED PLAN. The siren wants a plan, not a "
                           "photo -- a locked plan is what stops it. **")
            else:
                cp = self.engine.current_checkpoint(day, now)
                what = (f" for the block \"{cp['label']}\" (due {cp['due_at'][11:16]})"
                        if cp is not None and cp["label"] else "")
                out.append(f"** OVERDUE RIGHT NOW: {debt // 60} minutes behind. "
                           f"The siren is going. He owes a photo{what}. When you mention "
                           f"it, name that block so he knows which pic. **")

        mem = self.db.q("SELECT id, text, created_at FROM memories WHERE active = 1"
                        " ORDER BY id DESC LIMIT 40")
        if mem:
            out.append("WHAT YOU KNOW ABOUT HIM (long-term memory, true until he says "
                       "otherwise -- use it, don't re-ask it):")
            for m in reversed(mem):
                out.append(f"  m{m['id']} ({m['created_at'][:10]}): {m['text']}")

        terms = self.terms.all()
        if terms:
            out.append("His shorthand: " + "; ".join(
                f"{t['term']} = {t['meaning']}" for t in terms))

        said = self.standups.describe(day)
        if said:
            out.append("What he said he was doing, at the 20-min checks "
                       "(his own words -- compare them, notice drift):")
            out.append(said)

        bed_night = self.engine.bed_night(now)
        if bed_night is not None:
            bcp = self.engine.bed_checkpoint(bed_night)
            if bcp is not None and bcp["state"] != "satisfied":
                out.append(f"BEDTIME: it's past his bedtime ({self.engine.bed_due(bed_night):%H:%M}) "
                           "and the siren wants a photo of his bed, opened. If he has a real "
                           "deadline or is out with friends, use start_latenight.")

        if self.foundations.enabled:
            night = self.foundations.night_of(now)
            out.append("Tonight's basics (end-of-day checklist): "
                       + self.foundations.describe(night))

        if getattr(self, "doc_today", ""):
            out.append("HIS LIST FOR TODAY, from his own doc "
                       "([x] = he has ticked it off):")
            out.append(self.doc_today)
            out.append("That list is what he planned for himself. It is not "
                       "proof -- only photos are -- but it is what he means to "
                       "do, so talk about today from it.")

        big = self.tasks.describe(day)
        out.append("His big tasks (the long-running ones):")
        out.append(big or "  none saved.")
        if getattr(self, "agenda", None) is not None:
            risk = self.agenda.at_risk(day)
            if risk:
                out.append("Deadlines that do not currently fit in the time left:")
                for r in risk[:4]:
                    out.append("  " + self.agenda.line(r))

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
                   max_tokens: int = 400, schema: dict | None = None,
                   tier: str = "smart") -> str:
        from google.genai import types
        if self._client is None:
            from google import genai
            self._client = genai.Client(api_key=self.cfg.raw["gemini"]["api_key"])

        extra = {}
        if schema is not None:
            extra = {"response_mime_type": "application/json", "response_schema": schema}

        # The smart tier is allowed to think before it answers. "He said the
        # deadline moved, so it's due tonight, not overdue" is one step of
        # reasoning, and with thinking switched off the model skipped it.
        # Thinking is paid in tokens, not requests -- the free tier's limit
        # is requests -- so on the calls that matter it is nearly free.
        budget = int(self.conf.get("smart_thinking", 1024)) if tier == "smart" else 0

        def config(with_thinking: bool):
            more = ({"thinking_config": types.ThinkingConfig(thinking_budget=budget)}
                    if with_thinking else {})
            return types.GenerateContentConfig(
                system_instruction=system,
                temperature=float(self.conf.get("temperature", 0.7)),
                max_output_tokens=max_tokens + (budget if with_thinking else 0),
                **extra, **more,
            )

        last: Exception | None = None
        for model in self._models(tier):
            # The configured thinking first; some models reject the setting
            # with a 400, so the same model is asked again without it.
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
                    # Out of allowance (429), busy (503), broken on their side
                    # (500 INTERNAL), or refusing the request shape (400): all
                    # mean "ask a different model". Walking the chain IS the
                    # coach's retry loop. 10 Oct a 500 was not in this list, so
                    # a transient fault on one model became "something broke on
                    # my end" while three other models sat there working.
                    if any(k in text for k in ("RESOURCE_EXHAUSTED", "429", "UNAVAILABLE",
                                               "503", "INTERNAL", "500",
                                               "INVALID_ARGUMENT", "400")):
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
    def history(self, day: date, limit: int = 16) -> list:
        """The recent conversation, across midnight.

        It used to be today's rows only, so every morning Friday woke up with
        no idea what was said at 23:00.
        """
        from datetime import timedelta
        rows = self.db.q(
            "SELECT role, text FROM coach_log WHERE date >= ? ORDER BY id DESC LIMIT ?",
            ((day - timedelta(days=1)).isoformat(), limit))
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
            contents, now, max_tokens=int(self.conf.get("max_reply_tokens", 400)) + 600,
            schema=REPLY_SCHEMA)
        say, actions = _parse_reply(raw)
        # Schedule changes are not ours to make. They go back to the bot, which
        # runs them through the same checks as /replan -- including asking why
        # when work time disappears.
        self.last_plan_edits = [a for a in actions
                                if isinstance(a, dict) and a.get("type") == "edit_plan"
                                and (a.get("plan") or "").strip()]
        op = [a for a in actions if isinstance(a, dict) and a.get("type") == "on_paper"]
        self.last_on_paper = (op[0].get("text") or "").strip() if op else None
        mp = [a for a in actions if isinstance(a, dict) and a.get("type") == "make_plan"]
        self.last_make_plan = mp[0] if mp else None
        self.last_lock_plan = any(isinstance(a, dict) and a.get("type") == "lock_plan"
                                  for a in actions)
        ln = [a for a in actions if isinstance(a, dict) and a.get("type") == "start_latenight"]
        self.last_latenight = (ln[0].get("text") or "").strip() if ln else None
        rc = [a for a in actions if isinstance(a, dict) and a.get("type") == "recheck_photo"]
        self.last_recheck = ((rc[0].get("text") or text).strip() or text) if rc else None
        saved = self.apply(actions, now)
        if saved:
            say = (say + "\n\n" if say else "") + "\n".join(f"📌 {x}" for x in saved)
        # A list he asks for comes from the database, never from the model's
        # memory of it: 22 Sep it dropped half the tasks and cut off mid-JSON.
        shows = [a for a in actions if isinstance(a, dict) and a.get("type") == "show_list"]
        if shows:
            say = (say + "\n\n" if say else "") + self.render_list(
                (shows[0].get("text") or "all").strip().lower(), now)
        self.remember(day, now, "user", text)
        self.remember(day, now, "coach", say)
        return say

    def render_list(self, scope: str, now: datetime) -> str:
        """His tasks and events, numbered, exactly as stored."""
        from datetime import timedelta
        day = now.date()
        parts = []
        if not scope.startswith("event"):
            rows = self.tasks.open()
            lines = []
            for r in rows:
                bit = ""
                if r["due"]:
                    d = date.fromisoformat(r["due"])
                    left = (d - day).days
                    bit = (f" — OVERDUE ({d:%a %d %b})" if left < 0 else
                           " — due TODAY" if left == 0 else
                           " — due tomorrow" if left == 1 else
                           f" — due {d:%a %d %b} ({left}d)")
                lines.append(f"#{r['id']} {r['title']}{bit}")
            parts.append(f"📋 tasks ({len(rows)}):\n" + ("\n".join(lines) or "none"))
        if not scope.startswith("task"):
            ev = self.db.q(
                "SELECT at_ts, text FROM reminders WHERE at_ts >= ? AND at_ts < ?"
                " ORDER BY at_ts LIMIT 30",
                (now.isoformat(), (now + timedelta(days=60)).isoformat()))
            lines = [f"{datetime.fromisoformat(e['at_ts']):%a %d %b %H:%M} — {e['text']}"
                     for e in ev]
            parts.append(f"📅 events ({len(ev)}):\n" + ("\n".join(lines) or "none coming up"))
        return "\n\n".join(parts)

    def apply(self, actions: list, now: datetime) -> list[str]:
        """Carry out what the model asked for, and account for every one.

        18 Sep he sent a numbered list of sixteen things. Two of them --
        "Physics IA submit by 20/09" and "CAS mandatory project" -- matched
        tasks he already had, so the duplicate guard skipped them without a
        word, and the 20 Sep deadline went with them. A silent skip on a list
        of deadlines is the worst failure this thing can have: he believes it
        was saved.

        So nothing returns silently now. Every action produces a line: saved,
        merged into one he already had, or refused with the reason.
        """
        done: list[str] = []
        for a in actions or []:
            if not isinstance(a, dict):
                done.append("⚠️ couldn't read one of the items you sent — say it again?")
                continue
            kind = (a.get("type") or "").strip()
            try:
                if kind == "add_task":
                    title = (a.get("title") or "").strip()
                    if not title:
                        done.append("⚠️ one item had no name — what was it?")
                        continue
                    due = _date(a.get("date"))
                    have = self.tasks.find(title)
                    if have is not None:
                        # Already on the list. Keep the one he has, but never
                        # lose what he just told us about it.
                        extra = ""
                        if due and have["due"] != due.isoformat():
                            if self.tasks.set_due(have["id"], due):
                                extra = f", deadline set to {due:%a %d %b}"
                        done.append(f"already had #{have['id']} {have['title']}{extra}")
                        continue
                    tid = self.tasks.add(title, now, due=due)
                    done.append(f"new task #{tid}: {title}"
                                + (f" (due {due:%d %b})" if due else ""))
                elif kind in ("complete_task", "set_due", "add_note", "rename_task",
                              "drop_task"):
                    who = (a.get("task") or "").strip()
                    t = self.tasks.find(who)
                    if t is None and kind in ("complete_task", "drop_task"):
                        gone = self.tasks.find(who, status="any")
                        if gone is not None and gone["status"] != "open":
                            done.append(f"{gone['title']} was already closed")
                            continue
                    if t is None:
                        done.append(f"⚠️ couldn't find a task called \"{who}\" — which one?")
                        continue
                    if kind == "rename_task":
                        new = (a.get("title") or a.get("text") or "").strip()
                        twin = next((r for r in self.tasks.open() if r["id"] != t["id"]
                                     and r["title"].strip().lower() == new.lower()), None)
                        if twin is not None:
                            # Renaming onto one he already has: that's one task, not two.
                            self.tasks.close(t["id"], now, status="dropped")
                            done.append(f"merged #{t['id']} {t['title']} into "
                                        f"#{twin['id']} {twin['title']}")
                        elif self.tasks.rename(t["id"], new):
                            done.append(f"renamed #{t['id']}: {t['title']} → {new}")
                        else:
                            done.append(f"⚠️ what should #{t['id']} {t['title']} be called?")
                        continue
                    if kind == "drop_task":
                        self.tasks.close(t["id"], now, status="dropped")
                        done.append(f"removed: {t['title']}")
                        continue
                    if kind == "complete_task":
                        if self.tasks.close(t["id"], now):
                            done.append(f"done: {t['title']} ✅")
                        else:
                            done.append(f"{t['title']} was already closed")
                    elif kind == "set_due":
                        due = _date(a.get("date"))
                        if due is None:
                            done.append(f"⚠️ couldn't read the date for {t['title']} — when is it?")
                        elif due < now.date():
                            done.append(f"⚠️ {due:%d %b} is in the past, so I left "
                                        f"{t['title']} alone")
                        elif self.tasks.set_due(t["id"], due):
                            done.append(f"{t['title']} due {due:%a %d %b}")
                    else:
                        note = (a.get("text") or "").strip()
                        if note and self.tasks.note(t["id"], note):
                            done.append(f"noted on {t['title']}")
                        else:
                            done.append(f"⚠️ nothing to note on {t['title']}")
                elif kind == "tick_basic":
                    night = self.foundations.night_of(now)
                    row = self.foundations.find(night, a.get("text") or a.get("title") or "")
                    if row is None:
                        done.append("⚠️ which of tonight's basics did you mean?")
                    elif self.foundations.tick(night, row["idx"], now):
                        done.append(f"ticked: {row['text']} ✅")
                    else:
                        done.append(f"{row['text']} was already ticked")
                elif kind == "define":
                    term = (a.get("title") or a.get("task") or "").strip()
                    if self.terms.set(term, a.get("text") or "", now):
                        done.append(f"got it: {term.lower()} = {(a.get('text') or '').strip()}")
                    else:
                        done.append(f"⚠️ couldn't save what \"{term}\" means")
                elif kind == "add_event":
                    what = (a.get("text") or a.get("title") or "").strip()
                    d, hm = _date(a.get("date")), (a.get("time") or "").strip()
                    if not what:
                        done.append("⚠️ an event came through with no name")
                        continue
                    if not d or ":" not in hm:
                        done.append(f"⚠️ didn't catch when \"{what}\" is — date and time?")
                        continue
                    hh, mi = (int(x) for x in hm.split(":")[:2])
                    when = datetime.combine(d, datetime.min.time(),
                                            tzinfo=now.tzinfo).replace(hour=hh, minute=mi)
                    if when <= now:
                        done.append(f"⚠️ \"{what}\" reads as {when:%d %b %H:%M}, already past "
                                    "— when is it really?")
                        continue
                    if self.db.one("SELECT 1 FROM reminders WHERE at_ts = ? AND text = ?",
                                   (when.isoformat(), what)):
                        done.append(f"already had {what} — {when:%a %d %b %H:%M}")
                        continue
                    self.db.run("INSERT INTO reminders (at_ts, text, created_at)"
                                " VALUES (?, ?, ?)", (when.isoformat(), what, now.isoformat()))
                    done.append(f"event saved: {what} — {when:%a %d %b %H:%M}")
                elif kind == "remember":
                    what = (a.get("text") or "").strip()
                    if not what:
                        continue
                    if self.db.one("SELECT 1 FROM memories WHERE active=1 AND lower(text)=lower(?)",
                                   (what,)):
                        continue
                    self.db.run("INSERT INTO memories (text, created_at) VALUES (?, ?)",
                                (what[:300], now.isoformat()))
                elif kind == "forget":
                    ref = (a.get("task") or a.get("text") or "").strip().lower().lstrip("m#")
                    if ref.isdigit():
                        self.db.run("UPDATE memories SET active=0 WHERE id=?", (int(ref),))
                elif kind not in ("edit_plan", "recheck_photo", "start_latenight",
                                  "make_plan", "on_paper", "lock_plan", "show_list"):
                    done.append(f"⚠️ didn't know what to do with \"{kind}\"")
            except (ValueError, TypeError) as exc:
                log.warning("coach action %s failed: %s", kind, exc)
                done.append(f"⚠️ couldn't save one of those ({kind}) — say it again?")
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
        self.last_judge_was_reason = bool(data.get("is_reason", True))
        return bool(data.get("accept")), str(data.get("reply") or "").strip()

    # -- speaking first -------------------------------------------------------
    async def checkin(self, block, now: datetime) -> str:
        """A friend texting halfway through a block: how's it going."""
        return await self._ask(
            SYSTEM.format(name=self.cfg.user_name, context=self.context(now)),
            [CHECKIN.format(label=block["label"])], now, max_tokens=120, tier="cheap")

    async def say(self, job: str, facts: str, now: datetime,
                  max_tokens: int = 120) -> str:
        """One short message, in his voice, carrying these facts.

        The bot used to send fixed strings for this -- "your phone hasn't
        checked in for 1065 min, press Start on both flows" -- and they read
        like a machine because they were one. The facts are the same either
        way; who says them is the difference. Every caller keeps a written
        fallback for when there is no allowance left or the model is down.
        """
        return await self._ask(
            SYSTEM.format(name=self.cfg.user_name, context=self.context(now)),
            [SAY.format(job=job, facts=facts)], now, max_tokens=max_tokens,
            tier="cheap")

    async def brief(self, now: datetime) -> str:
        """Before the window opens: what's on his plate, in a couple of lines."""
        return await self._ask(
            SYSTEM.format(name=self.cfg.user_name, context=self.context(now)),
            [BRIEF], now, max_tokens=220, tier="cheap")


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
- {"type":"complete_task","task":"#id"}  he finished, submitted, handed in or
  is done with one. When he names several ("submitted the IA and DS IA, рнкп
  done"), one action EACH. Use his shorthand list above to map words to tasks.
- {"type":"rename_task","task":"#id","title":"new name"}  he wants a task
  reworded or changed ("change rec to get 2 rec letters"). NEVER add_task for
  this -- that makes a duplicate.
- {"type":"drop_task","task":"#id"}  he wants one off the list without doing it.
- {"type":"set_due","task":"#id","date":"YYYY-MM-DD"}  its deadline
- {"type":"add_note","task":"#id","text":...}  progress he described
  For "task", write the #id number from the list above ("#12"), nothing else.
- {"type":"show_list","text":"tasks" / "events" / "all"}  whenever he asks to
  see his to-do list, tasks, events, deadlines or "everything". The app
  appends the real list from the database. Your reply is ONE short line
  ("here's everything:") -- never write the list yourself.
- {"type":"remember","text":"one plain fact"}  anything lasting he tells you
  that is not a task or an event: an exam date, what he submitted, a
  preference, a person, how his school works. Short, in your words, dated
  facts with the date. Don't remember what's already a task or event.
- {"type":"forget","text":"m12"}  a memory above turned out wrong or old.
- {"type":"start_latenight","text":"the reason, in his words"}  when it's past
  bedtime (BEDTIME below) and he says he has a real deadline or is out with
  friends. The app then asks him for a photo showing it; that buys 2 hours,
  once a week. Don't promise it's granted -- the photo decides.
- {"type":"tick_basic","text":"socks" / "clothes" / "mouth"}  when he says he
  did one of tonight's basics (washed socks, laid out clothes, brushed teeth).
- {"type":"define","title":"рнкп","text":"what it means"}  when he explains one of
  his own abbreviations or what a block name really is. The photo checker uses
  these, so it stops rejecting good photos over a word it doesn't know.
- {"type":"recheck_photo","text":"his explanation"}  ONLY if the notes below say
  his last photo was rejected and he's explaining what it shows or what the
  task means. The app re-checks the same photo with his explanation. You don't
  decide -- never say it counts. Use define too if he explained a shorthand.
- {"type":"on_paper","text":"which block, or empty for the one he's in"}  when
  he says a block is on paper, in a notebook, by hand, or away from his
  computer and the app is demanding a blocker screenshot for it. The proof
  becomes a photo of him working instead.
- {"type":"make_plan","day":"today"/"tomorrow"/"YYYY-MM-DD","days":1,"text":"..."}
  when he asks YOU to build the plan ("plan my day", "plan the next 2 days",
  "rest plan yourself"). Put whatever he fixed in "text", word for word, and
  the app builds the rest around his deadlines. Use days:2 or 3 if he asked
  for more than one day. Don't write the blocks yourself in this case.
  If a PLAN IS WAITING (see the notes below) and he wants something in it
  changed ("swap sat and ia", "no math tonight", "dinner at 8"), use make_plan
  for that day with "text" = exactly the change he asked for. The app edits
  the waiting plan; it doesn't start over.
- {"type":"lock_plan"}  ONLY when a PLAN IS WAITING and he says it's good /
  lock it / go / ok. The app locks exactly the plan he was shown.
- {"type":"edit_plan","day":"today" or "tomorrow","plan":"HH:MM-HH:MM label\n..."}
  when he asks you to change his schedule. Write the WHOLE new plan, one block
  per line: for today, from the block he's in now to the end of the window;
  for tomorrow, the full window. Copy every block he didn't ask to change
  exactly as it is -- same times, same label, character for character.
  NEVER drop one, and never shorten one, to make room. The new thing takes its
  time from the block it replaces, or everything after it moves later by
  exactly that much and the last block still ends where it did. If something
  genuinely has to go, say which one in your reply and why -- a block that
  vanishes without a word is the worst thing you can do to his day.
  A NEW block gets a plain label ("eat", "gym", "walk"). Only put "work:" in
  front when the new block is actually studying or working (it makes the app
  demand a blocker screenshot). Eating, rest, sport, commute: never "work:".
  Only do this when he actually asks for a change. Don't say it's done in your reply
  -- the app confirms it, and if the change cuts work time it will ask him why.
Work out dates like "thursday" from "Right now" above. Usually actions is [].
When he sends a LIST, emit ONE action per item -- every single line, in order,
including sub-items like "6.1". Never merge two items, never leave one out
because it looks similar to something he already has: the app decides that and
tells him. A dropped line is the worst thing you can do to him.
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
                    "edit_plan", "define", "recheck_photo", "tick_basic",
                    "start_latenight", "make_plan", "on_paper", "lock_plan",
                    "rename_task", "drop_task", "show_list", "remember", "forget"]},
                "days": {"type": "INTEGER"},
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
ALSO ACCEPT moving the time to OTHER REAL WORK that is more urgent: an
unfinished presentation, a competition, a project or assignment with a
deadline. That is reprioritising, not slacking -- look at what the new plan
puts in place of the cut work.
ALWAYS ACCEPT eating, and say so briefly. Food is not a break and not a
negotiation -- a normal meal never had to be argued for, and the app already
lets a short one through without asking you at all. If what he is asking for
is a meal, the only question worth having is whether it is hours long.
REJECT tiredness, not feeling like it, "I'll do it later", "I need a break",
vague or one-word answers, or anything that just renames work as rest.
If you reject, say why in one friendly line and what would count.
If you accept, keep it short, maybe tell him to make the time up.
If his message is NOT a reason at all -- a question ("pic of what?"),
confusion, or small talk -- set is_reason to false, accept to false, and in
reply answer him plainly and say again what you need: a reason for the change.
Answer as JSON: {{"is_reason": true or false, "accept": true or false,
"reply": text to send him}}."""

JUDGE_SCHEMA = {
    "type": "OBJECT",
    "properties": {"is_reason": {"type": "BOOLEAN"}, "accept": {"type": "BOOLEAN"},
                   "reply": {"type": "STRING"}},
    "required": ["is_reason", "accept", "reply"],
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
    """The JSON the model was asked for -- or, if it ignored that, its text.

    22 Sep 08:58 a reply was cut off mid-JSON and he got `{ "reply": ...` as
    the message. Broken JSON is never shown: the reply is dug out of it, and
    if even that fails he gets a plain line.
    """
    import json
    import re
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        text = (raw or "").strip()
        if not text.startswith("{") and '"reply"' not in text:
            return text, []
        m = re.search(r'"reply"\s*:\s*"((?:[^"\\]|\\.)*)', text, re.S)
        if m:
            try:
                said = json.loads('"' + m.group(1) + '"')
            except ValueError:
                said = m.group(1).replace('\\n', '\n').replace('\\"', '"')
            return said.strip(), []
        return "sorry bro, my answer got cut off — say that again?", []
    if not isinstance(data, dict):
        return str(data), []
    acts = data.get("actions") or []
    return str(data.get("reply") or "").strip(), acts if isinstance(acts, list) else []


STANDUP_NOTE = """NOTE: this message is his answer to the 20-minute check for the
block "{label}", asked at {at}. The three questions were: what exactly he's
doing, whether he's moving fast, and what he's stuck on.

Judge the answer like a friend who can tell the difference:
- If it's vague ("math", "working", "fine", "nothing") -- push back ONCE and
  ask for the precise thing: which problem, which page, which slide. Don't
  lecture, one line.
- If he named something specific, say something short about it and leave him
  alone. Don't ask more questions; he's working.
- If he said he's stuck or drifting, ask for the smallest next move -- or name
  one yourself if it's obvious -- in one sentence.
- If he's been saying the same thing for two or three checks in a row, say so
  plainly ("that's the third check on the same paragraph bro").
Keep it to one or two sentences either way."""


SAY = """Text him now about this. You are starting the conversation, he has not
just written to you.

WHAT THIS IS ABOUT: {job}

THE FACTS -- all true, use what matters and drop the rest:
{facts}

One or two short lines, the way you actually text. No lists, no headings, no
sign-off. Keep every number and name that matters (the block, the minutes, the
app to open) -- he has to know what to do. Don't invent anything that isn't in
the facts, and don't promise to change anything."""
