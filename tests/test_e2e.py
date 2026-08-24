"""End-to-end: real API server, real HTTP, and the phone agent's contract.

The contract test matters more than it looks. The Termux script reaches into the
JSON with hard-coded jq paths; if a key is ever renamed server-side the script
silently reads empty strings and the phone just goes quiet -- the worst possible
failure for an accountability system, because it looks like success.
"""
import asyncio, json, os, re, sys, urllib.request
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fixtures import build, check, report
from server import planner
from server.api import build_app
from aiohttp import web

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8799
BASE = f"http://127.0.0.1:{PORT}"


def get(path, token=None):
    req = urllib.request.Request(BASE + path)
    if token:
        req.add_header("X-Lockstep-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, None


async def main():
    cfg, db, led, eng, lk = build()
    today = cfg.today()
    db.set_lock(enabled=1, commitment_end="2099-01-01")
    db.ensure_day(today)
    blocks = planner.parse(
        "16:00-19:00 work: linear algebra pset\n19:00-20:00 gym\n20:00-22:00 work: essay")
    planner.validate_coverage(blocks, 16 * 60, 22 * 60)
    db.replace_blocks(today, [b.as_tuple() for b in blocks])
    db.run("UPDATE days SET plan_locked=1 WHERE date=?", (today.isoformat(),))
    eng.build_checkpoints(today)

    runner = web.AppRunner(build_app(cfg, eng, led))
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", PORT).start()

    print("\n=== API auth ===")
    check("no token -> 401", (await asyncio.to_thread(get, "/agent/state"))[0], 401)
    check("bad token -> 401", (await asyncio.to_thread(get, "/agent/state", "nope"))[0], 401)
    status, body = await asyncio.to_thread(get, "/agent/state", "t")
    check("good token -> 200", status, 200)
    check("query-string token works (Tasker/MacroDroid)",
          (await asyncio.to_thread(get, "/agent/state?token=t"))[0], 200)

    print("\n=== the phone script's jq paths all exist ===")
    sh = open(os.path.join(ROOT, "agent-android", "termux", "nag.sh")).read()
    # Only the reads against $RESP -- the script also jq's its own config file.
    paths = sorted(set(re.findall(r'echo "\$RESP"\s*\|\s*jq -r \'\.(\w+)\'', sh)))
    check("found the script's field reads", len(paths) > 0, True)
    for key in paths:
        check(f"  .{key} is served by the API", key in body, True)
    print(f"       server sends: {sorted(body.keys())}")

    print("\n=== MacroDroid's JSON paths too ===")
    md = open(os.path.join(ROOT, "agent-android", "macrodroid", "RECIPE.md")).read()
    mdpaths = sorted(set(re.findall(r'getJsonPath\(resp, "\$\.(\w+)"\)', md)))
    for key in mdpaths:
        check(f"  $.{key} is served by the API", key in body, True)

    print("\n=== health ===")
    status, h = await asyncio.to_thread(get, "/health")
    check("health needs no token (for uptime monitors)", status, 200)
    check("reports ledger intact", h["ledger_intact"], True)

    print("\n=== ack round-trips into the ledger ===")
    def post_ack():
        req = urllib.request.Request(
            BASE + "/agent/ack", data=json.dumps({"agent": "android", "level": 1}).encode(),
            method="POST", headers={"X-Lockstep-Token": "t", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status
    check("ack accepted", await asyncio.to_thread(post_ack), 200)
    last = db.q("SELECT kind FROM events ORDER BY id DESC LIMIT 1")[0]["kind"]
    check("logged as agent_ack", last, "agent_ack")

    await runner.cleanup()
    return report("E2E TESTS")

sys.exit(asyncio.run(main()))
