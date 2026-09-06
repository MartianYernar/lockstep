"""Entry point: wire everything together and run."""
from __future__ import annotations

import asyncio
import logging
import os
import sys

from .api import build_app
from .bot import LockstepBot
from .config import ROOT, Config
from .db import Database
from .engine import Engine
from .ledger import Ledger
from .lock import Lock
from .vision import Validator

logging.basicConfig(
    format="%(asctime)s  %(levelname)-7s %(name)-10s %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("lockstep")


def main() -> None:
    cfg = Config.load(os.environ.get("LOCKSTEP_CONFIG"))
    db = Database(os.environ.get("LOCKSTEP_DB") or os.path.join(ROOT, "lockstep.db"))
    ledger = Ledger(db)
    engine = Engine(cfg, db, ledger)
    lock = Lock(cfg, db, ledger)
    validator = Validator(cfg)
    bot = LockstepBot(cfg, db, ledger, engine, lock, validator)

    intact, bad = ledger.verify()
    if not intact:
        log.error("LEDGER BROKEN at event %s — history was rewritten.", bad)

    if not lock.is_configured:
        log.warning("NO GUARDIAN REGISTERED — nothing is holding this shut. "
                    "Have them message the bot, then send /setguardian <id>.")

    log.info("commitment: %s",
             db.lock()["commitment_end"] or "not started — guardian sends /commit 3")

    app = bot.build()

    # The agent API shares the bot's event loop, so there is one process to run,
    # one process to supervise, and one thing that can be down.
    async def _post_init(application) -> None:
        # Nudge the guardian once per start, but never with a code that a
        # restart would invalidate. An earlier version mailed a fresh setup code
        # on every restart, which meant the code in their inbox was usually the
        # wrong one — the guardian was told their correct input was invalid.
        if lock.guardian_id() and not lock.commitment_active(cfg.today()):
            try:
                await application.bot.send_message(
                    lock.guardian_id(),
                    f"Reminder: {cfg.user_name}'s accountability system is set up and waiting "
                    "on you.\n\nThere is no password to choose. Send "
                    "<code>/commit 3</code> to start it for three months.\n\n"
                    "From then only you can stop it, with <code>/stopbot</code>.",
                    parse_mode="HTML")
            except Exception as exc:                          # noqa: BLE001
                log.error("could not reach guardian: %s", exc)

        await bot.publish_command_menu(application)

        from aiohttp import web
        runner = web.AppRunner(build_app(cfg, engine, ledger))
        await runner.setup()
        site = web.TCPSite(runner, cfg.api["host"], cfg.api_port)
        await site.start()
        application.bot_data["api_runner"] = runner
        from .api import heartbeat_loop
        application.bot_data["heartbeat"] = asyncio.create_task(
            heartbeat_loop(cfg, ledger))
        log.info("agent API on %s:%s", cfg.api["host"], cfg.api_port)

    async def _post_shutdown(application) -> None:
        task = application.bot_data.get("heartbeat")
        if task:
            task.cancel()
        runner = application.bot_data.get("api_runner")
        if runner:
            await runner.cleanup()

    app.post_init = _post_init
    app.post_shutdown = _post_shutdown

    # block_start used to fall through to the "fixed" branch and print
    # "every 3 min", which is the old every-few-minutes behaviour it replaced.
    # Reading that in the log on startup is genuinely alarming and wrong.
    cadence = {
        "spot_check": f"random spot-checks ~1 per {cfg.spot_avg_min} min",
        "block_start": "proof at the top of each block",
    }.get(cfg.mode, f"every {cfg.interval_min} min")
    log.info("lockstep up — window %02d:%02d-%02d:%02d, %s",
             cfg.enforce_start_min // 60, cfg.enforce_start_min % 60,
             cfg.enforce_end_min // 60, cfg.enforce_end_min % 60, cadence)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    sys.exit(main())
