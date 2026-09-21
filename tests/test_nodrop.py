"""18 Sep 22:33: sixteen numbered items, two of them silently dropped.

"Physics IA submit by 20/09/2026" and "CAS mandatory project" both matched
tasks he already had, so the duplicate guard skipped them without a word --
and the 20 September deadline went with them. He believed they were saved.

Nothing may be dropped in silence. Every action accounts for itself, and a
numbered list is checked line by line in code, because trusting the model to
report its own omissions is exactly the wrong place to trust it.
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

TZ = ZoneInfo("Asia/Almaty")
DAY = date(2026, 8, 25)
run = asyncio.get_event_loop().run_until_complete
NOW = datetime(2026, 8, 25, 22, 33, tzinfo=TZ)

SEED = [{"title": "Physics IA", "keywords": ["physics ia"]},
        {"title": "CAS mandatory project", "keywords": ["cas mandatory"]},
        {"title": "IELTS", "keywords": ["ielts"]}]

HIS_LIST = """1. DS HL IA submit on 19/09/2026
2. Physics IA submit by 20/09/2026
3. IELTS writing & speaking practice
4. CAS summer finish
5. CAS every month rewrite
6. CAS mandatory project
6.1 prepare documents for early application
7. Math solve all worksheets, prep for SA on 24th september"""


def make():
    raw = copy.deepcopy(RAW)
    raw["tasks"] = {"seed": SEED}
    raw["coach"] = {"enabled": True, "max_calls_per_day": 150}
    raw["standup"] = {"enabled": False}
    cfg, db, led, eng, lk = build(raw, now=NOW)
    bot = LockstepBot(cfg, db, led, eng, lk, StubValidator())
    bot.app = types.SimpleNamespace(bot=FakeBot())
    db.set_lock(enabled=1, commitment_end="2099-01-01")
    return cfg, db, led, eng, bot


def model(bot, actions, reply="got them saved"):
    async def fake(system, contents, now, max_tokens=400, schema=None, tier=None):
        fake.system = system
        bot.coach._charge(now.date())
        return json.dumps({"reply": reply, "actions": actions})
    bot.coach._ask = fake


print("=== the two that vanished ===")
cfg, db, led, eng, bot = make()
model(bot, [
    {"type": "add_task", "title": "DS HL IA", "date": "2026-09-19"},
    {"type": "add_task", "title": "Physics IA", "date": "2026-09-20"},
    {"type": "add_task", "title": "CAS mandatory project"},
])
u = FakeUpdate(HIS_LIST)
run(bot.on_text(u, None))

out = u.message.replies[0]
check("the new one is saved", "DS HL IA" in out, True)
check("the one he already had is reported, not dropped",
      "already had #1 Physics IA" in out, True)
check("and its deadline is taken from what he said",
      bot.tasks.find("physics ia")["due"], "2026-09-20")
check("the reply says the deadline was set", "20 Sep" in out, True)
check("the other duplicate is reported too",
      "already had #2 CAS mandatory project" in out, True)
check("no silent skips: every action produced a line", out.count("📌"), 3)

print("\n=== the line-by-line receipt ===")
cfg, db, led, eng, bot = make()
model(bot, [{"type": "add_task", "title": "DS HL IA", "date": "2026-09-19"}],
      reply="saved it")
u = FakeUpdate(HIS_LIST)
run(bot.on_text(u, None))
warn = u.message.replies[-1]
check("he is warned", warn.startswith("⚠️"), True)
check("the missing lines are listed", "Physics IA submit by 20/09/2026" in warn, True)
check("and the sub-item too, written without a dot",
      "prepare documents for early application" in warn, True)
check("the one that landed isn't in the warning", "DS HL IA" in warn, False)

print("\n=== nothing missing, nothing said ===")
cfg, db, led, eng, bot = make()
model(bot, [], reply=("DS HL IA · Physics IA submit · IELTS writing speaking practice · "
                      "CAS summer finish · CAS every month rewrite · CAS mandatory project · "
                      "prepare documents early application · Math solve worksheets prep"))
u = FakeUpdate(HIS_LIST)
run(bot.on_text(u, None))
check("no warning when everything is echoed", len(u.message.replies), 1)

print("\n=== a normal message is not a list ===")
cfg, db, led, eng, bot = make()
model(bot, [], reply="ok")
u = FakeUpdate("yo how's it going")
run(bot.on_text(u, None))
check("no receipt for ordinary chat", len(u.message.replies), 1)
u = FakeUpdate("1. one thing\n2. another")
run(bot.on_text(u, None))
check("two lines is not a list worth policing", len(u.message.replies), 1)

print("\n=== every other action reports itself ===")
cfg, db, led, eng, bot = make()
model(bot, [
    {"type": "complete_task", "task": "something that doesn't exist"},
    {"type": "set_due", "task": "IELTS", "date": "not a date"},
    {"type": "set_due", "task": "IELTS", "date": "2020-01-01"},
    {"type": "add_event", "text": "CAS meeting", "date": "", "time": ""},
    {"type": "add_event", "text": "past thing", "date": "2026-08-24", "time": "10:00"},
    {"type": "add_task", "title": ""},
    {"type": "mark_everything_done"},
    "garbage",
])
u = FakeUpdate("do all that")
run(bot.on_text(u, None))

out = u.message.replies[0]
for want in ("couldn't find a task", "couldn't read the date", "in the past",
             "didn't catch when", "already past", "had no name",
             "didn't know what to do", "couldn't read one of the items"):
    check(f"reports: {want}", want in out, True)
check("and nothing was quietly written", bot.tasks.find("ielts")["due"], None)

print("\n=== the model is told never to drop a line ===")
from server.coach import ACTIONS  # noqa: E402
check("the instruction is there",
      "ONE action per item" in ACTIONS and "dropped line" in ACTIONS.lower(), True)

sys.exit(report("NO-DROP TESTS"))
