"""Lock behaviour and the agent API, without touching Telegram."""
import asyncio, os, sys, tempfile, json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fixtures import build, check, report
from server.lock import LockError
from server.api import build_app


cfg, db, led, eng, lk = build()
TZ = ZoneInfo("Asia/Almaty")
NOW = datetime(2026, 8, 25, 12, 0, tzinfo=TZ)

print("\n=== who holds the switch — an address, not a secret ===")
cfg.raw["telegram"]["guardian_chat_id"] = 0     # nothing configured yet
check("nobody registered at first", lk.guardian_id(), 0)
check("...so nothing is holding it shut", lk.is_configured, False)

lk.register_guardian(2)
check("guardian registered", lk.guardian_id(), 2)
check("configured", lk.is_configured, True)
check("no password stored anywhere",
      any("password" in str(v).lower() or "hash" in str(k).lower()
          for k, v in dict(db.lock()).items() if v), False)

print("\n=== the commitment is one command, no code and no secret ===")
end_date = lk.start_commitment(3, date(2026, 8, 25))
check("runs three months out", end_date.isoformat(), "2026-11-25")
check("active during", lk.commitment_active(date(2026, 9, 1)), True)
check("over after", lk.commitment_active(date(2026, 12, 1)), False)
check("days remaining", lk.days_remaining(date(2026, 8, 25)), 92)
check("recorded", "commitment_started" in [r["kind"] for r in db.q("SELECT kind FROM events")], True)
check("extend pushes it out",
      lk.extend_commitment(3, date(2026, 8, 25)).isoformat(), "2027-02-25")

print("\n=== nothing survives a restart that could go stale ===")
# The old design mailed a fresh setup code on every restart, which invalidated
# the one already sitting in the guardian's inbox. There is no code any more.
check("no setup token concept at all", hasattr(lk, "new_setup_token"), False)
check("no password verification either", hasattr(lk, "verify"), False)

print("\n=== months: 0 means no lock, not an expired one ===")
import copy
from fixtures import RAW
trial = copy.deepcopy(RAW); trial["commitment"]["months"] = 0
tcfg, tdb, tled, teng, tlk = build(trial)
check("no commitment end computed", tcfg.commitment_end(), None)
tlk.register_guardian(2)
check("registering a guardian does not start the clock", tdb.lock()["commitment_end"], None)
check("commitment not active", tlk.commitment_active(date(2026, 8, 25)), False)
from datetime import datetime as _dt
from zoneinfo import ZoneInfo as _Z
kinds0 = [a.kind for a in teng.tick(_dt(2026, 8, 25, 12, 0, tzinfo=_Z("Asia/Almaty")))]
check("no bogus 'commitment over' message", "commitment_over" in kinds0, False)

print("\n=== disable is recorded ===")
lk.disable("had to travel for a family thing", NOW)
check("disabled", lk.enabled, False)
kinds = [r["kind"] for r in db.q("SELECT kind FROM events ORDER BY id")]
check("system_disabled in ledger", "system_disabled" in kinds, True)

lk.enable(NOW)
check("re-enable is a plain state change", lk.enabled, True)
check("ledger still verifies", led.verify(), (True, None))

print("\n=== agent API ===")
async def api_tests():
    from aiohttp.test_utils import TestClient, TestServer
    app = build_app(cfg, eng, led)
    async with TestClient(TestServer(app)) as client:
        r = await client.get("/agent/state")
        check("no token -> 401", r.status, 401)
        r = await client.get("/agent/state", headers={"X-Lockstep-Token": "wrong"})
        check("bad token -> 401", r.status, 401)
        r = await client.get("/agent/state", headers={"X-Lockstep-Token": "t"})
        check("good token -> 200", r.status, 200)
        body = await r.json()
        check("state has the agent contract",
              sorted(body.keys()),
              ["active", "alarm_repeats", "alarm_volume_pct", "apps_blocked",
               "debt_minutes", "detail", "headline", "level", "phone_alarm",
               "poll_sec"])
        r = await client.get("/agent/state?token=t")
        check("query-string token works (for Tasker)", r.status, 200)
        r = await client.get("/health")
        h = await r.json()
        check("health reports ledger intact", h["ledger_intact"], True)
        r = await client.post("/agent/ack", json={"agent": "macos", "opened": 6, "level": 1},
                              headers={"X-Lockstep-Token": "t"})
        check("ack accepted", r.status, 200)

        # The JSON route needs the phone to parse a body, build a dictionary
        # and test a field. That failed silently on the real phone -- the parse
        # returns the string "true", the If compares a boolean, nothing fires
        # and nothing errors. This one is a substring test.
        r = await client.get("/agent/siren")
        check("siren needs a token too", r.status, 401)
        r = await client.get("/agent/siren?token=t")
        check("plain-text siren -> 200", r.status, 200)
        check("...and it is text, not json", r.content_type, "text/plain")
        txt = await r.text()
        parts = txt.split()
        check("one line, no braces to parse", "{" in txt, False)
        check("says one of exactly two things", parts[0] in ("ALARM", "QUIET"), True)
        if parts[0] == "ALARM":
            check("...and carries volume and repeats", len(parts), 3)
            check("both are plain integers",
                  all(p.isdigit() for p in parts[1:]), True)
        # The phone only has to ask: does this text start with ALARM.
        check("a substring test is enough", txt.startswith("ALARM") or txt == "QUIET", True)

        # /agent/gap: the escalation ladder lives on the server, and the phone
        # only reads a number and sleeps for it. Nothing to parse means nothing
        # to get wrong -- the JSON version failed silently for a week.
        r = await client.get("/agent/gap")
        check("gap needs a token", r.status, 401)
        r = await client.get("/agent/gap?token=t")
        check("gap answers 200", r.status, 200)
        txt = (await r.text()).strip()
        check("it is a bare integer, nothing else", txt.isdigit(), True)
        n = int(txt)
        check("0 means quiet, anything else is seconds",
              n == 0 or 3 <= n <= 60, True)
        check("never below 3s — a tone needs room to finish", n == 0 or n >= 3, True)

print("\n=== $PORT wins, so a platform-assigned port is actually bound ===")
# Railway, Render and friends hand the container a port at boot and route only
# to that. Binding the configured 8765 means their router never finds the app:
# the deploy reports healthy and the phone gets nothing.
import os as _os
check("config wins when $PORT is unset", cfg.api_port, 8765)
_os.environ["PORT"] = "34567"
try:
    check("$PORT wins when the platform sets it", cfg.api_port, 34567)
finally:
    del _os.environ["PORT"]
check("and config is back once it is gone", cfg.api_port, 8765)
asyncio.run(api_tests())

sys.exit(report("LOCK/API TESTS"))
