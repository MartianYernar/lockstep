"""The block-start proof model.

The point of this design: you are asked once, at the top of each block, and
then left alone. A system that interrupts you every few minutes to prove you
are concentrating is interfering with the thing it is supposed to protect.
"""
import os, sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fixtures import build, check, report, RAW
from server import planner

TZ = ZoneInfo("Asia/Almaty")
DAY = date(2026, 8, 25)
BEFORE = datetime(2026, 8, 25, 15, 59, tzinfo=TZ)

PLAN = """16:00-18:00 work: hackathon website
18:00-18:30 sport spine training
18:30-19:00 work: essay draft
19:00-19:20 shower
19:20-22:00 work: analysing"""


def make():
    import copy
    raw = copy.deepcopy(RAW)
    raw["schedule"]["mode"] = "block_start"
    raw["proof"] = {"shot_gap_min": 1}
    cfg, db, led, eng, lk = build(raw)
    db.set_lock(enabled=1, commitment_end="2099-01-01")
    db.ensure_day(DAY)
    blocks = planner.parse(PLAN)
    planner.validate_coverage(blocks, 16 * 60, 22 * 60)
    db.replace_blocks(DAY, [b.as_tuple() for b in blocks])
    db.run("UPDATE days SET plan_locked=1 WHERE date=?", (DAY.isoformat(),))
    return cfg, db, led, eng


cfg, db, led, eng = make()
built = eng.build_checkpoints(DAY, BEFORE)
rows = eng.checkpoints(DAY)

def at(hhmm):
    return [r for r in rows if r["due_at"][11:16] == hhmm]

print("\n=== proof clusters at the top of each block, nowhere else ===")
by_time = {}
for r in rows:
    by_time.setdefault(r["due_at"][11:16], []).append((r["kind"], r["label"]))
for t in sorted(by_time):
    for kind, label in by_time[t]:
        print(f"  {t}  {kind:15} {label}")

check("no checkpoints once a block is under way", at("16:30") + at("17:00") + at("20:00"), [])
check("nothing between clusters", at("16:05"), [])

print("\n=== a work block wants the three blocker shots ===")
work = [r["kind"] for r in rows if r["due_at"][11:16] in ("16:00", "16:01", "16:02")]
check("three shots", work, ["blocker_toggle", "blocker_allow", "blocker_block"])
check("a minute apart", [r["due_at"][11:16] for r in rows[:3]], ["16:00", "16:01", "16:02"])

print("\n=== a non-work block wants the activity too ===")
sport = [r["kind"] for r in rows
         if r["due_at"][11:16] in ("18:00", "18:01", "18:02", "18:03")]
check("four shots for sport",
      sport, ["blocker_toggle", "blocker_allow", "blocker_block", "activity"])
shower = [r["kind"] for r in rows
          if r["due_at"][11:16] in ("19:00", "19:01", "19:02", "19:03")]
check("four for shower too", len(shower), 4)
check("...and the last one is the activity", shower[-1], "activity")

essay = [r["kind"] for r in rows
         if r["due_at"][11:16] in ("18:30", "18:31", "18:32", "18:33")]
check("but only three for a work block", len(essay), 3)
check("no activity photo demanded at a screen", "activity" in essay, False)

print("\n=== the day is far quieter than spot checks ===")
check("five blocks", len(db.blocks(DAY)), 5)
check("17 checkpoints total, not 24+", built["made"], 17)
print(f"       {built['made']} photos across a 6-hour day, all clustered at 5 moments")

print("\n=== a plan written late still cannot demand the past ===")
cfg2, db2, led2, eng2 = make()
late = datetime(2026, 8, 25, 18, 45, tzinfo=TZ)
b2 = eng2.build_checkpoints(DAY, late)
states = {r["state"] for r in eng2.checkpoints(DAY)
          if datetime.fromisoformat(r["due_at"]) <= late}
check("everything already past is void", states, {"excused"})
check("and some proof still lies ahead", b2["live"] > 0, True)

print("\n=== sleep is protected from every kind of shift ===")
cfg3, db3, led3, eng3 = make()
db3.run("UPDATE days SET offset_minutes = 60 WHERE date = ?", (DAY.isoformat(),))
check("an hour's offset stops at the hard end", eng3.window(DAY)[1], 22 * 60 + 20)
db3.run("UPDATE days SET offset_minutes = 240 WHERE date = ?", (DAY.isoformat(),))
check("four hours cannot break it either", eng3.window(DAY)[1], 22 * 60 + 20)

sys.exit(report("PROOF-MODEL TESTS"))
