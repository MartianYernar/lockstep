"""How much is left, how much time there is, and whether those two agree.

A deadline on its own is a date. "SAT on 3 October" tells him nothing he can
act on at 16:10 on a Tuesday. What acts on him is the arithmetic: twenty-five
hours still needed, eleven working days left, five and a half hours a day of
window -- so an hour and a half every single day, and every day he skips
raises the number for the rest.

Two facts make that possible:

- **hours needed**, which he sets per task, roughly; and
- **hours actually done**, which is not what he planned but what he proved.
  A block with no photo in it never happened as far as this is concerned,
  which is the entire point of the rest of the system.

From those comes a verdict per task -- comfortable, tight, or missing -- and
the ordering the planner uses when it decides what tomorrow is for.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

# A satisfied checkpoint stands for the stretch of block it sits in. With
# proof every 20 minutes, one photo is 20 minutes of proven work; the block's
# own length is the ceiling, so a 30-minute block can never bank an hour.
MIN_PER_PROOF = 20


class Agenda:
    def __init__(self, db, cfg, tasks):
        self.db = db
        self.cfg = cfg
        self.tasks = tasks

    # -- what he has actually done -------------------------------------------
    def hours_done(self, task_id: int, since: date | None = None) -> float:
        """Proven hours on this task. Planning a block is not doing it."""
        rows = self.db.q(
            "SELECT b.label, b.start_min, b.end_min, COUNT(c.id) proofs"
            "  FROM blocks b JOIN checkpoints c ON c.block_id = b.id AND c.date = b.date"
            " WHERE c.state = 'satisfied'" + (" AND b.date >= ?" if since else "") +
            " GROUP BY b.id",
            (since.isoformat(),) if since else ())
        gap = int((self.cfg.raw.get("proof", {}) or {}).get("within_block_min", MIN_PER_PROOF)) \
            or MIN_PER_PROOF
        minutes = 0
        for r in rows:
            if task_id in self.tasks.match(r["label"]):
                minutes += min(int(r["proofs"]) * gap, r["end_min"] - r["start_min"])
        return round(minutes / 60.0, 2)

    def hours_planned(self, task_id: int, day: date) -> float:
        """Hours the plan for `day` gives this task, proven or not."""
        minutes = sum(b["end_min"] - b["start_min"] for b in self.db.blocks(day)
                      if task_id in self.tasks.match(b["label"]))
        return round(minutes / 60.0, 2)

    # -- how much time there is ----------------------------------------------
    def window_hours(self, day: date) -> float:
        start = self.cfg.enforce_start_for(day)
        end = self.cfg.enforce_end_for(day)
        return round(max(0, end - start) / 60.0, 2)

    def hours_available(self, first: date, last: date) -> float:
        """Window hours between two dates, inclusive, minus a realism haircut.

        Nobody works every minute of an open window: there is dinner, a
        commute, a shower. The haircut is what stops the forecast being a
        cheerful lie.
        """
        keep = float((self.cfg.raw.get("agenda", {}) or {}).get("usable_fraction", 0.65))
        total, d = 0.0, first
        while d <= last:
            total += self.window_hours(d)
            d += timedelta(days=1)
        return round(total * keep, 1)

    # -- the verdict ----------------------------------------------------------
    def status(self, task, today: date) -> dict:
        """Everything worth knowing about one task, as numbers."""
        needed = float(task["hours_needed"] or 0)
        done = self.hours_done(task["id"])
        left = max(0.0, needed - done) if needed else 0.0
        out = {
            "id": task["id"], "title": task["title"], "due": task["due"],
            "priority": int(task["priority"] or 2),
            "hours_needed": needed, "hours_done": done, "hours_left": left,
            "days_left": None, "per_day": None, "available": None, "verdict": "no deadline",
        }
        if not task["due"]:
            if needed and left <= 0:
                out["verdict"] = "hours are in"
            return out

        due = date.fromisoformat(task["due"])
        days = (due - today).days
        out["days_left"] = days
        if days < 0:
            out["verdict"] = "OVERDUE"
            return out
        # The deadline day itself counts: something due Friday can be worked on
        # Friday, before it is handed in.
        avail = self.hours_available(today, due)
        out["available"] = avail
        if not needed:
            out["verdict"] = "no estimate"
            return out
        if left <= 0:
            out["verdict"] = "hours are in"
            return out
        out["per_day"] = round(left / max(1, days + 1), 2)
        if left > avail:
            out["verdict"] = "WILL MISS"
        elif left > avail * 0.6:
            out["verdict"] = "tight"
        else:
            out["verdict"] = "on track"
        return out

    def all_status(self, today: date) -> list[dict]:
        rows = [self.status(t, today) for t in self.tasks.open()]
        order = {"OVERDUE": 0, "WILL MISS": 1, "tight": 2, "on track": 3,
                 "no estimate": 4, "hours are in": 5, "no deadline": 6}
        rows.sort(key=lambda r: (order.get(r["verdict"], 9),
                                 r["days_left"] if r["days_left"] is not None else 999,
                                 r["priority"]))
        return rows

    def at_risk(self, today: date) -> list[dict]:
        return [r for r in self.all_status(today)
                if r["verdict"] in ("OVERDUE", "WILL MISS", "tight")]

    # -- how it reads ---------------------------------------------------------
    def line(self, r: dict) -> str:
        bits = []
        if r["days_left"] is not None:
            bits.append("today" if r["days_left"] == 0 else
                        f"{abs(r['days_left'])}d {'late' if r['days_left'] < 0 else 'left'}")
        if r["hours_needed"]:
            bits.append(f"{r['hours_done']:g}/{r['hours_needed']:g}h done")
        if r["per_day"]:
            bits.append(f"needs {r['per_day']:g}h/day")
        mark = {"OVERDUE": "🔴", "WILL MISS": "🔴", "tight": "🟠",
                "on track": "🟢", "hours are in": "✅"}.get(r["verdict"], "▫️")
        return f"{mark} {r['title']} — " + ", ".join(bits) if bits else f"{mark} {r['title']}"

    def describe(self, today: date, limit: int = 12) -> str:
        return "\n".join(self.line(r) for r in self.all_status(today)[:limit])

    def for_planner(self, today: date) -> str:
        """The same picture, written for the model that builds the timetable."""
        out = []
        for r in self.all_status(today)[:12]:
            bits = [f"#{r['id']} {r['title']}", f"priority {r['priority']}"]
            if r["due"]:
                bits.append(f"due {r['due']} ({r['days_left']}d)")
            if r["hours_needed"]:
                bits.append(f"{r['hours_left']:g}h of {r['hours_needed']:g}h still to do")
            if r["per_day"]:
                bits.append(f"{r['per_day']:g}h/day to finish in time")
            bits.append(r["verdict"])
            out.append("  " + " | ".join(bits))
        return "\n".join(out)
