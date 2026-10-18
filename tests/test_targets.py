"""Name what will exist by the end, then show it.

18 Oct 2026. He asked how the system can mandate anything when the ten-minute
steps are only text, and then: "what if i slack off on small steps, and do
some of them only and there still gonna be progress then what?"

Per-step policing is impossible and would be wrong anyway -- a step he wrote
five minutes ago is unfalsifiable to anyone else, and routes change. What has
a definite edge is the sentence he writes at the start of the block: at 17:30
this will exist. Six steps out of ten still fails if "questions 12-20 worked"
came back as 12-15.

His other instruction, the same evening: be specific. So the block cannot
start until that sentence is written, and the end-of-block photo is judged
against his own words plus the photo from when the block began.
"""
import asyncio
import copy
import io
import json
import os
import sys
import types
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fixtures import build, check, report, RAW
from test_friend_fakes import FakeUpdate, FakeBot
from server.bot import LockstepBot
from server.vision import Verdict
from server import planner

TZ = ZoneInfo("Asia/Almaty")
DAY = date(2026, 8, 25)
run = asyncio.get_event_loop().run_until_complete
PLAN = ("16:00-17:30 work: uni applications\n17:30-18:00 dinner\n"
        "18:00-19:30 work: physics questions\n19:30-22:00 work: essay")


def N(h, m):
    return datetime(2026, 8, 25, h, m, tzinfo=TZ)


class Checker:
    """Says yes to ordinary photos; the delivery answer is scripted."""

    def __init__(self, delivered=True, moved=True, what="four more rows"):
        self.answer = {"delivered": delivered, "moved": moved, "what_changed": what,
                       "confidence": 0.9, "reason": "only 2 rows are filled",
                       "unverified": False}
        self.seen = []

    async def check(self, blob, label, category, now, mime="image/jpeg", kind="activity"):
        return Verdict(ok=True, reason="ok", failure_code="ok")

    async def delivered(self, blob, declared, label, minutes, now, mime="image/jpeg",
                        before=None):
        self.seen.append({"declared": declared, "label": label, "minutes": minutes,
                          "had_before": before is not None})
        return dict(self.answer)


async def _file():
    async def dl():
        from PIL import Image
        buf = io.BytesIO(); Image.new("RGB", (900, 700)).save(buf, "JPEG")
        return bytearray(buf.getvalue())
    return types.SimpleNamespace(download_as_bytearray=dl)


def photo():
    u = FakeUpdate()
    u.message.photo = [types.SimpleNamespace(get_file=_file)]
    return u


def make(now=N(16, 0), checker=None, **deliver):
    raw = copy.deepcopy(RAW)
    raw["coach"] = {"enabled": False}
    raw["standup"] = {"enabled": False}
    raw["micro"] = {"enabled": False}
    raw["schedule"] = dict(raw["schedule"], mode="block_start", grace_min=3)
    raw["proof"] = {"within_block_min": 20}
    raw["deliver"] = {"enabled": True, "min_block_min": 45,
                      "skip_categories": ["commute", "meal", "sleep", "relax"], **deliver}
    cfg, db, led, eng, lk = build(raw, now=now)
    bot = LockstepBot(cfg, db, led, eng, lk, checker or Checker())
    bot.app = types.SimpleNamespace(bot=FakeBot())
    db.set_lock(enabled=1, commitment_end="2099-01-01")
    db.ensure_day(DAY)
    db.set_goals(DAY, ["uni", "physics", "essay"])
    db.replace_blocks(DAY, [b.as_tuple() for b in planner.parse(PLAN)])
    db.run("UPDATE days SET plan_locked=1 WHERE date=?", (DAY.isoformat(),))
    eng.build_checkpoints(DAY, N(15, 59))
    return cfg, db, led, eng, bot


def tick_at(db, eng, when):
    """Tick as if the server had been running all along.

    A jump of more than the grace period reads as downtime, and downtime voids
    everything that came due inside it -- correct in production, wrong in a
    test that skips an hour between two lines.
    """
    db.set_lock(last_tick_at=(when - timedelta(minutes=1)).isoformat())
    return eng.tick(when)


def kinds_at(db, hhmm):
    return [c["kind"] for c in db.q(
        "SELECT kind FROM checkpoints WHERE date=? AND due_at LIKE ?",
        (DAY.isoformat(), f"%T{hhmm}%"))]


print("=== a long block owes a target; a short one does not ===")
cfg, db, led, eng, bot = make()
check("the 90-minute block is asked at its start", "declare" in kinds_at(db, "16:00"), True)
check("and asked to show it at the end", "deliver" in kinds_at(db, "17:29"), True)
check("dinner is exempt", kinds_at(db, "17:30"), [])
check("so is a block under the minimum",
      bot.deliver.wants({"category": "work", "start_min": 600, "end_min": 630,
                         "label": "work: x"}), False)

print("\n=== the siren treats a missing target like a missing photo ===")
cfg, db, led, eng, bot = make()
acts = tick_at(db, eng, N(16, 0))
check("it is asked", [a.payload["kind"] for a in acts if a.kind == "ask"], ["declare"])
check("and being behind on it is real debt", eng.debt_seconds(DAY, N(16, 5)) > 0, True)

print("\n=== writing it ===")
cfg, db, led, eng, bot = make()
tick_at(db, eng, N(16, 0))
bot.cfg.now = lambda: N(16, 1)
u = FakeUpdate("Common App activities section with 5 entries filled")
run(bot.on_text(u, None))
check("it is locked, in his words",
      bot.deliver.declared_for(DAY, db.blocks(DAY)[0]),
      "Common App activities section with 5 entries filled")
check("he is told when it is due", "17:30" in u.message.last, True)
check("the siren stops", eng.debt_seconds(DAY, N(16, 2)), 0)

run(bot.on_text(FakeUpdate("actually just one entry"), None))
check("a second attempt does not rewrite it",
      bot.deliver.declared_for(DAY, db.blocks(DAY)[0]),
      "Common App activities section with 5 entries filled")

print("\n=== the end-of-block photo is judged against it ===")
cfg, db, led, eng, bot = make(checker=Checker(delivered=True, what="four more rows"))
tick_at(db, eng, N(16, 0))
bot.cfg.now = lambda: N(16, 1)
run(bot.on_text(FakeUpdate("activities section with 5 entries filled"), None))
# the start-of-block photo, kept as the "before"
for t in (N(16, 1), N(16, 2)):
    tick_at(db, eng, t)
bot.cfg.now = lambda: N(16, 2)
run(bot.on_photo(photo(), None))
row = bot.deliver.open_for(DAY, db.blocks(DAY)[0])
check("the first photo of the block is kept to compare against",
      bool(row["before_path"]) and os.path.exists(row["before_path"]), True)

tick_at(db, eng, N(17, 29))
bot.cfg.now = lambda: N(17, 29)
u = photo()
run(bot.on_photo(u, None))
asked = bot.validator.seen[-1]
check("the checker gets his sentence",
      asked["declared"], "activities section with 5 entries filled")
check("and both photos", asked["had_before"], True)
check("and how long the block was", asked["minutes"], 90)
check("delivered says so", "delivered" in u.message.last, True)
check("and it is recorded", bot.deliver.open_for(DAY, db.blocks(DAY)[0])["state"],
      "delivered")
check("on the ledger too", db.one(
    "SELECT COUNT(*) c FROM events WHERE kind='deliverable_settled'")["c"], 1)

print("\n=== six steps out of ten is still short ===")
cfg, db, led, eng, bot = make(now=N(18, 0),
                              checker=Checker(delivered=False, moved=True,
                                              what="questions 12-15 worked"))
tick_at(db, eng, N(18, 0))
bot.cfg.now = lambda: N(18, 1)
run(bot.on_text(FakeUpdate("questions 12-20 worked in my notebook"), None))
tick_at(db, eng, N(19, 29))
bot.cfg.now = lambda: N(19, 29)
u = photo()
run(bot.on_photo(u, None))
check("it is refused", "not what you said" in u.message.last, True)
check("quoting him to himself", "questions 12-20" in u.message.last, True)
check("the block is short, not delivered",
      bot.deliver.open_for(DAY, db.blocks(DAY)[2])["state"], "short")
check("and the checkpoint is not satisfied", db.one(
    "SELECT state FROM checkpoints WHERE date=? AND kind='deliver' AND due_at LIKE ?",
    (DAY.isoformat(), "%T19:29%"))["state"] in ("asked", "overdue"), True)

print("\n=== moving a bit is not the same as finishing ===")
cfg, db, led, eng, bot = make(now=N(18, 0),
                              checker=Checker(delivered=True, moved=False,
                                              what="nothing visibly different"))
tick_at(db, eng, N(18, 0))
bot.cfg.now = lambda: N(18, 1)
run(bot.on_text(FakeUpdate("questions 12-20 worked"), None))
tick_at(db, eng, N(18, 2))
bot.cfg.now = lambda: N(18, 2)
run(bot.on_photo(photo(), None))
tick_at(db, eng, N(19, 29))
bot.cfg.now = lambda: N(19, 29)
u = photo()
run(bot.on_photo(u, None))
check("the two photos show the same thing, so it is short",
      bot.deliver.open_for(DAY, db.blocks(DAY)[2])["state"], "short")

print("\n=== a checker that cannot answer never fails him ===")


class Dead(Checker):
    async def delivered(self, blob, declared, label, minutes, now, mime="image/jpeg",
                        before=None):
        return {"delivered": True, "moved": True, "unverified": True,
                "what_changed": "", "confidence": 0.0, "reason": "couldn't reach it"}


cfg, db, led, eng, bot = make(now=N(18, 0), checker=Dead())
tick_at(db, eng, N(18, 0))
bot.cfg.now = lambda: N(18, 1)
run(bot.on_text(FakeUpdate("questions 12-20 worked"), None))
tick_at(db, eng, N(19, 29))
bot.cfg.now = lambda: N(19, 29)
run(bot.on_photo(photo(), None))
check("it counts", bot.deliver.open_for(DAY, db.blocks(DAY)[2])["state"], "delivered")
check("and is flagged as unchecked", json.loads(db.one(
    "SELECT payload FROM events WHERE kind='deliverable_settled' ORDER BY id DESC"
    )["payload"])["unverified"], True)

print("\n=== the report says what he promised and what arrived ===")
cfg, db, led, eng, bot = make()
tick_at(db, eng, N(16, 0))
bot.cfg.now = lambda: N(16, 1)
run(bot.on_text(FakeUpdate("activities section with 5 entries filled"), None))
tick_at(db, eng, N(17, 29))
bot.cfg.now = lambda: N(17, 29)
run(bot.on_photo(photo(), None))
text = bot._report_text(DAY)
check("the count is there", "Targets hit 1/" in text, True)
check("his own words are there", "5 entries filled" in text, True)
check("and what the checker saw", "four more rows" in text, True)
check("it never says 'deliverable' to him", "deliverable" in text.lower(), False)

cfg, db, led, eng, bot = make()
check("a day with none says that plainly",
      "No block targets were set" in bot._report_text(DAY), True)

print("\n=== switched off, the day is exactly as it was ===")
cfg, db, led, eng, bot = make(enabled=False)
check("no target checkpoints", [k for k in kinds_at(db, "16:00") if k in
                                ("declare", "deliver")], [])
check("and the ordinary photo is still asked", "activity" in kinds_at(db, "16:00"), True)

sys.exit(report("TARGET TESTS"))
