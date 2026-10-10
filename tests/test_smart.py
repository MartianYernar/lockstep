"""21 Sep, 16:07-16:20, replayed: the afternoon Friday was "really stupid".

What happened, from the database:
- 16:07 he said the IA deadline was extended to 8pm. Friday answered "physics
  ia is already a day late" -- and in the same breath saved the new deadline.
- 16:10-16:19 seven proposals in nine minutes. Every "change this" was sent to
  the planner as if he had typed the whole old plan, under a rule that says
  keep what he typed, so it could not change anything properly.
- 16:15 he tapped lock, was asked for goals, sent them at 16:16 -- and the plan
  he had locked never went in.
- 16:17 "bro u locked plan, give me it" got "send the pic for physics ia
  first": there was no plan, so there was no pic to send.
- The first proposal had a "Lingua IELTS lesson" in it, copied from an old day.
- "sat prep" came back as "work: sat prep", which demands a blocker screenshot.

The model is stubbed. What is pinned is everything around it: what it is
shown, what it is told outranks what, and what the app does with its answer.
"""
import asyncio
import copy
import json
import os
import sys
import types
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fixtures import build, check, report, RAW
from test_friend_fakes import FakeUpdate, FakeBot, StubValidator
from server.bot import LockstepBot
from server import planner

TZ = ZoneInfo("Asia/Almaty")
DAY = date(2026, 9, 21)                         # a Monday
run = asyncio.get_event_loop().run_until_complete

SEED = [
    {"title": "Physics IA", "keywords": ["physics ia", "ia"], "due": "2026-09-20",
     "hours": 8, "priority": 1},
    {"title": "SAT prep", "keywords": ["sat"], "due": "2026-10-03", "hours": 30,
     "priority": 1},
    {"title": "Math worksheets", "keywords": ["math"], "hours": 4, "priority": 2},
]

PROPOSAL = {"blocks": [
    {"start": "16:00", "end": "17:30", "label": "work: Physics IA"},
    {"start": "17:30", "end": "19:30", "label": "work: sat prep"},
    {"start": "19:30", "end": "20:00", "label": "rest and dinner"},
    {"start": "20:00", "end": "22:00", "label": "Math worksheets"},
], "note": "IA first, it's due tonight"}

SWAPPED = {"blocks": [
    {"start": "16:00", "end": "18:00", "label": "sat prep"},
    {"start": "18:00", "end": "19:30", "label": "Physics IA"},
    {"start": "19:30", "end": "20:00", "label": "rest and dinner"},
    {"start": "20:00", "end": "22:00", "label": "Math worksheets"},
], "note": "moved SAT to the top"}


def N(h, m):
    return datetime(2026, 9, 21, h, m, tzinfo=TZ)


def make(now=N(16, 10), goals=True):
    raw = copy.deepcopy(RAW)
    raw["tasks"] = {"seed": SEED}
    raw["coach"] = {"enabled": True, "max_calls_per_day": 150, "say_reserve": 0}
    raw["standup"] = {"enabled": False}
    raw["schedule"] = dict(raw["schedule"], mode="block_start", grace_min=3)
    raw["proof"] = {"within_block_min": 20}
    cfg, db, led, eng, lk = build(raw, now=now)
    bot = LockstepBot(cfg, db, led, eng, lk, StubValidator())
    bot.app = types.SimpleNamespace(bot=FakeBot())
    db.set_lock(enabled=1, commitment_end="2099-01-01")
    db.ensure_day(DAY)
    if goals:
        db.set_goals(DAY, ["IA done", "sat 2h", "math"])
    return cfg, db, led, eng, bot


def model(bot, friday=(), planner_out=()):
    """Two queues: what Friday says, what the planner builds. Last one repeats."""
    seen = {"friday": [], "planner": []}
    fq, pq = list(friday), list(planner_out)

    async def fake(system, contents, now, max_tokens=400, schema=None, tier=None):
        bot.coach._charge(now.date())
        text = "\n".join(str(c) for c in contents)
        if system.startswith("You build one day's timetable"):
            seen["planner"].append(text)
            a = pq.pop(0) if len(pq) > 1 else pq[0]
        else:
            seen["friday"].append(system)
            a = fq.pop(0) if len(fq) > 1 else fq[0]
        return a if isinstance(a, str) else json.dumps(a)
    bot.coach._ask = fake
    return seen


def say(reply, *actions):
    return {"reply": reply, "actions": list(actions)}


def labels(db):
    return [b["label"] for b in db.blocks(DAY)]


class Q:
    def __init__(self, data, chat=1):
        self.data, self.edited = data, None
        self.message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=chat, send_action=lambda *a, **k: asyncio.sleep(0)),
            reply_text=self._reply)
        self.replies = []
    async def answer(self): pass
    async def edit_message_text(self, t, **kw): self.edited = t
    async def _reply(self, t, **kw): self.replies.append(t)


def tap(bot, data):
    q = Q(data)
    run(bot.on_button(types.SimpleNamespace(callback_query=q), None))
    return q


def offer(bot, cfg):
    """He asks Friday to build today; one proposal comes back."""
    u = FakeUpdate("bro can u build a plan yourself based on my tasks and events")
    run(bot.on_text(u, None))
    return u


print("=== 16:07: his news outranks the database ===")
cfg, db, led, eng, bot = make(N(16, 7))
ctx = bot.coach.context(N(16, 7))
check("the old deadline is shown with its date, so news can replace it",
      "OVERDUE (was due Sun 20 Sep)" in ctx, True)
from server.coach import SYSTEM  # noqa: E402
check("Friday is told his news beats the database", "His NEWS beats the database" in SYSTEM, True)
check("and never to answer news with the old value", "already a" in SYSTEM, True)
check("but proof still comes from the record", "0 proven hours" in SYSTEM, True)
check("and to answer what he asked before nagging", "answer his actual message" in SYSTEM, True)
seen = model(bot, friday=[say("bet, IA till 8 then sat",
                              {"type": "set_due", "task": "Physics IA", "date": "2026-09-21"})])
run(bot.on_text(FakeUpdate("Yo first I need to knock off physics IA, the deadline was "
                           "extended to 8pm"), None))
check("the new deadline is saved", bot.tasks.find("physics ia")["due"], "2026-09-21")
check("and from then on it reads as due today",
      "DUE TODAY" in bot.coach.context(N(16, 8)), True)
check("the planner puts it first, and says why",
      bot.agenda.for_planner(DAY).splitlines()[0].count("Physics IA") == 1
      and "DUE TODAY" in bot.agenda.for_planner(DAY).splitlines()[0], True)

print("\n=== 16:17: no plan means the siren wants a plan, not a photo ===")
cfg, db, led, eng, bot = make(N(16, 17))
ctx = bot.coach.context(N(16, 17))
check("the context says what is really owed", "TODAY HAS NO LOCKED PLAN" in ctx, True)
check("and does not invent a photo", "owes a photo" in ctx, False)

print("\n=== the planner reads today's chat, not an old day ===")
cfg, db, led, eng, bot = make()
db.ensure_day(date(2026, 9, 14))
db.replace_blocks(date(2026, 9, 14), [b.as_tuple() for b in planner.parse(
    "16:00-17:00 Lingua IELTS lesson\n17:00-22:00 work: essay")])
bot.coach.remember(DAY, N(16, 7), "user", "the IA deadline was extended to 8pm")
seen = model(bot, friday=[say("on it", {"type": "make_plan", "day": "today", "text": ""})],
             planner_out=[PROPOSAL])
u = offer(bot, cfg)
check("a plan was offered", "lock it in?" in u.message.last, True)
check("the planner saw what he told Friday today",
      "extended to 8pm" in seen["planner"][0], True)
check("and did not see an old day to copy a lesson from",
      "Lingua" in seen["planner"][0], False)
check("the work: it added itself is gone",
      "work: sat prep" in u.message.last or "work: Physics IA" in u.message.last, False)
check("the proposal is on the record for Friday",
      db.one("SELECT COUNT(*) c FROM coach_log WHERE text LIKE '[proposed a plan%'")["c"], 1)

print("\n=== a change edits the plan he was shown ===")
seen["planner"].clear()
seen = model(bot, friday=[say("say less", {"type": "make_plan", "day": "today",
                                           "text": "put sat prep first"})],
             planner_out=[SWAPPED])
first = bot.pending[1]["id"]
u = FakeUpdate("nah put sat first")
run(bot.on_text(u, None))
check("Friday saw the waiting plan", "PLAN IS WAITING" in seen["friday"][0], True)
check("and the exact blocks in it", "16:00-17:30 Physics IA" in seen["friday"][0], True)
p = seen["planner"][0]
check("the planner was asked to edit, not rebuild", "THE PLAN HE WAS SHOWN" in p, True)
check("with the plan as it was shown", "17:30-19:30 sat prep" in p, True)
check("and the change on its own", "put sat prep first" in p.split("THE CHANGE HE WANTS")[1], True)
check("a new offer", bot.pending[1]["id"] != first, True)
check("showing the change", "16:00-18:00  sat prep" in u.message.last, True)
check("still waiting for his yes", labels(db), [])

print("\n=== a question about it is answered, not rebuilt ===")
cfg, db, led, eng, bot = make()
seen = model(bot, friday=[say("go", {"type": "make_plan", "day": "today", "text": ""})],
             planner_out=[PROPOSAL])
offer(bot, cfg)
seen = model(bot, friday=[say("bc the IA's due at 8, sat can wait till after")],
             planner_out=[SWAPPED])
u = FakeUpdate("why ia first?")
run(bot.on_text(u, None))
check("he gets an answer", u.message.last, "bc the IA's due at 8, sat can wait till after")
check("nothing was rebuilt", seen["planner"], [])
check("the plan is still waiting", bot.pending[1]["action"], "plan_proposal")

print("\n=== yes in words locks it ===")
u = FakeUpdate("ok")
seen = model(bot, friday=[say("x")], planner_out=[SWAPPED])
run(bot.on_text(u, None))
check("a plain ok locks exactly what he saw", labels(db)[0], "Physics IA")
check("without asking the model", seen["friday"] + seen["planner"], [])

cfg, db, led, eng, bot = make()
model(bot, friday=[say("go", {"type": "make_plan", "day": "today", "text": ""})],
      planner_out=[PROPOSAL])
offer(bot, cfg)
model(bot, friday=[say("locked, go get it", {"type": "lock_plan"})], planner_out=[SWAPPED])
u = FakeUpdate("looks good bro send it")
run(bot.on_text(u, None))
check("Friday can lock it when he says so his own way", labels(db)[0], "Physics IA")
check("and he's told", any("locked in" in r.lower() for r in u.message.replies), True)
check("with no \"didn't know what to do\" noise", any("know what to do" in r for r in u.message.replies), False)

cfg, db, led, eng, bot = make()
model(bot, friday=[say("go", {"type": "make_plan", "day": "today", "text": ""})],
      planner_out=[PROPOSAL])
offer(bot, cfg)
bot.pending.clear()
model(bot, friday=[say("sure", {"type": "lock_plan"})], planner_out=[SWAPPED])
run(bot.on_text(FakeUpdate("lock whatever"), None))
check("lock_plan with nothing waiting locks nothing", labels(db), [])

print("\n=== 16:15: the lock survives the goals question ===")
cfg, db, led, eng, bot = make(goals=False)
model(bot, friday=[say("go", {"type": "make_plan", "day": "today", "text": ""})],
      planner_out=[PROPOSAL])
offer(bot, cfg)
pid = bot.pending[1]["id"]
q = tap(bot, f"plan:ok:{pid}")
check("he's asked for goals", any("goals first" in r for r in q.replies), True)
check("and told the plan follows", any("locks in right after" in r for r in q.replies), True)
check("nothing written yet", labels(db), [])
u = FakeUpdate("IA done\nsat 2h\nmath worksheets")
run(bot.on_text(u, None))
check("the goals are in", len(db.goals(DAY)), 3)
check("and so is the plan he locked", labels(db)[:2], ["Physics IA", "sat prep"])
check("he's told", "locked in" in u.message.last.lower(), True)

print("\n=== a plan made at 13:21 starts at 13:21 ===")
# 10 Oct 2026, 13:21, a Saturday: he said "I'm eating for 20 min now" and the
# plan came back with the eating block at 10:00-10:20, because the weekend
# window opens at 10:00 and the day was always built from there. Three and a
# half hours of it were already over.
LATE = N(18, 21)                      # mid-window, like his 13:21 on a Saturday
cfg, db, led, eng, bot = make(LATE)
check("the window still opens when it opens", cfg.enforce_start_for(DAY), 16 * 60)
check("but a plan written now starts now",
      cfg.plan_start_for(DAY, LATE), 18 * 60 + 21)
check("tomorrow is untouched",
      cfg.plan_start_for(DAY + timedelta(days=1), LATE), 16 * 60)
check("and one written before the window still starts at the window",
      cfg.plan_start_for(DAY, N(8, 0)), 16 * 60)

seen = model(bot, planner_out=[{"blocks": [
    {"start": "18:21", "end": "18:41", "label": "eat"},
    {"start": "18:41", "end": "22:00", "label": "prep for IELTS"}],
    "note": "food first"}], friday=[say("ok")])
blocks, note = run(bot.dayplanner.build(DAY, "imma eat for 20 min now", LATE))
check("the planner is told the day starts at 18:21", "18:21" in seen["planner"][0], True)
check("and that the hours before it are gone",
      "never write a block for them" in seen["planner"][0], True)
check("so the food block is now, not this morning", blocks[0].hhmm(), "18:21-18:41")
check("and it still runs to the end of the window",
      blocks[-1].hhmm().endswith("22:00"), True)

u = FakeUpdate("/plan\n13:21-13:41 eat\n13:41-22:00 prep for IELTS")
run(bot.cmd_plan(u, None))
check("a plan that skips the morning is not called full of holes",
      "no block" in u.message.last, False)
check("it just locks", [b["label"] for b in db.blocks(DAY)][0], "eat")

print("\n=== redo keeps the plan to edit ===")
cfg, db, led, eng, bot = make()
model(bot, friday=[say("go", {"type": "make_plan", "day": "today", "text": ""})],
      planner_out=[PROPOSAL])
offer(bot, cfg)
q = tap(bot, f"plan:redo:{bot.pending[1]['id']}")
check("he's asked what to change", "different" in q.edited, True)
seen = model(bot, friday=[say("bet", {"type": "make_plan", "day": "today",
                                      "text": "sat first"})], planner_out=[SWAPPED])
run(bot.on_text(FakeUpdate("sat first"), None))
check("his answer edits that plan", "17:30-19:30 sat prep" in seen["planner"][0], True)
check("and a new one is offered", bot.pending[1]["text"].startswith("16:00-18:00 sat prep"), True)

sys.exit(report("SMART TESTS"))
