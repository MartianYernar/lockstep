"""Tiny HTTP surface for the enforcement agents.

The laptop agent and the phone both poll GET /agent/state and act on what comes
back. All the rules live on the server, so the agents stay dumb -- which is the
point: a dumb agent has nothing in it worth tampering with, and rewriting one
buys you nothing because the server still knows.
"""
from __future__ import annotations

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
        st = engine.agent_state(cfg.now())
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

    app.router.add_get("/health", health)
    app.router.add_get("/agent/state", state)
    app.router.add_post("/agent/ack", ack)
    return app


async def start(cfg, engine, ledger) -> web.AppRunner:
    app = build_app(cfg, engine, ledger)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, cfg.api["host"], int(cfg.api["port"]))
    await site.start()
    log.info("agent API listening on %s:%s", cfg.api["host"], cfg.api["port"])
    return runner
