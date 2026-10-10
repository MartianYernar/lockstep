"""The day he wrote in Google Docs is the day the planner builds.

10 Oct 2026: a fresh database re-seeded "Physics IA, due 20 Sep" from a config
written in September, and the bot told him it was twenty days overdue -- a
thing he had finished and said so, twice. The real list lives in a doc he
keeps by hand, one section per day, boxes ticked as he goes.

The sample below is his actual doc, trimmed. Markdown is what the parser is
fed because the plain-text export drops the checkboxes entirely, and without
them a finished item looks exactly like an unfinished one.
"""
import asyncio
import copy
import os
import sys
import types
from datetime import date, datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fixtures import build, check, report, RAW
from test_friend_fakes import FakeUpdate, FakeBot, StubValidator
from server.bot import LockstepBot
from server.dailydoc import DailyDoc, export_url, parse

TZ = ZoneInfo("Asia/Almaty")
run = asyncio.get_event_loop().run_until_complete

DOC = """# **WEEK 1 · hour by hour**

*This week only, the plan is scheduled by time block.*

**SUNDAY 4 oct**   tonight    **SALVAGE BLOCK — 90 MINUTES, NOT A FULL DAY**

**60 min   EE: the plan only — no prose**

- [x]   ~~Write the research question in its final wording.~~
- [x] ~~**Write your argument in ONE sentence.**~~
- [x]   ~~List the section headings with a word budget each: Intro 400\\.~~
- [ ] finish reflection of may
- [ ] prepare for CAS gallery

**MONDAY 5 oct**

**School hours:**

- [ ] finish rewriting reflection of may
- [x] ~~finish CAS summer~~
- [ ] finish CAS mandatory project
- [ ] talk with Physics teacher about forecast IB grades

**After school:**

- [ ] start U of T application
"""

TODAY = date(2026, 10, 5)                      # the Monday in the sample
NOW = datetime(2026, 10, 5, 17, 30, tzinfo=TZ)

print("=== reading his doc ===")
secs = parse(DOC, TODAY)
check("both days found", [s["date"] for s in secs], [date(2026, 10, 4), TODAY])
mon = secs[1]
check("every line of Monday", len(mon["items"]), 5)
check("ticked ones are known to be done",
      [i["done"] for i in mon["items"]], [False, True, False, False, False])
check("the strikethrough is stripped off", mon["items"][1]["text"], "finish CAS summer")
check("so is the bold", secs[0]["items"][1]["text"],
      "Write your argument in ONE sentence.")
check("and the exporter's escapes", "Intro 400." in secs[0]["items"][2]["text"], True)
check("the sub-heading is kept with the item", mon["items"][0]["group"], "School hours:")
check("and it changes where he changed it", mon["items"][-1]["group"], "After school:")
check("what is actually left", [i["text"] for i in DailyDoc.open_items(mon)],
      ["finish rewriting reflection of may", "finish CAS mandatory project",
       "talk with Physics teacher about forecast IB grades",
       "start U of T application"])

print("\n=== dates the way he writes them ===")
for heading, want in (("**TUESDAY 13 oct**", date(2026, 10, 13)),
                      ("WEDNESDAY 1 November", date(2026, 11, 1)),
                      ("**Fri, 7 nov**", date(2026, 11, 7)),
                      ("SUNDAY oct 12", date(2026, 10, 12))):
    got = parse(heading + "\n- [ ] x\n", TODAY)
    check(f"{heading!r}", got[0]["date"] if got else None, want)
check("a heading with no date is not a day", parse("**School hours:**\n- [ ] x\n", TODAY), [])
check("January from December is next year, not eleven months ago",
      parse("**FRIDAY 3 jan**\n- [ ] x\n", date(2026, 12, 20))[0]["date"], date(2027, 1, 3))

print("\n=== the link he pastes ===")
ID = "1JnJJYnkhPumHqpnP8a2ntyGl7TwUGEvmE1p_Q4iZ_L8"
want = f"https://docs.google.com/document/d/{ID}/export?format=md"
check("an /edit link", export_url(f"https://docs.google.com/document/d/{ID}/edit?usp=sharing"), want)
check("a bare id", export_url(ID), want)
check("an export link already", export_url(want), want)


def make(doc_text=DOC, url="https://docs.google.com/document/d/x" + "y" * 25 + "/edit"):
    raw = copy.deepcopy(RAW)
    raw["coach"] = {"enabled": True, "max_calls_per_day": 150, "say_reserve": 0}
    raw["standup"] = {"enabled": False}
    raw["daily_doc"] = {"enabled": True, "url": url, "refresh_min": 20}
    cfg, db, led, eng, lk = build(raw, now=NOW)
    bot = LockstepBot(cfg, db, led, eng, lk, StubValidator())
    bot.app = types.SimpleNamespace(bot=FakeBot())
    db.set_lock(enabled=1, commitment_end="2099-01-01")
    fetches = {"n": 0}

    async def fake_text(now, force=False):
        if doc_text is None:
            return None
        if bot.doc._text is not None and bot.doc._fresh(now) and not force:
            return bot.doc._text
        fetches["n"] += 1
        bot.doc._text, bot.doc._at = doc_text, now
        return doc_text
    bot.doc.text = fake_text
    return cfg, db, led, eng, bot, fetches


print("\n=== /doc shows him what the bot reads ===")
cfg, db, led, eng, bot, fetches = make()
u = FakeUpdate("/doc")
run(bot.cmd_doc(u, None))
out = u.message.last
check("today's section", "Mon 05 Oct" in out, True)
check("counted what's left", "4 left of 5" in out, True)
check("done ones shown as done", "[x] finish CAS summer" in out, True)
check("the groups are there", "School hours:" in out, True)

u = FakeUpdate("/doc tomorrow")
run(bot.cmd_doc(u, None))
check("a day he hasn't written yet says so", "nothing in the doc" in u.message.last, True)
check("and lists the days it can see", "Sun 04 Oct" in u.message.last, True)

print("\n=== the planner is given his list ===")
cfg, db, led, eng, bot, fetches = make()
seen = {}


async def fake_ask(system, contents, now, max_tokens=400, schema=None, tier=None):
    seen["prompt"] = "\n".join(str(c) for c in contents)
    seen["system"] = system
    bot.coach._charge(now.date())
    return ('{"blocks": [{"start": "16:00", "end": "22:00", '
            '"label": "CAS reflections"}], "note": "from your doc"}')
bot.coach._ask = fake_ask
db.ensure_day(TODAY)
db.set_goals(TODAY, ["CAS", "uni", "rest"])
blocks, note = run(bot.dayplanner.build(TODAY, "", NOW))
check("his unticked lines reached the planner",
      "finish CAS mandatory project" in seen["prompt"], True)
check("so did the ticked ones, marked done",
      "[x] finish CAS summer" in seen["prompt"], True)
check("it is told not to re-plan those",
      "do not plan it again" in seen["prompt"] or "already done" in seen["system"], True)
check("and the rule is in the system prompt", "HIS LIST FOR TODAY" in seen["system"], True)

print("\n=== Friday sees it too ===")
cfg, db, led, eng, bot, fetches = make()


async def friday(system, contents, now, max_tokens=400, schema=None, tier=None):
    seen["system"] = system
    bot.coach._charge(now.date())
    return '{"reply": "ok", "actions": []}'
bot.coach._ask = friday
run(bot.on_text(FakeUpdate("what should I do rn"), None))
check("today's list is in his context", "finish CAS mandatory project" in seen["system"], True)
check("and it is told the doc is not proof",
      "not proof" in seen["system"], True)

print("\n=== a doc that will not load never stops the day ===")
cfg, db, led, eng, bot, fetches = make(doc_text=None)
u = FakeUpdate("/doc")
run(bot.cmd_doc(u, None))
check("he is told plainly", "can't read any day headings" in u.message.last
      or "nothing in the doc" in u.message.last, True)
bot.coach._ask = fake_ask
db.ensure_day(TODAY); db.set_goals(TODAY, ["a", "b", "c"])
blocks, note = run(bot.dayplanner.build(TODAY, "", NOW))
check("and the plan is still built", len(blocks) > 0, True)

print("\n=== it is not fetched on every message ===")
cfg, db, led, eng, bot, fetches = make()
run(bot.doc.for_day(TODAY, NOW))
run(bot.doc.for_day(TODAY, NOW))
check("cached inside the refresh window", fetches["n"], 1)
later = NOW.replace(hour=18, minute=30)
run(bot.doc.for_day(TODAY, later))
check("fetched again once it is stale", fetches["n"], 2)
run(bot.doc.for_day(TODAY, later, force=True))
check("/doc always asks for a fresh copy", fetches["n"], 3)

print("\n=== switched off by default ===")
raw = copy.deepcopy(RAW)
cfg, db, led, eng, lk = build(raw, now=NOW)
d = DailyDoc(cfg)
check("no config, nothing enabled", d.enabled, False)
check("and nothing is fetched", run(d.text(NOW)), None)

sys.exit(report("DAILY DOC TESTS"))
