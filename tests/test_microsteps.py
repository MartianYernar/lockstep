"""The ten-minute loop, 18 Oct 2026.

    "every 10min I need to write the bot the next super simple step that I am
     gonna do and then send text message that I did it and write next super
     simple step. bro but i kinda dont want to send image every 10 min"

Text is not proof and never becomes proof here. What it changes is the photo:
it is checked against the step he claimed, and while he answers the loop the
cadence relaxes from 20 minutes to 30. Stop answering and it tightens back.
"""
import asyncio
import copy
import json
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
PLAN = "16:00-19:00 work: uni essay\n19:00-19:30 dinner\n19:30-22:00 work: IELTS"


def N(h, m):
    return datetime(2026, 8, 25, h, m, tzinfo=TZ)


def make(now=N(16, 10), **micro):
    raw = copy.deepcopy(RAW)
    raw["coach"] = {"enabled": True, "max_calls_per_day": 150, "say_reserve": 0}
    raw["standup"] = {"enabled": False}
    raw["schedule"] = dict(raw["schedule"], mode="block_start", grace_min=3)
    raw["proof"] = {"within_block_min": 20}
    raw["micro"] = {"enabled": True, "every_min": 10, "defer_photo_min": 10,
                    "alive_min": 14, "skip_categories": ["commute", "meal", "sleep",
                                                         "relax"], **micro}
    cfg, db, led, eng, lk = build(raw, now=now)
    bot = LockstepBot(cfg, db, led, eng, lk, StubValidator())
    bot.app = types.SimpleNamespace(bot=FakeBot())
    db.set_lock(enabled=1, commitment_end="2099-01-01")
    db.ensure_day(DAY)
    db.set_goals(DAY, ["essay", "ielts", "rest"])
    db.replace_blocks(DAY, [b.as_tuple() for b in planner.parse(PLAN)])
    db.run("UPDATE days SET plan_locked=1 WHERE date=?", (DAY.isoformat(),))
    eng.build_checkpoints(DAY, N(15, 59))
    return cfg, db, led, eng, bot


def model(bot, reply="hey"):
    seen = {"n": 0, "system": ""}

    async def fake(system, contents, now, max_tokens=400, schema=None, tier=None):
        seen["n"] += 1
        seen["system"] = system
        bot.coach._charge(now.date())
        return json.dumps({"reply": reply, "actions": []})
    bot.coach._ask = fake
    return seen


def sent(bot):
    return bot.app.bot.to(1)


def say(bot, text, now):
    u = FakeUpdate(text)
    bot.cfg.now = lambda: now
    run(bot.on_text(u, None))
    return u


print("=== the loop ===")
cfg, db, led, eng, bot = make()
seen = model(bot)
run(bot._steps_tick(N(16, 10)))
check("asked on the ten-minute mark", len(sent(bot)), 1)
check("and it asks for one small thing", "next" in sent(bot)[0].lower(), True)
check("free: no model was called", seen["n"], 0)

u = say(bot, "open the essay doc and read my intro", N(16, 11))
check("his step is taken, not sent to Friday", seen["n"], 0)
check("and acknowledged instantly", "go" in sent(bot)[-1], True)
check("it is on the record", bot.steps.open_step(N(16, 12))["step"],
      "open the essay doc and read my intro")

u = say(bot, "done", N(16, 18))
check("done closes it", bot.steps.open_step(N(16, 19)), None)
check("and the next one is asked straight away", "next" in sent(bot)[-1].lower(), True)
check("the count is his", bot.steps.score(DAY), (1, 1))
check("still free", seen["n"], 0)

for word in ("done", "did it", "✅", "готово", "сделал", "finished"):
    cfg2, db2, led2, eng2, bot2 = make()
    bot2.steps.start_free("x", N(16, 11), "work: uni essay")
    model(bot2)
    say(bot2, word, N(16, 12))
    check(f"{word!r} counts as done", bot2.steps.score(DAY)[0], 1)

print("\n=== it never eats a real message ===")
cfg, db, led, eng, bot = make()
seen = model(bot, "bro what")
say(bot, "bro what should I even write in the intro?", N(16, 11))
check("a question goes to Friday, not into the loop", seen["n"], 1)
cfg, db, led, eng, bot = make()
seen = model(bot)
say(bot, "i am still filling out info", N(16, 11))
check("'still' is a more-time request, not a step", seen["n"], 0)
check("and no step was recorded", bot.steps.waiting(N(16, 12)), None)

print("\n=== /step ===")
cfg, db, led, eng, bot = make()
u = FakeUpdate("/step write the hook sentence")
run(bot.cmd_step(u, None))
check("he can name one unasked", bot.steps.open_step(N(16, 11))["step"],
      "write the hook sentence")
u = FakeUpdate("/step")
run(bot.cmd_step(u, None))
check("/step alone lists them", "steps today" in u.message.last, True)
check("with what he is on", "write the hook sentence" in u.message.last, True)

print("\n=== the photo relaxes to 30 minutes while he answers ===")


def ticks(eng, start, end):
    """Tick every minute, the way the server actually does."""
    out, t = [], start
    while t <= end:
        out += [(a.kind, a.payload.get("due_at", "")[11:16]) for a in eng.tick(t)]
        t += timedelta(minutes=1)
    return out


def asked(eng, start, end):
    return [due for kind, due in ticks(eng, start, end) if kind == "ask"]


cfg, db, led, eng, bot = make()
bot.steps.start_free("read my intro", N(16, 12), "work: uni essay")
seen20 = asked(eng, N(16, 0), N(16, 20))
check("the block's first photo is still asked at 16:00", "16:00" in seen20, True)
check("but the 16:20 one is not", "16:20" in seen20, False)
moved = db.one("SELECT due_at FROM checkpoints WHERE date=? AND deferred=1",
               (DAY.isoformat(),))
check("it slid ten minutes", moved["due_at"][11:16], "16:30")
check("and is asked then", "16:30" in asked(eng, N(16, 21), N(16, 31)), True)
check("only one slide", db.one(
    "SELECT COUNT(*) c FROM checkpoints WHERE date=? AND deferred=1",
    (DAY.isoformat(),))["c"], 1)

print("\n=== stop answering and it tightens back ===")
cfg, db, led, eng, bot = make()
check("nothing written, so the photo comes at 20",
      "16:20" in asked(eng, N(16, 0), N(16, 20)), True)

cfg, db, led, eng, bot = make()
bot.steps.start_free("x", N(16, 0), "work: uni essay")      # twenty minutes ago
check("an old step buys nothing", "16:20" in asked(eng, N(16, 0), N(16, 20)), True)

print("\n=== the first photo of a block never slides ===")
cfg, db, led, eng, bot = make(now=N(19, 25))
bot.steps.start_free("open the ielts book", N(19, 28), "work: IELTS")
check("the block-start photo is asked on time",
      "19:30" in asked(eng, N(19, 29), N(19, 31)), True)

print("\n=== the photo is judged against the step he claimed ===")


class Spy:
    def __init__(self):
        self.labels = []

    async def check(self, blob, label, category, now, mime="image/jpeg", kind="activity"):
        from server.vision import Verdict
        self.labels.append(label)
        return Verdict(ok=True, reason="ok", failure_code="ok")


async def _file():
    async def dl():
        from PIL import Image
        import io
        buf = io.BytesIO(); Image.new("RGB", (40, 40)).save(buf, "JPEG")
        return bytearray(buf.getvalue())
    return types.SimpleNamespace(download_as_bytearray=dl)


cfg, db, led, eng, bot = make()
bot.validator = Spy()
bot.steps.start_free("write the hook sentence", N(16, 12), "work: uni essay")
eng.tick(N(16, 20))
u = FakeUpdate()
u.message.photo = [types.SimpleNamespace(get_file=_file)]
cfg.now = lambda: N(16, 21)
run(bot.on_photo(u, None))
check("the checker is told the block", "uni essay" in bot.validator.labels[0], True)
check("and what he said he was doing this minute",
      "write the hook sentence" in bot.validator.labels[0], True)

print("\n=== not during dinner, not during the exam ===")
cfg, db, led, eng, bot = make(now=N(19, 10))
run(bot._steps_tick(N(19, 10)))
check("dinner is left alone", sent(bot), [])

print("\n=== Friday sees the list ===")
cfg, db, led, eng, bot = make()
bot.steps.start_free("read my intro", N(16, 11), "work: uni essay")
bot.steps.mark_done(N(16, 17))
bot.steps.start_free("write the hook", N(16, 18), "work: uni essay")
seen = model(bot, "keep going")
say(bot, "bro how is it looking?", N(16, 25))
check("the steps are in his context", "write the hook" in seen["system"], True)
check("with the one he finished ticked", "✓ read my intro" in seen["system"], True)

print("\n=== switched off, nothing changes ===")
cfg, db, led, eng, bot = make(enabled=False)
run(bot._steps_tick(N(16, 10)))
check("no questions", sent(bot), [])
check("and the photo comes at 20 as before",
      "16:20" in asked(eng, N(16, 0), N(16, 20)), True)
check("nothing was deferred", db.one(
    "SELECT COUNT(*) c FROM checkpoints WHERE date=? AND deferred=1",
    (DAY.isoformat(),))["c"], 0)

sys.exit(report("MICROSTEP TESTS"))
