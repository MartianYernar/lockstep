"""19 Sep: "work: math sat questions without chair at table, on knees".

He wrote "do math sat questions without chair at table, on knees". The
planner added "work: " to it, and "work: " is not a label -- it means the
proof is a screenshot of the computer blocker. So the bot spent half an hour
demanding a Cold Turkey window for questions being done on the floor with a
pen, and the siren ran the whole time.

Two things follow: the planner never touches his words, and when the blocker
cannot be shown there is a way out that costs him a photo rather than the
block.
"""
import asyncio
import copy
import io
import os
import sys
import types
from datetime import date, datetime
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
HIS_BLOCK = "work: math sat questions without chair at table, on knees"


def N(h, m):
    return datetime(2026, 8, 25, h, m, tzinfo=TZ)


class Checker:
    def __init__(self, code="no_blocker"):
        self.code, self.kinds = code, []

    async def check(self, blob, label, category, now, mime="image/jpeg", kind="activity"):
        self.kinds.append(kind)
        if kind == "activity":
            return Verdict(ok=True, reason="ok", failure_code="ok")
        return Verdict(ok=False, reason="No Cold Turkey window in the shot.",
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


def make(now=N(14, 45), code="no_blocker"):
    raw = copy.deepcopy(RAW)
    raw["schedule"] = dict(raw["schedule"], mode="block_start", grace_min=3,
                           enforce_start="14:00")
    raw["vision"] = dict(raw["vision"], require_blocker_for_work=True)
    raw["standup"] = {"enabled": False}
    cfg, db, led, eng, lk = build(raw, now=now)
    bot = LockstepBot(cfg, db, led, eng, lk, Checker(code))
    bot.app = types.SimpleNamespace(bot=FakeBot())
    db.set_lock(enabled=1, commitment_end="2099-01-01")
    db.ensure_day(DAY)
    db.replace_blocks(DAY, [b.as_tuple() for b in planner.parse(
        f"14:00-16:00 {HIS_BLOCK}\n16:00-22:00 relax")])
    db.run("UPDATE days SET plan_locked=1 WHERE date=?", (DAY.isoformat(),))
    eng.build_checkpoints(DAY, N(13, 59))
    eng.tick(now)
    return cfg, db, led, eng, bot


def kinds(db):
    """Kinds on his block only -- the bed check at 22:20 is nobody's business."""
    blk = db.blocks(DAY)[0]
    return [r["kind"] for r in db.q(
        "SELECT kind FROM checkpoints WHERE date=? AND block_id=? AND state!='excused'"
        " ORDER BY due_at", (DAY.isoformat(), blk["id"]))]


print("=== the block asks for a screenshot he cannot take ===")
cfg, db, led, eng, bot = make()
check("because the label starts with work:", kinds(db)[0], "blocker_toggle")
u = photo()
run(bot.on_photo(u, None))
check("so his photo is refused", u.message.last.startswith("❌"), True)
check("and he is told the way out", "/paper" in u.message.last, True)

print("\n=== /paper switches the proof, not the block ===")
u = FakeUpdate("/paper")
run(bot.cmd_paper(u, None))
check("he's told what changed", "on paper" in u.message.last, True)
check("the remaining checks want a photo now",
      set(k for k in kinds(db) if k), {"activity"})
check("the block keeps his words", db.blocks(DAY)[0]["label"], HIS_BLOCK)
check("the hours are untouched",
      (db.blocks(DAY)[0]["start_min"], db.blocks(DAY)[0]["end_min"]), (14 * 60, 16 * 60))
check("it's on the record",
      db.one("SELECT COUNT(*) c FROM events WHERE kind='block_on_paper'")["c"], 1)

u = photo()
run(bot.on_photo(u, None))
check("and now a photo of him working counts", u.message.last.startswith("✅"), True)

print("\n=== saying it twice, or about nothing ===")
u = FakeUpdate("/paper")
run(bot.cmd_paper(u, None))
check("nothing left to switch", "already wants a photo" in u.message.last, True)

cfg, db, led, eng, bot = make(now=N(13, 50))   # before the block starts
u = FakeUpdate("/paper")
run(bot.cmd_paper(u, None))
check("outside any block it asks which one", "which block" in u.message.last, True)
u = FakeUpdate("/paper math")
run(bot.cmd_paper(u, None))
check("naming it works from anywhere", "on paper" in u.message.last, True)

print("\n=== telling Friday in words does the same ===")
import json  # noqa: E402
cfg, db, led, eng, bot = make()


async def fake(system, contents, now, max_tokens=400, schema=None):
    bot.coach._charge(now.date())
    return json.dumps({"reply": "ah you're on paper, got it",
                       "actions": [{"type": "on_paper", "text": ""}]})
bot.coach._ask = fake
u = FakeUpdate("bro im doing it on paper on my knees, no laptop")
run(bot.on_text(u, None))
check("his words come first", u.message.replies[0], "ah you're on paper, got it")
check("then the switch", "on paper" in u.message.last, True)
check("the checks changed", set(k for k in kinds(db) if k), {"activity"})

print("\n=== the planner is told to leave his labels alone ===")
from server.dayplan import SYSTEM  # noqa: E402
check("work: is described as an instruction, not a label",
      "not a label" in SYSTEM and "blocker" in SYSTEM, True)
check("only for blocks it adds, at a computer", "AT A COMPUTER" in SYSTEM, True)
check("never on paper work", "on paper" in SYSTEM, True)
check("and never added to or removed from his own words",
      "NEVER add it to, or remove it from, a block HE wrote" in SYSTEM, True)

print("\n=== a blocker that is merely switched off is the same offer ===")
cfg, db, led, eng, bot = make(code="blocker_off")
u = photo()
run(bot.on_photo(u, None))
check("also offers /paper", "/paper" in u.message.last, True)

print("\n=== but a missing clock is not a paper problem ===")
cfg, db, led, eng, bot = make(code="no_timestamp")
u = photo()
run(bot.on_photo(u, None))
check("no /paper offer", "/paper" in u.message.last, False)

sys.exit(report("PAPER TESTS"))
