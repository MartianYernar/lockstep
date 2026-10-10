"""Configuration loading and time helpers."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _hhmm(value: str) -> time:
    hh, mm = value.split(":")
    return time(int(hh), int(mm))


def _minutes(value: str) -> int:
    hh, mm = value.split(":")
    return int(hh) * 60 + int(mm)


@dataclass
class Config:
    raw: dict
    tz: ZoneInfo

    # schedule
    enforce_start_min: int
    enforce_end_min: int
    interval_min: int
    grace_min: int
    plan_due: time
    plan_nag_times: list = field(default_factory=list)
    daily_report_at: time = time(22, 15)
    hard_end_min: int = 22 * 60 + 20
    mode: str = "spot_check"
    spot_avg_min: int = 45
    spot_min_gap_min: int = 20
    # A plan is allowed to reach earlier than the usual start, but not without
    # limit -- sleep is protected at both ends, not just at night.
    earliest_min: int = 6 * 60
    # The weekday window starts when school ends. Saturday and Sunday have no
    # school, so a single window for all seven days left every weekend morning
    # unenforced -- three weekends in a row scored nothing at all because the
    # day did not begin until 16:10. None means "same as a weekday", so a config
    # without a weekend section behaves exactly as before.
    weekend_days: tuple = (5, 6)                  # Mon=0 .. Sun=6
    weekend_start_min: int | None = None
    weekend_end_min: int | None = None

    @classmethod
    def load(cls, path: str | None = None) -> "Config":
        path = path or os.path.join(ROOT, "config.yaml")
        if not os.path.exists(path):
            raise SystemExit(
                f"No config at {path}. Copy config.example.yaml to config.yaml and fill it in."
            )
        # Explicit, because the file now holds Cyrillic (his shorthand) and a
        # Windows default codepage can't read it. -sig also eats the BOM the
        # live copy carries.
        with open(path, encoding="utf-8-sig") as fh:
            raw = yaml.safe_load(fh)

        # Environment overrides let you keep secrets out of the file on a VPS.
        for env, (section, key) in {
            "LOCKSTEP_BOT_TOKEN": ("telegram", "bot_token"),
            "LOCKSTEP_GEMINI_KEY": ("gemini", "api_key"),
            "LOCKSTEP_API_TOKEN": ("api", "token"),
        }.items():
            if os.environ.get(env):
                raw[section][key] = os.environ[env]

        sched = raw["schedule"]
        wk = sched.get("weekend") or {}
        return cls(
            raw=raw,
            tz=ZoneInfo(raw["timezone"]),
            enforce_start_min=_minutes(sched["enforce_start"]),
            enforce_end_min=_minutes(sched["enforce_end"]),
            weekend_days=tuple(int(d) for d in wk.get("days", (5, 6))),
            weekend_start_min=(_minutes(wk["enforce_start"])
                               if wk.get("enforce_start") else None),
            weekend_end_min=(_minutes(wk["enforce_end"])
                             if wk.get("enforce_end") else None),
            interval_min=int(sched["checkpoint_interval_min"]),
            grace_min=int(sched["grace_min"]),
            hard_end_min=_minutes(sched.get("hard_end", "22:20")),
            earliest_min=_minutes(sched.get("earliest", "06:00")),
            mode=sched.get("mode", "block_start"),
            spot_avg_min=int(sched.get("spot_check_avg_min", 45)),
            spot_min_gap_min=int(sched.get("spot_check_min_gap_min", 20)),
            plan_due=_hhmm(sched["plan_due"]),
            plan_nag_times=[_hhmm(t) for t in sched.get("plan_nag_times", [])],
            daily_report_at=_hhmm(sched.get("daily_report_at", "22:15")),
        )

    # -- the day's window ------------------------------------------------------
    #
    # Every caller that needs the window must ask for a specific DAY. The old
    # enforce_start_min / enforce_end_min fields are the weekday values and are
    # kept because plenty of code and tests read them directly, but anything
    # deciding what is enforced has to go through these.
    def is_weekend(self, day: date) -> bool:
        return day.weekday() in self.weekend_days

    def enforce_start_for(self, day: date) -> int:
        if self.is_weekend(day) and self.weekend_start_min is not None:
            return self.weekend_start_min
        return self.enforce_start_min

    def enforce_end_for(self, day: date) -> int:
        if self.is_weekend(day) and self.weekend_end_min is not None:
            return self.weekend_end_min
        return self.enforce_end_min

    def window_label(self, day: date) -> str:
        s, e = self.enforce_start_for(day), self.enforce_end_for(day)
        return f"{s // 60:02d}:{s % 60:02d}-{e // 60:02d}:{e % 60:02d}"

    # -- convenience accessors -------------------------------------------------
    @property
    def bot_token(self) -> str:
        return self.raw["telegram"]["bot_token"]

    @property
    def user_chat_id(self) -> int:
        return int(self.raw["telegram"]["user_chat_id"])

    @property
    def user_name(self) -> str:
        """What the guardian sees him called.

        It used to be hardcoded in eleven places in bot.py, which meant the
        guardian was reading a name that came from whoever wrote the file
        rather than from the person using it.
        """
        return str(self.raw["telegram"].get("user_name") or "He")

    @property
    def guardian_chat_id(self) -> int:
        return int(self.raw["telegram"]["guardian_chat_id"])

    @property
    def esc(self) -> dict:
        return self.raw["escalation"]

    @property
    def vision(self) -> dict:
        return self.raw["vision"]

    @property
    def offset(self) -> dict:
        return self.raw["offset"]

    @property
    def api(self) -> dict:
        return self.raw["api"]

    @property
    def api_port(self) -> int:
        """The port to actually listen on.

        Railway, Render, Koyeb and friends assign a port at boot and route to
        it; the container is told through $PORT and nothing else. Binding the
        configured 8765 there means the platform's router never finds the app,
        the deploy reports healthy, and the phone gets nothing -- silent in
        exactly the way this project keeps being bitten by.

        On the laptop and on a plain VPS $PORT is unset and config wins.
        """
        return int(os.environ.get("PORT") or self.raw["api"]["port"])

    def plan_start_for(self, day, now) -> int:
        """Where a plan written *now* has to begin.

        10 Oct 2026, 13:21: he asked for a 20-minute food break "right now" and
        the planner put it at 10:00-10:20, because the day is built from the
        window's opening time and Saturday's window opens at 10:00. Three and a
        half hours of it were already gone. For today, the plan starts at the
        current minute; for any other day, at the window.
        """
        start = self.enforce_start_for(day)
        if day != now.date():
            return start
        minute = now.hour * 60 + now.minute
        return max(start, min(minute, self.enforce_end_for(day)))

    # -- time helpers ----------------------------------------------------------
    def now(self) -> datetime:
        return datetime.now(self.tz)

    def today(self) -> date:
        return self.now().date()

    def at(self, day: date, minutes: int) -> datetime:
        """Local datetime for `minutes` past midnight on `day`."""
        return datetime.combine(day, time(0, 0), tzinfo=self.tz) + timedelta(minutes=minutes)

    def commitment_end(self) -> date | None:
        """None when months is 0 -- that means *no* lock, not one that already expired."""
        start = date.fromisoformat(str(self.raw["commitment"]["start"]))
        months = int(self.raw["commitment"]["months"])
        if months <= 0:
            return None
        year = start.year + (start.month - 1 + months) // 12
        month = (start.month - 1 + months) % 12 + 1
        day = min(start.day, [31, 29 if year % 4 == 0 and (year % 100 or year % 400 == 0) else 28,
                              31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
        return date(year, month, day)
