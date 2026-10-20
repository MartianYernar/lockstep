"""20 Oct 2026, 21:33. He asked to redo the day and said he was running late.

What he got was a block reading "Submit math homework, KAIST uni essay, KAIST
fill out application, brag sheets" from 21:35 to 22:00, under the line
"Updated the block to cover all your remaining doc tasks and brag sheets
before 22:00." Twenty-five minutes, four tasks, and a sentence saying that was
fine.

Four separate faults sat behind it, and this file pins each one:

1. He said "imma run latenight" and Friday said "got you, running late night".
   Nothing happened: make_plan was dispatched before start_latenight and
   returned, so the extension was dropped and the plan was built against a day
   that still ended at 22:00.
2. Even granted, a late night only moved the bed photo. The planning window
   and the enforcement window still shut at 22:00 and 22:20, so the two hours
   he earned were hours the system would neither plan nor check.
3. With 25 minutes left it still called the model instead of saying so.
4. The model answered with four tasks in one block, which the prompt has
   forbidden since the first version.
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

SEED = [{"title": "KAIST application", "keywords": ["kaist"], "due": "2026-08-30",
         "hours": 6, "priority": 1},
        {"title": "math homework", "keywords": ["math"], "due": "2026-08-26",
         "hours": 2, "priority": 1}]

PLAN = ("16:00-19:00 work: KAIST essay\n19:00-19:30 dinner\n"
        "19:30-21:30 work: math homework\n21:30-22:00 clean room")


def N(h, m, d=25):
    return datetime(2026, 8, d, h, m, tzinfo=TZ)


def make(now=N(21, 33)):
    raw = copy.deepcopy(RAW)
    raw["tasks"] = {"seed": SEED}
    raw["coach"] = {"enabled": True, "max_calls_per_day": 150, "say_reserve": 0}
    raw["standup"] = {"enabled": False}
    raw["micro"] = {"enabled": False}
    raw["deliver"] = {"enabled": False}
    raw["schedule"] = dict(raw["schedule"], mode="block_start", grace_min=3)
    cfg, db, led, eng, lk = build(raw, now=now)
    bot = LockstepBot(cfg, db, led, eng, lk, StubValidator())
    bot.app = types.SimpleNamespace(bot=FakeBot())
    db.set_lock(enabled=1, commitment_end="2099-01-01")
    db.ensure_day(DAY)
    db.set_goals(DAY, ["a", "b", "c"])
    db.replace_blocks(DAY, [b.as_tuple() for b in planner.parse(PLAN)])
    db.run("UPDATE days SET plan_locked=1 WHERE date=?", (DAY.isoformat(),))
    return cfg, db, led, eng, bot


def model(bot, answers):
    seen = {"prompts": [], "n": 0, "system": ""}

    async def fake(system, contents, now, max_tokens=400, schema=None, tier=None):
        seen["n"] += 1
        seen["prompts"].append("\n".join(str(c) for c in contents))
        seen["system"] = system
        bot.coach._charge(now.date())
        a = answers[min(seen["n"] - 1, len(answers) - 1)]
        return a if isinstance(a, str) else json.dumps(a)
    bot.coach._ask = fake
    return seen


print("=== 25 minutes left is 25 minutes, not a plan ===")
cfg, db, led, eng, bot = make()
seen = model(bot, [{"blocks": [{"start": "21:35", "end": "22:00",
                                "label": "math homework, KAIST essay, KAIST "
                                         "application, brag sheets"}],
                    "note": "covers all your remaining tasks"}])
u = FakeUpdate("redo the plan")
bot.cfg.now = lambda: N(21, 35)
handled = run(bot._offer_built_plan(u, DAY, "redo the plan", N(21, 35)))
check("it handles the ask itself", handled, True)
check("without asking the model at all", seen["n"], 0)
check("it says how long is actually left", "25 min" in u.message.last, True)
check("and when the day stops", "22:00" in u.message.last, True)
check("it asks for one thing", "one thing" in u.message.last.lower(), True)
check("it names what is still open", "KAIST application" in u.message.last, True)
check("and points at the only way to get more tonight",
      "/latenight" in u.message.last, True)
check("nothing was proposed", bot.pending.get(1), None)

print("\n=== a longer window still gets a real plan ===")
cfg, db, led, eng, bot = make(now=N(19, 0))
seen = model(bot, [{"blocks": [{"start": "19:00", "end": "20:30",
                                "label": "work: KAIST essay"},
                               {"start": "20:30", "end": "22:00",
                                "label": "work: math homework"}],
                    "note": "essay first"}])
u = FakeUpdate("redo the plan")
bot.cfg.now = lambda: N(19, 0)
check("the model is asked",
      run(bot._offer_built_plan(u, DAY, "redo the plan", N(19, 0))), True)
check("and it is offered to him", "lock it in?" in u.message.last, True)

print("\n=== four tasks in one short block is refused ===")
cfg, db, led, eng, bot = make(now=N(19, 0))
crammed = {"blocks": [{"start": "19:00", "end": "19:30",
                       "label": "math homework, KAIST essay, KAIST application"},
                      {"start": "19:30", "end": "22:00", "label": "work: KAIST essay"}],
           "note": "all of it"}
good = {"blocks": [{"start": "19:00", "end": "22:00", "label": "work: KAIST essay"}],
        "note": "one thing, properly"}
seen = model(bot, [crammed, good])
u = FakeUpdate("redo the plan")
bot.cfg.now = lambda: N(19, 0)
run(bot._offer_built_plan(u, DAY, "redo the plan", N(19, 0)))
check("it went back for a second version", seen["n"], 2)
check("telling it what was wrong",
      "different tasks in 30 minutes" in seen["prompts"][1], True)
check("and his plan is the clean one", "19:00-22:00  KAIST essay" in u.message.last, True)

print("\n=== the late night happens before the plan, not after it ===")
cfg, db, led, eng, bot = make()
seen = model(bot, [json.dumps({
    "say": "ok -- pic first and you've got till midnight",
    "actions": [{"type": "make_plan", "day": "today", "text": "KAIST tasks"},
                {"type": "start_latenight",
                 "text": "KAIST application deadline tonight"}]})])
u = FakeUpdate("imma run latenight, redo plan, you have tasks from doc")
bot.cfg.now = lambda: N(21, 35)
run(bot.on_text(u, None))
pend = bot.pending.get(1) or {}
check("it asks for the late-night photo", pend.get("action"), "latenight_photo")
check("in his own words", "KAIST" in (pend.get("reason") or ""), True)
check("and the plan is held until the hours are real",
      bool(pend.get("then_plan")), True)
check("no plan was proposed yet", "lock it in?" in u.message.last, False)

print("\n=== once it is granted, the day is actually longer ===")
db.run("UPDATE days SET bed_offset_minutes=120 WHERE date=?", (DAY.isoformat(),))
check("the planner may run to midnight", eng.plan_end(DAY), 24 * 60)
check("25 minutes left is now 2h25", eng.plan_end(DAY) - (21 * 60 + 35), 145)
db.replace_blocks(DAY, [b.as_tuple() for b in planner.parse(
    PLAN + "\n22:00-24:00 work: KAIST application")])
check("and blocks in the bought hours are enforced, not clipped",
      eng.window(DAY)[1], 24 * 60)

cfg2, db2, led2, eng2, bot2 = make()
check("without the grant nothing moves", eng2.plan_end(DAY), 22 * 60)
db2.replace_blocks(DAY, [b.as_tuple() for b in planner.parse(
    PLAN + "\n22:00-23:30 work: KAIST application")])
check("and an ambitious late block is still cut off at bedtime",
      eng2.window(DAY)[1], 22 * 60 + 20)

print("\n=== and the planner is told the real end ===")
cfg, db, led, eng, bot = make(now=N(19, 0))
db.run("UPDATE days SET bed_offset_minutes=120 WHERE date=?", (DAY.isoformat(),))
seen = model(bot, [{"blocks": [{"start": "19:00", "end": "24:00",
                                "label": "work: KAIST essay"}],
                    "note": "the long evening you bought"}])
u = FakeUpdate("redo it")
bot.cfg.now = lambda: N(19, 0)
run(bot._offer_built_plan(u, DAY, "redo it", N(19, 0)))
check("the prompt plans to midnight", "24:00" in seen["prompts"][0], True)
check("and the plan he is shown does too", "24:00" in u.message.last
      or "00:00" in u.message.last, True)

print("\n=== Friday is told what it cannot move ===")
cfg, db, led, eng, bot = make()
ctx = bot.coach.context(N(21, 35))
check("the real end of tonight is in the context", "window ends 22:00" in ctx, True)
check("with the minutes left", "25 min left" in ctx, True)
check("and that only the late night moves it", "/latenight" in ctx, True)

print("\n=== 21:52: 'bro redo plan' is not his next ten-minute step ===")
cfg, db, led, eng, bot = make()
bot.cfg.raw["micro"] = {"enabled": True, "every_min": 10}
bot.steps.ask(N(21, 50), "clean room")
bot.cfg.now = lambda: N(21, 52)
check("the loop leaves a request alone",
      run(bot._handle_step_text(FakeUpdate("bro redo plan"), "bro redo plan", N(21, 52))),
      False)
check("and the question stays open", bot.steps.waiting(N(21, 52)) is not None, True)
check("a real step is still taken",
      run(bot._handle_step_text(FakeUpdate("wipe the desk"), "wipe the desk", N(21, 52))),
      True)
for s in ("redo the plan", "can u do it yourself", "remind me at 9", "plan for tomorrow"):
    check(f"'{s}' is for Friday", bot._to_me(s), True)
for s in ("plan the essay outline", "write intro paragraph", "solve q12-20",
          "fill activities section"):
    check(f"'{s}' is a step", bot._to_me(s), False)

print("\n=== nor is it his target for the block ===")
cfg, db, led, eng, bot = make(now=N(19, 0))
bot.cfg.raw["deliver"] = {"enabled": True, "every_min": 25, "min_block_min": 25}
db.run("INSERT INTO checkpoints (date, due_at, kind, state, block_id)"
       " VALUES (?, ?, 'declare', 'asked', ?)",
       (DAY.isoformat(), N(19, 30).isoformat(), db.blocks(DAY)[2]["id"]))
check("a request is not locked in as his finish line",
      run(bot._handle_declaration(FakeUpdate("redo the plan"), "redo the plan",
                                  N(19, 31))), False)

print("\n=== 'redo the plan' does not spend his late night ===")
cfg, db, led, eng, bot = make()
# Friday saw "imma run latenight" further up and decided for him.
db.run("INSERT INTO coach_log (date, ts, role, text) VALUES (?, ?, 'user', ?)",
       (DAY.isoformat(), N(21, 35).isoformat(), "bro imma run latenight"))
seen = model(bot, [json.dumps({
    "say": "send the late night pic to unlock the extra hours. got you, rebuilt it",
    "actions": [{"type": "make_plan", "day": "today", "text": "doc tasks"},
                {"type": "start_latenight", "text": "finishing doc tasks tonight"}]})])
u = FakeUpdate("redo the plan")
bot.cfg.now = lambda: N(21, 52)
run(bot.on_text(u, None))
replies = "\n".join(u.message.replies) if hasattr(u.message, "replies") else u.message.last
check("no late-night photo is asked for",
      (bot.pending.get(1) or {}).get("action") == "latenight_photo", False)
check("no 'rebuilt it' that never happened", "rebuilt" in replies, False)
check("he gets the honest short window instead", "8 min" in replies, True)
check("with the one way to get more, if he wants it", "/latenight" in replies, True)

print("\n=== but asking for it in so many words still works ===")
cfg, db, led, eng, bot = make()
seen = model(bot, [json.dumps({
    "say": "ok, pic first",
    "actions": [{"type": "start_latenight", "text": "KAIST essay due at midnight"}]})])
u = FakeUpdate("imma run latenight, KAIST essay due at midnight")
bot.cfg.now = lambda: N(21, 52)
run(bot.on_text(u, None))
check("the photo is asked for", (bot.pending.get(1) or {}).get("action"),
      "latenight_photo")

sys.exit(report("LATE NIGHT / SHORT WINDOW"))
