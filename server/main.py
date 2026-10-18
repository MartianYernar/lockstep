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


def _seed_config(path: str | None) -> None:
    """Write the config on first boot from LOCKSTEP_CONFIG_B64, if it is missing.

    A fresh host has an empty volume, and the app cannot start without a
    config -- but a volume can only be written to while something is running.
    9 Oct 2026 that deadlock is what stood between a dead bot and a live one.
    So the whole config can live in one base64 environment variable: the first
    boot writes it to the volume, every later boot finds the file and leaves
    it alone, so edits made on the volume are never clobbered.
    """
    import base64
    blob = os.environ.get("LOCKSTEP_CONFIG_B64")
    if not path or not blob or os.path.exists(path):
        return
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(base64.b64decode(blob))
    log.info("seeded %s from LOCKSTEP_CONFIG_B64 (first boot)", path)


def main() -> None:
    _seed_config(os.environ.get("LOCKSTEP_CONFIG"))
    cfg = Config.load(os.environ.get("LOCKSTEP_CONFIG"))
    db = Database(os.environ.get("LOCKSTEP_DB") or os.path.join(ROOT, "lockstep.db"))
    ledger = Ledger(db)
    engine = Engine(cfg, db, ledger)
    retired = engine.retire_blocker_checks()
    if retired:
        log.info("screenshot proof is off: %d waiting checks now want a photo", retired)
    lock = Lock(cfg, db, ledger)
    validator = Validator(cfg)
    bot = LockstepBot(cfg, db, ledger, engine, lock, validator)
    # The API answers his drill app; when a penalty is paid there, the bot is
    # the thing that can tell him. Set after the bot exists -- setting it one
    # line earlier crash-looped the server on 18 Oct.
    cfg.on_penalty_paid = bot.on_penalty_paid

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
    # Both windows, because a startup line showing only the weekday one is how
    # three unenforced weekends went unnoticed.
    import datetime as _dt
    _mon, _sat = _dt.date(2026, 1, 5), _dt.date(2026, 1, 10)
    weekend = (f", weekends {cfg.window_label(_sat)}"
               if cfg.window_label(_sat) != cfg.window_label(_mon) else "")
    log.info("lockstep up — window %s%s, %s",
             cfg.window_label(_mon), weekend, cadence)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    sys.exit(main())
