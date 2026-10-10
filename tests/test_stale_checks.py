"""The siren asks for the plan you have, not the one you replaced.

10 Oct 2026, 14:43: "bro the siren's going off rn, send the pic for 'prep for
IELTS'" -- while the locked plan said CAS. He had replanned at 14:41; the
IELTS checkpoint had already been asked, so it survived the rewrite and went
on demanding a photo of work that was no longer in the day. There is no way
to answer that one honestly: the block is gone.

The miss still counts. What goes is the impossible demand, not the record.
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
from test_friend_fakes import FakeUpdate, FakeBot, StubValidator
from server.bot import LockstepBot
from server import planner

TZ = ZoneInfo("Asia/Almaty")
DAY = date(2026, 8, 25)
run = asyncio.get_event_loop().run_until_complete
PLAN = "16:00-18:00 work: prep for IELTS\n18:00-22:00 work: uni essay"


def N(h, m):
    return datetime(2026, 8, 25, h, m, tzinfo=TZ)


def make(now=N(16, 41)):
    raw = copy.deepcopy(RAW)
    raw["coach"] = {"enabled": False}
    raw["standup"] = {"enabled": False}
    raw["schedule"] = dict(raw["schedule"], mode="block_start", grace_min=3)
    raw["proof"] = {"within_block_min": 20}
    raw["friend"] = {"enabled": True, "guard_rewrites": False}
    cfg, db, led, eng, lk = build(raw, now=now)
    bot = LockstepBot(cfg, db, led, eng, lk, StubValidator())
    bot.app = types.SimpleNamespace(bot=FakeBot())
    db.set_lock(enabled=1, commitment_end="2099-01-01")
    db.ensure_day(DAY)
    db.set_goals(DAY, ["ielts", "essay", "cas"])
    db.replace_blocks(DAY, [b.as_tuple() for b in planner.parse(PLAN)])
    db.run("UPDATE days SET plan_locked=1 WHERE date=?", (DAY.isoformat(),))
    eng.build_checkpoints(DAY, N(15, 59))
    return cfg, db, led, eng, bot


def states(db):
    return [(c["state"], c["label"]) for c in db.q(
        "SELECT c.state, b.label FROM checkpoints c LEFT JOIN blocks b ON b.id = c.block_id"
        " WHERE c.date = ? ORDER BY c.due_at", (DAY.isoformat(),))]


print("=== replacing the block the siren is asking about ===")
cfg, db, led, eng, bot = make()
# 16:40's check has been asked and not answered -- the siren is going.
db.run("UPDATE checkpoints SET state='asked', asked_at=? WHERE date=? AND due_at=?",
       (N(16, 40).isoformat(), DAY.isoformat(), N(16, 40).isoformat()))
owed = eng.current_checkpoint(DAY, N(16, 41))
check("it is asking about IELTS", owed["label"], "work: prep for IELTS")
check("and he is behind", eng.debt_seconds(DAY, N(16, 44)) > 0, True)

# The path he actually used: the plan is locked and the window is open, so a
# change to today is a replan.
u = FakeUpdate("/replan\n16:41-17:11 CAS activity replacement\n"
               "17:11-18:00 work: prep for IELTS\n18:00-22:00 work: uni essay")
run(bot.cmd_replan(u, None))
# A replan keeps what is already behind him, truncated at the current minute,
# so the IELTS block survives as 16:00-16:41 and CAS starts now.
labels = [b["label"] for b in db.blocks(DAY)]
check("the new plan is in", labels[:2],
      ["work: prep for IELTS", "CAS activity replacement"])
check("and the old block stops at the moment he changed it",
      db.blocks(DAY)[0]["end_min"], 16 * 60 + 41)
after = [s for s, _ in states(db)]
check("nothing is still demanding the replaced block",
      any(s in ("asked", "overdue") and (l or "") == "work: prep for IELTS"
          for s, l in states(db)), False)
check("the unanswered one counts as missed", "missed" in after, True)
check("and he is told, so it is not a quiet pardon",
      "missed" in u.message.last, True)
check("the siren now asks about what he is actually doing",
      (eng.current_checkpoint(DAY, N(16, 45)) or {"label": None})["label"]
      in (None, "CAS activity replacement"), True)

print("\n=== a block he keeps keeps its checkpoint ===")
cfg, db, led, eng, bot = make()
db.run("UPDATE checkpoints SET state='asked', asked_at=? WHERE date=? AND due_at=?",
       (N(16, 40).isoformat(), DAY.isoformat(), N(16, 40).isoformat()))
run(bot.cmd_replan(FakeUpdate(
    "/replan\n16:41-18:00 work: prep for IELTS\n18:00-20:00 gym\n"
    "20:00-22:00 work: uni essay"), None))
check("the IELTS pic is still owed", any(
    s in ("asked", "overdue") and (l or "") == "work: prep for IELTS"
    for s, l in states(db)), True)

print("\n=== bedtime is nobody's block ===")
cfg, db, led, eng, bot = make(now=N(16, 41))
bed = db.one("SELECT id FROM checkpoints WHERE date=? AND kind='bed'", (DAY.isoformat(),))
if bed:
    db.run("UPDATE checkpoints SET state='asked' WHERE id=?", (bed["id"],))
run(bot.cmd_plan(FakeUpdate(
    "/plan\n16:41-22:00 work: uni essay"), None))
if bed:
    check("the bed check is left exactly as it was",
          db.one("SELECT state FROM checkpoints WHERE id=?", (bed["id"],))["state"], "asked")

print("\n=== and no database row numbers in what he reads ===")
import inspect  # noqa: E402
src = inspect.getsource(LockstepBot)
check("the proposal id never goes into Friday's memory",
      "[proposed a plan for" in src and "proposed plan #" not in src, True)
check("nor does the lock tap", "[tapped the lock button]" in src, True)

sys.exit(report("STALE CHECKPOINT TESTS"))
