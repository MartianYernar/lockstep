"""Random spot-checks: unpredictable, but never sloppy."""
import os, sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fixtures import build, check, report, spot_raw
from server import planner

TZ = ZoneInfo("Asia/Almaty")
DAY = date(2026, 8, 25)
PLAN = """16:00-16:40 commute to MNU library
16:40-19:00 work: linear algebra pset
19:00-19:30 gym
19:30-20:00 dinner
20:00-21:30 work: essay draft
21:30-22:00 clean room"""


def make(**over):
    cfg, db, led, eng, lk = build(spot_raw(**over))
    db.set_lock(enabled=1, commitment_end="2099-01-01")
    db.ensure_day(DAY)
    blocks = planner.parse(PLAN)
    planner.validate_coverage(blocks, 16 * 60, 22 * 60)
    db.replace_blocks(DAY, [b.as_tuple() for b in blocks])
    db.run("UPDATE days SET plan_locked=1 WHERE date=?", (DAY.isoformat(),))
    return cfg, db, led, eng


BEFORE = datetime(2026, 8, 25, 15, 59, tzinfo=TZ)   # before the window opens


def minutes_of(eng):
    out = []
    for c in eng.checkpoints(DAY):
        d = datetime.fromisoformat(c["due_at"])
        out.append((d.hour * 60 + d.minute, c["block_id"], c["label"]))
    return sorted(out)


print("\n=== every planned block gets watched ===")
cfg, db, led, eng = make()
n = eng.build_checkpoints(DAY, BEFORE)["made"]
pts = minutes_of(eng)
blocks = {b["id"]: b["label"] for b in db.blocks(DAY)}
covered = {p[1] for p in pts}
check("checkpoints created", n > 0, True)
check("no block left unchecked", len(covered), len(blocks))
check("none orphaned (every check has a block)", all(p[1] is not None for p in pts), True)
print(f"       {n} checks across {len(blocks)} blocks over a 6h window")
for m, bid, lbl in pts:
    print(f"         {m//60:02d}:{m%60:02d}  {lbl}")

print("\n=== each check lands inside its own block ===")
rows = {b["id"]: (b["start_min"], b["end_min"]) for b in db.blocks(DAY)}
ok = all(rows[bid][0] <= m < rows[bid][1] for m, bid, _ in pts)
check("all inside their block", ok, True)
check("all inside the enforced window", all(16*60 <= m < 22*60 for m, _, _ in pts), True)

print("\n=== minimum gap is respected ===")
gaps = [b[0] - a[0] for a, b in zip(pts, pts[1:])]
check("no two checks closer than 20 min", min(gaps) >= 20, True)
print(f"       gaps: {gaps}  (min {min(gaps)}, max {max(gaps)})")

print("\n=== rebuilding is idempotent (or every rebuild would pile up) ===")
again = eng.build_checkpoints(DAY, BEFORE)["made"]
check("second build adds nothing", again, 0)
check("count unchanged", len(minutes_of(eng)), len(pts))

print("\n=== but the times are genuinely unpredictable ===")
# Same plan, different day -> different draw. Otherwise it's a fixed schedule
# wearing a costume.
cfg2, db2, led2, eng2 = make()
db2.run("UPDATE blocks SET date=?", ((DAY + timedelta(days=1)).isoformat(),))
db2.ensure_day(DAY + timedelta(days=1))
eng2.build_checkpoints(DAY + timedelta(days=1), BEFORE)
d2 = [c["due_at"][11:16] for c in eng2.checkpoints(DAY + timedelta(days=1))]
d1 = [f"{m//60:02d}:{m%60:02d}" for m, _, _ in pts]
check("a different day draws different times", d1 == d2, False)
print(f"       day 1: {d1}")
print(f"       day 2: {d2}")

print("\n=== density follows the setting ===")
for avg, lo, hi in ((30, 9, 16), (45, 6, 12), (90, 3, 8)):
    _, _, _, e = make(avg=avg)
    c = e.build_checkpoints(DAY, BEFORE)["made"]
    check(f"avg={avg}min -> {lo}-{hi} checks/day", lo <= c <= hi, True)
    print(f"       avg {avg} min -> {c} checks")

print("\n=== short blocks still get exactly one ===")
cfg3, db3, led3, eng3 = build(spot_raw())[0:4] if False else make()
db3 = db; 
short = [b for b in db.blocks(DAY) if b["end_min"] - b["start_min"] <= 30]
for b in short:
    got = len([p for p in pts if p[1] == b["id"]])
    check(f"'{b['label']}' ({b['end_min']-b['start_min']}min) has 1 check", got, 1)

print("\n=== offset rebuilds cleanly (no orphans at the old times) ===")
cfg5, db5, led5, eng5 = make()
eng5.build_checkpoints(DAY, BEFORE)
before = {c["due_at"] for c in eng5.checkpoints(DAY)}
now5 = datetime(2026, 8, 25, 18, 0, tzinfo=TZ)
r5 = eng5.apply_offset(DAY, now5, 60, "meeting ran over")
after = eng5.checkpoints(DAY)
check("window extended", eng5.window(DAY)[1], 23 * 60)
check("no duplicate due times", len({c["due_at"] for c in after}), len(after))
stale = [c for c in after
         if c["state"] == "scheduled"
         and datetime.fromisoformat(c["due_at"]) < now5]
check("no scheduled checkpoint left in the past", len(stale), 0)
future = [c for c in after if datetime.fromisoformat(c["due_at"]) > now5]
check("future checks exist after the shift", len(future) > 0, True)
rows5 = {b["id"]: (b["start_min"], b["end_min"]) for b in db5.blocks(DAY)}
inside = all(rows5[c["block_id"]][0] <= datetime.fromisoformat(c["due_at"]).hour * 60
             + datetime.fromisoformat(c["due_at"]).minute < rows5[c["block_id"]][1]
             for c in future if c["block_id"] in rows5)
check("shifted checks land inside their shifted blocks", inside, True)

sys.exit(report("SPOT-CHECK TESTS"))
