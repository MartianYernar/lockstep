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

print("\n=== a work block wants one screenshot: the block running ===")
# It was three, then two, now one. The scope shot went because Cold Turkey's
# real UI does not present its allowlist the way the validation assumed, and a
# proof you cannot reliably produce is worth less than no proof at all -- it
# only teaches you to ignore the bot.
work = [r["kind"] for r in rows if r["due_at"][11:16] in ("16:00", "16:01", "16:02")]
check("one shot: the block running", work, ["blocker_toggle"])
check("no scope shot is demanded", "blocker_allow" in [r["kind"] for r in rows], False)
check("nor the old third one", "blocker_block" in [r["kind"] for r in rows], False)

print("\n=== a non-work block wants the activity, and only that ===")
sport = [r["kind"] for r in rows
         if r["due_at"][11:16] in ("18:00", "18:01", "18:02", "18:03")]
check("sport wants the body, not a website blocker", sport, ["activity"])
shower = [r["kind"] for r in rows
          if r["due_at"][11:16] in ("19:00", "19:01", "19:02", "19:03")]
check("one for shower too", len(shower), 1)
check("...and it is the activity", shower[-1], "activity")

essay = [r["kind"] for r in rows
         if r["due_at"][11:16] in ("18:30", "18:31", "18:32", "18:33")]
check("but only one for a work block", len(essay), 1)
check("no activity photo demanded at a screen", "activity" in essay, False)

print("\n=== the day is far quieter than spot checks ===")
check("five blocks", len(db.blocks(DAY)), 5)
check("5 blocks + the bed photo = 6, down from 17", built["made"], 6)
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
check("no screenshots demanded at all", kinds_off, {"activity", "bed"})
check("one photo per block, work or not, plus the bed photo", made["made"], 6)
check("...the same count either way now: one photo per block", made["made"], built["made"])

print("\n=== a plan written late still cannot demand the past ===")
cfg2, db2, led2, eng2 = make()
late = datetime(2026, 8, 25, 18, 45, tzinfo=TZ)
b2 = eng2.build_checkpoints(DAY, late)
states = {r["state"] for r in eng2.checkpoints(DAY)
          if datetime.fromisoformat(r["due_at"]) <= late}
check("everything already past is void", states, {"excused"})
check("and some proof still lies ahead", b2["live"] > 0, True)

print("\n=== 'workout' is not 'work:' ===")
# This tested startswith("work"), so a block called "workout outside" counted as
# screen work and was asked for a Cold Turkey screenshot of a gym session.
import copy as _cw
_raw = _cw.deepcopy(RAW)
_raw["schedule"]["mode"] = "block_start"
_raw["proof"] = {"shot_gap_min": 1}
_raw["vision"] = dict(_raw["vision"], require_blocker_for_work=True)
cfgw, dbw, ledw, engw, lkw = build(_raw)
dbw.ensure_day(DAY)
dbw.replace_blocks(DAY, [
    (16 * 60, 17 * 60, "workout outside", "sport"),
    (17 * 60, 18 * 60, "workshop with the robotics club", "other"),
    (18 * 60, 19 * 60, "working out what to do", "other"),
    (19 * 60, 20 * 60, "work: physics", "work"),
    (20 * 60, 22 * 60, "work:essay", "work"),
])
dbw.run("UPDATE days SET plan_locked=1 WHERE date=?", (DAY.isoformat(),))
engw.build_checkpoints(DAY, BEFORE)
got = {r["label"]: r["kind"] for r in engw.checkpoints(DAY) if r["kind"] != "bed"}
check("'workout outside' wants the body", got["workout outside"], "activity")
check("'workshop ...' too", got["workshop with the robotics club"], "activity")
check("'working out what to do' too", got["working out what to do"], "activity")
check("'work: physics' wants the blocker", got["work: physics"], "blocker_toggle")
check("'work:essay' too — no space needed", got["work:essay"], "blocker_toggle")

print("\n=== proof every N minutes inside a block, not only at the top ===")
# A block-start shot proves a session was started. It says nothing about the
# ninety minutes after it.
import copy as _cwb
_wb = _cwb.deepcopy(RAW)
_wb["schedule"]["mode"] = "block_start"
_wb["proof"] = {"shot_gap_min": 1, "within_block_min": 20}
_wb["vision"] = dict(_wb["vision"], require_blocker_for_work=True)
cfg8, db8, led8, eng8, lk8 = build(_wb)
db8.ensure_day(DAY)
db8.replace_blocks(DAY, [(16 * 60, 18 * 60, "work: pset", "work"),
                         (18 * 60, 18 * 60 + 30, "gym", "sport"),
                         (18 * 60 + 30, 22 * 60, "work: essay", "work")])
db8.run("UPDATE days SET plan_locked=1 WHERE date=?", (DAY.isoformat(),))
eng8.build_checkpoints(DAY, BEFORE)
rows8 = [r for r in eng8.checkpoints(DAY) if r["kind"] != "bed"]
times = [r["due_at"][11:16] for r in rows8]

check("top of the 16:00 block still asks for the blocker",
      [r["kind"] for r in rows8 if r["due_at"][11:16] == "16:00"], ["blocker_toggle"])
check("then every 20 min inside it",
      [t for t in times if t in ("16:20", "16:40", "17:00", "17:20", "17:40")],
      ["16:20", "16:40", "17:00", "17:20", "17:40"])
check("nothing at 18:00 from the first block — that is the next block's own top",
      len([r for r in rows8 if r["due_at"][11:16] == "18:00"]), 1)
check("the in-block ones ask for the activity, not another screenshot",
      {r["kind"] for r in rows8 if r["due_at"][11:16] in ("16:20", "17:40")}, {"activity"})
check("a 30-min gym block gets its top plus one at +20",
      sorted(t for t in times if t in ("18:00", "18:20")), ["18:00", "18:20"])
check("none land past the window", max(times) < "22:00", True)

print("\n=== and omitting it keeps block-starts only ===")
_off2 = _cwb.deepcopy(RAW)
_off2["schedule"]["mode"] = "block_start"
_off2["proof"] = {"shot_gap_min": 1}
_off2["vision"] = dict(_off2["vision"], require_blocker_for_work=True)
cfg9, db9, led9, eng9, lk9 = build(_off2)
db9.ensure_day(DAY)
db9.replace_blocks(DAY, [(16 * 60, 18 * 60, "work: pset", "work")])
db9.run("UPDATE days SET plan_locked=1 WHERE date=?", (DAY.isoformat(),))
eng9.build_checkpoints(DAY, BEFORE)
check("one checkpoint for a two-hour block",
      len([r for r in eng9.checkpoints(DAY) if r["kind"] != "bed"]), 1)

print("\n=== a block starting right now is checked, not written off ===")
# Replanning at 17:06 into a block running 17:06-17:20 produced a checkpoint
# born void, so the one block he had just decided to do went unchecked.
cfg7, db7, led7, eng7 = make()
NOW = datetime(2026, 8, 25, 17, 6, tzinfo=TZ)
db7.replace_blocks(DAY, [
    (16 * 60, 17 * 60 + 6, "work: the block that was truncated", "work"),
    (17 * 60 + 6, 17 * 60 + 20, "dress up, eat", "other"),
    (17 * 60 + 20, 22 * 60, "work: uni research", "work"),
])
eng7.build_checkpoints(DAY, NOW)
at_now = [r for r in eng7.checkpoints(DAY) if r["due_at"][11:16] == "17:06"]
check("the block starting now gets a checkpoint", len(at_now), 1)
check("...and it is LIVE, not written off", at_now[0]["state"], "scheduled")
check("...and it asks for the right thing", at_now[0]["kind"], "activity")

old = [r for r in eng7.checkpoints(DAY) if r["due_at"][11:16] == "16:00"]
check("but 16:00, an hour gone, is still void", old[0]["state"], "excused")

print("\n=== the day ends with proof of going to bed ===")
# The last block of the evening was the one nobody ever checked: the window shut
# at 22:00 and whatever happened after was invisible.
cfg5, db5, led5, eng5 = make()
eng5.build_checkpoints(DAY, BEFORE)
bed = eng5.bed_checkpoint(DAY)
check("a bed checkpoint exists", bed is not None, True)
check("due at 22:20, after the window shuts", bed["due_at"][11:16], "22:20")
check("it belongs to the day, not a block", bed["block_id"], None)


def N(h, m, day=25):
    return datetime(2026, 8, day, h, m, tzinfo=TZ)


check("21:00 — before bedtime, no bed siren", eng5.agent_state(N(21, 0)).headline.startswith("Go to bed"), False)
check("22:30 — siren on", eng5.agent_state(N(22, 30)).phone_alarm, True)
check("...and it says what it wants", "bed" in eng5.agent_state(N(22, 30)).headline.lower(), True)
check("00:30 — still on, past midnight", eng5.agent_state(N(0, 30, 26)).phone_alarm, True)
check("...and counts from bedtime, not midnight",
      eng5.agent_state(N(0, 30, 26)).debt_minutes, 130)
check("01:30 — past the cutoff, siren gives up", eng5.agent_state(N(1, 30, 26)).phone_alarm, False)

print("\n=== a missing plan for tomorrow sirens, but never past bedtime ===")
# Telegram nags were the only pressure to write tomorrow's plan, and 9 of 19
# days had one. A day with no plan produces nothing, because with no plan there
# are no checkpoints to miss.
import copy as _cpl
_praw = _cpl.deepcopy(RAW)
_praw["schedule"].update({"mode": "block_start", "plan_due": "22:00"})
_praw["proof"] = {"shot_gap_min": 1}
cfgp, dbp, ledp, engp, lkp = build(_praw)
dbp.set_lock(enabled=1, commitment_end="2099-01-01")
TOM = DAY + __import__("datetime").timedelta(days=1)

check("21:50 — before the deadline, silent",
      engp.plan_overdue_for(datetime(2026, 8, 25, 21, 50, tzinfo=TZ)), None)
check("22:05 — past it and no plan, so it is owed",
      engp.plan_overdue_for(datetime(2026, 8, 25, 22, 5, tzinfo=TZ)), TOM)
st = engp.agent_state(datetime(2026, 8, 25, 22, 5, tzinfo=TZ))
check("...and the siren is on", st.phone_alarm, True)
check("...naming the day it wants", TOM.strftime("%A") in st.headline, True)

engp.build_checkpoints(DAY, datetime(2026, 8, 25, 15, 0, tzinfo=TZ))
check("22:25 — past bedtime, the plan siren stops",
      engp.plan_overdue_for(datetime(2026, 8, 25, 22, 25, tzinfo=TZ)), None)
check("...and the bed check has taken over",
      "bed" in engp.agent_state(datetime(2026, 8, 25, 22, 25, tzinfo=TZ)).headline.lower(), True)

dbp.ensure_day(TOM)
dbp.run("UPDATE days SET plan_locked=1 WHERE date=?", (TOM.isoformat(),))
check("once the plan lands it goes quiet",
      engp.plan_overdue_for(datetime(2026, 8, 25, 22, 5, tzinfo=TZ)), None)

print("\n=== a phone about to die is a siren about to stop ===")
cfgb, dbb, ledb, engb, lkb = build(_cpl.deepcopy(_praw))
dbb.set_lock(enabled=1, commitment_end="2099-01-01")
NOWB = datetime(2026, 8, 25, 18, 0, tzinfo=TZ)
cfgb.raw["escalation"]["charge_below_pct"] = 30

check("no reading at all means nothing fires", engb.phone_needs_charger(NOWB), None)
dbb.set_lock(phone_battery=12, phone_charging=0, phone_batt_at=NOWB.isoformat())
check("12% and unplugged is a problem", engb.phone_needs_charger(NOWB), 12)
dbb.set_lock(phone_charging=1)
check("12% but plugged in is fine", engb.phone_needs_charger(NOWB), None)
dbb.set_lock(phone_battery=80, phone_charging=0)
check("80% unplugged is fine", engb.phone_needs_charger(NOWB), None)
dbb.set_lock(phone_battery=12,
             phone_batt_at=(NOWB - __import__("datetime").timedelta(minutes=30)).isoformat())
check("a half-hour-old reading says nothing about now",
      engb.phone_needs_charger(NOWB), None)
cfgb.raw["escalation"]["charge_below_pct"] = 0
dbb.set_lock(phone_batt_at=NOWB.isoformat())
check("and 0 switches the whole rule off", engb.phone_needs_charger(NOWB), None)

print("\n=== the bed photo turns it off ===")
db5.run("UPDATE checkpoints SET state='satisfied' WHERE id=?", (bed["id"],))
check("silent once the photo lands", eng5.agent_state(N(22, 45)).phone_alarm, False)

print("\n=== /latenight moves both bedtime and the cutoff ===")
cfg6, db6, led6, eng6 = make()
eng6.build_checkpoints(DAY, BEFORE)
check("normally bed at 22:20", eng6.bed_due(DAY).strftime("%H:%M"), "22:20")
check("normally quiet by 01:00", eng6.bed_cutoff(DAY).strftime("%d %H:%M"), "26 01:00")
db6.run("UPDATE days SET bed_offset_minutes=120 WHERE date=?", (DAY.isoformat(),))
check("with the late night, bed at 00:20", eng6.bed_due(DAY).strftime("%d %H:%M"), "26 00:20")
check("and the siren runs to 03:00", eng6.bed_cutoff(DAY).strftime("%d %H:%M"), "26 03:00")
check("23:00 is no longer past bedtime", eng6.bed_night(N(23, 0)), None)
check("01:00 now is", eng6.bed_night(N(1, 0, 26)), DAY)

print("\n=== sleep is protected from every kind of shift ===")
cfg3, db3, led3, eng3 = make()
db3.run("UPDATE days SET offset_minutes = 60 WHERE date = ?", (DAY.isoformat(),))
check("an hour's offset stops at the hard end", eng3.window(DAY)[1], 22 * 60 + 20)
db3.run("UPDATE days SET offset_minutes = 240 WHERE date = ?", (DAY.isoformat(),))
check("four hours cannot break it either", eng3.window(DAY)[1], 22 * 60 + 20)

sys.exit(report("PROOF-MODEL TESTS"))
