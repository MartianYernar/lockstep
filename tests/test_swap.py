"""17 Sep, 20:05: physics swapped for an unfinished competition presentation.

What went wrong, and is pinned here:
- the swap was treated as cutting work, though it was work for work;
- "I couldn't finish the competition presentation" was refused;
- "pic of what exactly?" was judged as an excuse and used up a try;
- Friday never said which photo was owed;
- the rejected presentation screenshot stayed rejected after the plan
  changed to the presentation.
"""
import asyncio
import copy
import io
import json
import os
import sys
import types
from datetime import date, datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fixtures import build, check, report, RAW
from test_friend_fakes import FakeUpdate, FakeBot
from server.bot import LockstepBot
from server.coach import JUDGE_SCHEMA
from server.vision import Verdict
from server import planner

TZ = ZoneInfo("Asia/Almaty")
DAY = date(2026, 8, 25)
run = asyncio.get_event_loop().run_until_complete
SEED = [{"title": "Physics solving", "keywords": ["physics"]},
        {"title": "РНКП presentation", "keywords": ["рнкп", "ркнп", "presentation"]}]


def N(h, m):
    return datetime(2026, 8, 25, h, m, tzinfo=TZ)


class Checker:
    def __init__(self):
        self.labels = []

    async def check(self, blob, label, category, now, mime="image/jpeg", kind="activity"):
        self.labels.append(label)
        if "presentation" in label.lower() or "рнкп" in label.lower():
            return Verdict(ok=True, reason="ok", failure_code="ok")
        return Verdict(ok=False, reason="that's a slide deck, not physics",
                       failure_code="mismatch")


async def _file():
    async def dl():
        from PIL import Image
        buf = io.BytesIO(); Image.new("RGB", (40, 40)).save(buf, "JPEG")
        return bytearray(buf.getvalue())
    return types.SimpleNamespace(download_as_bytearray=dl)


def make(now):
    raw = copy.deepcopy(RAW)
    raw["tasks"] = {"seed": SEED}
    raw["coach"] = {"enabled": True, "max_calls_per_day": 50}
    raw["schedule"] = dict(raw["schedule"], mode="block_start", grace_min=3)  # his real mode
    raw["proof"] = {"shot_gap_min": 1}
    cfg, db, led, eng, lk = build(raw, now=now)
    bot = LockstepBot(cfg, db, led, eng, lk, Checker())
    bot.app = types.SimpleNamespace(bot=FakeBot())
    db.set_lock(enabled=1, commitment_end="2099-01-01")
    db.ensure_day(DAY)
    db.replace_blocks(DAY, [b.as_tuple() for b in planner.parse(
        "16:00-17:00 gym\n17:00-19:00 physics\n19:00-22:00 work: essay")])
    db.run("UPDATE days SET plan_locked=1 WHERE date=?", (DAY.isoformat(),))
    eng.build_checkpoints(DAY, N(15, 59))
    db.run("UPDATE checkpoints SET state='satisfied' WHERE date=? AND due_at < ?",
           (DAY.isoformat(), N(17, 0).isoformat()))
    eng.tick(N(17, 1))
    u = FakeUpdate()
    u.message.photo = [types.SimpleNamespace(get_file=_file)]
    run(bot.on_photo(u, None))
    assert bot.last_reject is not None, u.message.replies
    return cfg, db, led, eng, bot


def model(bot, chat=None, judge=None):
    seen = {"systems": []}

    async def fake(system, contents, now, max_tokens=400, schema=None):
        seen["systems"].append(system)
        bot.coach._charge(now.date())
        if schema is JUDGE_SCHEMA:
            return judge(contents) if callable(judge) else judge
        return chat(contents) if callable(chat) else chat
    bot.coach._ask = fake
    return seen


def labels(db):
    return [b["label"] for b in db.blocks(DAY)]


def counted(db):
    return db.one("SELECT COUNT(*) c FROM checkpoints WHERE date=? AND state='satisfied'"
                  " AND due_at >= ?", (DAY.isoformat(), N(17, 0).isoformat()))["c"]


print("=== work for work: no question, and the photo is looked at again ===")
cfg, db, led, eng, bot = make(N(17, 10))
seen = model(bot, chat=json.dumps({"reply": "ok switching it", "actions": [
    {"type": "edit_plan", "day": "today",
     "plan": "17:00-19:00 рнкп presentation\n19:00-22:00 work: essay"}]}))
check("Friday is told which pic is owed", 'block "physics"' in bot.coach.context(N(17, 10)), True)
u = FakeUpdate("yeah change the plan then")
run(bot.on_text(u, None))
check("no 'why?'", any("why?" in r for r in u.message.replies), False)
check("the plan changed", "рнкп presentation" in labels(db), True)
check("the old photo was looked at again, for the new block",
      "presentation" in bot.validator.labels[-1], True)
check("and it counts", counted(db), 1)
check("he's told", any(r.startswith("✅") for r in u.message.replies), True)

print("\n=== a question is answered, not judged ===")
cfg, db, led, eng, bot = make(N(17, 10))
model(bot,
      chat=json.dumps({"reply": "ok", "actions": [
          {"type": "edit_plan", "day": "today",
           "plan": "17:00-19:00 competition slides\n19:00-22:00 work: essay"}]}),
      judge=lambda c: json.dumps({"is_reason": False, "accept": False,
                                  "reply": "the pic for physics. and why the change?"}))
run(bot.on_text(FakeUpdate("change physics to competition slides"), None))
check("cutting a task for a non-task still asks why", bot.pending[1]["action"], "rewrite_why")
u = FakeUpdate("pic of what exactly?")
run(bot.on_text(u, None))
check("the question is answered", "the pic for physics" in u.message.last, True)
check("no try used up", bot.pending[1]["tries"], 0)
check("not logged as an excuse",
      db.one("SELECT COUNT(*) c FROM events WHERE kind='rewrite_reason'")["c"], 0)

print("\n=== an unfinished competition presentation is a real reason ===")
model(bot, judge=lambda c: json.dumps({"is_reason": True, "accept": True,
                                        "reply": "fair, deadline first"}))
bot.coach.terms.set("competition slides", "the РНКП competition presentation", N(17, 0))
u = FakeUpdate("i couldn't finish the presentation for the competition, it's due")
run(bot.on_text(u, None))
check("accepted", any("deadline first" in r for r in u.message.replies), True)
check("plan changed", "competition slides" in labels(db), True)
check("the photo counts under the new block", counted(db), 1)

print("\n=== the judge is told other real work is a reason ===")
from server.coach import JUDGE  # noqa: E402
check("prompt says so", "OTHER REAL WORK" in JUDGE and "competition" in JUDGE, True)

print("\n=== config tasks are added later, removed ones stay removed ===")
cfg, db, led, eng, bot = make(N(17, 10))
t = bot.tasks
t.close(t.find("physics solving")["id"], N(17, 0), "dropped")
added = t.seed(SEED + [{"title": "Docs collection"}], N(17, 10))
check("only the new one is added", added, 1)
check("the dropped one stays dropped", t.find("physics solving"), None)
check("the new one is open", t.find("docs collection") is not None, True)

sys.exit(report("SWAP TESTS"))
