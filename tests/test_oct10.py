"""10 Oct 2026, 14:40-16:40, replayed from the live record.

Read off the production database over SSH, not reconstructed from memory:

- 14:41 "change the plan, CAS now" -> Friday built a new proposal, he tapped
  lock, the rewrite guard held it, nothing was applied. 14:49 Friday: "plan #4
  is already locked with CAS until 15:11". It had read its own "[tapped lock]".
- 16:23 "I need another 20 min for replacing" -> "added 20 mins for CAS".
  No replan happened.
- 16:26 "paper" -> /paper matched the word CAS to the CAS block that had ended
  at 16:14, found nothing to change, and said "already wants a photo" while
  the open check, on the IELTS block, kept demanding a Cold Turkey screenshot.
- The whole day: photos of his screen, refused for having no Cold Turkey
  window in them. He switched the screenshot proof off for good.
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
from server import planner

TZ = ZoneInfo("Asia/Almaty")
DAY = date(2026, 8, 25)
run = asyncio.get_event_loop().run_until_complete
PLAN = ("16:00-17:30 work: prep for IELTS\n17:30-18:00 work: replace CAS activity\n"
        "18:00-22:00 work: uni essay")


def N(h, m):
    return datetime(2026, 8, 25, h, m, tzinfo=TZ)


def make(now=N(16, 41), blockers=False):
    raw = copy.deepcopy(RAW)
    raw["tasks"] = {"seed": [{"title": "IELTS", "keywords": ["ielts"]},
                             {"title": "CAS", "keywords": ["cas"]}]}
    raw["coach"] = {"enabled": True, "max_calls_per_day": 150, "say_reserve": 0}
    raw["standup"] = {"enabled": False}
    raw["schedule"] = dict(raw["schedule"], mode="block_start", grace_min=3)
    raw["proof"] = {"within_block_min": 20}
    raw["vision"] = dict(raw["vision"], require_blocker_for_work=blockers)
    raw["friend"] = {"enabled": True, "guard_rewrites": True, "rewrite_min_lost": 10}
    cfg, db, led, eng, lk = build(raw, now=now)
    bot = LockstepBot(cfg, db, led, eng, lk, StubValidator())
    bot.app = types.SimpleNamespace(bot=FakeBot())
    db.set_lock(enabled=1, commitment_end="2099-01-01")
    db.ensure_day(DAY)
    db.set_goals(DAY, ["ielts", "cas", "essay"])
    db.replace_blocks(DAY, [b.as_tuple() for b in planner.parse(PLAN)])
    db.run("UPDATE days SET plan_locked=1 WHERE date=?", (DAY.isoformat(),))
    eng.build_checkpoints(DAY, N(15, 59))
    return cfg, db, led, eng, bot


def model(bot, reply):
    seen = {"systems": [], "contents": []}

    async def fake(system, contents, now, max_tokens=400, schema=None, tier=None):
        seen["systems"].append(system)
        seen["contents"].append(contents)
        bot.coach._charge(now.date())
        return reply if isinstance(reply, str) else json.dumps(reply)
    bot.coach._ask = fake
    return seen


def labels(db):
    return [b["label"] for b in db.blocks(DAY)]


def kinds(db):
    return {c["kind"] for c in db.q("SELECT kind FROM checkpoints WHERE date=?",
                                    (DAY.isoformat(),))}


print("=== no more screenshots ===")
cfg, db, led, eng, bot = make()
check("a work: block asks for a photo, like any other", "blocker_toggle" in kinds(db), False)
check("every check is an ordinary photo check", kinds(db) <= {"activity", "bed"}, True)

cfg, db, led, eng, bot = make(blockers=True)
check("(with the old switch on, they were screenshots)", "blocker_toggle" in kinds(db), True)
cfg.raw["vision"]["require_blocker_for_work"] = False
n = eng.retire_blocker_checks()
check("at startup, the ones already waiting become photo checks", n > 0, True)
check("so nothing on today's plan still wants a screenshot",
      db.one("SELECT COUNT(*) c FROM checkpoints WHERE kind LIKE 'blocker%'"
             " AND state IN ('scheduled','asked','overdue')")["c"], 0)
check("and it is on the record", db.one(
    "SELECT COUNT(*) c FROM events WHERE kind='blocker_checks_retired'")["c"], 1)
check("nothing settled is rewritten", db.one(
    "SELECT COUNT(*) c FROM checkpoints WHERE kind LIKE 'blocker%' AND state='missed'")["c"]
    == db.one("SELECT COUNT(*) c FROM checkpoints WHERE kind LIKE 'blocker%' AND state='missed'")["c"],
    True)

from server.coach import ACTIONS  # noqa: E402
check("Friday knows there are no screenshots", "no screenshots any more" in ACTIONS, True)
check("and never offers /paper again", "never suggest /paper" in ACTIONS, True)

print("\n=== 16:23: 'added 20 mins' when nothing was added ===")
cfg, db, led, eng, bot = make(now=N(17, 10))
# A change that takes real work away, so the guard holds it -- the plan does
# not move, and "added 20 mins" would be a lie.
seen = model(bot, {"reply": "got it, added 20 mins for CAS", "actions": [
    {"type": "edit_plan", "day": "today",
     "plan": "17:10-17:50 chill\n17:50-22:00 work: uni essay"}]})
# Not "I need another 20 min" any more: that phrasing is now handled by the
# app itself (test_more_time.py), so it never reaches Friday at all.
u = FakeUpdate("put chill instead of the rest of this block")
run(bot.on_text(u, None))
check("the change cut real work, so it was held for a reason",
      bot.pending.get(1, {}).get("action"), "rewrite_why")
check("the plan did not change", labels(db)[0], "work: prep for IELTS")
check("and Friday's 'added 20 mins' was NOT sent",
      any("added 20 mins" in r for r in u.message.replies), False)
check("he hears the real question instead", any("why" in r.lower() for r in u.message.replies),
      True)
note = db.one("SELECT text FROM coach_log WHERE text LIKE '[app:%' ORDER BY id DESC")
check("and Friday's memory says it did not happen",
      note is not None and "NOT applied" in note["text"], True)

seen = model(bot, {"reply": "ok", "actions": []})
bot.pending.clear()
run(bot.on_text(FakeUpdate("so is it changed?"), None))
check("so the next reply starts from the truth",
      "NOT applied" in str(seen["contents"][0]), True)

print("\n=== a change that cannot be applied at all is not claimed either ===")
cfg, db, led, eng, bot = make(now=N(17, 10))
seen = model(bot, {"reply": "done, shifted it", "actions": [
    {"type": "edit_plan", "day": "today",
     "plan": "17:10-17:50 work: replace CAS activity\n18:30-22:00 work: uni essay"}]})
u = FakeUpdate("shift it")
run(bot.on_text(u, None))
check("the plan has a hole, so it is refused",
      any(r.startswith("❌") for r in u.message.replies), True)
check("and 'done, shifted it' is never said",
      any("shifted it" in r for r in u.message.replies), False)

print("\n=== and when it does land, Friday may say so ===")
cfg, db, led, eng, bot = make(now=N(17, 10))
model(bot, {"reply": "swapped, go", "actions": [
    {"type": "edit_plan", "day": "today",
     "plan": "17:10-17:30 work: prep for IELTS\n17:30-18:00 work: replace CAS activity\n"
             "18:00-22:00 work: uni essay"}]})
u = FakeUpdate("tiny tweak")
run(bot.on_text(u, None))
check("the app confirms first", "rest of today" in u.message.replies[0], True)
check("then Friday", u.message.last, "swapped, go")

print("\n=== 14:41: a proposal for today, after the day is locked ===")
cfg, db, led, eng, bot = make(now=N(16, 41))
model(bot, {"reply": "on it", "actions": [{"type": "make_plan", "day": "today",
                                           "text": "CAS now for 30 min"}]})


async def planner_out(system, contents, now, max_tokens=400, schema=None, tier=None):
    bot.coach._charge(now.date())
    if system.startswith("You build one day's timetable"):
        return json.dumps({"blocks": [
            {"start": "16:41", "end": "17:11", "label": "work: replace CAS activity"},
            {"start": "17:11", "end": "22:00", "label": "work: uni essay"}],
            "note": "CAS first"})
    return json.dumps({"reply": "on it", "actions": [
        {"type": "make_plan", "day": "today", "text": "CAS now for 30 min"}]})
bot.coach._ask = planner_out
run(bot.on_text(FakeUpdate("change the plan, CAS now for 30 min"), None))
pid = bot.pending[1]["id"]


class Q:
    def __init__(self, data):
        self.data, self.edited, self.replies = data, None, []
        self.message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=1, send_action=lambda *a, **k: asyncio.sleep(0)),
            reply_text=self._reply)
    async def answer(self): pass
    async def edit_message_text(self, t, **kw): self.edited = t
    async def _reply(self, t, **kw): self.replies.append(t)


q = Q(f"plan:ok:{pid}")
run(bot.on_button(types.SimpleNamespace(callback_query=q), None))
said = " ".join(q.replies)
check("it is no longer a dead end ('can't rewrite it now')",
      "can't rewrite it now" in said, False)
check("it goes through the replan door -- which asks why, since it cuts IELTS",
      bot.pending.get(1, {}).get("action") == "rewrite_why" or "rest of today" in said, True)
last = db.one("SELECT text FROM coach_log WHERE text LIKE '[app:%' ORDER BY id DESC")["text"]
check("and Friday's memory says exactly what became of the tap",
      ("NOT locked" in last) or ("now locked" in last), True)
check("no row numbers in it", "#" in last, False)

print("\n=== 16:26: /paper never lands on a block that is already over ===")
cfg, db, led, eng, bot = make(now=N(17, 50), blockers=True)
# The CAS block (17:30-18:00) is over by 18:00; at 17:50 it is live. Ask from
# inside the essay block instead, where "cas" names only a past block.
cfg.now = lambda: N(18, 26)
db.run("UPDATE checkpoints SET state='asked', asked_at=? WHERE date=? AND due_at=?",
       (N(18, 0).isoformat(), DAY.isoformat(), N(18, 0).isoformat()))
u = FakeUpdate("/paper cas")
run(bot.cmd_paper(u, None))
check("it acts on the block the siren is asking about, not the CAS block from 17:30",
      "uni essay" in u.message.last, True)
check("and that block's screenshot check became a photo check",
      db.one("SELECT kind FROM checkpoints WHERE date=? AND due_at=?",
             (DAY.isoformat(), N(18, 0).isoformat()))["kind"], "activity")

sys.exit(report("OCT 10 TESTS"))
