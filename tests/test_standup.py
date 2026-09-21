"""The 20-minute check: what exactly, how fast, what's in the way."""
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
PLAN = ("16:00-17:00 commute\n17:00-19:00 work: physics IA\n"
        "19:00-19:30 eat\n19:30-22:00 work: essay")


def N(h, m):
    return datetime(2026, 8, 25, h, m, tzinfo=TZ)


def make(now, proof=None, **conf):
    raw = copy.deepcopy(RAW)
    raw["coach"] = {"enabled": True, "max_calls_per_day": 150}
    raw["friend"] = {"enabled": True, "checkins_per_day": 0}
    raw["standup"] = {"enabled": True, "every_min": 20, "max_per_day": 24,
                      "skip_categories": ["commute", "meal", "sleep", "relax"], **conf}
    raw["schedule"] = dict(raw["schedule"], mode="block_start", grace_min=3)
    if proof:
        raw["proof"] = proof
    cfg, db, led, eng, lk = build(raw, now=now)
    bot = LockstepBot(cfg, db, led, eng, lk, StubValidator())
    bot.app = types.SimpleNamespace(bot=FakeBot())
    db.set_lock(enabled=1, commitment_end="2099-01-01")
    db.ensure_day(DAY)
    db.replace_blocks(DAY, [b.as_tuple() for b in planner.parse(PLAN)])
    db.run("UPDATE days SET plan_locked=1 WHERE date=?", (DAY.isoformat(),))
    eng.build_checkpoints(DAY, N(15, 59))
    db.run("UPDATE checkpoints SET state='satisfied' WHERE date=?", (DAY.isoformat(),))
    return cfg, db, led, eng, bot


def model(bot, reply="ok"):
    seen = {"notes": []}

    async def fake(system, contents, now, max_tokens=400, schema=None, tier=None):
        seen["notes"].append(system)
        bot.coach._charge(now.date())
        return json.dumps({"reply": reply, "actions": []})
    bot.coach._ask = fake
    return seen


def sent(bot):
    return bot.app.bot.to(1)


print("=== asked on the 20-minute marks inside a block ===")
cfg, db, led, eng, bot = make(N(17, 0))
run(bot._standup_tick(N(17, 0)))
check("not at the very start of the block", sent(bot), [])
run(bot._standup_tick(N(17, 10)))
check("not at 10 past", sent(bot), [])
run(bot._standup_tick(N(17, 20)))
check("asked at 20 past", len(sent(bot)), 1)
q = sent(bot)[0]
check("asks the exact thing", "exactly" in q or "exact" in q or "precise" in q, True)
check("asks about speed", "drifting" in q or "fast" in q or "speed" in q, True)
check("asks what's in the way", "stuck" in q or "in the way" in q or "blocking" in q, True)
check("gives an example of specific", "q12" in q or "page" in q or "slide" in q, True)
run(bot._standup_tick(N(17, 20)))
check("only once per mark", len(sent(bot)), 1)
run(bot._standup_tick(N(17, 40)))
check("again at 40", len(sent(bot)), 2)
run(bot._standup_tick(N(18, 0)))
check("and at the hour", len(sent(bot)), 3)
check("the wording rotates", len(set(sent(bot))), 3)

print("\n=== not during a commute, a meal, or while behind ===")
cfg, db, led, eng, bot = make(N(16, 20))
run(bot._standup_tick(N(16, 20)))
check("commute is left alone", sent(bot), [])
cfg, db, led, eng, bot = make(N(19, 20))
run(bot._standup_tick(N(19, 20)))
check("eating is left alone", sent(bot), [])
cfg, db, led, eng, bot = make(N(22, 20))
run(bot._standup_tick(N(22, 20)))
check("outside the window, nothing", sent(bot), [])

cfg, db, led, eng, bot = make(N(17, 20))
db.run("UPDATE checkpoints SET state='overdue', asked_at=? WHERE date=? AND due_at<=?",
       (N(17, 0).isoformat(), DAY.isoformat(), N(17, 5).isoformat()))
run(bot._standup_tick(N(17, 20)))
check("no questions while he owes a photo", sent(bot), [])

print("\n=== his next message is the answer, and Friday judges it ===")
cfg, db, led, eng, bot = make(N(17, 20))
run(bot._standup_tick(N(17, 20)))
seen = model(bot, "which problem though?")
u = FakeUpdate("doing math")
run(bot.on_text(u, None))
check("Friday is told it's a standup answer", "20-minute check" in seen["notes"][0], True)
check("and told to push back on vague ones", "vague" in seen["notes"][0], True)
check("the block is named", "work: physics IA" in seen["notes"][0], True)
check("he gets the reply", u.message.last, "which problem though?")
rows = bot.standups.today(DAY)
check("the answer is kept", rows[0]["answer"], "doing math")
check("scored", bot.standups.score(DAY), (1, 1))

u2 = FakeUpdate("anything else")
run(bot.on_text(u2, None))
check("a second message is not another answer", bot.standups.today(DAY)[0]["answer"], "doing math")
check("and isn't treated as a standup", "20-minute check" in seen["notes"][1], False)

print("\n=== an answer that never comes ===")
cfg, db, led, eng, bot = make(N(17, 20))
run(bot._standup_tick(N(17, 20)))
check("pending right after", bot.standups.pending(N(17, 25)) is not None, True)
check("stale after half an hour", bot.standups.pending(N(17, 50)), None)
model(bot)
cfg.now = lambda: N(17, 50)
u = FakeUpdate("hey")
run(bot.on_text(u, None))
check("unanswered stays unanswered", bot.standups.score(DAY), (0, 1))

print("\n=== what he said feeds back into the conversation ===")
cfg, db, led, eng, bot = make(N(18, 0))
for m, t in (("solving q12 on page 40", N(17, 20)), ("still q12, stuck on the integral", N(17, 40))):
    sid = bot.standups.ask(t, "work: physics IA")
    bot.standups.answer(sid, m, t)
ctx = bot.coach.context(N(18, 0))
check("both answers are in the context", "q12 on page 40" in ctx and "the integral" in ctx, True)
check("and it is told to watch for drift", "drift" in ctx, True)

print("\n=== caps and switches ===")
cfg, db, led, eng, bot = make(N(17, 20), max_per_day=1)
run(bot._standup_tick(N(17, 20)))
run(bot._standup_tick(N(17, 40)))
check("the daily cap holds", len(sent(bot)), 1)

cfg, db, led, eng, bot = make(N(17, 20), enabled=False)
run(bot._standup_tick(N(17, 20)))
check("can be switched off", sent(bot), [])
check("and drops out of the report", "20-min checks" in bot._report_text(DAY), False)

cfg, db, led, eng, bot = make(N(17, 30), every_min=30)
run(bot._standup_tick(N(17, 30)))
check("the interval is configurable", len(sent(bot)), 1)

print("\n=== one message on the mark, not two ===")
# 21 Sep: the photo ask and the three questions landed in the same second,
# every 20 minutes, all evening.
cfg, db, led, eng, bot = make(N(17, 20), proof={"within_block_min": 20})
cp = db.one("SELECT * FROM checkpoints WHERE date=? AND due_at=?",
            (DAY.isoformat(), N(17, 20).isoformat()))
db.run("UPDATE checkpoints SET state='scheduled' WHERE id=?", (cp["id"],))
run(bot._standup_tick(N(17, 20)))
check("the standup holds back while a photo is due", sent(bot), [])
run(bot._dispatch(types.SimpleNamespace(kind="ask", payload={
    "due_at": N(17, 20).isoformat(), "label": "work: physics IA", "kind": "activity",
    "checkpoint_id": cp["id"]}), N(17, 20)))
msgs = sent(bot)
check("one message went out", len(msgs), 1)
check("it asks for the photo", "pic" in msgs[0].lower() or "show me" in msgs[0].lower(), True)
check("and carries the questions", "exactly" in msgs[0] and "stuck" in msgs[0], True)
check("the standup is recorded, so his text is the answer",
      bot.standups.count_asked(DAY), 1)
run(bot._standup_tick(N(17, 20)))
check("and it isn't asked a second time", len(sent(bot)), 1)

cfg, db, led, eng, bot = make(N(17, 0), proof={"within_block_min": 20})
cp = db.one("SELECT * FROM checkpoints WHERE date=? AND due_at=?",
            (DAY.isoformat(), N(17, 0).isoformat()))
run(bot._dispatch(types.SimpleNamespace(kind="ask", payload={
    "due_at": N(17, 0).isoformat(), "label": "work: physics IA", "kind": "blocker_toggle",
    "checkpoint_id": cp["id"]}), N(17, 0)))
check("the start of a block asks only for the screenshot", "stuck" in sent(bot)[0], False)
check("and records no standup", bot.standups.count_asked(DAY), 0)

print("\n=== /standup asks on demand ===")
cfg, db, led, eng, bot = make(N(17, 5))
u = FakeUpdate("/standup")
run(bot.cmd_standup(u, None))
check("asked", "1." in u.message.last, True)
check("counted", bot.standups.count_asked(DAY), 1)

print("\n=== in the nightly report ===")
cfg, db, led, eng, bot = make(N(21, 0))
for i, (m, t) in enumerate((("q12 p40", N(17, 20)), (None, N(17, 40)), ("slide 11", N(18, 0)))):
    sid = bot.standups.ask(t, "work: physics IA")
    if m:
        bot.standups.answer(sid, m, t)
check("counts answered", "20-min checks answered 2/3" in bot._report_text(DAY), True)

print("\n=== the old mid-block check-in is out of the way ===")
cfg, db, led, eng, bot = make(N(18, 0))
run(bot._friend_tick(N(18, 0)))
check("no duplicate 'how's it going'",
      [m for m in sent(bot) if "how's" in m], [])

sys.exit(report("STANDUP TESTS"))
