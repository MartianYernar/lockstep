"""22 Sep, 08:48-08:58, replayed: "its memory doesn't consolidate".

- "i submitted physics ia and ds hl ia, ркнп is completed" -- the friend wrote
  the tasks back as "#1 Physics IA", every lookup failed, nothing closed, and
  the next list still showed them.
- "change the rec to get the 2 rec letters" made a second task.
- "give me the numbered list of all tasks" came back as raw, cut-off JSON.
- At 08:48 it had no idea what was said the night before.
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
from server.coach import _parse_reply

TZ = ZoneInfo("Asia/Almaty")
run = asyncio.get_event_loop().run_until_complete
SEED = [
    {"title": "Physics IA", "keywords": ["physics ia"], "due": "2026-09-20"},
    {"title": "Math worksheets", "keywords": ["math"], "due": "2026-09-24"},
    {"title": "IELTS", "keywords": ["ielts"]},
    {"title": "РНКП presentation", "keywords": ["рнкп"]},
    {"title": "DS HL IA", "keywords": ["ds hl ia", "ds ia"]},
    {"title": "Prep recom letters", "keywords": ["recom", "rec"]},
]


def N(h, m, d=22):
    return datetime(2026, 9, d, h, m, tzinfo=TZ)


def make(now=N(8, 50)):
    raw = copy.deepcopy(RAW)
    raw["tasks"] = {"seed": SEED}
    raw["terms"] = {"ркнп": "same as рнкп: research project presentation"}
    raw["coach"] = {"enabled": True, "max_calls_per_day": 150, "say_reserve": 0}
    raw["standup"] = {"enabled": False}
    cfg, db, led, eng, lk = build(raw, now=now)
    bot = LockstepBot(cfg, db, led, eng, lk, StubValidator())
    bot.app = types.SimpleNamespace(bot=FakeBot())
    db.set_lock(enabled=1, commitment_end="2099-01-01")
    return cfg, db, led, eng, bot


def model(bot, answer):
    seen = {"system": [], "contents": []}

    async def fake(system, contents, now, max_tokens=400, schema=None, tier=None):
        seen["system"].append(system)
        seen["contents"].append(contents)
        bot.coach._charge(now.date())
        return answer if isinstance(answer, str) else json.dumps(answer)
    bot.coach._ask = fake
    return seen


def talk(bot, text):
    u = FakeUpdate(text)
    run(bot.on_text(u, None))
    return "\n".join(u.message.replies)


def open_titles(bot):
    return [r["title"] for r in bot.tasks.open()]


print("=== the tasks, however they're named back ===")
cfg, db, led, eng, bot = make()
t = bot.tasks
ia, ds = t.find("physics ia"), t.find("ds hl ia")
check("'#1 Physics IA' is task 1", t.find(f"#{ia['id']} Physics IA")["id"], ia["id"])
check("'#12' alone", t.find(f"#{ds['id']}")["id"], ds["id"])
check("'12. DS HL IA'", t.find(f"{ds['id']}. DS HL IA")["id"], ds["id"])
check("a wrong number with the right name follows the name",
      t.find(f"#{ia['id']} DS HL IA")["id"], ds["id"])
check("punctuation and case don't matter", t.find("physics-IA!")["id"], ia["id"])
check("Cyrillic works", t.find("рнкп presentation")["title"], "РНКП presentation")
check("an unknown name is still unknown", t.find("chemistry lab"), None)

print("\n=== 08:51 'i submitted physics ia and ds hl ia, ркнп is completed' ===")
ids = {r["title"]: r["id"] for r in t.open()}
model(bot, {"reply": "let's gooo, three off the list", "actions": [
    {"type": "complete_task", "task": f"#{ids['Physics IA']} Physics IA"},
    {"type": "complete_task", "task": f"#{ids['DS HL IA']} DS HL IA"},
    {"type": "complete_task", "task": f"#{ids['РНКП presentation']} РНКП presentation"},
    {"type": "set_due", "task": f"#{ids['IELTS']} IELTS", "date": "2026-10-18"},
    {"type": "remember", "text": "IELTS exam is on 18 Oct"}]})
out = talk(bot, "bro i submitted physics ia and ds hl ia. i have ielts at 18th oct. "
                "ркнп is completed bro.")
check("nothing came back 'couldn't find'", "couldn't find" in out, False)
check("all three are closed", [x for x in ("Physics IA", "DS HL IA", "РНКП presentation")
                               if x in open_titles(bot)], [])
check("IELTS got its date", t.find("ielts")["due"], "2026-10-18")
check("closing one twice says so, not 'couldn't find'",
      bot.coach.apply([{"type": "complete_task", "task": "Physics IA"}], N(8, 52)),
      ["Physics IA was already closed"])

print("\n=== and it stays that way ===")
check("a restart doesn't bring them back", t.seed(SEED, N(9, 0)), 0)
check("the context no longer lists them", "Physics IA" in bot.coach.context(N(9, 0)), False)
check("the fact is kept for good", "IELTS exam is on 18 Oct" in bot.coach.context(N(9, 0, 25)),
      True)

print("\n=== 08:58 'change the rec to get the 2 rec letters' ===")
rec = t.find("prep recom letters")
model(bot, {"reply": "done", "actions": [
    {"type": "rename_task", "task": f"#{rec['id']} Prep recom letters",
     "title": "Get 2 rec letters"}]})
out = talk(bot, "change the rec to get the 2 rec letters")
check("renamed, not duplicated", open_titles(bot).count("Get 2 rec letters"), 1)
check("the old name is gone", "Prep recom letters" in open_titles(bot), False)
check("it says what changed", "renamed" in out, True)
check("and a restart doesn't bring the old name back", t.seed(SEED, N(9, 5)), 0)
check("the old name still finds it", t.find("prep recom letters")["id"], rec["id"])

dup = t.add("get 2 rec letters", N(9, 6))
out = bot.coach.apply([{"type": "rename_task", "task": f"#{rec['id']}",
                        "title": "get 2 rec letters"}], N(9, 7))
check("renaming onto a task he already has merges them", out[0].startswith("merged"), True)
check("leaving one", [r["title"].lower() for r in t.open()].count("get 2 rec letters"), 1)

print("\n=== 08:58 'give me the numbered list of all tasks' ===")
db.run("INSERT INTO reminders (at_ts, text, created_at) VALUES (?, ?, ?)",
       (N(15, 0, 23).isoformat(), "meeting with Rauan Abiltay", N(8, 48).isoformat()))
model(bot, {"reply": "here's everything:", "actions": [{"type": "show_list", "text": "all"}]})
out = talk(bot, "give me the numbered list of all tasks")
check("every open task is in it, from the database",
      all(x in out for x in open_titles(bot)), True)
check("numbered by id", f"#{t.find('math worksheets')['id']} Math worksheets" in out, True)
check("with the real deadline, said right", "Math worksheets — due Thu 24 Sep (2d)" in out, True)
check("and the events", "meeting with Rauan Abiltay" in out, True)
check("closed ones aren't", "Physics IA" in out, False)

print("\n=== broken JSON is never shown ===")
cut = '{\n  "reply": "here\'s the full list: 1. ds hl ia, 2. physics ia, 3. math'
check("a cut-off reply is dug out", _parse_reply(cut)[0].startswith("here's the full list"), True)
check("with no braces", "{" in _parse_reply(cut)[0], False)
check("garbage becomes a plain line", "{" in _parse_reply('{"actions": [')[0], False)
check("plain text still passes through", _parse_reply("yo")[0], "yo")

print("\n=== tomorrow morning remembers tonight ===")
cfg, db, led, eng, bot = make(N(8, 48))
bot.coach.remember(date(2026, 9, 21), N(23, 10, 21), "user", "i'm doing sat math first tomorrow")
seen = model(bot, {"reply": "yep", "actions": []})
talk(bot, "morning")
check("last night's chat is in front of Friday",
      "sat math first tomorrow" in str(seen["contents"][0]), True)
check("dates are said with the weekday so 'tomorrow' can't be wrong",
      "due in 2 days (Thu 24 Sep)" in bot.coach.context(N(8, 48)), True)

sys.exit(report("MEMORY TESTS"))
