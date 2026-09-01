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
    raw["vision"] = dict(raw["vision"], require_blocker_for_work=True)
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

print("\n=== a work block wants two blocker shots, not three ===")
# It was three until an allow-only block turned out to show its allowed and
# blocked lists in the same window -- the third shot asked for one screen to be
# photographed twice, a minute apart, at the top of every block.
work = [r["kind"] for r in rows if r["due_at"][11:16] in ("16:00", "16:01", "16:02")]
check("two shots", work, ["blocker_toggle", "blocker_allow"])
check("a minute apart", [r["due_at"][11:16] for r in rows[:2]], ["16:00", "16:01"])
check("the redundant third shot is gone", "blocker_block" in [r["kind"] for r in rows], False)

print("\n=== a non-work block wants the activity too ===")
sport = [r["kind"] for r in rows
         if r["due_at"][11:16] in ("18:00", "18:01", "18:02", "18:03")]
check("three shots for sport", sport, ["blocker_toggle", "blocker_allow", "activity"])
shower = [r["kind"] for r in rows
          if r["due_at"][11:16] in ("19:00", "19:01", "19:02", "19:03")]
check("three for shower too", len(shower), 3)
check("...and the last one is the activity", shower[-1], "activity")

essay = [r["kind"] for r in rows
         if r["due_at"][11:16] in ("18:30", "18:31", "18:32", "18:33")]
check("but only two for a work block", len(essay), 2)
check("no activity photo demanded at a screen", "activity" in essay, False)

print("\n=== the day is far quieter than spot checks ===")
check("five blocks", len(db.blocks(DAY)), 5)
check("12 checkpoints total, down from 17", built["made"], 12)
print(f"       {built['made']} photos across a 6-hour day, all clustered at 5 moments")

print("\n=== the committed block policy ===")
# A scope screenshot proves a list exists, not that it restricts anything: a
# block permitting everything except one site passes identically to one
# permitting nothing. The strictness is fixed in advance and checked later --
# the same principle as the plan itself.
from server.vision import compare_policy, policy_fingerprint, _norm_sites
POL = {"Physics lock in sesh": {"allow": ["chatgpt.com", "gemini.google.com"]},
       "Distractions": {"allow": []}}


def pol(name, kind, sites):
    return compare_policy(POL, name, kind, _norm_sites(sites))[0]


check("the committed list passes",
      pol("Physics lock in sesh", "allow", ["chatgpt.com", "gemini.google.com"]), True)
check("url noise is not a difference",
      pol("Physics lock in sesh", "allow", ["https://ChatGPT.com/", "www.gemini.google.com"]), True)
check("stricter than committed is fine",
      pol("Physics lock in sesh", "allow", ["chatgpt.com"]), True)
check("LOOSENED: an extra permitted site fails",
      pol("Physics lock in sesh", "allow", ["chatgpt.com", "youtube.com"]), False)
check("LOOSENED: block one site and allow the rest fails",
      pol("Physics lock in sesh", "block", ["youtube.com"]), False)
check("a block with nothing committed is not policed",
      pol("Some block I have not listed", "allow", ["anything.com"]), True)
check("no policy at all polices nothing",
      compare_policy({}, "Physics lock in sesh", "allow", ("youtube.com",))[0], True)

check("the fingerprint is stable across reorderings",
      policy_fingerprint(POL),
      policy_fingerprint({"Distractions": {"allow": []},
                          "Physics lock in sesh": {"allow": ["gemini.google.com", "chatgpt.com"]}}))
check("but loosening the policy changes it",
      policy_fingerprint(POL) == policy_fingerprint(
          {"Physics lock in sesh": {"allow": ["chatgpt.com", "gemini.google.com", "youtube.com"]},
           "Distractions": {"allow": []}}), False)

print("\n=== the off switch is real, not just a config key ===")
# require_blocker_for_work was documented in README and SETUP as the way to
# turn all of this off, and was referenced nowhere in the code. Flipping it did
# nothing, which is worse than not having it: it reads as a fix that failed.
import copy as _c
_off = _c.deepcopy(RAW)
_off["schedule"]["mode"] = "block_start"
_off["proof"] = {"shot_gap_min": 1}
_off["vision"] = dict(_off["vision"], require_blocker_for_work=False)
cfg4, db4, led4, eng4, lk4 = build(_off)
db4.ensure_day(DAY)
_b = planner.parse(PLAN)
db4.replace_blocks(DAY, [b.as_tuple() for b in _b])
db4.run("UPDATE days SET plan_locked=1 WHERE date=?", (DAY.isoformat(),))
made = eng4.build_checkpoints(DAY, BEFORE)
kinds_off = {r["kind"] for r in eng4.checkpoints(DAY)}
check("no screenshots demanded at all", kinds_off, {"activity"})
check("one photo per block, work or not", made["made"], 5)
check("...which is five, not twelve", made["made"] < built["made"], True)

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
