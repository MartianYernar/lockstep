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


async def fake(system, contents, now, max_tokens=400, schema=None, tier=None):
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
from server.dayplan import SYSTEM, DayPlanner  # noqa: E402
check("the planner is told it's a switch only he flips",
      "only he flips" in SYSTEM and "Never write \"work: \"" in SYSTEM, True)
# 21 Sep it added "work:" to "sat prep" anyway. The code has the last word.
built = planner.parse("16:10-17:40 work: Physics IA\n17:40-19:40 work: sat prep\n"
                      "19:40-22:00 work: essay")
kept = DayPlanner._keep_his_switch(built, "16:10-17:40 work: physics IA\nrest yourself")
check("a work: he never wrote is taken off", kept[1].label, "sat prep")
check("and the proof it demands follows the label",
      kept[1].category, planner.categorise("sat prep"))
check("one he wrote himself stays", kept[0].label, "work: Physics IA")
check("even with different spacing and case",
      DayPlanner._keep_his_switch(built, "WORK:   ESSAY")[2].label, "work: essay")
check("nothing he wrote, nothing kept",
      [b.label for b in DayPlanner._keep_his_switch(built, "")],
      ["Physics IA", "sat prep", "essay"])

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

print("\n=== /paper still works after the plan was rewritten ===")
# 10 Oct 16:24. He replanned, then asked for paper mode, and was told the
# block "already wants a photo" while the siren kept demanding a Cold Turkey
# screenshot. replace_blocks had re-created every block row, so the update --
# keyed on block_id -- matched nothing at all.
cfg, db, led, eng, bot = make(N(16, 1))
run(bot.cmd_replan(FakeUpdate(
    "/replan\n16:01-18:00 work: replace Activity of May in CAS site\n"
    "18:00-22:00 work: essay"), None))
opens = db.q("SELECT id, kind, state, block_id FROM checkpoints WHERE date=?"
             " AND state IN ('scheduled','asked','overdue') AND kind LIKE 'blocker%'",
             (DAY.isoformat(),))
check("there is a blocker check to flip", len(opens) > 0, True)
check("and it points at a block that exists",
      all(c["block_id"] in [b["id"] for b in db.blocks(DAY)] for c in opens), True)

u = FakeUpdate("/paper")
run(bot.cmd_paper(u, None))
check("paper mode is applied, not waved away", "on paper" in u.message.last, True)
check("the blocker check inside that block became a photo check",
      db.one("SELECT COUNT(*) c FROM checkpoints WHERE date=? AND kind LIKE 'blocker%'"
             " AND state IN ('scheduled','asked','overdue') AND due_at < ?",
             (DAY.isoformat(), N(18, 0).isoformat()))["c"], 0)
check("and the evening block, which he said nothing about, still wants its screenshot",
      db.one("SELECT COUNT(*) c FROM checkpoints WHERE date=? AND kind LIKE 'blocker%'"
             " AND due_at >= ?", (DAY.isoformat(), N(18, 0).isoformat()))["c"] > 0, True)

sys.exit(report("PAPER TESTS"))
