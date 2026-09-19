"""19 Sep 14:41: a playground of white poles was "location-revealing".

The rule said a street, buildings or signage counted, so every outdoor frame
failed -- and he is asked for outdoor photos: workouts, walks, the podyezd.
A rule that makes the required proof impossible teaches him to ignore the bot.

What replaces it: could a stranger FIND him from this frame. A street name, a
house number, his school's logo, an address on screen -- yes. Tarmac, poles,
grass, a bus -- no. And because that is a judgement rather than a fact, a
location-only refusal is one he can argue with; a face or a login is not.
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
from server.vision import Validator
from server import planner

TZ = ZoneInfo("Asia/Almaty")
DAY = date(2026, 8, 25)
run = asyncio.get_event_loop().run_until_complete


def N(h, m):
    return datetime(2026, 8, 25, h, m, tzinfo=TZ)


BASE = {
    "timestamp_visible": True, "timestamp_text": "16:05", "replay_suspected": False,
    "activity_seen": "someone on a playground", "activity_match": True,
    "confidence": 0.9, "reason": "fine", "face_visible": False,
    "identity_visible": False, "location_revealing": False, "privacy_detail": "",
    "blocker_visible": True, "blocker_running": True, "blocker_clock": "16:05",
    "allowed_items": [], "blocked_items": [],
}


def validator(**over):
    cfg, _db, _led, _eng, _lk = build(copy.deepcopy(RAW), now=N(16, 5))
    v = Validator(cfg)

    async def fake(image_bytes, mime, prompt):
        fake.prompt = prompt
        return {**BASE, **over}
    v._ask = fake
    return v


def verdict(**over):
    v = validator(**over)
    return run(v.check(b"x", "workout outside", "sport", N(16, 5), kind="activity"))


print("=== an ordinary outdoor frame passes ===")
r = verdict()
check("counted", r.ok, True)

print("\n=== a place that could find him is refused, but arguably ===")
r = verdict(location_revealing=True, privacy_detail="The street sign names your road.")
check("refused", r.ok, False)
check("with its own code", r.failure_code, "privacy_location")
check("and says what to move", "street sign" in r.reason, True)
check("and invites him to explain", "look again" in r.reason, True)

print("\n=== a face is not up for discussion ===")
r = verdict(face_visible=True, privacy_detail="Your face is in shot.")
check("refused", r.ok, False)
check("plain privacy code", r.failure_code, "privacy")
check("no invitation to argue", "look again" in r.reason, False)
check("it names whose rule it is", "Your rule" in r.reason, True)

r = verdict(identity_visible=True)
check("a login is the same", r.failure_code, "privacy")

r = verdict(face_visible=True, location_revealing=True)
check("a face plus a place is still a face", r.failure_code, "privacy")
check("both are named", "a face" in r.reason and "place you" in r.reason, True)

print("\n=== the rule the checker is given ===")
v = validator()
run(v.check(b"x", "workout outside", "sport", N(16, 5), kind="activity"))
p = v._ask.prompt
check("it asks whether a stranger could find him", "STRANGER COULD FIND HIM" in p, True)
for allowed in ("playground", "tarmac", "grass", "a bus", "stairwell"):
    check(f"{allowed} is explicitly allowed", allowed in p, True)
for refused in ("street name", "house number", "school"):
    check(f"{refused} still counts", refused in p, True)
check("and it is told to default to allowing", "you would have to guess" in p.lower(), True)


print("\n=== in the bot: a place can be explained, a face cannot ===")


async def _file():
    async def dl():
        from PIL import Image
        buf = io.BytesIO(); Image.new("RGB", (40, 40)).save(buf, "JPEG")
        return bytearray(buf.getvalue())
    return types.SimpleNamespace(download_as_bytearray=dl)


class Checker:
    def __init__(self, code):
        self.code = code

    async def check(self, blob, label, category, now, mime="image/jpeg", kind="activity"):
        from server.vision import Verdict
        if "explains" in label:                       # the recheck
            return Verdict(ok=True, reason="ok", failure_code="ok")
        return Verdict(ok=False, reason="no", failure_code=self.code)


def bot_with(code):
    raw = copy.deepcopy(RAW)
    raw["schedule"] = dict(raw["schedule"], mode="block_start", grace_min=3)
    cfg, db, led, eng, lk = build(raw, now=N(16, 5))
    b = LockstepBot(cfg, db, led, eng, lk, Checker(code))
    b.app = types.SimpleNamespace(bot=FakeBot())
    db.set_lock(enabled=1, commitment_end="2099-01-01")
    db.ensure_day(DAY)
    db.replace_blocks(DAY, [x.as_tuple() for x in planner.parse("16:00-22:00 workout outside")])
    db.run("UPDATE days SET plan_locked=1 WHERE date=?", (DAY.isoformat(),))
    eng.build_checkpoints(DAY, N(15, 59))
    eng.tick(N(16, 1))
    u = FakeUpdate()
    u.message.photo = [types.SimpleNamespace(get_file=_file)]
    run(b.on_photo(u, None))
    return b, u


b, u = bot_with("privacy_location")
check("he is told he can explain it", "tell me what it is" in u.message.last, True)
check("and the photo is kept for another look", b.last_reject is not None, True)

b, u = bot_with("privacy")
check("a face rejection offers nothing", "tell me what it is" in u.message.last, False)
check("and keeps no photo", b.last_reject, None)

sys.exit(report("PRIVACY TESTS"))
