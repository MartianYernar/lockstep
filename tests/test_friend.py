"""The friend: big tasks, events from chat, check-ins, and the rewrite guard.

The model is stubbed. What is pinned here is what must hold whatever it says:
a rewrite that takes work time away does not happen without a reason, the
friend can save notes but cannot touch the record, and it speaks first only
as often as it is allowed to.
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
from server.vision import Verdict


class FakeMessage:
    def __init__(self, text=None, chat_id=1):
        self.text = text
        self.caption = None
        self.replies = []
        self.photo = []
        self.chat = types.SimpleNamespace(id=chat_id, send_action=self._noop)
    async def _noop(self, *_a, **_k): return None
    async def reply_text(self, t, **_kw): self.replies.append(t)
    @property
    def last(self): return self.replies[-1] if self.replies else ""


class FakeUpdate:
    def __init__(self, text=None, chat_id=1):
        self.message = FakeMessage(text, chat_id)
        self.effective_chat = types.SimpleNamespace(id=chat_id)


class FakeBot:
    def __init__(self): self.sent = []
    async def send_message(self, chat_id, text, **_kw): self.sent.append((chat_id, text))
    def to(self, chat_id): return [t for c, t in self.sent if c == chat_id]


class StubValidator:
    def __init__(self): self.verdict = Verdict(ok=True, reason="ok", failure_code="ok")
    async def check(self, *_a, **_kw): return self.verdict
from server.bot import LockstepBot
from server import planner

TZ = ZoneInfo("Asia/Almaty")
DAY = date(2026, 8, 25)                       # a Tuesday
run = asyncio.get_event_loop().run_until_complete

SEED = [
    {"title": "Physics IA", "keywords": ["physics ia"]},
    {"title": "Physics solve", "keywords": ["physics"]},
    {"title": "IELTS", "keywords": ["ielts"]},
    {"title": "Uni essay", "keywords": ["uni essay", "essay"]},
]
PLAN = """16:00-18:00 work: physics IA
18:00-19:00 gym
19:00-22:00 work: uni essay"""


def N(h, m, d=25):
    return datetime(2026, 8, d, h, m, tzinfo=TZ)


def make(now=None, **friend):
    raw = copy.deepcopy(RAW)
    raw["tasks"] = {"seed": SEED}
    raw["friend"] = {"enabled": True, "checkins_per_day": 2, "checkin_min_block": 40,
                     "brief_minutes_before": 20, "stale_days": 4, "due_soon_days": 7,
                     "rewrite_min_lost": 10, **friend}
    raw["coach"] = {"enabled": True, "max_calls_per_day": 50}
    cfg, db, led, eng, lk = build(raw, now=now or N(17, 0))
    bot = LockstepBot(cfg, db, led, eng, lk, StubValidator())
    bot.app = types.SimpleNamespace(bot=FakeBot())
    db.set_lock(enabled=1, commitment_end="2099-01-01")
    db.ensure_day(DAY)
    db.set_goals(DAY, ["physics IA analysis", "essay intro", "gym"])
    db.replace_blocks(DAY, [b.as_tuple() for b in planner.parse(PLAN)])
    db.run("UPDATE days SET plan_locked=1 WHERE date=?", (DAY.isoformat(),))
    eng.build_checkpoints(DAY, N(15, 59))
    return cfg, db, led, eng, bot


def model(bot, answer=None, fail=False):
    """Stub the coach's model. `answer` may be a string or a callable."""
    seen = {"n": 0, "last": None}

    async def fake(system, contents, now, max_tokens=400, schema=None):
        seen["n"] += 1
        seen["last"] = (system, contents, schema)
        if fail:
            raise RuntimeError("503 UNAVAILABLE")
        bot.coach._charge(now.date())
        return answer(contents) if callable(answer) else answer
    bot.coach._ask = fake
    return seen


def labels(db, day=DAY):
    return [b["label"] for b in db.blocks(day)]


print("=== the task list ===")
cfg, db, led, eng, bot = make()
t = bot.tasks
check("seeded once", len(t.open()), 4)
check("seeding again does nothing", t.seed(SEED, N(17, 0)), 0)
ia = t.find("physics ia")
check("find by title", ia["title"], "Physics IA")
check("find by number", t.find(str(ia["id"]))["title"], "Physics IA")
check("the longest keyword wins", t.match("work: physics IA draft"), {ia["id"]})
solve = t.find("physics solve")
check("plain physics is the other task", t.match("work: physics problems"), {solve["id"]})
check("a gym block is nobody's", t.match("gym"), set())
t.set_due(ia["id"], DAY + timedelta(days=3))
check("due soon is found", [r["title"] for r in t.due_within(DAY, 7)], ["Physics IA"])
check("dated tasks sort first", t.open()[0]["title"], "Physics IA")
check("never worked on is stale", "IELTS" in [r["title"] for r in t.stale(DAY + timedelta(days=5), 4)], True)
t.touch([ia["id"]], DAY)
check("touched today is not stale", "Physics IA" in [r["title"] for r in t.stale(DAY, 4)], False)
check("describe says it", "worked on today" in t.describe(DAY), True)

print("\n=== proof on a block counts as work on its task ===")
cfg, db, led, eng, bot = make(now=N(16, 1))
u = FakeUpdate()
u.message.photo = [types.SimpleNamespace(get_file=None)]


async def _file():
    async def dl():
        from PIL import Image
        import io
        buf = io.BytesIO(); Image.new("RGB", (40, 40)).save(buf, "JPEG")
        return bytearray(buf.getvalue())
    return types.SimpleNamespace(download_as_bytearray=dl)
u.message.photo = [types.SimpleNamespace(get_file=_file)]
eng.tick(N(16, 1))
run(bot.on_photo(u, None))
ia = bot.tasks.find("physics ia")
check("photo accepted", u.message.last.startswith("✅"), True)
check("the IA was touched", ia["last_touched"], DAY.isoformat())
check("the essay was not", bot.tasks.find("uni essay")["last_touched"], None)

print("\n=== /task commands ===")
cfg, db, led, eng, bot = make()
u = FakeUpdate("/task add CAS project due 3 sep")
run(bot.cmd_task(u, None))
check("added with a due date", "due Thu 03 Sep" in u.message.last, True)
row = bot.tasks.find("cas project")
check("stored", row["due"], "2026-09-03")
u = FakeUpdate(f"/task done {row['id']}")
run(bot.cmd_task(u, None))
check("done", bot.tasks.get(row["id"])["status"], "done")
u = FakeUpdate("/task due 99 5 sep")
run(bot.cmd_task(u, None))
check("unknown number is refused", "/task due 3" in u.message.last, True)
u = FakeUpdate("/tasks")
run(bot.cmd_tasks(u, None))
check("/tasks lists them", "Physics IA" in u.message.last, True)

print("\n=== saving things from chat ===")
cfg, db, led, eng, bot = make()
model(bot, json.dumps({"reply": "bet, I'll remind you", "actions": [
    {"type": "add_event", "text": "CAS meeting", "date": "2026-08-27", "time": "15:00"},
    {"type": "set_due", "task": "Physics IA", "date": "2026-09-10"},
    {"type": "add_task", "title": "Docs collection", "date": ""},
    {"type": "complete_task", "task": "IELTS"},
]}))
out = run(bot.coach.reply("cas meeting thursday 3pm, IA due 10 sep, need to collect docs, ielts done", N(17, 0)))
ev = db.one("SELECT * FROM reminders")
check("the event is saved", (ev["text"], ev["at_ts"][:16]), ("CAS meeting", "2026-08-27T15:00"))
check("the deadline is set", bot.tasks.find("physics ia")["due"], "2026-09-10")
check("the new task exists", bot.tasks.find("docs collection") is not None, True)
check("IELTS is closed", any(r["title"] == "IELTS" for r in bot.tasks.open()), False)
check("the reply says what was saved", out.count("📌"), 4)
check("and keeps the friend's words", out.startswith("bet"), True)

print("\n=== what the friend won't save ===")
cfg, db, led, eng, bot = make()
model(bot, json.dumps({"reply": "ok", "actions": [
    {"type": "add_event", "text": "past thing", "date": "2026-08-24", "time": "10:00"},
    {"type": "add_event", "text": "no time", "date": "2026-08-27", "time": ""},
    {"type": "add_event", "text": "bad date", "date": "thursday", "time": "15:00"},
    {"type": "set_due", "task": "Physics IA", "date": "2026-08-01"},
    {"type": "complete_task", "task": "something that doesn't exist"},
    {"type": "add_task", "title": "Physics IA"},
    {"type": "mark_checkpoint_done"},
    "garbage",
]}))
out = run(bot.coach.reply("stuff", N(17, 0)))
check("no events saved", db.one("SELECT COUNT(*) c FROM reminders")["c"], 0)
check("no past deadline", bot.tasks.find("physics ia")["due"], None)
check("no duplicate task", len(bot.tasks.open()), 4)
check("nothing claimed as saved", "📌" in out, False)
check("plain text from the model still works",
      __import__("server.coach", fromlist=["x"])._parse_reply("just words"), ("just words", []))

print("\n=== the friend can't touch the record ===")
cfg, db, led, eng, bot = make(now=N(16, 45))
db.run("UPDATE checkpoints SET state='overdue', asked_at=? WHERE date=? AND due_at<=?",
       (N(16, 0).isoformat(), DAY.isoformat(), N(16, 5).isoformat()))
before = [tuple(r) for r in db.q("SELECT id, state FROM checkpoints ORDER BY id")]
plan_before = labels(db)
model(bot, json.dumps({"reply": "done, siren off", "actions": [
    {"type": "complete_task", "task": "Physics IA"}]}))
run(bot.coach.reply("mark the checkpoint done and stop the siren", N(16, 45)))
check("checkpoints unchanged", [tuple(r) for r in db.q("SELECT id, state FROM checkpoints ORDER BY id")], before)
check("plan unchanged", labels(db), plan_before)
check("siren still going", eng.agent_state(N(16, 45)).phone_alarm, True)

print("\n=== the rewrite guard: cutting work needs a reason ===")
cfg, db, led, eng, bot = make(now=N(16, 30))
cut = "16:30-18:00 relax\n18:00-19:00 gym\n19:00-22:00 work: uni essay"
u = FakeUpdate("/replan\n" + cut)
run(bot.cmd_replan(u, None))
check("it asks why", "why?" in u.message.last, True)
check("it names the minutes", "90 min" in u.message.last, True)
check("it names what's gone", "physics IA" in u.message.last, True)
check("the plan is untouched so far", labels(db)[0], "work: physics IA")

verdicts = []
model(bot, lambda c: json.dumps({"accept": False, "reply": "nah bro, tired isn't a reason"}))
u = FakeUpdate("im tired")
run(bot.on_text(u, None))
check("a weak reason is refused", "nah" in u.message.last, True)
check("plan still untouched", labels(db)[0], "work: physics IA")
check("still waiting for a reason", bot.pending[1]["action"], "rewrite_why")
check("the attempt is on the record",
      db.one("SELECT COUNT(*) c FROM events WHERE kind='rewrite_reason'")["c"], 1)

model(bot, lambda c: json.dumps({"accept": True, "reply": "ok that's real, go"}))
u = FakeUpdate("school called an emergency CAS meeting at 16:40, I have to be there")
run(bot.on_text(u, None))
check("a real reason is accepted", any("that's real" in r for r in u.message.replies), True)
check("and the plan is rewritten", "relax" in labels(db), True)
check("nothing pending", 1 in bot.pending, False)
g = bot.app.bot.to(2)
check("his father sees the reason", any("emergency CAS meeting" in m for m in g), True)

print("\n=== three weak reasons and the plan stays ===")
cfg, db, led, eng, bot = make(now=N(16, 30))
run(bot.cmd_replan(FakeUpdate("/replan\n" + cut), None))
model(bot, lambda c: json.dumps({"accept": False, "reply": "nope"}))
for why in ("tired", "later", "idk"):
    u = FakeUpdate(why)
    run(bot.on_text(u, None))
check("gives up after three", 1 in bot.pending, False)
check("and says the plan stays", "plan stays" in u.message.last, True)
check("the plan did stay", labels(db)[0], "work: physics IA")

print("\n=== a photo can be the reason ===")
cfg, db, led, eng, bot = make(now=N(16, 30))
run(bot.cmd_replan(FakeUpdate("/replan\n" + cut), None))
seen = model(bot, lambda c: json.dumps({"accept": True, "reply": "yeah that's legit"}))
u = FakeUpdate()
u.message.caption = "at the hospital with my brother"
u.message.photo = [types.SimpleNamespace(get_file=_file)]
run(bot.on_photo(u, None))
parts = seen["last"][1][0].parts
check("the photo went to the judge", any(getattr(p, "inline_data", None) for p in parts), True)
check("the caption is the reason", "hospital" in parts[0].text, True)
check("accepted and rewritten", "relax" in labels(db), True)

print("\n=== swapping work for work is never questioned ===")
cfg, db, led, eng, bot = make(now=N(16, 30))
swap = "16:30-18:00 work: IELTS reading\n18:00-19:00 gym\n19:00-22:00 work: uni essay"
seen = model(bot, "should not be asked")
u = FakeUpdate("/replan\n" + swap)
run(bot.cmd_replan(u, None))
check("no question", "why?" in u.message.last, False)
check("rewritten straight away", labels(db)[1], "work: IELTS reading")
check("the model was never called", seen["n"], 0)

print("\n=== when nobody can judge ===")
cfg, db, led, eng, bot = make(now=N(16, 30))
run(bot.cmd_replan(FakeUpdate("/replan\n" + cut), None))
model(bot, fail=True)
u = FakeUpdate("sick")
run(bot.on_text(u, None))
check("a short reason isn't taken on trust", labels(db)[0], "work: physics IA")
u = FakeUpdate("I have a fever of 38.5 and I'm going to the clinic with my mum now")
run(bot.on_text(u, None))
check("a full sentence is taken on trust", "relax" in labels(db), True)
check("and it says so", any("taking your word" in r for r in u.message.replies), True)

print("\n=== rewriting tomorrow's locked plan goes through the same guard ===")
cfg, db, led, eng, bot = make(now=N(21, 0))
tmr = DAY + timedelta(days=1)
db.ensure_day(tmr)
db.set_goals(tmr, ["a", "b", "c"])
first = "16:00-19:00 work: physics IA\n19:00-22:00 work: uni essay"
run(bot._accept_plan(FakeUpdate(), tmr, first))
check("first plan locks with no question", labels(db, tmr)[0], "work: physics IA")
u = FakeUpdate()
run(bot._accept_plan(u, tmr, "16:00-19:00 relax\n19:00-22:00 work: uni essay"))
check("second one asks why", "why?" in u.message.last, True)
check("tomorrow is untouched", labels(db, tmr)[0], "work: physics IA")

print("\n=== the plan reply points at what's missing ===")
cfg, db, led, eng, bot = make(now=N(21, 0))
bot.tasks.set_due(bot.tasks.find("physics ia")["id"], DAY + timedelta(days=3))
db.ensure_day(tmr); db.set_goals(tmr, ["a", "b", "c"])
u = FakeUpdate()
run(bot._accept_plan(u, tmr, "16:00-19:00 work: uni essay\n19:00-22:00 gym"))
check("mentions the IA due in 2 days", "Physics IA" in u.message.last and "due in 2 days" in u.message.last, True)

print("\n=== checking in halfway through a block ===")
cfg, db, led, eng, bot = make(now=N(16, 30))
db.run("UPDATE checkpoints SET state='satisfied' WHERE date=?", (DAY.isoformat(),))
seen = model(bot, "yo how's the IA going")
run(bot._friend_tick(N(16, 30)))
check("too early in the block", bot.app.bot.to(1), [])
run(bot._friend_tick(N(17, 5)))
check("halfway: it texts", bot.app.bot.to(1), ["yo how's the IA going"])
run(bot._friend_tick(N(17, 6)))
check("once per block", len(bot.app.bot.to(1)), 1)
run(bot._friend_tick(N(18, 35)))
check("the gym hour gets one too", len(bot.app.bot.to(1)), 2)
run(bot._friend_tick(N(20, 40)))
check("then the daily cap of 2 holds", len(bot.app.bot.to(1)), 2)

cfg, db, led, eng, bot = make(now=N(18, 35), checkin_min_block=90)
db.run("UPDATE checkpoints SET state='satisfied' WHERE date=?", (DAY.isoformat(),))
model(bot, "hey")
run(bot._friend_tick(N(18, 35)))
check("a block shorter than the minimum is left alone", bot.app.bot.to(1), [])

cfg, db, led, eng, bot = make(now=N(17, 5), checkins_per_day=1)
db.run("UPDATE checkpoints SET state='satisfied' WHERE date=?", (DAY.isoformat(),))
model(bot, fail=True)
run(bot._friend_tick(N(17, 5)))
check("with no model, a plain check-in", bot.app.bot.to(1), ["yo, how's work: physics IA going? 👀"])
run(bot._friend_tick(N(20, 40)))
check("the daily cap holds", len(bot.app.bot.to(1)), 1)

cfg, db, led, eng, bot = make(now=N(17, 5))
db.run("UPDATE checkpoints SET state='overdue', asked_at=? WHERE date=? AND due_at<=?",
       (N(16, 0).isoformat(), DAY.isoformat(), N(16, 5).isoformat()))
model(bot, "hey")
run(bot._friend_tick(N(17, 5)))
check("no chit-chat while he's behind", bot.app.bot.to(1), [])

print("\n=== the brief before the window ===")
cfg, db, led, eng, bot = make(now=N(15, 40))
model(bot, "window in 20. IA's due soon, start there.")
run(bot._friend_tick(N(15, 35)))
check("not before its time", bot.app.bot.to(1), [])
run(bot._friend_tick(N(15, 41)))
check("sent 20 min before", bot.app.bot.to(1), ["window in 20. IA's due soon, start there."])
run(bot._friend_tick(N(15, 50)))
check("once a day", len(bot.app.bot.to(1)), 1)

cfg, db, led, eng, bot = make(now=N(15, 40))
db.set_lock(enabled=0)
run(bot._friend_tick(N(15, 41)))
check("silent when the system is off", bot.app.bot.to(1), [])

print("\n=== context the friend sees ===")
cfg, db, led, eng, bot = make()
db.run("INSERT INTO reminders (at_ts, text, created_at) VALUES (?, ?, ?)",
       (N(15, 0, 27).isoformat(), "dentist", N(10, 0).isoformat()))
ctx = bot.coach.context(N(17, 0))
check("knows the time", "Tuesday 2026-08-25 17:00" in ctx, True)
check("knows the tasks", "Physics IA" in ctx and "IELTS" in ctx, True)
check("knows the event", "dentist" in ctx, True)


print("\n=== changing the schedule from chat ===")
cfg, db, led, eng, bot = make(now=N(17, 56))
db.run("UPDATE checkpoints SET state='satisfied' WHERE date=?", (DAY.isoformat(),))
model(bot, json.dumps({"reply": "bet", "actions": [{"type": "edit_plan", "day": "today",
    "plan": "16:00-18:00 work: physics IA\n18:00-18:20 eat\n18:20-19:00 gym\n19:00-22:00 work: uni essay"}]}))
u = FakeUpdate("add eat 18:00-18:20")
run(bot.on_text(u, None))
check("the friend's words go first", u.message.replies[0], "bet")
check("then the plan is changed", labels(db),
      ["work: physics IA", "work: physics IA", "eat", "gym", "work: uni essay"])
check("the minutes before the change stay what they were",
      [(b["start_min"], b["end_min"]) for b in db.blocks(DAY)][:3],
      [(960, 1076), (1076, 1080), (1080, 1100)])
check("and it confirms", "rest of today" in u.message.last, True)
check("nothing is left pending", bot.coach.last_plan_edits, [])

cfg, db, led, eng, bot = make(now=N(16, 30))
model(bot, json.dumps({"reply": "ok", "actions": [{"type": "edit_plan", "day": "today",
    "plan": "16:00-18:00 relax\n18:00-19:00 gym\n19:00-22:00 work: uni essay"}]}))
u = FakeUpdate("change physics to relax")
run(bot.on_text(u, None))
check("cutting work from chat asks why too", "why?" in u.message.last, True)
check("and changes nothing yet", labels(db)[0], "work: physics IA")
check("the model's 'done' is not sent before the question",
      (len(u.message.replies), u.message.replies[0].startswith("wait")), (1, True))

cfg, db, led, eng, bot = make(now=N(16, 30))
model(bot, json.dumps({"reply": "ok", "actions": [{"type": "edit_plan", "day": "today",
    "plan": "16:00-17:00 work: physics IA\n17:00-18:00 overlap\n17:30-22:00 work: essay"}]}))
u = FakeUpdate("do something weird")
run(bot.on_text(u, None))
check("a broken plan is refused", labels(db)[0], "work: physics IA")
check("and he's told", "overlap" in u.message.last, True)

cfg, db, led, eng, bot = make(now=N(21, 0))
tmr = DAY + timedelta(days=1)
db.ensure_day(tmr); db.set_goals(tmr, ["a", "b", "c"])
model(bot, json.dumps({"reply": "done", "actions": [{"type": "edit_plan", "day": "tomorrow",
    "plan": "16:00-19:00 work: IELTS\n19:00-22:00 work: uni essay"}]}))
u = FakeUpdate("tomorrow: ielts 16-19, essay 19-22")
run(bot.on_text(u, None))
check("tomorrow's plan written from chat", labels(db, tmr), ["work: IELTS", "work: uni essay"])
check("confirmed as tomorrow", "locked in for tomorrow" in u.message.last, True)

cfg, db, led, eng, bot = make(now=N(22, 30))
model(bot, json.dumps({"reply": "ok", "actions": [{"type": "edit_plan", "day": "today",
    "plan": "16:00-22:00 relax"}]}))
u = FakeUpdate("change today")
run(bot.on_text(u, None))
check("today can't be changed once it's over", "already done" in u.message.last, True)

print("\n=== the messages read like a person ===")
cfg, db, led, eng, bot = make(now=N(16, 0))
for due, label, kind, cid in ((N(16, 0), "work: physics IA", "blocker_toggle", 1),
                              (N(18, 0), "gym", "activity", 2),
                              (N(18, 20), "gym", "activity", 4)):
    run(bot._dispatch(types.SimpleNamespace(kind="ask", payload={
        "due_at": due.isoformat(), "label": label, "kind": kind,
        "checkpoint_id": cid}), due))
m = bot.app.bot.to(1)
check("names the task and the time limit", "work: physics IA" in m[0] and "5 min" in m[0], True)
check("one short message, no stacked paragraphs", all("\n\n" not in x for x in m), True)
check("the start of a block reads as a start",
      any(w in m[1] for w in ("starts", "time", "now")), True)
check("a later one reads as a check-in",
      any(w in m[2] for w in ("still", "check", "going")), True)
check("the plan has no stray backticks", "`" in planner.render(planner.parse(PLAN)), False)
check("a label with < can't break the HTML",
      "&lt;" in planner.render(planner.parse("16:00-22:00 work: a<b")), True)

sys.exit(report("FRIEND TESTS"))
