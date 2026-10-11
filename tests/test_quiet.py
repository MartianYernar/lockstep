"""The exam window, and faces that are only pictures on a screen.

11 Oct 2026, his two asks:

    "save so that it doesnt bother me on 18th oct from 10am till 3 pm, i have
     ielts on that day, dont want bot to fire sirens at all on that time"

    "make so that it accepts screenshots where some faces from images of
     website for example are visible, because bro those people put their faces
     into public themselves anyway"

A siren in an exam hall is not accountability, and a university's staff photo
is not his privacy.
"""
import asyncio
import copy
import json
import os
import sys
import types
from datetime import date, datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fixtures import build, check, report, RAW
from test_friend_fakes import FakeUpdate, FakeBot, StubValidator
from server.bot import LockstepBot
from server.vision import Validator
from server import planner

TZ = ZoneInfo("Asia/Almaty")
EXAM = date(2026, 10, 18)                      # a Sunday
run = asyncio.get_event_loop().run_until_complete


def N(h, m, d=18):
    return datetime(2026, 10, d, h, m, tzinfo=TZ)


def make(now=N(11, 0)):
    raw = copy.deepcopy(RAW)
    raw["schedule"] = dict(raw["schedule"], mode="block_start", grace_min=3,
                           weekend={"days": [5, 6], "enforce_start": "10:00",
                                    "enforce_end": "22:00"})
    raw["proof"] = {"within_block_min": 20}
    raw["coach"] = {"enabled": True, "max_calls_per_day": 150, "say_reserve": 0}
    raw["standup"] = {"enabled": False}
    raw["routines"] = [
        {"days": "weekends", "time": "10:00-11:00", "label": "teaching"},
        {"date": "2026-10-18", "time": "10:00-15:00", "label": "IELTS exam", "quiet": True},
    ]
    cfg, db, led, eng, lk = build(raw, now=now)
    bot = LockstepBot(cfg, db, led, eng, lk, StubValidator())
    bot.app = types.SimpleNamespace(bot=FakeBot())
    db.set_lock(enabled=1, commitment_end="2099-01-01")
    db.ensure_day(EXAM)
    db.set_goals(EXAM, ["ielts", "rest", "uni"])
    return cfg, db, led, eng, bot


print("=== 18 Oct, 10:00-15:00: nothing asked, nothing sounds ===")
cfg, db, led, eng, bot = make()
db.replace_blocks(EXAM, [b.as_tuple() for b in planner.parse(
    "10:00-15:00 IELTS exam\n15:00-22:00 work: uni applications")])
db.run("UPDATE days SET plan_locked=1 WHERE date=?", (EXAM.isoformat(),))
eng.build_checkpoints(EXAM, N(9, 0))
check("no photo is asked for during the exam", db.one(
    "SELECT COUNT(*) c FROM checkpoints WHERE date=? AND due_at < ?",
    (EXAM.isoformat(), N(15, 0).isoformat()))["c"], 0)
check("the first one is after it", db.one(
    "SELECT MIN(due_at) d FROM checkpoints WHERE date=? AND kind != 'bed'",
    (EXAM.isoformat(),))["d"][11:16], "15:00")
for h, m in ((10, 0), (12, 30), (14, 59)):
    st = eng.agent_state(N(h, m))
    check(f"{h:02d}:{m:02d} — silent", (st.active, st.phone_alarm, st.debt_minutes),
          (False, False, 0))
check("and the siren is live again at 15:00",
      eng.quiet_now(N(15, 0)) is None, True)

print("\n=== even with no plan at all, and photos owed ===")
cfg, db, led, eng, bot = make()
check("a missing plan does not siren during the exam",
      eng.agent_state(N(11, 30)).active, False)
check("but it does after it, with the day still unplanned",
      eng.agent_state(N(15, 30)).active, True)

cfg, db, led, eng, bot = make()
db.replace_blocks(EXAM, [b.as_tuple() for b in planner.parse(
    "10:00-22:00 work: uni applications")])
db.run("UPDATE days SET plan_locked=1 WHERE date=?", (EXAM.isoformat(),))
eng.build_checkpoints(EXAM, N(9, 0))
check("the exam hours carry no checks to be behind on", db.one(
    "SELECT COUNT(*) c FROM checkpoints WHERE date=? AND due_at < ?",
    (EXAM.isoformat(), N(15, 0).isoformat()))["c"], 0)
check("nothing is owed during it", eng.debt_seconds(EXAM, N(12, 0)), 0)
# The first check after the exam is at 15:00; ignore it and the siren returns.
db.run("UPDATE checkpoints SET state='overdue', asked_at=? WHERE date=? AND due_at=?",
       (N(15, 0).isoformat(), EXAM.isoformat(), N(15, 0).isoformat()))
check("and the day is enforced again the moment it ends",
      eng.debt_seconds(EXAM, N(15, 10)) > 0, True)

print("\n=== the phone is told to stay quiet before he leaves ===")
cfg, db, led, eng, bot = make()
check("45 min before, the answer is already 'quiet'", eng.quiet_soon(N(9, 20)), True)
check("an hour and a half before, it is not", eng.quiet_soon(N(8, 30)), False)
check("inside, yes", eng.quiet_soon(N(13, 0)), True)
check("after, no", eng.quiet_soon(N(15, 30)), False)

print("\n=== an exam day plans itself ===")
# "make exam days plan themselves": the bot is silent through the exam, but a
# day with no plan sirens the moment its window opens, and the night before an
# exam is exactly when he forgets to write one.
cfg, db, led, eng, bot = make(now=N(9, 15))


async def planner_ask(system, contents, now, max_tokens=400, schema=None, tier=None):
    bot.coach._charge(now.date())
    planner_ask.saw = "\n".join(str(c) for c in contents)
    return json.dumps({"blocks": [
        {"start": "10:00", "end": "15:00", "label": "IELTS exam"},
        {"start": "15:00", "end": "22:00", "label": "rest and uni applications"}],
        "note": "exam first"})
bot.coach._ask = planner_ask
run(bot._friend_tick(N(9, 15)))
check("the day is planned without being asked",
      [b["label"] for b in db.blocks(EXAM)][:1], ["IELTS exam"])
check("and locked", eng.has_plan(EXAM), True)
check("the exam hours are exactly his",
      (db.blocks(EXAM)[0]["start_min"], db.blocks(EXAM)[0]["end_min"]), (600, 900))
check("the planner was told the exam is fixed", "IELTS exam" in planner_ask.saw, True)
check("he is told, and told how to change it",
      any("I've written it" in t and "/replan" in t for t in bot.app.bot.to(1)), True)
check("goals exist too, so it could lock", len(db.goals(EXAM)), 3)
check("it is on the record", db.one(
    "SELECT COUNT(*) c FROM events WHERE kind='exam_day_planned'")["c"], 1)
check("and still nothing is asked during the exam", db.one(
    "SELECT COUNT(*) c FROM checkpoints WHERE date=? AND due_at < ?",
    (EXAM.isoformat(), N(15, 0).isoformat()))["c"], 0)
run(bot._friend_tick(N(9, 20)))
check("it does not do it twice", db.one(
    "SELECT COUNT(*) c FROM events WHERE kind='exam_day_planned'")["c"], 1)

print("\n=== a plan he wrote himself is never overwritten ===")
cfg, db, led, eng, bot = make(now=N(9, 15))
db.replace_blocks(EXAM, [b.as_tuple() for b in planner.parse(
    "10:00-15:00 IELTS exam\n15:00-22:00 sleep it off")])
db.run("UPDATE days SET plan_locked=1 WHERE date=?", (EXAM.isoformat(),))
bot.coach._ask = planner_ask
run(bot._friend_tick(N(9, 15)))
check("his own plan stands", [b["label"] for b in db.blocks(EXAM)][1], "sleep it off")

print("\n=== the day after is a normal day ===")
cfg, db, led, eng, bot = make(now=N(11, 0, 19))
bot.coach._ask = planner_ask
run(bot._friend_tick(N(9, 15, 19)))
check("no quiet window, so no automatic plan", eng.has_plan(date(2026, 10, 19)), False)
check("no quiet window on the 19th", eng.quiet_now(N(11, 0, 19)), None)
check("and 10-11 teaching is still a weekend thing only",
      [r["label"] for r in bot.coach.routines.for_day(date(2026, 10, 19))], [])

print("\n=== Friday can save one from chat ===")
cfg, db, led, eng, bot = make()


async def fake(system, contents, now, max_tokens=400, schema=None, tier=None):
    bot.coach._charge(now.date())
    return json.dumps({"reply": "noted, silent that morning", "actions": [
        {"type": "add_routine", "date": "2026-11-05", "time": "09:00-12:00",
         "title": "SAT exam", "quiet": True}]})
bot.coach._ask = fake
u = FakeUpdate("i have my SAT on 5 nov 9am to 12, don't ping me")
run(bot.on_text(u, None))
check("saved as a silent window", any("silent window saved" in r for r in u.message.replies),
      True)
check("on that date only", [r["label"] for r in bot.coach.routines.for_day(date(2026, 11, 5))],
      ["SAT exam"])
check("and it is silent", bot.coach.routines.quiet_now(
    datetime(2026, 11, 5, 10, 0, tzinfo=TZ)) is not None, True)
check("Friday's view of his week says so",
      "SILENT" in bot.coach.routines.describe(), True)

print("\n=== a face on a website is not his face ===")
BASE = {
    "timestamp_visible": True, "timestamp_text": "16:05", "replay_suspected": False,
    "activity_seen": "a university admissions page", "activity_match": True,
    "confidence": 0.9, "reason": "fine", "face_visible": False,
    "identity_visible": False, "location_revealing": False, "privacy_detail": "",
    "blocker_visible": True, "blocker_running": True, "blocker_clock": "16:05",
    "allowed_items": [], "blocked_items": [],
}


def verdict(**over):
    cfg2, _d, _l, _e, _lk = build(copy.deepcopy(RAW), now=N(16, 5))
    v = Validator(cfg2)

    async def ask(image_bytes, mime, prompt):
        ask.prompt = prompt
        return {**BASE, **over}
    v._ask = ask
    out = run(v.check(b"x", "uni applications", "work", N(16, 5), kind="activity"))
    out.prompt = ask.prompt
    return out

r = verdict()
check("a screenshot of a uni page counts", r.ok, True)
rule = r.prompt.split("face_visible")[1].split("identity_visible")[0]
check("the rule is about a real person in the room", "REAL PERSON is in the room" in rule, True)
check("website photos are explicitly fine", "website" in rule and "are false" in rule, True)
check("staff pages and video thumbnails too",
      "staff page" in rule and "thumbnails" in rule, True)
check("but his own face still counts", "selfie preview" in rule and "still true" in rule, True)
r = verdict(face_visible=True, privacy_detail="Your face is in shot.")
check("and a real face is still refused", (r.ok, r.failure_code), (False, "privacy"))

sys.exit(report("QUIET WINDOW TESTS"))
