"""A siren on the machine the bot is running on.

Why this exists
---------------
The phone was the entire enforcement surface, and on 1 Sep it went silent for
an evening: the macro fetched `"phone_alarm": true` every sixty seconds and did
nothing audible with it, and the phone could not be reached to fix the macro.
A system whose only punishment lives behind a UI you cannot open is a system
with no punishment.

So the escalation stops depending on one device. The bot already runs on the
laptop he works at during a `work:` block, and a laptop can make a noise.

What this is not
----------------
Weaker than the phone, and honestly so. It only reaches him while he is at this
machine; he can mute the laptop; closing the lid ends it. The phone version
survives silent mode and Do Not Disturb because it uses Android's alarm stream,
and nothing here matches that.

It is a second surface, not a replacement. Two things that fail differently are
better than one thing that fails completely -- which is exactly what happened
tonight.

Windows-only by construction: `winsound` is in the standard library there and
absent everywhere else. On the VPS this import fails, `available()` returns
False, and nothing else in the system notices.
"""
from __future__ import annotations

import asyncio
import logging
import sys

log = logging.getLogger("siren")

try:                                                          # pragma: no cover
    import winsound                                           # type: ignore
except ImportError:                                           # pragma: no cover
    winsound = None                                           # noqa: N816


def available() -> bool:
    return sys.platform == "win32" and winsound is not None


def _wail(repeats: int) -> None:
    """Blocking. Always called on a worker thread.

    A rising-falling two-tone rather than one flat pitch. A steady beep fades
    into the background within about a minute; an alternating one does not,
    which is the entire point of a siren.
    """
    if not available():
        return
    for _ in range(max(1, repeats)):
        for freq in (880, 1320):
            try:
                winsound.Beep(freq, 400)
            except RuntimeError:                              # no audio device
                return


async def fire(repeats: int = 4) -> None:
    """Sound the siren without blocking the bot.

    winsound.Beep holds the thread for the whole duration, and the bot's event
    loop also drives Telegram and the agent API. Blocking it would stop the very
    checkpoint that ends the siren from being processed.
    """
    if not available():
        return
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, _wail, repeats)
    except Exception as exc:                                  # noqa: BLE001
        log.warning("laptop siren failed: %s", exc)
