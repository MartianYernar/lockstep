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
        token = lock.new_setup_token()
        banner = (
            "\n" + "=" * 62 +
            "\n  NO GUARDIAN PASSWORD SET — the lock is not live yet."
            f"\n\n  Have your father send this to the bot, from his own Telegram:"
            f"\n\n      /guardian {token} his-chosen-password"
            "\n\n  The code dies when this process restarts."
            "\n" + "=" * 62 + "\n"
        )
        log.warning(banner)

    if not lock.db.lock()["commitment_end"]:
        db.set_lock(commitment_end=cfg.commitment_end().isoformat())
    log.info("commitment runs until %s", db.lock()["commitment_end"])

    app = bot.build()

    # The agent API shares the bot's event loop, so there is one process to run,
    # one process to supervise, and one thing that can be down.
    async def _post_init(application) -> None:
        from aiohttp import web
        runner = web.AppRunner(build_app(cfg, engine, ledger))
        await runner.setup()
        site = web.TCPSite(runner, cfg.api["host"], int(cfg.api["port"]))
        await site.start()
        application.bot_data["api_runner"] = runner
        log.info("agent API on %s:%s", cfg.api["host"], cfg.api["port"])

    async def _post_shutdown(application) -> None:
        runner = application.bot_data.get("api_runner")
        if runner:
            await runner.cleanup()

    app.post_init = _post_init
    app.post_shutdown = _post_shutdown

    log.info("lockstep up — window %02d:%02d-%02d:%02d every %d min",
             cfg.enforce_start_min // 60, cfg.enforce_start_min % 60,
             cfg.enforce_end_min // 60, cfg.enforce_end_min % 60, cfg.interval_min)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    sys.exit(main())
