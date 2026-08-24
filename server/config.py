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
    mode: str = "spot_check"
    spot_avg_min: int = 45
    spot_min_gap_min: int = 20

    @classmethod
    def load(cls, path: str | None = None) -> "Config":
        path = path or os.path.join(ROOT, "config.yaml")
        if not os.path.exists(path):
            raise SystemExit(
                f"No config at {path}. Copy config.example.yaml to config.yaml and fill it in."
            )
        with open(path) as fh:
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
        return cls(
            raw=raw,
            tz=ZoneInfo(raw["timezone"]),
            enforce_start_min=_minutes(sched["enforce_start"]),
            enforce_end_min=_minutes(sched["enforce_end"]),
            interval_min=int(sched["checkpoint_interval_min"]),
            grace_min=int(sched["grace_min"]),
            mode=sched.get("mode", "spot_check"),
            spot_avg_min=int(sched.get("spot_check_avg_min", 45)),
            spot_min_gap_min=int(sched.get("spot_check_min_gap_min", 20)),
            plan_due=_hhmm(sched["plan_due"]),
            plan_nag_times=[_hhmm(t) for t in sched.get("plan_nag_times", [])],
            daily_report_at=_hhmm(sched.get("daily_report_at", "22:15")),
        )

    # -- convenience accessors -------------------------------------------------
    @property
    def bot_token(self) -> str:
        return self.raw["telegram"]["bot_token"]

    @property
    def user_chat_id(self) -> int:
        return int(self.raw["telegram"]["user_chat_id"])

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
