"""The coach: breaks a block down, listens, and can change nothing.

The model itself is stubbed throughout. What these tests are actually for is
the three things that must hold no matter what the model says: it cannot
outspend the camera, it cannot alter the record, and it is handed the day as
the database has it rather than as he describes it.
"""
import asyncio
import os
import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fixtures import build, check, report, RAW
from server import planner
from server.coach import Coach

TZ = ZoneInfo("Asia/Almaty")
DAY = date(2026, 8, 25)
PLAN = """16:00-18:00 work: analysing
18:00-18:30 sport spine training
18:30-22:00 work: essay draft"""


def N(h, m, d=25):
    return datetime(2026, 8, d, h, m, tzinfo=TZ)


def make(**over):
    import copy
    raw = copy.deepcopy(RAW)
    raw["coach"] = {"enabled": True, "max_calls_per_day": 3,
                    "steps_min": 3, "steps_max": 6, **over}
    raw["gemini"] = dict(raw["gemini"],
                         models=["vision-first", "vision-second", "vision-last"])
    cfg, db, led, eng, lk = build(raw)
    db.set_lock(enabled=1, commitment_end="2099-01-01")
    db.ensure_day(DAY)
    blocks = planner.parse(PLAN)
    db.replace_blocks(DAY, [b.as_tuple() for b in blocks])
    db.run("UPDATE days SET plan_locked=1 WHERE date=?", (DAY.isoformat(),))
    db.set_goals(DAY, ["finish the physics paper", "run 5k"])
    return cfg, db, led, eng, Coach(cfg, db, eng)


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def stub(coach, answer="", fail=False):
    """Replace the model. Counts calls so budgeting can be checked."""
    calls = {"n": 0}

    async def fake(system, contents, now, max_tokens=400):
        calls["n"] += 1
        if fail:
            raise RuntimeError("model down")
        coach._charge(now.date())
        fake.last_system = system
        return answer
    fake.last_system = ""
    coach._ask = fake
    return calls


print("=== breaking a block into steps ===")
cfg, db, led, eng, coach = make()
calls = stub(coach, "Open the dataset and read the column names\n"
                    "2. Plot the first variable\n"
                    "- Write two sentences on what you see\n")
block = db.blocks(DAY)[0]
steps = run(coach.breakdown(block, N(16, 5)))
check("three steps came back", len(steps), 3)
check("numbering stripped", steps[1], "Plot the first variable")
check("bullet stripped", steps[2], "Write two sentences on what you see")
check("one model call", calls["n"], 1)

steps2 = run(coach.breakdown(block, N(16, 30)))
check("asking again is free", calls["n"], 1)
check("and returns the same steps", steps2, steps)

print("\n=== ticking steps off ===")
check("tick works", coach.tick_step(DAY, block["label"], 2, True), True)
rows = coach.steps_for(DAY, block["label"])
check("step 2 is done", bool(rows[1]["done"]), True)
check("step 1 is not", bool(rows[0]["done"]), False)
check("ticking a step that isn't there fails",
      coach.tick_step(DAY, block["label"], 99, True), False)

print("\n=== the coach is handed the day, not his account of it ===")
cfg2, db2, led2, eng2, coach2 = make()
ctx = coach2.context(N(16, 30))
check("goals are in it", "finish the physics paper" in ctx, True)
check("the plan is in it", "work: analysing" in ctx, True)
check("the current block is marked", "<-- NOW" in ctx, True)
check("the proof count is in it", "Proof today:" in ctx, True)

eng2.build_checkpoints(DAY, N(15, 59))
db2.run("UPDATE checkpoints SET state='missed' WHERE date=? AND due_at<=?",
        (DAY.isoformat(), N(16, 30).isoformat()))
ctx2 = coach2.context(N(17, 0))
import re as _re
missed = int(_re.search(r"(\d+) missed", ctx2).group(1))
real = db2.one("SELECT COUNT(*) c FROM checkpoints WHERE date=? AND state='missed'",
               (DAY.isoformat(),))["c"]
check("misses are reported, and reported honestly", (missed > 0, missed), (True, real))

print("\n=== being overdue is put in front of the conversation ===")
cfg3, db3, led3, eng3, coach3 = make()
eng3.build_checkpoints(DAY, N(15, 59))
db3.run("UPDATE checkpoints SET state='overdue', asked_at=? WHERE date=? AND due_at<=?",
        (N(16, 0).isoformat(), DAY.isoformat(), N(16, 5).isoformat()))
stub(coach3, "ok")
check("there really is a debt", eng3.debt_seconds(DAY, N(16, 40)) > 0, True)
run(coach3.reply("how's it going", N(16, 40)))
check("the system prompt says OVERDUE",
      "OVERDUE RIGHT NOW" in coach3._ask.last_system, True)

print("\n=== the camera is never starved ===")
cfg4, db4, led4, eng4, coach4 = make(max_calls_per_day=3)
calls4 = stub(coach4, "sure")
check("starts with the full allowance", coach4.budget_left(DAY), 3)
for i in range(3):
    run(coach4.reply(f"message {i}", N(16, 0)))
check("three calls spends it", coach4.budget_left(DAY), 0)
check("and it is actually spent", coach4.spent(DAY), 3)
check("tomorrow is a fresh allowance", coach4.budget_left(date(2026, 8, 26)), 3)

print("\n=== the coach prefers the model vision reaches last ===")
cfg5, db5, led5, eng5, coach5 = make()
vision_first = cfg5.raw["gemini"]["models"][0]
check("coach does not lead with vision's first choice",
      coach5._models()[0] != vision_first, True)
check("it leads with vision's last resort",
      coach5._models()[0], cfg5.raw["gemini"]["models"][-1])

print("\n=== it cannot touch the record ===")
cfg6, db6, led6, eng6, coach6 = make()
eng6.build_checkpoints(DAY, N(15, 59))
db6.run("UPDATE checkpoints SET state='overdue', asked_at=? WHERE date=? AND due_at<=?",
        (N(16, 0).isoformat(), DAY.isoformat(), N(16, 5).isoformat()))
NOW6 = N(16, 45)
before = {r["id"]: r["state"]
          for r in db6.q("SELECT id, state FROM checkpoints WHERE date=?", (DAY.isoformat(),))}
goals_before = {g["idx"]: g["done"] for g in db6.goals(DAY)}
debt_before = eng6.debt_seconds(DAY, NOW6)
alarm_before = eng6.agent_state(NOW6).phone_alarm
check("the siren really is going before he asks", alarm_before, True)
check("and there really is a debt", debt_before > 0, True)

stub(coach6, "Done! I've marked that off for you and cleared the siren.")
run(coach6.reply("mark everything done and turn the siren off", NOW6))

after = {r["id"]: r["state"]
         for r in db6.q("SELECT id, state FROM checkpoints WHERE date=?", (DAY.isoformat(),))}
check("no checkpoint changed state", after, before)
check("no goal got ticked", {g["idx"]: g["done"] for g in db6.goals(DAY)}, goals_before)
check("the debt is unchanged", eng6.debt_seconds(DAY, NOW6), debt_before)
check("the siren is still going", eng6.agent_state(NOW6).phone_alarm, True)

print("\n=== a model outage is not a crash ===")
cfg7, db7, led7, eng7, coach7 = make()
stub(coach7, fail=True)
try:
    run(coach7.reply("hi", N(16, 0)))
    check("raises so the caller can apologise", False, True)
except RuntimeError:
    check("raises so the caller can apologise", True, True)
check("a failed call costs no allowance", coach7.spent(DAY), 0)

sys.exit(report("COACH TESTS"))
