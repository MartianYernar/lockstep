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

print("\n=== guardian enrolment ===")
check("starts unconfigured", lk.is_configured, False)
tok = lk.new_setup_token()

try:
    lk.complete_setup(tok, "correcthorse", from_chat_id=1)     # the USER's chat id
    check("user cannot enrol themselves", "no error", "LockError")
except LockError as e:
    check("user cannot enrol themselves", "rejected", "rejected")

try:
    lk.complete_setup("BADCODE", "correcthorse", from_chat_id=2)
    check("wrong setup code rejected", "no error", "LockError")
except LockError:
    check("wrong setup code rejected", "rejected", "rejected")

try:
    lk.complete_setup(tok, "short", from_chat_id=2)
    check("short password rejected", "no error", "LockError")
except LockError:
    check("short password rejected", "rejected", "rejected")

lk.complete_setup(tok, "correcthorsebattery", from_chat_id=2)
check("guardian enrolled", lk.is_configured, True)
check("password not stored in clear",
      "correcthorsebattery" in json.dumps(dict(db.lock())), False)
check("hash is bcrypt", db.lock()["guardian_hash"].startswith("$2b$"), True)

try:
    lk.complete_setup(lk.new_setup_token(), "another", from_chat_id=2)
    check("cannot re-enrol once set", "no error", "LockError")
except LockError:
    check("cannot re-enrol once set", "rejected", "rejected")

print("\n=== password verification and lockout ===")
check("correct password", lk.verify("correcthorsebattery", NOW), True)
check("wrong password", lk.verify("nope", NOW), False)
check("wrong password 2", lk.verify("nope2", NOW), False)
check("wrong password 3", lk.verify("nope3", NOW), False)
try:
    lk.verify("correcthorsebattery", NOW)
    check("locked out after 3 fails", "no error", "LockError")
except LockError as e:
    check("locked out after 3 fails", "rejected", "rejected")
    print(f"        message: {e}")
later = NOW + timedelta(minutes=31)
check("lockout expires after 30 min", lk.verify("correcthorsebattery", later), True)
check("counter reset on success", db.lock()["failed_attempts"], 0)

print("\n=== commitment window ===")
check("commitment end recorded", db.lock()["commitment_end"], "2026-11-25")
check("active during period", lk.commitment_active(date(2026, 9, 1)), True)
check("over after period", lk.commitment_active(date(2026, 12, 1)), False)
check("days remaining", lk.days_remaining(date(2026, 8, 25)), 92)
check("extend pushes it out", lk.extend_commitment(3, date(2026, 8, 25)).isoformat(), "2027-02-25")

print("\n=== disable is recorded ===")
lk.disable("had to travel for a family thing", NOW)
check("disabled", lk.enabled, False)
kinds = [r["kind"] for r in db.q("SELECT kind FROM events ORDER BY id")]
check("system_disabled in ledger", "system_disabled" in kinds, True)
check("all 6 attempts logged, lockout included", kinds.count("password_attempt"), 6)
lk.enable(NOW)
check("re-enable needs no password", lk.enabled, True)
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
              ["active", "alarm_volume_pct", "cycle_sec", "debt_minutes", "detail",
               "headline", "level", "phone_alarm", "windows"])
        r = await client.get("/agent/state?token=t")
        check("query-string token works (for Tasker)", r.status, 200)
        r = await client.get("/health")
        h = await r.json()
        check("health reports ledger intact", h["ledger_intact"], True)
        r = await client.post("/agent/ack", json={"agent": "macos", "opened": 6, "level": 1},
                              headers={"X-Lockstep-Token": "t"})
        check("ack accepted", r.status, 200)
asyncio.run(api_tests())

sys.exit(report("LOCK/API TESTS"))
