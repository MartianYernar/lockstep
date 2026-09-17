"""A rejected photo can be explained, and his shorthand is remembered.

17 Sep, 18:56: a screenshot of his presentation was rejected for the block
"рнкп", because the checker had no idea рнкп *is* the presentation. Friday
could neither explain that nor get the photo looked at again, and told him
to "upload it in the app". These tests pin the fix: he explains, the same
photo is checked again with his words in front of the checker, and the
checker -- not the chat -- still decides.
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


def N(h, m):
    return datetime(2026, 8, 25, h, m, tzinfo=TZ)


class Checker:
    """Says no unless the label it's given mentions a presentation."""
    def __init__(self, code="mismatch", yes_word="presentation"):
        self.code, self.yes_word, self.labels = code, yes_word, []

    async def check(self, blob, label, category, now, mime="image/jpeg", kind="activity"):
        self.labels.append(label)
        if self.yes_word and self.yes_word in label.lower():
            return Verdict(ok=True, reason="ok", failure_code="ok")
        return Verdict(ok=False, reason="the image shows a presentation slide deck",
                       failure_code=self.code)


async def _file():
    async def dl():
        from PIL import Image
        buf = io.BytesIO(); Image.new("RGB", (40, 40)).save(buf, "JPEG")
        return bytearray(buf.getvalue())
    return types.SimpleNamespace(download_as_bytearray=dl)


def photo():
    u = FakeUpdate()
    u.message.photo = [types.SimpleNamespace(get_file=_file)]
    return u


def make(now, checker=None):
    cfg, db, led, eng, lk = build(copy.deepcopy(RAW), now=now)
    bot = LockstepBot(cfg, db, led, eng, lk, checker or Checker())
    bot.app = types.SimpleNamespace(bot=FakeBot())
    db.set_lock(enabled=1, commitment_end="2099-01-01")
    db.ensure_day(DAY)
    db.replace_blocks(DAY, [b.as_tuple() for b in planner.parse(
        "16:00-18:00 рнкп\n18:00-22:00 work: essay")])
    db.run("UPDATE days SET plan_locked=1 WHERE date=?", (DAY.isoformat(),))
    eng.build_checkpoints(DAY, N(15, 59))
    eng.tick(N(16, 1))
    return cfg, db, led, eng, bot


def model(bot, reply):
    seen = {"notes": []}

    async def fake(system, contents, now, max_tokens=400, schema=None):
        seen["notes"].append(system)
        bot.coach._charge(now.date())
        return reply
    bot.coach._ask = fake
    return seen


def state(db):
    return db.one("SELECT state FROM checkpoints WHERE date=? ORDER BY due_at LIMIT 1",
                  (DAY.isoformat(),))["state"]


EXPLAIN = json.dumps({"reply": "ahh gotcha", "actions": [
    {"type": "define", "title": "рнкп", "text": "my research project presentation"},
    {"type": "recheck_photo", "text": "рнкп is my presentation, that's the slides"}]})

print("=== 17 Sep, replayed ===")
cfg, db, led, eng, bot = make(N(16, 1))
u = photo()
run(bot.on_photo(u, None))
check("the photo is rejected", u.message.last.startswith("❌"), True)
check("and he's told he can explain it", "tell me what it is" in u.message.last, True)
check("the photo is kept in memory", bot.last_reject is not None, True)

seen = model(bot, EXPLAIN)
u = FakeUpdate("bro but my рнкп is the presentation")
run(bot.on_text(u, None))
check("Friday was told the photo was rejected", "REJECTED" in seen["notes"][0], True)
check("the same photo is checked again, with his words",
      "presentation" in bot.validator.labels[-1] and "He explains" in bot.validator.labels[-1], True)
check("this time it counts", state(db), "satisfied")
check("he hears it", any(r.startswith("✅") for r in u.message.replies), True)
check("the shorthand is remembered", bot.coach.terms.found_in("рнкп")[0]["meaning"],
      "my research project presentation")
check("the photo is let go", bot.last_reject, None)
check("the recheck is on the record",
      db.one("SELECT COUNT(*) c FROM events WHERE kind='photo_rechecked'")["c"], 1)

print("\n=== next time, the checker already knows ===")
cfg, db, led, eng, bot = make(N(16, 1))
bot.coach.terms.set("рнкп", "my research project presentation", N(16, 0))
u = photo()
run(bot.on_photo(u, None))
check("the label is explained to the checker", "presentation" in bot.validator.labels[0], True)
check("so it counts first time", state(db), "satisfied")

print("\n=== explaining can't argue a no into a yes ===")
cfg, db, led, eng, bot = make(N(16, 1), Checker(yes_word=None))
run(bot.on_photo(photo(), None))
model(bot, EXPLAIN)
u = FakeUpdate("it's the presentation bro")
run(bot.on_text(u, None))
check("the checker still says no", "still a no" in u.message.last, True)
check("nothing counted", state(db) in ("asked", "overdue"), True)
run(bot.on_text(FakeUpdate("trust me"), None))
u = FakeUpdate("come on")
run(bot.on_text(u, None))
check("two looks, then it stops", "looked twice" in u.message.last, True)
check("the photo is dropped", bot.last_reject, None)
check("still not counted", state(db) in ("asked", "overdue"), True)

print("\n=== some rejections can't be explained away ===")
for code in ("no_timestamp", "privacy", "stale", "replay"):
    cfg, db, led, eng, bot = make(N(16, 1), Checker(code=code, yes_word=None))
    u = photo()
    run(bot.on_photo(u, None))
    check(f"{code}: nothing kept to recheck", bot.last_reject, None)
    check(f"{code}: no offer to explain", "tell me what it is" in u.message.last, False)

print("\n=== the chat can't invent a recheck ===")
cfg, db, led, eng, bot = make(N(16, 1))
seen = model(bot, EXPLAIN)
u = FakeUpdate("рнкп is my presentation")
run(bot.on_text(u, None))
check("no rejected photo, nothing to recheck", bot.validator.labels, [])
check("nothing counted", state(db) in ("asked", "overdue", "scheduled"), True)
check("Friday wasn't told about a rejection", "REJECTED" in seen["notes"][0], False)

print("\n=== an old rejection expires ===")
cfg, db, led, eng, bot = make(N(16, 1))
run(bot.on_photo(photo(), None))
check("kept at first", bot._live_reject(N(16, 10)) is not None, True)
check("gone after half an hour", bot._live_reject(N(16, 40)), None)

print("\n=== shorthand matching is exact ===")
cfg, db, led, eng, bot = make(N(16, 1))
t = bot.coach.terms
t.set("рнкп", "presentation", N(16, 0))
check("its own block", t.expand("рнкп") != "рнкп", True)
check("a different word left alone", t.expand("ркнп"), "ркнп")
check("inside a longer label", "presentation" in t.expand("work: рнкп slides"), True)
check("not inside another word", t.expand("срнкпх"), "срнкпх")
check("blank definitions refused", t.set("x", "  ", N(16, 0)), False)

sys.exit(report("RECHECK TESTS"))
