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
        end = cfg.commitment_end()
        if end:
            db.set_lock(commitment_end=end.isoformat())
    log.info("commitment: %s", db.lock()["commitment_end"] or "none set (trial mode)")

    app = bot.build()

    # The agent API shares the bot's event loop, so there is one process to run,
    # one process to supervise, and one thing that can be down.
    async def _post_init(application) -> None:
        # Close the enrolment loop automatically: if we know who the guardian is
        # but they have not set a password, send them the code directly. It never
        # passes through the user's chat.
        if not lock.is_configured and lock.guardian_id():
            code = lock.new_setup_token()
            try:
                await application.bot.send_message(
                    lock.guardian_id(),
                    "You are the guardian for Ansar's accountability system.\n\n"
                    "He cannot switch it off before the agreed date without a password "
                    "only you know. To set it, reply with:\n\n"
                    f"<code>/guardian {code} your-chosen-password</code>\n\n"
                    "Choose something he will not guess, write it in your notes app, "
                    "and I will delete your message the moment I have read it.\n\n"
                    "You will get one short report each evening, and a message the "
                    "instant he asks to stop.",
                    parse_mode="HTML")
                log.info("sent setup code to guardian %s", lock.guardian_id())
            except Exception as exc:                          # noqa: BLE001
                log.error("could not reach guardian: %s", exc)

        from aiohttp import web
        runner = web.AppRunner(build_app(cfg, engine, ledger))
        await runner.setup()
        site = web.TCPSite(runner, cfg.api["host"], int(cfg.api["port"]))
        await site.start()
        application.bot_data["api_runner"] = runner
        from .api import heartbeat_loop
        application.bot_data["heartbeat"] = asyncio.create_task(
            heartbeat_loop(cfg, ledger))
        log.info("agent API on %s:%s", cfg.api["host"], cfg.api["port"])

    async def _post_shutdown(application) -> None:
        task = application.bot_data.get("heartbeat")
        if task:
            task.cancel()
        runner = application.bot_data.get("api_runner")
        if runner:
            await runner.cleanup()

    app.post_init = _post_init
    app.post_shutdown = _post_shutdown

    cadence = (f"random spot-checks ~1 per {cfg.spot_avg_min} min"
               if cfg.mode == "spot_check" else f"every {cfg.interval_min} min")
    log.info("lockstep up — window %02d:%02d-%02d:%02d, %s",
             cfg.enforce_start_min // 60, cfg.enforce_start_min % 60,
             cfg.enforce_end_min // 60, cfg.enforce_end_min % 60, cadence)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    sys.exit(main())
