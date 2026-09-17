"""The big things that are not today's timetable but decide the year.

A daily plan answers "what are the next six hours for". It never answers
"when did I last touch the Physics IA", and that is the question that
actually decides whether the IA gets done. Ten long-running tasks, each easy
to put off for one more day, is exactly the shape of thing that disappears
from a plan without anyone deciding to drop it.

So each task carries two facts the day never had:

- **when it is due**, if he has said, so the friend can count down to it;
- **when it was last actually worked on** -- meaning a checkpoint inside a
  block about it was satisfied with a photo. Planning a block does not count.
  Putting "work: physics IA" in a plan and then not doing it is precisely the
  failure this is here to notice.
"""
from __future__ import annotations

from datetime import date, datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    title        TEXT NOT NULL,
    keywords     TEXT NOT NULL DEFAULT '',   -- comma list, lowercase
    due          TEXT,                       -- ISO date, or NULL
    status       TEXT NOT NULL DEFAULT 'open',   -- open | done | dropped
    notes        TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    last_touched TEXT,                       -- ISO date of the last proven work
    closed_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
"""


def _kw(title: str, keywords) -> str:
    words = [k.strip().lower() for k in (keywords or []) if k and k.strip()]
    if not words:
        words = [title.strip().lower()]
    return ",".join(dict.fromkeys(words))


class Tasks:
    def __init__(self, db):
        self.db = db
        db.conn.executescript(SCHEMA)
        db.conn.commit()

    # -- seeding --------------------------------------------------------------
    def seed(self, items, now: datetime) -> int:
        """Load the starting list once. Never again: after that the list is his."""
        if self.db.one("SELECT 1 FROM tasks LIMIT 1"):
            return 0
        n = 0
        for it in items or []:
            if isinstance(it, str):
                it = {"title": it}
            self.add(it["title"], now, keywords=it.get("keywords"), due=it.get("due"))
            n += 1
        return n

    # -- reading --------------------------------------------------------------
    def open(self) -> list:
        # Dated ones first, soonest first; then the undated ones in the order
        # they were written down.
        return self.db.q(
            "SELECT * FROM tasks WHERE status = 'open'"
            " ORDER BY due IS NULL, due, id")

    def get(self, task_id: int):
        return self.db.one("SELECT * FROM tasks WHERE id = ?", (task_id,))

    def find(self, text: str):
        """An open task by id, or by a piece of its title."""
        t = (text or "").strip().lower()
        if not t:
            return None
        if t.isdigit():
            row = self.get(int(t))
            return row if row and row["status"] == "open" else None
        rows = self.open()
        exact = [r for r in rows if r["title"].lower() == t]
        if exact:
            return exact[0]
        part = [r for r in rows if t in r["title"].lower()
                or any(t == k for k in r["keywords"].split(","))]
        return part[0] if len(part) == 1 else None

    # -- writing --------------------------------------------------------------
    def add(self, title: str, now: datetime, keywords=None, due=None) -> int:
        cur = self.db.run(
            "INSERT INTO tasks (title, keywords, due, created_at) VALUES (?, ?, ?, ?)",
            (title.strip(), _kw(title, keywords),
             str(due) if due else None, now.isoformat()))
        return cur.lastrowid

    def set_due(self, task_id: int, due: date | None) -> bool:
        return self.db.run("UPDATE tasks SET due = ? WHERE id = ? AND status = 'open'",
                           (due.isoformat() if due else None, task_id)).rowcount > 0

    def note(self, task_id: int, text: str) -> bool:
        row = self.get(task_id)
        if not row:
            return False
        notes = (row["notes"] + "\n" if row["notes"] else "") + text.strip()
        self.db.run("UPDATE tasks SET notes = ? WHERE id = ?", (notes[-1500:], task_id))
        return True

    def close(self, task_id: int, now: datetime, status: str = "done") -> bool:
        return self.db.run(
            "UPDATE tasks SET status = ?, closed_at = ? WHERE id = ? AND status = 'open'",
            (status, now.isoformat(), task_id)).rowcount > 0

    def touch(self, ids, day: date) -> None:
        for i in ids:
            self.db.run("UPDATE tasks SET last_touched = ? WHERE id = ?"
                        " AND (last_touched IS NULL OR last_touched < ?)",
                        (day.isoformat(), i, day.isoformat()))

    # -- matching a block to a task ------------------------------------------
    def match(self, label: str) -> set[int]:
        """Which open tasks a block label is about.

        The longest keyword wins where one contains another: "work: physics IA"
        is the IA, not also "physics solve" just because both say physics.
        """
        text = (label or "").lower()
        hits = []
        for r in self.open():
            for k in filter(None, r["keywords"].split(",")):
                if k in text:
                    hits.append((k, r["id"]))
        keep = set()
        for k, tid in hits:
            if not any(k != k2 and k in k2 for k2, _ in hits):
                keep.add(tid)
        return keep

    # -- what deserves a mention ---------------------------------------------
    def stale(self, day: date, days: int) -> list:
        """Open tasks nobody has proven any work on for `days` days or more."""
        out = []
        for r in self.open():
            since = r["last_touched"] or r["created_at"][:10]
            if (day - date.fromisoformat(since)).days >= days:
                out.append(r)
        return out

    def due_within(self, day: date, days: int) -> list:
        return [r for r in self.open()
                if r["due"] and 0 <= (date.fromisoformat(r["due"]) - day).days <= days]

    def describe(self, day: date) -> str:
        """One line per open task, for the friend's context and for /tasks."""
        lines = []
        for r in self.open():
            bits = []
            if r["due"]:
                left = (date.fromisoformat(r["due"]) - day).days
                bits.append("OVERDUE" if left < 0 else
                            "due today" if left == 0 else f"due in {left}d ({r['due']})")
            else:
                bits.append("no deadline set")
            if r["last_touched"]:
                ago = (day - date.fromisoformat(r["last_touched"])).days
                bits.append("worked on today" if ago == 0 else f"last worked {ago}d ago")
            else:
                bits.append("never worked on (with proof)")
            lines.append(f"  #{r['id']} {r['title']} — {', '.join(bits)}")
        return "\n".join(lines)
