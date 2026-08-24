"""The two-party lock.

What this actually guarantees, stated plainly:

  * The password is chosen by your father in his own Telegram chat. You never
    see it, and only a bcrypt hash is stored, so reading the database gives you
    nothing usable.
  * Every disable attempt -- successful, failed, or merely started -- is pushed
    to his Telegram before anything happens. Turning the system off is not a
    private act.
  * Before the commitment date, the correct password alone is not enough: he
    must also see your written reason, because that is the part that costs you
    something.

What it does NOT guarantee: you have root on the box. You can always `rm` the
database. The design answer to that is not a stronger lock, it's that the
wreckage is obvious -- the ledger chain breaks, the daily heartbeat stops, and
he notices. See ledger.py.
"""
from __future__ import annotations

import secrets
from datetime import date, datetime, timedelta

import bcrypt

MAX_ATTEMPTS = 3
LOCKOUT_MINUTES = 30


class LockError(Exception):
    """Message is safe to show the user verbatim."""


class Lock:
    def __init__(self, cfg, db, ledger):
        self.cfg = cfg
        self.db = db
        self.ledger = ledger
        self._setup_token: str | None = None

    # -- guardian enrolment ----------------------------------------------------
    @property
    def is_configured(self) -> bool:
        return bool(self.db.lock()["guardian_hash"])

    def new_setup_token(self) -> str:
        self._setup_token = secrets.token_hex(4).upper()
        return self._setup_token

    def complete_setup(self, token: str, password: str, from_chat_id: int) -> None:
        if self.is_configured:
            raise LockError("A guardian password is already set. Use /guardian_change.")
        if from_chat_id != self.cfg.guardian_chat_id:
            raise LockError("Only the guardian's own Telegram account can set the password.")
        if not self._setup_token or not secrets.compare_digest(token.upper(), self._setup_token):
            raise LockError("That setup code is wrong or expired. Restart the bot for a new one.")
        if len(password) < 8:
            raise LockError("Use at least 8 characters.")
        self._store(password)
        self._setup_token = None
        self.ledger.append("guardian_enrolled", {"chat_id": from_chat_id})

    def change_password(self, old: str, new: str, from_chat_id: int) -> None:
        if from_chat_id != self.cfg.guardian_chat_id:
            raise LockError("Only the guardian can change the password.")
        if not self._matches(old):
            raise LockError("Current password is wrong.")
        if len(new) < 8:
            raise LockError("Use at least 8 characters.")
        self._store(new)
        self.ledger.append("guardian_password_changed", {})

    def _store(self, password: str) -> None:
        digest = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        end = self.cfg.commitment_end()
        self.db.set_lock(
            guardian_hash=digest,
            guardian_set_at=datetime.now().astimezone().isoformat(timespec="seconds"),
            failed_attempts=0,
            commitment_end=end.isoformat() if end else None,
        )

    def _matches(self, password: str) -> bool:
        row = self.db.lock()
        if not row["guardian_hash"]:
            return False
        try:
            return bcrypt.checkpw(password.encode(), row["guardian_hash"].encode())
        except ValueError:
            return False

    # -- brute-force damping ---------------------------------------------------
    def _lockout_remaining(self, now: datetime) -> int:
        row = self.db.lock()
        if row["failed_attempts"] < MAX_ATTEMPTS or not row["last_attempt_at"]:
            return 0
        until = datetime.fromisoformat(row["last_attempt_at"]) + timedelta(minutes=LOCKOUT_MINUTES)
        return max(0, int((until - now).total_seconds() // 60))

    def verify(self, password: str, now: datetime) -> bool:
        """Check the password, recording the attempt either way."""
        remaining = self._lockout_remaining(now)
        if remaining:
            # Log it too. Someone hammering during a lockout is precisely the
            # pattern the guardian should be able to see afterwards.
            self.ledger.append("password_attempt", {"ok": False, "blocked": True})
            raise LockError(f"Too many wrong attempts. Locked for another {remaining} min.")

        ok = self._matches(password)
        row = self.db.lock()
        self.db.set_lock(
            failed_attempts=0 if ok else row["failed_attempts"] + 1,
            last_attempt_at=now.isoformat(),
        )
        self.ledger.append("password_attempt", {"ok": ok})
        return ok

    # -- commitment ------------------------------------------------------------
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

    # -- state changes ---------------------------------------------------------
    def disable(self, reason: str, now: datetime) -> None:
        self.db.set_lock(enabled=0)
        self.ledger.append("system_disabled", {"reason": reason, "at": now.isoformat()})

    def enable(self, now: datetime) -> None:
        self.db.set_lock(enabled=1, paused_until=None)
        self.ledger.append("system_enabled", {"at": now.isoformat()})

    def pause(self, until: datetime, reason: str) -> None:
        self.db.set_lock(paused_until=until.isoformat())
        self.ledger.append("system_paused", {"until": until.isoformat(), "reason": reason})

    def extend_commitment(self, months: int, today: date) -> date:
        base = max(today, date.fromisoformat(self.db.lock()["commitment_end"] or today.isoformat()))
        year = base.year + (base.month - 1 + months) // 12
        month = (base.month - 1 + months) % 12 + 1
        leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
        last = [31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]
        end = date(year, month, min(base.day, last))
        self.db.set_lock(commitment_end=end.isoformat(), enabled=1, paused_until=None)
        self.ledger.append("commitment_extended", {"months": months, "until": end.isoformat()})
        return end

    @property
    def enabled(self) -> bool:
        return bool(self.db.lock()["enabled"])
