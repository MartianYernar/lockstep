"""A target every 25 minutes, not one per block. 19 Oct 2026, his call.

Ninety minutes is long enough to drift for an hour and still hand something in
at the end. Twenty-five is short enough that the drift shows up while there is
still a day left to fix it -- and each stretch is judged against the photo that
ended the one before, so "did this move" is asked four times an hour instead
of once.
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
from server.vision import Verdict
from server import planner

TZ = ZoneInfo("Asia/Almaty")
DAY = date(2026, 8, 25)
run = asyncio.get_event_loop().run_until_complete
PLAN = ("16:00-17:30 work: uni applications\n17:30-18:00 dinner\n"
        "18:00-18:20 work: quick email\n18:20-22:00 work: essay")


def N(h, m):
    return datetime(2026, 8, 25, h, m, tzinfo=TZ)


class Checker:
    def __init__(self, delivered=True, moved=True, what="two more rows"):
        self.answer = {"delivered": delivered, "moved": moved, "what_changed": what,
                       "confidence": 0.9, "reason": "nothing new is visible",
                       "unverified": False}
        self.seen = []

    async def check(self, blob, label, category, now, mime="image/jpeg", kind="activity"):
        return Verdict(ok=True, reason="ok", failure_code="ok")

    async def delivered(self, blob, declared, label, minutes, now, mime="image/jpeg",
                        before=None):
        self.seen.append({"declared": declared, "minutes": minutes,
                          "had_before": before is not None})
        return dict(self.answer)


async def _file():
    async def dl():
        from PIL import Image
        buf = io.BytesIO(); Image.new("RGB", (800, 600)).save(buf, "JPEG")
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
    raw["penalty"] = {"enabled": False}
    raw["deliver"] = {"enabled": True, "every_min": 25, "min_block_min": 25, **deliver}
    raw["schedule"] = dict(raw["schedule"], mode="block_start", grace_min=3)
    raw["proof"] = {"within_block_min": 20}
    cfg, db, led, eng, lk = build(raw, now=now)
    bot = LockstepBot(cfg, db, led, eng, lk, checker or Checker())
    bot.app = types.SimpleNamespace(bot=FakeBot())
    db.set_lock(enabled=1, commitment_end="2099-01-01")
    db.ensure_day(DAY)
    db.set_goals(DAY, ["a", "b", "c"])
    db.replace_blocks(DAY, [b.as_tuple() for b in planner.parse(PLAN)])
    db.run("UPDATE days SET plan_locked=1 WHERE date=?", (DAY.isoformat(),))
    eng.build_checkpoints(DAY, N(15, 59))
    return cfg, db, led, eng, bot


def at(db, kind):
    return [c["due_at"][11:16] for c in db.q(
        "SELECT due_at FROM checkpoints WHERE date=? AND kind=? ORDER BY due_at",
        (DAY.isoformat(), kind))]


def tick_at(db, eng, when):
    from datetime import timedelta
    db.set_lock(last_tick_at=(when - timedelta(minutes=1)).isoformat())
    return eng.tick(when)


print("=== a 90-minute block is three stretches, not one ===")
cfg, db, led, eng, bot = make()
check("targets asked for at 16:00, 16:25, 16:50",
      at(db, "declare")[:3], ["16:00", "16:25", "16:50"])
check("and shown at the end of each", at(db, "deliver")[:3],
      ["16:24", "16:49", "17:29"])
check("the 15-minute tail folds into the last one, not its own target",
      "17:15" in at(db, "declare"), False)
check("dinner gets none", [t for t in at(db, "declare") if "17:3" in t], [])
check("and so does a 20-minute block", [t for t in at(db, "declare")
                                        if t == "18:00"], [])
check("the long evening block is cut up too",
      at(db, "declare")[3:6], ["18:20", "18:45", "19:10"])

print("\n=== the 20-minute photo stops doubling up ===")
cfg, db, led, eng, bot = make()
check("no separate periodic photo inside a segmented block",
      "16:20" in at(db, "activity"), False)
check("the block still opens with one", at(db, "activity")[0], "16:01")
cfg2, db2, led2, eng2, bot2 = make(every_min=0)
check("with one target per block, the 20-minute photo is back",
      "16:20" in at(db2, "activity"), True)

print("\n=== each stretch gets its own target ===")
cfg, db, led, eng, bot = make()
tick_at(db, eng, N(16, 0))
bot.cfg.now = lambda: N(16, 1)
run(bot.on_text(FakeUpdate("intro paragraph written"), None))
segs = bot.deliver.today(DAY)
check("the first one is stored against 16:00",
      [(s["start_min"], s["declared"]) for s in segs if s["declared"]],
      [(960, "intro paragraph written")])

tick_at(db, eng, N(16, 25))
bot.cfg.now = lambda: N(16, 26)
run(bot.on_text(FakeUpdate("activities section, 3 rows"), None))
check("the second lands on 16:25, not on top of the first",
      sorted((s["start_min"], s["declared"]) for s in bot.deliver.today(DAY)
             if s["declared"]),
      [(960, "intro paragraph written"), (985, "activities section, 3 rows")])
acts = tick_at(db, eng, N(16, 50))     # the third stretch opens
for a in [a for a in acts if a.kind == "ask"]:
    run(bot._dispatch(a, N(16, 50)))
check("and the ask names the stretch's own length, not the block's",
      any("40 min" in t for t in bot.app.bot.to(1)), True)
check("and the time it is due", any("17:30" in t for t in bot.app.bot.to(1)), True)

print("\n=== one stretch is judged against the photo that ended the last ===")
cfg, db, led, eng, bot = make(checker=Checker(delivered=True, what="two more rows"))
tick_at(db, eng, N(16, 0))
bot.cfg.now = lambda: N(16, 1)
run(bot.on_text(FakeUpdate("intro written"), None))
tick_at(db, eng, N(16, 1))
bot.cfg.now = lambda: N(16, 2)
run(bot.on_photo(photo(), None))          # the block's opening photo
tick_at(db, eng, N(16, 24))
bot.cfg.now = lambda: N(16, 24)
run(bot.on_photo(photo(), None))          # ends stretch one
check("the first stretch was judged", bot.validator.seen[-1]["declared"], "intro written")
check("against its own 25 minutes", bot.validator.seen[-1]["minutes"], 25)
check("and it is settled", bot.deliver.today(DAY)[0]["state"], "delivered")
nxt = [s for s in bot.deliver.today(DAY) if s["start_min"] == 985]
check("the next stretch already has its before-photo",
      bool(nxt and nxt[0]["before_path"]), True)

tick_at(db, eng, N(16, 25))
bot.cfg.now = lambda: N(16, 26)
run(bot.on_text(FakeUpdate("three activity rows"), None))
tick_at(db, eng, N(16, 49))
bot.cfg.now = lambda: N(16, 49)
run(bot.on_photo(photo(), None))
check("the second stretch is judged against where the first ended",
      bot.validator.seen[-1]["had_before"], True)
check("with his second target", bot.validator.seen[-1]["declared"], "three activity rows")
got, said = bot.deliver.score(DAY)
check("two targets, two hits", (got, said), (2, 2))

print("\n=== missing one of three does not sink the block ===")
cfg, db, led, eng, bot = make(checker=Checker(delivered=False, what="nothing new"))
tick_at(db, eng, N(16, 0))
bot.cfg.now = lambda: N(16, 1)
run(bot.on_text(FakeUpdate("intro written"), None))
tick_at(db, eng, N(16, 24))
bot.cfg.now = lambda: N(16, 24)
u = photo()
run(bot.on_photo(u, None))
check("that stretch is short", bot.deliver.today(DAY)[0]["state"], "short")
check("and he is told which words he missed", "intro written" in u.message.last, True)
check("the later stretches are still open to win",
      [s["state"] for s in bot.deliver.today(DAY)][1:], ["open"])

print("\n=== the report lists them one by one ===")
text = bot._report_text(DAY)
check("counted", "Targets hit 0/1" in text, True)
check("with his words", "intro written" in text, True)

sys.exit(report("SEGMENT TESTS"))
