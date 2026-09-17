"""17 Sep, 22:26: past bedtime, on a deadline, and Friday was out of messages.

It answered "I'm out of juice for today" twice and never mentioned
/latenight, the one thing he needed.
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


def N(h, m):
    return datetime(2026, 8, 25, h, m, tzinfo=TZ)


def make(now, calls=40):
    raw = copy.deepcopy(RAW)
    raw["coach"] = {"enabled": True, "max_calls_per_day": calls}
    raw["sleep"] = {"bed_photo_at": "22:20", "siren_until": "01:00",
                    "latenight_hours": 2, "latenight_every_days": 7}
    cfg, db, led, eng, lk = build(raw, now=now)
    bot = LockstepBot(cfg, db, led, eng, lk, StubValidator())
    bot.app = types.SimpleNamespace(bot=FakeBot())
    db.set_lock(enabled=1, commitment_end="2099-01-01")
    db.ensure_day(DAY)
    db.replace_blocks(DAY, [b.as_tuple() for b in planner.parse("16:00-22:00 work: essay")])
    db.run("UPDATE days SET plan_locked=1 WHERE date=?", (DAY.isoformat(),))
    eng.build_checkpoints(DAY, N(15, 59))
    return cfg, db, led, eng, bot


def spend(bot, day=DAY):
    cap = int(bot.coach.conf["max_calls_per_day"])
    for _ in range(cap):
        bot.coach._charge(day)


print("=== out of messages, past bedtime, on a deadline ===")
cfg, db, led, eng, bot = make(N(22, 26))
check("bed is owed", bot._bed_owed(N(22, 26)), True)
spend(bot)
u = FakeUpdate("Bro I can't sleep I am on deadline")
run(bot.on_text(u, None))
check("it says why it can't chat", "used up my AI messages" in u.message.replies[0], True)
check("and starts the late night anyway", bot.pending.get(1, {}).get("action"), "latenight_photo")
check("asking for the pic", "send the pic" in u.message.last, True)

print("\n=== out of messages, anything else ===")
cfg, db, led, eng, bot = make(N(22, 26))
spend(bot)
u = FakeUpdate("Wdym")
run(bot.on_text(u, None))
check("says it hit the limit", "hit my limit of 40" in u.message.last, True)
check("says when it's back", "midnight" in u.message.last, True)
check("points at /latenight", "/latenight" in u.message.last, True)
check("nothing started", 1 in bot.pending, False)

print("\n=== outside bedtime, deadline words don't start anything ===")
cfg, db, led, eng, bot = make(N(19, 0))
spend(bot)
u = FakeUpdate("deadline tomorrow bro")
run(bot.on_text(u, None))
check("no late night before bedtime", 1 in bot.pending, False)
check("just the limit message", "hit my limit" in u.message.last, True)

print("\n=== with messages left, Friday can start it ===")
cfg, db, led, eng, bot = make(N(22, 26))


async def fake(system, contents, now, max_tokens=400, schema=None):
    fake.system = system
    bot.coach._charge(now.date())
    return json.dumps({"reply": "ok, show me the work", "actions": [
        {"type": "start_latenight", "text": "physics IA draft due at 8am tomorrow"}]})
bot.coach._ask = fake
u = FakeUpdate("i can't sleep, IA due at 8am")
run(bot.on_text(u, None))
check("Friday knows it's bedtime", "BEDTIME" in fake.system, True)
check("reply first", u.message.replies[0], "ok, show me the work")
check("late night started with the reason", bot.pending[1]["reason"],
      "physics IA draft due at 8am tomorrow")
check("nothing granted yet -- the photo decides",
      int(db.day(DAY)["bed_offset_minutes"] or 0), 0)

print("\n=== a too-short reason still gets asked properly ===")
cfg, db, led, eng, bot = make(N(22, 26))
spend(bot)
u = FakeUpdate("deadline")
run(bot.on_text(u, None))
check("asked for a real reason", "what for?" in u.message.last, True)

sys.exit(report("BEDTIME CHAT TESTS"))
