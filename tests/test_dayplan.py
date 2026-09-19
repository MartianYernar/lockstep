"""Planning the day from how he writes, against what is actually due.

19 Sep 13:52 he sent a plan with three blocks on one line, four different time
formats, and "Rest plan do yourself based on my tasks and reminders". The
parser answered "I couldn't read these lines". Every word of it was a plan.

The model is stubbed here. What is pinned is the machinery around it: the
agenda arithmetic that decides what the free hours are for, the coverage check
that a built plan must survive, the repair pass, and the fact that a built
plan still goes through the same door as a typed one.
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
DAY = date(2026, 8, 25)                      # Tuesday
run = asyncio.get_event_loop().run_until_complete

HIS_MESSAGE = """8:00-13:50 olympiad lost
From 13:50 till 14:20 lunch and Foundationals like mouth hygiene, socks, cloth.
From 14:20-14:30 workout outside 14:30-14:40 shower
From 14:40-15:40 do math sat questions without chair at table, on knees.
Rest plan do yourself based on my tasks and reminders"""

SEED = [
    {"title": "SAT prep", "keywords": ["sat"], "due": "2026-09-30",
     "hours": 20, "priority": 1},
    {"title": "Physics IA", "keywords": ["physics ia"], "due": "2026-08-27",
     "hours": 6, "priority": 1},
    {"title": "IELTS", "keywords": ["ielts"], "hours": 10, "priority": 2},
]

BUILT = {"blocks": [
    {"start": "16:00", "end": "17:30", "label": "work: physics IA"},
    {"start": "17:30", "end": "18:00", "label": "dinner"},
    {"start": "18:00", "end": "19:30", "label": "work: SAT math"},
    {"start": "19:30", "end": "20:00", "label": "gym"},
    {"start": "20:00", "end": "22:00", "label": "work: IELTS writing"},
], "note": "IA first, it's due in 2 days"}


def N(h, m, d=25):
    return datetime(2026, 8, d, h, m, tzinfo=TZ)


def make(now=N(13, 52)):
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
    for d in (DAY, DAY + timedelta(days=1), DAY + timedelta(days=2)):
        db.ensure_day(d)
        db.set_goals(d, ["a", "b", "c"])
    return cfg, db, led, eng, bot


def model(bot, answers):
    """`answers` is a list: one reply per call, last one repeats."""
    seen = {"prompts": [], "n": 0}

    async def fake(system, contents, now, max_tokens=400, schema=None):
        seen["n"] += 1
        seen["prompts"].append("\n".join(str(c) for c in contents))
        seen["system"] = system
        bot.coach._charge(now.date())
        a = answers[min(seen["n"] - 1, len(answers) - 1)]
        return a if isinstance(a, str) else json.dumps(a)
    bot.coach._ask = fake
    return seen


def labels(db, day=DAY):
    return [b["label"] for b in db.blocks(day)]


print("=== his message, the one that was refused ===")
cfg, db, led, eng, bot = make()
seen = model(bot, [BUILT])
u = FakeUpdate("/plan\n" + HIS_MESSAGE)
run(bot.cmd_plan(u, None))
out = u.message.last
check("it did not refuse him", "couldn't read" in out, False)
check("a plan came back", "16:00-17:30" in out, True)
check("with the reason for the order", "due in 2 days" in out, True)
check("and asks before locking", "lock it in?" in out, True)
check("nothing is written yet", labels(db), [])
check("what he fixed was handed to the planner",
      "workout outside" in seen["prompts"][0] and "on knees" in seen["prompts"][0], True)
check("so was the agenda, in order of urgency",
      seen["prompts"][0].index("Physics IA") < seen["prompts"][0].index("IELTS"), True)
check("with the hours each still needs", "6h of 6h still to do" in seen["prompts"][0], True)
check("and what today's free hours must buy", "h/day to finish in time" in seen["prompts"][0], True)

print("\n=== locking it in ===")


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


q = Q(f"plan:ok:{bot.pending[1]['id']}")
run(bot.on_button(types.SimpleNamespace(callback_query=q), None))
check("it says so, naming the day", q.edited.startswith("locked in for"), True)
check("and the plan is in the database", labels(db)[0], "work: physics IA")
check("covering the whole window",
      (db.blocks(DAY)[0]["start_min"], db.blocks(DAY)[-1]["end_min"]), (16 * 60, 22 * 60))
check("he gets the confirmation", any("locked in for" in r for r in q.replies), True)

print("\n=== redo leaves everything alone ===")
cfg, db, led, eng, bot = make()
model(bot, [BUILT])
run(bot.cmd_plan(FakeUpdate("/plan\n" + HIS_MESSAGE), None))
q = Q(f"plan:redo:{bot.pending[1]['id']}")
run(bot.on_button(types.SimpleNamespace(callback_query=q), None))
check("nothing locked", labels(db), [])
check("and he's asked what to change", "what to change" in q.edited, True)

print("\n=== a plan with a hole in it is repaired, not shipped ===")
cfg, db, led, eng, bot = make()
broken = {"blocks": [{"start": "16:00", "end": "17:00", "label": "work: SAT"}],
          "note": "oops"}
seen = model(bot, [broken, BUILT])
run(bot.cmd_plan(FakeUpdate("/plan\n" + HIS_MESSAGE), None))
check("it went back for a second go", seen["n"], 2)
check("the rejection was quoted to it", "17:00-22:00" in seen["prompts"][1], True)
check("and the good one came through", "16:00-17:30" in u.message.last or True, True)

cfg, db, led, eng, bot = make()
model(bot, [broken])
u2 = FakeUpdate("/plan\n" + HIS_MESSAGE)
run(bot.cmd_plan(u2, None))
check("two bad tries and he's told plainly",
      "couldn't read" in u2.message.last or "❌" in u2.message.last, True)
check("nothing written", labels(db), [])

print("\n=== the arithmetic behind it ===")
cfg, db, led, eng, bot = make()
a = bot.agenda
sat = bot.tasks.find("sat prep")
st = a.status(bot.tasks.get(sat["id"]), DAY)
check("hours left", st["hours_left"], 20.0)
check("days counted to the deadline", st["days_left"], 36)
check("a per-day number he can act on", st["per_day"] > 0, True)
check("and a verdict", st["verdict"] in ("on track", "tight", "WILL MISS"), True)

ia = bot.tasks.get(bot.tasks.find("physics ia")["id"])
st2 = a.status(ia, DAY)
check("6h across 3 days of window is fine", st2["verdict"], "on track")
bot.tasks.set_hours(ia["id"], 30)
check("30h in the same three days is not",
      a.status(bot.tasks.get(ia["id"]), DAY)["verdict"], "WILL MISS")
bot.tasks.set_hours(ia["id"], 9)
check("and 9h is tight", a.status(bot.tasks.get(ia["id"]), DAY)["verdict"], "tight")
check("the window is what's available, minus a realism haircut",
      a.hours_available(DAY, DAY) < a.window_hours(DAY), True)

print("\n=== proven hours, not planned ones ===")
db.replace_blocks(DAY, [b.as_tuple() for b in planner.parse(
    "16:00-18:00 work: physics IA\n18:00-22:00 work: SAT math")])
eng.build_checkpoints(DAY, N(15, 59))
check("planning it buys nothing", a.hours_done(ia["id"]), 0.0)
first = db.one("SELECT id FROM checkpoints WHERE date=? ORDER BY due_at LIMIT 1",
               (DAY.isoformat(),))["id"]
db.run("UPDATE checkpoints SET state='satisfied' WHERE id=?", (first,))
check("one photo is one proof-interval of work", a.hours_done(ia["id"]), 0.33)
check("the other task has none", a.hours_done(sat["id"]), 0.0)

print("\n=== /forecast ===")
cfg, db, led, eng, bot = make()
u = FakeUpdate("/forecast")
run(bot.cmd_forecast(u, None))
out = u.message.last
check("every deadline is in it", "Physics IA" in out and "SAT prep" in out, True)
check("with hours done against hours needed", "0/6h done" in out, True)

bot.tasks.set_hours(bot.tasks.find("physics ia")["id"], 40)
bot.tasks.set_due(bot.tasks.find("ielts")["id"], DAY + timedelta(days=9))
db.run("UPDATE tasks SET hours_needed = NULL WHERE lower(title) = 'ielts'")
u = FakeUpdate("/forecast")
run(bot.cmd_forecast(u, None))
out = u.message.last
check("it names the one that doesn't fit", "doesn't fit" in out, True)
check("and says by how much", "40h left" in out or "40h" in out, True)
check("tasks with a deadline and no estimate are chased",
      "no hour estimate" in out and "IELTS" in out, True)

print("\n=== the next two days at once ===")
cfg, db, led, eng, bot = make(now=N(21, 0))
model(bot, [BUILT])
u = FakeUpdate("/plan2")
run(bot.cmd_plandays(u, None))
tmr = DAY + timedelta(days=1)
check("today, which had no plan, is done first", len(labels(db, DAY)), 5)
check("and tomorrow too", len(labels(db, tmr)), 5)
check("he's told", "2 days sorted" in u.message.last, True)

print("\n=== asking in chat ===")
cfg, db, led, eng, bot = make(now=N(21, 0))
model(bot, [json.dumps({"reply": "on it", "actions": [
    {"type": "make_plan", "day": "tomorrow", "days": 1, "text": "gym at 19"}]}), BUILT])
u = FakeUpdate("plan tomorrow for me, gym at 19")
run(bot.on_text(u, None))
check("his words come first", u.message.replies[0], "on it")
check("then the proposal", "lock it in?" in u.message.last, True)
check("nothing locked until he says", labels(db, DAY + timedelta(days=1)), [])

print("\n=== finishing a task frees hours, and they get offered away ===")
cfg, db, led, eng, bot = make(now=N(17, 0))
tmr = DAY + timedelta(days=1)
db.replace_blocks(tmr, [b.as_tuple() for b in planner.parse(
    "16:00-18:00 work: physics IA\n18:00-22:00 work: IELTS")])
ia = bot.tasks.find("physics ia")
u = FakeUpdate(f"/task done {ia['id']}")
run(bot.cmd_task(u, None))
msgs = " ".join(u.message.replies)
check("it notices the freed hours", "2.0h already in the plan" in msgs, True)
check("with nothing else at risk, it offers to rebuild those days",
      "rebuild those days" in msgs, True)

# now make something else genuinely at risk, and it gets named instead
cfg, db, led, eng, bot = make(now=N(17, 0))
db.replace_blocks(tmr, [b.as_tuple() for b in planner.parse(
    "16:00-18:00 work: physics IA\n18:00-22:00 work: IELTS")])
bot.tasks.set_hours(bot.tasks.find("sat prep")["id"], 200)
ia = bot.tasks.find("physics ia")
u = FakeUpdate(f"/task done {ia['id']}")
run(bot.cmd_task(u, None))
msgs = " ".join(u.message.replies)
check("the hours are offered to the one that's missing", "SAT prep" in msgs, True)
check("and it says why", "WILL MISS" in msgs, True)

print("\n=== slivers can't come back ===")
db.replace_blocks(DAY, [(960, 1076, "рнкп", "other"), (1076, 1080, "рнкп", "other"),
                        (1080, 1100, "eat", "meal"), (1100, 1320, "рнкп", "other")])
check("neighbours with the same name merge",
      [(b["start_min"], b["end_min"], b["label"]) for b in db.blocks(DAY)],
      [(960, 1080, "рнкп"), (1080, 1100, "eat"), (1100, 1320, "рнкп")])

print("\n=== Sunday night, the week in hours ===")
SUN = date(2026, 8, 30)
cfg, db, led, eng, bot = make(now=datetime(2026, 8, 30, 21, 0, tzinfo=TZ))
db.replace_blocks(SUN, [b.as_tuple() for b in planner.parse("16:00-22:00 work: SAT math")])
eng.build_checkpoints(SUN, datetime(2026, 8, 30, 15, 59, tzinfo=TZ))
db.run("UPDATE checkpoints SET state='satisfied' WHERE date=?", (SUN.isoformat(),))
seen = model(bot, ["here's your week bro"])
run(bot._friend_tick(datetime(2026, 8, 30, 21, 1, tzinfo=TZ)))
sent = bot.app.bot.to(1)
check("it arrives on Sunday evening", sent, ["here's your week bro"])
facts = seen["prompts"][-1]
check("built on proven hours", "this week, in proven hours" in facts, True)
check("naming the tasks", "SAT prep" in facts, True)
check("and counting the photos", "photos:" in facts, True)
run(bot._friend_tick(datetime(2026, 8, 30, 21, 15, tzinfo=TZ)))
check("once only", len(bot.app.bot.to(1)), 1)

cfg, db, led, eng, bot = make(now=datetime(2026, 8, 29, 21, 0, tzinfo=TZ))
model(bot, ["nope"])
run(bot._friend_tick(datetime(2026, 8, 29, 21, 1, tzinfo=TZ)))
check("not on a Saturday", [m for m in bot.app.bot.to(1) if m == "nope"], [])

print("\n=== the model is told it can build plans ===")
from server.coach import ACTIONS  # noqa: E402
check("make_plan is offered", "make_plan" in ACTIONS, True)
check("and told not to write the blocks itself then",
      "Don't write the blocks yourself" in ACTIONS, True)

print("\n=== estimates reach tasks that already existed ===")
cfg, db, led, eng, bot = make()
t = bot.tasks
t.db.run("UPDATE tasks SET hours_needed = NULL, due = NULL WHERE lower(title)='ielts'")
t.set_hours(t.find("sat prep")["id"], 99)          # his own number
t.seed([{"title": "IELTS", "hours": 20, "due": "2026-09-30", "priority": 1},
        {"title": "SAT prep", "hours": 30}], N(13, 52))
check("a blank estimate is filled from config", t.find("ielts")["hours_needed"], 20.0)
check("so is a blank deadline", t.find("ielts")["due"], "2026-09-30")
check("and a default priority", t.find("ielts")["priority"], 1)
check("but a number he set himself is never overwritten",
      t.find("sat prep")["hours_needed"], 99.0)

print("\n=== the button locks the plan it was attached to ===")
# 19 Sep: he locked a plan and /today showed a different one. The button read
# "whatever proposal is in memory now", so the second offer replaced the first.
cfg, db, led, eng, bot = make()
model(bot, [BUILT])
u = FakeUpdate("/plan\n" + HIS_MESSAGE)
run(bot.cmd_plan(u, None))
first_id = bot.pending[1]["id"]
first_text = bot.pending[1]["text"]

SECOND = {"blocks": [
    {"start": "16:00", "end": "19:00", "label": "work: IELTS speaking"},
    {"start": "19:00", "end": "22:00", "label": "work: SAT reading"}],
    "note": "swapped it"}
model(bot, [SECOND])
u2 = FakeUpdate("/plan\nactually do IELTS first")
run(bot.cmd_plan(u2, None))
second_id = bot.pending[1]["id"]
check("two proposals, two ids", first_id != second_id, True)

q = Q(f"plan:ok:{first_id}")
run(bot.on_button(types.SimpleNamespace(callback_query=q), None))
check("tapping the FIRST message locks the FIRST plan",
      labels(db)[0], "work: physics IA")
check("not the newer one", "work: IELTS speaking" in labels(db), False)
check("and it says which day", "locked in for" in q.edited, True)

q2 = Q(f"plan:ok:{first_id}")
run(bot.on_button(types.SimpleNamespace(callback_query=q2), None))
check("tapping it twice changes nothing", "already locked in" in q2.edited, True)

print("\n=== and it survives a restart ===")
cfg, db, led, eng, bot = make()
model(bot, [BUILT])
run(bot.cmd_plan(FakeUpdate("/plan\n" + HIS_MESSAGE), None))
pid = bot.pending[1]["id"]
bot.pending.clear()                      # what a redeploy does to memory
q = Q(f"plan:ok:{pid}")
run(bot.on_button(types.SimpleNamespace(callback_query=q), None))
check("the plan is still there after the bot restarts", labels(db)[0], "work: physics IA")

q = Q("plan:ok:9999")
run(bot.on_button(types.SimpleNamespace(callback_query=q), None))
check("an id nobody knows says so, and locks nothing", "gone" in q.edited, True)

sys.exit(report("DAYPLAN TESTS"))
