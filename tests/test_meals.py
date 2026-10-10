"""Eating does not have to be argued for.

10 Oct 2026, his words: "bro the bot isn't letting me eat". Moving work aside
for dinner looked to the rewrite guard exactly like moving it aside for
nothing, and the judge's instructions say to refuse "I need a break". So a
meal went to the model to be approved, and could be refused.

A body is not a negotiation. A normal meal goes through in silence; a
two-hour one is still a fair question.
"""
import asyncio
import copy
import json
import os
import sys
import types
from datetime import date, datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fixtures import build, check, report, RAW
from test_friend_fakes import FakeUpdate, FakeBot, StubValidator
from server.bot import LockstepBot
from server import planner

TZ = ZoneInfo("Asia/Almaty")
DAY = date(2026, 8, 25)
run = asyncio.get_event_loop().run_until_complete
PLAN = "16:00-19:00 work: physics IA\n19:00-22:00 work: uni essay"


def N(h, m):
    return datetime(2026, 8, 25, h, m, tzinfo=TZ)


def make(now=N(18, 30), **friend):
    raw = copy.deepcopy(RAW)
    raw["tasks"] = {"seed": [{"title": "Physics IA", "keywords": ["physics ia"]}]}
    raw["coach"] = {"enabled": True, "max_calls_per_day": 150, "say_reserve": 0}
    raw["standup"] = {"enabled": False}
    raw["friend"] = {"enabled": True, "guard_rewrites": True, "rewrite_min_lost": 10,
                     "meal_grace_min": 45, **friend}
    cfg, db, led, eng, lk = build(raw, now=now)
    bot = LockstepBot(cfg, db, led, eng, lk, StubValidator())
    bot.app = types.SimpleNamespace(bot=FakeBot())
    db.set_lock(enabled=1, commitment_end="2099-01-01")
    db.ensure_day(DAY)
    db.set_goals(DAY, ["IA", "essay", "gym"])
    db.replace_blocks(DAY, [b.as_tuple() for b in planner.parse(PLAN)])
    db.run("UPDATE days SET plan_locked=1 WHERE date=?", (DAY.isoformat(),))
    return cfg, db, led, eng, bot


def model(bot, reply):
    seen = {"n": 0, "systems": []}

    async def fake(system, contents, now, max_tokens=400, schema=None, tier=None):
        seen["n"] += 1
        seen["systems"].append(system)
        bot.coach._charge(now.date())
        return reply if isinstance(reply, str) else json.dumps(reply)
    bot.coach._ask = fake
    return seen


def labels(db):
    return [b["label"] for b in db.blocks(DAY)]


print("=== half an hour for dinner, no questions ===")
cfg, db, led, eng, bot = make()
seen = model(bot, {"is_reason": False, "accept": False, "reply": "why?"})
u = FakeUpdate("/replan\n18:30-19:00 dinner\n19:00-22:00 work: uni essay")
run(bot.cmd_replan(u, None))
check("it just happens", "dinner" in " ".join(labels(db)), True)
check("nobody was asked to justify it", seen["n"], 0)
check("and he is not interrogated", "why" in u.message.last.lower(), False)

print("\n=== the work still has to be somewhere ===")
cfg, db, led, eng, bot = make()
seen = model(bot, {"is_reason": False, "accept": False, "reply": "what's the reason?"})
u = FakeUpdate("/replan\n18:30-21:00 dinner\n21:00-22:00 work: uni essay")
run(bot.cmd_replan(u, None))
check("two and a half hours of dinner is a real question",
      bot.pending.get(1, {}).get("action"), "rewrite_why")
check("so it was asked", seen["n"] > 0 or "why" in u.message.last.lower(), True)
check("and nothing moved yet", "dinner" in " ".join(labels(db)), False)

print("\n=== the exact minute it stops being a meal ===")
# The 45 minutes of grace sit on top of the guard's own 10-minute threshold,
# so dinner is waved through below 55 minutes and questioned from there on.
for mins, guarded in ((45, False), (54, False), (55, True), (90, True)):
    cfg, db, led, eng, bot = make()
    model(bot, {"is_reason": False, "accept": False, "reply": "?"})
    end_h, end_m = divmod(18 * 60 + 30 + mins, 60)
    run(bot.cmd_replan(FakeUpdate(
        f"/replan\n18:30-{end_h:02d}:{end_m:02d} dinner\n"
        f"{end_h:02d}:{end_m:02d}-22:00 work: uni essay"), None))
    check(f"{mins} min of dinner {'is held' if guarded else 'goes through'}",
          bot.pending.get(1, {}).get("action") == "rewrite_why", guarded)

print("\n=== it is a meal allowance, not a free hour ===")
cfg, db, led, eng, bot = make()
model(bot, {"is_reason": False, "accept": False, "reply": "?"})
run(bot.cmd_replan(FakeUpdate(
    "/replan\n18:30-19:00 relax\n19:00-22:00 work: uni essay"), None))
check("half an hour of nothing is still questioned",
      bot.pending.get(1, {}).get("action"), "rewrite_why")

cfg, db, led, eng, bot = make()
model(bot, {"is_reason": False, "accept": False, "reply": "?"})
run(bot.cmd_replan(FakeUpdate(
    "/replan\n18:30-19:00 dinner\n19:00-19:30 chill\n19:30-22:00 work: uni essay"), None))
check("and dinner does not pay for the chill after it",
      bot.pending.get(1, {}).get("action"), "rewrite_why")

print("\n=== through Friday, the same ===")
cfg, db, led, eng, bot = make()
seen = model(bot, {"reply": "yeah go eat", "actions": [
    {"type": "edit_plan", "day": "today",
     "plan": "18:30-19:00 dinner\n19:00-22:00 work: uni essay"}]})
u = FakeUpdate("bro i'm starving, gonna eat now")
run(bot.on_text(u, None))
# Since 11 Oct the app does this itself rather than hoping a model emits the
# right action -- 10 Oct 20:06 it said "grabbed 10 mins for food" and didn't.
check("he is answered", any("eat" in r or "🍽" in r for r in u.message.replies), True)
check("the plan changed", "eat" in labels(db), True)
check("without asking a model", seen["n"], 0)
check("and he was never asked why he eats",
      any("why" in r.lower() for r in u.message.replies), False)

print("\n=== and the judge is told what food is ===")
from server.coach import JUDGE  # noqa: E402
check("in plain words", "Food is not a break" in JUDGE, True)
check("accept it", "ALWAYS ACCEPT eating" in JUDGE, True)

sys.exit(report("MEAL TESTS"))
