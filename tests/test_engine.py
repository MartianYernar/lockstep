"""Simulated day: does the state machine actually behave?"""
import sys, os, tempfile
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fixtures import build, check, report
from server.ledger import Ledger
from server import planner


TZ = ZoneInfo("Asia/Almaty")
DAY = date(2026, 8, 25)
def T(h, m): return datetime(2026, 8, 25, h, m, tzinfo=TZ)

cfg, db, _led, eng, _lk = build()

def tick(t):
    """tick() as a live server sees it: 30 seconds since the last one, and a
    phone that is polling normally (otherwise the watchdog fires too)."""
    db.set_lock(last_tick_at=(t - timedelta(seconds=30)).isoformat(),
                last_agent_poll=(t - timedelta(seconds=20)).isoformat())
    return eng.tick(t)

db.set_lock(enabled=1, commitment_end="2026-11-25")
db.ensure_day(DAY)


print("\n=== 1. no plan at 16:00 => plan_missing + debt starts ===")
acts = tick(T(16, 0))
check("action kinds", [a.kind for a in acts], ["plan_missing"])
st = eng.agent_state(T(16, 30))
check("no-plan active", st.active, True)
check("no-plan debt", st.debt_minutes, 30)
check("no-plan headline", st.headline, "No plan submitted for today")

print("\n=== 2. submit plan, checkpoints materialise ===")
blocks = planner.parse("""16:00-16:40 commute to MNU library
16:40-19:00 work: linear algebra pset
19:00-19:30 gym
19:30-22:00 work: essay""")
planner.validate_coverage(blocks, 16*60, 22*60)
db.replace_blocks(DAY, [b.as_tuple() for b in blocks])
db.run("UPDATE days SET plan_locked=1, plan_submitted_at=? WHERE date=?", (T(15,0).isoformat(), DAY.isoformat()))
n = eng.build_checkpoints(DAY, T(15, 59))["made"]
check("checkpoints for 16:00-22:00 @15min", n, 24)
check("debt cleared once planned", eng.debt_seconds(DAY, T(16, 30)), 0)

print("\n=== 3. asking + grace ===")
acts = tick(T(16, 0))
check("asks at 16:00", [a.kind for a in acts], ["ask"])
check("asks for the right block", acts[0].payload["label"], "commute to MNU library")
check("silent at 16:03 (inside grace)", [a.kind for a in tick(T(16, 3))], [])
acts = tick(T(16, 6))
check("missed at 16:06 (grace blown)", [a.kind for a in acts], ["mark_missed"])

print("\n=== 4. escalation ladder (repetition grows, volume does not) ===")
# 16:06 is only 1 min behind — under phone_alarm_after_min, so silence.
for at, want_lvl, want_rep in [((16,6),0,0), ((16,11),0,10), ((16,24),0,10), ((16,25),1,12),
                                ((16,26),1,12), ((16,46),2,14), ((17,6),3,15)]:
    tick(T(*at))
    s = eng.agent_state(T(*at))
    check(f"{at[0]:02d}:{at[1]:02d} level", s.level, want_lvl)
    check(f"{at[0]:02d}:{at[1]:02d} alarm repeats", s.alarm_repeats, want_rep)
check("repeats capped", eng.agent_state(T(19, 0)).alarm_repeats, 15)
check("silent below the alarm threshold", eng.agent_state(T(16, 8)).phone_alarm, False)
check("...but the level is still tracked", eng.agent_state(T(16, 8)).active, True)
check("volume never escalates", eng.agent_state(T(19, 0)).alarm_volume_pct, 85)
check("phone alarm on after 5min", eng.agent_state(T(16, 12)).phone_alarm, True)

print("\n=== 5. one valid photo clears debt, older stay missed ===")
now = T(17, 10)
tick(now)
cp = eng.current_checkpoint(DAY, now)
check("current cp is the 17:00 one", cp["due_at"][11:16], "17:00")
res = eng.satisfy(DAY, now, cp["id"], '{"ok":true}', None)
check("older open ones swept to missed", res["swept"] >= 4, True)
check("debt is zero after valid photo", eng.debt_seconds(DAY, now), 0)
check("agent goes idle", eng.agent_state(now).active, False)
sc = eng.score(DAY, now)
print("   score:", sc)
check("one hit recorded", sc["hit"], 1)

print("\n=== 6. outside window is always idle ===")
check("15:59 idle", eng.agent_state(T(15, 59)).active, False)
check("22:30 idle", eng.agent_state(T(22, 30)).active, False)

print("\n=== 7. offset shifts the day ===")
before_end = eng.window(DAY)[1]
r = eng.apply_offset(DAY, T(18, 0), 60, "met homies after class")
check("offset stops at the 22:20 hard end, not 23:00", eng.window(DAY)[1], 22*60 + 20)
check("checkpoints excused", r["excused"] > 0, True)
# The count of "added" stopped meaning anything once apply_offset started
# clearing future checkpoints and rebuilding (it has to, or a shifted block
# leaves orphans at its old times). Assert the property that actually matters.
_cps = eng.checkpoints(DAY)
_last = max(datetime.fromisoformat(c["due_at"]) for c in _cps)
check("checkpoints reach the extended window", _last.hour * 60 + _last.minute >= 22*60, True)
check("no duplicate due times", len({c["due_at"] for c in _cps}), len(_cps))
check("nothing scheduled past the new window end",
      max(datetime.fromisoformat(c["due_at"]).hour * 60 +
          datetime.fromisoformat(c["due_at"]).minute for c in _cps) < eng.window(DAY)[1], True)
check("no debt during the offset hour", eng.debt_seconds(DAY, T(18, 40)), 0)
b = db.blocks(DAY)
check("gym block slid +60 (19:00->20:00)", [x["start_min"] for x in b if x["label"]=="gym"], [20*60])

print("\n=== 8. ledger tamper detection ===")
led = Ledger(db)
for k in ("plan_submitted", "checkpoint_satisfied", "offset_used"):
    led.append(k, {"day": DAY.isoformat()})
check("chain verifies", led.verify(), (True, None))
db.run("UPDATE events SET payload='{\"day\":\"1999-01-01\"}' WHERE id=2")
ok, bad = led.verify()
check("tamper detected", ok, False)
check("points at the edited row", bad, 2)

print("\n=== 9. outage amnesty: a reboot must not manufacture debt ===")
cfg2, db2, led2, eng2, _ = build()
db2.set_lock(enabled=1, commitment_end="2099-01-01")
db2.ensure_day(DAY)
bl = planner.parse("16:00-22:00 work: essay")
db2.replace_blocks(DAY, [b.as_tuple() for b in bl])
db2.run("UPDATE days SET plan_locked=1 WHERE date=?", (DAY.isoformat(),))
eng2.build_checkpoints(DAY, T(15, 59))

eng2.tick(T(16, 0))                       # normal tick; records last_tick_at
check("first tick asks", len(eng2.checkpoints(DAY)), 24)
db2.set_lock(last_tick_at=T(16, 2).isoformat())   # ...then the server dies

acts = eng2.tick(T(17, 32))               # 90 minutes later it comes back
kinds = [a.kind for a in acts]
check("amnesty raised on restart", "outage_amnesty" in kinds, True)
am = next(a for a in acts if a.kind == "outage_amnesty").payload
check("outage length reported", am["minutes"], 90)
check("the whole gap is voided", am["excused"], 7)   # 16:00 through 17:30
check("no debt manufactured by the outage", eng2.debt_seconds(DAY, T(17, 32)), 0)
check("agent stays idle after a reboot", eng2.agent_state(T(17, 32)).active, False)
check("no flood of backlogged asks", len([k for k in kinds if k == "ask"]) <= 1, True)

st = {c["state"] for c in eng2.checkpoints(DAY)
      if T(16, 0) <= datetime.fromisoformat(c["due_at"]) < T(17, 30)}
check("gap checkpoints are excused, not missed", st, {"excused"})
check("excused never count against the score", eng2.score(DAY, T(17, 32))["missed"], 0)

eng2.tick(T(17, 33))                      # a normal 1-minute tick after recovery
check("no amnesty on a normal tick",
      "outage_amnesty" in [a.kind for a in eng2.tick(T(17, 34))], False)

print("\n=== 10. the tick runs every 30s — nothing may flood ===")
cfg3, db3, led3, eng3, _ = build()
db3.set_lock(enabled=1, commitment_end="2026-08-25")   # already expired
db3.ensure_day(DAY)
counts = {}
t = T(16, 0)
for _ in range(120):                                   # one full hour of ticks
    db3.set_lock(last_tick_at=(t - timedelta(seconds=30)).isoformat())
    for a in eng3.tick(t):
        counts[a.kind] = counts.get(a.kind, 0) + 1
    t += timedelta(seconds=30)
check("plan_missing throttled to 1 per 10 min", counts.get("plan_missing"), 6)
check("commitment_over fires exactly once ever", counts.get("commitment_over"), 1)

# plan nags: after the window, at each configured nag time, once each.
cfg4, db4, led4, eng4, _ = build()
db4.set_lock(enabled=1, commitment_end="2099-01-01")
db4.ensure_day(DAY)
# Today must be planned, or tick() returns early on plan_missing and never
# reaches the nag for tomorrow. Tomorrow stays unplanned, which is the point.
bl4 = planner.parse("16:00-22:00 work: essay")
db4.replace_blocks(DAY, [b.as_tuple() for b in bl4])
db4.run("UPDATE days SET plan_locked=1 WHERE date=?", (DAY.isoformat(),))
eng4.build_checkpoints(DAY, T(15, 59))
check("tomorrow is unplanned", eng4.has_plan(DAY + timedelta(days=1)), False)
nags = 0
t = T(19, 58)
for _ in range(20):                                    # 19:58 -> 20:03
    db4.set_lock(last_tick_at=(t - timedelta(seconds=30)).isoformat())
    nags += len([a for a in eng4.tick(t) if a.kind == "plan_nag"])
    t += timedelta(seconds=30)
check("one plan_nag per configured nag time", nags, 1)

print("\n=== 11. a plan submitted mid-window cannot create instant debt ===")
# Exactly the bug from the live run: plan sent at 18:56 for a window that
# opened at 16:00 produced demands for 16:34 and 18:26 and two hours of debt.
cfg6, db6, led6, eng6, _ = build()
db6.set_lock(enabled=1, commitment_end="2099-01-01")
db6.ensure_day(DAY)
bl6 = planner.parse("16:00-19:00 work: linear algebra pset\n19:00-22:00 work: essay")
db6.replace_blocks(DAY, [b.as_tuple() for b in bl6])
db6.run("UPDATE days SET plan_locked=1 WHERE date=?", (DAY.isoformat(),))

late = T(18, 56)
built = eng6.build_checkpoints(DAY, late)
check("past slots were voided", built["voided"] > 0, True)
check("some slots remain live", built["live"] > 0, True)
past_states = {c["state"] for c in eng6.checkpoints(DAY)
               if datetime.fromisoformat(c["due_at"]) <= late}
check("every past slot is excused, none scheduled", past_states, {"excused"})
check("no instant debt", eng6.debt_seconds(DAY, late), 0)
check("agent stays quiet", eng6.agent_state(late).active, False)

acts6 = [a.kind for a in eng6.tick(T(18, 57))]
check("no retroactive asks", "ask" in acts6, False)
check("no retroactive misses", "mark_missed" in acts6, False)
check("voided slots never count against the score", eng6.score(DAY, late)["missed"], 0)
check("first live checkpoint is in the future",
      datetime.fromisoformat(built["first"]) > late, True)

print("\n=== 12. a silent phone is not the same as a compliant one ===")
cfg7, db7, led7, eng7, _ = build()
db7.set_lock(enabled=1, commitment_end="2099-01-01")
db7.ensure_day(DAY)
bl7 = planner.parse("16:00-22:00 work: essay")
db7.replace_blocks(DAY, [b.as_tuple() for b in bl7])
db7.run("UPDATE days SET plan_locked=1 WHERE date=?", (DAY.isoformat(),))
eng7.build_checkpoints(DAY, T(15, 59))

def tick7(t, poll_ago_min=None):
    db7.set_lock(last_tick_at=(t - timedelta(seconds=30)).isoformat())
    if poll_ago_min is not None:
        db7.set_lock(last_agent_poll=(t - timedelta(minutes=poll_ago_min)).isoformat())
    return eng7.tick(t)

kinds = [a.kind for a in tick7(T(16, 30), poll_ago_min=0.5)]
check("healthy phone raises nothing", "phone_silent" in kinds, False)

# A phone that has NEVER polled is an unfinished setup, not a fault.
cfg8, db8, led8, eng8, _ = build()
db8.set_lock(enabled=1, commitment_end="2099-01-01", last_agent_poll=None)
db8.ensure_day(DAY)
db8.replace_blocks(DAY, [b.as_tuple() for b in planner.parse("16:00-22:00 work: essay")])
db8.run("UPDATE days SET plan_locked=1 WHERE date=?", (DAY.isoformat(),))
eng8.build_checkpoints(DAY, T(15, 59))
db8.set_lock(last_tick_at=T(17, 59, ).isoformat())
check("never-installed agent is not nagged about",
      "phone_silent" in [a.kind for a in eng8.tick(T(18, 0))], False)

acts = tick7(T(16, 45), poll_ago_min=12)
sil = [a for a in acts if a.kind == "phone_silent"]
check("12 min of silence is noticed", len(sil), 1)
check("reports how long", sil[0].payload["minutes"], 12)
check("short silence does not escalate to the guardian", sil[0].payload["escalate"], False)

acts = tick7(T(17, 30), poll_ago_min=75)
sil = [a for a in acts if a.kind == "phone_silent"]
check("long silence escalates", sil[0].payload["escalate"], True)

# and it must not repeat every 30 seconds
n = 0
for i in range(20):
    t = T(17, 31) + timedelta(seconds=30 * i)
    n += len([a for a in tick7(t, poll_ago_min=80) if a.kind == "phone_silent"])
check("throttled, not a flood", n <= 1, True)

print("\n=== a plan may reach outside the usual hours, and is enforced there ===")
# The point: an hour of work before school is proof-bearing like any other
# block. It used to be accepted, shown in /today, and then silently never
# checked, because the window clamped it out of existence.
import copy as _copy
from fixtures import RAW as _RAW
_raw = _copy.deepcopy(_RAW)
_raw["schedule"].update({"enforce_start": "16:10", "enforce_end": "22:00",
                         "mode": "block_start", "earliest": "06:00"})
cfg, db, led, eng, lk = build(_raw)
day = cfg.today()
db.ensure_day(day)
check("window with no plan is the configured one", eng.window(day), (16 * 60 + 10, 22 * 60))

db.replace_blocks(day, [
    (7 * 60, 8 * 60, "work: pset before school", "work"),
    (16 * 60 + 10, 22 * 60, "work: essay", "work"),
])
check("window stretches back to the early block", eng.window(day)[0], 7 * 60)
check("window end is unchanged", eng.window(day)[1], 22 * 60)
check("07:30 now counts as inside the window",
      eng.in_window(datetime(day.year, day.month, day.day, 7, 30, tzinfo=cfg.tz)), True)
check("06:30 is still outside it",
      eng.in_window(datetime(day.year, day.month, day.day, 6, 30, tzinfo=cfg.tz)), False)

built = eng.build_checkpoints(day, datetime(day.year, day.month, day.day, 6, 0, tzinfo=cfg.tz))
early = db.q("SELECT * FROM checkpoints WHERE date=? AND due_at < ? ORDER BY due_at",
             (day.isoformat(), f"{day.isoformat()}T09:00:00"))
check("the early block produces checkpoints", len(early) > 0, True)
check("first one is at 07:00", early[0]["due_at"][11:16], "07:00")
check("and it asks for blocker proof", early[0]["kind"], "blocker_toggle")

print("\n=== a replan does not orphan the checkpoints it leaves behind ===")
# replace_blocks is DELETE + INSERT, so every rewrite issues fresh block ids.
# Checkpoints used to keep the old ones and point at rows that no longer
# existed: no label to ask for, and nothing for the score to attribute to.
db.replace_blocks(day, [(16 * 60, 18 * 60, "work: physics", "work")])
eng.build_checkpoints(day, datetime(day.year, day.month, day.day, 15, 0, tzinfo=cfg.tz))
before = db.q("SELECT id, block_id FROM checkpoints WHERE date=? AND block_id IS NOT NULL",
              (day.isoformat(),))
check("checkpoints were attached to the first set of blocks", len(before) > 0, True)
old_ids = {r["block_id"] for r in before}

db.replace_blocks(day, [(16 * 60, 18 * 60, "work: physics rewritten", "work")])
new_ids = {b["id"] for b in db.blocks(day)}
check("the rewrite really did issue new block ids", old_ids & new_ids, set())

live = db.q("SELECT block_id FROM checkpoints WHERE date=? AND block_id IS NOT NULL",
            (day.isoformat(),))
check("every checkpoint still points at a block that exists",
      all(r["block_id"] in new_ids for r in live), True)
check("none were lost in the move", len(live), len(before))

print("\n=== a checkpoint whose block was planned away is detached, not left dangling ===")
db.replace_blocks(day, [(20 * 60, 21 * 60, "work: somewhere else entirely", "work")])
stray = db.q("SELECT block_id FROM checkpoints WHERE date=? AND due_at LIKE ?",
             (day.isoformat(), f"{day.isoformat()}T16:%"))
check("16:00 checkpoints no longer claim a block", all(r["block_id"] is None for r in stray), True)

print("\n=== but sleep is protected at both ends ===")
db.replace_blocks(day, [(4 * 60, 5 * 60, "work: absurdly early", "work"),
                        (16 * 60 + 10, 23 * 60 + 30, "work: absurdly late", "work")])
s, e = eng.window(day)
check("a 04:00 block cannot drag the day earlier than 06:00", s, 6 * 60)
check("a 23:30 block cannot push past the hard end 22:20", e, 22 * 60 + 20)

sys.exit(report("ENGINE TESTS"))
