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

SYSTEM = """You are {name}'s mentor. Not a system, not an assistant -- a person
who knows him, is in his corner, and does not flatter him.

He is 17, final years at NIS in Kazakhstan, 92 days into a commitment he made
to himself. You are in Telegram.

HOW YOU TALK
- Short. Two or three sentences most of the time. One is often enough.
- Like a person texting, not a product writing copy. No headings, no bullet
  lists unless he asked for steps, no bold, no emoji.
- Never open with "Great question", "I understand", "Absolutely", or any
  variant. Start with the thing itself.
- Do not explain your reasoning about how to answer. Just answer.
- Warm, but you do not soften the truth to be liked. If he is about to do the
  easy thing instead of the hard thing, say it once, plainly, and stop. No
  lecture, no moral, no speech about discipline.
- No praise for anything he has not actually done.
- If he is vague, ask one specific question. One.

WHAT YOU CANNOT DO
You cannot mark anything done, excuse a miss, move a deadline, or stop the
siren. Only a photo does that. If he asks you to let him off, say you have no
such button and that the photo is the way out. Never imply you changed anything.

TODAY, from the database -- not from what he tells you:
{context}

If anything above says OVERDUE, lead with that in one line and keep the rest
very short. He can talk once the proof is in."""

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
        db.conn.executescript(SCHEMA)
        db.conn.commit()

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
        day = now.date()
        out: list[str] = []

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
                   max_tokens: int = 400) -> str:
        from google.genai import types
        if self._client is None:
            from google import genai
            self._client = genai.Client(api_key=self.cfg.raw["gemini"]["api_key"])

        cfg = types.GenerateContentConfig(
            system_instruction=system,
            temperature=float(self.conf.get("temperature", 0.7)),
            max_output_tokens=max_tokens,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        )
        last: Exception | None = None
        for model in self._models():
            try:
                resp = await self._client.aio.models.generate_content(
                    model=model, contents=contents, config=cfg)
                self._charge(now.date())
                return (resp.text or "").strip()
            except Exception as exc:                          # noqa: BLE001
                last = exc
                text = str(exc)
                # Out of allowance (429) or the model is simply busy (503).
                # Both are "ask a different one", and 503 is the common case on
                # the free tier -- the first smoke test against the real API hit
                # it, and without this the whole feature died on one busy model.
                # Vision survives 503 through its own retry loop; walking the
                # chain IS the coach's retry loop.
                if any(k in text for k in
                       ("RESOURCE_EXHAUSTED", "429", "UNAVAILABLE", "503")):
                    log.warning("coach: %s unavailable (%s), next model",
                                model, text.split(".")[0][:40])
                    continue
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

    async def reply(self, text: str, now: datetime) -> str:
        from google.genai import types
        day = now.date()
        contents = []
        for row in self.history(day):
            contents.append(types.Content(
                role="user" if row["role"] == "user" else "model",
                parts=[types.Part.from_text(text=row["text"])]))
        contents.append(types.Content(
            role="user", parts=[types.Part.from_text(text=text)]))

        out = await self._ask(
            SYSTEM.format(name=self.cfg.user_name, context=self.context(now)),
            contents, now, max_tokens=int(self.conf.get("max_reply_tokens", 400)))
        self.remember(day, now, "user", text)
        self.remember(day, now, "coach", out)
        return out
