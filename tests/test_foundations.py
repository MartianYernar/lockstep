"""The nightly basics: socks, clothes, teeth. Reminded, ticked, never sirened."""
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
from test_friend_fakes import FakeUpdate, StubValidator
from server.bot import LockstepBot

TZ = ZoneInfo("Asia/Almaty")
DAY = date(2026, 8, 25)
run = asyncio.get_event_loop().run_until_complete


def N(h, m, d=25):
    return datetime(2026, 8, d, h, m, tzinfo=TZ)


class Bot:
    def __init__(self): self.sent = []
    async def send_message(self, chat_id, text, reply_markup=None, **_kw):
        self.sent.append((chat_id, text, reply_markup))
    def to(self, cid): return [t for c, t, _ in self.sent if c == cid]
    def keys(self, cid):
        return [m for c, _, m in self.sent if c == cid]


class Query:
    def __init__(self, data):
        self.data, self.edited = data, None
    async def answer(self): pass
    async def edit_message_text(self, text, **kw): self.edited = (text, kw.get("reply_markup"))


def make(now, **conf):
    raw = copy.deepcopy(RAW)
    raw["foundations"] = {"enabled": True, "at": "21:50", "nudge_at": "22:15",
                          "items": ["wash socks", "prepare clothes for tomorrow",
                                    "mouth hygiene"], **conf}
    raw["terms"] = {"рнкп": "research project presentation",
                    "ркнп": "same as рнкп: research project presentation"}
    cfg, db, led, eng, lk = build(raw, now=now)
    bot = LockstepBot(cfg, db, led, eng, lk, StubValidator())
    bot.app = types.SimpleNamespace(bot=Bot())
    db.set_lock(enabled=1, commitment_end="2099-01-01")
    return cfg, db, led, eng, bot


print("=== sent near the end of the day, once ===")
cfg, db, led, eng, bot = make(N(21, 49))
run(bot._foundations_tick(N(21, 49)))
check("not before 21:50", bot.app.bot.to(1), [])
run(bot._foundations_tick(N(21, 50)))
msgs = bot.app.bot.to(1)
check("sent at 21:50", len(msgs), 1)
check("lists all three", all(w in msgs[0] for w in ("socks", "clothes", "mouth")), True)
check("with a button each", len(bot.app.bot.keys(1)[0].inline_keyboard), 3)
run(bot._foundations_tick(N(21, 55)))
check("only once", len(bot.app.bot.to(1)), 1)

print("\n=== ticking them off ===")
q = Query(f"basic:{DAY.isoformat()}:1")
run(bot.on_button(types.SimpleNamespace(callback_query=q), None))
check("socks ticked", bot.foundations.left(DAY)[0]["text"], "prepare clothes for tomorrow")
check("message updated", "✅ wash socks" in q.edited[0], True)
check("two buttons left", len(q.edited[1].inline_keyboard), 2)

print("\n=== the nudge before bed, only if something is left ===")
run(bot._foundations_tick(N(22, 15)))
nudge = bot.app.bot.to(1)[-1]
check("nudged", "still left" in nudge, True)
check("names what's left", "clothes" in nudge and "mouth" in nudge and "socks" not in nudge.split("still left")[1].split("\n")[0], True)
run(bot._foundations_tick(N(22, 20)))
check("nudged once", len(bot.app.bot.to(1)), 2)

cfg, db, led, eng, bot = make(N(21, 50))
run(bot._foundations_tick(N(21, 50)))
for i in (1, 2, 3):
    bot.foundations.tick(DAY, i, N(22, 0))
run(bot._foundations_tick(N(22, 15)))
check("no nudge when all done", len(bot.app.bot.to(1)), 1)
q = Query(f"basic:{DAY.isoformat()}:3")
run(bot.on_button(types.SimpleNamespace(callback_query=q), None))
check("all done message", "all done" in q.edited[0], True)
check("no buttons left", q.edited[1], None)

print("\n=== telling Friday counts too ===")
cfg, db, led, eng, bot = make(N(22, 5))

async def fake(system, contents, now, max_tokens=400, schema=None):
    fake.system = system
    bot.coach._charge(now.date())
    return json.dumps({"reply": "clean 🙌", "actions": [
        {"type": "tick_basic", "text": "brushed my teeth"},
        {"type": "tick_basic", "text": "socks"},
        {"type": "tick_basic", "text": "something else"}]})
bot.coach._ask = fake
u = FakeUpdate("brushed my teeth and washed socks")
run(bot.on_text(u, None))
left = [r["text"] for r in bot.foundations.left(DAY)]
check("teeth and socks ticked", left, ["prepare clothes for tomorrow"])
check("Friday sees tonight's list", "Tonight's basics" in fake.system, True)
check("he's shown what was ticked", u.message.last.count("ticked:"), 2)

print("\n=== after midnight it's still last night's list ===")
cfg, db, led, eng, bot = make(N(0, 30, 26))
check("00:30 belongs to the 25th", bot.foundations.night_of(N(0, 30, 26)), DAY)
u = FakeUpdate("/night")
run(bot.cmd_night(u, None))
check("/night shows the list", "wash socks" in u.message.last, True)

print("\n=== in the nightly report ===")
cfg, db, led, eng, bot = make(N(22, 15))
bot.foundations.tick(DAY, 3, N(22, 0))
text = bot._report_text(DAY)
check("report counts them", "Basics 1/3" in text, True)

print("\n=== can be switched off ===")
cfg, db, led, eng, bot = make(N(21, 50), enabled=False)
run(bot._foundations_tick(N(21, 50)))
check("nothing sent", bot.app.bot.to(1), [])
check("not in the report", "Basics" in bot._report_text(DAY), False)

print("\n=== his typo is one word ===")
cfg, db, led, eng, bot = make(N(18, 0))
t = bot.coach.terms
check("рнкп explained", "presentation" in t.expand("рнкп"), True)
check("ркнп explained the same way", "presentation" in t.expand("ркнп"), True)
t.set("рнкп", "his own words", N(18, 0))
t.seed({"рнкп": "config words"}, N(18, 1))
check("the seed never overwrites what he said", t.found_in("рнкп")[0]["meaning"], "his own words")

sys.exit(report("FOUNDATION TESTS"))
