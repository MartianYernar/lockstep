"""The two-party lock.

There is deliberately no password anywhere in this file.

A password is a secret, and secrets fail in exactly the ways this project has
already been bitten by: forgotten, lost with a phone, written down somewhere
unhelpful, or talked out of someone at eleven at night. The App Lock on the
phone is still shut for precisely that reason.

What replaces it is an address. The system does what one Telegram account tells
it to and ignores everyone else. An account cannot be forgotten, cannot be
reset by guessing a childhood pet, and cannot be typed by someone who isn't
holding that phone.

What this does NOT guarantee: you have root on the box, and you can always `rm`
the database. The answer to that is not a stronger lock -- it's that the wreckage
is loud. The ledger chain breaks, the nightly report stops, and the external
heartbeat fires at an address you don't control. See ledger.py and
docs/THREAT-MODEL.md.
"""
from __future__ import annotations

import secrets
from datetime import date, datetime, timedelta

class LockError(Exception):
    """Message is safe to show the user verbatim."""


class Lock:
    def __init__(self, cfg, db, ledger):
        self.cfg = cfg
        self.db = db
        self.ledger = ledger

    # -- who holds the switch ---------------------------------------------
    def guardian_id(self) -> int:
        """Whoever is registered, falling back to the config file."""
        row = self.db.lock()
        return int(row["guardian_chat_id"] or self.cfg.guardian_chat_id or 0)

    def register_guardian(self, chat_id: int) -> None:
        self.db.set_lock(guardian_chat_id=int(chat_id))
        self.ledger.append("guardian_registered", {"chat_id": int(chat_id)})

    @property
    def is_configured(self) -> bool:
        return bool(self.guardian_id())

    # -- the commitment ----------------------------------------------------
    def start_commitment(self, months: int, today: date) -> date:
        end = _add_months(today, months)
        self.db.set_lock(commitment_end=end.isoformat(), enabled=1, paused_until=None)
        self.ledger.append("commitment_started", {"months": months, "until": end.isoformat()})
        return end

    def extend_commitment(self, months: int, today: date) -> date:
        row = self.db.lock()
        base = max(today, date.fromisoformat(row["commitment_end"] or today.isoformat()))
        end = _add_months(base, months)
        self.db.set_lock(commitment_end=end.isoformat(), enabled=1, paused_until=None)
        self.ledger.append("commitment_extended", {"months": months, "until": end.isoformat()})
        return end

    def commitment_active(self, today: date) -> bool:
        row = self.db.lock()
        if not row["commitment_end"]:
            return False
        return today < date.fromisoformat(row["commitment_end"])

    def days_remaining(self, today: date) -> int:
        row = self.db.lock()
        if not row["commitment_end"]:
            return 0
        return max(0, (date.fromisoformat(row["commitment_end"]) - today).days)

    # -- stopping, on a delay ---------------------------------------------
    def schedule_stop(self, reason: str, now: datetime, hours: int) -> datetime:
        """Agree to stop, but not yet.

        This is the one mechanism here with real evidence behind it. An urge is
        not a decision -- it is a state, and states pass. Almost nobody who
        wants out at 11pm still wants out at 11pm tomorrow, so a delay costs
        nothing to the person with a genuine reason and everything to the person
        with a craving.

        Denial is what you fight. A delay is what you wait through, and waiting
        is the one thing an urge cannot do.
        """
        at = now + timedelta(hours=hours)
        self.db.set_lock(stop_at=at.isoformat(), stop_reason=reason)
        self.ledger.append("stop_scheduled", {"at": at.isoformat(), "reason": reason})
        return at

    def cancel_stop(self) -> bool:
        if not self.db.lock()["stop_at"]:
            return False
        self.db.set_lock(stop_at=None, stop_reason=None)
        self.ledger.append("stop_cancelled", {})
        return True

    def pending_stop(self) -> datetime | None:
        raw = self.db.lock()["stop_at"]
        return datetime.fromisoformat(raw) if raw else None

    # -- state changes -----------------------------------------------------
    def disable(self, reason: str, now: datetime) -> None:
        self.db.set_lock(enabled=0)
        self.ledger.append("system_disabled", {"reason": reason, "at": now.isoformat()})

    def enable(self, now: datetime) -> None:
        self.db.set_lock(enabled=1, paused_until=None)
        self.ledger.append("system_enabled", {"at": now.isoformat()})

    def pause(self, until: datetime, reason: str) -> None:
        self.db.set_lock(paused_until=until.isoformat())
        self.ledger.append("system_paused", {"until": until.isoformat(), "reason": reason})

    @property
    def enabled(self) -> bool:
        return bool(self.db.lock()["enabled"])


def _add_months(base: date, months: int) -> date:
    year = base.year + (base.month - 1 + months) // 12
    month = (base.month - 1 + months) % 12 + 1
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    last = [31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]
    return date(year, month, min(base.day, last))
