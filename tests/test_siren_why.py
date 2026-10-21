"""21 Oct, 19:20-19:30: three loops talking over each other.

- "bro why the siren is going, what should i send to stop it" -> "go. tell
  me when it's done." It should have been told, from the record, what to send.
- The ten-minute step question kept coming while a target was owed, so his
  step answer was locked in as his 25-minute target.
- A photo captioned with his next step had the caption dropped.
"""
import asyncio
import copy
import io
import os
import sys
import types
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fixtures import build, check, report, RAW
from test_friend_fakes import FakeUpdate, FakeBot, StubValidator
from server.bot import LockstepBot
from server import planner

TZ = ZoneInfo("Asia/Almaty")
DAY = date(2026, 8, 25)
run = asyncio.get_event_loop().run_until_complete
PLAN = "17:00-19:00 dinner and rest\n19:00-21:00 work: KAIST application\n21:00-22:00 clean room"


def N(h, m):
    return datetime(2026, 8, 25, h, m, tzinfo=TZ)


def tick_at(db, eng, when):
    db.set_lock(last_tick_at=(when - timedelta(minutes=1)).isoformat())
    return eng.tick(when)


def make(deliver=True, micro=True):
    raw = copy.deepcopy(RAW)
    raw["coach"] = {"enabled": False}
    raw["standup"] = {"enabled": False}
    raw["penalty"] = {"enabled": False}
    raw["micro"] = {"enabled": micro, "every_min": 10}
    raw["deliver"] = {"enabled": deliver, "every_min": 25, "min_block_min": 25}
    raw["schedule"] = dict(raw["schedule"], mode="block_start", grace_min=3)
    raw["proof"] = {"within_block_min": 20}
    cfg, db, led, eng, lk = build(raw, now=N(18, 59))
    bot = LockstepBot(cfg, db, led, eng, lk, StubValidator())
    bot.app = types.SimpleNamespace(bot=FakeBot())
    db.set_lock(enabled=1, commitment_end="2099-01-01")
    db.ensure_day(DAY)
    db.set_goals(DAY, ["a", "b", "c"])
    db.replace_blocks(DAY, [b.as_tuple() for b in planner.parse(PLAN)])
    db.run("UPDATE days SET plan_locked=1 WHERE date=?", (DAY.isoformat(),))
    eng.build_checkpoints(DAY, N(18, 59))
    return cfg, db, led, eng, bot


print("=== 'why is the siren going' gets the list, not 'go' ===")
cfg, db, led, eng, bot = make()
tick_at(db, eng, N(19, 0))
tick_at(db, eng, N(19, 5))
bot.cfg.now = lambda: N(19, 5)
u = FakeUpdate("bro why the siren is going, what should i send to stop it")
run(bot.on_text(u, None))
out = u.message.last
check("it is not taken as a step", "go. tell me" in out, False)
check("it says what to send", "to stop it, send" in out, True)
check("the target, as one line of text", "one line of text" in out, True)
check("with the time it is for", "19:25" in out, True)
check("and the opening photo", "a photo of you on it" in out, True)
check("both, since both are owed", "all of them" in out, True)
check("nothing was locked as a target",
      [r for r in bot.deliver.today(DAY) if r["declared"]], [])

print("\n=== nothing owed: says so ===")
cfg, db, led, eng, bot = make()
bot.cfg.now = lambda: N(18, 30)
u = FakeUpdate("why is the siren going")
run(bot.on_text(u, None))
check("nothing's owed", "nothing's owed" in u.message.last, True)

print("\n=== the step question waits while something is owed ===")
cfg, db, led, eng, bot = make()
tick_at(db, eng, N(19, 0))
bot.cfg.now = lambda: N(19, 10)
run(bot._steps_tick(N(19, 10)))
check("no step question on top of an owed target",
      any("small step" in t or "next" in t for t in bot.app.bot.to(1)), False)

cfg, db, led, eng, bot = make(deliver=False)
tick_at(db, eng, N(19, 0))
tick_at(db, eng, N(19, 2))
bot.cfg.now = lambda: N(19, 2)
for c in eng.open_checkpoints(DAY):
    db.run("UPDATE checkpoints SET state='satisfied' WHERE id=?", (c["id"],))
run(bot._steps_tick(N(19, 10)))
check("once nothing is owed, it asks again", len(bot.app.bot.to(1)) > 0, True)

print("\n=== a caption answers the step question ===")


async def _file():
    async def dl():
        from PIL import Image
        buf = io.BytesIO(); Image.new("RGB", (800, 600)).save(buf, "JPEG")
        return bytearray(buf.getvalue())
    return types.SimpleNamespace(download_as_bytearray=dl)


cfg, db, led, eng, bot = make(deliver=False)
bot.steps.ask(N(19, 20), "work: KAIST application")
u = FakeUpdate()
u.message.photo = [types.SimpleNamespace(get_file=_file)]
u.message.caption = "to attach the 10th grade and 11th grade files"
bot.cfg.now = lambda: N(19, 21)
run(bot.on_photo(u, None))
row = db.one("SELECT step FROM microsteps ORDER BY id DESC LIMIT 1")
check("his caption is the step", row["step"], "to attach the 10th grade and 11th grade files")

sys.exit(report("SIREN WHY"))
