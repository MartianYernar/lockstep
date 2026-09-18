"""Every minute the server asks for noise goes on the record.

18 Sep 02:00: the siren ran and nobody could say whether the server had asked
for it. It had not -- but proving that meant downloading the database and
replaying the night. One ledger row per sirening minute makes the same
question a single query.
"""
import asyncio
import copy
import os
import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fixtures import build, check, report, RAW
from server.api import build_app
from server import planner

TZ = ZoneInfo("Asia/Almaty")
DAY = date(2026, 8, 25)


def N(h, m):
    return datetime(2026, 8, 25, h, m, tzinfo=TZ)


def make(now):
    raw = copy.deepcopy(RAW)
    raw["schedule"] = dict(raw["schedule"], mode="block_start", grace_min=3)
    cfg, db, led, eng, lk = build(raw, now=now)
    db.set_lock(enabled=1, commitment_end="2099-01-01")
    db.ensure_day(DAY)
    db.replace_blocks(DAY, [b.as_tuple() for b in planner.parse("16:00-22:00 work: essay")])
    db.run("UPDATE days SET plan_locked=1 WHERE date=?", (DAY.isoformat(),))
    eng.build_checkpoints(DAY, N(15, 59))
    return cfg, db, led, eng


def ask(cfg, db, led, eng, now):
    cfg.now = lambda: now
    app = build_app(cfg, eng, led)

    async def go():
        from aiohttp.test_utils import TestClient, TestServer
        async with TestClient(TestServer(app)) as c:
            r = await c.get("/agent/gap", params={"token": cfg.api["token"]})
            return (await r.text()).strip()
    return asyncio.get_event_loop().run_until_complete(go())


def told(db):
    return db.q("SELECT ts, payload FROM events WHERE kind='siren_told' ORDER BY id")


print("=== quiet is not logged ===")
cfg, db, led, eng = make(N(16, 0))
check("nothing owed yet", ask(cfg, db, led, eng, N(16, 0)), "0")
check("no row", len(told(db)), 0)

print("\n=== a sirening minute is logged once ===")
db.run("UPDATE checkpoints SET state='overdue', asked_at=? WHERE date=? AND due_at<=?",
       (N(16, 0).isoformat(), DAY.isoformat(), N(16, 5).isoformat()))
gap = ask(cfg, db, led, eng, N(16, 30))
check("the phone is told to sound", gap.isdigit() and int(gap) > 0, True)
rows = told(db)
check("one row", len(rows), 1)
check("it says why", "essay" in rows[0]["payload"] or "behind" in rows[0]["payload"], True)
check("and how far behind", "debt_min" in rows[0]["payload"], True)

for _ in range(5):
    ask(cfg, db, led, eng, N(16, 30))
check("five more polls in the same minute add nothing", len(told(db)), 1)
ask(cfg, db, led, eng, N(16, 31))
check("the next minute is its own row", len(told(db)), 2)

print("\n=== the question the 2am siren raised, answerable in one query ===")
cfg2, db2, led2, eng2 = make(N(2, 0))
db2.run("UPDATE checkpoints SET state='satisfied' WHERE date=?", (DAY.isoformat(),))
for h, m in ((1, 0), (2, 0), (2, 30), (3, 0)):
    check(f"{h:02d}:{m:02d} the server says quiet", ask(cfg2, db2, led2, eng2, N(h, m)), "0")
check("so the ledger has nothing to answer for", len(told(db2)), 0)

print("\n=== the log survives a restart, unlike anything in memory ===")
check("it is in the hash-chained ledger",
      db.one("SELECT COUNT(*) c FROM events WHERE kind='siren_told'")["c"], 2)
ok, bad = led.verify()
check("and the chain is still intact", (ok, bad), (True, None))

sys.exit(report("SIREN AUDIT TESTS"))
