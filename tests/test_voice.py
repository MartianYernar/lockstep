"""The messages the bot starts are worded by Friday, not by the code.

"📵 your phone hasn't checked in for 1065 min, so the siren is dead rn. Open
Automate, press Start on both flows 🙏" is a machine talking. The facts are
the same either way; who says them is the difference. Every one of these keeps
a written fallback for when the allowance is gone or the model is down -- the
facts must still arrive.
"""
import asyncio
import copy
import os
import sys
import types
from datetime import date, datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fixtures import build, check, report, RAW
from test_friend_fakes import FakeBot, StubValidator
from server.bot import LockstepBot
from server import planner

TZ = ZoneInfo("Asia/Almaty")
DAY = date(2026, 8, 25)
run = asyncio.get_event_loop().run_until_complete


def N(h, m):
    return datetime(2026, 8, 25, h, m, tzinfo=TZ)


def make(now=N(17, 0), reserve=30, calls=150, proof=None):
    raw = copy.deepcopy(RAW)
    if proof:
        raw["proof"] = proof
    raw["coach"] = {"enabled": True, "max_calls_per_day": calls, "say_reserve": reserve}
    raw["standup"] = {"enabled": False}
    raw["schedule"] = dict(raw["schedule"], mode="block_start", grace_min=3)
    cfg, db, led, eng, lk = build(raw, now=now)
    bot = LockstepBot(cfg, db, led, eng, lk, StubValidator())
    bot.app = types.SimpleNamespace(bot=FakeBot())
    db.set_lock(enabled=1, commitment_end="2099-01-01")
    db.ensure_day(DAY)
    db.replace_blocks(DAY, [b.as_tuple() for b in planner.parse(
        "16:00-19:00 work: physics IA\n19:00-22:00 math")])
    db.run("UPDATE days SET plan_locked=1 WHERE date=?", (DAY.isoformat(),))
    eng.build_checkpoints(DAY, N(15, 59))
    return cfg, db, led, eng, bot


def model(bot, reply="yo, pic for physics IA whenever you're ready", fail=False):
    seen = {"jobs": [], "facts": [], "n": 0}

    async def fake(system, contents, now, max_tokens=400, schema=None, tier=None):
        seen["n"] += 1
        text = contents[0] if isinstance(contents[0], str) else ""
        if "WHAT THIS IS ABOUT" in text:
            seen["jobs"].append(text.split("WHAT THIS IS ABOUT:")[1].split("\n")[0].strip())
            seen["facts"].append(text.split("THE FACTS")[1])
        if fail:
            raise RuntimeError("503 UNAVAILABLE")
        bot.coach._charge(now.date())
        return reply
    bot.coach._ask = fake
    return seen


def ask(bot, now=N(16, 0), kind="activity", label="work: physics IA", cid=1):
    run(bot._dispatch(types.SimpleNamespace(kind="ask", payload={
        "due_at": now.isoformat(), "label": label, "kind": kind,
        "checkpoint_id": cid}), now))


def sent(bot):
    return bot.app.bot.to(1)


print("=== the photo request is worded by Friday ===")
cfg, db, led, eng, bot = make()
seen = model(bot)
ask(bot)
check("it went out in his voice", sent(bot), ["yo, pic for physics IA whenever you're ready"])
check("Friday was told the job", "proof photo" in seen["jobs"][0], True)
check("and given the block", "work: physics IA" in seen["facts"][0], True)
check("and the grace period", "3 minutes" in seen["facts"][0], True)
check("and what the photo has to show", "clock" in seen["facts"][0], True)

print("\n=== a blocker block asks for the right thing ===")
cfg, db, led, eng, bot = make()
seen = model(bot)
ask(bot, kind="blocker_toggle")
check("the screenshot is described", "Cold Turkey" in seen["facts"][0], True)

print("\n=== the phone going quiet ===")
cfg, db, led, eng, bot = make()
seen = model(bot, "bro your phone's been dead to me for 17 hours, open automate")
run(bot._dispatch(types.SimpleNamespace(
    kind="phone_silent", payload={"minutes": 1065, "escalate": False}), N(17, 0)))
check("worded by Friday", sent(bot)[0].startswith("bro your phone"), True)
check("given the minutes", "1065" in seen["facts"][0], True)
check("and the hours, so it can say it like a person", "17 hours" in seen["facts"][0], True)
check("and the app to open", "Automate" in seen["facts"][0], True)
check("and told the siren is what's broken", "siren" in seen["jobs"][0], True)

print("\n=== the missed photo ===")
cfg, db, led, eng, bot = make()
seen = model(bot, "math pic? siren's on")
run(bot._dispatch(types.SimpleNamespace(
    kind="mark_missed", payload={"labels": ["math"], "count": 1,
                                 "checkpoint_id": 3}), N(19, 30)))
check("worded by Friday", sent(bot), ["math pic? siren's on"])
check("told which block", "math" in seen["facts"][0], True)
check("told whether the siren is already going",
      "siren" in seen["facts"][0].lower(), True)

print("\n=== bedtime and the plan nag go through it too ===")
cfg, db, led, eng, bot = make()
seen = model(bot, "bed pic bro")
run(bot._dispatch(types.SimpleNamespace(
    kind="bed_overdue", payload={"minutes": 12}), N(22, 32)))
check("bedtime", sent(bot), ["bed pic bro"])
check("it knows about /latenight", "/latenight" in seen["facts"][0], True)

cfg, db, led, eng, bot = make()
seen = model(bot, "plan for tomorrow?")
run(bot._dispatch(types.SimpleNamespace(
    kind="plan_nag", payload={"date": "2026-08-26"}), N(20, 0)))
check("the plan nag", sent(bot), ["plan for tomorrow?"])
check("with the date and the deadline",
      "2026-08-26" in seen["facts"][0] and "23:30" in seen["facts"][0], True)

print("\n=== when Friday can't write it, the facts still arrive ===")
cfg, db, led, eng, bot = make()
model(bot, fail=True)
ask(bot)
out = sent(bot)[0]
check("something was still sent", bool(out), True)
check("it names the block", "physics IA" in out, True)
check("and the time limit", "3 min" in out, True)

cfg, db, led, eng, bot = make()
model(bot, fail=True)
run(bot._dispatch(types.SimpleNamespace(
    kind="phone_silent", payload={"minutes": 1065, "escalate": False}), N(17, 0)))
check("the fallback still says what to open", "Automate" in sent(bot)[0], True)
check("and how long it's been", "1065" in sent(bot)[0], True)

print("\n=== chat keeps the last of the allowance ===")
cfg, db, led, eng, bot = make(reserve=30, calls=40)
seen = model(bot)
for _ in range(11):
    bot.coach._charge(DAY)
check("still above the reserve", bot.coach.budget_left(DAY), 29)
ask(bot)
check("so the nag is not worded by Friday", seen["n"], 0)
check("but it is still sent, from the written line", len(sent(bot)), 1)

cfg, db, led, eng, bot = make(reserve=0)
seen = model(bot)
ask(bot)
check("with no reserve set, Friday words everything", seen["n"], 1)

print("\n=== ignored twice, the written line is enough ===")
# 21 Sep: seventeen asks and seventeen "where's the pic", each worded fresh,
# spent the allowance on messages nobody was reading.
cfg, db, led, eng, bot = make(N(17, 0), reserve=0, proof={"within_block_min": 20})
db.run("UPDATE checkpoints SET state='missed' WHERE date=? AND due_at < ?",
       (DAY.isoformat(), N(16, 40).isoformat()))
cp = db.one("SELECT id, due_at FROM checkpoints WHERE date=? AND due_at >= ? ORDER BY due_at",
            (DAY.isoformat(), N(16, 40).isoformat()))
seen = model(bot)
ask(bot, now=datetime.fromisoformat(cp["due_at"]), cid=cp["id"])
check("the ask still goes out", len(sent(bot)), 1)
check("from the written line, no model call", seen["n"], 0)
run(bot._dispatch(types.SimpleNamespace(
    kind="mark_missed", payload={"labels": ["work: physics IA"], "count": 1,
                                 "checkpoint_id": cp["id"]}), N(17, 5)))
check("the miss too", (len(sent(bot)), seen["n"]), (2, 0))

cfg, db, led, eng, bot = make(N(17, 0), reserve=0, proof={"within_block_min": 20})
db.run("UPDATE checkpoints SET state='satisfied' WHERE date=? AND due_at < ?",
       (DAY.isoformat(), N(16, 20).isoformat()))
db.run("UPDATE checkpoints SET state='missed' WHERE date=? AND due_at >= ? AND due_at < ?",
       (DAY.isoformat(), N(16, 20).isoformat(), N(16, 40).isoformat()))
cp = db.one("SELECT id, due_at FROM checkpoints WHERE date=? AND due_at >= ? ORDER BY due_at",
            (DAY.isoformat(), N(16, 40).isoformat()))
seen = model(bot)
ask(bot, now=datetime.fromisoformat(cp["due_at"]), cid=cp["id"])
check("one miss alone still gets Friday's words", seen["n"], 1)

print("\n=== nothing is invented ===")
cfg, db, led, eng, bot = make()
seen = model(bot)
ask(bot)
facts = seen["facts"][0]
check("the prompt forbids inventing", "Don't invent" in facts or "invent" in facts, True)
check("and forbids promising changes", "promise" in facts, True)

sys.exit(report("VOICE TESTS"))
