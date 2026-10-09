"""A stranger who finds the bot gets nothing out of it.

9 Oct 2026, his question: "if another user starts using this bot, can he view
my images?" Photos were already safe -- on_photo checks who is asking. These
did not: /today printed his plan, /status his state, /why the private file he
wrote about his own commitment, /reminders his calendar, and /latenight and
/paper could change what proof he owed.

A Telegram bot's username is discoverable, so "nobody knows it exists" is not
a control.
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
from server import planner

TZ = ZoneInfo("Asia/Almaty")
DAY = date(2026, 8, 25)
run = asyncio.get_event_loop().run_until_complete
STRANGER = 999999


def N(h, m):
    return datetime(2026, 8, 25, h, m, tzinfo=TZ)


def make():
    raw = copy.deepcopy(RAW)
    raw["coach"] = {"enabled": False}
    raw["standup"] = {"enabled": False}
    cfg, db, led, eng, lk = build(raw, now=N(18, 0))
    bot = LockstepBot(cfg, db, led, eng, lk, StubValidator())
    bot.app = types.SimpleNamespace(bot=FakeBot())
    db.set_lock(enabled=1, commitment_end="2099-01-01")
    db.ensure_day(DAY)
    db.set_goals(DAY, ["physics IA", "essay", "gym"])
    db.replace_blocks(DAY, [b.as_tuple() for b in planner.parse(
        "16:00-19:00 work: physics IA\n19:00-22:00 math")])
    db.run("UPDATE days SET plan_locked=1 WHERE date=?", (DAY.isoformat(),))
    return cfg, db, led, eng, bot


def ask(bot, cmd, chat):
    u = FakeUpdate(cmd, chat_id=chat)
    name = {"/today": bot.cmd_today, "/status": bot.cmd_status, "/why": bot.cmd_why,
            "/reminders": bot.cmd_reminders, "/report": bot.cmd_report,
            "/latenight": bot.cmd_latenight, "/paper": bot.cmd_paper,
            "/disable": bot.cmd_disable, "/tasks": bot.cmd_tasks,
            "/plan": bot.cmd_plan, "/goals": bot.cmd_goals}[cmd.split()[0]]
    run(name(u, None))
    return u.message.replies


print("=== a stranger is answered by nothing ===")
cfg, db, led, eng, bot = make()
for cmd in ("/today", "/status", "/why", "/reminders", "/report", "/tasks",
            "/latenight tired", "/paper", "/disable", "/plan", "/goals"):
    check(f"{cmd} says nothing to a stranger", ask(bot, cmd, STRANGER), [])

print("\n=== and he still gets everything ===")
cfg, db, led, eng, bot = make()
check("/today shows his plan", "physics IA" in "".join(ask(bot, "/today", 1)), True)
check("/status answers", bool(ask(bot, "/status", 1)), True)
check("/reminders answers", bool(ask(bot, "/reminders", 1)), True)
check("/report answers", bool(ask(bot, "/report", 1)), True)

print("\n=== his photos were never reachable, and still aren't ===")
cfg, db, led, eng, bot = make()
u = FakeUpdate(chat_id=STRANGER)
u.message.photo = [types.SimpleNamespace(get_file=None)]   # would crash if used
run(bot.on_photo(u, None))
check("a stranger's photo is dropped before anything touches it",
      u.message.replies, [])

print("\n=== buttons only work in his chat ===")
cfg, db, led, eng, bot = make()


class Q:
    def __init__(self, data, chat):
        self.data, self.edited = data, None
        self.message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=chat, send_action=lambda *a, **k: asyncio.sleep(0)),
            reply_text=self._reply)
        self.replies = []
    async def answer(self): pass
    async def edit_message_text(self, t, **kw): self.edited = t
    async def _reply(self, t, **kw): self.replies.append(t)


q = Q(f"goal:{DAY.isoformat()}:1", STRANGER)
run(bot.on_button(types.SimpleNamespace(callback_query=q), None))
check("a goal is not ticked off by a stranger",
      [g["done"] for g in db.goals(DAY)], [0, 0, 0])
check("and nothing is said back", (q.edited, q.replies), (None, []))

print("\n=== the one thing a stranger does get ===")
cfg, db, led, eng, bot = make()
u = FakeUpdate("hello?", chat_id=STRANGER)
run(bot.on_text(u, None))
check("plain text gets the 'not registered' line, on purpose",
      "not registered" in u.message.last, True)
check("it says nothing about his day",
      any(w in u.message.last for w in ("physics", "plan", "goal")), False)
check("and he is told someone turned up",
      any("isn't registered" in t for t in bot.app.bot.to(1)), True)

sys.exit(report("STRANGER TESTS"))
