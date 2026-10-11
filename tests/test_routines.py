"""11 Oct 2026, Sunday, 10:02-10:04, from the live record.

He told Friday he was out teaching until 11am, then asked for a plan. It came
back "10:04-11:30 Do U of T application" and the siren started asking for a
photo of work he could not possibly be doing. He teaches every weekend
10:00-11:00 and is home by 12:00 -- a fact about his week, now written into
every plan by the app itself.
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
from server.routines import parse_days, parse_span

TZ = ZoneInfo("Asia/Almaty")
SUN = date(2026, 10, 11)
run = asyncio.get_event_loop().run_until_complete

# What the planner actually returned that morning, give or take.
BUILT = {"blocks": [
    {"start": "10:04", "end": "11:30", "label": "Do U of T application"},
    {"start": "11:30", "end": "12:00", "label": "Break"},
    {"start": "12:00", "end": "22:00", "label": "prep for IELTS"}],
    "note": "list first"}


def N(h, m):
    return datetime(2026, 10, 11, h, m, tzinfo=TZ)


def make(now=N(10, 4)):
    raw = copy.deepcopy(RAW)
    raw["schedule"] = dict(raw["schedule"], mode="block_start", grace_min=3,
                           weekend={"days": [5, 6], "enforce_start": "10:00",
                                    "enforce_end": "22:00"})
    raw["proof"] = {"within_block_min": 20}
    raw["coach"] = {"enabled": True, "max_calls_per_day": 150, "say_reserve": 0}
    raw["standup"] = {"enabled": False}
    raw["routines"] = [{"days": "weekends", "time": "10:00-11:00", "label": "teaching"},
                       {"days": "weekends", "time": "11:00-12:00", "label": "commute home"}]
    cfg, db, led, eng, lk = build(raw, now=now)
    bot = LockstepBot(cfg, db, led, eng, lk, StubValidator())
    bot.app = types.SimpleNamespace(bot=FakeBot())
    db.set_lock(enabled=1, commitment_end="2099-01-01")
    db.ensure_day(SUN)
    db.set_goals(SUN, ["uoft", "ielts", "cas"])
    return cfg, db, led, eng, bot


def model(bot, built=BUILT, friday=None):
    seen = {"planner": []}

    async def fake(system, contents, now, max_tokens=400, schema=None, tier=None):
        bot.coach._charge(now.date())
        if system.startswith("You build one day's timetable"):
            seen["planner"].append("\n".join(str(c) for c in contents))
            return json.dumps(built)
        seen["friday"] = system
        return json.dumps(friday or {"reply": "on it", "actions": [
            {"type": "make_plan", "day": "today", "text": ""}]})
    bot.coach._ask = fake
    return seen


print("=== reading how he says it ===")
check("weekends", parse_days("weekends"), [5, 6])
check("sat,sun", parse_days("sat, sun"), [5, 6])
check("weekdays", parse_days("weekdays"), [0, 1, 2, 3, 4])
check("mon wed", parse_days("mon and wed"), [0, 2])
check("a time span", parse_span("10:00-11:00"), (600, 660))
check("with dots", parse_span("11.00 - 12.00"), (660, 720))
check("nonsense is refused", parse_span("in the morning"), None)

print("\n=== 10:04: 'Can u build plan for me' ===")
cfg, db, led, eng, bot = make()
check("his routine came in from the config", len(bot.coach.routines.for_day(SUN)), 2)
seen = model(bot)
u = FakeUpdate("Can u build plan for me")
run(bot.on_text(u, None))
shown = bot.pending[1]["text"]
check("the planner was told about the teaching hour", "teaching" in seen["planner"][0], True)
check("the plan he is shown starts with what he is actually doing",
      shown.splitlines()[0], "10:04-11:00 teaching")
check("then the ride home", "11:00-12:00 commute home" in shown, True)
check("the U of T work did not land on top of his class",
      any(l.startswith("10:") and "U of T" in l for l in shown.splitlines()), False)
check("and the day still covers the window to 22:00",
      shown.splitlines()[-1].endswith("prep for IELTS") and "-22:00" in shown.splitlines()[-1],
      True)

print("\n=== and they cost no photo ===")
cfg, db, led, eng, bot = make(now=N(9, 0))
from server import planner  # noqa: E402
db.replace_blocks(SUN, [b.as_tuple() for b in planner.parse(
    "10:00-11:00 teaching\n11:00-12:00 commute home\n12:00-22:00 prep for IELTS")])
db.run("UPDATE days SET plan_locked=1 WHERE date=?", (SUN.isoformat(),))
eng.build_checkpoints(SUN, N(9, 0))
check("no photo check while he teaches or rides home", db.one(
    "SELECT COUNT(*) c FROM checkpoints WHERE date=? AND due_at < ?",
    (SUN.isoformat(), N(12, 0).isoformat()))["c"], 0)
check("the first one is when he is home and working", db.one(
    "SELECT MIN(due_at) d FROM checkpoints WHERE date=? AND kind != 'bed'",
    (SUN.isoformat(),))["d"][11:16], "12:00")

print("\n=== a weekday is untouched ===")
MON = date(2026, 10, 12)
check("no routine on Monday", bot.coach.routines.for_day(MON), [])

print("\n=== Friday can learn a new one ===")
cfg, db, led, eng, bot = make()
model(bot, friday={"reply": "noted", "actions": [
    {"type": "add_routine", "day": "sat", "time": "15:00-16:30",
     "title": "robotics club"}]})
u = FakeUpdate("every saturday I have robotics club 15:00-16:30")
run(bot.on_text(u, None))
check("saved", any("routine saved" in r for r in u.message.replies), True)
check("on saturdays only", [r["label"] for r in bot.coach.routines.for_day(date(2026, 10, 10))
                            if r["label"] == "robotics club"], ["robotics club"])
check("it is in Friday's view of his week", "robotics club" in bot.coach.context(N(10, 30)),
      True)
check("restarting does not duplicate the config ones",
      bot.coach.routines.seed(cfg.raw["routines"], N(10, 31)), 0)

sys.exit(report("ROUTINE TESTS"))
