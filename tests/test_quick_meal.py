"""10 Oct 2026, 20:06-20:07, from the live record:

    YOU  Lemme eat for 10min
    FRI  go ahead, grabbed 10 mins for food. back to ielts right after though.
         (no replan happened)
    YOU  Bro wtf I said Imma eat for 10min, stop siren
    FRI  bro you know i literally can't stop it, only a pic does that
    YOU  If I send u image of food, can u stop it

Eating is a schedule change with one obvious shape, so the app does it
itself; a short meal needs no photo; and a reply that claims a change it did
not make is corrected before he reads it as fact.
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
PLAN = "16:00-20:30 work: prep for IELTS\n20:30-22:00 work: uni list"


def N(h, m):
    return datetime(2026, 8, 25, h, m, tzinfo=TZ)


def make(now=N(20, 6)):
    raw = copy.deepcopy(RAW)
    raw["tasks"] = {"seed": [{"title": "IELTS", "keywords": ["ielts"]}]}
    raw["coach"] = {"enabled": True, "max_calls_per_day": 150, "say_reserve": 0}
    raw["standup"] = {"enabled": False}
    raw["schedule"] = dict(raw["schedule"], mode="block_start", grace_min=3)
    raw["proof"] = {"within_block_min": 20}
    raw["friend"] = {"enabled": True, "guard_rewrites": True, "rewrite_min_lost": 10,
                     "meal_grace_min": 45}
    cfg, db, led, eng, lk = build(raw, now=now)
    bot = LockstepBot(cfg, db, led, eng, lk, StubValidator())
    bot.app = types.SimpleNamespace(bot=FakeBot())
    db.set_lock(enabled=1, commitment_end="2099-01-01")
    db.ensure_day(DAY)
    db.set_goals(DAY, ["ielts", "uni", "rest"])
    db.replace_blocks(DAY, [b.as_tuple() for b in planner.parse(PLAN)])
    db.run("UPDATE days SET plan_locked=1 WHERE date=?", (DAY.isoformat(),))
    eng.build_checkpoints(DAY, N(15, 59))
    return cfg, db, led, eng, bot


def model(bot, reply):
    seen = {"n": 0}

    async def fake(system, contents, now, max_tokens=400, schema=None, tier=None):
        seen["n"] += 1
        bot.coach._charge(now.date())
        return reply if isinstance(reply, str) else json.dumps(reply)
    bot.coach._ask = fake
    return seen


def blocks(db):
    return [(f"{b['start_min']//60:02d}:{b['start_min']%60:02d}-"
             f"{b['end_min']//60:02d}:{b['end_min']%60:02d}", b["label"])
            for b in db.blocks(DAY)]


print("=== 20:06 'Lemme eat for 10min' ===")
cfg, db, led, eng, bot = make()
# the 20:00 check is owed and the siren is going
db.run("UPDATE checkpoints SET state='overdue', asked_at=? WHERE date=? AND due_at=?",
       (N(20, 0).isoformat(), DAY.isoformat(), N(20, 0).isoformat()))
check("he is behind before he asks", eng.debt_seconds(DAY, N(20, 6)) > 0, True)
seen = model(bot, {"reply": "grabbed 10 mins for food", "actions": []})
u = FakeUpdate("Lemme eat for 10min")
run(bot.on_text(u, None))
check("the app did it -- no model was asked", seen["n"], 0)
check("the meal is in the plan, starting now", ("20:06-20:16", "eat") in blocks(db), True)
check("the block he was in resumes after it",
      ("20:16-20:30", "work: prep for IELTS") in blocks(db), True)
check("nothing later moved", ("20:30-22:00", "work: uni list") in blocks(db), True)
check("he was not asked to justify eating",
      bot.pending.get(1, {}).get("action") == "rewrite_why", False)
check("the siren stops", eng.debt_seconds(DAY, N(20, 7)), 0)
check("he is told plainly", "no pic needed" in u.message.last and "20:16" in u.message.last,
      True)
check("and when he's back on what", "prep for IELTS" in u.message.last, True)
check("the meal asks for no photo at all", db.one(
    "SELECT COUNT(*) c FROM checkpoints WHERE date=? AND due_at >= ? AND due_at < ?"
    " AND state != 'missed'", (DAY.isoformat(), N(20, 6).isoformat(),
                               N(20, 16).isoformat()))["c"], 0)
check("Friday knows he's eating", db.one(
    "SELECT COUNT(*) c FROM coach_log WHERE text LIKE '[app: he is eating%'")["c"], 1)

print("\n=== the ways he says it ===")
cfg, db, led, eng, bot = make()
for text, want in (("Lemme eat for 10min", 10), ("imma eat now", 20),
                   ("lunch 25 min", 25), ("eat for 15 minutes then ielts", 15),
                   ("щас поем 10 мин", 10), ("dinner rn", 20),
                   ("what should i eat?", None), ("i ate already", None),
                   ("food for thought", None), ("ielts now", None)):
    check(f"{text!r}", bot._meal_request(text), want)

print("\n=== an hour is not a snack ===")
cfg, db, led, eng, bot = make()
model(bot, {"reply": "ok", "actions": []})
run(bot.on_text(FakeUpdate("eat for 90 min"), None))
meal = next(b for b in db.blocks(DAY) if b["label"] == "eat")
check("capped at the meal allowance", meal["end_min"] - meal["start_min"], 45)

print("\n=== /eat ===")
cfg, db, led, eng, bot = make()
u = FakeUpdate("/eat 15")
run(bot.cmd_eat(u, None))
check("/eat 15 does the same", ("20:06-20:21", "eat") in blocks(db), True)
cfg, db, led, eng, bot = make(now=N(23, 0))
u = FakeUpdate("/eat")
run(bot.cmd_eat(u, None))
check("outside the window there's nothing to change", "just go eat" in u.message.last, True)

print("\n=== a planned dinner needs no photo either ===")
cfg, db, led, eng, bot = make(now=N(15, 0))
db.replace_blocks(DAY, [b.as_tuple() for b in planner.parse(
    "16:00-19:00 work: ielts\n19:00-19:30 dinner\n19:30-22:00 work: uni list")])
db.run("DELETE FROM checkpoints WHERE date=?", (DAY.isoformat(),))
db.run("UPDATE days SET checkpoints_built=0 WHERE date=?", (DAY.isoformat(),))
eng.build_checkpoints(DAY, N(15, 0))
check("no check inside a 30-minute dinner", db.one(
    "SELECT COUNT(*) c FROM checkpoints WHERE date=? AND due_at >= ? AND due_at < ?",
    (DAY.isoformat(), N(19, 0).isoformat(), N(19, 30).isoformat()))["c"], 0)
db.replace_blocks(DAY, [b.as_tuple() for b in planner.parse(
    "16:00-18:00 work: ielts\n18:00-20:00 dinner with friends\n20:00-22:00 work: uni list")])
db.run("DELETE FROM checkpoints WHERE date=?", (DAY.isoformat(),))
eng.build_checkpoints(DAY, N(15, 0))
check("but a two-hour 'dinner' is still proved", db.one(
    "SELECT COUNT(*) c FROM checkpoints WHERE date=? AND due_at >= ? AND due_at < ?",
    (DAY.isoformat(), N(18, 0).isoformat(), N(20, 0).isoformat()))["c"] > 0, True)

print("\n=== a change claimed but not made ===")
cfg, db, led, eng, bot = make(now=N(19, 0))
model(bot, {"reply": "go ahead, grabbed 10 mins for the walk. back to ielts after",
            "actions": []})
u = FakeUpdate("gonna walk outside for a bit")
run(bot.on_text(u, None))
check("he is told it didn't change", "didn't actually change the plan" in u.message.last, True)
check("and Friday's memory says so", db.one(
    "SELECT COUNT(*) c FROM coach_log WHERE text LIKE '%claimed a schedule change%'")["c"], 1)

cfg, db, led, eng, bot = make(now=N(19, 0))
model(bot, {"reply": "added it for thursday 3pm", "actions": [
    {"type": "add_event", "text": "CAS meeting", "date": "2026-08-27", "time": "15:00"}]})
u = FakeUpdate("cas meeting thursday 3pm")
run(bot.on_text(u, None))
check("saving an event is not mistaken for a fake schedule change",
      "didn't actually change" in u.message.last, False)

cfg, db, led, eng, bot = make(now=N(19, 0))
model(bot, {"reply": "nice, keep the pace up", "actions": []})
u = FakeUpdate("just finished task 2")
run(bot.on_text(u, None))
check("ordinary chat is left alone", u.message.last, "nice, keep the pace up")

sys.exit(report("QUICK MEAL TESTS"))
