"""Shared test fixture: a config that doesn't need a real config.yaml."""
import os, sys, tempfile
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

RAW = {
    "timezone": "Asia/Almaty",
    "telegram": {"bot_token": "x", "user_chat_id": 1, "guardian_chat_id": 2},
    "gemini": {"api_key": "x", "model": "gemini-2.5-flash"},
    "schedule": {"enforce_start": "16:00", "enforce_end": "22:00", "mode": "fixed",
                 "checkpoint_interval_min": 15, "grace_min": 5, "plan_due": "23:30",
                 "plan_nag_times": ["20:00"], "daily_report_at": "22:15"},
    "commitment": {"start": "2026-08-25", "months": 3, "total_months": 12},
    "offset": {"max_per_day": 1, "minutes": 60, "require_photo": True},
    "escalation": {"step_minutes": 20, "phone_poll_sec": 60, "phone_alarm_after_min": 5,
                   "phone_alarm_volume_pct": 55, "phone_repeats_base": 1,
                   "phone_repeats_per_step": 1, "phone_repeats_max": 4},
    "vision": {"timestamp_tolerance_min": 12, "min_confidence": 0.55, "max_retries": 3},
    "api": {"host": "127.0.0.1", "port": 8765, "token": "t"},
}

FAILS = []


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}" + ("" if ok else f" want {want!r}"))
    if not ok:
        FAILS.append(name)
    return ok


def build(raw=None):
    """Fresh config + db + ledger + engine + lock on a throwaway directory."""
    from server.config import Config
    from server.db import Database
    from server.ledger import Ledger
    from server.engine import Engine
    from server.lock import Lock

    tmp = tempfile.mkdtemp()
    cpath = os.path.join(tmp, "config.yaml")
    with open(cpath, "w") as fh:
        yaml.safe_dump(raw or RAW, fh)
    cfg = Config.load(cpath)
    db = Database(os.path.join(tmp, "t.db"))
    led = Ledger(db)
    return cfg, db, led, Engine(cfg, db, led), Lock(cfg, db, led)


def spot_raw(**over):
    """RAW, but in spot-check mode."""
    import copy
    r = copy.deepcopy(RAW)
    r["schedule"]["mode"] = "spot_check"
    r["schedule"]["spot_check_avg_min"] = over.get("avg", 45)
    r["schedule"]["spot_check_min_gap_min"] = over.get("gap", 20)
    return r


def report(title):
    print("\n" + (f"ALL {title} PASSED" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
    return 1 if FAILS else 0
