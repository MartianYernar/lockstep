"""Exercise the Telegram layer with fake Updates — no network, no token.

bot.py is the biggest module and the one a user actually touches, so the
conversational flows (plan submission, the disable gauntlet, offsets) get
driven here rather than trusted.
"""
import asyncio, os, sys, types
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fixtures import build, check, report, spot_raw
from server.bot import LockstepBot
from server.vision import Verdict

TZ = ZoneInfo("Asia/Almaty")
USER, GUARDIAN = 1, 2


class FakeMessage:
    def __init__(self, text=None, chat_id=USER):
        self.text = text
        self.replies: list[str] = []
        self.deleted = False
        self.photo = []
        self.chat = types.SimpleNamespace(
            id=chat_id, send_action=self._noop_action)
    async def _noop_action(self, *_a, **_k): return None
    async def reply_text(self, t, **_kw): self.replies.append(t); return None
    async def delete(self): self.deleted = True
    @property
    def last(self): return self.replies[-1] if self.replies else ""


class FakeUpdate:
    def __init__(self, text=None, chat_id=USER):
        self.message = FakeMessage(text, chat_id)
        self.effective_chat = types.SimpleNamespace(id=chat_id)


class FakeBot:
    def __init__(self): self.sent: list[tuple[int, str]] = []; self.blocked_by = set()
    async def send_message(self, chat_id, text, **_kw):
        if chat_id in self.blocked_by:
            from telegram.error import Forbidden
            raise Forbidden("bot was blocked by the user")
        self.sent.append((chat_id, text)); return None
    def to(self, chat_id): return [t for c, t in self.sent if c == chat_id]


class StubValidator:
    """Stands in for Gemini so the bot layer can be tested without a key."""
    def __init__(self): self.verdict = Verdict(ok=True, reason="Counted.", failure_code="ok")
    async def check(self, *_a, **_kw): return self.verdict


def make():
    cfg, db, led, eng, lk = build()
    val = StubValidator()
    bot = LockstepBot(cfg, db, led, eng, lk, val)
    bot.app = types.SimpleNamespace(bot=FakeBot())
    db.set_lock(enabled=1, commitment_end="2099-01-01")
    return cfg, db, led, eng, lk, bot, val


run = asyncio.get_event_loop().run_until_complete
GOOD = """16:00-19:00 work: linear algebra pset
19:00-20:00 gym
20:00-22:00 work: essay draft"""

print("\n=== /plan: multiline after the command ===")
cfg, db, led, eng, lk, bot, val = make()
# Capture the target BEFORE submitting: once today is planned,
# target_plan_date() correctly rolls forward to tomorrow.
target = bot.target_plan_date(cfg.now())
u = FakeUpdate("/plan\n" + GOOD)
run(bot.cmd_plan(u, None))
check("plan accepted", "Locked in" in u.message.last, True)
check("blocks stored", len(db.blocks(target)), 3)
check("plan is locked", eng.has_plan(target), True)
check("target rolls to tomorrow once today is planned",
      bot.target_plan_date(cfg.now()) > target, True)

print("\n=== /plan: gaps are refused with the gap named ===")
cfg, db, led, eng, lk, bot, val = make()
u = FakeUpdate("/plan\n16:00-17:00 work\n18:00-22:00 relax")
run(bot.cmd_plan(u, None))
check("refused", u.message.last.startswith("❌"), True)
check("names the actual gap", "17:00-18:00" in u.message.last, True)
check("nothing was stored", len(db.blocks(bot.target_plan_date(cfg.now()))), 0)

print("\n=== /plan: bare command prompts, then the next message is the plan ===")
cfg, db, led, eng, lk, bot, val = make()
u1 = FakeUpdate("/plan")
run(bot.cmd_plan(u1, None))
check("prompts for the plan", "one block per line" in u1.message.last, True)
check("pending state set", bot.pending[USER]["action"], "plan")
u2 = FakeUpdate(GOOD)
run(bot.on_text(u2, None))
check("free text accepted as the plan", "Locked in" in u2.message.last, True)
check("pending cleared", USER in bot.pending, False)

print("\n=== the disable gauntlet ===")
cfg, db, led, eng, lk, bot, val = make()
lk._setup_token = "ABC123"
lk.complete_setup("ABC123", "father-password", GUARDIAN)
bot.cfg.raw["telegram"]["guardian_chat_id"] = GUARDIAN

u = FakeUpdate("/disable")
run(bot.cmd_disable(u, None))
check("asks for a reason first", "reason" in u.message.last.lower(), True)
# The prompt *mentions* the password to explain what's coming; what matters is
# that it will not accept one yet.
check("is not yet accepting a password", bot.pending[USER]["action"], "disable_reason")

u2 = FakeUpdate("too short")
run(bot.on_text(u2, None))
check("a throwaway reason is refused", "Longer" in u2.message.last, True)
check("still awaiting the reason", bot.pending[USER]["action"], "disable_reason")

u3 = FakeUpdate("i have a fever and cannot do any of this today")
run(bot.on_text(u3, None))
g = bot.app.bot.to(GUARDIAN)
check("father is told BEFORE the password step", len(g), 1)
check("his exact words are forwarded", "fever and cannot do any of this" in g[0], True)
check("now asks for the password", bot.pending[USER]["action"], "disable_password")

u4 = FakeUpdate("wrong-guess")
run(bot.on_text(u4, None))
check("wrong password rejected", "Wrong" in u4.message.last, True)
check("password message deleted from the chat", u4.message.deleted, True)
check("system still on", lk.enabled, True)
check("father told about the failed attempt", len(bot.app.bot.to(GUARDIAN)), 2)

u5 = FakeUpdate("father-password")
run(bot.on_text(u5, None))
check("correct password turns it off", lk.enabled, False)
check("father told it went off", "turned OFF" in bot.app.bot.to(GUARDIAN)[-1], True)

u6 = FakeUpdate("/enable")
run(bot.cmd_enable(u6, None))
check("re-enabling needs no password", lk.enabled, True)

print("\n=== /offset: one per day, reason then photo ===")
cfg, db, led, eng, lk, bot, val = make()
today = cfg.today()
# Pin the clock inside the window — this test used to depend on what time of
# day it happened to be run, and quietly took a different branch in the morning.
cfg.now = lambda: datetime.combine(today, datetime.min.time(),
                                   tzinfo=cfg.tz).replace(hour=18, minute=0)
db.ensure_day(today)
db.replace_blocks(today, [(16*60, 22*60, "work: essay", "work")])
db.run("UPDATE days SET plan_locked=1 WHERE date=?", (today.isoformat(),))
eng.build_checkpoints(today, cfg.now())

u = FakeUpdate("/offset x")
run(bot.cmd_offset(u, None))
check("a one-word reason is refused", "actually happened" in u.message.last, True)

u = FakeUpdate("/offset stayed back for a department meeting after class")
run(bot.cmd_offset(u, None))
check("asks for photo proof", "send one photo" in u.message.last.lower(), True)
check("pending proof state", bot.pending[USER]["action"], "offset_proof")

u = FakeUpdate("/offset another reason entirely, second try today")
db.run("UPDATE days SET offsets_used = 1 WHERE date = ?", (today.isoformat(),))
run(bot.cmd_offset(u, None))
check("only one offset a day", "already used" in u.message.last, True)

print("\n=== guardian-only commands reject the user ===")
cfg, db, led, eng, lk, bot, val = make()
bot.cfg.raw["telegram"]["guardian_chat_id"] = GUARDIAN
u = FakeUpdate("/pause 24 whatever", chat_id=USER)
run(bot.cmd_pause(u, None))
check("user cannot pause", "Only the guardian" in u.message.last, True)
check("not actually paused", db.lock()["paused_until"], None)

ug = FakeUpdate("/pause 24 he has a fever", chat_id=GUARDIAN)
run(bot.cmd_pause(ug, None))
check("guardian can pause", "Paused until" in ug.message.last, True)
check("pause recorded", db.lock()["paused_until"] is not None, True)

print("\n=== status renders without blowing up ===")
cfg, db, led, eng, lk, bot, val = make()
u = FakeUpdate("/status")
run(bot.cmd_status(u, None))
check("status produced", len(u.message.last) > 40, True)
check("mentions the guardian password state", "Guardian password" in u.message.last, True)
check("reports the log integrity", "Log:" in u.message.last, True)
print("      ", u.message.last.replace("\n", "\n       "))

print("\n=== a late plan is confirmed honestly, not with a fake first-photo time ===")
cfg, db, led, eng, lk, bot, val = make()
today = cfg.today()
# Pretend it is 18:56 and the window opened at 16:00.
import server.config as _c
real_now = cfg.now
cfg.now = lambda: datetime.combine(today, datetime.min.time(),
                                   tzinfo=cfg.tz).replace(hour=18, minute=56)
u = FakeUpdate("/plan\n16:00-19:00 work: linear algebra pset\n19:00-22:00 work: essay")
run(bot.cmd_plan(u, None))
msg = u.message.last
check("accepted", "Locked in" in msg, True)
check("says the passed slots are void", "void" in msg, True)
check("does not claim a first photo at 16:00", "at <b>16:0" in msg, False)
print("      ", msg.split("\n\n")[-1].replace("\n", "\n       "))
cfg.now = real_now

print("\n=== duplicate nags collapse into one message ===")
from server.engine import Action
merged = bot._merge([
    Action("mark_missed", {"checkpoint_id": 1, "label": "work: linear algebra pset"}),
    Action("mark_missed", {"checkpoint_id": 2, "label": "work: linear algebra pset"}),
])
check("two misses become one action", len([a for a in merged if a.kind == "mark_missed"]), 1)
check("count preserved", merged[0].payload["count"], 2)
check("identical labels deduped", merged[0].payload["labels"], ["work: linear algebra pset"])

merged2 = bot._merge([
    Action("ask", {"checkpoint_id": 1, "label": "a", "category": "work", "due_at": "2026-08-25T16:00:00+05:00"}),
    Action("ask", {"checkpoint_id": 2, "label": "b", "category": "work", "due_at": "2026-08-25T17:00:00+05:00"}),
])
asks = [a for a in merged2 if a.kind == "ask"]
check("two asks become one", len(asks), 1)
check("keeps the newest", asks[0].payload["checkpoint_id"], 2)
check("mentions the other", asks[0].payload["also_due"], 1)

print("\n=== a stranger messaging the bot is surfaced, not swallowed ===")
cfg, db, led, eng, lk, bot, val = make()
bot.cfg.raw["telegram"]["guardian_chat_id"] = 0        # nobody registered yet
DAD = 99
u = FakeUpdate("hello?", chat_id=DAD)
u.effective_chat.first_name = "Yerlan"; u.effective_chat.last_name = ""
run(bot.on_text(u, None))
check("stranger gets an answer", "not registered" in u.message.last, True)
check("stranger is remembered", DAD in bot.seen_strangers, True)
sent = bot.app.bot.to(USER)
check("user is told, with the chat id", any(str(DAD) in t for t in sent), True)
check("user is told the exact command", any("/setguardian" in t for t in sent), True)

print("\n=== /setguardian registers them and sends the setup code ===")
u2 = FakeUpdate(f"/setguardian {DAD}")
run(bot.cmd_setguardian(u2, None))
check("registered", lk.guardian_id(), DAD)
check("confirmed to user", any("Guardian registered" in r for r in u2.message.replies), True)
dad_msgs = bot.app.bot.to(DAD)
check("setup code went to him, not to you", any("/guardian " in t for t in dad_msgs), True)
check("the code never appears in your chat",
      any("/guardian " in t for t in bot.app.bot.to(USER)), False)

print("\n=== you cannot make yourself the guardian ===")
cfg, db, led, eng, lk, bot, val = make()
bot.cfg.raw["telegram"]["guardian_chat_id"] = 0
u3 = FakeUpdate(f"/setguardian {USER}")
run(bot.cmd_setguardian(u3, None))
check("refused", "has to be someone else" in u3.message.last, True)
check("nobody registered", lk.guardian_id(), 0)

print("\n=== once a password is set, the guardian can't be swapped ===")
lk.register_guardian(DAD)
lk._setup_token = "Z"; lk.complete_setup("Z", "fatherpassword", DAD)
u4 = FakeUpdate("/setguardian 12345")
run(bot.cmd_setguardian(u4, None))
check("swap refused", "already set" in u4.message.last, True)
check("still the original guardian", lk.guardian_id(), DAD)

print("\n=== blocking the bot cannot be a quiet exit ===")
cfg, db, led, eng, lk, bot, val = make()
bot.cfg.raw["telegram"]["guardian_chat_id"] = GUARDIAN
run(bot.send_user("checkpoint due"))
check("normal send works", len(bot.app.bot.to(USER)), 1)
check("not marked blocked", db.lock()["blocked_since"], None)

bot.app.bot.blocked_by.add(USER)             # he blocks the bot
run(bot.send_user("checkpoint due"))
check("block detected", db.lock()["blocked_since"] is not None, True)
g = bot.app.bot.to(GUARDIAN)
check("father is told immediately", len(g), 1)
check("told it needed no password", "no password" in g[0], True)
check("told the siren keeps running", "keep going" in g[0], True)
check("recorded in the tamper-evident log",
      "bot_blocked" in [r["kind"] for r in db.q("SELECT kind FROM events")], True)

run(bot.send_user("another checkpoint"))
check("father is not spammed on every later send", len(bot.app.bot.to(GUARDIAN)), 1)

bot.app.bot.blocked_by.discard(USER)         # he unblocks
run(bot.send_user("checkpoint due"))
check("unblock detected", db.lock()["blocked_since"], None)
check("father told it was lifted",
      "unblocked" in bot.app.bot.to(GUARDIAN)[-1], True)
check("both events in the log",
      [r["kind"] for r in db.q("SELECT kind FROM events") if r["kind"].startswith("bot_")],
      ["bot_blocked", "bot_unblocked"])

print("\n=== ...and the phone siren is unaffected by any of it ===")
today = cfg.today()
db.ensure_day(today)
db.replace_blocks(today, [(16*60, 22*60, "work: essay", "work")])
db.run("UPDATE days SET plan_locked=1 WHERE date=?", (today.isoformat(),))
noon = datetime.combine(today, datetime.min.time(), tzinfo=cfg.tz).replace(hour=18)
eng.build_checkpoints(today, datetime.combine(today, datetime.min.time(),
                                              tzinfo=cfg.tz).replace(hour=15, minute=59))
db.run("UPDATE checkpoints SET state='overdue', asked_at=? WHERE date=? AND due_at <= ?",
       (noon.isoformat(), today.isoformat(), noon.isoformat()))
db.set_lock(blocked_since=noon.isoformat())
st = eng.agent_state(noon)
check("agent state ignores the block entirely", st.active, True)
check("siren still armed", st.phone_alarm, True)
print(f"       debt {st.debt_minutes} min, alarm {st.alarm_repeats}x/min — served over HTTP, not Telegram")

sys.exit(report("BOT TESTS"))
