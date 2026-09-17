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
GOALS = ["finish linear algebra pset ch3",
         "first draft of the essay, any quality",
         "gym, legs"]


def goalup(db, bot, cfg):
    """Goals gate the plan now, so a plan test has to set them first."""
    t = bot.target_plan_date(cfg.now())
    db.ensure_day(t)
    db.set_goals(t, GOALS)
    return t

print("\n=== /plan: multiline after the command ===")
cfg, db, led, eng, lk, bot, val = make()
# Capture the target BEFORE submitting: once today is planned,
# target_plan_date() correctly rolls forward to tomorrow.
target = bot.target_plan_date(cfg.now())
db.ensure_day(target); db.set_goals(target, GOALS)
u = FakeUpdate("/plan\n" + GOOD)
run(bot.cmd_plan(u, None))
check("plan accepted", "locked in" in u.message.last.lower(), True)
check("blocks stored", len(db.blocks(target)), 3)
check("plan is locked", eng.has_plan(target), True)
check("target rolls to tomorrow once today is planned",
      bot.target_plan_date(cfg.now()) > target, True)

print("\n=== /plan: gaps are refused with the gap named ===")
cfg, db, led, eng, lk, bot, val = make()
goalup(db, bot, cfg)
u = FakeUpdate("/plan\n16:00-17:00 work\n18:00-22:00 relax")
run(bot.cmd_plan(u, None))
check("refused", u.message.last.startswith("❌"), True)
check("names the actual gap", "17:00-18:00" in u.message.last, True)
check("nothing was stored", len(db.blocks(bot.target_plan_date(cfg.now()))), 0)

print("\n=== /plan: bare command prompts, then the next message is the plan ===")
cfg, db, led, eng, lk, bot, val = make()
goalup(db, bot, cfg)
u1 = FakeUpdate("/plan")
run(bot.cmd_plan(u1, None))
check("prompts for the plan", "one block per line" in u1.message.last, True)
check("pending state set", bot.pending[USER]["action"], "plan")
u2 = FakeUpdate(GOOD)
run(bot.on_text(u2, None))
check("free text accepted as the plan", "locked in" in u2.message.last.lower(), True)
check("pending cleared", USER in bot.pending, False)

print("\n=== only your father can stop it — there is no password to type ===")
cfg, db, led, eng, lk, bot, val = make()
bot.cfg.raw["telegram"]["guardian_chat_id"] = GUARDIAN

u = FakeUpdate("/disable")
run(bot.cmd_disable(u, None))
check("asks you to write why", "Write why" in u.message.last, True)
check("says only he can, and not for 24h", "not for 24 hours" in u.message.last, True)
check("awaiting the reason", bot.pending[USER]["action"], "disable_reason")

u2 = FakeUpdate("too short")
run(bot.on_text(u2, None))
check("a throwaway reason is refused", "write more" in u2.message.last.lower(), True)

u3 = FakeUpdate("i have a fever and cannot do any of this today")
run(bot.on_text(u3, None))
g = bot.app.bot.to(GUARDIAN)
check("father is told", len(g), 1)
check("his exact words are forwarded", "fever and cannot do any of this" in g[0], True)
check("father is told the command", "/stopbot" in g[0], True)
check("father is told he may simply ignore it", "do nothing" in g[0].lower(), True)
check("system is still on — asking is not stopping", lk.enabled, True)

print("\n=== you cannot stop it yourself, by any route ===")
for cmd, fn in (("/stopbot", bot.cmd_stopbot), ("/startbot", bot.cmd_startbot)):
    u4 = FakeUpdate(cmd, chat_id=USER)
    run(fn(u4, None))
    check(f"{cmd} refused from you", "Only your father" in u4.message.last, True)
check("still on", lk.enabled, True)

ue = FakeUpdate("/enable", chat_id=USER)
run(bot.cmd_enable(ue, None))
check("/enable is not yours either", "not your call" in ue.message.last.lower(), True)

print("\n=== his stop is a delay, not a switch ===")
now0 = cfg.now()
ug = FakeUpdate("/stopbot he says he is done", chat_id=GUARDIAN)
run(bot.cmd_stopbot(ug, None))
check("system is STILL ON", lk.enabled, True)
check("a stop is scheduled", lk.pending_stop() is not None, True)
check("24 hours out", int((lk.pending_stop() - now0).total_seconds() // 3600), 24)
check("he is told why the delay exists", "urge is a state" in ug.message.last, True)
check("you are told when", "not now" in bot.app.bot.to(USER)[-1].lower(), True)

print("\n=== the urge passes, and you can say so yourself ===")
un = FakeUpdate("/nevermind")
run(bot.cmd_nevermind(un, None))
check("called off", lk.pending_stop(), None)
check("still running", lk.enabled, True)
check("names what just happened", "feeling passed" in un.message.last, True)
check("father told he need not act", "Nothing for you to do" in bot.app.bot.to(GUARDIAN)[-1], True)
check("withdrawal is on the record",
      "stop_withdrawn_by_user" in [r["kind"] for r in db.q("SELECT kind FROM events")], True)

print("\n=== the delay actually fires when it runs out ===")
run(bot.cmd_stopbot(FakeUpdate("/stopbot done", chat_id=GUARDIAN), None))
later = now0 + timedelta(hours=25)
cfg.now = lambda: later
run(bot.tick(None))
check("stopped once the time came", lk.enabled, False)
check("nothing left scheduled", lk.pending_stop(), None)

print("\n=== but a real emergency doesn't wait ===")
cfg.now = lambda: now0
lk.enable(now0)
ue = FakeUpdate("/stopnow he is in hospital", chat_id=GUARDIAN)
run(bot.cmd_stopnow(ue, None))
check("immediate", lk.enabled, False)
check("no delay involved", lk.pending_stop(), None)

print("\n=== and only he restarts it ===")
run(bot.cmd_enable(FakeUpdate("/enable", chat_id=USER), None))
check("you cannot", lk.enabled, False)
run(bot.cmd_startbot(FakeUpdate("/startbot", chat_id=GUARDIAN), None))
check("he can", lk.enabled, True)

print("\n=== /offset: one per day, reason then photo ===")
cfg, db, led, eng, lk, bot, val = make()
today = cfg.today()
db.ensure_day(today)
db.replace_blocks(today, [(16*60, 22*60, "work: essay", "work")])
db.run("UPDATE days SET plan_locked=1 WHERE date=?", (today.isoformat(),))
eng.build_checkpoints(today, cfg.now())

u = FakeUpdate("/offset x")
run(bot.cmd_offset(u, None))
check("a one-word reason is refused", "what happened" in u.message.last.lower(), True)

u = FakeUpdate("/offset stayed back for a department meeting after class")
run(bot.cmd_offset(u, None))
check("asks for photo proof", "photo of where you are" in u.message.last.lower(), True)
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
check("names who holds the switch", "Guardian: registered" in u.message.last, True)
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
goalup(db, bot, cfg)
u = FakeUpdate("/plan\n16:00-19:00 work: linear algebra pset\n19:00-22:00 work: essay")
run(bot.cmd_plan(u, None))
msg = u.message.last
check("accepted", "locked in" in msg.lower(), True)
check("says the passed slots are void", "don't count" in msg, True)
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

print("\n=== /setguardian registers them, before any commitment ===")
db.set_lock(commitment_end=None)          # you can only do this before it starts
u2 = FakeUpdate(f"/setguardian {DAD}")
run(bot.cmd_setguardian(u2, None))
check("registered", lk.guardian_id(), DAD)
check("confirmed to user", any("Guardian registered" in r for r in u2.message.replies), True)
dad_msgs = bot.app.bot.to(DAD)
check("he is told what to send, and you are not", any("/commit 3" in t for t in dad_msgs), True)
check("you are only told he was asked",
      any("asked to send /commit" in r for r in u2.message.replies), True)

print("\n=== you cannot make yourself the guardian ===")
cfg, db, led, eng, lk, bot, val = make()
bot.cfg.raw["telegram"]["guardian_chat_id"] = 0
db.set_lock(commitment_end=None)
u3 = FakeUpdate(f"/setguardian {USER}")
run(bot.cmd_setguardian(u3, None))
check("refused", "has to be someone else" in u3.message.last, True)
check("nobody registered", lk.guardian_id(), 0)

print("\n=== once the commitment starts, the guardian can't be swapped ===")
lk.register_guardian(DAD)
lk.start_commitment(3, cfg.today())
u4 = FakeUpdate("/setguardian 12345")
run(bot.cmd_setguardian(u4, None))
check("swap refused mid-commitment", "halfway through" in u4.message.last, True)
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
check("told it needed no password", "no password" in g[0].lower(), True)
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

print("\n=== /testsiren fires the alarm outside the window ===")
cfg, db, led, eng, lk, bot, val = make()
noon = datetime.combine(cfg.today(), datetime.min.time(), tzinfo=cfg.tz).replace(hour=12)
cfg.now = lambda: noon
check("nothing happening at noon", eng.agent_state(noon).phone_alarm, False)
u = FakeUpdate("/testsiren 120")
run(bot.cmd_testsiren(u, None))
st = eng.agent_state(noon)
check("siren armed", st.phone_alarm, True)
check("at the configured volume, not louder", st.alarm_volume_pct, 85)
check("agent sees it as active", st.active, True)
check("headline says it is a test", "TEST" in st.headline, True)
check("instructions mention silent mode", "silent" in u.message.last, True)

later = noon + timedelta(seconds=121)
cfg.now = lambda: later
check("expires on its own", eng.agent_state(later).phone_alarm, False)

cfg.now = lambda: noon
run(bot.cmd_testsiren(FakeUpdate("/testsiren 120"), None))
run(bot.cmd_stopsiren(FakeUpdate("/stopsiren"), None))
check("/stopsiren ends it", eng.agent_state(noon).phone_alarm, False)

print("\n=== /more: extend the task you are actually doing ===")
cfg, db, led, eng, lk, bot, val = make()
today = cfg.today()
noon = datetime.combine(today, datetime.min.time(), tzinfo=cfg.tz).replace(hour=17)
cfg.now = lambda: noon
db.ensure_day(today)
db.replace_blocks(today, [(16*60, 18*60, "work: hackathon", "work"),
                          (18*60, 20*60, "work: essay", "work"),
                          (20*60, 22*60, "relax", "relax")])
db.run("UPDATE days SET plan_locked=1 WHERE date=?", (today.isoformat(),))
eng.build_checkpoints(today, datetime.combine(today, datetime.min.time(),
                                              tzinfo=cfg.tz).replace(hour=15, minute=59))

u = FakeUpdate("/more 30")
run(bot.cmd_more(u, None))
check("refused before you've proven you're on it", "photo of" in u.message.last.lower(), True)
check("nothing moved", [b["end_min"] for b in db.blocks(today)], [18*60, 20*60, 22*60])

# prove it
cp = eng.current_checkpoint(today, noon) or eng.checkpoints(today)[0]
db.run("UPDATE checkpoints SET state='satisfied' WHERE id=?", (cp["id"],))

before_end = eng.window(today)[1]
u = FakeUpdate("/more 30")
run(bot.cmd_more(u, None))
blocks = db.blocks(today)
ends = [b["end_min"] for b in blocks]
check("current block gained 30 min", ends[0], 18*60 + 30)
check("the day does NOT get longer", eng.window(today)[1], before_end)
check("bedtime is not pushed back", ends[-1], 22*60)
# the next block pays for it by losing the time, not by moving
check("next block starts later and ends the same", (blocks[1]["start_min"], blocks[1]["end_min"]),
      (18*60 + 30, 20*60))
check("so it is 30 min shorter",
      (blocks[1]["end_min"] - blocks[1]["start_min"]), 90)
check("says where it now ends", "18:30" in u.message.last, True)
check("says what paid for it", "Paid for by" in u.message.last, True)
check("names the block that lost time", "essay" in u.message.last, True)

u = FakeUpdate("/more 90")
run(bot.cmd_more(u, None))
check("daily cap enforced", "limit is 90" in u.message.last, True)
check("nothing moved past the cap", [b["end_min"] for b in db.blocks(today)][0], 18*60 + 30)

evening = datetime.combine(today, datetime.min.time(), tzinfo=cfg.tz).replace(hour=23)
cfg.now = lambda: evening
u = FakeUpdate("/more 30")
run(bot.cmd_more(u, None))
check("refused outside the window", "not in the window" in u.message.last.lower(), True)




print("\n=== goals gate the plan: timetable without intent is refused ===")
cfg, db, led, eng, lk, bot, val = make()
target = bot.target_plan_date(cfg.now())
u = FakeUpdate("/plan\n" + GOOD)
run(bot.cmd_plan(u, None))
check("plan refused with no goals", "goals first" in u.message.last.lower(), True)
check("nothing stored", len(db.blocks(target)), 0)
check("now waiting for goals", bot.pending[USER]["action"], "goals")

u2 = FakeUpdate("finish pset ch3\nessay draft\ngym")
run(bot.on_text(u2, None))
check("goals accepted", len(db.goals(target)), 3)
check("pending cleared", USER in bot.pending, False)
check("told to send the plan next", "/plan" in u2.message.last, True)

u3 = FakeUpdate("/plan\n" + GOOD)
run(bot.cmd_plan(u3, None))
check("plan now accepted", "locked in" in u3.message.last.lower(), True)

print("\n=== too few and too many are both refused ===")
cfg, db, led, eng, lk, bot, val = make()
t = bot.target_plan_date(cfg.now())
u = FakeUpdate("/goals\nonly one thing")
run(bot.cmd_goals(u, None))
check("one goal refused", "at least" in u.message.last, True)
check("nothing stored", len(db.goals(t)), 0)

u = FakeUpdate("/goals\na\nb\nc\nd\ne\nf")
run(bot.cmd_goals(u, None))
check("six goals refused", "wish list" in u.message.last, True)
check("still nothing stored", len(db.goals(t)), 0)

print("\n=== /done ticks one off and the report carries it ===")
cfg, db, led, eng, lk, bot, val = make()
today = cfg.today()
db.ensure_day(today); db.set_goals(today, GOALS)
u = FakeUpdate("/done 2")
run(bot.cmd_done(u, None))
check("marked done", [g["done"] for g in db.goals(today)], [0, 1, 0])
check("shows the tally", "1/3" in u.message.last, True)

u = FakeUpdate("/done 9")
run(bot.cmd_done(u, None))
check("out-of-range is refused", "goal with that number" in u.message.last.lower(), True)

rep = bot._report_text(today)
check("report names the goals", "first draft of the essay" in rep, True)
check("report carries the tally", "Goals 1/3" in rep, True)

print("\n=== rewriting goals clears stale ticks ===")
db.set_goals(today, ["a different plan entirely", "second thing", "third thing"])
check("ticks reset with the goals", [g["done"] for g in db.goals(today)], [0, 0, 0])

print("\n=== a day with no goals says so in the report ===")
cfg, db, led, eng, lk, bot, val = make()
check("report is explicit about it", "No goals were set" in bot._report_text(cfg.today()), True)


print("\n=== /latenight is a weekly budget, not an excuse on tap ===")
# This breaks a rule the README states in bold. It is broken narrowly on
# purpose: once every seven days, needing a photo, and the guardian is told.
cfg, db, led, eng, lk, bot, val = make()
bot.cfg.raw["telegram"]["guardian_chat_id"] = GUARDIAN
today = cfg.today()

u = FakeUpdate("/latenight")
run(bot.cmd_latenight(u, None))
check("a bare command is refused", "what for?" in u.message.last.lower(), True)

u = FakeUpdate("/latenight busy")
run(bot.cmd_latenight(u, None))
check("a two-word reason is refused too", "what for?" in u.message.last.lower(), True)

u = FakeUpdate("/latenight hackathon submission is due at midnight")
run(bot.cmd_latenight(u, None))
check("a real reason asks for the photo", "send the pic" in u.message.last.lower(), True)
check("and waits for it", bot.pending[USER]["action"], "latenight_photo")
_row = db.day(today)
check("nothing granted yet — a reason alone buys nothing",
      int(_row["bed_offset_minutes"] or 0) if _row else 0, 0)

# Granting it directly; the photo path is exercised by the vision stub elsewhere.
db.ensure_day(today)
db.run("UPDATE days SET bed_offset_minutes=120, bed_reason=? WHERE date=?",
       ("hackathon", today.isoformat()))
check("bedtime moved by two hours",
      int((eng.bed_due(today) - cfg.at(today, 22 * 60 + 20)).total_seconds() // 60), 120)

u = FakeUpdate("/latenight another one please tonight")
run(bot.cmd_latenight(u, None))
check("twice in one night is refused", "already used it tonight" in u.message.last.lower(), True)

print("\n=== and it stays used for the whole week ===")
cfg, db, led, eng, lk, bot, val = make()
today = cfg.today()
db.ensure_day(today - timedelta(days=3))
db.run("UPDATE days SET bed_offset_minutes=120 WHERE date=?",
       ((today - timedelta(days=3)).isoformat(),))
u = FakeUpdate("/latenight friends are still out and about")
run(bot.cmd_latenight(u, None))
check("3 days later: refused", "once every 7 days" in u.message.last, True)
check("...and told when it comes back", "next one on" in u.message.last, True)

cfg, db, led, eng, lk, bot, val = make()
today = cfg.today()
db.ensure_day(today - timedelta(days=8))
db.run("UPDATE days SET bed_offset_minutes=120 WHERE date=?",
       ((today - timedelta(days=8)).isoformat(),))
u = FakeUpdate("/latenight friends are still out and about")
run(bot.cmd_latenight(u, None))
check("8 days later: allowed again", bot.pending[USER]["action"], "latenight_photo")

print("\n=== a weekend plan is validated against weekend hours ===")
# Validating a Saturday plan against the weekday window would accept one that
# leaves the whole weekend morning unblocked -- inside an enforced window, so
# never checked, and silent about it.
cfg, db, led, eng, lk, bot, val = make()
SATB = date(2026, 9, 12)
cfg.weekend_start_min, cfg.weekend_end_min = 10 * 60, 22 * 60
db.ensure_day(SATB); db.set_goals(SATB, GOALS)

u = FakeUpdate("16:00-22:00 work: essay")
run(bot._accept_plan(u, SATB, u.message.text))
check("a weekday-shaped plan is refused on a Saturday", u.message.last.startswith("❌"), True)
check("and it names the morning left uncovered", "10:00-16:00" in u.message.last, True)

u2 = FakeUpdate("10:00-22:00 work: essay")
run(bot._accept_plan(u2, SATB, u2.message.text))
check("covering 10:00-22:00 is accepted", "locked in" in u2.message.last.lower(), True)

print("\n=== replan closes the obligation it just made impossible ===")
# Replan truncates the block you were in, so an unanswered checkpoint inside it
# was left demanding proof of a task the plan no longer says you are doing --
# and the block is over, so nothing could satisfy it. The siren then ran on an
# obligation whose only exit was a faked photo.
cfg, db, led, eng, lk, bot, val = make()
today = cfg.today()
db.ensure_day(today); db.set_goals(today, GOALS)
db.replace_blocks(today, [(16 * 60, 19 * 60, "work: linear algebra", "work"),
                          (19 * 60, 22 * 60, "work: essay", "work")])
db.run("UPDATE days SET plan_locked=1 WHERE date=?", (today.isoformat(),))
eng.build_checkpoints(today, cfg.at(today, 15 * 60 + 59))
old = db.one("SELECT id FROM checkpoints WHERE date=? AND due_at LIKE ? LIMIT 1",
             (today.isoformat(), f"{today.isoformat()}T16:00%"))
db.run("UPDATE checkpoints SET state='overdue' WHERE id=?", (old["id"],))

# replan at 17:06, which truncates the 16:00-19:00 block to 16:00-17:06
cfg.now = lambda: cfg.at(today, 17 * 60 + 6)
u = FakeUpdate("17:06-19:00 work: uni research\n19:00-22:00 work: essay")
run(bot._accept_replan(u, u.message.text))
check("replan accepted", "rest of today" in u.message.last, True)

after = db.one("SELECT state FROM checkpoints WHERE id=?", (old["id"],))
check("the unanswerable one is closed as MISSED", after["state"], "missed")
check("not deleted — the miss stays on the record",
      db.one("SELECT COUNT(*) n FROM checkpoints WHERE id=?", (old["id"],))["n"], 1)
check("and it says so", "counts as missed" in u.message.last, True)
check("debt is clear, so the siren stops demanding the impossible",
      eng.debt_seconds(today, cfg.now()), 0)

print("\n=== the overdue message says when the siren starts, not that it has ===")
# It used to announce "Escalation has started" the moment the grace ran out,
# while phone_alarm_after_min held the siren back for another five minutes. The
# silence then read as a broken system rather than a deliberate wait.
from server.engine import Action as _A
cfg, db, led, eng, lk, bot, val = make()
now_m = cfg.now()

bot.cfg.raw["escalation"]["phone_alarm_after_min"] = 5
run(bot._dispatch(_A("mark_missed", {"label": "work: physics", "count": 1, "checkpoint_id": 1}), now_m))
msg = bot.app.bot.to(USER)[-1]
check("names the wait instead of claiming it started", "starts in 5 min" in msg, True)
check("does not claim the siren is already on", "siren's on now" in msg, False)

bot.cfg.raw["escalation"]["phone_alarm_after_min"] = 0
run(bot._dispatch(_A("mark_missed", {"label": "work: physics", "count": 1, "checkpoint_id": 1}), now_m))
msg = bot.app.bot.to(USER)[-1]
check("with no wait, it truthfully says it has started", "siren's on now" in msg, True)
check("and promises no future minute", "starts in" in msg, False)

print("\n=== the poke: waking a phone that cannot wake itself ===")
# Android froze the phone agent with the screen off and still reported it as
# running -- two hours, not one request. A push notification is the one thing
# it must deliver, so the server pokes rather than waiting to be asked.
cfg, db, led, eng, lk, bot, val = make()


class _Behind:
    phone_alarm, debt_minutes, headline = True, 14, "Send a photo of: work: physics"


class _Clear:
    phone_alarm, debt_minutes, headline = False, 0, ""


deleted = []


async def _fake_delete(chat_id, message_id):
    deleted.append(message_id)


bot.app.bot.delete_message = _fake_delete
_msgs = []
_orig_send = bot.app.bot.send_message


async def _counting_send(chat_id, text, **kw):
    _msgs.append(text)
    await _orig_send(chat_id, text, **kw)
    return types.SimpleNamespace(message_id=100 + len(_msgs))


bot.app.bot.send_message = _counting_send
now0 = cfg.now()

bot.engine.agent_state = lambda n: _Behind()
run(bot._poke(now0))
check("pokes when behind", len(_msgs), 1)
check("says how far behind, so the notification is worth reading", "14 min behind" in _msgs[0], True)
check("names the task", "work: physics" in _msgs[0], True)
check("remembers what it sent", db.lock()["poke_message_id"], 101)

run(bot._poke(now0))
check("only once a minute, not once a tick", len(_msgs), 1)

run(bot._poke(now0 + timedelta(minutes=1)))
check("pokes again the next minute", len(_msgs), 2)
check("and deletes the previous one — the chat holds one, not sixty", deleted, [101])

bot.engine.agent_state = lambda n: _Clear()
run(bot._poke(now0 + timedelta(minutes=2)))
check("silent once nothing is owed", len(_msgs), 2)
check("and clears the last poke behind it", deleted, [101, 102])
check("nothing left pointing at a deleted message", db.lock()["poke_message_id"], None)

bot.cfg.raw["escalation"]["telegram_poke"] = False
bot.engine.agent_state = lambda n: _Behind()
run(bot._poke(now0 + timedelta(minutes=3)))
check("config can switch it off", len(_msgs), 2)
bot.cfg.raw["escalation"]["telegram_poke"] = True

print("\n=== buttons: tapping beats typing ===")
cfg, db, led, eng, lk, bot, val = make()
bot.cfg.raw["telegram"]["guardian_chat_id"] = GUARDIAN
today = cfg.today()
db.ensure_day(today); db.set_goals(today, GOALS)


def keys(markup):
    return [b.text for row in markup.keyboard for b in row]


u = FakeUpdate("/done")
run(bot.cmd_done(u, None))
check("bare /done lists the goals instead of scolding", "linear algebra" in u.message.last, True)

kb = bot._goal_buttons(today)
check("one button per open goal", len(kb.inline_keyboard), 3)
check("button carries the goal index", kb.inline_keyboard[0][0].callback_data, "done:1")

db.mark_goal(today, 1); db.mark_goal(today, 2); db.mark_goal(today, 3)
check("no buttons left once all are ticked", bot._goal_buttons(today), None)

check("user keyboard offers /goals", "/goals" in keys(bot._keyboard(FakeUpdate("/help"))), True)
gk = keys(bot._keyboard(FakeUpdate("/help", chat_id=GUARDIAN)))
check("guardian keyboard offers /report", "/report" in gk, True)
check("guardian is not offered /plan — it isn't his", "/plan" in gk, False)

print("\n=== /done on a day with no goals doesn't offer an empty keyboard ===")
cfg, db, led, eng, lk, bot, val = make()
u = FakeUpdate("/done")
run(bot.cmd_done(u, None))
check("says there is nothing to tick", "no goals for today" in u.message.last.lower(), True)
check("no keyboard built", bot._goal_buttons(cfg.today()), None)

sys.exit(report("BOT TESTS"))
