"""The access log must never carry the phone's token.

21 Oct 2026: `railway logs` showed GET /agent/gap?token=<the real one> every
14 seconds. Anyone with the logs could be the phone. The query param still has
to WORK -- Automate sends it that way -- it just must not be written down.
"""
import asyncio, os, sys, logging

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fixtures import build, check, report
from server.api import build_app, redact, make_runner

TOKEN = "phone-secret-7f3a9c"
SAT = "sat-secret-41be02"

cfg, db, led, eng, lk = build()
cfg.api["token"] = TOKEN
cfg.api["sat_token"] = SAT

print("\n=== redact() on its own ===")
check("query token gone",
      redact('GET /agent/gap?token=%s HTTP/1.1' % TOKEN),
      'GET /agent/gap?token=REDACTED HTTP/1.1')
check("other params kept",
      redact("/agent/gap?batt=40&token=%s&chg=1" % TOKEN),
      "/agent/gap?batt=40&token=REDACTED&chg=1")
check("sat_token too", redact("/x?sat_token=%s" % SAT), "/x?sat_token=REDACTED")
check("nothing to redact, nothing changed", redact("/health"), "/health")


class Grab(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines, self.extras = [], []

    def emit(self, record):
        self.lines.append(record.getMessage())
        self.extras.append(repr(record.__dict__))


async def main():
    import aiohttp
    from aiohttp import web
    grab = Grab()
    access = logging.getLogger("aiohttp.access")
    access.addHandler(grab)
    access.setLevel(logging.INFO)
    try:
        # The same runner main.py uses, on a real port: aiohttp's TestServer
        # quietly drops runner kwargs, so it would test the default logger.
        runner = make_runner(build_app(cfg, eng, led))
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        base = "http://127.0.0.1:%d" % port
        async with aiohttp.ClientSession(base) as client:
            print("\n=== the phone still gets in by query param ===")
            r = await client.get("/agent/gap?token=%s&batt=55" % TOKEN)
            check("gap with ?token= -> 200", r.status, 200)
            r = await client.get("/agent/gap",
                                 headers={"X-Lockstep-Token": TOKEN})
            check("gap with header -> 200", r.status, 200)
            r = await client.get("/agent/state?token=%s" % TOKEN)
            check("state with ?token= -> 200", r.status, 200)
            r = await client.get("/agent/sat?token=%s" % SAT)
            check("sat token accepted (not 401)", r.status != 401, True)
            r = await client.post("/agent/reps?token=%s" % SAT, json={})
            check("reps token accepted (not 401)", r.status != 401, True)
            r = await client.get("/agent/gap?token=%s" % TOKEN,
                                 headers={"Referer": "https://x/?token=%s" % TOKEN})
            check("gap with a leaky Referer -> 200", r.status, 200)
        await runner.cleanup()
    finally:
        access.removeHandler(grab)

    print("\n=== ...and none of it is written down ===")
    check("something was logged", len(grab.lines) >= 6, True)
    for line in grab.lines:
        print("   log:", line)
    check("no main token in any log line",
          any(TOKEN in l for l in grab.lines), False)
    check("no sat token in any log line",
          any(SAT in l for l in grab.lines), False)
    check("not in the record's extra fields either",
          any(TOKEN in e or SAT in e for e in grab.extras), False)
    check("the redaction is visible, so the line still reads",
          any("/agent/gap?token=REDACTED" in l for l in grab.lines), True)


asyncio.run(main())
report("access log")
