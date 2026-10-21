"""21 Oct: "it replies to my images after decades".

From the Railway log, three causes, each pinned here:

- photo.get_file() hit the 5-second default read timeout and raised; nothing
  caught it, so the photo got no answer at all.
- Updates ran one at a time, so a photo waited behind a 30-second Friday reply.
- One model answering 503 held a check ~47 s before the chain moved on.
"""
import asyncio
import copy
import os
import sys
import types
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fixtures import build, check, report, RAW
from test_friend_fakes import FakeUpdate, FakeBot, StubValidator
from server.bot import LockstepBot
from server import vision

TZ = ZoneInfo("Asia/Almaty")
run = asyncio.get_event_loop().run_until_complete


def make():
    raw = copy.deepcopy(RAW)
    cfg, db, led, eng, lk = build(raw, now=datetime(2026, 8, 25, 17, 0, tzinfo=TZ))
    bot = LockstepBot(cfg, db, led, eng, lk, StubValidator())
    bot.app = types.SimpleNamespace(bot=FakeBot())
    return cfg, bot


print("=== a slow Telegram download is retried, not fatal ===")
cfg, bot = make()
calls = {"n": 0}


class FlakyPhoto:
    async def get_file(self):
        calls["n"] += 1
        if calls["n"] < 3:
            raise TimeoutError("Timed out")

        async def dl():
            return bytearray(b"jpeg")
        return types.SimpleNamespace(download_as_bytearray=dl)


async def no_sleep(*_a, **_k):
    return None
real_sleep, asyncio.sleep = asyncio.sleep, no_sleep
try:
    got = run(bot._download(FlakyPhoto()))
finally:
    asyncio.sleep = real_sleep
check("third try gets it", got, b"jpeg")
check("after two timeouts", calls["n"], 3)

print("\n=== a crash is said out loud ===")
cfg, bot = make()
u = FakeUpdate()
u.message.photo = [object()]
u.effective_message = u.message
run(bot.on_error(u, types.SimpleNamespace(error=TimeoutError("Timed out"))))
check("he is told to send the pic again", "send it again" in u.message.last, True)
check("and that it was a pic", "pic" in u.message.last, True)

stranger = FakeUpdate(chat_id=999)
stranger.effective_message = stranger.message
run(bot.on_error(stranger, types.SimpleNamespace(error=RuntimeError("x"))))
check("a stranger gets nothing", stranger.message.last, "")

print("\n=== the app runs photos beside talk, with sane timeouts ===")
cfg, bot = make()
app = bot.build()
check("updates run concurrently", app.update_processor.max_concurrent_updates > 1, True)
req = app.bot._request[1]
check("read timeout is no longer 5 s", req.read_timeout, 30)
check("an error handler is registered", len(app.error_handlers) > 0, True)

print("\n=== one model call has a ceiling ===")
cfg.raw["gemini"]["timeout_s"] = 25
v = vision.Validator(cfg)
client = v._get_client()
opts = client._api_client._http_options
check("vision calls time out at 25 s", opts.timeout, 25000)

sys.exit(report("SPEED"))
