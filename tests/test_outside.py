"""/outside -- leaving the wifi without the phone screaming about it.

The phone has no mobile data, so being out of the house and switching the wifi
off to dodge a checkpoint look identical from the agent's side: no connection.
The agent is offline by then and cannot ask anything, so the answer has to be
handed over in advance and remembered. These tests pin the two halves of that:
who gets granted an excuse, and what the phone is told before it goes dark.
"""
import asyncio
import os
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fixtures import build, check, report, RAW
from server import planner
from server.api import build_app

TZ = ZoneInfo("Asia/Almaty")
DAY = date(2026, 8, 25)          # a Tuesday
PLAN = """16:00-18:00 work: hackathon website
18:00-18:30 sport spine training
18:30-22:00 work: analysing"""


def N(h, m, d=25):
    return datetime(2026, 8, d, h, m, tzinfo=TZ)


def make(**over):
    import copy
    raw = copy.deepcopy(RAW)
    raw["outside"] = {"minutes": 90, "max_per_day": 2, **over}
    cfg, db, led, eng, lk = build(raw)
    db.set_lock(enabled=1, commitment_end="2099-01-01")
    db.ensure_day(DAY)
    blocks = planner.parse(PLAN)
    db.replace_blocks(DAY, [b.as_tuple() for b in blocks])
    db.run("UPDATE days SET plan_locked=1 WHERE date=?", (DAY.isoformat(),))
    return cfg, db, led, eng


print("=== the excuse runs for exactly as long as it was granted ===")
cfg, db, led, eng = make()
check("nothing granted, nothing active", eng.outside_active(N(17, 0)), False)
until = eng.grant_outside(N(17, 0), 90)
check("granted at 17:00 runs to 18:30", until.strftime("%H:%M"), "18:30")
check("active at 17:00", eng.outside_active(N(17, 0)), True)
check("active at 18:29", eng.outside_active(N(18, 29)), True)
check("expired at 18:30", eng.outside_active(N(18, 30)), False)
check("expired at 19:00", eng.outside_active(N(19, 0)), False)

print("\n=== a trip granted late at night survives midnight ===")
cfg2, db2, led2, eng2 = make()
eng2.grant_outside(N(23, 30), 90)
check("still out at 00:30 the next day", eng2.outside_active(N(0, 30, 26)), True)
check("done by 01:30", eng2.outside_active(N(1, 30, 26)), False)

print("\n=== the daily cap counts grants, not minutes ===")
cfg3, db3, led3, eng3 = make()
check("starts at zero", eng3.outside_used(DAY), 0)
eng3.grant_outside(N(16, 30), 90)
check("one used", eng3.outside_used(DAY), 1)
eng3.grant_outside(N(18, 30), 90)
check("two used", eng3.outside_used(DAY), 2)

print("\n=== what the phone is told, which is all it will remember ===")


def gap(eng, cfg, led, now):
    """Drive the real endpoint, with the clock pinned."""
    cfg.now = lambda: now
    app = build_app(cfg, eng, led)

    async def go():
        from aiohttp.test_utils import TestClient, TestServer
        async with TestClient(TestServer(app)) as client:
            r = await client.get("/agent/gap", params={"token": cfg.api["token"]})
            return await r.text()
    return asyncio.get_event_loop().run_until_complete(go())


cfg4, db4, led4, eng4 = make()
eng4.build_checkpoints(DAY, N(15, 59))
check("caught up, in the window -> 0", gap(eng4, cfg4, led4, N(16, 0)), "0")

eng4.grant_outside(N(16, 30), 90)
check("out and in the window -> -1", gap(eng4, cfg4, led4, N(16, 31)), "-1")
check("still -1 near the end", gap(eng4, cfg4, led4, N(17, 59)), "-1")
check("back to normal once it lapses", gap(eng4, cfg4, led4, N(18, 5)) != "-1", True)

print("\n=== an afternoon walk cannot buy silence at bedtime ===")
cfg5, db5, led5, eng5 = make()
eng5.build_checkpoints(DAY, N(15, 59))
# Granted at 21:30 for 90 minutes, so it is still "active" at 22:30 -- but the
# enforcement window has shut by then and bedtime is its own siren.
eng5.grant_outside(N(21, 30), 90)
check("still technically active at 22:30", eng5.outside_active(N(22, 30)), True)
check("but the window is shut", eng5.in_window(N(22, 30)), False)
check("so the phone is NOT told -1", gap(eng5, cfg5, led5, N(22, 30)) != "-1", True)

print("\n=== being behind does not stop you going out, and vice versa ===")
cfg6, db6, led6, eng6 = make()
eng6.build_checkpoints(DAY, N(15, 59))
db6.run("UPDATE checkpoints SET state='overdue', asked_at=? WHERE date=? AND due_at<=?",
        (N(16, 0).isoformat(), DAY.isoformat(), N(16, 30).isoformat()))
behind = gap(eng6, cfg6, led6, N(16, 40))
check("behind, with no excuse, the phone is told to sound", behind not in ("0", "-1"), True)
eng6.grant_outside(N(16, 41), 90)
check("with the excuse it goes quiet", gap(eng6, cfg6, led6, N(16, 42)), "-1")
still = db6.one("SELECT COUNT(*) c FROM checkpoints WHERE date=? AND state='overdue'",
                (DAY.isoformat(),))["c"]
check("but the overdue checkpoints are still overdue", still > 0, True)

sys.exit(report("OUTSIDE TESTS"))
