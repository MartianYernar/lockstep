"""Tiny HTTP surface for the enforcement agents.

The laptop agent and the phone both poll GET /agent/state and act on what comes
back. All the rules live on the server, so the agents stay dumb -- which is the
point: a dumb agent has nothing in it worth tampering with, and rewriting one
buys you nothing because the server still knows.
"""
from __future__ import annotations

import asyncio
import logging

from aiohttp import web

log = logging.getLogger("api")


def _authorised(request: web.Request, token: str) -> bool:
    header = request.headers.get("X-Lockstep-Token")
    if header and header == token:
        return True
    # Tasker and Shortcuts make query strings much easier than headers.
    return request.query.get("token") == token


def build_app(cfg, engine, ledger) -> web.Application:
    token = cfg.api["token"]
    app = web.Application()

    async def health(_request: web.Request) -> web.Response:
        ok, bad = ledger.verify()
        return web.json_response({
            "status": "ok",
            "time": cfg.now().isoformat(),
            "ledger_intact": ok,
            "ledger_break_at": bad,
        })

    async def state(request: web.Request) -> web.Response:
        if not _authorised(request, token):
            return web.json_response({"error": "unauthorised"}, status=401)
        # Every poll is a heartbeat. A phone that stops asking is a phone that
        # can no longer nag you, which looks exactly like compliance.
        cfg_now = cfg.now()
        engine.db.set_lock(last_agent_poll=cfg_now.isoformat())
        st = engine.agent_state(cfg_now)
        return web.json_response(st.as_dict())

    async def ack(request: web.Request) -> web.Response:
        """Agents report what they actually did, so the log reflects reality."""
        if not _authorised(request, token):
            return web.json_response({"error": "unauthorised"}, status=401)
        try:
            body = await request.json()
        except Exception:                                     # noqa: BLE001
            body = {}
        ledger.append("agent_ack", {
            "agent": body.get("agent", "unknown"),
            "opened": body.get("opened"),
            "closed": body.get("closed"),
            "level": body.get("level"),
        })
        return web.json_response({"ok": True})

    async def siren(request: web.Request) -> web.Response:
        """The same answer as /agent/state, in one line of plain text.

        The JSON route needs the phone to parse a body into a dictionary and
        then test a field. That is four steps and it failed silently on the
        real phone: MacroDroid's JSON Parse hands back the string "true", the
        If compares it against a boolean, no match, no siren, no error. A macro
        that fetches correctly and then does nothing is indistinguishable from
        a working one until the night you need it.

        This is a substring test instead. "ALARM 85 10" or "QUIET".
        """
        if not _authorised(request, token):
            return web.Response(status=401, text="UNAUTHORISED")
        now = cfg.now()
        engine.db.set_lock(last_agent_poll=now.isoformat())
        st = engine.agent_state(now).as_dict()
        body = (f"ALARM {int(st['alarm_volume_pct'])} {int(st['alarm_repeats'])}"
                if st["phone_alarm"] else "QUIET")
        return web.Response(text=body, content_type="text/plain")

    app.router.add_get("/health", health)
    app.router.add_get("/agent/state", state)
    app.router.add_get("/agent/siren", siren)
    app.router.add_post("/agent/ack", ack)
    return app


async def heartbeat_loop(cfg, ledger) -> None:
    """Ping an outside service on a schedule, so its silence is the alarm.

    Everything else in this project can be switched off by whoever holds the
    server. This cannot: the alerting lives somewhere else entirely, and the
    signal is the ABSENCE of a ping. Deleting the box, revoking the token,
    pulling the plug -- every one of those makes the monitor fire rather than
    go quiet, because there is nothing to suppress. You cannot delete your way
    out of a thing that reacts to deletion.

    Point it at a free healthchecks.io or UptimeRobot check whose account
    belongs to the guardian, not to you.
    """
    conf = cfg.raw.get("heartbeat", {})
    url = conf.get("url")
    if not url:
        return
    every = int(conf.get("every_sec", 300))
    log.info("heartbeat -> %s every %ds", url.split("/")[2], every)
    import aiohttp
    while True:
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                    if r.status >= 400:
                        log.warning("heartbeat rejected: %s", r.status)
        except Exception as exc:                              # noqa: BLE001
            log.warning("heartbeat failed: %s", exc)
        await asyncio.sleep(every)


async def start(cfg, engine, ledger) -> web.AppRunner:
    app = build_app(cfg, engine, ledger)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, cfg.api["host"], cfg.api_port)
    await site.start()
    log.info("agent API listening on %s:%s", cfg.api["host"], cfg.api_port)
    return runner
