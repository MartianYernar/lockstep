"""Miss the target, owe an SAT drill. His design, 18 Oct 2026.

    "it is 10 push ups if i miss a task... though only problem is that how to
     verify me doing push ups? ohhh or there should be other punishment like
     ... doing 10 questions of sat, one minute each, and i need to get do over
     and over until i get at least 8/10, each time new questions... until i
     dont send that 8/10 siren should keep firing as punnishment as well"

The drill app reports the score to the server itself, so the things that would
otherwise make this theatre -- an old screenshot, a slower drill, the same
good result sent twice -- are all closed here.
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
from server.penalty import Penalties
from server import planner

TZ = ZoneInfo("Asia/Almaty")
DAY = date(2026, 8, 25)
run = asyncio.get_event_loop().run_until_complete


def N(h, m):
    return datetime(2026, 8, 25, h, m, tzinfo=TZ)


def make(now=N(18, 0), **pen):
    raw = copy.deepcopy(RAW)
    raw["coach"] = {"enabled": False}
    raw["standup"] = {"enabled": False}
    raw["micro"] = {"enabled": False}
    raw["deliver"] = {"enabled": True, "min_block_min": 45}
    raw["schedule"] = dict(raw["schedule"], mode="block_start", grace_min=3)
    raw["proof"] = {"within_block_min": 20}
    raw["penalty"] = {"enabled": True, "questions": 10, "seconds_each": 60,
                      "pass_score": 8, "max_per_day": 3, "siren": True,
                      "url": "https://sat-speedy-training.vercel.app", **pen}
    cfg, db, led, eng, lk = build(raw, now=now)
    bot = LockstepBot(cfg, db, led, eng, lk, StubValidator())
    bot.app = types.SimpleNamespace(bot=FakeBot())
    db.set_lock(enabled=1, commitment_end="2099-01-01")
    db.ensure_day(DAY)
    db.set_goals(DAY, ["a", "b", "c"])
    db.replace_blocks(DAY, [b.as_tuple() for b in planner.parse(
        "16:00-17:30 work: uni applications\n17:30-19:00 work: physics\n"
        "19:00-22:00 work: essay")])
    db.run("UPDATE days SET plan_locked=1 WHERE date=?", (DAY.isoformat(),))
    return cfg, db, led, eng, bot


def sent(bot):
    return bot.app.bot.to(1)


print("=== what clears a drill and what doesn't ===")
cfg, db, led, eng, bot = make()
p = bot.penalty
check("8 of 10 clears it", p.judge(8, 10, 60)[0], True)
check("10 of 10 too", p.judge(10, 10, 55)[0], True)
check("7 of 10 does not", p.judge(7, 10, 60)[0], False)
check("and it says what was needed", "need 8" in p.judge(7, 10, 60)[1], True)
check("five questions is not the drill", p.judge(5, 5, 60)[0], False)
check("nor is five minutes a question", p.judge(9, 10, 300)[0], False)
check("a longer drill needs proportionally more", p.judge(8, 20, 60)[0], False)
check("16 of 20 clears it", p.judge(16, 20, 60)[0], True)

print("\n=== a missed target owes one ===")
cfg, db, led, eng, bot = make()
run(bot._owe_drill(N(18, 5), "target missed", "work: physics"))
check("it is owed", bot.penalty.owed(), 1)
check("he is told what and where", "8/10" in sent(bot)[-1]
      and "sat-speedy-training" in sent(bot)[-1], True)
check("and it is on the record", db.one(
    "SELECT COUNT(*) c FROM events WHERE kind='penalty_owed'")["c"], 1)

print("\n=== and the siren runs until it is paid ===")
cfg, db, led, eng, bot = make()
check("nothing owed, nothing sounding", eng.agent_state(N(18, 5)).active, False)
run(bot._owe_drill(N(18, 5), "target missed", "work: physics"))
st = eng.agent_state(N(18, 6))
check("now it is", (st.active, st.phone_alarm), (True, True))
check("and it says why", "SAT drill" in st.headline and "8/10" in st.headline, True)
check("sleep still wins: outside the window it is quiet",
      eng.agent_state(N(23, 30)).active
      and "drill" in eng.agent_state(N(23, 30)).headline.lower(), False)
check("but it has not gone away", bot.penalty.owed(), 1)

print("\n=== paying it ===")
cfg, db, led, eng, bot = make()
run(bot._owe_drill(N(18, 5), "target missed", "work: physics"))
row = bot.penalty.next_one()
ok, why = bot.penalty.judge(9, 10, 58)
bot.penalty.record("sess-1", 9, 10, 58, ok, N(18, 40), row["id"])
check("a pass clears it", bot.penalty.pay(row["id"], N(18, 40), {"result": why}), True)
check("nothing owed now", bot.penalty.owed(), 0)
check("the siren stops", eng.agent_state(N(18, 41)).active, False)
check("the same session cannot be spent twice", bot.penalty.seen_session("sess-1"), True)

print("\n=== through the endpoint his app actually calls ===")
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402
from server.api import build_app  # noqa: E402


async def endpoint_case():
    cfg2, db2, led2, eng2, bot2 = make()
    told = {}

    async def on_paid(result, left, now):
        told["result"], told["left"] = result, left
    cfg2.on_penalty_paid = on_paid
    await bot2._owe_drill(N(18, 5), "target missed", "work: physics")
    app = build_app(cfg2, eng2, led2)
    tok = cfg2.api.get("sat_token") or cfg2.api["token"]
    async with TestClient(TestServer(app)) as cl:
        r = await cl.post(f"/agent/sat?token={tok}",
                          json={"session": "s1", "score": 9, "total": 10, "seconds": 58})
        body = await r.json()
        check("a pass clears it over HTTP", (body["ok"], body["cleared"], body["owed"]),
              (True, True, 0))
        check("and the bot is told", told.get("result"), "9/10")

        r = await cl.post(f"/agent/sat?token={tok}",
                          json={"session": "s1", "score": 9, "total": 10, "seconds": 58})
        check("the same result cannot pay twice", (await r.json())["ok"], False)

        await bot2._owe_drill(N(18, 50), "target missed", "work: essay")
        r = await cl.post(f"/agent/sat?token={tok}",
                          json={"session": "s2", "score": 6, "total": 10, "seconds": 60})
        body = await r.json()
        check("a fail does not clear it", (body["ok"], body["owed"]), (False, 1))
        check("and says what was needed", "need 8" in body["reason"], True)

        r = await cl.post("/agent/sat?token=nope",
                          json={"session": "s3", "score": 9, "total": 10})
        check("a wrong token is refused", r.status, 401)

        r = await cl.post(f"/agent/sat?token={tok}",
                          json={"session": "s4", "score": 9, "total": 10, "seconds": 58})
        check("paying the last one clears the day", (await r.json())["owed"], 0)

        r = await cl.post(f"/agent/sat?token={tok}",
                          json={"session": "s5", "score": 10, "total": 10, "seconds": 50})
        body = await r.json()
        check("a drill with nothing owed is not wasted, just not a payment",
              (body["ok"], body["cleared"]), (True, False))

        r = await cl.get("/health")
        check("health counts what is owed", (await r.json())["drills_owed"], 0)

        r = await cl.options("/agent/sat")
        check("the browser gets its CORS answer",
              r.headers.get("Access-Control-Allow-Origin"), "*")

run(endpoint_case())

print("\n=== the token the browser holds can do nothing else ===")


async def scope_case():
    cfg2, db2, led2, eng2, bot2 = make()
    app = build_app(cfg2, eng2, led2)
    sat = cfg2.api.get("sat_token")
    async with TestClient(TestServer(app)) as cl:
        check("it cannot read his state", (await cl.get(f"/agent/state?token={sat}")).status,
              401)
        check("it cannot poll the siren", (await cl.get(f"/agent/gap?token={sat}")).status,
              401)
        check("it cannot claim his phone is alive",
              (await cl.post(f"/agent/ack?token={sat}", json={})).status, 401)

run(scope_case())

print("\n=== a bad evening does not become homework he can never finish ===")
cfg, db, led, eng, bot = make()
for i in range(5):
    run(bot._owe_drill(N(18, i), "target missed", f"block {i}"))
check("capped at three a day", bot.penalty.owed(), 3)

print("\n=== /drill ===")
cfg, db, led, eng, bot = make()
u = FakeUpdate("/drill")
run(bot.cmd_drill(u, None))
check("nothing owed says so", "nothing owed" in u.message.last, True)
run(bot._owe_drill(N(18, 5), "target missed", "work: physics"))
u = FakeUpdate("/drill")
run(bot.cmd_drill(u, None))
check("owed shows both ways to pay", "SAT questions" in u.message.last
      and "8/10" in u.message.last, True)
check("and the link", "sat-speedy-training" in u.message.last, True)

print("\n=== it is a cost on top, not an exchange rate ===")
cfg, db, led, eng, bot = make()
db.run("INSERT INTO checkpoints (date, due_at, block_id, kind, state) VALUES (?,?,?,?,?)",
       (DAY.isoformat(), N(17, 0).isoformat(), db.blocks(DAY)[0]["id"], "activity",
        "missed"))
run(bot._owe_drill(N(18, 5), "target missed", "work: uni applications"))
row = bot.penalty.next_one()
bot.penalty.pay(row["id"], N(18, 40), {"result": "9/10"})
check("paying the drill does not unmiss the photo", db.one(
    "SELECT COUNT(*) c FROM checkpoints WHERE date=? AND state='missed'",
    (DAY.isoformat(),))["c"], 1)

print("\n=== the simple version: a photo in the position ===")
# 19 Oct: "is there something even simplier... maybe just a photo bro for now
# without face that i am in push up position, and just trust me for now that i
# made those 10 push ups".
import io  # noqa: E402


class PhotoChecker(StubValidator):
    def __init__(self, in_position=True, face=False, reason="standing in a kitchen"):
        super().__init__()
        self.answer = {"in_position": in_position, "face_visible": face,
                       "confidence": 0.9, "reason": reason, "unverified": False}
        self.calls = 0

    async def pushup_photo(self, blob, now, mime="image/jpeg"):
        self.calls += 1
        return dict(self.answer)


async def _file():
    async def dl():
        from PIL import Image
        buf = io.BytesIO(); Image.new("RGB", (60, 60)).save(buf, "JPEG")
        return bytearray(buf.getvalue())
    return types.SimpleNamespace(download_as_bytearray=dl)


def photo_update():
    u = FakeUpdate()
    u.message.photo = [types.SimpleNamespace(get_file=_file)]
    return u


def with_photo(checker, **pen):
    # Mirrors the live config from 19 Oct: push-ups only, photo proof, 10 reps.
    pen.setdefault("sat", {"enabled": False})
    cfg2, db2, led2, eng2, bot2 = make(pushups={"enabled": True, "photo": True,
                                                "reps": 10}, **pen)
    bot2.validator = checker
    return cfg2, db2, led2, eng2, bot2


cfg, db, led, eng, bot = with_photo(PhotoChecker())
run(bot._owe_drill(N(18, 5), "target missed", "work: physics"))
check("he is told the simple way", "photo in the position" in sent(bot)[-1], True)
check("and how many", "10 push-ups" in sent(bot)[-1], True)
check("the SAT drill is not offered any more", "SAT" in sent(bot)[-1], False)

u = photo_update()
bot.cfg.now = lambda: N(18, 10)
run(bot.on_photo(u, None))
check("a photo in the position clears it", bot.penalty.owed(), 0)
check("he is told it is on trust", "on trust" in u.message.last, True)
check("the siren stops", eng.agent_state(N(18, 11)).active, False)
check("and the record says it was trusted, not verified", json.loads(db.one(
    "SELECT payload FROM events WHERE kind='penalty_paid' ORDER BY id DESC"
    )["payload"])["trusted"], True)

cfg, db, led, eng, bot = with_photo(PhotoChecker(in_position=False))
run(bot._owe_drill(N(18, 5), "target missed", "x"))
u = photo_update()
bot.cfg.now = lambda: N(18, 10)
run(bot.on_photo(u, None))
check("a photo of him standing around does not", bot.penalty.owed(), 1)
check("and says what it saw", "kitchen" in u.message.last, True)

cfg, db, led, eng, bot = with_photo(PhotoChecker(face=True))
run(bot._owe_drill(N(18, 5), "target missed", "x"))
u = photo_update()
bot.cfg.now = lambda: N(18, 10)
run(bot.on_photo(u, None))
check("his face in frame is refused", bot.penalty.owed(), 1)
check("with the fix, not a lecture", "camera lower" in u.message.last, True)

print("\n=== /pushups ===")
cfg, db, led, eng, bot = with_photo(PhotoChecker())
u = FakeUpdate("/pushups")
run(bot.cmd_pushups(u, None))
check("nothing owed says so", "nothing owed" in u.message.last, True)
run(bot._owe_drill(N(18, 5), "target missed", "x"))
u = FakeUpdate("/pushups")
run(bot.cmd_pushups(u, None))
check("owed tells him to go", "10 push-ups" in u.message.last, True)
check("and it waits for the photo", bot.pending[1]["action"], "pushup_photo")

print("\n=== a checker that is down leaves him able to pay ===")


class DeadPhoto(PhotoChecker):
    async def pushup_photo(self, blob, now, mime="image/jpeg"):
        return {"in_position": True, "face_visible": False, "confidence": 0.0,
                "unverified": True, "reason": "couldn't reach it"}


cfg, db, led, eng, bot = with_photo(DeadPhoto())
run(bot._owe_drill(N(18, 5), "target missed", "x"))
bot.cfg.now = lambda: N(18, 10)
run(bot.on_photo(photo_update(), None))
check("it counts", bot.penalty.owed(), 0)
check("and is flagged unchecked", json.loads(db.one(
    "SELECT payload FROM events WHERE kind='penalty_paid' ORDER BY id DESC"
    )["payload"])["unverified"], True)

print("\n=== the SAT road is closed while it is off ===")


async def sat_off_case():
    cfg2, db2, led2, eng2, bot2 = with_photo(PhotoChecker())
    app = build_app(cfg2, eng2, led2)
    tok = cfg2.api.get("sat_token") or cfg2.api["token"]
    async with TestClient(TestServer(app)) as cl:
        r = await cl.post(f"/agent/sat?token={tok}",
                          json={"session": "z1", "score": 10, "total": 10, "seconds": 55})
        body = await r.json()
        check("a perfect drill does not pay it off now", body["ok"], False)
        check("and says why", "not a way to pay" in body["error"], True)

run(sat_off_case())

print("\n=== push-ups, counted by the phone ===")
cfg, db, led, eng, bot = make(pushups={"enabled": True, "reps": 20,
                                       "min_seconds_per_rep": 1.1,
                                       "max_seconds_per_rep": 6.0})
p = bot.penalty
check("twenty at a human pace clears it", p.judge_reps(20, 40)[0], True)
check("nineteen does not", p.judge_reps(19, 40)[0], False)
check("and it says how many were short", "19 of 20" in p.judge_reps(19, 40)[1], True)
check("twenty in eight seconds is a hand over the sensor", p.judge_reps(20, 8)[0], False)
check("and it says so", "hand over the sensor" in p.judge_reps(20, 8)[1], True)
check("twenty over five minutes is a phone on a desk", p.judge_reps(20, 300)[0], False)
check("the slow one is named too", "too slow" in p.judge_reps(20, 300)[1], True)
check("the edges are fair", (p.judge_reps(20, 23)[0], p.judge_reps(20, 115)[0]),
      (True, True))


async def reps_case():
    cfg2, db2, led2, eng2, bot2 = make(pushups={"enabled": True, "reps": 20,
                                                "min_seconds_per_rep": 1.1,
                                                "max_seconds_per_rep": 6.0})
    told = {}

    async def on_paid(result, left, now):
        told["result"] = result
    cfg2.on_penalty_paid = on_paid
    await bot2._owe_drill(N(18, 5), "target missed", "work: physics")
    app = build_app(cfg2, eng2, led2)
    tok = cfg2.api.get("sat_token") or cfg2.api["token"]
    async with TestClient(TestServer(app)) as cl:
        r = await cl.post(f"/agent/reps?token={tok}",
                          json={"session": "p1", "reps": 20, "seconds": 44})
        body = await r.json()
        check("a real set clears it over HTTP",
              (body["ok"], body["cleared"], body["owed"]), (True, True, 0))
        check("and he is told", told.get("result"), "20 push-ups in 44s")

        await bot2._owe_drill(N(18, 30), "target missed", "work: essay")
        r = await cl.post(f"/agent/reps?token={tok}",
                          json={"session": "p2", "reps": 20, "seconds": 6})
        check("waving a hand does not", (await r.json())["owed"], 1)

        r = await cl.post(f"/agent/reps?token={tok}",
                          json={"session": "p1", "reps": 20, "seconds": 44})
        check("one set cannot be sent twice", (await r.json())["ok"], False)

        r = await cl.post("/agent/reps?token=nope", json={"session": "p9", "reps": 20})
        check("wrong token, no", r.status, 401)

        r = await cl.post(f"/agent/sat?token={tok}",
                          json={"session": "s9", "score": 9, "total": 10, "seconds": 58})
        check("either road pays the same debt", (await r.json())["owed"], 0)

run(reps_case())

cfg, db, led, eng, bot = make(pushups={"enabled": False})
run(bot._owe_drill(N(18, 5), "target missed", "x"))
check("with push-ups off only the drill is offered",
      "push-ups" in sent(bot)[-1], False)

print("\n=== switched off ===")
cfg, db, led, eng, bot = make(enabled=False)
run(bot._owe_drill(N(18, 5), "target missed", "x"))
check("nothing is owed", bot.penalty.owed(), 0)
check("and nothing sounds", eng.agent_state(N(18, 6)).active, False)

sys.exit(report("PENALTY TESTS"))
