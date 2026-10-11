"""Write today's routines into an already-locked plan. Run in the container:

    railway ssh -- python /app/scripts/apply_routines_today.py

11 Oct 2026: the plan locked at 10:04 had U of T work on top of his teaching
hour. Checks asked during a routine were the plan's fault, not his, so they
are excused (not missed) and the excuse is on the ledger.
"""
import os
import sys

sys.path.insert(0, "/app")
from server.config import Config      # noqa: E402
from server.db import Database        # noqa: E402
from server.ledger import Ledger      # noqa: E402
from server.engine import Engine      # noqa: E402
from server.routines import Routines  # noqa: E402
from server import planner            # noqa: E402

cfg = Config.load(os.environ.get("LOCKSTEP_CONFIG"))
db = Database(os.environ.get("LOCKSTEP_DB"))
led = Ledger(db)
eng = Engine(cfg, db, led)
rt = Routines(db)
now = cfg.now()
day = now.date()
fixed = rt.for_day(day)
if not fixed or not eng.has_plan(day):
    print("nothing to do"); sys.exit(0)
start, end = eng.window(day)
old = [planner.Block(b["start_min"], b["end_min"], b["label"], b["category"])
       for b in db.blocks(day)]
new = rt.overlay(day, old, start, end, planner)
planner.validate_coverage(new, start, end)
db.replace_blocks(day, [b.as_tuple() for b in new])
lo = min(r["start_min"] for r in fixed)
hi = max(r["end_min"] for r in fixed)
d0 = cfg.at(day, lo).isoformat()
d1 = cfg.at(day, hi).isoformat()
ex = db.run("UPDATE checkpoints SET state='excused', settled_at=? WHERE date=?"
            " AND due_at >= ? AND due_at < ? AND state IN ('scheduled','asked','overdue')"
            " AND kind != 'bed'", (now.isoformat(), day.isoformat(), d0, d1)).rowcount
db.run("DELETE FROM checkpoints WHERE date=? AND state='scheduled'", (day.isoformat(),))
db.run("UPDATE days SET checkpoints_built=0 WHERE date=?", (day.isoformat(),))
built = eng.build_checkpoints(day, now)
led.append("routines_applied", {"date": day.isoformat(), "excused": ex,
                                "blocks": len(new)})
print(f"excused {ex} check(s) inside the routine; plan now:")
for b in db.blocks(day):
    print(f"  {b['start_min']//60:02d}:{b['start_min']%60:02d}-"
          f"{b['end_min']//60:02d}:{b['end_min']%60:02d} {b['label']}")
print("first check:", built["first"])
